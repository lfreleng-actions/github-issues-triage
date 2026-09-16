# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validate an agent's triage proposal and apply it.

The agent proposes; this applies. Keeping the two apart means no
agent session ever holds a write-capable credential, so the
containment weaknesses of any particular harness stop mattering
for writes (see DESIGN.md section 13.2).

Everything the agent says is untrusted input. A proposal reaches
GitHub only if it survives every check here: the repository sits
inside the run's organisation, matches the single-repository
restriction and avoids the exclusion list; the issue appeared in
this run's own snapshot, so a proposal cannot reach an issue the
scan never saw; the target is an issue rather than a pull
request; each label already exists in that repository; priority
and type name options the organisation defines; and no human has
set a priority already.

Usage:
    apply_triage.py --proposal <file> --snapshot <before.json>
                    --output-json <file> [--dry-run]

Environment:
    GH_TOKEN             token for the gh CLI
    TRIAGE_ORG           (required) owner every target must match
    TRIAGE_REPOSITORY    restrict targets to this repository name
    TRIAGE_EXCLUDE_FILE  file of excluded repository names
    TRIAGE_RETRIAGE      'true' when labelled issues are in scope
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from triage_github import (
    Rejected,
    apply,
    check_target,
    existing_priority,
    load_field_options,
    load_issue_types,
    repo_labels,
)

PRIORITIES = ("Urgent", "High", "Medium", "Low")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")
MAX_LABELS = 2
# The agent emits one fenced json block; prose around it is for
# humans. Take the last block that parses and carries the key, so
# a worked example earlier in the message cannot win.
FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class Context:
    """Everything a proposal is checked against.

    Gathered once so validation takes the proposal and the run,
    rather than a long tail of positional arguments that invite
    being passed in the wrong order.
    """

    org: str
    only: str | None
    excluded: set[str]
    retriage: bool
    fields: dict[str, dict[str, int]]
    types: set[str]
    snapshot: dict[tuple[str, int], frozenset[str]]
    label_cache: dict[str, set[str]] = field(default_factory=dict)


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


def load_snapshot(path: Path) -> dict[tuple[str, int], frozenset[str]]:
    """Index the run's snapshot by target, carrying its labels.

    Membership answers "did this run's scan actually see this
    issue", which bounds an untrusted proposal to the population
    the scan covered rather than every issue in the organisation.
    """
    entries: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    index: dict[tuple[str, int], frozenset[str]] = {}
    for entry in entries:
        repository: dict[str, Any] = entry.get("repository") or {}
        full_name = str(repository.get("nameWithOwner", ""))
        labels: list[dict[str, Any]] = entry.get("labels") or []
        index[(full_name.lower(), int(entry["number"]))] = frozenset(
            str(label["name"]) for label in labels
        )
    return index


def check_scope(repo: str, ctx: Context) -> None:
    """Refuse targets outside the run's declared scope."""
    if not REPO_RE.match(repo):
        raise Rejected(f"repository fails validation: {repo}")
    owner, _, name = repo.partition("/")
    if owner.lower() != ctx.org.lower():
        raise Rejected(f"repository outside triage scope ({ctx.org}): {repo}")
    if ctx.only and name.lower() != ctx.only.lower():
        raise Rejected(f"run restricted to {ctx.org}/{ctx.only}: {repo}")
    if name.lower() in ctx.excluded:
        raise Rejected(f"repository is excluded from triage: {repo}")


def check_labels(repo: str, item: dict[str, Any], ctx: Context) -> list[str]:
    """Validate the proposed labels against the repository's own."""
    raw: Any = item.get("labels") or []
    if not isinstance(raw, list) or not all(
        isinstance(name, str) for name in cast("list[Any]", raw)
    ):
        raise Rejected("labels must be a list of strings")
    labels = cast("list[str]", raw)
    if len(labels) > MAX_LABELS:
        raise Rejected(f"at most {MAX_LABELS} labels per issue")
    known = repo_labels(repo, ctx.label_cache)
    for name in labels:
        if name not in known:
            raise Rejected(f"label does not exist in {repo}: {name}")
    return labels


