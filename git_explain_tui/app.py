from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import curses
import hashlib
import json
from pathlib import Path
import queue
import re
import subprocess
import textwrap
import threading

from .git_repo import Branch, Commit, FileChange, GitError, GitRepo
from .llm_client import LLMClient


QUICK_ACTIONS = {
    # A one-letter action expands into a normal prompt, so saved chats remain understandable.
    "s": "Summarize this change. Focus on intent, key files, and observable behavior.",
    "R": "Review this change for risks, likely bugs, migration concerns, and edge cases.",
    "t": "Suggest the most useful tests for this change. Include existing tests to run and any missing coverage.",
    "b": "Explain the bug or user-facing problem this change appears to fix, using only evidence from the diff.",
    "p": "Draft a concise PR description or commit note with summary, rationale, risks, and tests.",
}
# Ctrl+g uses an explicit map instead of hard-coded conditionals in the input loop.
PANE_JUMPS = {"b": "branches", "c": "commits", "f": "files", "d": "diff", "h": "chat"}


def re_sub_bad_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "export"


@dataclass
class Message:
    """One locally stored chat turn; model records which system produced it."""
    role: str
    text: str
    model: str = ""


@dataclass
class Conversation:
    """Lazy-loaded, commit-scoped history plus transient request state."""
    messages: list[Message] = field(default_factory=list)
    busy: bool = False
    loaded: bool = False


