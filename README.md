# PKU Course Videos Downloader

Small `uv` project for downloading replay videos from `https://course.pku.edu.cn/`.

## Setup

```bash
uv sync
uv run playwright install chromium
```

`ffmpeg` is required when the replay is served as HLS (`.m3u8`):

```bash
brew install ffmpeg
```

## TUI Usage

Start the terminal UI:

```bash
uv run pku-video-tui
```

Workflow:

1. Enter your PKU account and password.
2. Press `Login / Refresh`.
3. Choose a course from the left table.
4. Use `space` to select one or more replays from the right table.
5. Press `d` to download the selected queue.

The app uses PKU's `校园卡用户` / IAAA login flow and stores browser session cookies under `.browser-profile/`. It does not write your password to project files.

Keys:

```text
j/k       move down/up
h/l       focus courses/replays
1/2/3/4   focus login/courses/replays/queue
space     open course, select/unselect replay, or remove queued item
p/P       raise/lower selected replay priority
a         pause/resume active downloads
d         download selected replays
r         refresh courses
q         quit
```

Set the `Workers` input to control maximum concurrent downloads. Replay lists are cached per course while the app is open, so returning to a course does not reload its video list.

Downloaded files are saved under `downloads/<course-name>/` as `course-teacher-time.mp4`. The replay table shows a done flag for files already present in the download folder and skips them when building the queue.

## CLI Usage

Do not put your password in source files. Use environment variables or let the tool prompt for it:

```bash
export PKU_ACCOUNT=2000012432
export PKU_PASSWORD='your-password'

uv run pku-video --headful 'https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action?token=...'
```

The first run uses a visible browser because PKU login may require SSO, captcha, or a second-factor prompt. Cookies are stored in `.browser-profile/`, so later runs can usually reuse the session:

```bash
uv run pku-video 'https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action?token=...'
```

Useful options:

```bash
uv run pku-video --print-media-url --headful '<replay-url>'
uv run pku-video --name lecture-01 '<replay-url>'
uv run pku-video --keep-browser-open --headful '<replay-url>'
```

Downloaded files are saved under `downloads/`.
