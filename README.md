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
organisation's open issues, runs an agent session — **Claude Code,
the Gemini CLI, or the GitHub Copilot CLI**, selected per run —
that applies category labels per a versioned policy prompt, and
attaches full run evidence to the workflow run: before/after
snapshots, engine-specific session evidence (Claude: a
turn-by-turn transcript; Gemini: telemetry plus the session
summary, with transcript fidelity under verification — design doc
§12; Copilot: CLI logs plus the shared session transcript), and a
diff-based report.

## 📚 Documentation

<https://lfreleng-actions.github.io/github-issues-triage/>

Per-engine setup — credentials, permissions, and the checks that
prove them — lives in [`docs/setup/`](docs/setup/README.md). The
[design document](docs/development/DESIGN.md) covers the
architecture, containment model, and rollout plan in full.

## How it works

```text
snapshot (before) -> agent session -> snapshot (after)
                                        -> diff -> report + summary
```

1. Capture the organisation's open-issue state as JSON
2. Skip the agent session when zero unlabelled issues exist
3. Run the selected engine with read/label `gh` verbs and a policy
   prompt ([`prompt/triage.md`](prompt/triage.md))
4. Capture the state again, diff, and report observed label
   movement — never the agent's own claims — to the step summary
5. Upload the artefact bundle (snapshots, transcript, prompt,
   report) with 90-day retention, even when the session fails

## Consuming the reusable workflow

Other organisations and users can call the pipeline directly,
supplying their own tokens:

<!-- markdownlint-disable MD013 -->

```yaml
jobs:
  triage:
    permissions:
      # Snapshot reads; label writes use the App token instead
      issues: read
      contents: read
    # Pin to an immutable release commit SHA; the tag rides along
    # as a comment. Never reference a mutable branch here: the
    # workflow receives your API key and an issues:write token.
    # yamllint disable-line rule:line-length
    uses: lfreleng-actions/github-issues-triage/.github/workflows/issues-triage.yaml@<commit-sha>  # vX.Y.Z
    with:
      org: 'your-org'
      dry_run: true
      github_app_client_id: ${{ vars.YOUR_APP_CLIENT_ID }}
    secrets:
      anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
      github_app_private_key: ${{ secrets.YOUR_APP_PRIVATE_KEY }}
```

<!-- markdownlint-enable MD013 -->

Pinning the workflow pins its scripts and prompt too: the assets
checkout defaults to the called workflow's own commit.

### Using the Copilot engine

> [!WARNING]
> The Copilot engine holds a weaker containment boundary than the
> other two — see the safety model below and design doc §13.2. Use
> it for evaluation, not for live or scheduled triage.

The `copilot` engine needs no model API key. Grant the calling
job `copilot-requests: write` and hand its `GITHUB_TOKEN` to the
pipeline; usage meters to the organisation, gated on the "Allow
use of Copilot CLI billed to the organization" policy.

<!-- markdownlint-disable MD013 -->

```yaml
jobs:
  triage:
    permissions:
      issues: read
      contents: read
      # Authorises Copilot model requests; carries no repository
      # access of its own.
      copilot-requests: write
    # yamllint disable-line rule:line-length
    uses: lfreleng-actions/github-issues-triage/.github/workflows/issues-triage.yaml@<commit-sha>  # vX.Y.Z
    with:
      org: 'your-org'
      engine: 'copilot'
      dry_run: true
    secrets:
      copilot_token: ${{ secrets.GITHUB_TOKEN }}
```

<!-- markdownlint-enable MD013 -->

Where that policy is unavailable, pass a fine-grained PAT holding
the "Copilot Requests" permission as `copilot_token` instead and
drop the `copilot-requests` grant.

Either way the credential carries **no repository write access**:
labels travel over the App token alone. Note the two routes differ
in what else they carry. A PAT scoped to Copilot Requests reaches
nothing but the model. The caller `GITHUB_TOKEN` also carries
whatever else that job grants — `issues: read` and
`contents: read` in the example above — so the guarantee there is
the absence of write, not the absence of repository access.

The reusable workflow does not declare `copilot-requests` itself:
a called workflow can narrow the caller's permissions but never
widen them, so declaring it there would fail every caller that
runs a different engine. Passing the token in as a secret leaves
that decision with you. Design doc §13.3 covers the reasoning, and
§13.5 records that no run has yet confirmed the grant survives the
hand-off — if it does not, the CLI rejects the token and the step
fails with the evidence bundle intact.

