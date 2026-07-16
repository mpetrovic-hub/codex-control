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


DEFAULT_SCHEDULE_MINUTES = (7, 15, 18, 30, 45, 60, 90, 180, 240)

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


def gh_graphql(query: str, **fields: object) -> Any:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in fields.items():
        cmd.extend(["-F", f"{key}={value}"])
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "graphql"
        raise RuntimeError(f"gh api graphql failed: {detail}")
    return json.loads(proc.stdout or "null")


def get_pr_review_context(repo: str, pr: int) -> dict[str, Any]:
    owner, name = repo.split("/", 1)
    query = """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          headRefOid
          reviewThreads(first: 100) {
            nodes {
              isResolved
              isOutdated
              comments(first: 50) {
                nodes {
                  databaseId
                }
              }
            }
          }
        }
      }
    }
    """
    data = gh_graphql(query, owner=owner, name=name, number=pr)
    pull = (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
    comment_threads: dict[int, dict[str, bool]] = {}
    for thread in (((pull.get("reviewThreads") or {}).get("nodes")) or []):
        state = {
            "is_resolved": bool(thread.get("isResolved")),
            "is_outdated": bool(thread.get("isOutdated")),
        }
        for comment in (((thread.get("comments") or {}).get("nodes")) or []):
            database_id = comment.get("databaseId")
            if database_id is not None:
                comment_threads[int(database_id)] = state
    return {
        "head_sha": pull.get("headRefOid"),
        "comment_threads": comment_threads,
    }


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


def has_clean_approval_signal(codex_activity: list[dict[str, Any]]) -> bool:
    """A Codex thumbs-up means its review completed without a finding."""
    return any(
        item.get("source") == "issue_reaction" and item.get("body") == "+1"
        for item in codex_activity
    )


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
        "body": str(item.get("body") or item.get("content") or "").strip()[:4000],
    }


def should_skip_stale_item(source: str, item: dict[str, Any], review_context: dict[str, Any]) -> bool:
    head_sha = review_context.get("head_sha")
    if source == "review":
        commit_id = item.get("commit_id")
        return bool(head_sha and commit_id and commit_id != head_sha)

    if source == "review_comment":
        # GitHub can move commit_id forward when the original line still maps to
        # the new head. original_commit_id preserves which code was reviewed.
        reviewed_commit_id = item.get("original_commit_id") or item.get("commit_id")
        if head_sha and reviewed_commit_id and reviewed_commit_id != head_sha:
            return True
        thread_state = (review_context.get("comment_threads") or {}).get(int(item.get("id") or 0))
        if thread_state and (thread_state.get("is_resolved") or thread_state.get("is_outdated")):
            return True
        if item.get("line") is None:
            return True

    return False


