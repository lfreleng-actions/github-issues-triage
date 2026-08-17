#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Constrained label applicator for the triage agent.
#
# The agent's live-mode tool allow-list grants this wrapper and not
# gh issue edit, because the latter authorises every issue mutation
# (titles, bodies, assignees, milestones, arbitrary label removal).
# The wrapper accepts a validated repository, issue number, and one
# or two labels that exist in the repository, then performs a fixed
# --add-label operation. A single narrowly coded migration replaces
# the retired 'enhancement' label with 'feature'.
#
# Beyond syntax, the wrapper enforces the workflow's resolved scope:
# the target must sit inside TRIAGE_ORG, match TRIAGE_REPOSITORY
# when set, avoid every repository listed in TRIAGE_EXCLUDE_FILE,
# and resolve to a real issue rather than a pull request (the
# issues API also serves pull requests; those carry a pull_request
# field). A confused or prompt-injected agent therefore cannot
# label out-of-scope repositories or pull requests.
#
# Usage:
#   apply-label.sh <owner/repo> <issue-number> <label> [<label>]
#   apply-label.sh --migrate-enhancement <owner/repo> <issue-number>
#
# Environment:
#   GH_TOKEN             token for the gh CLI
#   TRIAGE_ORG           (required) owner the target must belong to
#   TRIAGE_REPOSITORY    restrict targets to this repository name
#   TRIAGE_EXCLUDE_FILE  file of excluded repository names

set -euo pipefail

die() {
  echo "apply-label.sh: $*" >&2
  exit 1
}

migrate="false"
if [ "${1:-}" = "--migrate-enhancement" ]; then
  migrate="true"
  shift
fi

[ "$#" -ge 2 ] || die "expected <owner/repo> <issue-number> [labels]"
repo="$1"
number="$2"
shift 2

case "$repo" in
  */*) ;;
  *) die "repository must take owner/repo form: $repo" ;;
esac
printf '%s' "$repo" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$' \
  || die "repository fails validation: $repo"
printf '%s' "$number" | grep -Eq '^[0-9]+$' \
  || die "issue number fails validation: $number"

[ -n "${TRIAGE_ORG:-}" ] || die "TRIAGE_ORG is not set"
owner="${repo%%/*}"
name="${repo#*/}"
[ "$owner" = "$TRIAGE_ORG" ] \
  || die "repository outside triage scope ($TRIAGE_ORG): $repo"
if [ -n "${TRIAGE_REPOSITORY:-}" ] && [ "$name" != "$TRIAGE_REPOSITORY" ]
then
  die "run restricted to $TRIAGE_ORG/$TRIAGE_REPOSITORY: $repo"
fi
if [ -n "${TRIAGE_EXCLUDE_FILE:-}" ] && [ -f "$TRIAGE_EXCLUDE_FILE" ]; then
  grep -Fxq -- "$name" "$TRIAGE_EXCLUDE_FILE" \
    && die "repository is excluded from triage: $repo"
fi

# The issues API also serves pull requests; reject those. This call
# doubles as an existence check for the target issue.
kind="$(gh api "repos/$repo/issues/$number" \
  --jq 'if .pull_request then "pull-request" else "issue" end')" \
  || die "cannot fetch $repo#$number"
[ "$kind" = "issue" ] \
  || die "target is a pull request, not an issue: $repo#$number"

if [ "$migrate" = "true" ]; then
  [ "$#" -eq 0 ] || die "--migrate-enhancement accepts no labels"
  gh issue edit "$number" --repo "$repo" \
    --remove-label 'enhancement' --add-label 'feature'
  echo "Migrated enhancement -> feature on $repo#$number"
  exit 0
fi

[ "$#" -ge 1 ] || die "expected at least one label"
[ "$#" -le 2 ] || die "two labels at most per issue"

known="$(gh label list --repo "$repo" --limit 200 --json name \
  --jq '.[].name')"
add=""
for label in "$@"; do
  printf '%s\n' "$known" | grep -Fxq -- "$label" \
    || die "label does not exist in $repo: $label"
  add="${add:+$add,}$label"
done

gh issue edit "$number" --repo "$repo" --add-label "$add"
echo "Applied to $repo#$number: $add"
