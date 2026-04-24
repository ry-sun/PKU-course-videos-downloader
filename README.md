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

## Usage

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
