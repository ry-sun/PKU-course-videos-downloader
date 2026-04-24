from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright


PORTAL_URL = "https://course.pku.edu.cn/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1"
SSO_URL = "https://course.pku.edu.cn/webapps/bb-sso-BBLEARN/login.html"
DEFAULT_PROFILE_DIR = Path(".browser-profile").resolve()
DEFAULT_OUTPUT_DIR = Path("downloads").resolve()

MEDIA_RE = re.compile(
    r"https?://[^'\"<>\s]+(?:\.m3u8|\.mp4)(?:\?[^'\"<>\s]*)?",
    re.IGNORECASE,
)
RELATIVE_MEDIA_RE = re.compile(
    r"(?P<quote>['\"])(?P<url>[^'\"]+\.(?:m3u8|mp4)(?:\?[^'\"]*)?)(?P=quote)",
    re.IGNORECASE,
)
COURSE_ID_RE = re.compile(r"key=(_\d+_1)")


@dataclass(frozen=True)
class MediaCandidate:
    url: str
    source: str


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class Replay:
    title: str
    record_time: str
    teacher: str
    url: str

    @property
    def display_name(self) -> str:
        parts = [self.title]
        if self.record_time:
            parts.append(self.record_time)
        if self.teacher:
            parts.append(self.teacher)
        return " | ".join(parts)


ProgressCallback = Callable[[float | None, str], Awaitable[None] | None]


def safe_filename(name: str, fallback: str = "course-replay") -> str:
    safe = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", name).strip("._")
    return safe or fallback


def make_output_path(output_dir: Path, page_url: str, requested_name: str | None, suffix: str = ".mp4") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    token = parse_qs(urlparse(page_url).query).get("token", ["course-replay"])[0]
    safe_name = safe_filename(requested_name or token[:24] or "course-replay")
    return output_dir / f"{safe_name}{suffix}"


def cookie_header(cookies: list[dict[str, str]], media_url: str) -> str:
    media_host = urlparse(media_url).hostname or ""
    pairs = []
    for cookie in cookies:
        domain = cookie.get("domain", "").lstrip(".")
        if not domain or media_host.endswith(domain):
            pairs.append(f"{cookie['name']}={cookie['value']}")
    return "; ".join(pairs)


def choose_media(candidates: list[MediaCandidate]) -> MediaCandidate:
    if not candidates:
        raise RuntimeError("No media URL was detected.")
    priority = {".m3u8": 0, ".mp4": 1}
    return sorted(
        candidates,
        key=lambda item: min(
            (rank for marker, rank in priority.items() if marker in item.url.lower()),
            default=5,
        ),
    )[0]


async def maybe_await(value):
    if asyncio.iscoroutine(value):
        await value