def collect(
    repo: str,
    pr: int,
    since: dt.datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    review_context = get_pr_review_context(repo, pr)
    endpoints = (
        ("issue_comment", f"/repos/{repo}/issues/{pr}/comments?per_page=100"),
        ("review_comment", f"/repos/{repo}/pulls/{pr}/comments?per_page=100"),
        ("review", f"/repos/{repo}/pulls/{pr}/reviews?per_page=100"),
        ("issue_reaction", f"/repos/{repo}/issues/{pr}/reactions?per_page=100"),
    )
    findings: list[dict[str, Any]] = []
    codex_activity: list[dict[str, Any]] = []
    for source, endpoint in endpoints:
        for item in gh_api(endpoint) or []:
            created = parse_time(item.get("created_at") or item.get("submitted_at"))
            if created is None or created < since:
                continue
            if not is_codex_source(item):
                continue
            if should_skip_stale_item(source, item, review_context):
                continue
            normalized = normalize(source, item)
            codex_activity.append(normalized)
            if source not in {"issue_reaction", "review"} and is_actionable(item):
                findings.append(normalized)
    findings.sort(key=lambda item: str(item.get("created_at") or ""))
    codex_activity.sort(key=lambda item: str(item.get("created_at") or ""))
    return findings, codex_activity, review_context.get("head_sha")


def result_status(result: dict[str, Any]) -> str:
    if result["findings"]:
        return "actionable_found"
    if result.get("last_error"):
        return "inconclusive_error"
    if result.get("clean_approval_observed"):
        return "no_actionable_after_observed_codex_review"
    if not result.get("completed_schedule", True):
        return "inconclusive_incomplete_schedule"
    if not result.get("codex_activity"):
        return "inconclusive_no_codex_review_activity"
    return "no_actionable_after_observed_codex_review"


def print_report(result: dict[str, Any]) -> None:
    print("CODEX_PR_REVIEW_POLL_JSON_START")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("CODEX_PR_REVIEW_POLL_JSON_END")
    print()

    findings = result["findings"]
    status = result.get("status") or result_status(result)
    print("## Codex PR Review Poll")
    print()
    if not findings:
        if status == "no_actionable_after_observed_codex_review":
            if result.get("clean_approval_observed"):
                print("Codex reacted with a thumbs-up and no actionable review feedback was found. Polling ended early as clean.")
            else:
                print("No actionable Codex review feedback was found after the full polling window, and Codex review activity was observed.")
        elif status == "inconclusive_no_codex_review_activity":
            print("No actionable Codex review feedback was found, but no Codex review activity was observed. Treat this as inconclusive, not green; Codex review may not have run yet.")
        elif status == "inconclusive_incomplete_schedule":
            print("No actionable Codex review feedback was found yet, but the full polling schedule did not complete. Treat this as inconclusive, not green.")
        elif status == "inconclusive_error":
            print("No actionable Codex review feedback was found, but polling ended with an API error. Treat this as inconclusive, not green.")
        else:
            print("No actionable Codex review feedback was found. Treat this as inconclusive unless the JSON status proves the full schedule completed and Codex review activity was observed.")
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
    # Keep one observation window across head-cycle restarts. The schedule is
    # reset per head, but feedback already fetched for the new head must survive.
    since = started_at - dt.timedelta(seconds=since_seconds_ago)
    head_restarts: list[dict[str, Any]] = []
    carried_head_sha: str | None = None
    carried_findings: list[dict[str, Any]] = []
    carried_activity: list[dict[str, Any]] = []

    while True:
        cycle_started_at = now_utc()
        cycle_head_sha = get_pr_review_context(repo, pr).get("head_sha")
        if not cycle_head_sha:
            raise RuntimeError(f"Could not read head SHA for PR #{pr}")
        if carried_head_sha == cycle_head_sha:
            findings = carried_findings
            codex_activity = carried_activity
        else:
            findings = []
            codex_activity = []
        carried_head_sha = None
        carried_findings = []
        carried_activity = []
        last_error: str | None = None
        polls: list[dict[str, Any]] = []
        clean_approval_observed = has_clean_approval_signal(codex_activity)
        restart_for_new_head = False

        if findings or clean_approval_observed:
            break

        for minute in schedule_minutes:
            due_at = cycle_started_at + dt.timedelta(minutes=minute)
            sleep_until(due_at)
            checked_at = now_utc()
            observed_head_sha: str | None = None
            try:
                findings, codex_activity, observed_head_sha = collect(
                    repo,
                    pr,
                    since,
                )
                last_error = None
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                findings = []
                codex_activity = []

            polls.append(
                {
                    "minute": minute,
                    "due_at": due_at.isoformat(),
                    "checked_at": checked_at.isoformat(),
                    "head_sha": observed_head_sha,
                    "finding_count": len(findings),
                    "codex_activity_count": len(codex_activity),
                    "error": last_error,
                }
            )
            if observed_head_sha and observed_head_sha != cycle_head_sha:
                head_restarts.append(
                    {
                        "detected_at": checked_at.isoformat(),
                        "previous_head_sha": cycle_head_sha,
                        "new_head_sha": observed_head_sha,
                    }
                )
                # collect() used the observed new head for filtering, so carry
                # its valid results into that head's restarted schedule.
                carried_head_sha = observed_head_sha
                carried_findings = findings
                carried_activity = codex_activity
                restart_for_new_head = True
                break

            clean_approval_observed = has_clean_approval_signal(codex_activity)
            if findings or clean_approval_observed:
                break

        if not restart_for_new_head:
            break

    completed_schedule = len(polls) == len(schedule_minutes) and not findings
    result = {
        "repo": repo,
        "pr": pr,
        "started_at": started_at.isoformat(),
        "poll_anchor": "head_cycle_started_at",
        "cycle_started_at": cycle_started_at.isoformat(),
        "head_sha": cycle_head_sha,
        "head_restarts": head_restarts,
        "since": since.isoformat(),
        "schedule_minutes": schedule_minutes,
        "completed_schedule": completed_schedule,
        "polls": polls,
        "last_error": last_error,
        "clean_approval_observed": clean_approval_observed,
        "codex_activity": codex_activity,
        "findings": findings,
    }
    result["status"] = result_status(result)
    return result


def poll_with_timeout(
    repo: str,
    pr: int,
    timeout_minutes: float,
    interval_seconds: float,
    since_seconds_ago: float,
) -> dict[str, Any]:
    started_at = now_utc()
    # Timeout cycles use the same persistent observation window as schedules.
    since = started_at - dt.timedelta(seconds=since_seconds_ago)
    head_restarts: list[dict[str, Any]] = []
    carried_head_sha: str | None = None
    carried_findings: list[dict[str, Any]] = []
    carried_activity: list[dict[str, Any]] = []

    while True:
        cycle_started_at = now_utc()
        cycle_head_sha = get_pr_review_context(repo, pr).get("head_sha")
        if not cycle_head_sha:
            raise RuntimeError(f"Could not read head SHA for PR #{pr}")
        deadline = cycle_started_at + dt.timedelta(minutes=timeout_minutes)
        if carried_head_sha == cycle_head_sha:
            findings = carried_findings
            codex_activity = carried_activity
        else:
            findings = []
            codex_activity = []
        carried_head_sha = None
        carried_findings = []
        carried_activity = []
        last_error: str | None = None
        polls: list[dict[str, Any]] = []
        clean_approval_observed = has_clean_approval_signal(codex_activity)
        restart_for_new_head = False

        if findings or clean_approval_observed:
            break

        while now_utc() <= deadline:
            checked_at = now_utc()
            observed_head_sha: str | None = None
            try:
                findings, codex_activity, observed_head_sha = collect(
                    repo,
                    pr,
                    since,
                )
                last_error = None
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

            polls.append(
                {
                    "checked_at": checked_at.isoformat(),
                    "head_sha": observed_head_sha,
                    "finding_count": len(findings),
                    "codex_activity_count": len(codex_activity),
                    "error": last_error,
                }
            )
            if observed_head_sha and observed_head_sha != cycle_head_sha:
                head_restarts.append(
                    {
                        "detected_at": checked_at.isoformat(),
                        "previous_head_sha": cycle_head_sha,
                        "new_head_sha": observed_head_sha,
                    }
                )
                # Preserve feedback already filtered against the observed head.
                carried_head_sha = observed_head_sha
                carried_findings = findings
                carried_activity = codex_activity
                restart_for_new_head = True
                break

            clean_approval_observed = has_clean_approval_signal(codex_activity)
            if findings or clean_approval_observed:
                break

            remaining = (deadline - now_utc()).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(interval_seconds, remaining))

        if not restart_for_new_head:
            break

    completed_schedule = now_utc() >= deadline and not findings
    result = {
        "repo": repo,
        "pr": pr,
        "started_at": started_at.isoformat(),
        "poll_anchor": "head_cycle_started_at",
        "cycle_started_at": cycle_started_at.isoformat(),
        "head_sha": cycle_head_sha,
        "head_restarts": head_restarts,
        "since": since.isoformat(),
        "timeout_minutes": timeout_minutes,
        "interval_seconds": interval_seconds,
        "completed_schedule": completed_schedule,
        "polls": polls,
        "last_error": last_error,
        "clean_approval_observed": clean_approval_observed,
        "codex_activity": codex_activity,
        "findings": findings,
    }
    result["status"] = result_status(result)
    return result


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="mpetrovic-hub/backend")
    parser.add_argument("--pr", required=True, help="PR number, PR URL, or owner/repo#number")
    parser.add_argument(
        "--schedule-minutes",
        help="Comma-separated minutes after the current-head polling cycle starts. Default: 7,15,18,30,45,60,90,180,240.",
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
