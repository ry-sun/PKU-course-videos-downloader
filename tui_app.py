from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, ProgressBar, Static

from pku_client import (
    Course,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PROFILE_DIR,
    PKUClient,
    Replay,
    choose_media,
    download_media,
    safe_filename,
)


@dataclass
class DownloadItem:
    key: str
    course: Course
    replay: Replay
    status: str = "Queued"
    progress: float = 0.0
    saved_path: Path | None = None


class QuitConfirmScreen(ModalScreen[bool]):
    CSS = """
    QuitConfirmScreen {
        align: center middle;
    }

    #quit_dialog {
        width: 56;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $warning;
    }

    #quit_dialog Button {
        width: 18;
        margin-right: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="quit_dialog"):
            yield Static("There are unfinished download tasks. Quit anyway?")
            with Horizontal():
                yield Button("Stay", id="stay", variant="primary")
                yield Button("Quit", id="quit_now", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "quit_now")


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
        width: 24;
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
        height: 18;
        padding: 1 2;
        background: $surface;
    }

    #queue_table {
        height: 11;
    }

    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_courses", "Refresh"),
        ("1", "focus_login", "Login"),
        ("2", "focus_courses", "Courses"),
        ("3", "focus_replays", "Replays"),
        ("4", "focus_queue", "Queue"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("h", "focus_courses", "Courses"),
        ("l", "focus_replays", "Replays"),
        ("space", "toggle_selected", "Select"),
        ("p", "priority_up", "Priority up"),
        ("P", "priority_down", "Priority down"),
        ("a", "pause_resume", "Pause/Resume"),
        ("d", "start_downloads", "Download"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.client: PKUClient | None = None
        self.courses: list[Course] = []
        self.replays: list[Replay] = []
        self.replay_cache: dict[str, list[Replay]] = {}
        self.selected_course: Course | None = None
        self.selected_replay_keys: list[str] = []
        self.download_items: dict[str, DownloadItem] = {}
        self.downloading = False
        self.paused = False
        self.pause_event = asyncio.Event()
        self.pause_event.set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            "PKU Course Replay Downloader\n"
            "Enter your PKU account and password once. The app uses 校园卡用户 / IAAA and keeps browser session data in .browser-profile/. "
            "1-4 switches panels; Space selects multiple replays; p/P changes priority; a pauses/resumes; d starts parallel downloads.",
            id="notice",
        )
        with Horizontal(id="login"):
            yield Input(placeholder="Student ID / account", id="account")
            yield Input(placeholder="Password", password=True, id="password")
            yield Input(value="2", placeholder="Workers", id="workers")
            yield Button("Login / Refresh", id="login_btn", variant="primary")
        with Horizontal(id="main"):
            with Vertical(id="courses"):
                yield Label("Courses")
                yield DataTable(id="course_table", cursor_type="row")
            with Vertical(id="replays"):
                yield Label("Course Replays")
                yield DataTable(id="replay_table", cursor_type="row")
        with Vertical(id="download"):
            yield Label("Download Queue")
            yield ProgressBar(id="overall_progress", total=100)
            yield DataTable(id="queue_table", cursor_type="row")
            yield Static("Idle", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#course_table", DataTable).add_columns("Course", "ID")
        self.query_one("#replay_table", DataTable).add_columns("Done", "Sel", "#", "Replay", "Time", "Teacher")
        queue = self.query_one("#queue_table", DataTable)
        queue.add_column("#", key="rank")
        queue.add_column("Replay", key="replay")
        queue.add_column("Status", key="status")
        queue.add_column("Progress", key="progress")
        self.query_one("#course_table", DataTable).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login_btn":
            await self.load_courses()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "course_table":
            await self.open_current_course()
        elif event.data_table.id == "replay_table":
            self.toggle_current_replay()

    async def action_refresh_courses(self) -> None:
        await self.load_courses()

    def action_cursor_down(self) -> None:
        table = self.active_table
        if table:
            table.action_cursor_down()

    def action_cursor_up(self) -> None:
        table = self.active_table
        if table:
            table.action_cursor_up()

    def action_focus_courses(self) -> None:
        self.query_one("#course_table", DataTable).focus()

    def action_focus_replays(self) -> None:
        self.query_one("#replay_table", DataTable).focus()

    def action_focus_login(self) -> None:
        self.query_one("#account", Input).focus()

    def action_focus_queue(self) -> None:
        self.query_one("#queue_table", DataTable).focus()

    async def action_toggle_selected(self) -> None:
        focused = self.active_table
        if focused and focused.id == "course_table":
            await self.open_current_course()
        elif focused and focused.id == "replay_table":
            self.toggle_current_replay()
        elif focused and focused.id == "queue_table":
            self.drop_current_queue_item()

    def action_priority_up(self) -> None:
        self.move_priority(-1)

    def action_priority_down(self) -> None:
        self.move_priority(1)

    def action_start_downloads(self) -> None:
        if self.downloading:
            self.set_status("Downloads are already running.")
            return
        self.paused = False
        self.pause_event.set()
        self.run_worker(self.download_selected_replays(), name="download-queue", exclusive=False)

    def action_pause_resume(self) -> None:
        if not self.downloading:
            self.set_status("No active download to pause.")
            return
        self.paused = not self.paused
        if self.paused:
            self.pause_event.clear()
            self.set_status("Downloads paused. Press a to resume.")
        else:
            self.pause_event.set()
            self.set_status("Downloads resumed.")

    def action_quit(self) -> None:
        if self.has_unfinished_tasks():
            self.push_screen(QuitConfirmScreen(), self.handle_quit_confirmation)
            return
        self.exit()

    @property
    def active_table(self) -> DataTable | None:
        focused = self.focused
        return focused if isinstance(focused, DataTable) else None

    async def load_courses(self) -> None:
        account = self.query_one("#account", Input).value.strip()
        password = self.query_one("#password", Input).value
        self.set_status("Logging in and loading courses...")
        self.query_one("#overall_progress", ProgressBar).update(progress=0)
        try:
            await self.close_client()
            self.client = await PKUClient(DEFAULT_PROFILE_DIR, headful=False).__aenter__()
            self.courses = await self.client.list_courses(account or None, password or None)
            table = self.query_one("#course_table", DataTable)
            table.clear()
            for course in self.courses:
                table.add_row(course.name, course.id)
            self.set_status(f"Loaded {len(self.courses)} courses. 2/3/4 switches panels, j/k navigates, space opens.")
            self.query_one("#overall_progress", ProgressBar).update(progress=100)
        except Exception as exc:
            self.set_status(f"Error: {exc}")

    async def open_current_course(self) -> None:
        table = self.query_one("#course_table", DataTable)
        index = table.cursor_row
        if not (0 <= index < len(self.courses)):
            return
        course = self.courses[index]
        self.selected_course = course
        await self.load_replays(course)
        self.query_one("#replay_table", DataTable).focus()

    async def load_replays(self, course: Course) -> None:
        if not self.client:
            self.set_status("Login first.")
            return
        self.set_status(f"Loading replays for {course.name}...")
        self.query_one("#overall_progress", ProgressBar).update(progress=0)
        try:
            if course.id in self.replay_cache:
                self.replays = self.replay_cache[course.id]
                source = "cache"
            else:
                self.replays = await self.client.list_replays(course)
                self.replay_cache[course.id] = self.replays
                source = "network"
            self.refresh_replay_table()
            self.set_status(f"Loaded {len(self.replays)} replays from {source}. Done flags come from the downloads folder.")
            self.query_one("#overall_progress", ProgressBar).update(progress=100)
        except Exception as exc:
            self.set_status(f"Error: {exc}")

    def refresh_replay_table(self) -> None:
        table = self.query_one("#replay_table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for replay in self.replays:
            key = self.replay_key(replay)
            selected = key in self.selected_replay_keys
            rank = self.selected_replay_keys.index(key) + 1 if selected else ""
            done = "✓" if self.output_path_for(self.selected_course, replay).exists() else ""
            table.add_row(done, "x" if selected else "", str(rank), replay.title, replay.record_time, replay.teacher)
        if self.replays:
            table.move_cursor(row=max(0, min(cursor, len(self.replays) - 1)))

    def toggle_current_replay(self) -> None:
        if not self.selected_course:
            return
        table = self.query_one("#replay_table", DataTable)
        index = table.cursor_row
        if not (0 <= index < len(self.replays)):
            return
        replay = self.replays[index]
        key = self.replay_key(replay)
        if key in self.selected_replay_keys:
            self.selected_replay_keys.remove(key)
            self.download_items.pop(key, None)
        else:
            output_path = self.output_path_for(self.selected_course, replay)
            if output_path.exists():
                self.set_status(f"Already downloaded: {output_path.name}")
                return
            self.selected_replay_keys.append(key)
            self.download_items[key] = DownloadItem(key=key, course=self.selected_course, replay=replay)
        self.refresh_replay_table()
        self.refresh_queue_table()

    def move_priority(self, direction: int) -> None:
        key = self.current_priority_key()
        if key not in self.selected_replay_keys:
            return
        current = self.selected_replay_keys.index(key)
        target = max(0, min(len(self.selected_replay_keys) - 1, current + direction))
        if current == target:
            return
        self.selected_replay_keys.pop(current)
        self.selected_replay_keys.insert(target, key)
        self.refresh_replay_table()
        self.refresh_queue_table()
        self.restore_queue_cursor(key)

    def current_priority_key(self) -> str | None:
        focused = self.active_table
        if focused and focused.id == "queue_table":
            index = focused.cursor_row
            if 0 <= index < len(self.selected_replay_keys):
                return self.selected_replay_keys[index]
            return None

        table = self.query_one("#replay_table", DataTable)
        index = table.cursor_row
        if not (0 <= index < len(self.replays)):
            return None
        return self.replay_key(self.replays[index])

    def drop_current_queue_item(self) -> None:
        table = self.query_one("#queue_table", DataTable)
        index = table.cursor_row
        if not (0 <= index < len(self.selected_replay_keys)):
            return
        key = self.selected_replay_keys.pop(index)
        item = self.download_items.get(key)
        if item and item.status not in ("Queued", "Already downloaded"):
            self.selected_replay_keys.insert(index, key)
            self.set_status("Only queued items can be removed from the queue.")
            return
        self.download_items.pop(key, None)
        self.refresh_replay_table()
        self.refresh_queue_table(preferred_row=max(0, index - 1))

    def refresh_queue_table(self, preferred_key: str | None = None, preferred_row: int | None = None) -> None:
        table = self.query_one("#queue_table", DataTable)
        if preferred_key is None and table.has_focus:
            index = table.cursor_row
            if 0 <= index < len(self.selected_replay_keys):
                preferred_key = self.selected_replay_keys[index]
            else:
                preferred_row = index
        table.clear()
        for index, key in enumerate(self.selected_replay_keys, start=1):
            item = self.download_items.get(key)
            if not item:
                continue
            table.add_row(
                str(index),
                item.replay.display_name,
                item.status,
                self.progress_text(item.progress),
                key=key,
            )
        if self.selected_replay_keys:
            if preferred_key in self.selected_replay_keys:
                row = self.selected_replay_keys.index(preferred_key)
            elif preferred_row is not None:
                row = max(0, min(preferred_row, len(self.selected_replay_keys) - 1))
            else:
                row = max(0, min(table.cursor_row, len(self.selected_replay_keys) - 1))
            table.move_cursor(row=row)

    def restore_queue_cursor(self, key: str) -> None:
        if key in self.selected_replay_keys:
            self.query_one("#queue_table", DataTable).move_cursor(row=self.selected_replay_keys.index(key))

    async def download_selected_replays(self) -> None:
        if not self.client:
            self.set_status("Login first.")
            return
        if not self.selected_replay_keys:
            self.set_status("Select one or more replays first.")
            return
        self.downloading = True
        try:
            workers = self.worker_count()
            self.set_status(f"Resolving media for {len(self.selected_replay_keys)} selected replay(s)...")
            queue = [self.download_items[key] for key in self.selected_replay_keys if key in self.download_items]
            resolved = []
            for item in queue:
                await self.pause_event.wait()
                output_path = self.output_path_for(item.course, item.replay)
                if output_path.exists():
                    item.saved_path = output_path
                    item.progress = 1.0
                    item.status = "Already downloaded"
                    self.refresh_queue_table(item.key)
                    self.update_overall_progress()
                    continue
                item.status = "Resolving media"
                self.refresh_queue_table(item.key)
                candidates, cookies = await self.client.collect_media(item.replay.url)
                media = choose_media(candidates)
                resolved.append((item, media.url, cookies))

            semaphore = asyncio.Semaphore(workers)

            async def run_one(item: DownloadItem, media_url: str, cookies: list[dict[str, str]]) -> None:
                async with semaphore:
                    await self.pause_event.wait()
                    output_path = self.output_path_for(item.course, item.replay)
                    if output_path.exists():
                        item.saved_path = output_path
                        item.progress = 1.0
                        item.status = "Already downloaded"
                        self.refresh_queue_table(item.key)
                        self.update_overall_progress()
                        return
                    item.status = "Downloading"
                    item.progress = 0.0
                    self.refresh_queue_table(item.key)

                    async def update_progress(fraction: float | None, message: str) -> None:
                        if fraction is not None:
                            item.progress = max(0.0, min(1.0, fraction))
                        item.status = message
                        self.refresh_queue_table(item.key)
                        self.update_overall_progress()

                    try:
                        item.saved_path = await download_media(
                            media_url,
                            cookies,
                            output_path,
                            update_progress,
                            pause_event=self.pause_event,
                        )
                        item.progress = 1.0
                        item.status = "Done"
                    except Exception as exc:
                        item.status = f"Error: {exc}"
                    self.refresh_queue_table(item.key)
                    self.refresh_replay_table()
                    self.update_overall_progress()

            await asyncio.gather(*(run_one(*args) for args in resolved))
            self.set_status("Downloads finished.")
        finally:
            self.downloading = False
            self.paused = False
            self.pause_event.set()

    def update_overall_progress(self) -> None:
        items = [self.download_items[key] for key in self.selected_replay_keys if key in self.download_items]
        if not items:
            self.query_one("#overall_progress", ProgressBar).update(progress=0)
            return
        value = sum(item.progress for item in items) / len(items)
        self.query_one("#overall_progress", ProgressBar).update(progress=value * 100)

    def worker_count(self) -> int:
        raw = self.query_one("#workers", Input).value.strip()
        try:
            return max(1, min(8, int(raw)))
        except ValueError:
            return 2

    def progress_text(self, progress: float) -> str:
        width = 18
        filled = int(progress * width)
        return f"[{'#' * filled}{'.' * (width - filled)}] {progress * 100:5.1f}%"

    def replay_key(self, replay: Replay) -> str:
        return replay.url

    def output_path_for(self, course: Course | None, replay: Replay) -> Path:
        course_name = self.course_display_name(course)
        course_dir = DEFAULT_OUTPUT_DIR / safe_filename(course_name)
        filename = safe_filename("-".join(part for part in (course_name, replay.teacher, replay.record_time) if part))
        return course_dir / f"{filename}.mp4"

    def course_display_name(self, course: Course | None) -> str:
        if not course:
            return "course"
        return course.name.split(": ", 1)[-1]

    def set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def has_unfinished_tasks(self) -> bool:
        if self.downloading:
            return True
        unfinished = {"Queued", "Resolving media", "Downloading"}
        return any(item.status in unfinished for item in self.download_items.values())

    def handle_quit_confirmation(self, quit_now: bool) -> None:
        if quit_now:
            self.exit()

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
