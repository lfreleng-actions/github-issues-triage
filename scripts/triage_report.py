# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Build the triage run report from before/after issue snapshots.

The triage workflow captures the organisation's open-issue state
before and after the agent session (``gh search issues --json``
output). This script diffs the two snapshots and emits:

- a Markdown report (also appended to ``$GITHUB_STEP_SUMMARY`` when
  that variable is set), showing per-repository before/after counts
  and a per-issue table of observed label changes
- a machine-readable JSON report with the same content

The diff is the ground truth for what the run changed: the report
reflects observed label movement, not the agent's own claims. When
the optional session transcript is present, the script folds its
cost telemetry (turns, token usage) into the report.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

UNKNOWN = "unknown"


@dataclass(frozen=True)
class Issue:
    """One open issue from a snapshot, reduced to what the diff needs."""

    repo: str
    number: int
    title: str
    url: str
    labels: frozenset[str]

    @property
    def key(self) -> tuple[str, int]:
        """Identity of the issue across snapshots."""
        return (self.repo, self.number)


def load_snapshot(path: Path) -> dict[tuple[str, int], Issue]:
    """Parse a ``gh search issues --json`` dump into issues by key."""
    raw: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    issues: dict[tuple[str, int], Issue] = {}
    for entry in raw:
        repo = str(entry.get("repository", {}).get("name", UNKNOWN))
        issue = Issue(
            repo=repo,
            number=int(entry.get("number", 0)),
            title=str(entry.get("title", "")),
            url=str(entry.get("url", "")),
            labels=frozenset(
                str(label.get("name", ""))
                for label in entry.get("labels", [])
                if label.get("name")
            ),
        )
        issues[issue.key] = issue
    return issues


def load_transcript_stats(path: Path) -> dict[str, Any]:
    """Pull cost telemetry out of a Claude Code execution log.

    The log format is an implementation detail of claude-code-action,
    so parse defensively: harvest recognisable telemetry fields from
    entries when present, and degrade to an empty mapping on anything
    unexpected.
    """
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries: list[Any] = cast("list[Any]", raw) if isinstance(raw, list) else [raw]
    stats: dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for field in ("num_turns", "total_cost_usd", "usage", "duration_ms"):
            if field in entry:
                stats[field] = entry[field]
    return stats


@dataclass(frozen=True)
class IssueChange:
    """Observed label movement on one issue between snapshots."""

    issue: Issue
    before: frozenset[str]
    after: frozenset[str]

    @property
    def added(self) -> list[str]:
        """Labels present after the run but not before, sorted."""
        return sorted(self.after - self.before)

    @property
    def removed(self) -> list[str]:
        """Labels present before the run but not after, sorted."""
        return sorted(self.before - self.after)


def diff_snapshots(
    before: dict[tuple[str, int], Issue],
    after: dict[tuple[str, int], Issue],
) -> list[IssueChange]:
    """Issues whose label sets differ between the two snapshots."""
    changes: list[IssueChange] = []
    for key, issue in sorted(before.items()):
        later = after.get(key)
        if later is not None and later.labels != issue.labels:
            changes.append(
                IssueChange(issue=later, before=issue.labels, after=later.labels)
            )
    return changes


def repo_rows(
    before: dict[tuple[str, int], Issue],
    after: dict[tuple[str, int], Issue],
) -> list[dict[str, Any]]:
    """Per-repository open/untriaged counts, before and after."""
    repos = sorted({issue.repo for issue in before.values()})
    rows: list[dict[str, Any]] = []
    for repo in repos:
        b_issues = [i for i in before.values() if i.repo == repo]
        a_issues = [i for i in after.values() if i.repo == repo]
        rows.append(
            {
                "repository": repo,
                "open": len(b_issues),
                "untriaged_before": sum(1 for i in b_issues if not i.labels),
                "untriaged_after": sum(1 for i in a_issues if not i.labels),
            }
        )
    return rows


def _labels_cell(labels: frozenset[str]) -> str:
    """Render a label set for a Markdown table cell."""
    return ", ".join(f"`{name}`" for name in sorted(labels)) or "*(none)*"


def render_markdown(
    rows: list[dict[str, Any]],
    changes: list[IssueChange],
    stats: dict[str, Any],
    dry_run: bool,
) -> str:
    """Render the full Markdown report."""
    lines = ["# Issues Triage Report", ""]
    mode = "dry-run (no labels applied)" if dry_run else "live"
    lines += [f"- **Mode:** {mode}"]
    lines += [f"- **Issues changed:** {len(changes)}"]
    if "num_turns" in stats:
        lines += [f"- **Agent turns:** {stats['num_turns']}"]
    if "total_cost_usd" in stats:
        lines += [f"- **Session cost (USD):** {stats['total_cost_usd']}"]
    lines += ["", "## Untriaged issues by repository", ""]
    lines += [
        "| Repository | Open | Untriaged before | Untriaged after |",
        "| ---------- | ---- | ---------------- | --------------- |",
    ]
    for row in rows:
        lines += [
            f"| {row['repository']} | {row['open']} "
            f"| {row['untriaged_before']} | {row['untriaged_after']} |"
        ]
    lines += ["", "## Label changes", ""]
    if changes:
        lines += [
            "| Issue | Labels before | Labels after |",
            "| ----- | ------------- | ------------ |",
        ]
        for change in changes:
            issue = change.issue
            link = f"[{issue.repo}#{issue.number}]({issue.url})"
            lines += [
                f"| {link} | {_labels_cell(change.before)} "
                f"| {_labels_cell(change.after)} |"
            ]
    else:
        lines += ["The snapshots show no label changes."]
    lines += [""]
    return "\n".join(lines)


def build_json(
    rows: list[dict[str, Any]],
    changes: list[IssueChange],
    stats: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """Assemble the machine-readable report."""
    return {
        "dry_run": dry_run,
        "issues_changed": len(changes),
        "repositories": rows,
        "changes": [
            {
                "repository": c.issue.repo,
                "number": c.issue.number,
                "title": c.issue.title,
                "url": c.issue.url,
                "labels_added": c.added,
                "labels_removed": c.removed,
            }
            for c in changes
        ],
        "session": stats,
    }


def main() -> None:
    """Parse arguments, build the report, and write every output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    before = load_snapshot(args.before)
    after = before if args.after is None else load_snapshot(args.after)
    stats: dict[str, Any] = {}
    if args.transcript is not None and args.transcript.exists():
        stats = load_transcript_stats(args.transcript)

    changes = diff_snapshots(before, after)
    rows = repo_rows(before, after)
    markdown = render_markdown(rows, changes, stats, args.dry_run)
    args.output_md.write_text(markdown, encoding="utf-8")
    args.output_json.write_text(
        json.dumps(build_json(rows, changes, stats, args.dry_run), indent=2) + "\n",
        encoding="utf-8",
    )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(markdown)


if __name__ == "__main__":
    main()
