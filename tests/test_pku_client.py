from __future__ import annotations

import asyncio
import unittest

from pku_client import Course, PKUClient


class FakeReplayListPage:
    def __init__(self) -> None:
        self.url = ""
        self.visited_urls: list[str] = []
        self.evaluate_calls = 0

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url
        self.visited_urls.append(url)

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    async def evaluate(self, script: str):
        self.evaluate_calls += 1
        page_data = self._page_data()
        if "nextUrl" not in script:
            return page_data["rows"]
        return page_data

    def _page_data(self) -> dict[str, object]:
        if "page=2" in self.url:
            return {
                "rows": [
                    {
                        "cells": [f"Replay {index}", f"2026-03-{index:02d}", "Teacher B"],
                        "href": f"https://course.pku.edu.cn/playVideo.action?token={index}",
                    }
                    for index in range(26, 31)
                ],
                "nextUrl": None,
            }

        return {
            "rows": [
                {
                    "cells": [f"Replay {index}", f"2026-03-{index:02d}", "Teacher A"],
                    "href": f"https://course.pku.edu.cn/playVideo.action?token={index}",
                }
                for index in range(1, 26)
            ],
            "nextUrl": "https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action?course_id=_123_1&mode=view&page=2",
        }


class FakeJavascriptPaginationReplayListPage(FakeReplayListPage):
    def __init__(self) -> None:
        super().__init__()
        self.page_number = 1

    async def evaluate(self, script: str):
        self.evaluate_calls += 1
        if "clickReplayListNextPage" in script:
            if self.page_number == 1:
                self.page_number = 2
                self.url = f"{self.url}#page-2"
                return True
            return False

        page_data = self._page_data()
        if "nextUrl" not in script:
            return page_data["rows"]
        return page_data

    def _page_data(self) -> dict[str, object]:
        if self.page_number == 2:
            return {
                "rows": [
                    {
                        "cells": [f"Replay {index}", f"2026-03-{index:02d}", "Teacher B"],
                        "href": f"https://course.pku.edu.cn/playVideo.action?token={index}",
                    }
                    for index in range(26, 31)
                ],
                "nextUrl": None,
                "hasNextControl": False,
            }

        return {
            "rows": [
                {
                    "cells": [f"Replay {index}", f"2026-03-{index:02d}", "Teacher A"],
                    "href": f"https://course.pku.edu.cn/playVideo.action?token={index}",
                }
                for index in range(1, 26)
            ],
            "nextUrl": None,
            "hasNextControl": True,
        }


class FakeBlackboardStartIndexReplayListPage(FakeReplayListPage):
    def _page_data(self) -> dict[str, object]:
        if "startIndex=25" in self.url:
            return {
                "rows": [
                    {
                        "cells": [f"Replay {index}", f"2026-03-{index:02d}", "Teacher B"],
                        "href": f"https://course.pku.edu.cn/playVideo.action?token={index}",
                    }
                    for index in range(26, 29)
                ],
                "nextUrl": None,
                "hasNextControl": False,
            }

        next_url = None
        if "startIndex" in self._last_script:
            next_url = "https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action?sortDir=ASCENDING&numResults=25&editPaging=false&course_id=_123_1&mode=view&startIndex=25"
        return {
            "rows": [
                {
                    "cells": [f"Replay {index}", f"2026-03-{index:02d}", "Teacher A"],
                    "href": f"https://course.pku.edu.cn/playVideo.action?token={index}",
                }
                for index in range(1, 26)
            ],
            "nextUrl": next_url,
            "hasNextControl": False,
        }

    async def evaluate(self, script: str):
        self.evaluate_calls += 1
        self._last_script = script
        page_data = self._page_data()
        if "nextUrl" not in script:
            return page_data["rows"]
        return page_data


class ListReplaysTests(unittest.TestCase):
    def test_list_replays_follows_pagination_until_no_next_page(self) -> None:
        page = FakeReplayListPage()
        client = PKUClient()
        client.page = page  # type: ignore[assignment]

        replays = asyncio.run(client.list_replays(Course(id="_123_1", name="地球与行星构造_25-26学年第2学期", url="")))

        self.assertEqual(len(replays), 30)
        self.assertEqual(replays[0].title, "Replay 1")
        self.assertEqual(replays[-1].title, "Replay 30")
        self.assertEqual(
            page.visited_urls,
            [
                "https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action?course_id=_123_1&mode=view",
                "https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action?course_id=_123_1&mode=view&page=2",
            ],
        )

    def test_list_replays_clicks_javascript_pagination_controls(self) -> None:
        page = FakeJavascriptPaginationReplayListPage()
        client = PKUClient()
        client.page = page  # type: ignore[assignment]

        replays = asyncio.run(client.list_replays(Course(id="_123_1", name="地球与行星构造_25-26学年第2学期", url="")))

        self.assertEqual(len(replays), 30)
        self.assertEqual(replays[0].title, "Replay 1")
        self.assertEqual(replays[-1].title, "Replay 30")
        self.assertEqual(
            page.visited_urls,
            ["https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action?course_id=_123_1&mode=view"],
        )

    def test_list_replays_follows_blackboard_start_index_pagination_links(self) -> None:
        page = FakeBlackboardStartIndexReplayListPage()
        page._last_script = ""
        client = PKUClient()
        client.page = page  # type: ignore[assignment]

        replays = asyncio.run(client.list_replays(Course(id="_123_1", name="地球与行星构造_25-26学年第2学期", url="")))

        self.assertEqual(len(replays), 28)
        self.assertEqual(replays[0].title, "Replay 1")
        self.assertEqual(replays[-1].title, "Replay 28")
        self.assertEqual(
            page.visited_urls,
            [
                "https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action?course_id=_123_1&mode=view",
                "https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action?sortDir=ASCENDING&numResults=25&editPaging=false&course_id=_123_1&mode=view&startIndex=25",
            ],
        )
