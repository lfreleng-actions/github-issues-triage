# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validate an agent's triage proposal and apply it.

The agent proposes; this applies. Keeping the two apart means no
agent session ever holds a write-capable credential, so the
containment weaknesses of any particular harness stop mattering
for writes (see DESIGN.md section 13.2).

Everything the agent says is treated as untrusted input. A
proposal reaches GitHub only if it survives every check here:

* the repository sits inside the run's organisation, matches the
  single-repository restriction when one is set, and is absent
  from the exclusion list;
* the target is an issue, not a pull request;
* each label already exists in that repository;
* priority and type name options the organisation defines;
* the issue carries no priority a human set already.

Labels travel through ``gh``. Priority and type travel through
the REST issue-field endpoints, which ``gh issue edit`` cannot
reach.

Usage:
    apply_triage.py --proposal <file> --snapshot <before.json>
                    --output-json <file> [--dry-run]

Environment:
    GH_TOKEN             token for the gh CLI
    TRIAGE_ORG           (required) owner every target must match
    TRIAGE_REPOSITORY    restrict targets to this repository name
    TRIAGE_EXCLUDE_FILE  file of excluded repository names
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

PRIORITIES = ("Urgent", "High", "Medium", "Low")
TYPES = ("Task", "Bug", "Feature")
# Reserved key holding a field's own id alongside its option ids.
# Option names come from the organisation's field definitions and
# never take this shape, so no collision is possible.
FIELD_ID_KEY = "__field_id__"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")
MAX_LABELS = 2
# The agent emits one fenced json block; prose around it is for
# humans. Take the last block that parses and carries the key,
# so a worked example earlier in the message cannot win.
FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


class Rejected(Exception):
    """A proposal failed validation and will not be applied."""


def run_gh(args: list[str]) -> str:
    """Run a gh command, returning stdout and raising on failure."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise Rejected(proc.stderr.strip() or f"gh {' '.join(args)} failed")
    return proc.stdout


def extract_proposal(text: str) -> dict[str, Any]:
    """Pull the proposal object out of the agent's final message."""
    for block in reversed(FENCE_RE.findall(text)):
        try:
            parsed: Any = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "proposals" in parsed:
            return cast("dict[str, Any]", parsed)
    raise Rejected("no json block carrying 'proposals' found")


def load_exclusions(path: str | None) -> set[str]:
    """Read the excluded repository names, lowercased."""
    if not path or not Path(path).is_file():
        return set()
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return {line.strip().lower() for line in lines if line.strip()}


def load_field_options(org: str) -> dict[str, dict[str, int]]:
    """Map each org issue field to its option names and ids.

    Both the field id and its option ids are integers the
    issue-field-values endpoint expects, so one dict carries both
    and saves a second lookup at apply time.
    """
    raw = run_gh(["api", f"orgs/{org}/issue-fields"])
    data: list[dict[str, Any]] = json.loads(raw)
    fields: dict[str, dict[str, int]] = {}
    for field in data:
        options: list[dict[str, Any]] = field.get("options") or []
        entry = {str(option["name"]): int(option["id"]) for option in options}
        entry[FIELD_ID_KEY] = int(field["id"])
        fields[str(field["name"])] = entry
    return fields


def check_scope(repo: str, org: str, only: str | None, excluded: set[str]) -> None:
    """Refuse targets outside the run's declared scope."""
    if not REPO_RE.match(repo):
        raise Rejected(f"repository fails validation: {repo}")
    owner, _, name = repo.partition("/")
    if owner.lower() != org.lower():
        raise Rejected(f"repository outside triage scope ({org}): {repo}")
    if only and name.lower() != only.lower():
        raise Rejected(f"run restricted to {org}/{only}: {repo}")
    if name.lower() in excluded:
        raise Rejected(f"repository is excluded from triage: {repo}")


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


