from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, ProgressBar, Static

from pku_client import (
    Course,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PROFILE_DIR,
    PKUClient,
    Replay,
    choose_media,
    download_media,
    make_output_path,
    safe_filename,
)


class PKUDownloaderApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #notice {
        padding: 1 2;
        background: $panel;
        color: $text;
    }

    #login {
        height: auto;
        padding: 1 2;
        background: $surface;
    }

    #login Input {
        width: 20;
        margin-right: 1;
    }

    #login Button {
        width: 16;
        margin-right: 1;
    }

    #main {
        height: 1fr;
        padding: 1 2;
    }

    #courses, #replays {
        width: 1fr;
        height: 1fr;
        margin-right: 1;
    }

    #download {
        height: 8;
        padding: 1 2;
        background: $surface;
    }

    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_courses", "Refresh courses"),
        ("d", "download_selected", "Download selected"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.client: PKUClient | None = None
        self.courses: list[Course] = []
        self.replays: list[Replay] = []
        self.selected_course: Course | None = None
        self.account = ""
        self.password = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            "PKU Course Replay Downloader\n"
            "Enter your PKU account and password once. The app logs in through 校园卡用户 / IAAA and keeps only browser session data in .browser-profile/. "
            "Your password is not written to project files.",
            id="notice",
        )
        with Horizontal(id="login"):
            yield Input(placeholder="Student ID / account", id="account")
            yield Input(placeholder="Password", password=True, id="password")
            yield Button("Login / Refresh", id="login_btn", variant="primary")
            yield Button("Headful Login", id="headful_btn")
        with Horizontal(id="main"):
            with Vertical(id="courses"):
                yield Label("Courses")
                yield DataTable(id="course_table", cursor_type="row")
            with Vertical(id="replays"):
                yield Label("Course Replays")
                yield DataTable(id="replay_table", cursor_type="row")
        with Vertical(id="download"):
            yield Label("Download")
            yield ProgressBar(id="progress", total=100)
            yield Static("Idle", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#course_table", DataTable).add_columns("Course", "ID")
        self.query_one("#replay_table", DataTable).add_columns("Replay", "Time", "Teacher")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login_btn":
            await self.load_courses(headful=False)
        elif event.button.id == "headful_btn":
            await self.load_courses(headful=True)

    async def action_refresh_courses(self) -> None:
        await self.load_courses(headful=False)

    async def action_download_selected(self) -> None:
        await self.download_selected()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "course_table":
            index = event.cursor_row
            if 0 <= index < len(self.courses):
                self.selected_course = self.courses[index]
                await self.load_replays(self.selected_course)
        elif event.data_table.id == "replay_table":
            await self.download_selected()

    async def load_courses(self, headful: bool) -> None:
        self.account = self.query_one("#account", Input).value.strip()
        self.password = self.query_one("#password", Input).value
        self.set_status("Logging in and loading courses...")
        self.query_one("#progress", ProgressBar).update(progress=0)
        try:
            await self.close_client()
            self.client = await PKUClient(DEFAULT_PROFILE_DIR, headful=headful).__aenter__()
            self.courses = await self.client.list_courses(self.account or None, self.password or None)
            table = self.query_one("#course_table", DataTable)
            table.clear()
            for course in self.courses:
                table.add_row(course.name, course.id)
            self.set_status(f"Loaded {len(self.courses)} courses. Select a course.")
            self.query_one("#progress", ProgressBar).update(progress=100)
        except Exception as exc:
            self.set_status(f"Error: {exc}")

    async def load_replays(self, course: Course) -> None:
        if not self.client:
            self.set_status("Login first.")
            return
        self.set_status(f"Loading replays for {course.name}...")
        self.query_one("#progress", ProgressBar).update(progress=0)
        try:
            self.replays = await self.client.list_replays(course)
            table = self.query_one("#replay_table", DataTable)
            table.clear()
            for replay in self.replays:
                table.add_row(replay.title, replay.record_time, replay.teacher)
            self.set_status(f"Loaded {len(self.replays)} replays.")
            self.query_one("#progress", ProgressBar).update(progress=100)
        except Exception as exc:
            self.set_status(f"Error: {exc}")

    async def download_selected(self) -> None:
        table = self.query_one("#replay_table", DataTable)
        index = table.cursor_row
        if not self.client or not (0 <= index < len(self.replays)):
            self.set_status("Select a replay first.")
            return
        replay = self.replays[index]
        course_part = safe_filename(self.selected_course.name if self.selected_course else "course")
        replay_part = safe_filename(replay.display_name)
        output_path = make_output_path(DEFAULT_OUTPUT_DIR / course_part, replay.url, replay_part)
        self.set_status("Finding media playlist...")
        self.query_one("#progress", ProgressBar).update(progress=0)
        try:
            candidates, cookies = await self.client.collect_media(replay.url)
            media = choose_media(candidates)
            self.set_status(f"Downloading {replay.display_name}")

            async def update_progress(fraction: float | None, message: str) -> None:
                if fraction is not None:
                    self.query_one("#progress", ProgressBar).update(progress=max(0, min(100, fraction * 100)))
                self.set_status(f"{message} | {output_path.name}")

            saved = await download_media(media.url, cookies, output_path, update_progress)
            self.query_one("#progress", ProgressBar).update(progress=100)
            self.set_status(f"Saved: {saved}")
        except Exception as exc:
            self.set_status(f"Error: {exc}")

    def set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    async def close_client(self) -> None:
        if self.client:
            await self.client.__aexit__(None, None, None)
            self.client = None

    async def on_unmount(self) -> None:
        await self.close_client()


def main() -> None:
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print("usage: pku-video-tui\n\nOpen the PKU course replay downloader terminal UI.")
        return
    PKUDownloaderApp().run()


if __name__ == "__main__":
    main()