class PKUClient:
    def __init__(self, profile_dir: Path = DEFAULT_PROFILE_DIR, headful: bool = False) -> None:
        self.profile_dir = profile_dir.resolve()
        self.headful = headful
        self._playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def __aenter__(self) -> "PKUClient":
        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=not self.headful,
            accept_downloads=True,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.context:
            await self.context.close()
        if self._playwright:
            await self._playwright.stop()

    async def login(self, account: str, password: str) -> None:
        page = self._page
        await page.goto(SSO_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector("#user_name, input[name='userName']", timeout=30_000)
        await page.locator("#user_name, input[name='userName']").first.fill(account)
        await page.locator("#password, input[name='password']").first.fill(password)

        for selector in ("#valid_code", "#sms_code", "#otp_code"):
            challenge = page.locator(selector).first
            try:
                if await challenge.is_visible(timeout=500):
                    box = await challenge.bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        if not self.headful:
                            raise RuntimeError("PKU IAAA requires CAPTCHA/SMS/OTP. Start with --headful and complete it in the browser.")
                        await challenge.focus()
                        await self._wait_for_authenticated()
                        return
            except RuntimeError:
                raise
            except Exception:
                continue

        await page.locator("#logon_button, input[type='submit']").first.click(timeout=10_000)
        await self._wait_for_authenticated()

    async def ensure_logged_in(self, account: str | None = None, password: str | None = None) -> None:
        page = self._page
        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
        if "login" not in page.url.lower():
            return
        if not account or not password:
            raise RuntimeError("Not logged in. Enter account/password first.")
        await self.login(account, password)

    async def list_courses(self, account: str | None = None, password: str | None = None) -> list[Course]:
        await self.ensure_logged_in(account, password)
        page = self._page
        await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60_000)
        items = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a')).map((a) => ({
              text: a.innerText.trim(),
              href: a.href
            })).filter((x) => x.text && x.href.includes('/webapps/blackboard/execute/launcher?type=Course'))
            """
        )
        courses: list[Course] = []
        seen: set[str] = set()
        for item in items:
            match = COURSE_ID_RE.search(item["href"])
            if not match:
                continue
            course_id = match.group(1)
            if course_id in seen:
                continue
            seen.add(course_id)
            courses.append(Course(id=course_id, name=item["text"], url=item["href"]))
        return courses

    async def list_replays(self, course: Course) -> list[Replay]:
        page = self._page
        video_list_url = f"https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action?course_id={course.id}&mode=view"
        await page.goto(video_list_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        rows = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('tr')).map((tr) => {
              const cells = Array.from(tr.querySelectorAll('th, td')).map((td) => td.innerText.trim());
              const link = tr.querySelector('a[href*="playVideo.action"]');
              return link ? {
                cells,
                href: link.href
              } : null;
            }).filter(Boolean)
            """
        )
        replays: list[Replay] = []
        for row in rows:
            cells = [cell for cell in row["cells"] if cell]
            title = cells[0] if cells else "课程回放"
            record_time = cells[1] if len(cells) > 1 else ""
            teacher = cells[2] if len(cells) > 2 else ""
            replays.append(Replay(title=title, record_time=record_time, teacher=teacher, url=row["href"]))
        return replays

    async def collect_media(self, url: str) -> tuple[list[MediaCandidate], list[dict[str, str]]]:
        page = self._page
        candidates: list[MediaCandidate] = []
        seen: set[str] = set()

        def add_candidate(candidate_url: str, source: str) -> None:
            candidate_url = unquote(candidate_url)
            if candidate_url not in seen:
                seen.add(candidate_url)
                candidates.append(MediaCandidate(candidate_url, source))

        def inspect_response(response):
            response_url = response.url
            lowered = response_url.lower()
            if any(marker in lowered for marker in (".m3u8", ".mp4")):
                add_candidate(response_url, "network")
            content_type = response.headers.get("content-type", "").lower()
            if "mpegurl" in content_type or "video/" in content_type:
                add_candidate(response_url, f"content-type:{content_type}")

        page.on("response", inspect_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            html = await page.content()
            for match in MEDIA_RE.findall(html):
                add_candidate(urljoin(page.url, match), "html")
            for match in RELATIVE_MEDIA_RE.finditer(html):
                add_candidate(urljoin(page.url, match.group("url")), "html")

            for frame in page.frames:
                try:
                    frame_html = await frame.content()
                except Exception:
                    continue
                for match in MEDIA_RE.findall(frame_html):
                    add_candidate(urljoin(frame.url, match), "frame")
                for match in RELATIVE_MEDIA_RE.finditer(frame_html):
                    add_candidate(urljoin(frame.url, match.group("url")), "frame")

            media_from_dom = await page.evaluate(
                """
                () => Array.from(document.querySelectorAll('video, audio, source'))
                  .map((el) => el.currentSrc || el.src)
                  .filter(Boolean)
                """
            )
            for media_url in media_from_dom:
                add_candidate(urljoin(page.url, media_url), "dom")

            cookies = await self._context.cookies()
            return candidates, cookies
        finally:
            page.remove_listener("response", inspect_response)

    @property
    def _page(self) -> Page:
        if not self.page:
            raise RuntimeError("PKUClient is not started.")
        return self.page

    @property
    def _context(self) -> BrowserContext:
        if not self.context:
            raise RuntimeError("PKUClient is not started.")
        return self.context

    async def _wait_for_authenticated(self) -> None:
        page = self._page
        if self.headful:
            for _ in range(300):
                await page.wait_for_timeout(1000)
                if not await page.locator("#user_name, input[name='userName']").first.is_visible(timeout=500):
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10_000)
                    except Exception:
                        pass
                    return
            raise RuntimeError("Timed out waiting for PKU login to complete.")

        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        if await page.locator("#user_name, input[name='userName']").first.is_visible(timeout=500):
            raise RuntimeError("Login did not complete.")


def media_headers(cookies: list[dict[str, str]], media_url: str) -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://course.pku.edu.cn/",
    }
    cookies_text = cookie_header(cookies, media_url)
    if cookies_text:
        headers["Cookie"] = cookies_text
    return headers


async def probe_duration(media_url: str, cookies: list[dict[str, str]]) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    headers = "".join(f"{key}: {value}\r\n" for key, value in media_headers(cookies, media_url).items())
    proc = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-headers",
        headers,
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        media_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    try:
        value = json.loads(stdout.decode()).get("format", {}).get("duration")
        return float(value) if value else None
    except Exception:
        return None


async def download_media(
    media_url: str,
    cookies: list[dict[str, str]],
    output_path: Path,
    progress: ProgressCallback | None = None,
    pause_event: asyncio.Event | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    headers = media_headers(cookies, media_url)
    if ".m3u8" in media_url.lower():
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("Detected HLS video, but ffmpeg is not installed or not on PATH.")
        duration = await probe_duration(media_url, cookies)
        command = [
            ffmpeg,
            "-y",
            "-headers",
            "".join(f"{key}: {value}\r\n" for key, value in headers.items()),
            "-i",
            media_url,
            "-c",
            "copy",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_path.with_suffix(".mp4")),
        ]
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout
        while True:
            if pause_event:
                await pause_event.wait()
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="ignore").strip()
            if line.startswith("out_time_ms="):
                current = int(line.split("=", 1)[1]) / 1_000_000
                fraction = current / duration if duration else None
                if progress:
                    await maybe_await(progress(min(fraction, 1.0) if fraction is not None else None, f"{current / 60:.1f} min"))
            elif line == "progress=end" and progress:
                await maybe_await(progress(1.0, "Done"))
        code = await proc.wait()
        if code != 0:
            raise RuntimeError(f"ffmpeg exited with code {code}")
        return output_path.with_suffix(".mp4")

    async with httpx.AsyncClient(follow_redirects=True, timeout=None, verify=False) as client:
        async with client.stream("GET", media_url, headers=headers) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            done = 0
            with output_path.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    if pause_event:
                        await pause_event.wait()
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        await maybe_await(progress(done / total if total else None, f"{done / 1024 / 1024:.1f} MiB"))
    if progress:
        await maybe_await(progress(1.0, "Done"))
    return output_path
