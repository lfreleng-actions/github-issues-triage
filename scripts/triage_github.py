# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads and writes for the triage applier.

Kept apart from the policy in ``triage_policy`` so that the rules
a proposal must satisfy stay readable without the API plumbing
interleaved through them.

Nothing here decides whether an action is permitted; callers do
that first. These helpers assume validation has already passed.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

# Reserved key holding a field's own id alongside its option ids.
# Option names come from the organisation's field definitions and
# never take this shape, so no collision is possible.
FIELD_ID_KEY = "__field_id__"
# gh reports the status in its stderr line: "gh: Not Found (HTTP 404)".
STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")
# Statuses meaning "this owner has no such endpoint, or this token
# may not see it" -- a fact about the configuration. Anything else
# means GitHub could not answer just now, which is a different
# thing and must not be read as an empty answer.
ABSENT = frozenset({403, 404, 410})
# 403 carries a second meaning: GitHub returns it for primary and
# secondary rate limits, which are the most transient failures of
# all. The status alone cannot tell the two apart, so the message
# has to.
RATE_LIMIT_RE = re.compile(r"rate limit", re.IGNORECASE)


class Rejected(Exception):
    """A proposal failed validation and will not be applied."""


class GitHubError(Exception):
    """A call to GitHub failed.

    Distinct from ``Rejected`` because the two mean opposite
    things: a rejection is the applier working correctly, while
    this is the applier unable to do its job. Conflating them
    would let a failed write be counted as a harmless rejection
    and the step exit successfully.

    Carries the HTTP status where gh reported one, so callers can
    tell an endpoint that is absent from one that is briefly
    unreachable.
    """

    def __init__(self, message: str) -> None:
        """Record the message and the status gh named, if any."""
        super().__init__(message)
        found = STATUS_RE.search(message)
        self.status: int | None = int(found.group(1)) if found else None


def run_gh(args: list[str]) -> str:
    """Run a gh command, returning stdout and raising on failure."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitHubError(proc.stderr.strip() or f"gh {' '.join(args)} failed")
    return proc.stdout


def absent(exc: GitHubError) -> bool:
    """Whether a failure means the endpoint is not there at all.

    Separates a configuration fact, which the caller degrades
    around, from a GitHub that could not answer, which must stop
    the run. Reading a rate limit as "no fields" would drop every
    priority, apply the labels anyway, and report success.
    """
    if exc.status not in ABSENT:
        return False
    return not RATE_LIMIT_RE.search(str(exc))


def load_field_options(org: str) -> dict[str, dict[str, int]]:
    """Map each org issue field to its option names and ids.

    Empty when the owner has no such endpoint: it may be a user
    rather than an organisation, or the token may lack the
    organisation grant. Callers drop a proposed priority in that
    case and record why, rather than failing the whole run and
    losing the label work too.

    A transient failure raises instead. Reading a timeout as "no
    fields" would drop every priority, apply the labels anyway,
    and report success.
    """
    try:
        raw = run_gh(["api", f"orgs/{org}/issue-fields"])
    except GitHubError as exc:
        if absent(exc):
            return {}
        raise
    data: list[dict[str, Any]] = json.loads(raw)
    fields: dict[str, dict[str, int]] = {}
    for entry in data:
        options: list[dict[str, Any]] = entry.get("options") or []
        mapping = {str(option["name"]): int(option["id"]) for option in options}
        mapping[FIELD_ID_KEY] = int(entry["id"])
        fields[str(entry["name"])] = mapping
    return fields


def load_issue_types(org: str) -> set[str]:
    """Names of the issue types the organisation actually enables.

    Organisations customise or disable types, so the usual trio is
    not safe to assume: an unknown value would fail ``gh issue
    edit --type`` only after labels had already changed, leaving
    the issue half-applied.

    Empty when unavailable, and raising on a transient failure,
    for the reasons in ``load_field_options``.
    """
    try:
        raw = run_gh(["api", f"orgs/{org}/issue-types"])
    except GitHubError as exc:
        if absent(exc):
            return set()
        raise
    entries: list[dict[str, Any]] = json.loads(raw)
    # The endpoint lists retired definitions alongside live ones.
    # Accepting a disabled name would pass validation and then
    # fail at ``gh issue edit --type``, after apply() had already
    # changed the labels.
    return {str(entry["name"]) for entry in entries if entry.get("is_enabled", True)}


@dataclass(frozen=True)
class LiveIssue:
    """An issue as it stands now, rather than as the scan saw it."""

    is_pull_request: bool
    state: str
    labels: frozenset[str]


def read_issue(repo: str, number: int) -> LiveIssue:
    """Read a target's current kind, state and labels."""
    raw = run_gh(
        [
            "api",
            f"repos/{repo}/issues/{number}",
            "--jq",
            "{pr: (.pull_request != null), state: .state, labels: [.labels[].name]}",
        ]
    )
    data: dict[str, Any] = json.loads(raw)
    return LiveIssue(
        is_pull_request=bool(data["pr"]),
        state=str(data["state"]),
        labels=frozenset(str(name) for name in data["labels"]),
    )


