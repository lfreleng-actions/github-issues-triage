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
from typing import Any, cast

# Reserved key holding a field's own id alongside its option ids.
# Option names come from the organisation's field definitions and
# never take this shape, so no collision is possible.
FIELD_ID_KEY = "__field_id__"
# gh reports the status in its stderr line: "gh: Not Found (HTTP 404)".
STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")
ABSENT = frozenset({404, 410})
# A 403 can mean missing grants, rate limiting or an unknown failure.
# Only known permission denials may degrade an explicitly allowed read.
PERMISSION_DENIED_RE = re.compile(
    r"\bresource not accessible by (?:integration|personal access token)\b",
    re.IGNORECASE,
)
RATE_LIMIT_RE = re.compile(r"rate[\s-]*limit|\babuse\b", re.IGNORECASE)


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


def run_gh(args: list[str], *, input: str | None = None) -> str:
    """Run gh with a pinned REST API version, returning stdout or raising on failure."""
    if args[:1] == ["api"]:
        args = [*args, "--header", "X-GitHub-Api-Version: 2026-03-10"]
    try:
        proc = subprocess.run(
            ["gh", *args],
            input=input,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubError("gh timed out after 30 seconds") from exc
    except OSError as exc:
        raise GitHubError(f"could not run gh: {exc}") from exc
    if proc.returncode != 0:
        raise GitHubError(proc.stderr.strip() or f"gh {' '.join(args)} failed")
    return proc.stdout


def decode_response(raw: str) -> Any:
    """Keep malformed API responses on the operational-failure path."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise GitHubError(f"invalid JSON from GitHub: {exc}") from exc


def api_list(endpoint: str) -> list[dict[str, Any]]:
    """Read every page, refusing incomplete or non-list responses."""
    raw = run_gh(["api", endpoint, "--paginate", "--slurp"])
    pages = decode_response(raw)
    if not isinstance(pages, list):
        raise GitHubError(f"expected paginated arrays from {endpoint}")
    entries: list[dict[str, Any]] = []
    for page in cast("list[Any]", pages):
        if not isinstance(page, list):
            raise GitHubError(f"expected an array page from {endpoint}")
        for entry in cast("list[Any]", page):
            if not isinstance(entry, dict):
                raise GitHubError(f"expected an object entry from {endpoint}")
            entries.append(cast("dict[str, Any]", entry))
    return entries


def absent(exc: GitHubError) -> bool:
    """Whether a failure is a supported dry-run configuration absence.

    Unknown 403s and abuse limits must not masquerade as missing
    permissions. Live callers must fail even for a known absence.
    """
    message = str(exc)
    if RATE_LIMIT_RE.search(message):
        return False
    return exc.status in ABSENT or (
        exc.status == 403 and PERMISSION_DENIED_RE.search(message) is not None
    )


def load_field_options(
    org: str, *, allow_unavailable: bool = False
) -> dict[str, dict[str, int]]:
    """Map each org issue field to its option names and ids.

    By default every read failure raises. A dry-run caller may
    explicitly allow known endpoint absence or permission denial;
    only then is it returned as empty configuration. Transient
    and unrecognised failures always raise.
    """
    try:
        data = api_list(f"orgs/{org}/issue-fields")
    except GitHubError as exc:
        if allow_unavailable and absent(exc):
            return {}
        raise
    fields: dict[str, dict[str, int]] = {}
    try:
        for entry in data:
            options: list[dict[str, Any]] = entry.get("options") or []
            mapping = {str(option["name"]): int(option["id"]) for option in options}
            mapping[FIELD_ID_KEY] = int(entry["id"])
            fields[str(entry["name"])] = mapping
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubError(f"invalid issue field definition: {exc}") from exc
    return fields


def load_issue_types(org: str, *, allow_unavailable: bool = False) -> set[str]:
    """Names of the issue types the organisation actually enables.

    Organisations customise or disable types, so the usual trio is
    not safe to assume: an unknown value would fail ``gh issue
    edit --type`` only after labels had already changed, leaving
    the issue half-applied.

    Read failures raise unless a dry-run caller explicitly allows
    known unavailability, as in ``load_field_options``.
    """
    try:
        entries = api_list(f"orgs/{org}/issue-types")
    except GitHubError as exc:
        if allow_unavailable and absent(exc):
            return set()
        raise
    # The endpoint lists retired definitions alongside live ones.
    # Accepting a disabled name would pass validation and then
    # fail at ``gh issue edit --type``, after apply() had already
    # changed the labels.
    try:
        return {
            str(entry["name"]) for entry in entries if entry.get("is_enabled", True)
        }
    except (KeyError, TypeError) as exc:
        raise GitHubError(f"invalid issue type definition: {exc}") from exc


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
    parsed = decode_response(raw)
    if not isinstance(parsed, dict):
        raise GitHubError("expected an issue object from GitHub")
    data = cast("dict[str, Any]", parsed)
    pr = data.get("pr")
    state = data.get("state")
    labels = data.get("labels")
    if (
        not isinstance(pr, bool)
        or state not in ("open", "closed")
        or not isinstance(labels, list)
        or not all(isinstance(name, str) for name in cast("list[Any]", labels))
    ):
        raise GitHubError("invalid live issue kind, state or labels")
    return LiveIssue(
        is_pull_request=pr,
        state=state,
        labels=frozenset(cast("list[str]", labels)),
    )


def existing_priority(repo: str, number: int) -> str | None:
    """Return the priority already set on an issue, if any."""
    values = api_list(f"repos/{repo}/issues/{number}/issue-field-values")
    for value in values:
        if not isinstance(value.get("issue_field_name"), str):
            raise GitHubError("issue field value has no field name")
        if value["issue_field_name"] == "Priority":
            option = value.get("single_select_option")
            if option is None and "value" in value and value["value"] is None:
                return None
            if isinstance(option, dict):
                name = cast("dict[str, Any]", option).get("name")
                if isinstance(name, str) and name:
                    return name
            # A stored value with no readable option is not an empty
            # priority. Refuse to overwrite a human's unresolved value.
            raise GitHubError("could not resolve the existing Priority option")
    return None


def repo_labels(repo: str, cache: dict[str, set[str]]) -> set[str]:
    """List a repository's labels, once per repository."""
    if repo not in cache:
        entries = api_list(f"repos/{repo}/labels")
        try:
            cache[repo] = {str(entry["name"]) for entry in entries}
        except (KeyError, TypeError) as exc:
            raise GitHubError(f"invalid repository label: {exc}") from exc
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
    run_gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{repo}/issues/{number}/issue-field-values",
            "--input",
            "-",
        ],
        input=payload,
    )


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
