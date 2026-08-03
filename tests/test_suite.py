from __future__ import annotations

import curses
from pathlib import Path
import tempfile
import unittest

from .test_git_repo import (
    test_context_is_truncated_with_notice,
    test_file_diff_is_scoped_to_path,
    test_ignores_malformed_commit_records,
    test_lists_and_reads_local_branches_without_switching,
    test_lists_changed_files,
    test_parses_commits,
    test_rejects_empty_diff_sha,
    test_rejects_non_git_directory_with_clear_message,
)
from .test_llm_client import (
    test_default_model_is_cheap,
    test_first_message_sends_context,
    test_follow_up_resends_context_and_saved_history,
    test_provider_prefix_is_preserved,
    test_api_key_hint_names_the_selected_provider,
    test_local_ollama_does_not_require_an_api_key,
)
from git_explain_tui.app import App, Conversation, Message, QUICK_ACTIONS
from git_explain_tui.git_repo import Branch, Commit, FileChange, GitRepo


class FakeClient:
    model = "gpt-5-nano"
    max_output_tokens = 600
    max_context_chars = 40000


class FakeRepo:
    def __init__(self, git_dir: Path):
        self.git_dir = git_dir


class GitExplainTests(unittest.TestCase):
    def test_commit_parsing(self) -> None:
        test_parses_commits()

    def test_malformed_commit_records(self) -> None:
        test_ignores_malformed_commit_records()

    def test_empty_diff_sha(self) -> None:
        test_rejects_empty_diff_sha()

    def test_non_git_directory_error(self) -> None:
        test_rejects_non_git_directory_with_clear_message()

    def test_branch_navigation(self) -> None:
        test_lists_and_reads_local_branches_without_switching()

    def test_reload_reads_the_selected_branch(self) -> None:
        class ReadOnlyRepo:
            root = Path("/repo")

            def branches(self):
                return [Branch("main", "abc1234", True), Branch("feature", "def5678")]

            def commits(self, ref):
                self.ref = ref
                return []

        app = object.__new__(App)
        app.repo = ReadOnlyRepo()
        app.client = type("Client", (), {"has_api_key": True})()
        app.branches = app.repo.branches()
        app.selected_branch = 1
        app.commits = []
        app.selected = 0
        app.commit_range_anchor = None
        app.commit_range = None
        app._load_diff = lambda: None

        app.reload()

        assert app.repo.ref == "feature"

    def test_changed_files(self) -> None:
        test_lists_changed_files()

    def test_file_diff_path_scope(self) -> None:
        test_file_diff_is_scoped_to_path()

    def test_context_limit(self) -> None:
        test_context_is_truncated_with_notice()

    def test_initial_chat_request(self) -> None:
        test_first_message_sends_context()

    def test_cheap_default_model(self) -> None:
        test_default_model_is_cheap()

    def test_follow_up_chat_request(self) -> None:
        test_follow_up_resends_context_and_saved_history()

    def test_provider_model_name(self) -> None:
        test_provider_prefix_is_preserved()

    def test_provider_api_key_hint(self) -> None:
        test_api_key_hint_names_the_selected_provider()

    def test_local_ollama_configuration(self) -> None:
        test_local_ollama_does_not_require_an_api_key()

    def test_quick_actions_are_available(self) -> None:
        assert "Summarize" in QUICK_ACTIONS["s"]
        assert "risks" in QUICK_ACTIONS["R"]
        assert "tests" in QUICK_ACTIONS["t"]
        assert "bug" in QUICK_ACTIONS["b"]
        assert "PR" in QUICK_ACTIONS["p"]

    def test_chat_shortcut_expands_to_an_explicit_prompt(self) -> None:
        assert App._expand_quick_action("s") == (
            "Quick action [s]: " + QUICK_ACTIONS["s"]
        )
        assert App._expand_quick_action("short summary") == "short summary"

    def test_diff_search_finds_and_cycles_matches(self) -> None:
        app = object.__new__(App)
        app.diff_lines = ["first TODO", "unchanged", "second todo"]
        app.diff_scroll = 0
        app.diff_visible_rows = 1
        app.search_text = "todo"
        app.search_matches = []
        app.search_index = -1
        app.search_active = True

        app._run_diff_search()

        assert app.search_matches == [0, 2]
        assert app.search_index == 0
        app._next_search_match(forward=True)
        assert app.search_index == 1
        assert app.diff_scroll == 2
        app._next_search_match(forward=False)
        assert app.search_index == 0

    def test_commit_filter_matches_message_sha_author_and_refs(self) -> None:
        app = object.__new__(App)
        app.commits = [
            Commit(
                sha="a" * 40,
                short_sha="aaaaaaa",
                subject="Fix renderer",
                author="Ada",
                relative_date="today",
                decorations="HEAD -> main",
            ),
            Commit(
                sha="b" * 40,
                short_sha="bbbbbbb",
                subject="Add tests",
                author="Grace",
                relative_date="yesterday",
                decorations="",
            ),
        ]

        app.commit_filter = "grace"
        assert app._filtered_commit_indices() == [1]
        app.commit_filter = "HEAD"
        assert app._filtered_commit_indices() == [0]
        app.commit_filter = "bbbb"
        assert app._filtered_commit_indices() == [1]

    def test_commit_navigation_stays_within_filtered_results(self) -> None:
        app = object.__new__(App)
        app.commits = [
            Commit("a" * 40, "aaaaaaa", "keep first", "Ada", "today"),
            Commit("b" * 40, "bbbbbbb", "skip", "Ada", "today"),
            Commit("c" * 40, "ccccccc", "keep last", "Ada", "today"),
        ]
        app.commit_filter = "keep"
        app.selected = 0
        app.selected_file = 0
        app.commit_range = None
        app._load_diff = lambda: None

        app._move_selection(1)
        assert app.selected == 2
        app._move_selection(1)
        assert app.selected == 2
        app._move_selection(-1)
        assert app.selected == 0

    def test_filter_navigation_selects_first_visible_result(self) -> None:
        app = object.__new__(App)
        app.commits = [
            Commit("a" * 40, "aaaaaaa", "skip", "Ada", "today"),
            Commit("b" * 40, "bbbbbbb", "keep first", "Ada", "today"),
            Commit("c" * 40, "ccccccc", "keep last", "Ada", "today"),
        ]
        app.commit_filter = "keep"
        app.selected = 0
        app.selected_file = 0
        app.commit_range = None
        app._load_diff = lambda: None

        app._move_selection(1)

        assert app.selected == 1

    def test_applying_commit_filter_opens_chat_for_selected_match(self) -> None:
        app = object.__new__(App)
        app.commits = [
            Commit("a" * 40, "aaaaaaa", "keep", "Ada", "today"),
            Commit("b" * 40, "bbbbbbb", "skip", "Ada", "today"),
        ]
        app.commit_filter = "keep"
        app.commit_filter_active = True
        app.commit_scroll = 0
        app.selected = 0
        app.focus = "commits"
        app.status = ""

        app._apply_commit_filter()

        assert app.commit_filter_active is False
        assert app.focus == "chat"

    def test_commit_range_selects_consecutive_commits(self) -> None:
        app = object.__new__(App)
        app.commits = [
            Commit("a" * 40, "aaaaaaa", "newest", "Ada", "today"),
            Commit("b" * 40, "bbbbbbb", "middle", "Ada", "today"),
            Commit("c" * 40, "ccccccc", "oldest", "Ada", "today"),
        ]
        app.selected = 0
        app.commit_range_anchor = None
        app.commit_range = None
        app.context_mode = "file"
        app.context_mode_before_range = None
        app.status = ""
        app._load_commit_range_diff = lambda: None
        app._load_diff = lambda: None

        app._toggle_commit_range()
        assert list(app.range_preview_indices) == [0]
        app.selected = 2
        assert list(app.range_preview_indices) == [0, 1, 2]
        app._toggle_commit_range()

        assert app.commit_range == (0, 2)
        assert app.context_mode == "range"
        assert list(app.selected_range_indices) == [0, 1, 2]
        app._clear_commit_range()
        assert app.commit_range is None
        assert app.context_mode == "file"

    def test_pending_range_keeps_focus_in_commits_until_completed_or_cancelled(self) -> None:
        app = object.__new__(App)
        app.show_help = False
        app.pending_question = None
        app.pane_jump_active = False
        app.commit_range_anchor = 0
        app.focus = "commits"
        app.status = ""

        app.handle_key("\t")

        assert app.focus == "commits"
        assert "Finish the range" in app.status

        app._handle_browser_key("d")

        assert app.focus == "commits"
        assert "press x to cancel" in app.status

    def test_selected_commit_range_keeps_diff_pinned_while_browsing(self) -> None:
        app = object.__new__(App)
        app.selected = 0
        app.selected_file = 0
        app.commit_range = (0, 1)
        app._load_diff = lambda: (_ for _ in ()).throw(AssertionError("diff changed"))

        app._select(1)

        assert app.selected == 1

    def test_range_file_selection_keeps_the_range_diff_loaded(self) -> None:
        app = object.__new__(App)
        app.files = [FileChange(path="a.py", status="M")]
        app.selected_file = 0
        app.commit_range = (0, 1)
        loaded: list[bool] = []
        app._load_commit_range_diff = lambda: loaded.append(True)

        app._select_file(1)

        assert loaded == [True]

    def test_context_preview_for_file_mode(self) -> None:
        app = object.__new__(App)
        app.commits = [
            Commit(
                sha="a" * 40,
                short_sha="aaaaaaa",
                subject="Change thing",
                author="Ada",
                relative_date="today",
            )
        ]
        app.files = [FileChange(path="a.py", status="M")]
        app.selected_file = 1
        app.context_mode = "file"
        app.diff_lines = ["diff --git a/a.py b/a.py", "+x"]
        app.client = FakeClient()

        assert app._context_preview() == len("diff --git a/a.py b/a.py") + 1 + len("+x") + 1

    def test_request_preview_includes_saved_history(self) -> None:
        app = object.__new__(App)
        app.diff_lines = ["+change"]
        app.context_mode = "patch"
        app.commits = [object()]
        conversation = Conversation(
            messages=[Message("you", "Question"), Message("assistant", "Answer")]
        )

        assert app._request_context_preview(conversation) == len("+change") + 1 + len("Question") + 1 + len("Answer") + 1

    def test_clear_chat_history_keeps_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            git_dir = Path(temp_dir)
            chats = git_dir / "git-explain-tui" / "chats"
            exports = git_dir / "git-explain-tui" / "exports"
            chats.mkdir(parents=True)
            exports.mkdir()
            (chats / "one.json").write_text("{}")
            (chats / "two.json").write_text("{}")
            (exports / "answer.md").write_text("keep")
            repo = object.__new__(GitRepo)
            repo.git_dir = git_dir

            assert repo.clear_chat_history() == 2
            assert not list(chats.glob("*.json"))
            assert (exports / "answer.md").read_text() == "keep"

    def test_conversation_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = object.__new__(App)
            app.repo = FakeRepo(Path(temp_dir))
            app.commits = [
                Commit(
                    sha="a" * 40,
                    short_sha="aaaaaaa",
                    subject="Change thing",
                    author="Ada",
                    relative_date="today",
                )
            ]
            app.selected = 0
            app.context_mode = "summary"
            app.files = []
            app.selected_file = 0
            app.status = ""

            conversation = Conversation(
                messages=[
                    Message("you", "Why?", "gpt-5-nano"),
                    Message("assistant", "Because.", "gpt-5-nano"),
                ],
                loaded=True,
            )
            key = app.conversation_key
            app._save_conversation(key, conversation)

            loaded = Conversation()
            app._load_conversation(key, loaded)

            assert [(message.text, message.model) for message in loaded.messages] == [
                ("Why?", "gpt-5-nano"),
                ("Because.", "gpt-5-nano"),
            ]

    def test_answer_export_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = object.__new__(App)
            app.repo = FakeRepo(Path(temp_dir))
            app.client = FakeClient()
            app.commits = [
                Commit(
                    sha="a" * 40,
                    short_sha="aaaaaaa",
                    subject="Change thing",
                    author="Ada",
                    relative_date="today",
                )
            ]
            app.selected = 0
            app.context_mode = "file"
            app.files = [FileChange(path="a.py", status="M")]
            app.selected_file = 1

            text = app._answer_markdown("Looks good.")

            assert "# aaaaaaa Change thing" in text
            assert "- Mode: file" in text
            assert "- Scope: a.py" in text
            assert "Looks good." in text

    def test_chat_focus_treats_quick_action_letters_as_text(self) -> None:
        app = object.__new__(App)
        app.focus = "chat"
        app.show_help = False
        app.pending_question = None
        app.input_text = "in short: what is it"

        app.handle_key("n")

        assert app.input_text == "in short: what is itn"

    def test_chat_focus_treats_question_mark_as_text(self) -> None:
        app = object.__new__(App)
        app.focus = "chat"
        app.show_help = False
        app.pending_question = None
        app.input_text = "what changed"

        app.handle_key("?")

        assert app.input_text == "what changed?"
        assert app.show_help is False

    def test_tab_cycles_focus_from_chat(self) -> None:
        app = object.__new__(App)
        app.focus = "chat"
        app.show_help = False
        app.pending_question = None

        app.handle_key("\t")

        assert app.focus == "branches"

    def test_shift_tab_cycles_focus_back_to_chat(self) -> None:
        app = object.__new__(App)
        app.focus = "branches"
        app.show_help = False
        app.pending_question = None

        app.handle_key(curses.KEY_BTAB)

        assert app.focus == "chat"

    def test_ctrl_g_jumps_directly_to_chat(self) -> None:
        app = object.__new__(App)
        app.focus = "branches"
        app.show_help = False
        app.pending_question = None
        app.status = ""

        app.handle_key("\x07")
        app.handle_key("h")

        assert app.focus == "chat"
        assert app.pane_jump_active is False


if __name__ == "__main__":
    unittest.main()
