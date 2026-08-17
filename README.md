<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🏷️ GitHub Issues Triage

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/github-issues-triage) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/github-issues-triage/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/github-issues-triage)
<!-- prettier-ignore-end -->

Scheduled AI triage of GitHub issues. A reusable workflow scans an
organisation's open issues, runs a Claude Code agent session that
applies category labels per a versioned policy prompt, and attaches
full run evidence to the workflow run: before/after snapshots, the
complete agent session transcript, and a diff-based report.

The [design document](docs/design.md) covers the architecture,
containment model, and rollout plan in full.

## How it works

```text
snapshot (before) -> agent session -> snapshot (after)
                                        -> diff -> report + summary
```

1. Capture the organisation's open-issue state as JSON
2. Skip the agent session when zero unlabelled issues exist
3. Run Claude Code with read/label `gh` verbs and a policy prompt
   ([`prompt/triage.md`](prompt/triage.md))
4. Capture the state again, diff, and report observed label
   movement — never the agent's own claims — to the step summary
5. Upload the artefact bundle (snapshots, transcript, prompt,
   report) with 90-day retention, even when the session fails

## Consuming the reusable workflow

Other organisations and users can call the pipeline directly,
supplying their own tokens:

```yaml
jobs:
  triage:
    permissions:
      issues: write
      contents: read
    uses: lfreleng-actions/github-issues-triage/.github/workflows/issues-triage.yaml@main
    with:
      org: 'your-org'
      dry_run: true
      github_app_client_id: ${{ vars.YOUR_APP_CLIENT_ID }}
    secrets:
      anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
      github_app_private_key: ${{ secrets.YOUR_APP_PRIVATE_KEY }}
```

Without a GitHub App, the workflow falls back to the caller's
`github.token` and labels issues in the calling repository alone —
enough for single-repository consumers. Cross-repository triage
needs an App installed across the target with `issues: write` and
`metadata: read`.

### Inputs

<!-- markdownlint-disable MD013 -->

| Input | Default | Purpose |
| ----- | ------- | ------- |
| `org` | (required) | GitHub organisation or user to triage |
| `model` | `claude-opus-5` | Model passed to Claude Code |
| `dry_run` | `true` | Report intended labels; apply nothing |
| `retriage` | `false` | Re-examine issues that carry labels |
| `repository` | `''` | Restrict the scan to one repository |
| `exclude_repos` | `''` | Comma-separated repositories to skip |
| `max_turns` | `80` | Agent session turn ceiling |
| `egress_policy` | `audit` | harden-runner mode (`audit`/`block`) |
| `egress_allow_config` | `''` | `harden-runner-block-action` config coordinate |
| `github_app_client_id` | `''` | App auth; empty falls back to `github.token` |
| `assets_repository` | this repo | Source of the prompt and scripts |
| `assets_ref` | `main` | Ref of `assets_repository` to fetch |

| Secret | Required | Purpose |
| ------ | -------- | ------- |
| `anthropic_api_key` | yes | Anthropic API authentication |
| `github_app_private_key` | no | Pairs with `github_app_client_id` |

<!-- markdownlint-enable MD013 -->

## Workflows in this repository

<!-- markdownlint-disable MD013 -->

| Workflow | Purpose | Trigger |
| -------- | ------- | ------- |
| `issues-triage.yaml` | The reusable pipeline | `workflow_call` |
| `issues-triage-cron.yaml` | Org triage caller | 07:00 UTC weekdays / dispatch |
| `testing.yaml` | Pipeline dry-run, no writes | Pull request / dispatch |
| `release.yaml` | Promote draft release on tag push | Tag push |

<!-- markdownlint-enable MD013 -->

## Safety model

- **Dry-run by default**: consumers opt in to live labelling
- **Tool containment**: the agent receives read/label `gh` verbs;
  `gh issue edit` joins in live mode alone — no `git`, no
  arbitrary shell
- **Token scope**: App tokens carry `issues: write` and
  `metadata: read`, down-scoped at mint time, expiring in an hour
- **Prompt-injection defence**: the policy prompt instructs the
  agent to treat issue text as data; containment limits the worst
  case to a wrong label
- **Egress control**: harden-runner in audit or block mode, with
  the allow-list loaded from a tagged org baseline

## Development

Run the linting suite before pushing:

```bash
uvx pre-commit run --all-files
```

The report generator has a strict type-checking gate
(`basedpyright`, see `pyproject.toml`) and the prompt document
passes the same prose linting as the rest of the repository.
