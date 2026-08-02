# git-explain-tui

`git-explain-tui` is a terminal UI for switching local branches,
browsing their commits and diffs, and keeping a separate AI conversation
attached to each commit.

![git-explain-tui browsing commits, diffs, and AI chat](https://raw.githubusercontent.com/mmcs-work/git-explain-tui/main/docs/git-explain-tui-interface.png)

## Quick start

1. Install the latest standalone release (no Python or uv required):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/mmcs-work/git-explain-tui/main/install.sh | sh
   ```

2. Configure an API key if you want AI chat. Browsing branches, commits, and
   diffs works without one. For multiple providers, keep each provider's normal
   environment variable; LiteLLM selects the right one from the model name:

   ```bash
   export OPENAI_API_KEY="sk-..."
   export ANTHROPIC_API_KEY="..."
   export GEMINI_API_KEY="..."
   ```

3. Start it in the Git repository you want to inspect:

   ```bash
   cd /path/to/a/git-repository
   git-explain-tui
   ```

   Or keep your current directory and pass the repository explicitly:

   ```bash
   git-explain-tui /path/to/a/git-repository
   ```

Use the arrow keys to select a branch and commit, `f` to select a changed
file, then `Tab` to reach Chat and ask a question. Press `?` outside Chat for
the complete keyboard reference.

If the path is not a Git repository, `git-explain-tui` exits with a clear error.
If no compatible API key is configured, Git browsing still works and Chat shows
the exact environment variable to set for the selected provider. Local Ollama
models do not require an API key.

### Other ways to start

- While developing this checkout, run `uv run git-explain-tui /path/to/repository`
  without globally installing it.
- Run `git-explain-tui -h` to see command-line options.
- Run `git-explain-tui -v` to confirm which installed version is running.

## Install with uv

Python 3.10+ and Git are required. The installer also brings in LiteLLM, which
provides a common API for supported hosted and local LLM providers.

**Supported platforms:** macOS and Linux (including Ubuntu). Windows is not
currently supported because the terminal UI relies on `curses`.

### Standalone executable (easiest)

Each GitHub Release includes native executables for macOS (Apple Silicon and
Intel) and Linux x86_64. This route needs Git, but not Python, uv, or pip:

```bash
curl -fsSL https://raw.githubusercontent.com/mmcs-work/git-explain-tui/main/install.sh | sh
```

The installer places `git-explain-tui` in `~/.local/bin`. If that directory is
not on your `PATH`, it prints the one-line command to add it. To choose a
specific release or installation directory, set `GIT_EXPLAIN_TUI_VERSION` or
`GIT_EXPLAIN_TUI_INSTALL_DIR` before running it. You can also download an asset
manually from [GitHub Releases](https://github.com/mmcs-work/git-explain-tui/releases).
The installer verifies the release asset against its published SHA-256 checksum.

### Install from PyPI

The normal Python-based installation command is:

```bash
uv tool install git-explain-tui
```

### Install the latest GitHub version

Until the first PyPI release, or when you want the newest unreleased changes,
install directly from this repository:

```bash
uv tool install git+https://github.com/mmcs-work/git-explain-tui.git
```

For local development from a checkout, use `uv tool install .` instead.

This installs `git-explain-tui` into uv's user-level tool environment. From any
Git repository, run:

```bash
export OPENAI_API_KEY="..."
git-explain-tui
```

To persist the API key and default model for future terminals, add them to
`~/.zshrc`:

```bash
export OPENAI_API_KEY="sk-..."
export GIT_EXPLAIN_TUI_MODEL="gpt-5-nano"
```

You can also provide a repository explicitly:

```bash
git-explain-tui /path/to/repository
```

After changing a local checkout, reinstall it with `uv tool install --force .`.

To delete all saved AI chats for the current repository (but keep exported
Markdown answers), run:

```bash
git-explain-tui --clear-history
```

Use `git-explain-tui -h` for command help and `git-explain-tui -v` for the installed
version.

The default model is `gpt-5-nano`, with a 600-token output cap and a
40,000-character commit-context cap. Override the cost/quality knobs with:

```bash
export GIT_EXPLAIN_TUI_MODEL="gpt-5-mini" # bare names select OpenAI
export GIT_EXPLAIN_TUI_MAX_OUTPUT_TOKENS="1200"
export GIT_EXPLAIN_TUI_CONTEXT_CHARS="80000"
```

For another provider, use LiteLLM's `provider/model` form and that provider's
normal API-key environment variable. For example:

```bash
export GIT_EXPLAIN_TUI_MODEL="anthropic/claude-sonnet-4-5"
export ANTHROPIC_API_KEY="..."
```

For a local Ollama model, download and test a model first:

```bash
ollama run deepseek-coder:1.3b
```

Ask a test question, then type `/bye` to exit. Configure `git-explain-tui` in the
same terminal:

```bash
export GIT_EXPLAIN_TUI_MODEL="ollama/deepseek-coder:1.3b"
export GIT_EXPLAIN_TUI_CONTEXT_CHARS="8000"
export GIT_EXPLAIN_TUI_MAX_OUTPUT_TOKENS="400"
git-explain-tui
```

No API key is required for Ollama's local endpoint. If Ollama is not already
running, start `ollama serve` in another terminal and leave it open. Because
`deepseek-coder:1.3b` is a small model, prefer `file` or `summary` context mode
over large full patches.

`GIT_EXPLAIN_TUI_API_BASE` (or the legacy `OPENAI_BASE_URL`) supports an
OpenAI-compatible/local endpoint. See LiteLLM's provider documentation for
supported model names and provider-specific variables.

## Releasing to PyPI

Maintainers can follow [RELEASING.md](RELEASING.md) to configure PyPI Trusted
Publishing and publish a tagged release. Releases use GitHub Actions' OpenID
Connect identity, so no PyPI API token needs to be saved in GitHub.

## Website on GitHub Pages

The project includes a static landing page in `docs/`. To publish it, open the
repository's **Settings → Pages**, choose **Deploy from a branch**, then select
the default branch and the `/docs` folder. GitHub Pages will serve `docs/index.html`.

## Keyboard

| Key | Action |
| --- | --- |
| `j` / `k`, arrows | Select a branch or commit |
| `Enter` on a branch | Switch to that local branch |
| `f` | Focus the changed-files pane |
| `j` / `k`, arrows in files | Select a changed file or `[all files]` |
| `h` / `l`, left/right in commits | Pan long commit messages horizontally |
| `d` | Focus the diff pane |
| `j` / `k`, arrows in diff | Scroll the diff |
| `J` / `K`, Page Down/Up | Scroll the diff by a page |
| `/` in commits | Filter commits by message, SHA, author, or ref |
| `Enter` after filtering | Open chat for the selected matching commit |
| `/` elsewhere | Search the displayed diff |
| `n` / `N` | Next / previous diff search match |
| `h` / `l`, left/right in diff | Pan long diff lines horizontally |
| `0` in commits/diff | Reset horizontal pan |
| `m` | Cycle chat context mode: summary, patch, file, range |
| `s` | Quick action: summarize |
| `R` | Quick action: review risks |
| `t` | Quick action: suggest tests |
| `b` | Quick action: explain likely bug fixed |
| `p` | Quick action: draft PR/commit note |
| `y` | Copy the latest AI answer |
| `Y` | Copy the active commit context |
| `e` | Export the latest AI answer as Markdown |
| `g` / `G` | Jump to first/last commit |
| `Space`, move, `Space` | Select an inclusive range of consecutive commits |
| Range selection pending | Finish with `Space` or cancel with `x` before changing panes |
| `x` in commits | Clear the selected commit range |
| `Tab` / `Shift+Tab` | Next / previous pane (wraps around) |
| `Ctrl+g`, then `b` / `c` / `f` / `d` / `h` | Jump to branches / commits / files / diff / chat |
| `Enter` | Submit a chat question |
| `Esc` | Return to commit browsing from diff/chat |
| `r` | Reload Git history |
| `?` | Show help |
| `q` | Quit |

## Context Modes

The default is `file` mode. Press `m` to choose what the first chat question
sends:

| Mode | Context sent |
| --- | --- |
| `summary` | Commit metadata and file stats, without patch content |
| `patch` | Full selected commit patch |
| `file` | Selected file patch, or summary if `[all files]` is selected |
| `range` | Current branch compared with `main`, using the merge base |

Important: in `file` mode, `[all files]` falls back to `summary`; it does not
send every patch. Select a file to send that file's diff, or choose `patch` to
send the complete commit diff. Use `summary` for a cheap overview, `file` for a
focused code question, and `patch` when the question requires the full change.

When you select a commit range in the commits pane, `range` instead sends that
inclusive sequence of commits and its combined diff. The Diff pane stays pinned
to that combined change while you browse the selected commits; press `x` to
return it to the current commit. The Files pane lists files changed by the
range, and selecting one scopes the pinned diff to that file.
Chat also locks to `range` mode: only history saved for those exact two range
endpoints is shown. While choosing the second endpoint, Chat remains empty so a
single-commit answer cannot be mistaken for a range answer.
Range selection is intentionally a short modal action: after the first `Space`,
move within the commit list and either press `Space` again to lock the range or
`x` to cancel it. `Tab`, `Shift+Tab`, pane jumps, filtering, reload, and
Diff/Files focus are held until you make that choice.

Follow-ups resend the selected Git context plus the saved conversation history.
That costs more than provider-specific server-side conversation state, but lets
the same persisted commit chat continue when you switch to another
LiteLLM-supported model.
Switching commits, files, or context modes switches conversations; returning to
the same combination resumes its existing conversation.

The status line shows a live preview before the first send:

```text
Context: ~18,000 chars | Mode: patch | Model: gpt-5-nano | Out: 600
```

For large first sends, `git-explain-tui` pauses instead of calling the API
immediately:

```text
y send anyway | f switch to file mode | s send summary instead | Esc cancel
```

For cost control, unusually large commit contexts are clipped at 40,000
characters by default and marked as truncated. Conversations currently live
under `.git/git-explain-tui/chats/`, and exported answers are written under
`.git/git-explain-tui/exports/`. Each saved question and answer records the model
used for that request, so a conversation remains interpretable after switching
models.