class App:
    """Own all curses state and coordinate Git data, chat persistence, and rendering."""
    def __init__(self, screen: curses.window, repo: GitRepo, client: LLMClient):
        self.screen = screen
        self.repo = repo
        self.client = client
        self.branches: list[Branch] = []
        self.selected_branch = 0
        self.branch_scroll = 0
        self.commits: list[Commit] = []
        self.selected = 0
        self.commit_scroll = 0
        self.commit_x_scroll = 0
        self.commit_filter = ""
        self.commit_filter_active = False
        self.commit_range_anchor: int | None = None
        self.commit_range: tuple[int, int] | None = None
        self.context_mode_before_range: str | None = None
        self.files: list[FileChange] = []
        self.selected_file = 0
        self.file_scroll = 0
        self.context_modes = ["summary", "patch", "file", "range"]
        self.context_mode = "file"
        self.large_context_threshold = 30000
        self.pending_question: str | None = None
        self.pending_context_chars = 0
        self.diff_lines: list[str] = []
        self.diff_scroll = 0
        self.diff_x_scroll = 0
        self.diff_visible_rows = 1
        self.search_active = False
        self.search_text = ""
        self.search_matches: list[int] = []
        self.search_index = -1
        self.chat_scroll = 0
        self.input_text = ""
        self.focus = "branches"
        self.pane_jump_active = False
        self.status = ""
        self.conversations: dict[str, Conversation] = {}
        self.results: queue.Queue[tuple[str, str, str, str | None]] = queue.Queue()
        self.running = True
        self.show_help = False
        self._setup()
        self.reload()

    def _setup(self) -> None:
        # A short timeout lets `run()` redraw when a background answer arrives.
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.timeout(100)
        if curses.has_colors():
            # Pairs are reused throughout draw methods; keep their meanings stable.
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)
            curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(6, curses.COLOR_MAGENTA, -1)
            curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)

    def reload(self) -> None:
        try:
            # Preserve selection by stable Git identity, never by list position.
            current_branch_name = (
                self.branches[self.selected_branch].name if self.branches else None
            )
            self.branches = self.repo.branches()
            active_index = next(
                (i for i, branch in enumerate(self.branches) if branch.current),
                0,
            )
            if current_branch_name and any(
                branch.name == current_branch_name for branch in self.branches
            ):
                self.selected_branch = next(
                    i
                    for i, branch in enumerate(self.branches)
                    if branch.name == current_branch_name
                )
            else:
                self.selected_branch = active_index
            current_sha = self.current_commit.sha if self.commits else None
            self.commits = self.repo.commits()
            # Commit positions may point to different history after a reload.
            self.commit_range_anchor = None
            self.commit_range = None
            if current_sha:
                self.selected = next(
                    (i for i, commit in enumerate(self.commits) if commit.sha == current_sha),
                    0,
                )
            else:
                self.selected = 0
            self._load_diff()
            self.status = f"Loaded {len(self.commits)} commits from {self.repo.root}"
            if not self.client.has_api_key:
                self.status += f" | {self.client.api_key_hint}"
        except GitError as exc:
            self.status = str(exc)

    @property
    def current_commit(self) -> Commit:
        return self.commits[self.selected]

    @property
    def conversation(self) -> Conversation | None:
        # A pending range has no stable endpoints, so it must not borrow a commit chat.
        if not self.commits or self.commit_range_anchor is not None:
            return None
        key = self.conversation_key
        conversation = self.conversations.setdefault(key, Conversation())
        if not conversation.loaded:
            self._load_conversation(key, conversation)
        return conversation

    @property
    def conversation_key(self) -> str:
        # File and range scope are part of identity: each gets its own AI thread.
        if self.context_mode == "range" and self.commit_range:
            newer_index, older_index = self.commit_range
            return "\0".join(
                (
                    self.commits[newer_index].sha,
                    "range",
                    self.commits[older_index].sha,
                )
            )
        parts = [self.current_commit.sha, self.context_mode]
        if self.context_mode == "file" and self.selected_file_change:
            parts.append(self.selected_file_change.path)
        return "\0".join(parts)

    @property
    def selected_file_change(self) -> FileChange | None:
        if self.selected_file <= 0:
            return None
        index = self.selected_file - 1
        if index >= len(self.files):
            return None
        return self.files[index]

    def _load_diff(self) -> None:
        # A new diff invalidates all view-local scroll and search positions.
        self.diff_scroll = 0
        self.diff_x_scroll = 0
        self.search_text = ""
        self.search_matches = []
        self.search_index = -1
        self.chat_scroll = 0
        if not self.commits:
            # Empty repositories are valid Git repositories, just without a selectable commit.
            self.files = []
            self.selected_file = 0
            self.diff_lines = ["No commits found in this repository."]
            return
        try:
            # Keep a file selected across reloads when it still exists in the diff.
            current_path = self.selected_file_change.path if self.selected_file_change else None
            self.files = self.repo.changed_files(self.current_commit.sha)
            if current_path:
                self.selected_file = next(
                    (
                        index + 1
                        for index, file_change in enumerate(self.files)
                        if file_change.path == current_path
                    ),
                    0,
                )
            else:
                self.selected_file = min(self.selected_file, len(self.files))
            path = self.selected_file_change.path if self.selected_file_change else None
            self.diff_lines = self.repo.diff(self.current_commit.sha, path=path).splitlines()
        except GitError as exc:
            self.diff_lines = [f"Could not load diff: {exc}"]

    def run(self) -> None:
        """Run one UI cycle repeatedly: accept completed work, draw, then read one key."""
        while self.running:
            self._consume_results()
            self.draw()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            self.handle_key(key)

    def handle_key(self, key: int | str) -> None:
        if self.show_help:
            self.show_help = False
            return
        if self.pending_question:
            self._handle_pending_key(key)
            return
        # A pending endpoint is deliberately modal: leaving Commit selection
        # would make it unclear which cursor position completes the range.
        if self._range_selection_pending() and key in (
            "\x07",
            "\t",
            curses.KEY_BTAB,
            "\x1b[Z",
            "/",
        ):
            self._require_range_selection_completion()
            return
        if key == "\x07":  # Ctrl+g
            self.pane_jump_active = True
            self.status = "Go to pane: b branches, c commits, f files, d diff, h chat."
            return
        if key in ("\t", curses.KEY_BTAB, "\x1b[Z"):
            self.pane_jump_active = False
            if getattr(self, "commit_filter_active", False):
                # Do not leave an invisible filter prompt intercepting keys
                # after the user changes panes.
                self.commit_filter_active = False
            order = ["branches", "commits", "files", "diff", "chat"]
            direction = -1 if key in (curses.KEY_BTAB, "\x1b[Z") else 1
            self.focus = order[(order.index(self.focus) + direction) % len(order)]
            return
        if getattr(self, "pane_jump_active", False):
            self._handle_pane_jump_key(key)
            return
        # Chat must consume normal letters before global shortcuts see them.
        if self.focus == "chat":
            self._handle_chat_key(key)
            return
        if self.commit_filter_active:
            self._handle_commit_filter_key(key)
            return
        if self.search_active:
            self._handle_search_key(key)
            return
        if key == "?":
            self.show_help = True
            return
        if key == "/":
            if self.focus == "commits":
                self._start_commit_filter()
            else:
                self._start_diff_search()
            return
        if key in ("n", "N"):
            self._next_search_match(forward=key == "n")
            return
        if key == "y":
            self._copy_last_answer()
            return
        if key == "Y":
            self._copy_commit_context()
            return
        if key == "e":
            self._export_last_answer()
            return
        if key in QUICK_ACTIONS:
            self._submit_question(QUICK_ACTIONS[key])
            return
        if key == "m":
            self._cycle_context_mode()
            return

        if self.focus == "branches":
            self._handle_branch_key(key)
        elif self.focus == "files":
            self._handle_file_key(key)
        elif self.focus == "diff":
            self._handle_diff_key(key)
        else:
            self._handle_browser_key(key)

    def _handle_pane_jump_key(self, key: int | str) -> None:
        """Finish the two-key Ctrl+g pane jump without leaking its key into another handler."""
        self.pane_jump_active = False
        if key == "\x1b":
            self.status = "Pane jump cancelled."
            return
        pane = PANE_JUMPS.get(key.lower()) if isinstance(key, str) else None
        if pane:
            self.focus = pane
            self.status = f"Focused {pane}."
        else:
            self.status = "Pane jump cancelled: use b, c, f, d, or h."

    def _handle_branch_key(self, key: int | str) -> None:
        if key in ("q", "Q"):
            self.running = False
        elif key in ("j", curses.KEY_DOWN) and self.branches:
            self.selected_branch = min(
                len(self.branches) - 1, self.selected_branch + 1
            )
        elif key in ("k", curses.KEY_UP) and self.branches:
            self.selected_branch = max(0, self.selected_branch - 1)
        elif key in ("\n", "\r", curses.KEY_ENTER, curses.KEY_RIGHT, "l"):
            self._switch_selected_branch()
        elif key == "r":
            self.reload()

    def _switch_selected_branch(self) -> None:
        if not self.branches:
            return
        branch = self.branches[self.selected_branch]
        if branch.current:
            self.focus = "commits"
            self.status = f"Already on {branch.name}"
            return
        try:
            self.status = f"Switching to {branch.name}…"
            self.repo.switch_branch(branch.name)
            self.commits = []
            self.reload()
            self.focus = "commits"
            self.status = f"Switched to {branch.name}"
        except GitError as exc:
            self.status = f"Could not switch branch: {exc}"

    def _handle_browser_key(self, key: int | str) -> None:
        if key in ("q", "Q"):
            self.running = False
        elif self._range_selection_pending() and key in ("r", "d", "f"):
            self._require_range_selection_completion()
        elif key in ("j", curses.KEY_DOWN):
            self._move_selection(1)
        elif key in ("k", curses.KEY_UP):
            self._move_selection(-1)
        elif key in ("J", curses.KEY_NPAGE):
            self._move_selection(10)
        elif key in ("K", curses.KEY_PPAGE):
            self._move_selection(-10)
        elif key == "g":
            self._select_visible_commit(0)
        elif key == "G" and self.commits:
            self._select_visible_commit(-1)
        elif key == " ":
            self._toggle_commit_range()
        elif key == "x":
            self._clear_commit_range()
        elif key == "r":
            self.reload()
        elif key == "d":
            self.focus = "diff"
        elif key == "f":
            self.focus = "files"
        elif key in (curses.KEY_RIGHT, "l"):
            self.commit_x_scroll += 8
        elif key in (curses.KEY_LEFT, "h"):
            self.commit_x_scroll = max(0, self.commit_x_scroll - 8)
        elif key == "L":
            self.commit_x_scroll += 24
        elif key == "H":
            self.commit_x_scroll = max(0, self.commit_x_scroll - 24)
        elif key in ("0", "^"):
            self.commit_x_scroll = 0

    def _range_selection_pending(self) -> bool:
        """Return whether the first Space has started, but not completed, a range."""
        return getattr(self, "commit_range_anchor", None) is not None

    def _require_range_selection_completion(self) -> None:
        """Explain how to leave the short modal range-selection step."""
        self.status = "Finish the range with Space, or press x to cancel before changing panes."

    def _handle_file_key(self, key: int | str) -> None:
        if key in ("q", "Q"):
            self.running = False
        elif key == "\x1b":
            self.focus = "commits"
        elif key in ("j", curses.KEY_DOWN):
            self._select_file(self.selected_file + 1)
        elif key in ("k", curses.KEY_UP):
            self._select_file(self.selected_file - 1)
        elif key == "g":
            self._select_file(0)
        elif key == "G":
            self._select_file(len(self.files))
        elif key in ("d", curses.KEY_RIGHT, "l", "\n", "\r", curses.KEY_ENTER):
            self.focus = "diff"
        elif key == "r":
            self.reload()

    def _handle_diff_key(self, key: int | str) -> None:
        if key in ("q", "Q"):
            self.running = False
        elif key == "\x1b":
            self.focus = "commits"
        elif key in ("j", curses.KEY_DOWN):
            self.diff_scroll += 1
        elif key in ("k", curses.KEY_UP):
            self.diff_scroll = max(0, self.diff_scroll - 1)
        elif key in ("J", curses.KEY_NPAGE):
            self.diff_scroll += 10
        elif key in ("K", curses.KEY_PPAGE):
            self.diff_scroll = max(0, self.diff_scroll - 10)
        elif key in (curses.KEY_RIGHT, "l"):
            self.diff_x_scroll += 8
        elif key in (curses.KEY_LEFT, "h"):
            self.diff_x_scroll = max(0, self.diff_x_scroll - 8)
        elif key in (curses.KEY_SRIGHT, "L"):
            self.diff_x_scroll += 24
        elif key == "H":
            self.diff_x_scroll = max(0, self.diff_x_scroll - 24)
        elif key in ("0", "^"):
            self.diff_x_scroll = 0
        elif key == "g":
            self.diff_scroll = 0
        elif key == "G":
            self.diff_scroll = len(self.diff_lines)
        elif key == "r":
            self.reload()

    def _start_diff_search(self) -> None:
        # Search is a small modal editor; its input is kept separate from chat input.
        self.focus = "diff"
        self.search_active = True
        self.status = "Find text in the displayed diff; Enter searches, Esc cancels."

    def _start_commit_filter(self) -> None:
        # Filtering keeps the original commit list intact and changes only visible navigation.
        self.commit_filter_active = True
        self.status = "Filter commits by message, SHA, author, or ref; Enter closes filter."

    def _handle_commit_filter_key(self, key: int | str) -> None:
        if key == "\x1b":
            self.commit_filter_active = False
            self.status = "Commit filter cancelled."
        elif key == curses.KEY_DOWN:
            self._move_selection(1)
        elif key == curses.KEY_UP:
            self._move_selection(-1)
        elif key in ("J", curses.KEY_NPAGE):
            self._move_selection(10)
        elif key in ("K", curses.KEY_PPAGE):
            self._move_selection(-10)
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self._apply_commit_filter()
        elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
            self.commit_filter = self.commit_filter[:-1]
        elif isinstance(key, str) and key.isprintable():
            self.commit_filter += key

    def _apply_commit_filter(self) -> None:
        """Leave filter-editing mode and move to the first matching commit, if any."""
        self.commit_filter_active = False
        matches = self._filtered_commit_indices()
        self.commit_scroll = 0
        if not self.commit_filter.strip():
            self.status = f"Showing all {len(self.commits)} commits."
            return
        if not matches:
            self.status = f"No commits match {self.commit_filter!r}."
            return
        self._select(matches[0])
        self.focus = "chat"
        self.status = (
            f"Showing {len(matches)}/{len(self.commits)} matching commits. "
            "Chat is ready for the selected commit."
        )

    def _handle_search_key(self, key: int | str) -> None:
        if key == "\x1b":
            self.search_active = False
            self.status = "Search cancelled."
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self._run_diff_search()
        elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
            self.search_text = self.search_text[:-1]
        elif isinstance(key, str) and key.isprintable():
            self.search_text += key

    def _run_diff_search(self) -> None:
        """Find matching displayed lines; search intentionally does not inspect hidden files."""
        query = self.search_text.strip()
        self.search_active = False
        if not query:
            self.search_matches = []
            self.search_index = -1
            self.status = "Search needs text."
            return
        needle = query.casefold()
        self.search_matches = [
            index for index, line in enumerate(self.diff_lines) if needle in line.casefold()
        ]
        if not self.search_matches:
            self.search_index = -1
            self.status = f"No matches for {query!r}."
            return
        self.search_index = next(
            (index for index, line in enumerate(self.search_matches) if line >= self.diff_scroll),
            0,
        )
        self._jump_to_search_match()
        self.status = f"Match {self.search_index + 1}/{len(self.search_matches)} for {query!r}."

    def _next_search_match(self, forward: bool) -> None:
        if not self.search_matches:
            self.status = "No active search. Press / to search this diff."
            return
        step = 1 if forward else -1
        self.search_index = (self.search_index + step) % len(self.search_matches)
        self._jump_to_search_match()
        self.status = (
            f"Match {self.search_index + 1}/{len(self.search_matches)} "
            f"for {self.search_text!r}."
        )

    def _jump_to_search_match(self) -> None:
        line = self.search_matches[self.search_index]
        if line < self.diff_scroll:
            self.diff_scroll = line
        elif line >= self.diff_scroll + self.diff_visible_rows:
            self.diff_scroll = line - self.diff_visible_rows + 1

    def _handle_chat_key(self, key: int | str) -> None:
        # Unlike other panes, printable characters here are text, never shortcuts.
        if key == "\x1b":
            self.focus = "commits"
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self._submit_question()
        elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
            self.input_text = self.input_text[:-1]
        elif key == curses.KEY_UP:
            self.chat_scroll += 1
        elif key == curses.KEY_DOWN:
            self.chat_scroll = max(0, self.chat_scroll - 1)
        elif isinstance(key, str) and key.isprintable():
            self.input_text += key

    def _move_selection(self, delta: int) -> None:
        """Move through filtered commits when filtering, otherwise through all commits."""
        visible = self._filtered_commit_indices()
        if not visible:
            return
        try:
            position = visible.index(self.selected)
        except ValueError:
            self._select(visible[0 if delta >= 0 else -1])
            return
        self._select(visible[max(0, min(len(visible) - 1, position + delta))])

    def _select_visible_commit(self, position: int) -> None:
        visible = self._filtered_commit_indices()
        if visible:
            self._select(visible[position])

    def _filtered_commit_indices(self) -> list[int]:
        query = self.commit_filter.strip().lower()
        if not query:
            return list(range(len(self.commits)))
        return [
            index
            for index, commit in enumerate(self.commits)
            if query
            in " ".join(
                (commit.short_sha, commit.subject, commit.author, commit.decorations)
            ).lower()
        ]

    @property
    def selected_range_indices(self) -> range:
        if not self.commit_range:
            return range(0)
        newer_index, older_index = self.commit_range
        return range(newer_index, older_index + 1)

    @property
    def range_preview_indices(self) -> range:
        """Show the pending anchor-to-cursor range before it is locked."""
        if self.commit_range:
            return self.selected_range_indices
        if self.commit_range_anchor is None:
            return range(0)
        start, end = sorted((self.commit_range_anchor, self.selected))
        return range(start, end + 1)

    def _toggle_commit_range(self) -> None:
        """Use two Space presses to turn an anchor and cursor into an inclusive range."""
        if self.commit_range_anchor is None:
            self.commit_range = None
            self.context_mode_before_range = self.context_mode
            self.commit_range_anchor = self.selected
            self.status = "Range start set. Move to another commit, then press Space."
            return
        if self.commit_range_anchor == self.selected:
            self.status = "Move to a different commit to select a range."
            return
        # Git history is newest-first, so normalize indexes as (newer, older).
        self.commit_range = tuple(sorted((self.commit_range_anchor, self.selected)))
        self.commit_range_anchor = None
        # A selected range owns Chat as well as Diff; its key uses both endpoints.
        self.context_mode = "range"
        self._load_commit_range_diff()
        self.status = f"Selected {len(self.selected_range_indices)} consecutive commits."

    def _clear_commit_range(self) -> None:
        """Restore ordinary commit/file behavior after a range has owned the panes."""
        if self.commit_range or self.commit_range_anchor is not None:
            self.commit_range = None
            self.commit_range_anchor = None
            if self.context_mode_before_range:
                self.context_mode = self.context_mode_before_range
            self.context_mode_before_range = None
            self._load_diff()
            self.status = "Commit range cleared."

    @staticmethod
    def _expand_quick_action(question: str) -> str:
        """Make an exact chat shortcut explicit before it reaches the model."""
        if question not in QUICK_ACTIONS:
            return question
        return f"Quick action [{question}]: {QUICK_ACTIONS[question]}"

    def _select(self, index: int) -> None:
        if index != self.selected:
            self.selected = index
            # A completed range owns the Diff pane while commits are browsed.
            if self.commit_range:
                return
            self.selected_file = 0
            self._load_diff()

    def _load_commit_range_diff(self) -> None:
        """Keep the Diff pane pinned to the completed commit range."""
        if not self.commit_range:
            return
        newer_index, older_index = self.commit_range
        self.diff_scroll = 0
        self.diff_x_scroll = 0
        try:
            # Range file selection follows the same path-preservation rule as a commit.
            current_path = self.selected_file_change.path if self.selected_file_change else None
            self.files = self.repo.commit_range_changed_files(
                self.commits[older_index].sha,
                self.commits[newer_index].sha,
            )
            self.selected_file = next(
                (
                    index + 1
                    for index, file_change in enumerate(self.files)
                    if file_change.path == current_path
                ),
                0,
            )
            path = self.selected_file_change.path if self.selected_file_change else None
            self.diff_lines = self.repo.commit_range_diff(
                self.commits[older_index].sha,
                self.commits[newer_index].sha,
                path=path,
            ).splitlines()
        except GitError as exc:
            self.diff_lines = [f"Could not load selected range diff: {exc}"]

    def _select_file(self, index: int) -> None:
        index = max(0, min(len(self.files), index))
        if index != self.selected_file:
            self.selected_file = index
            if self.commit_range:
                self._load_commit_range_diff()
            else:
                self._load_diff()

    def _cycle_context_mode(self) -> None:
        if self.commit_range:
            self.status = "Selected commit ranges always use range chat context. Press x to clear it."
            return
        next_index = (self.context_modes.index(self.context_mode) + 1) % len(
            self.context_modes
        )
        self.context_mode = self.context_modes[next_index]
        self.status = f"Chat context mode: {self.context_mode}"

    def _submit_question(self, question: str | None = None, force: bool = False) -> None:
        question = (question if question is not None else self.input_text).strip()
        conversation = self.conversation
        if self.commit_range_anchor is not None:
            self.status = "Complete the commit range before starting a range chat."
            return
        if not question or not conversation or conversation.busy:
            return
        if not self.client.has_api_key:
            self.status = self.client.api_key_hint
            return
        question = self._expand_quick_action(question)
        sha = self.current_commit.sha
        conversation_key = self.conversation_key
        model = self.client.model
        is_follow_up = bool(conversation.messages)
        if not force:
            preview = self._request_context_preview(conversation)
            if preview >= self.large_context_threshold:
                self.pending_question = question
                self.pending_context_chars = preview
                self.status = (
                    f"Large context: ~{preview:,} chars. y send, f file mode, s summary."
                )
                return
        conversation.messages.append(Message("you", question, model))
        conversation.busy = True
        self._save_conversation(conversation_key, conversation)
        self.input_text = ""
        self.chat_scroll = 0
        self.status = "Sending commit context…" if not is_follow_up else "Sending saved chat history…"

        def worker() -> None:
            try:
                # LiteLLM works across providers, so we own the portable transcript.
                history = [(message.role, message.text) for message in conversation.messages[:-1]]
                answer = self.client.ask(
                    question,
                    commit_context=self._chat_context(sha),
                    history=history,
                )
                self.results.put((conversation_key, answer, model, None))
            except Exception as exc:
                self.results.put((conversation_key, "", model, str(exc)))

        # Network work stays off the curses thread; results are consumed in run().
        threading.Thread(target=worker, daemon=True).start()

    def _handle_pending_key(self, key: int | str) -> None:
        question = self.pending_question
        if not question:
            return
        if key in ("y", "Y"):
            self.pending_question = None
            self._submit_question(question, force=True)
        elif key == "f":
            self.pending_question = None
            self.context_mode = "file"
            self.status = "Switched to file context. Select a file or press Enter to send."
            self.focus = "files"
        elif key == "s":
            self.pending_question = None
            self.context_mode = "summary"
            self._submit_question(question, force=True)
        elif key in ("\x1b", "n", "N", "q"):
            self.pending_question = None
            self.status = "Cancelled send."

    def _chat_context(self, sha: str) -> str:
        """Ask Git for exactly the context promised by the active mode and selection."""
        max_chars = self.client.max_context_chars
        if self.context_mode == "summary":
            return self.repo.summary_context(sha, max_chars=max_chars)
        if self.context_mode == "file":
            file_change = self.selected_file_change
            if file_change:
                return self.repo.context(sha, max_chars=max_chars, path=file_change.path)
            return self.repo.summary_context(sha, max_chars=max_chars)
        if self.context_mode == "range":
            if self.commit_range:
                newer_index, older_index = self.commit_range
                return self.repo.commit_range_context(
                    self.commits[older_index].sha,
                    self.commits[newer_index].sha,
                    max_chars=max_chars,
                )
            return self.repo.range_context(max_chars=max_chars)
        return self.repo.context(sha, max_chars=max_chars)

    def _context_preview(self) -> int:
        if not self.commits:
            return 0
        if self.context_mode == "summary":
            return sum(len(line) + 1 for line in self._summary_preview_lines())
        if self.context_mode == "file":
            file_change = self.selected_file_change
            if not file_change:
                return sum(len(line) + 1 for line in self._summary_preview_lines())
            return sum(len(line) + 1 for line in self.diff_lines)
        if self.context_mode == "range":
            return self.client.max_context_chars
        return sum(len(line) + 1 for line in self.diff_lines)

    @staticmethod
    def _history_chars(conversation: Conversation | None) -> int:
        if not conversation:
            return 0
        return sum(
            len(message.text) + 1
            for message in conversation.messages
            if message.role in {"you", "assistant"}
        )

    def _request_context_preview(self, conversation: Conversation | None = None) -> int:
        """Include our portable transcript, not just the selected Git context."""
        return self._context_preview() + self._history_chars(
            self.conversation if conversation is None else conversation
        )

    def _summary_preview_lines(self) -> list[str]:
        """Mirror summary context cheaply so the UI can estimate a request before Git is called."""
        if not self.commits:
            return []
        commit = self.current_commit
        lines = [
            commit.sha,
            commit.subject,
            commit.author,
            commit.relative_date,
            commit.decorations,
        ]
        lines.extend(f"{file_change.status}\t{file_change.path}" for file_change in self.files)
        return lines

    def _preview_text(self) -> str:
        if not self.commits:
            return ""
        chars = self._request_context_preview()
        return (
            f"Context: ~{chars:,} chars | Mode: {self.context_mode} | "
            f"Model: {self.client.model} | Out: {self.client.max_output_tokens}"
        )

    def _chat_dir(self) -> Path:
        # History is per repository, not global, so separate projects cannot mix conversations.
        path = self.repo.git_dir / "git-explain-tui" / "chats"
        legacy_root = self.repo.git_dir / "git-explain"
        if legacy_root.is_dir() and not path.parent.exists():
            # Keep existing users' saved conversations when they install the renamed tool.
            legacy_root.rename(path.parent)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _export_dir(self) -> Path:
        path = self.repo.git_dir / "git-explain-tui" / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _conversation_path(self, key: str) -> Path:
        sha = key.split("\0", 1)[0]
        # The digest distinguishes file/range/mode conversations for one commit.
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        return self._chat_dir() / f"{sha}-{digest}.json"

    def _load_conversation(self, key: str, conversation: Conversation) -> None:
        # Mark first to avoid retrying a missing or malformed history file every draw.
        conversation.loaded = True
        path = self._conversation_path(key)
        if not path.exists():
            return
        try:
            # Older history files have no model field; the default keeps them readable.
            data = json.loads(path.read_text(encoding="utf-8"))
            conversation.messages = [
                Message(
                    str(item.get("role", "")),
                    str(item.get("text", "")),
                    str(item.get("model", "")),
                )
                for item in data.get("messages", [])
                if item.get("role") in {"you", "assistant", "error"}
            ]
        except (OSError, json.JSONDecodeError, TypeError):
            self.status = f"Could not load chat history: {path}"

    def _save_conversation(self, key: str, conversation: Conversation) -> None:
        # JSON is intentionally human-readable so an owner can inspect or recover history.
        path = self._conversation_path(key)
        data = {
            "key": key,
            "commit": self.current_commit.sha if self.commits else "",
            "mode": self.context_mode,
            "file": self.selected_file_change.path if self.selected_file_change else "",
            "messages": [
                {"role": message.role, "text": message.text, "model": message.model}
                for message in conversation.messages
            ],
        }
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            self.status = f"Could not save chat history: {exc}"

    def _last_answer(self) -> str | None:
        conversation = self.conversation
        if not conversation:
            return None
        for message in reversed(conversation.messages):
            if message.role == "assistant":
                return message.text
        return None

    def _copy_last_answer(self) -> None:
        answer = self._last_answer()
        if not answer:
            self.status = "No AI answer to copy yet."
            return
        self._copy_text(answer, "AI answer")

    def _copy_commit_context(self) -> None:
        if not self.commits:
            self.status = "No commit context to copy."
            return
        try:
            context = self._chat_context(self.current_commit.sha)
        except Exception as exc:
            self.status = f"Could not build context: {exc}"
            return
        self._copy_text(context, "commit context")

    def _copy_text(self, text: str, label: str) -> None:
        # macOS provides `pbcopy`; on other systems a recoverable text export is safer than failure.
        try:
            subprocess.run(
                ["pbcopy"],
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            self.status = f"Copied {label} to clipboard."
        except (OSError, subprocess.CalledProcessError):
            path = self._export_text(text, label.replace(" ", "-"), "txt")
            self.status = f"Clipboard unavailable; wrote {path}"

    def _export_last_answer(self) -> None:
        conversation = self.conversation
        answer = next(
            (message for message in reversed(conversation.messages) if message.role == "assistant"),
            None,
        ) if conversation else None
        if not answer:
            self.status = "No AI answer to export yet."
            return
        path = self._export_text(self._answer_markdown(answer.text, answer.model), "answer", "md")
        self.status = f"Exported answer to {path}"

    def _answer_markdown(self, answer: str, model: str = "") -> str:
        commit = self.current_commit
        file_change = self.selected_file_change
        scope = file_change.path if file_change else "all files"
        return (
            f"# {commit.short_sha} {commit.subject}\n\n"
            f"- Mode: {self.context_mode}\n"
            f"- Scope: {scope}\n"
            f"- Model: {model or self.client.model}\n"
            f"- Exported: {datetime.now().isoformat(timespec='seconds')}\n\n"
            f"{answer.strip()}\n"
        )

    def _export_text(self, text: str, label: str, suffix: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        short_sha = self.current_commit.short_sha if self.commits else "no-commit"
        safe_label = re_sub_bad_filename(label)
        path = self._export_dir() / f"{short_sha}-{safe_label}-{timestamp}.{suffix}"
        path.write_text(text, encoding="utf-8")
        return path

    def _consume_results(self) -> None:
        """Move background request results into the matching conversation on the UI thread."""
        while True:
            try:
                conversation_key, answer, model, error = self.results.get_nowait()
            except queue.Empty:
                return
            conversation = self.conversations.setdefault(conversation_key, Conversation())
            conversation.busy = False
            if error:
                conversation.messages.append(Message("error", error, model))
                self.status = error
            else:
                conversation.messages.append(Message("assistant", answer, model))
                self.status = "Answer received. Follow-ups reuse this saved commit conversation."
            self._save_conversation(conversation_key, conversation)

    def draw(self) -> None:
        """Redraw every pane from current state; curses does not retain a declarative layout."""
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 12 or width < 60:
            self._safe_add(0, 0, "Terminal too small — use at least 60×12.", curses.A_BOLD)
            self.screen.refresh()
            return

        left_width = max(36, min(72, width * 2 // 5))
        right_x = left_width + 1
        right_width = width - right_x
        body_top = 2
        input_y = height - 2
        status_y = height - 1
        usable = input_y - body_top
        diff_height = max(4, usable * 3 // 5)
        chat_y = body_top + diff_height + 1
        chat_height = input_y - chat_y

        title = " git-explain-tui "
        model = f" model: {self.client.model} "
        self._safe_add(0, 0, title, curses.color_pair(7) | curses.A_BOLD)
        self._safe_add(0, len(title), str(self.repo.root), curses.A_DIM)
        self._safe_add(0, max(0, width - len(model)), model, curses.A_DIM)
        self._hline(1, width)

        left_height = input_y - body_top
        branch_height = max(5, left_height // 4)
        file_height = max(5, left_height // 4)
        commit_y = body_top + branch_height + 1
        file_y = input_y - file_height
        self._draw_branches(body_top, branch_height, left_width)
        self._hline(commit_y - 1, left_width)
        # Keep the row immediately above FILES for its separator.  The commits
        # renderer includes its header in ``height`` and otherwise draws right
        # through its final row, so passing the full gap here made the last
        # highlighted commit overwrite the separator as the list scrolled.
        self._draw_commits(commit_y, max(1, file_y - commit_y - 1), left_width)
        self._hline(file_y - 1, left_width)
        self._draw_files(file_y, input_y - file_y, left_width)
        self._vline(left_width, body_top, input_y - body_top)
        self._draw_diff(body_top, diff_height, right_x, right_width)
        self._hline(chat_y - 1, width, right_x)
        self._draw_chat(chat_y, chat_height, right_x, right_width)
        input_cursor_x = self._draw_input(input_y, width, right_x)
        preview = self._preview_text()
        status = self.status
        if preview:
            status = f"{status} | {preview}" if status else preview
        self._safe_add(status_y, 0, status[: width - 1], curses.A_DIM)

        if self.show_help:
            self._draw_help(height, width)
        elif input_cursor_x is not None:
            # Drawing the status line moves curses' physical cursor.  Put it
            # back in the active question field immediately before refresh.
            self.screen.move(input_y, input_cursor_x)
        self.screen.refresh()

    @staticmethod
    def _pane_heading(text: str, active: bool, color: int) -> tuple[str, int, str]:
        """Keep pane titles colored normally and show focus as a red badge."""
        return f" {text.strip()} ", color | curses.A_BOLD, "[ACTIVE]" if active else ""

    def _draw_branches(self, y: int, height: int, width: int) -> None:
        """Render local branches and scroll the selected row into the visible viewport."""
        label, attr, active = self._pane_heading(
            "LOCAL BRANCHES", self.focus == "branches", curses.color_pair(4)
        )
        self._safe_add(y, 1, label, attr)
        self._safe_add(y, 1 + len(label), active, curses.color_pair(3) | curses.A_BOLD)
        rows = max(1, height - 1)
        if self.selected_branch < self.branch_scroll:
            self.branch_scroll = self.selected_branch
        if self.selected_branch >= self.branch_scroll + rows:
            self.branch_scroll = self.selected_branch - rows + 1
        for row, branch in enumerate(
            self.branches[self.branch_scroll : self.branch_scroll + rows]
        ):
            index = self.branch_scroll + row
            active = "*" if branch.current else " "
            marker = "›" if index == self.selected_branch else " "
            text = f"{marker}{active} {branch.name}  {branch.short_sha}"
            attr = (
                curses.color_pair(5) | curses.A_BOLD
                if index == self.selected_branch
                else (curses.color_pair(2) | curses.A_BOLD if branch.current else 0)
            )
            self._safe_add(y + 1 + row, 0, text[: width - 1].ljust(width - 1), attr)

    def _draw_commits(self, y: int, height: int, width: int) -> None:
        """Render matching commits, range highlights, and horizontal message panning."""
        visible_indices = self._filtered_commit_indices()
        count = (
            f" {len(visible_indices)}/{len(self.commits)}"
            if self.commit_filter.strip()
            else ""
        )
        selected_range = self.range_preview_indices
        range_count = len(selected_range)
        range_label = f" · {range_count} selected" if range_count else ""
        if self.commit_range_anchor is not None:
            range_label = f" · {range_count} preview"
        label, attr, active = self._pane_heading(
            "COMMITS" + count + range_label,
            self.focus == "commits",
            curses.color_pair(1),
        )
        self._safe_add(y, 1, label, attr)
        self._safe_add(y, 1 + len(label), active, curses.color_pair(3) | curses.A_BOLD)
        rows = max(1, height - 1)
        if not visible_indices:
            self.commit_scroll = 0
            self._safe_add(y + 1, 1, "No commits match this filter.", curses.A_DIM)
            return
        try:
            selected_position = visible_indices.index(self.selected)
        except ValueError:
            selected_position = 0
        if selected_position < self.commit_scroll:
            self.commit_scroll = selected_position
        if selected_position >= self.commit_scroll + rows:
            self.commit_scroll = selected_position - rows + 1
        self.commit_scroll = min(
            self.commit_scroll, max(0, len(visible_indices) - rows)
        )

        commit_texts: list[str] = []
        for index in visible_indices:
            commit = self.commits[index]
            text = f"  {commit.short_sha} {commit.subject}"
            if commit.decorations:
                text += f" ({commit.decorations})"
            commit_texts.append(text)
        longest = max((len(line) for line in commit_texts), default=0)
        max_x_scroll = max(0, longest - max(1, width - 1))
        self.commit_x_scroll = min(self.commit_x_scroll, max_x_scroll)

        for row, index in enumerate(
            visible_indices[self.commit_scroll : self.commit_scroll + rows]
        ):
            commit = self.commits[index]
            marker = "›" if index == self.selected else "◆" if index in selected_range else " "
            text = f"{marker} {commit.short_sha} {commit.subject}"
            if commit.decorations:
                text += f" ({commit.decorations})"
            if index == self.selected and index != self.commit_range_anchor:
                attr = curses.color_pair(5) | curses.A_BOLD
            elif index in selected_range:
                attr = curses.color_pair(4) | curses.A_BOLD
            else:
                attr = 0
            shown = text[self.commit_x_scroll : self.commit_x_scroll + width - 1]
            self._safe_add(y + 1 + row, 0, shown.ljust(width - 1), attr)

        if self.commit_x_scroll:
            indicator = f"x+{self.commit_x_scroll}"
            self._safe_add(y, max(1, width - len(indicator) - 1), indicator, curses.A_DIM)

    def _draw_files(self, y: int, height: int, width: int) -> None:
        """Render the synthetic all-files row followed by Git's changed paths."""
        label, attr, active = self._pane_heading(
            "FILES", self.focus == "files", curses.color_pair(4)
        )
        self._safe_add(y, 1, label, attr)
        self._safe_add(y, 1 + len(label), active, curses.color_pair(3) | curses.A_BOLD)
        rows = max(1, height - 1)
        total = len(self.files) + 1
        if self.selected_file < self.file_scroll:
            self.file_scroll = self.selected_file
        if self.selected_file >= self.file_scroll + rows:
            self.file_scroll = self.selected_file - rows + 1

        entries = [("A", "[all files]")]
        for file_change in self.files:
            path = file_change.path
            if file_change.old_path:
                path = f"{file_change.old_path} -> {file_change.path}"
            entries.append((file_change.status, path))

        for row, (status, path) in enumerate(entries[self.file_scroll : self.file_scroll + rows]):
            index = self.file_scroll + row
            marker = "›" if index == self.selected_file else " "
            text = f"{marker} {status[:3]:<3} {path}"
            attr = curses.color_pair(5) | curses.A_BOLD if index == self.selected_file else 0
            self._safe_add(y + 1 + row, 0, text[: width - 1].ljust(width - 1), attr)

        indicator = f"{min(self.selected_file + 1, total)}/{max(1, total)}"
        self._safe_add(y, max(1, width - len(indicator) - 1), indicator, curses.A_DIM)

    def _draw_diff(self, y: int, height: int, x: int, width: int) -> None:
        """Render syntax-colored patch lines with independent vertical and horizontal scroll."""
        if self.commit_range:
            newer_index, older_index = self.commit_range
            label = (
                f" DIFF RANGE {self.commits[older_index].short_sha}.."
                f"{self.commits[newer_index].short_sha} · "
                f"{len(self.selected_range_indices)} commits "
            )
        elif self.commits:
            commit = self.current_commit
            file_change = self.selected_file_change
            scope = file_change.path if file_change else "all files"
            label = (
                f" DIFF {commit.short_sha} · {scope} · "
                f"{commit.author} · {commit.relative_date} "
            )
        else:
            label = " DIFF "
        label, attr, active = self._pane_heading(
            label, self.focus == "diff", curses.color_pair(1)
        )
        self._pane_add(y, x + 1, width - 1, label, attr)
        self._pane_add(
            y, x + 1 + len(label), len(active), active, curses.color_pair(3) | curses.A_BOLD
        )
        visible = max(1, height - 1)
        self.diff_visible_rows = visible
        max_scroll = max(0, len(self.diff_lines) - visible)
        self.diff_scroll = min(self.diff_scroll, max_scroll)
        longest = max((len(line) for line in self.diff_lines), default=0)
        max_x_scroll = max(0, longest - max(1, width - 2))
        self.diff_x_scroll = min(self.diff_x_scroll, max_x_scroll)
        for row, line in enumerate(self.diff_lines[self.diff_scroll : self.diff_scroll + visible]):
            line_index = self.diff_scroll + row
            shown = line[self.diff_x_scroll : self.diff_x_scroll + width - 1]
            attr = self._diff_attr(line)
            if line_index in self.search_matches:
                attr |= curses.A_REVERSE
            self._pane_add(y + row + 1, x, width, shown, attr)
        indicator = f"{self.diff_scroll + 1}/{max(1, len(self.diff_lines))}"
        if self.search_matches and self.search_index >= 0:
            indicator = (
                f"find {self.search_index + 1}/{len(self.search_matches)} · {indicator}"
            )
        if self.diff_x_scroll:
            indicator += f" x+{self.diff_x_scroll}"
        self._pane_add(y, max(x + 1, x + width - len(indicator) - 1), len(indicator), indicator, curses.A_DIM)

    def _draw_chat(self, y: int, height: int, x: int, width: int) -> None:
        conversation = self.conversation
        label = f"CHAT [{self.context_mode}]"
        if conversation and conversation.messages:
            label += " · history saved"
        label, attr, active = self._pane_heading(
            label, self.focus == "chat", curses.color_pair(6)
        )
        self._pane_add(y, x + 1, width - 1, label, attr)
        self._pane_add(
            y, x + 1 + len(label), len(active), active, curses.color_pair(3) | curses.A_BOLD
        )
        lines: list[tuple[str, int]] = []
        if not conversation or not conversation.messages:
            if self.commit_range_anchor is not None:
                hint = "Complete the commit range to view or start its chat."
            elif not self.client.has_api_key:
                hint = self.client.api_key_hint
            else:
                hint = "Ask freely, or type s/R/t/b/p then Enter to expand a quick-action prompt."
            lines.append((hint, curses.A_DIM))
        else:
            for message in conversation.messages:
                label = {"you": "You", "assistant": "AI", "error": "Error"}[message.role]
                prefix = f"{label} [{message.model}]: " if message.model else f"{label}: "
                attr = {
                    "you": curses.color_pair(1) | curses.A_BOLD,
                    "assistant": 0,
                    "error": curses.color_pair(3),
                }[message.role]
                wrapped = self._wrap_chat_message(prefix, message.text, max(10, width - 2))
                lines.extend((line, attr) for line in wrapped)
                lines.append(("", 0))
            if conversation.busy:
                lines.append(("AI: thinking…", curses.color_pair(4)))

        visible = max(1, height - 1)
        max_scroll = max(0, len(lines) - visible)
        self.chat_scroll = min(self.chat_scroll, max_scroll)
        start = max(0, len(lines) - visible - self.chat_scroll)
        for row, (line, attr) in enumerate(lines[start : start + visible]):
            self._pane_add(y + 1 + row, x, width, line, attr)

    def _draw_input(self, y: int, width: int, x: int) -> int | None:
        """Draw the one-line modal/chat input and return the cursor column when editing."""
        pane_width = width - x
        if self.pane_jump_active:
            prompt = " Go to › b:branches c:commits f:files d:diff h:chat  Esc:cancel "
        elif self.commit_filter_active:
            prompt = " Filter commits › "
        elif self.search_active:
            prompt = " Find › "
        elif self.focus == "chat":
            prompt = " Ask › "
        else:
            prompt = " Tab: next  /: search  ?: help  q: quit "
        attr = (
            curses.color_pair(5) | curses.A_BOLD
            if self.focus == "chat" or self.search_active or self.commit_filter_active or self.pane_jump_active
            else curses.A_REVERSE
        )
        self._pane_add(y, x, pane_width, prompt, attr)
        room = pane_width - len(prompt) - 1
        if self.focus == "chat" or self.search_active or self.commit_filter_active:
            if self.commit_filter_active:
                text = self.commit_filter
            elif self.search_active:
                text = self.search_text
            else:
                text = self.input_text
            shown = text[-room:]
            self._pane_add(y, x + len(prompt), max(0, room), shown, curses.A_REVERSE)
            curses.curs_set(1)
            cursor_x = min(width - 1, x + len(prompt) + len(shown))
            self.screen.move(y, cursor_x)
            return cursor_x
        else:
            curses.curs_set(0)
            return None

    def _draw_help(self, height: int, width: int) -> None:
        """Size the help box from its content so key descriptions never overflow it."""
        bindings = [
            ("j/k or ↑/↓", "select branch/commit"),
            ("Enter", "switch selected branch"),
            ("f", "focus changed files"),
            ("m", "cycle chat context mode"),
            ("s/R/t/b/p", "summary/risks/tests/bug/PR note"),
            ("y/Y/e", "copy answer/context/export"),
            ("h/l or ←/→", "pan commits/diff"),
            ("d", "focus diff"),
            ("j/k or ↑/↓", "scroll focused diff"),
            ("J/K or PgDn/Up", "scroll diff page"),
            ("0", "reset horizontal pan"),
            ("g/G", "first/last commit"),
            ("Space then Space", "select consecutive commit range"),
            ("Range pending", "Space finishes; x cancels before changing panes"),
            ("x", "clear selected commit range"),
            ("r", "reload history"),
            ("Tab / Shift+Tab", "next / previous pane"),
            ("Ctrl+g then b/c/f/d/h", "jump to a pane"),
            ("? (outside chat)", "open help"),
            ("/ in commits", "filter commit list"),
            ("/ elsewhere", "search displayed diff"),
            ("n / N", "next / previous search match"),
            ("Enter", "send chat question"),
            ("Large send", "y send, f file, s summary"),
            ("Esc", "leave chat/diff"),
            ("q", "quit"),
        ]
        key_width = max(len(key) for key, _ in bindings) + 3
        lines = ["Keyboard", ""]
        lines.extend(f"{key:<{key_width}}{description}" for key, description in bindings)
        lines.extend(["", "Press any key to close"])
        content_width = max(len(line) for line in lines)
        box_width = min(width - 2, max(44, content_width + 4))
        box_height = min(height, len(lines) + 2)
        top = max(0, (height - box_height) // 2)
        left = max(0, (width - box_width) // 2)
        for row in range(box_height):
            self._pane_add(top + row, left, box_width, "", curses.color_pair(7))
        visible_lines = lines[: max(0, box_height - 2)]
        for row, line in enumerate(visible_lines):
            self._pane_add(
                top + 1 + row,
                left + 2,
                max(0, box_width - 4),
                line,
                curses.color_pair(7) | (curses.A_BOLD if row == 0 else 0),
            )

    @staticmethod
    def _diff_attr(line: str) -> int:
        """Map standard diff prefixes to terminal colors without parsing source languages."""
        if line.startswith("+") and not line.startswith("+++"):
            return curses.color_pair(2)
        if line.startswith("-") and not line.startswith("---"):
            return curses.color_pair(3)
        if line.startswith("@@"):
            return curses.color_pair(1)
        if line.startswith(("diff --git", "index ", "+++", "---", " rename ")):
            return curses.color_pair(4) | curses.A_BOLD
        return 0

    @staticmethod
    def _wrap_chat_message(prefix: str, text: str, width: int) -> list[str]:
        """Wrap continuation lines under the message body instead of repeating the speaker label."""
        lines: list[str] = []
        paragraphs = text.splitlines() or [""]
        for paragraph_index, paragraph in enumerate(paragraphs):
            initial = prefix if paragraph_index == 0 else " " * len(prefix)
            subsequent = " " * len(prefix)
            wrapped = textwrap.wrap(
                paragraph,
                width=width,
                initial_indent=initial,
                subsequent_indent=subsequent,
                replace_whitespace=False,
                drop_whitespace=False,
                break_long_words=True,
                break_on_hyphens=False,
            )
            lines.extend(wrapped or [initial.rstrip()])
        return lines

    def _pane_add(self, y: int, x: int, width: int, text: str, attr: int = 0) -> None:
        """Draw one clipped line inside a pane, tolerating terminal resize races."""
        height, screen_width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= screen_width or width <= 0:
            return
        room = min(width, screen_width - x)
        if room <= 0:
            return
        shown = text[:room].ljust(room)
        try:
            self.screen.addnstr(y, x, shown, room, attr)
        except curses.error:
            pass

    def _safe_add(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """Best-effort curses write: an out-of-bounds cell should never crash the UI."""
        height, width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        try:
            self.screen.addnstr(y, x, text, max(0, width - x - 1), attr)
        except curses.error:
            pass

    def _hline(self, y: int, width: int, start: int = 0) -> None:
        self._safe_add(y, start, "─" * max(0, width - start - 1), curses.A_DIM)

    def _vline(self, x: int, y: int, height: int) -> None:
        for row in range(height):
            self._safe_add(y + row, x, "│", curses.A_DIM)


def run(path: str = ".") -> None:
    """Create the repository/client dependencies and hand control to curses."""
    repo = GitRepo(path)
    client = LLMClient()
    curses.wrapper(lambda screen: App(screen, repo, client).run())
