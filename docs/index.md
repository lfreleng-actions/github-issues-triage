<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# GitHub Issues Triage

Scheduled AI triage of GitHub issues. A reusable workflow scans an
organisation's open issues, runs an agent session that applies
category labels per a versioned policy prompt, and attaches full run
evidence to the workflow run.

## Three engines, one pipeline

The agent session is the single provider-specific step. Snapshots,
exclusion filtering, the policy prompt, the label wrapper, the diff
report, and the artefact bundle stay the same whichever engine runs:

<!-- markdownlint-disable MD013 -->

| Engine | `engine` input | Credential | Setup |
| ------ | -------------- | ---------- | ----- |
| Anthropic Claude | `claude` (the default) | `anthropic_api_key` | [ANTHROPIC.md](setup/ANTHROPIC.md) |
| Google Gemini | `gemini` | `gemini_api_key` | [GOOGLE.md](setup/GOOGLE.md) |
| GitHub Copilot | `copilot` | `copilot_token` | [GITHUB.md](setup/GITHUB.md) |

<!-- markdownlint-enable MD013 -->

## How a run works

```text
snapshot (before) -> agent session -> snapshot (after)
                                        -> diff -> report + summary
```

1. Capture the organisation's open-issue state as JSON
2. Skip the agent session when zero unlabelled issues exist
3. Run the selected engine with read and label `gh` verbs, plus the
   policy prompt
4. Capture the state again, diff, and report observed label movement
   — never the agent's own claims — to the step summary
5. Upload the artefact bundle (snapshots, session evidence, prompt,
   report) with 90-day retention, even when the session fails

The report describes what changed, not what the agent said it
changed. That distinction is the point: a session that claims a label
it never applied shows up as a difference between the two snapshots.

## Where to start

- **Running the pipeline against your own organisation** —
  [Setup](setup/README.md) covers the shared GitHub App that performs
  every label write, then the credential each engine needs.
- **Understanding why the pipeline works this way** —
  [Design](development/DESIGN.md) records the architecture, the
  containment model, and the reasoning behind each engine's
  integration.
- **Calling the reusable workflow** — the
  [repository README](https://github.com/lfreleng-actions/github-issues-triage#consuming-the-reusable-workflow)
  carries the consumption example and the full input reference.

## Safety model in brief

Dry-run is the default: consumers opt in to live labelling. The
agent receives read verbs of `gh` plus, in live mode, a
constrained wrapper that validates repository, issue number, and
label existence before a fixed `--add-label` operation. Every
label write travels over the GitHub App token, which stays
separate from the model credential; the Anthropic and Gemini keys
carry no GitHub permissions at all. Issue text counts as data,
never instructions.

Containment differs by engine, so the worst case does too. The
Claude and Gemini engines hold the agent to a tool allow-list the
harness enforces, and a mislabelled issue is the ceiling. The
Copilot engine's allow-list is an approval policy rather than a
filter, and one of its routes reuses the calling job's
`GITHUB_TOKEN`, so the workflow refuses a live run on that engine
until the enforcement in
[Design §13.4](development/DESIGN.md) lands.
