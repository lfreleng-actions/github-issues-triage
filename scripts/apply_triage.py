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
    GitHubError,
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
# The agent's proposal is the last fenced json block carrying
# "proposals". There is always more than one candidate: the
# shared transcript echoes the prompt, whose own worked example
# has the same shape. Position decides, not parseability -- a
# malformed final block must reject rather than quietly falling
# back to an earlier one, or the prompt's example could stand in
# for a proposal the agent never made.
FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
PROPOSAL_KEY = '"proposals"'


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
    candidates = [block for block in FENCE_RE.findall(text) if PROPOSAL_KEY in block]
    if not candidates:
        raise Rejected("no json block carrying 'proposals' found")
    try:
        parsed: Any = json.loads(candidates[-1])
    except json.JSONDecodeError as exc:
        raise Rejected(f"the final proposal block is malformed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise Rejected("the final proposal block is not an object")
    return cast("dict[str, Any]", parsed)


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
    """Validate the proposed labels against the repository's own.

    Falsey values are not silently treated as "no labels": the
    contract asks for one or two, so `null` or `0` in that field
    is a malformed proposal rather than an empty one. Empty is
    permitted alongside a migration, which supplies `feature`
    itself.
    """
    raw: Any = item.get("labels")
    if raw is None and item.get("migrate_enhancement") is True:
        raw = []
    if not isinstance(raw, list) or not all(
        isinstance(name, str) for name in cast("list[Any]", raw)
    ):
        raise Rejected(f"labels must be a list of strings: {raw!r}")
    labels = cast("list[str]", raw)
    if not labels and item.get("migrate_enhancement") is not True:
        raise Rejected("propose at least one label")
    if len(labels) > MAX_LABELS:
        raise Rejected(f"at most {MAX_LABELS} labels per issue")
    known = repo_labels(repo, ctx.label_cache)
    for name in labels:
        if name not in known:
            raise Rejected(f"label does not exist in {repo}: {name}")
    return labels


def duplicate_targets(items: list[Any]) -> set[tuple[str, int]]:
    """Targets named more than once across the whole proposal.

    The label ceiling counts per proposal object, so repeating a
    target would let each entry add its own two labels and
    overwrite the type again. Detected before any write, so no
    member of a duplicated group is half-applied.
    """
    seen: dict[tuple[str, int], int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, Any]", item)
        repo = entry.get("repository")
        number = entry.get("issue")
        if isinstance(repo, str) and isinstance(number, int):
            key = (repo.lower(), number)
            seen[key] = seen.get(key, 0) + 1
    return {key for key, count in seen.items() if count > 1}


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

    # Unavailable fields are a configuration fact, not a bad
    # proposal: a user-owned target has no organisation fields,
    # and an App-less dry run cannot read them. Rejecting the
    # whole proposal would discard the label work too, which is
    # everything the pipeline did before issue fields existed. So
    # drop what cannot be written and record why.
    dropped: list[str] = []

    priority = item.get("priority")
    if priority is not None:
        if not isinstance(priority, str):
            raise Rejected(f"priority must be a string: {priority!r}")
        if priority not in PRIORITIES:
            raise Rejected(f"unknown priority: {priority!r}")
        if "Priority" not in ctx.fields:
            dropped.append("priority: no readable organisation Priority field")
            priority = None
        elif priority not in ctx.fields["Priority"]:
            raise Rejected(f"organisation defines no Priority option: {priority}")

    issue_type = item.get("type")
    if issue_type is not None:
        if not isinstance(issue_type, str):
            raise Rejected(f"type must be a string: {issue_type!r}")
        if not ctx.types:
            dropped.append("type: no readable organisation issue types")
            issue_type = None
        elif issue_type not in ctx.types:
            raise Rejected(f"organisation defines no issue type: {issue_type!r}")

    migrate = item.get("migrate_enhancement", False)
    if not isinstance(migrate, bool):
        raise Rejected(f"migrate_enhancement must be a boolean: {migrate!r}")
    if migrate:
        if "enhancement" not in snapshot_labels:
            raise Rejected(f"{repo}#{number} does not carry 'enhancement'")
        if "feature" not in repo_labels(repo, ctx.label_cache):
            raise Rejected(f"{repo} has no 'feature' label to migrate to")
        # apply() removes the label and then adds the proposed
        # set, so naming it in both would put it straight back and
        # the migration would be a no-op that reports success.
        if "enhancement" in labels:
            raise Rejected("cannot migrate 'enhancement' and re-apply it")

    return {
        "repository": repo,
        "issue": number,
        "labels": labels,
        "priority": priority,
        "type": issue_type,
        "migrate_enhancement": migrate,
        "dropped": dropped,
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


def process(
    items: list[Any], ctx: Context, dry_run: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Check each proposal and, unless dry-running, apply it.

    Returns the actions taken, those refused, and those that
    failed part-way — three outcomes that a caller must not
    conflate, since only the last leaves an issue in an unknown
    state.
    """
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    duplicates = duplicate_targets(items)

    for item in items:
        if not isinstance(item, dict):
            rejected.append({"item": repr(item), "reason": "not an object"})
            continue
        entry = cast("dict[str, Any]", item)
        where = {
            "repository": entry.get("repository"),
            "issue": entry.get("issue"),
        }
        if (str(entry.get("repository", "")).lower(), entry.get("issue")) in (
            duplicates
        ):
            rejected.append({**where, "reason": "target proposed more than once"})
            continue
        try:
            action = validate(entry, ctx)
        except Rejected as exc:
            rejected.append({**where, "reason": str(exc)})
            continue
        except GitHubError as exc:
            rejected.append(
                {**where, "reason": f"could not verify against GitHub: {exc}"}
            )
            continue
        if not dry_run:
            try:
                apply(action, ctx.fields)
            except (GitHubError, Rejected) as exc:
                # A write that failed part-way leaves the issue in
                # an unknown state, so it is recorded separately
                # and fails the step rather than counting as a
                # rejection the run can shrug off.
                failed.append({**where, "reason": str(exc)})
                continue
        applied.append(action)

    return applied, rejected, failed


def report(
    applied: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    counts: dict[str, int],
    dry_run: bool,
) -> None:
    """Print the run's outcome, then fail if any write failed."""
    verb = "would apply" if dry_run else "applied"
    print(f"Proposals: {counts['proposed']}")
    print(f"{verb.capitalize()}: {counts['applied']}")
    print(f"Rejected: {counts['rejected']}")
    for action in applied:
        for note in action["dropped"]:
            print(f"  dropped from {action['repository']}#{action['issue']}: {note}")
    for entry in rejected:
        print(
            f"  rejected {entry.get('repository')}#{entry.get('issue')}: "
            f"{entry.get('reason')}"
        )
    if failed:
        print(f"Failed: {counts['failed']}")
        for entry in failed:
            print(
                f"  FAILED {entry.get('repository')}#{entry.get('issue')}: "
                f"{entry.get('reason')}"
            )
        sys.exit("one or more writes failed; see apply-result.json")


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

    try:
        proposal = extract_proposal(args.proposal.read_text(encoding="utf-8"))
    except Rejected as exc:
        # A proposal nobody can read is a failed run, not a quiet
        # no-op: the session spent its budget and produced nothing
        # usable, and the operator needs to see that.
        sys.exit(f"Could not read the agent's proposal: {exc}")
    ctx = build_context(load_snapshot(args.snapshot))

    items: list[Any] = proposal.get("proposals") or []
    applied, rejected, failed = process(items, ctx, args.dry_run)

    counts = {
        "proposed": len(items),
        "applied": len(applied),
        "rejected": len(rejected),
        "failed": len(failed),
    }
    result: dict[str, Any] = {
        "dry_run": args.dry_run,
        "applied": applied,
        "rejected": rejected,
        "failed": failed,
        "skipped": proposal.get("skipped", []),
        "injection_attempts": proposal.get("injection_attempts", []),
        "counts": counts,
    }
    args.output_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    report(applied, rejected, failed, counts, args.dry_run)


if __name__ == "__main__":
    main()