Without a GitHub App the pipeline cannot write: dry-run reports
work with the caller's `github.token` (`issues: read`), and live
runs refuse to start. Applying labels needs an App installed
across the target with `issues: write` and `metadata: read`;
single-repository runs scope the App token to that repository at
mint time.

### Inputs

<!-- markdownlint-disable MD013 -->

| Input | Default | Purpose |
| ----- | ------- | ------- |
| `org` | (required) | GitHub organisation or user to triage |
| `engine` | `claude` | Agent engine: `claude`, `gemini`, or `copilot` |
| `model` | engine default | `claude-opus-5` / `gemini-3.5-flash-lite` / `claude-sonnet-4.6` |
| `dry_run` | `true` | Report intended labels; apply nothing |
| `retriage` | `false` | Re-examine issues that carry labels |
| `skip_agent` | `false` | Plumbing test: skip the agent session |
| `repository` | `''` | Restrict the scan to one repository |
| `exclude_repos` | `''` | Comma-separated repositories to skip |
| `max_turns` | `80` | Agent session turn ceiling (ignored by `copilot`) |
| `egress_policy` | `audit` | harden-runner mode (`audit`/`block`) |
| `egress_allow_config` | `''` | `harden-runner-block-action` config coordinate |
| `github_app_client_id` | `''` | App auth; empty limits runs to dry-run |
| `assets_repository` | this repo | Source of the prompt and scripts |
| `assets_ref` | called workflow's commit | Ref of `assets_repository` to fetch |

| Secret | Required | Purpose |
| ------ | -------- | ------- |
| `anthropic_api_key` | claude runs, unless `skip_agent` | Anthropic API authentication |
| `gemini_api_key` | gemini runs, unless `skip_agent` | Gemini API (AI Studio) authentication |
| `copilot_token` | copilot runs, unless `skip_agent` | Copilot model requests: caller `GITHUB_TOKEN` or a "Copilot Requests" PAT |
| `github_app_private_key` | no | Pairs with `github_app_client_id` |

<!-- markdownlint-enable MD013 -->

## Workflows in this repository

<!-- markdownlint-disable MD013 -->

| Workflow | Purpose | Trigger |
| -------- | ------- | ------- |
| `issues-triage.yaml` | The reusable pipeline | `workflow_call` |
| `issues-triage-cron.yaml` | Org triage caller | 07:00 UTC weekdays / dispatch |
| `testing.yaml` | Secretless plumbing test / manual dry-run | Pull request / dispatch |
| `documentation.yaml` | Build and publish the docs site | Push to `main` / dispatch |
| `release.yaml` | Promote draft release on tag push | Tag push |

<!-- markdownlint-enable MD013 -->

## Safety model

- **Dry-run by default**: consumers opt in to live labelling
- **Tool containment**: the agent receives read verbs of `gh` plus,
  in live mode, a constrained wrapper that validates repository,
  issue number, and label existence before a fixed `--add-label`
  operation — no `gh issue edit`, no `git`, no arbitrary shell
- **Copilot engine caveat**: that engine restricts the model to
  shell tools, denies the mutating `gh` and `git` verbs, and turns
  off built-in MCP servers and custom-instruction loading. Its
  allow-list is an approval policy rather than a filter, though,
  and the CLI keeps auto-approving shell commands it treats as
  reads. Those commands see both credentials in their environment,
  and redaction works by value, so a command that encodes a token
  defeats it. Writes stay shut, but treat this engine as opt-in
  and unsuited to live or scheduled runs until the enforcement in
  design doc §13.4 lands
- **Token scope**: App tokens carry `issues: write` and
  `metadata: read`, down-scoped at mint time, expiring in an hour.
  Model credentials stay separate and never carry repository
  write, leaving the App token as the sole route to a label
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

Build and preview the documentation site locally:

```bash
uv run --no-project --with-requirements docs/requirements.txt \
  mkdocs serve
```

The report generator has a strict type-checking gate
(`basedpyright`, see `pyproject.toml`) and the prompt document
passes the same prose linting as the rest of the repository.
