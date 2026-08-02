from __future__ import annotations

import argparse
from importlib.metadata import version

from .app import run
from .git_repo import GitError, GitRepo


def main() -> None:
    """Parse command-line options before starting the interactive terminal UI."""
    parser = argparse.ArgumentParser(
        prog="git-explain-tui",
        description="Browse Git commits and chat with each commit.",
    )
    parser.add_argument("repository", nargs="?", default=".", help="Git repository path")
    parser.add_argument(
        "--clear-history",
        action="store_true",
        help="delete saved AI chats for this repository, then exit",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {version('git-explain-tui')}",
    )
    args = parser.parse_args()
    try:
        if args.clear_history:
            # This mode intentionally exits before curses takes control of the terminal.
            count = GitRepo(args.repository).clear_chat_history()
            print(f"Cleared {count} saved chat{'s' if count != 1 else ''}.")
            return
        run(args.repository)
    except GitError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
