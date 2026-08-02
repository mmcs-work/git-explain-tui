"""Entry point used only when creating platform-specific standalone binaries."""

# Importing the package rather than executing __main__.py as a file preserves
# its package-relative imports inside the PyInstaller executable.
from git_explain_tui.__main__ import main


if __name__ == "__main__":
    main()
