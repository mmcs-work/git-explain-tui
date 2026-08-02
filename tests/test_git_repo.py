from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from git_explain_tui.git_repo import GitError, GitRepo


def test_rejects_non_git_directory_with_clear_message() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 128, "", "not a repository")

        try:
            GitRepo(Path(temp_dir), runner=runner)
        except GitError as exc:
            assert "Not a Git repository" in str(exc)
            assert "pass its path explicitly" in str(exc)
        else:
            raise AssertionError("Expected GitError")


def test_parses_commits() -> None:
    def runner(command, **kwargs):
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "/repo\n", "")
        output = (
            "a" * 40
            + "\0abc1234\0Add chat\0Ada\0"
            + "2 hours ago\0HEAD -> main"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    repo = GitRepo(".", runner=runner)
    commits = repo.commits()

    assert len(commits) == 1
    assert commits[0].short_sha == "abc1234"
    assert commits[0].subject == "Add chat"
    assert commits[0].decorations == "HEAD -> main"


def test_ignores_malformed_commit_records() -> None:
    def runner(command, **kwargs):
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "/repo\n", "")
        output = "broken\0bad\0Bad record\0Ada\0now\0"
        return subprocess.CompletedProcess(command, 0, output, "")

    repo = GitRepo(".", runner=runner)

    assert repo.commits() == []


def test_rejects_empty_diff_sha() -> None:
    def runner(command, **kwargs):
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "/repo\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    repo = GitRepo(".", runner=runner)

    try:
        repo.diff("")
    except GitError as exc:
        assert "No valid commit selected" in str(exc)
    else:
        raise AssertionError("Expected GitError")


def test_lists_and_switches_local_branches() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "/repo\n", "")
        if "for-each-ref" in command:
            output = "main\tabc1234\t*\nfeature/chat\tdef5678\t \n"
            return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    repo = GitRepo(".", runner=runner)
    branches = repo.branches()
    repo.switch_branch("feature/chat")

    assert branches[0].current is True
    assert branches[1].name == "feature/chat"
    assert any(
        command[-4:] == ["switch", "--quiet", "--", "feature/chat"]
        for command in calls
    )


def test_lists_changed_files() -> None:
    def runner(command, **kwargs):
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "/repo\n", "")
        output = "M\tREADME.md\nR100\told.py\tnew.py\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    repo = GitRepo(".", runner=runner)
    files = repo.changed_files("a" * 40)

    assert files[0].status == "M"
    assert files[0].path == "README.md"
    assert files[1].status == "R100"
    assert files[1].old_path == "old.py"
    assert files[1].path == "new.py"


def test_file_diff_is_scoped_to_path() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "/repo\n", "")
        return subprocess.CompletedProcess(command, 0, "diff", "")

    repo = GitRepo(".", runner=runner)
    repo.diff("a" * 40, path="README.md")

    assert calls[-1][-2:] == ["--", "README.md"]


def test_context_is_truncated_with_notice() -> None:
    def runner(command, **kwargs):
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "/repo\n", "")
        return subprocess.CompletedProcess(command, 0, "abcdefghij", "")

    repo = GitRepo(".", runner=runner)
    context = repo.context("a" * 40, max_chars=4)

    assert context.startswith("abcd")
    assert "6 characters omitted" in context
