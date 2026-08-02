from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Callable


@dataclass(frozen=True)
class Commit:
    """Small, display-friendly subset of a Git commit."""
    sha: str
    short_sha: str
    subject: str
    author: str
    relative_date: str
    decorations: str = ""


@dataclass(frozen=True)
class Branch:
    """A local branch as shown in the left-hand branch pane."""
    name: str
    short_sha: str
    current: bool = False


@dataclass(frozen=True)
class FileChange:
    """One changed path; rename/copy operations also retain their old path."""
    path: str
    status: str
    old_path: str = ""


Runner = Callable[..., subprocess.CompletedProcess]
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class GitError(RuntimeError):
    """A Git command failed in a way the UI can present to the user."""
    pass


class GitRepo:
    """Translate the application's data requests into safe, focused Git commands."""
    def __init__(self, path: str | Path = ".", runner: Runner = subprocess.run):
        self._runner = runner
        requested_path = Path(path).expanduser()
        if not requested_path.is_dir():
            raise GitError(f"Repository directory not found: {requested_path}")
        self.root = requested_path.resolve()
        # Resolve both paths through Git so worktrees store history beside their own .git.
        try:
            self.root = Path(self._git("rev-parse", "--show-toplevel").strip())
            self.git_dir = Path(self._git("rev-parse", "--git-dir").strip())
        except GitError as exc:
            raise GitError(
                f"Not a Git repository: {self.root}. Run git-explain-tui inside a Git "
                "repository or pass its path explicitly."
            ) from exc
        if not self.git_dir.is_absolute():
            self.git_dir = self.root / self.git_dir

    def _git(self, *args: str) -> str:
        # Every Git call flows here so callers receive one consistent GitError type.
        command = ["git", "-C", str(getattr(self, "root", Path.cwd())), *args]
        result = self._runner(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            message = result.stderr.strip() or "Git command failed"
            raise GitError(message)
        return result.stdout

    def clear_chat_history(self) -> int:
        """Delete this tool's saved chats, including pre-rename history, but keep exports."""
        paths = [
            path
            for name in ("git-explain-tui", "git-explain")
            for path in (self.git_dir / name / "chats").glob("*.json")
        ]
        for path in paths:
            path.unlink()
        return len(paths)

    def commits(self, limit: int = 500) -> list[Commit]:
        try:
            output = self._git(
                "log",
                f"--max-count={limit}",
                "--date=relative",
                "--pretty=format:%H%x00%h%x00%s%x00%an%x00%ad%x00%D",
            )
        except GitError as exc:
            if "does not have any commits yet" in str(exc):
                return []
            raise

        commits: list[Commit] = []
        for line in output.splitlines():
            # NUL separators keep subjects and decorations safe to parse.
            fields = line.split("\0")
            if len(fields) != 6:
                continue
            sha, short_sha, subject, author, date, decorations = fields
            if FULL_SHA_RE.match(sha):
                commits.append(
                    Commit(
                        sha=sha,
                        short_sha=short_sha,
                        subject=subject,
                        author=author,
                        relative_date=date,
                        decorations=decorations.strip(),
                    )
                )
        return commits

    @staticmethod
    def _validated_sha(sha: str) -> str:
        sha = sha.strip()
        if not FULL_SHA_RE.match(sha):
            raise GitError("No valid commit selected.")
        return sha

    def branches(self) -> list[Branch]:
        # `for-each-ref` is more predictable than parsing human-formatted `git branch`.
        output = self._git(
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)%09%(objectname:short)%09%(HEAD)",
            "refs/heads",
        )
        branches: list[Branch] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                name, short_sha, head = parts
                branches.append(Branch(name, short_sha, head == "*"))
        return branches

    def current_branch_name(self) -> str:
        return self._git("branch", "--show-current").strip()

    def switch_branch(self, name: str) -> None:
        # Validate against Git's branch list so `--` cannot be mistaken for an option.
        available = {branch.name for branch in self.branches()}
        if name not in available:
            raise GitError(f"Local branch not found: {name}")
        self._git("switch", "--quiet", "--", name)

    def changed_files(self, sha: str) -> list[FileChange]:
        sha = self._validated_sha(sha)
        output = self._git("show", "--format=", "--name-status", "-M", "-C", sha)
        return self._parse_file_changes(output)

    def commit_range_changed_files(self, older_sha: str, newer_sha: str) -> list[FileChange]:
        """Return files changed by an inclusive commit range."""
        older_sha = self._validated_sha(older_sha)
        newer_sha = self._validated_sha(newer_sha)
        output = self._git(
            "diff", "--name-status", "-M", "-C", f"{older_sha}^..{newer_sha}"
        )
        return self._parse_file_changes(output)

    @staticmethod
    def _parse_file_changes(output: str) -> list[FileChange]:
        """Parse Git's tab-separated name-status output, including renames and copies."""
        changes: list[FileChange] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                status, path = parts
                changes.append(FileChange(path=path, status=status))
            elif len(parts) == 3:
                status, old_path, path = parts
                changes.append(FileChange(path=path, status=status, old_path=old_path))
        return changes

    def diff(self, sha: str, path: str | None = None) -> str:
        # Disable external diff tools: the TUI needs stable, plain Git output to render.
        sha = self._validated_sha(sha)
        args = [
            "show",
            "--no-ext-diff",
            "--find-renames",
            "--find-copies",
            "--stat",
            "--patch",
            "--format=",
            sha,
        ]
        if path:
            args.extend(["--", path])
        return self._git(*args)

    def summary_context(self, sha: str, max_chars: int = 120_000) -> str:
        """Build low-cost context: commit metadata and statistics, without patch hunks."""
        sha = self._validated_sha(sha)
        content = self._git(
            "show",
            "--no-ext-diff",
            "--find-renames",
            "--find-copies",
            "--stat",
            "--format=fuller",
            sha,
        )
        return self._clip(content, max_chars)

    def context(
        self,
        sha: str,
        max_chars: int = 120_000,
        path: str | None = None,
    ) -> str:
        """Build patch context for one commit, optionally narrowed to one file."""
        sha = self._validated_sha(sha)
        args = [
            "show",
            "--no-ext-diff",
            "--find-renames",
            "--find-copies",
            "--format=fuller",
            "--stat",
            "--patch",
            sha,
        ]
        if path:
            args.extend(["--", path])
        content = self._git(*args)
        return self._clip(content, max_chars)

    def range_context(
        self,
        base_branch: str = "main",
        max_chars: int = 120_000,
    ) -> str:
        """Build a branch-versus-base context when no explicit commit range is selected."""
        branches = {branch.name for branch in self.branches()}
        if base_branch not in branches:
            base_branch = next(
                (branch.name for branch in self.branches() if not branch.current),
                "",
            )
        if not base_branch:
            raise GitError("No base branch available for range context.")
        head_branch = self.current_branch_name() or "HEAD"
        merge_base = self._git("merge-base", base_branch, "HEAD").strip()
        log = self._git(
            "log",
            "--date=short",
            "--pretty=format:%h %ad %an %s",
            f"{merge_base}..HEAD",
        )
        diff = self._git(
            "diff",
            "--no-ext-diff",
            "--find-renames",
            "--find-copies",
            "--stat",
            "--patch",
            f"{merge_base}..HEAD",
        )
        content = (
            f"Range: {base_branch}...{head_branch}\n"
            f"Merge base: {merge_base}\n\n"
            f"Commits:\n{log or '[No commits unique to this branch]'}\n\n"
            f"Diff:\n{diff}"
        )
        return self._clip(content, max_chars)

    def commit_range_context(
        self,
        older_sha: str,
        newer_sha: str,
        max_chars: int = 120_000,
    ) -> str:
        """Return the log and combined patch for an inclusive commit range."""
        older_sha = self._validated_sha(older_sha)
        newer_sha = self._validated_sha(newer_sha)
        # Include the older endpoint by starting the range at its parent.
        range_spec = f"{older_sha}^..{newer_sha}"
        log = self._git(
            "log",
            "--date=short",
            "--pretty=format:%h %ad %an %s",
            range_spec,
        )
        diff = self._git(
            "diff",
            "--no-ext-diff",
            "--find-renames",
            "--find-copies",
            "--stat",
            "--patch",
            range_spec,
        )
        content = (
            f"Selected commit range: {older_sha[:8]}..{newer_sha[:8]}\n\n"
            f"Commits:\n{log or '[No commits found]'}\n\nDiff:\n{diff}"
        )
        return self._clip(content, max_chars)

    def commit_range_diff(
        self, older_sha: str, newer_sha: str, path: str | None = None
    ) -> str:
        """Return the combined patch for an inclusive commit range."""
        older_sha = self._validated_sha(older_sha)
        newer_sha = self._validated_sha(newer_sha)
        args = [
            "diff",
            "--no-ext-diff",
            "--find-renames",
            "--find-copies",
            "--stat",
            "--patch",
            f"{older_sha}^..{newer_sha}",
        ]
        if path:
            args.extend(["--", path])
        return self._git(*args)

    @staticmethod
    def _clip(content: str, max_chars: int) -> str:
        """Limit context by characters and make the truncation visible to the model and user."""
        if len(content) <= max_chars:
            return content
        omitted = len(content) - max_chars
        return (
            content[:max_chars]
            + f"\n\n[Context truncated by git-explain-tui; {omitted:,} characters omitted.]"
        )
