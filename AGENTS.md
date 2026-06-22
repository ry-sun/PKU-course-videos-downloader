# Repository Guidelines

## Project Structure & Module Organization

This Python 3.12 project uses `uv`. Source files are top-level modules:

- `main.py`: CLI entry point for downloading a single replay URL.
- `pku_client.py`: shared login, course/replay discovery, media selection, and download logic.
- `tui_app.py`: Textual terminal UI and queue handling.
- `assets/`: documentation images.
- `downloads/` and `.browser-profile/`: local runtime data; keep untracked.

There is no committed test directory yet. Add tests under `tests/` and mirror module names, for example `tests/test_pku_client.py`.

## Build, Test, and Development Commands

- `uv sync`: install dependencies from `pyproject.toml` and `uv.lock`.
- `uv run playwright install chromium`: install the browser used for login automation.
- `brew install ffmpeg`: install the required HLS downloader dependency on macOS.
- `uv run pku-video-tui`: start the Textual TUI.
- `uv run pku-video '<replay-url>'`: run the CLI downloader for one replay URL.
- `uv run pku-video --print-media-url '<replay-url>'`: debug media detection.
- `uv run python -m pytest`: run tests once a `tests/` suite exists.

## Coding Style & Naming Conventions

Use standard Python style with 4-space indentation, type hints for public helpers, and `from __future__ import annotations` in modules. Keep parsed-data dataclasses small and immutable with `frozen=True`, as in `Course`, `Replay`, and `MediaCandidate`. Use `snake_case` for functions and variables, `PascalCase` for classes, and uppercase constants for shared paths or URLs.

Prefer `pathlib.Path` for filesystem paths and async Playwright/httpx APIs for network/browser work. Keep UI-specific behavior in `tui_app.py`; put reusable course, media, and download logic in `pku_client.py`.

## Testing Guidelines

Use `pytest` for new tests. Cover pure helpers such as `safe_filename`, `make_output_path`, `choose_media`, and parsing behavior before adding browser-dependent tests. Name files `test_<module>.py` and functions `test_<behavior>()`.

For browser or download changes, document manual verification in the PR, including the command run and whether login, media detection, and `.mp4` output were checked.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit prefixes, including `feat:`, `fix:`, and `chore:`. Keep that pattern, for example `fix: preserve queue priority after pause`.

Pull requests should include a summary, linked issue if applicable, commands run, and screenshots or terminal output for TUI changes. Do not include credentials, cookies, downloaded videos, `.browser-profile/`, `.env`, or `downloads/` content.

## Security & Configuration Tips

Do not hard-code PKU credentials. Use `PKU_ACCOUNT` and `PKU_PASSWORD` only in your local shell, or rely on the interactive prompt. Treat downloaded course materials as private study files and do not redistribute them.