def validate(item: dict[str, Any], ctx: Context) -> dict[str, Any]:
    """Check one proposal, returning the actions it authorises."""
    repo = str(item.get("repository", ""))
    number = item.get("issue")
    if not isinstance(number, int):
        raise Rejected(f"issue number is not an integer: {number!r}")

    check_scope(repo, ctx)

    # Bound the proposal to what this run's scan actually saw.
    # Without this, an agent could name any issue in an allowed
    # repository, including ones the scan deliberately passed over.
    key = (repo.lower(), number)
    if key not in ctx.snapshot:
        raise Rejected(f"{repo}#{number} is absent from this run's snapshot")
    snapshot_labels = ctx.snapshot[key]
    if snapshot_labels and not ctx.retriage:
        raise Rejected(f"{repo}#{number} already carries labels; not retriaging")

    check_target(repo, number)
    labels = check_labels(repo, item, ctx)

    # Checked whatever the agent proposed, including nothing: a
    # human-set priority takes the whole issue out of scope, so an
    # omitted priority must not become a way to relabel it.
    current = existing_priority(repo, number)
    if current is not None:
        raise Rejected(f"priority already set to {current}; leaving it")

    priority = item.get("priority")
    if priority is not None:
        if priority not in PRIORITIES:
            raise Rejected(f"unknown priority: {priority!r}")
        if "Priority" not in ctx.fields:
            raise Rejected("organisation defines no Priority field")
        if priority not in ctx.fields["Priority"]:
            raise Rejected(f"organisation defines no Priority option: {priority}")

    issue_type = item.get("type")
    if issue_type is not None and issue_type not in ctx.types:
        raise Rejected(f"organisation defines no issue type: {issue_type!r}")

    migrate = item.get("migrate_enhancement", False)
    if not isinstance(migrate, bool):
        raise Rejected(f"migrate_enhancement must be a boolean: {migrate!r}")
    if migrate:
        if "enhancement" not in snapshot_labels:
            raise Rejected(f"{repo}#{number} does not carry 'enhancement'")
        if "feature" not in repo_labels(repo, ctx.label_cache):
            raise Rejected(f"{repo} has no 'feature' label to migrate to")

    return {
        "repository": repo,
        "issue": number,
        "labels": labels,
        "priority": priority,
        "type": issue_type,
        "migrate_enhancement": migrate,
        "rationale": str(item.get("rationale", "")),
    }


def build_context(snapshot: dict[tuple[str, int], frozenset[str]]) -> Context:
    """Assemble the run's configuration from the environment."""
    org = os.environ.get("TRIAGE_ORG", "")
    if not org:
        sys.exit("TRIAGE_ORG is not set")
    return Context(
        org=org,
        only=os.environ.get("TRIAGE_REPOSITORY") or None,
        excluded=load_exclusions(os.environ.get("TRIAGE_EXCLUDE_FILE")),
        retriage=os.environ.get("TRIAGE_RETRIAGE", "") == "true",
        fields=load_field_options(org),
        types=load_issue_types(org),
        snapshot=snapshot,
    )


def main() -> None:
    """Validate every proposal, then apply those that survive."""
    parser = argparse.ArgumentParser(description="Apply a triage proposal")
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.proposal.is_file():
        sys.exit(f"no proposal file at {args.proposal}")
    if not args.snapshot.is_file():
        sys.exit(f"no snapshot file at {args.snapshot}")

    proposal = extract_proposal(args.proposal.read_text(encoding="utf-8"))
    ctx = build_context(load_snapshot(args.snapshot))

    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    items: list[Any] = proposal.get("proposals") or []

    for item in items:
        if not isinstance(item, dict):
            rejected.append({"item": repr(item), "reason": "not an object"})
            continue
        entry = cast("dict[str, Any]", item)
        try:
            action = validate(entry, ctx)
            if not args.dry_run:
                apply(action, ctx.fields)
        except Rejected as exc:
            rejected.append(
                {
                    "repository": entry.get("repository"),
                    "issue": entry.get("issue"),
                    "reason": str(exc),
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
