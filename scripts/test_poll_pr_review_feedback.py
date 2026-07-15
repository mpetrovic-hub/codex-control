#!/usr/bin/env python3
"""Focused regression tests for head-scoped Codex review polling."""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import unittest
from unittest import mock


SCRIPT_PATH = pathlib.Path(__file__).with_name("poll_pr_review_feedback.py")
SPEC = importlib.util.spec_from_file_location("poll_pr_review_feedback", SCRIPT_PATH)
assert SPEC and SPEC.loader
poller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(poller)


class StaleReviewCommentTests(unittest.TestCase):
    def test_original_commit_prevents_rebound_old_comment_from_blocking(self) -> None:
        context = {"head_sha": "new", "comment_threads": {}}
        item = {
            "id": 1,
            "commit_id": "new",
            "original_commit_id": "old",
            "line": 10,
        }

        self.assertTrue(poller.should_skip_stale_item("review_comment", item, context))

    def test_comment_created_for_current_head_remains_visible(self) -> None:
        context = {"head_sha": "new", "comment_threads": {}}
        item = {
            "id": 2,
            "commit_id": "new",
            "original_commit_id": "new",
            "line": 10,
        }

        self.assertFalse(poller.should_skip_stale_item("review_comment", item, context))


class HeadCycleTests(unittest.TestCase):
    def test_schedule_restarts_when_head_changes(self) -> None:
        base = dt.datetime(2026, 7, 15, 20, 0, tzinfo=dt.timezone.utc)
        times = iter((base, base, base, base, base, base, base, base))
        contexts = iter(({"head_sha": "old"}, {"head_sha": "new"}))
        collect_results = iter((([], [], "new"), ([], [{"source": "review"}], "new")))

        with (
            mock.patch.object(poller, "now_utc", side_effect=lambda: next(times)),
            mock.patch.object(poller, "get_pr_review_context", side_effect=lambda *_: next(contexts)),
            mock.patch.object(poller, "collect", side_effect=lambda *_, **__: next(collect_results)),
            mock.patch.object(poller, "sleep_until"),
        ):
            result = poller.poll_with_schedule("owner/repo", 1, [0], 90)

        self.assertEqual("new", result["head_sha"])
        self.assertEqual("head_cycle_started_at", result["poll_anchor"])
        self.assertEqual(1, len(result["head_restarts"]))
        self.assertEqual("old", result["head_restarts"][0]["previous_head_sha"])
        self.assertEqual("new", result["head_restarts"][0]["new_head_sha"])

    def test_schedule_is_anchored_to_invocation_cycle(self) -> None:
        base = dt.datetime(2026, 7, 15, 20, 0, tzinfo=dt.timezone.utc)
        times = iter((base, base, base, base))

        with (
            mock.patch.object(poller, "now_utc", side_effect=lambda: next(times)),
            mock.patch.object(poller, "get_pr_review_context", return_value={"head_sha": "head"}),
            mock.patch.object(poller, "collect", return_value=([], [], "head")),
            mock.patch.object(poller, "sleep_until") as sleep_until,
        ):
            result = poller.poll_with_schedule("owner/repo", 1, [7], 90)

        sleep_until.assert_called_once_with(base + dt.timedelta(minutes=7))
        self.assertEqual(base.isoformat(), result["cycle_started_at"])


class UnboundActivityTests(unittest.TestCase):
    def test_reaction_from_previous_cycle_is_ignored(self) -> None:
        cycle_started = dt.datetime(2026, 7, 15, 20, 0, tzinfo=dt.timezone.utc)
        reaction = {
            "id": 3,
            "content": "+1",
            "created_at": (cycle_started - dt.timedelta(seconds=30)).isoformat(),
            "user": {"login": "chatgpt-codex-connector[bot]"},
        }

        def api(endpoint: str):
            return [reaction] if endpoint.endswith("reactions?per_page=100") else []

        with (
            mock.patch.object(
                poller,
                "get_pr_review_context",
                return_value={"head_sha": "head", "comment_threads": {}},
            ),
            mock.patch.object(poller, "gh_api", side_effect=api),
        ):
            _, activity, _ = poller.collect(
                "owner/repo",
                1,
                cycle_started - dt.timedelta(seconds=90),
                unbound_since=cycle_started,
            )

        self.assertEqual([], activity)


if __name__ == "__main__":
    unittest.main()
