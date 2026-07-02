#!/usr/bin/env python3
"""Poll a GitHub PR for early Codex review feedback.

The script keeps the API polling mechanics out of the Implementer prompt. It
uses `gh api`, so authentication stays with the GitHub CLI/session.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from typing import Any


DEFAULT_SCHEDULE_MINUTES = (3, 5, 7, 15, 30, 45, 60, 90, 180, 240)

CODEX_SOURCE_HINTS = (
    "chatgpt-codex-connector",
    "chatgpt-codex-connector[bot]",
    "codex-review",
    "codex-cloud-review",
)

PRIORITY_BADGE_RE = re.compile(
    r"(?:^|\b|\[|`)P[0-3](?:\b|\]|`|:|-)",
    re.IGNORECASE,
)

CODEX_SUGGESTION_HINTS = (
    "```suggestion",
    "<details",
    "suggestion:",
    "suggested change",
)


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def pr_number_from(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)

    pull_match = re.search(r"/pull/(\d+)(?:\D*$|$)", value)
    if pull_match:
        return int(pull_match.group(1))

    shorthand_match = re.search(r"#(\d+)$", value)
    if shorthand_match:
        return int(shorthand_match.group(1))

    raise SystemExit(f"Could not derive PR number from: {value}")


def gh_api(path: str) -> Any:
    proc = subprocess.run(
        ["gh", "api", path],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or path
        raise RuntimeError(f"gh api failed: {detail}")
    return json.loads(proc.stdout or "null")


def get_pr_created_at(repo: str, pr: int) -> dt.datetime:
    pr_data = gh_api(f"/repos/{repo}/pulls/{pr}")
    created = parse_time(pr_data.get("created_at"))
    if created is None:
        raise RuntimeError(f"Could not read created_at for PR #{pr}")
    return created


def parse_schedule_minutes(value: str | None) -> list[float]:
    if not value:
        return list(DEFAULT_SCHEDULE_MINUTES)
    minutes: list[float] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        minutes.append(float(part))
    if not minutes:
        raise SystemExit("Schedule must contain at least one minute value")
    return sorted(set(minutes))


def lower_join(*values: object) -> str:
    return " ".join(str(value or "").lower() for value in values)


def source_text(item: dict[str, Any]) -> str:
    user = item.get("user") or {}
    app = item.get("app") or {}
    return lower_join(
        user.get("login"),
        user.get("type"),
        app.get("slug"),
        app.get("name"),
        item.get("author_association"),
        item.get("body"),
    )


def is_codex_source(item: dict[str, Any]) -> bool:
    text = source_text(item)
    return any(hint in text for hint in CODEX_SOURCE_HINTS)


def is_actionable(item: dict[str, Any]) -> bool:
    text = lower_join(item.get("body"), item.get("state"))
    if str(item.get("state") or "").upper() == "CHANGES_REQUESTED":
        return True
    if PRIORITY_BADGE_RE.search(str(item.get("body") or "")):
        return True
    return any(hint in text for hint in CODEX_SUGGESTION_HINTS)


def normalize(source: str, item: dict[str, Any]) -> dict[str, Any]:
    user = item.get("user") or {}
    app = item.get("app") or {}
    links = item.get("_links") or {}
    html = links.get("html") or {}
    return {
        "source": source,
        "id": item.get("id"),
        "author": user.get("login"),
        "app": app.get("slug") or app.get("name"),
        "state": item.get("state"),
        "created_at": item.get("created_at") or item.get("submitted_at"),
        "url": item.get("html_url") or html.get("href"),
        "path": item.get("path"),
        "line": item.get("line") or item.get("original_line"),
        "body": str(item.get("body") or "").strip()[:4000],
    }


def collect(repo: str, pr: int, since: dt.datetime) -> list[dict[str, Any]]:
    endpoints = (
        ("issue_comment", f"/repos/{repo}/issues/{pr}/comments?per_page=100"),
        ("review_comment", f"/repos/{repo}/pulls/{pr}/comments?per_page=100"),
        ("review", f"/repos/{repo}/pulls/{pr}/reviews?per_page=100"),
    )
    findings: list[dict[str, Any]] = []
    for source, endpoint in endpoints:
        for item in gh_api(endpoint) or []:
            created = parse_time(item.get("created_at") or item.get("submitted_at"))
            if created is None or created < since:
                continue
            if is_codex_source(item) and is_actionable(item):
                findings.append(normalize(source, item))
    findings.sort(key=lambda item: str(item.get("created_at") or ""))
    return findings


def print_report(result: dict[str, Any]) -> None:
    print("CODEX_PR_REVIEW_POLL_JSON_START")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("CODEX_PR_REVIEW_POLL_JSON_END")
    print()

    findings = result["findings"]
    print("## Codex PR Review Poll")
    print()
    if not findings:
        print("No actionable Codex review feedback was found during the polling window.")
        return

    print("Actionable Codex review feedback was found:")
    print()
    for index, item in enumerate(findings, start=1):
        location = ""
        if item.get("path"):
            location = f" `{item['path']}`"
            if item.get("line"):
                location += f":{item['line']}"
        print(f"{index}. `{item['source']}` by `{item.get('author') or item.get('app')}`{location}")
        if item.get("url"):
            print(f"   URL: {item['url']}")
        body = str(item.get("body") or "")
        if body:
            print("   Body:")
            for line in body.splitlines()[:12]:
                print(f"   > {line}")
        print()


def sleep_until(target: dt.datetime) -> None:
    while True:
        remaining = (target - now_utc()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(60.0, remaining))


def poll_with_schedule(
    repo: str,
    pr: int,
    schedule_minutes: list[float],
    since_seconds_ago: float,
) -> dict[str, Any]:
    started_at = now_utc()
    pr_created_at = get_pr_created_at(repo, pr)
    since = pr_created_at - dt.timedelta(seconds=since_seconds_ago)
    findings: list[dict[str, Any]] = []
    last_error: str | None = None
    polls: list[dict[str, Any]] = []

    for minute in schedule_minutes:
        due_at = pr_created_at + dt.timedelta(minutes=minute)
        sleep_until(due_at)
        checked_at = now_utc()
        try:
            findings = collect(repo, pr, since)
            last_error = None
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            findings = []

        polls.append(
            {
                "minute": minute,
                "due_at": due_at.isoformat(),
                "checked_at": checked_at.isoformat(),
                "finding_count": len(findings),
                "error": last_error,
            }
        )
        if findings:
            break

    return {
        "repo": repo,
        "pr": pr,
        "started_at": started_at.isoformat(),
        "pr_created_at": pr_created_at.isoformat(),
        "since": since.isoformat(),
        "schedule_minutes": schedule_minutes,
        "polls": polls,
        "last_error": last_error,
        "findings": findings,
    }


def poll_with_timeout(
    repo: str,
    pr: int,
    timeout_minutes: float,
    interval_seconds: float,
    since_seconds_ago: float,
) -> dict[str, Any]:
    started_at = now_utc()
    since = started_at - dt.timedelta(seconds=since_seconds_ago)
    deadline = started_at + dt.timedelta(minutes=timeout_minutes)
    findings: list[dict[str, Any]] = []
    last_error: str | None = None
    polls: list[dict[str, Any]] = []

    while now_utc() <= deadline:
        checked_at = now_utc()
        try:
            findings = collect(repo, pr, since)
            last_error = None
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

        polls.append(
            {
                "checked_at": checked_at.isoformat(),
                "finding_count": len(findings),
                "error": last_error,
            }
        )
        if findings:
            break

        remaining = (deadline - now_utc()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(interval_seconds, remaining))

    return {
        "repo": repo,
        "pr": pr,
        "started_at": started_at.isoformat(),
        "since": since.isoformat(),
        "timeout_minutes": timeout_minutes,
        "interval_seconds": interval_seconds,
        "polls": polls,
        "last_error": last_error,
        "findings": findings,
    }


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="mpetrovic-hub/backend")
    parser.add_argument("--pr", required=True, help="PR number, PR URL, or owner/repo#number")
    parser.add_argument(
        "--schedule-minutes",
        help="Comma-separated PR-age minutes to poll at. Default: 3,5,7,15,30,45,60,90,180,240.",
    )
    parser.add_argument("--timeout-minutes", type=float)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--since-seconds-ago", type=float, default=90.0)
    args = parser.parse_args()

    pr = pr_number_from(args.pr)
    if args.timeout_minutes is not None:
        result = poll_with_timeout(
            args.repo,
            pr,
            args.timeout_minutes,
            args.interval_seconds,
            args.since_seconds_ago,
        )
    else:
        result = poll_with_schedule(
            args.repo,
            pr,
            parse_schedule_minutes(args.schedule_minutes),
            args.since_seconds_ago,
        )

    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
