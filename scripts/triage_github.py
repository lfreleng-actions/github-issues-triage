# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads and writes for the triage applier.

Kept apart from the policy in ``apply_triage.py`` so that the
rules a proposal must satisfy stay readable without the API
plumbing interleaved through them.

Nothing here decides whether an action is permitted; callers do
that first. These helpers assume validation has already passed.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

# Reserved key holding a field's own id alongside its option ids.
# Option names come from the organisation's field definitions and
# never take this shape, so no collision is possible.
FIELD_ID_KEY = "__field_id__"


class Rejected(Exception):
    """A proposal failed validation and will not be applied."""


def run_gh(args: list[str]) -> str:
    """Run a gh command, returning stdout and raising on failure."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise Rejected(proc.stderr.strip() or f"gh {' '.join(args)} failed")
    return proc.stdout


def load_field_options(org: str) -> dict[str, dict[str, int]]:
    """Map each org issue field to its option names and ids.

    The option ids prove an option exists at validation time; the
    write itself sends the option's name.
    """
    raw = run_gh(["api", f"orgs/{org}/issue-fields"])
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
    """
    raw = run_gh(["api", f"orgs/{org}/issue-types"])
    entries: list[dict[str, Any]] = json.loads(raw)
    return {str(entry["name"]) for entry in entries}


def check_target(repo: str, number: int) -> None:
    """Confirm the target exists and is an issue, not a pull request."""
    raw = run_gh(
        [
            "api",
            f"repos/{repo}/issues/{number}",
            "--jq",
            'if .pull_request then "pull-request" else "issue" end',
        ]
    )
    if raw.strip() != "issue":
        raise Rejected(f"target is a pull request, not an issue: {repo}#{number}")


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


def put_fields(repo: str, number: str, payload: str) -> None:
    """PUT issue field values, passing the body on stdin.

    ``gh issue edit`` cannot set issue fields, so priority goes
    through the REST endpoint directly.
    """
    proc = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "PUT",
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
        raise Rejected(proc.stderr.strip() or "setting issue fields failed")


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
        put_fields(repo, number, payload)
