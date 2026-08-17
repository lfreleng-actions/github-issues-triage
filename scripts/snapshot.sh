#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Capture the open-issue state of an organisation (or one of its
# repositories) as JSON, dropping excluded repositories. The triage
# workflow calls this twice: before and after the agent session.
#
# Arguments:
#   $1  output file for the snapshot JSON
#
# Environment:
#   ORG            (required) organisation or user to scan
#   REPOSITORY     restrict the scan to this repository name
#   EXCLUDE_REPOS  comma-separated repository names to skip;
#                  overrides EXCLUDE_FILE when non-empty
#   EXCLUDE_FILE   file of repository names to skip, one per line,
#                  '#' comments permitted
#   GH_TOKEN       token for the gh CLI
#
# Alongside the snapshot, the script writes the resolved exclusion
# list to excluded-repos.txt in the output directory, so later
# steps (prompt assembly, the report) share one source of truth.

set -euo pipefail

outfile="$1"
outdir="$(dirname "$outfile")"
mkdir -p "$outdir"

excludes=""
if [ -n "${EXCLUDE_REPOS:-}" ]; then
  excludes="$(printf '%s' "$EXCLUDE_REPOS" | tr ',' '\n')"
elif [ -n "${EXCLUDE_FILE:-}" ] && [ -f "${EXCLUDE_FILE}" ]; then
  excludes="$(sed -e 's/#.*$//' -e 's/[[:space:]]//g' \
    "${EXCLUDE_FILE}" | grep -v '^$' || true)"
fi
printf '%s\n' "$excludes" > "$outdir/excluded-repos.txt"

args=(--owner "$ORG" --state open --limit 1000
  --json 'repository,number,title,url,labels,createdAt,updatedAt')
if [ -n "${REPOSITORY:-}" ]; then
  args+=(--repo "$ORG/$REPOSITORY")
fi

gh search issues "${args[@]}" > "$outfile.raw"
jq --arg excl "$excludes" '
  ($excl | split("\n") | map(select(length > 0))) as $list
  | map(select(.repository.name as $n | ($list | index($n)) | not))
' "$outfile.raw" > "$outfile"
rm -f "$outfile.raw"

count="$(jq 'length' "$outfile")"
echo "Snapshot: $count open issue(s) -> $outfile"
