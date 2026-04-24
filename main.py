from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from playwright.async_api import Page, async_playwright


MEDIA_RE = re.compile(
    r"https?://[^'\"<>\s]+(?:\.m3u8|\.mp4)(?:\?[^'\"<>\s]*)?",
    re.IGNORECASE,
)
RELATIVE_MEDIA_RE = re.compile(
    r"(?P<quote>['\"])(?P<url>[^'\"]+\.(?:m3u8|mp4)(?:\?[^'\"]*)?)(?P=quote)",
    re.IGNORECASE,
)

DEFAULT_PROFILE_DIR = Path(".browser-profile").resolve()
DEFAULT_OUTPUT_DIR = Path("downloads").resolve()


@dataclass(frozen=True)
class MediaCandidate:
    url: str
    source: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download PKU course replay videos from course.pku.edu.cn."
    )
    parser.add_argument("url", help="Replay page URL, e.g. playVideo.action?token=...")
    parser.add_argument("--account", default=os.getenv("PKU_ACCOUNT"), help="PKU account/student ID")
    parser.add_argument(
        "--password",
        default=os.getenv("PKU_PASSWORD"),
        help="PKU password. Prefer PKU_PASSWORD or the interactive prompt.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--name", help="Output file name without extension")
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show the browser. Useful for first login, captcha, or SSO prompts.",
    )
    parser.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="Pause before closing the browser so you can inspect the page.",
    )
    parser.add_argument(
        "--print-media-url",
        action="store_true",
        help="Print the detected media URL instead of downloading.",
    )
    return parser.parse_args()