def existing_priority(repo: str, number: int) -> str | None:
    """Return the priority already set on an issue, if any."""
    raw = run_gh(["api", f"repos/{repo}/issues/{number}/issue-field-values"])
    values: list[dict[str, Any]] = json.loads(raw)
    for value in values:
        if value.get("issue_field_name") == "Priority":
            option: dict[str, Any] = value.get("single_select_option") or {}
            name = option.get("name")
            return str(name) if name is not None else None
    return None


def repo_labels(repo: str, cache: dict[str, set[str]]) -> set[str]:
    """List a repository's labels, once per repository."""
    if repo not in cache:
        raw = run_gh(
            ["label", "list", "--repo", repo, "--limit", "200", "--json", "name"]
        )
        entries: list[dict[str, Any]] = json.loads(raw)
        cache[repo] = {str(entry["name"]) for entry in entries}
    return cache[repo]


def add_fields(repo: str, number: str, payload: str) -> None:
    """Add issue field values, passing the body on stdin.

    ``gh issue edit`` cannot set issue fields, so priority goes
    through the REST endpoint directly.

    POST rather than PUT. The two differ by more than spelling:
    PUT *replaces every value on the issue*, so sending Priority
    alone would clear whatever a human had put in the other
    fields. The organisation defines Effort, Start date and
    Target date beside it, and triage has no business touching
    any of them.
    """
    proc = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repo}/issues/{number}/issue-field-values",
            "--input",
            "-",
        ],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitHubError(proc.stderr.strip() or "setting issue fields failed")


def apply(action: dict[str, Any], fields: dict[str, dict[str, int]]) -> None:
    """Perform one validated action against GitHub."""
    repo = str(action["repository"])
    number = str(action["issue"])

    if action["migrate_enhancement"]:
        run_gh(
            [
                "issue",
                "edit",
                number,
                "--repo",
                repo,
                "--remove-label",
                "enhancement",
                "--add-label",
                "feature",
            ]
        )
    if action["labels"]:
        run_gh(
            [
                "issue",
                "edit",
                number,
                "--repo",
                repo,
                "--add-label",
                ",".join(action["labels"]),
            ]
        )
    if action["type"]:
        run_gh(["issue", "edit", number, "--repo", repo, "--type", action["type"]])
    if action["priority"]:
        # The endpoint wants the option's *name*, not its id:
        # sending the integer earns "must be a string option name".
        payload = json.dumps(
            {
                "issue_field_values": [
                    {
                        "field_id": fields["Priority"][FIELD_ID_KEY],
                        "value": action["priority"],
                    }
                ]
            }
        )
        add_fields(repo, number, payload)