def validate(
    item: dict[str, Any],
    org: str,
    only: str | None,
    excluded: set[str],
    label_cache: dict[str, set[str]],
    fields: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """Check one proposal, returning the actions it authorises."""
    repo = str(item.get("repository", ""))
    number = item.get("issue")
    if not isinstance(number, int):
        raise Rejected(f"issue number is not an integer: {number!r}")

    check_scope(repo, org, only, excluded)
    check_target(repo, number)

    raw_labels: Any = item.get("labels") or []
    if not isinstance(raw_labels, list) or not all(
        isinstance(name, str) for name in cast("list[Any]", raw_labels)
    ):
        raise Rejected("labels must be a list of strings")
    labels = cast("list[str]", raw_labels)
    if len(labels) > MAX_LABELS:
        raise Rejected(f"at most {MAX_LABELS} labels per issue")
    known = repo_labels(repo, label_cache)
    for name in labels:
        if name not in known:
            raise Rejected(f"label does not exist in {repo}: {name}")

    priority = item.get("priority")
    if priority is not None:
        if priority not in PRIORITIES:
            raise Rejected(f"unknown priority: {priority!r}")
        # A human triager outranks the agent, exactly as for labels.
        current = existing_priority(repo, number)
        if current is not None:
            raise Rejected(f"priority already set to {current}; leaving it")

    issue_type = item.get("type")
    if issue_type is not None and issue_type not in TYPES:
        raise Rejected(f"unknown issue type: {issue_type!r}")

    if item.get("migrate_enhancement") and "enhancement" not in known:
        raise Rejected(f"{repo} has no 'enhancement' label to migrate")

    if "Priority" not in fields and priority is not None:
        raise Rejected("organisation defines no Priority field")

    return {
        "repository": repo,
        "issue": number,
        "labels": labels,
        "priority": priority,
        "type": issue_type,
        "migrate_enhancement": bool(item.get("migrate_enhancement")),
        "rationale": str(item.get("rationale", "")),
    }


def apply(action: dict[str, Any], fields: dict[str, dict[str, int]]) -> None:
    """Perform one validated action against GitHub."""
    repo = action["repository"]
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
        priority_field = fields["Priority"]
        payload = json.dumps(
            {
                "issue_field_values": [
                    {
                        "field_id": priority_field[FIELD_ID_KEY],
                        "value": priority_field[action["priority"]],
                    }
                ]
            }
        )
        put_fields(repo, number, payload)


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


def main() -> None:
    """Validate every proposal, then apply those that survive."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    org = os.environ.get("TRIAGE_ORG", "")
    if not org:
        sys.exit("TRIAGE_ORG is not set")
    only = os.environ.get("TRIAGE_REPOSITORY") or None
    excluded = load_exclusions(os.environ.get("TRIAGE_EXCLUDE_FILE"))

    if not args.proposal.is_file():
        sys.exit(f"no proposal file at {args.proposal}")
    proposal = extract_proposal(args.proposal.read_text(encoding="utf-8"))

    fields = load_field_options(org)
    label_cache: dict[str, set[str]] = {}
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    items: list[Any] = proposal.get("proposals") or []

    for item in items:
        if not isinstance(item, dict):
            rejected.append({"item": repr(item), "reason": "not an object"})
            continue
        entry = cast("dict[str, Any]", item)
        try:
            action = validate(entry, org, only, excluded, label_cache, fields)
        except Rejected as exc:
            rejected.append(
                {
                    "repository": entry.get("repository"),
                    "issue": entry.get("issue"),
                    "reason": str(exc),
                }
            )
            continue
        if not args.dry_run:
            try:
                apply(action, fields)
            except Rejected as exc:
                rejected.append(
                    {
                        "repository": action["repository"],
                        "issue": action["issue"],
                        "reason": f"apply failed: {exc}",
                    }
                )
                continue
        applied.append(action)

    counts = {
        "proposed": len(items),
        "applied": len(applied),
        "rejected": len(rejected),
    }
    result: dict[str, Any] = {
        "dry_run": args.dry_run,
        "applied": applied,
        "rejected": rejected,
        "skipped": proposal.get("skipped", []),
        "injection_attempts": proposal.get("injection_attempts", []),
        "counts": counts,
    }
    args.output_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    verb = "would apply" if args.dry_run else "applied"
    print(f"Proposals: {counts['proposed']}")
    print(f"{verb.capitalize()}: {counts['applied']}")
    print(f"Rejected: {counts['rejected']}")
    for entry in rejected:
        print(
            f"  rejected {entry.get('repository')}#{entry.get('issue')}: "
            f"{entry.get('reason')}"
        )


if __name__ == "__main__":
    main()