async def maybe_login(page: Page, account: str | None, password: str | None, headful: bool) -> None:
    if "course.pku.edu.cn" in page.url and "login" not in page.url.lower():
        return

    if not account:
        return

    password = password or getpass.getpass("PKU password: ")
    await dismiss_overlays(page)

    if "login" in page.url.lower():
        await page.goto("https://course.pku.edu.cn/webapps/bb-sso-BBLEARN/login.html", wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        await maybe_complete_iaaa_login(page, account, password, headful)
        return

    campus_link = page.locator("a.login_stu_a").first
    try:
        if await campus_link.is_visible(timeout=1500):
            await campus_link.click(timeout=10_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
            await maybe_complete_iaaa_login(page, account, password, headful)
            return
    except Exception:
        pass

    selectors = {
        "username": [
            'input[name="username"]',
            'input[name="userName"]',
            'input[name="uid"]',
            'input[name="j_username"]',
            'input[type="text"]',
        ],
        "password": [
            'input[name="password"]',
            'input[name="pwd"]',
            'input[name="j_password"]',
            'input[type="password"]',
        ],
        "submit": [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("登录")',
            'button:has-text("Login")',
        ],
    }

    user_box = await first_visible(page, selectors["username"])
    pass_box = await first_visible(page, selectors["password"])
    if not user_box or not pass_box:
        print("Login form was not detected. Complete login manually in the browser if needed.")
        return

    await user_box.fill(account)
    await pass_box.fill(password)
    await dismiss_overlays(page)
    submit = await first_visible(page, selectors["submit"])
    if submit:
        await submit.click(timeout=10_000)
    else:
        await pass_box.press("Enter")

    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass

    await maybe_complete_campus_login(page, account, password, headful)


async def maybe_complete_iaaa_login(
    page: Page,
    account: str | None,
    password: str | None,
    headful: bool,
) -> None:
    try:
        await page.wait_for_selector("#user_name, input[name='userName']", timeout=30_000)
    except Exception:
        return

    user_box = page.locator("#user_name, input[name='userName']").first
    pass_box = page.locator("#password, input[name='password']").first
    if account and await user_box.is_visible(timeout=5000):
        await user_box.fill(account)
    if password and await pass_box.is_visible(timeout=5000):
        await pass_box.fill(password)

    extra_challenges = [
        page.locator("#valid_code").first,
        page.locator("#sms_code").first,
        page.locator("#otp_code").first,
    ]
    challenge_visible = False
    for challenge in extra_challenges:
        try:
            if await challenge.is_visible(timeout=500):
                box = await challenge.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    challenge_visible = True
                    await challenge.focus()
                    break
        except Exception:
            continue

    if challenge_visible:
        if not headful:
            raise RuntimeError("PKU IAAA requires CAPTCHA/SMS/OTP. Re-run with --headful and complete it in the browser.")
        print("PKU IAAA challenge is open. Complete CAPTCHA/SMS/OTP in the browser; download will resume automatically.")
    else:
        await page.locator("#logon_button, input[type='submit']").first.click(timeout=10_000)

    await wait_until_authenticated(page, headful)


async def maybe_complete_campus_login(
    page: Page,
    account: str | None,
    password: str | None,
    headful: bool,
) -> None:
    body_text = ""
    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        pass
    if "验证码" not in body_text and "bb-sso-BBLEARN" not in page.url:
        return

    campus_link = page.locator("a.login_stu_a").first
    try:
        if await campus_link.is_visible(timeout=1500):
            await campus_link.click(timeout=10_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
            await maybe_complete_iaaa_login(page, account, password, headful)
            return
    except Exception:
        pass

    user_box = page.locator("#user_id").first
    pass_box = page.locator("#password").first
    captcha_box = page.locator("#captcha").first
    if account and await user_box.is_visible(timeout=3000):
        await user_box.fill(account)
    if password and await pass_box.is_visible(timeout=3000):
        await pass_box.fill(password)
    if await captcha_box.is_visible(timeout=3000):
        await captcha_box.focus()

    if not headful:
        raise RuntimeError("PKU campus login requires a captcha. Re-run with --headful and complete the captcha in the browser.")

    print("PKU captcha page is open. Complete the captcha/login in the browser; download will resume automatically.")
    for _ in range(300):
        await page.wait_for_timeout(1000)
        if "login" not in page.url.lower() and "bb-sso-BBLEARN" not in page.url:
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            return
    raise RuntimeError("Timed out waiting for manual PKU captcha login.")


async def wait_until_authenticated(page: Page, headful: bool) -> None:
    if not headful:
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        iaaa_form_visible = await page.locator("#user_name, input[name='userName']").first.is_visible(timeout=500)
        if "iaaa.pku.edu.cn" in page.url or iaaa_form_visible:
            raise RuntimeError("Login did not complete. Re-run with --headful for any interactive challenge.")
        return

    for _ in range(300):
        await page.wait_for_timeout(1000)
        if (
            "iaaa.pku.edu.cn" not in page.url
            and "login" not in page.url.lower()
            and not await page.locator("#user_name, input[name='userName']").first.is_visible(timeout=500)
        ):
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            return
    raise RuntimeError("Timed out waiting for PKU login to complete.")


async def first_visible(page: Page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=1500):
                return locator
        except Exception:
            continue
    return None


async def dismiss_overlays(page: Page) -> None:
    selectors = [
        'button:has-text("同意")',
        'button:has-text("接受")',
        'button:has-text("确定")',
        'button:has-text("我知道了")',
        'button:has-text("Agree")',
        'button:has-text("Accept")',
        'input[type="button"][value*="同意"]',
        'input[type="button"][value*="接受"]',
        '.consent-footer button',
        '.lb-wrapper button',
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=2000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def collect_media(
    url: str,
    account: str | None,
    password: str | None,
    profile_dir: Path,
    headful: bool,
    keep_open: bool,
) -> tuple[list[MediaCandidate], list[dict[str, str]]]:
    candidates: list[MediaCandidate] = []
    seen: set[str] = set()

    def add_candidate(candidate_url: str, source: str) -> None:
        candidate_url = unquote(candidate_url)
        if candidate_url not in seen:
            seen.add(candidate_url)
            candidates.append(MediaCandidate(candidate_url, source))

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=not headful,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        def inspect_response(response):
            response_url = response.url
            lowered = response_url.lower()
            if any(marker in lowered for marker in (".m3u8", ".mp4")):
                add_candidate(response_url, "network")
            content_type = response.headers.get("content-type", "").lower()
            if "mpegurl" in content_type or "video/" in content_type:
                add_candidate(response_url, f"content-type:{content_type}")

        page.on("response", inspect_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if os.getenv("PKU_DEBUG"):
            print(f"after initial goto: {page.url}", file=sys.stderr)
        await maybe_login(page, account, password, headful)
        if os.getenv("PKU_DEBUG"):
            print(f"after login: {page.url}", file=sys.stderr)
        if urlparse(page.url).netloc != urlparse(url).netloc or page.url != url:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if os.getenv("PKU_DEBUG"):
                print(f"after replay goto: {page.url}", file=sys.stderr)

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

        cookies = await context.cookies()
        if os.getenv("PKU_DEBUG"):
            print(f"candidate count: {len(candidates)}", file=sys.stderr)
        if keep_open:
            input("Press Enter to close the browser...")
        await context.close()
        return candidates, cookies


def choose_media(candidates: list[MediaCandidate]) -> MediaCandidate:
    if not candidates:
        raise RuntimeError("No media URL was detected.")
    priority = {".m3u8": 0, ".mp4": 1}
    return sorted(
        candidates,
        key=lambda item: min((rank for marker, rank in priority.items() if marker in item.url.lower()), default=5),
    )[0]


def make_output_path(output_dir: Path, page_url: str, requested_name: str | None, suffix: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    token = parse_qs(urlparse(page_url).query).get("token", ["course-replay"])[0]
    safe_name = requested_name or token[:24] or "course-replay"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_name).strip("._") or "course-replay"
    return output_dir / f"{safe_name}{suffix}"


def cookie_header(cookies: list[dict[str, str]], media_url: str) -> str:
    media_host = urlparse(media_url).hostname or ""
    pairs = []
    for cookie in cookies:
        domain = cookie.get("domain", "").lstrip(".")
        if not domain or media_host.endswith(domain):
            pairs.append(f"{cookie['name']}={cookie['value']}")
    return "; ".join(pairs)


def download(media_url: str, cookies: list[dict[str, str]], output_path: Path) -> None:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://course.pku.edu.cn/",
    }
    cookies_text = cookie_header(cookies, media_url)
    if cookies_text:
        headers["Cookie"] = cookies_text

    if ".m3u8" in media_url.lower():
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("Detected HLS video, but ffmpeg is not installed or not on PATH.")
        command = [
            ffmpeg,
            "-y",
            "-headers",
            "".join(f"{key}: {value}\r\n" for key, value in headers.items()),
            "-i",
            media_url,
            "-c",
            "copy",
            str(output_path.with_suffix(".mp4")),
        ]
        subprocess.run(command, check=True)
        return

    with httpx.stream(
        "GET",
        media_url,
        headers=headers,
        follow_redirects=True,
        timeout=None,
        verify=False,
    ) as response:
        response.raise_for_status()
        total = 0
        with output_path.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
                total += len(chunk)
                if total and total % (10 * 1024 * 1024) < len(chunk):
                    print(f"Downloaded {total / 1024 / 1024:.1f} MiB", file=sys.stderr)


async def async_main() -> None:
    args = parse_args()
    candidates, cookies = await collect_media(
        args.url,
        args.account,
        args.password,
        args.profile_dir.resolve(),
        args.headful,
        args.keep_browser_open,
    )
    media = choose_media(candidates)
    print(f"Detected media URL from {media.source}: {media.url}")
    if args.print_media_url:
        print(json.dumps([candidate.__dict__ for candidate in candidates], ensure_ascii=False, indent=2))
        return

    output_path = make_output_path(args.output_dir.resolve(), args.url, args.name, ".mp4")
    download(media.url, cookies, output_path)
    print(f"Saved to {output_path}")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
