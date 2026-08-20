<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Design: Scheduled AI Triage of GitHub Issues

**Status:** Draft
**Repository:** `lfreleng-actions/github-issues-triage`
**Author:** Matthew Watkins (AI-drafted, human-reviewed)
**Last updated:** 2026-08-17

## 1. Problem Statement

Open issues across the `lfreleng-actions` organisation routinely sit
unlabelled. The daily report produced by
[`github-security-report-action`](https://github.com/lfreleng-actions/github-security-report-action)
surfaces this as the **Untriaged** column of its GitHub Issues table —
at one recent count, 25 of 37 open issues carried no labels at all.
Unlabelled issues do not appear in release-drafter categories, evade
maintainers' label filters, and skew the org's backlog-hygiene signal.

Manual triage works (a one-off AI-assisted pass cleared the backlog in
August 2026) but does not stay done. New issues arrive daily; the org
needs a recurring, automated triage pass.

## 2. Goal

A scheduled GitHub Actions workflow in this repository that:

1. Runs at **07:00 UTC every weekday** (`cron: '0 7 * * 1-5'`)
2. Scans **all open issues across the `lfreleng-actions` org**
3. Drives an agent harness running **Anthropic Claude Opus 5**
4. Applies labels per the org's canonical taxonomy (see §6)
5. Reports what it did (step summary, and optionally Slack)

Closing the loop: `github-security-report-action` *surfaces* the
untriaged backlog; this pipeline *clears* it. Together the Untriaged
column should trend to zero and stay there.

### Non-goals (initial release)

- Closing, reopening, or commenting on issues
- Editing issue titles or bodies
- Assigning issues to people or milestones
- Triage of pull requests (the PR autolabeler in
  `lfreleng-actions/.github` already covers those)
- Creating labels that do not already exist in a repository

Each of these is a possible later phase, gated behind explicit
configuration, but the initial release applies labels and nothing
else: labels are low-risk, reversible, and auditable.

## 3. Model Access: Copilot vs. Anthropic API

Two candidate routes to Claude models existed at design time; one
was viable then. GitHub has since shipped the route the other
assessment ruled out — see §13.

### 3.1 GitHub Copilot subscription — superseded ⚠️

The original assessment recorded here: the org-level Copilot
subscription exposes Anthropic models **within Copilot surfaces**
(IDE chat, Copilot CLI, the Copilot coding agent) and nowhere
else. There is no general-purpose API entitlement, and no
supported harness exists for "run Claude Code against the Copilot
backend". GitHub Models (the `models` permission on
`GITHUB_TOKEN`) offers API access to a catalogue of models, but
Anthropic models are not part of that catalogue and its rate
limits target experimentation, not production batch jobs.

Most of that still holds: no general-purpose API entitlement
exists, and GitHub Models remains the wrong tool. The conclusion
drawn from it does not. Copilot CLI now carries a supported
**programmatic mode** and documented GitHub Actions integration,
which makes the Copilot surface itself a usable harness rather
than a dead end. Section 13 records the third engine that follows
from it.

### 3.2 Anthropic API key — recommended ✅

The org holds paid Anthropic subscriptions and can issue API keys. The
official [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action)
accepts an `anthropic_api_key` input and runs Claude Code
non-interactively against a prompt. This is Anthropic's supported path
for CI automation.

- Secret: `ANTHROPIC_API_KEY`, stored as a **repository Actions
  secret** on this repository (not org-wide — no other pipeline needs
  it, and scoping narrowly limits blast radius). If the org later
  standardises on 1Password retrieval
  (`1password-secrets-action` / `credential-load-action`), migration
  is a two-line change.
- Recommended hygiene: a **dedicated Anthropic workspace** for this
  pipeline so spend is attributable and capped, and the key is
  rotatable without touching other consumers.

An alternative — authenticating Claude Code with a Claude Pro/Max
subscription OAuth token (`claude_code_oauth_token`) — works with
the action but ties the pipeline to an individual's subscription and
its consumer rate limits. Not appropriate for org infrastructure.

## 4. Agent Harness

### 4.1 Options considered

<!-- markdownlint-disable MD013 -->

| Option | Verdict |
| ------ | ------- |
| `anthropics/claude-code-action` (official) | ✅ **Selected** |
| Bespoke script calling the Messages API | ❌ Re-implements tool-use loop, retries, context mgmt |
| Copilot coding agent | ❌ Repo-scoped, PR-oriented, wrong shape; the CLI is the usable Copilot surface (§13) |
| Self-hosted agent framework | ❌ Heavyweight; nothing to gain over Claude Code here |

<!-- markdownlint-enable MD013 -->

`claude-code-action` runs Claude Code in "agent mode" when given a
`prompt` input: no human in the loop, tool use (including `gh` via
Bash) available, and `claude_args` passes through CLI arguments such
as `--model` and `--allowedTools`.

### 4.2 Invocation sketch

```yaml
- uses: anthropics/claude-code-action@<commit-sha>  # vX.Y.Z
  with:
    anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
    github_token: ${{ steps.app-token.outputs.token }}
    prompt: ${{ steps.build-prompt.outputs.prompt }}
    claude_args: >-
      --model claude-opus-5
      --max-turns 80
      --allowedTools "Bash(gh issue list:*),Bash(gh issue view:*),
      Bash(gh issue edit:*),Bash(gh label list:*),
      Bash(gh search issues:*)"
```

Notes:

- **Pin by commit SHA** (org rule: never bare version tags, never tag
  object SHAs). Resolve the SHA at implementation time.
- **Model identifier**: confirm the exact API model string for Opus 5
  (`claude-opus-5-…`) against the Anthropic model list when
  implementing; treat it as a workflow-level `env` value so bumps are
  one-line changes.
- **Tool allow-list is the primary containment**: the agent gets
  read/label verbs of `gh` and nothing more — no `git`, no file
  writes that matter, no arbitrary shell. Even a fully confused agent
  cannot close issues, comment, or push code, because the token (§5)
  and the tool list both forbid it.
- `--max-turns` bounds runaway loops; the job also carries a
  `timeout-minutes` ceiling (suggest 30).

### 4.3 Cost control

Opus-class models are expensive. Two mitigations:

1. **Pre-filter before invoking the model.** A cheap `gh search
   issues --owner lfreleng-actions --state open` step lists unlabelled
   issues first. If there are none (the steady state), the workflow
   **skips the model call entirely** — a daily no-op costs one API-free
   minute of runner time.
2. **Batch, don't fan out.** One agent session triages all pending
   issues in a single run rather than one session per issue. Volume is
   modest (tens of issues at worst after a weekend).

A weekday 07:00 UTC schedule (rather than daily) matches the ask and
avoids paying for weekend runs into an empty queue.

## 5. GitHub Authentication and Permissions

The default `GITHUB_TOKEN` covers this repository alone; the
pipeline must label issues **org-wide**, so it will not suffice.

### 5.1 Recommended: the existing org bot GitHub App

The org already operates a bot App (used in `test-release-process` via
`vars.LF_RELENG_BOT_CLIENT_ID` / `secrets.LF_RELENG_BOT_PRIVATE_KEY`).
Reuse it — or, if its installation permissions are broader than
needed, create a sibling `lf-releng-triage` App. Requirements:

- **Permissions:** `issues: write`, `metadata: read` — nothing else
- **Installation:** all repositories in `lfreleng-actions`
- **Token minting in-workflow:**

```yaml
- uses: actions/create-github-app-token@<commit-sha>  # vX.Y.Z
  id: app-token
  with:
    client-id: ${{ vars.LF_RELENG_BOT_CLIENT_ID }}
    private-key: ${{ secrets.LF_RELENG_BOT_PRIVATE_KEY }}
    owner: lfreleng-actions
    permission-issues: write
    permission-metadata: read
```

The `permission-*` inputs down-scope the minted token even if the App
itself holds more, and the token expires after one hour — well inside
the job timeout. App-authored label events also carry attribution
in each issue's timeline (`lf-releng-bot` avatar), which
matters for auditability.

### 5.2 Rejected: fine-grained PAT

A PAT bound to a human account works but rots when that human leaves,
is harder to down-scope per-run, and attributes bot actions to a
person. Treat it as a stopgap if App reuse stalls.

## 6. Triage Policy (the prompt's contract)

The label taxonomy is **already canonical** in the org: the PR
autolabeler in `.github/.github/release-drafter.yml` maps Conventional
Commit types to labels, and the August 2026 manual triage pass applied
the same scheme to issues. The agent must follow it, not invent one.

| Label | Apply when the issue… |
| ----- | --------------------- |
| `bug` | reports incorrect behaviour |
| `feature` | requests new capability (replaces retired `enhancement`) |
| `documentation` | concerns docs/README/CONTRIBUTING content |
| `code-quality` | concerns linting, typing, test coverage, hygiene |
| `CI` | concerns workflows, runners, pre-commit infrastructure |
| `chore` | tracks maintenance (archival, dep housekeeping) |
| `refactor` | requests restructuring without behaviour change |
| `breaking-change` | describes/implies a compatibility break |
| `performance` | concerns speed or resource use |

Hard rules encoded in the prompt:

1. **Add existing labels, nothing else**
   (`gh label list` first; never create labels).
2. **One-to-two labels per issue** — a primary category, plus at most
   one secondary (e.g. `bug,code-quality` for a broken linter config).
3. **Never remove labels a human applied**, with one standing
   exception: migrate `enhancement` → `feature` (org convention).
4. **Skip issues that already have labels** unless run with the
   `retriage` input (see §7).
5. **When genuinely uncertain, apply `question`** rather than
   guessing a category — that label *is* the "needs human" signal.
6. Report every action taken (and every skip, with reason) in a final
   summary written to `$GITHUB_STEP_SUMMARY`.

The report's own classification
(`github-security-report-action`'s `DEFAULT_ISSUE_LABELS`) buckets
`bug|defect` → Bug, `feature|enhancement` → Feature,
`documentation|docs` → Docs; the remaining labels above land in the
report's Other column. That is fine — Other means "labelled, but not
Bug/Feature/Docs", a healthy state.

The prompt lives in the repository as `prompt/triage.md` (versioned,
reviewable, diffable); the workflow loads it at run time rather than
inlining it in YAML.

## 7. Workflow Design

The job below lives in the **reusable** workflow
(`issues-triage.yaml`); the scheduled caller
(`issues-triage-cron.yaml`) contributes the triggers and org
secrets. See §7.3 for the split.

```text
Triggers (caller):
  schedule: '0 7 * * 1-5'      # 07:00 UTC, Mon-Fri
  workflow_dispatch:            # manual runs
    inputs:
      retriage:  boolean, default false   # re-examine labelled issues
      dry_run:   boolean, default false   # report, apply nothing
      repository: string, optional        # limit scope to one repo

Jobs:
  scan (ubuntu-latest, timeout-minutes: 30)
    permissions: {}             # job-level; App token carries the writes
    steps:
      1. harden-runner-block-action  -> loads org egress allow-list
      2. step-security/harden-runner -> egress-policy: block
      3. actions/create-github-app-token -> issues:write org token
      4. checkout (this repo, for the prompt file)
      5. snapshot (before): dump org-wide open-issue state to
         artefacts/before.json; derive unlabelled count
         -> if zero and !retriage: write summary, upload snapshot,
            exit success
      6. anthropics/claude-code-action -> the triage session
         (session transcript captured; see 7.2)
      7. snapshot (after): same dump to artefacts/after.json
      8. report: diff snapshots + parse transcript ->
         artefacts/triage-report.{json,md} and $GITHUB_STEP_SUMMARY
      9. actions/upload-artifact (always(), even on failure)
     10. (optional) Slack notification via existing org mechanism
```

Design points:

- **`concurrency`**: `group: issues-triage, cancel-in-progress: false`
  — never let two triage sessions race; queue instead.
- **Note on schedule drift**: GitHub `schedule` events can fire late
  under load. 07:00 UTC is a target, not a guarantee; nothing in this
  design is time-critical.
- **Dry-run mode** maps to removing `gh issue edit` from
  `--allowedTools` and telling the agent to report intended labels
  without applying them. First week of operation should run dry with
  human review of the proposals.
- **Idempotence**: a re-run over an already-triaged org is a no-op by
  construction (step 5 short-circuits; rule 4 in §6 guards the rest).

### 7.1 Egress (harden-runner)

The org runs `step-security/harden-runner` in **block** mode, with
`harden-runner-block-action` loading the allow-list from the newest
tagged file in the `.github` special repository. The org allow-list
(`.github/harden-runner/lfreleng-actions/allow_list.txt`) contains
**no Anthropic endpoints** today. Required additions:

```text
api.anthropic.com:443
```

plus whatever telemetry/statsig endpoints the pinned Claude Code
release requires. The rollout sequence:

1. The reusable workflow exposes an `egress_policy` input
   (default `audit`), passed straight to harden-runner.
2. Initial scheduled runs use **audit** mode; the harden-runner
   report captures the agent session's outbound endpoints.
3. A PR against `lfreleng-actions/.github` adds those endpoints to
   the allow-list.
4. The org caller flips `egress_policy` to **block**, with
   `harden-runner-block-action` loading the updated tagged list.

Whether the endpoints belong in the shared
baseline or in a **supplemental per-repo allow-list** is the
question of [`lfreleng-actions/.github#161`](https://github.com/lfreleng-actions/.github/issues/161);
if that lands first, use it — an Anthropic API grant is a
single-consumer permission and should not be fleet-wide. Until then,
additions go to the shared list by PR against `lfreleng-actions/.github`.

### 7.2 Reporting and Run Artefacts

Every run must be fully reconstructable after the fact: what the org
looked like before, what the agent saw, what it decided and why, and
what the org looked like afterwards. Three mechanisms deliver this.

#### Before/after snapshots

Steps 5 and 7 dump the complete org-wide open-issue state (repo,
number, title, labels, timestamps) via a single `gh search issues
--json` call each, to `artefacts/before.json` and
`artefacts/after.json`. The snapshots serve two purposes:

1. The report step diffs them to compute **ground truth** for what
   changed — the summary reports observed label changes, not the
   agent's claims about what it did. Any divergence between the two
   (agent claims X, diff shows Y) is itself flagged in the summary.
2. They are cheap insurance: if a run ever needs reverting, the
   before-snapshot is the authoritative record of prior state.

#### Agent session transcript

The pipeline captures the **entire agent session** and attaches it to
the run, so we can debug the triage logic turn by turn:

- `claude-code-action` writes a structured execution log (JSON,
  turn-by-turn: prompts, tool calls, tool results, token usage) and
  exposes its path as a step output. The workflow copies that file to
  `artefacts/session-transcript.json` verbatim.
- The transcript also yields **cost telemetry** (turns used,
  input/output tokens, model id), which the report step folds into
  the step summary so spend per run is visible without opening the
  Anthropic console.

Transcript content is issue text from public repositories plus agent
reasoning — nothing sensitive by construction. Actions masks
registered secrets in logs, and the API key is never part of the
transcript. Should the org later point this pipeline at private
repositories, GitHub already restricts artefact access to users with
read access to this repository's Actions runs — but revisit the
decision then.

#### Artefact upload

A single `actions/upload-artifact` step (SHA-pinned) runs with
`if: always()` so failed or timed-out sessions still surface whatever
the run captured — the failure cases are precisely the ones needing
the transcript. The **agent step carries its own 20-minute timeout**
inside the job's 30-minute ceiling: a session that hits it fails that
step alone, leaving the evidence steps guaranteed time to run (a
job-level timeout would cancel `always()` steps too):

```yaml
- uses: actions/upload-artifact@<commit-sha>  # vX.Y.Z
  if: always()
  with:
    name: issues-triage-${{ github.run_id }}
    path: artefacts/
    retention-days: 90
    include-hidden-files: false
```

Contents:

| File | Purpose |
| ---- | ------- |
| `before.json` | Org issue state before the session |
| `after.json` | Org issue state after the session |
| `session-transcript.json` | Full structured agent session (debugging) |
| `prompt.md` | The exact assembled prompt the session received |
| `triage-report.json` | Machine-readable diff + per-issue actions |
| `triage-report.md` | The same report as rendered in the summary |

90-day retention (vs. the 30-day default) keeps a rolling quarter of
triage history for trend analysis and post-hoc debugging without
notable storage cost at these file sizes.

#### Step summary (`$GITHUB_STEP_SUMMARY`)

The summary is the at-a-glance view; artefacts carry the detail. It
must show the **before/after** movement as well as the actions taken:

1. **Headline counts** — issues scanned / triaged / skipped / errors,
   turns and tokens used, dry-run flag.
2. **Before/after table** — per repository: open issues, untriaged
   before, untriaged after (mirroring the security report's Issues
   table, so the two documents read side by side).
3. **Actions table** — one row per touched issue: repo, issue link,
   labels before → labels after, one-line rationale quoted from the
   agent.
4. **Skips and errors** — every issue examined but left alone, with
   reason (already labelled / `question` applied / API error).
5. **Artefact pointer** — link to the run's artefact bundle.

- **On failure or non-empty run (optional, phase 2):** Slack message
  to the existing org channel using the org-wide Slack
  secrets/variables already consumed by `project-reporting-tool` and
  `github-security-report-action`.
- The next morning's security report independently verifies the
  outcome: its Untriaged column is the external metric of success.

### 7.3 Modular consumption (reusable workflow)

The pipeline splits into a **reusable workflow** carrying the whole
job, and a **thin scheduled caller** carrying org-specific choices.
This mirrors the `generic-workflows` pattern the org already uses,
and lets other organisations (or individuals) consume the pipeline
by supplying their own tokens — no fork required.

<!-- markdownlint-disable MD013 -->

| File | Role |
| ---- | ---- |
| `.github/workflows/issues-triage.yaml` | Reusable (`workflow_call`): the full triage job |
| `.github/workflows/issues-triage-cron.yaml` | Org caller: schedule, dispatch inputs, secrets |

<!-- markdownlint-enable MD013 -->

Interface of the reusable workflow:

<!-- markdownlint-disable MD013 -->

| Input | Default | Purpose |
| ----- | ------- | ------- |
| `org` | (required) | GitHub organisation or user to triage |
| `engine` | `claude` | Agent engine: `claude`, `gemini`, or `copilot` |
| `model` | engine default | Model; empty resolves per engine (§12, §13) |
| `dry_run` | `true` | Report intended labels; apply nothing |
| `retriage` | `false` | Re-examine issues that carry labels |
| `skip_agent` | `false` | Plumbing test: skip the agent session (secretless) |
| `repository` | `''` | Restrict the scan to one repository |
| `exclude_repos` | `''` | Comma-separated repositories to skip |
| `max_turns` | `80` | Agent session turn ceiling (no effect on `copilot`, §13.2) |
| `egress_policy` | `audit` | harden-runner mode (`audit`/`block`) |
| `egress_allow_config` | `''` | `harden-runner-block-action` config coordinate (block mode) |
| `github_app_client_id` | `''` | App auth; empty limits runs to dry-run |
| `assets_repository` | this repo | Where to fetch the prompt and report script |
| `assets_ref` | called workflow's commit | Ref of `assets_repository` to fetch |

| Secret | Required | Purpose |
| ------ | -------- | ------- |
| `anthropic_api_key` | claude runs, unless `skip_agent` | Anthropic API authentication |
| `gemini_api_key` | gemini runs, unless `skip_agent` | Gemini API (AI Studio) authentication |
| `copilot_token` | copilot runs, unless `skip_agent` | Copilot model requests: caller `GITHUB_TOKEN` or a "Copilot Requests" PAT (§13.3) |
| `github_app_private_key` | no | Pairs with `github_app_client_id` |

<!-- markdownlint-enable MD013 -->

Defaults favour safe onboarding: a first-time consumer gets a
dry-run in audit mode scoped by their own token. Without App
credentials the pipeline cannot write: the job's own token
carries `issues: read` for snapshots, and **live runs fail fast**
rather than spend an agent session discovering they cannot label.
Applying labels — in one repository or across an organisation —
needs the App (or a PAT passed as the private-key secret
alternative, discouraged per §5.2); single-repository runs scope
the App token to that repository at mint time.

## 8. Repository Layout

This repository started from `workflows-template`, which
targets *reusable-workflow* repositories. This pipeline is a **caller**
workflow, not a reusable one, so much of the skeleton does not apply.

### Keep (from `workflows-template`)

- `.pre-commit-config.yaml`, `.gitlint`, `.yamllint`, `.editorconfig`,
  `.ruff.toml`, `.gitignore` — org-standard linting
- `.github/workflows/release.yaml` — **required**: promotes draft
  releases to full releases on tag push (thin caller for the
  `generic-workflows` release reusable)
- `.github/workflows/openssf-scorecard.yaml`, `release-drafter.yaml`,
  `clear-action-cache.yaml`
- `.github/actionlint.yaml`, `.github/dependabot.yml`
- `.readthedocs.yml` — kept: this repository now carries `docs/`
- `LICENSES/`, `SECURITY.md`

### Remove (template skeletons that do not apply)

- `.github/workflows/build-test.yaml`
- `.github/workflows/build-test-release.yaml`
- `.github/workflows/merge.yaml`
- `.github/workflows/testing.yaml` — the template version
  exercised the removed `build-test.yaml` skeleton; **replaced** by
  a triage-specific test. Pull request runs are **secretless**: a
  same-repository PR can alter the local reusable workflow, so PR
  checks skip the agent session (`skip_agent`) and prove the
  plumbing from the PR head with no secret in reach. The full
  agent dry-run runs from `workflow_dispatch`, a maintainer-
  triggered, trusted execution path.
- `examples/` (build-test-release / merge examples)

### Import from `actions-template`

- `.aislop/config.yml` — AI-slop scan configuration (workflows-template
  lacks it; this repo will carry a sizeable prompt document and the
  scanner needs configuring for it)
- `.github/workflows/tag-push.yaml` — tag-push automation, if we
  version/release this pipeline's prompt+workflow as a unit
- `README.md` structure as a reference for badge/header conventions
  (rewrite content for this repo)

### Add (new)

```text
docs/development/DESIGN.md                   # this document
docs/setup/README.md                         # setup index
docs/setup/ANTHROPIC.md                      # Claude engine setup
docs/setup/GOOGLE.md                         # Gemini engine setup
docs/setup/GITHUB.md                         # Copilot engine setup
prompt/triage.md                             # the agent's triage policy
config/excluded-repos.txt                    # repos the scan skips
scripts/snapshot.sh                          # issue-state capture
scripts/triage_report.py                     # snapshot diff -> report
.github/workflows/issues-triage.yaml         # reusable (workflow_call)
.github/workflows/issues-triage-cron.yaml    # scheduled thin caller
.github/workflows/testing.yaml               # PR dry-run of the above
README.md                                    # rewritten for this repo
```

## 9. Failure Modes and Mitigations

<!-- markdownlint-disable MD013 -->

| Failure | Mitigation |
| ------- | ---------- |
| Anthropic API outage / 529s | Job fails visibly; next weekday run catches up. No retry storm: one run per day. |
| Agent mislabels an issue | Labels are reversible; step summary makes every action reviewable; humans can relabel (rule 3 stops the agent undoing them). |
| Agent goes off-script | `--allowedTools` denies every verb except issue reads plus a constrained label wrapper (validated repo/number/labels, org/repo scope and exclusion enforcement, pull requests rejected, `--add-label` and the coded enhancement migration, nothing else); `--max-turns` and step/job timeouts bound the session. |
| Prompt-injection via issue body | Real risk: issue text arrives as untrusted input. Containment as above — worst case within the sandbox is a wrong label on some issue, not code execution or data exfiltration (egress is allow-listed). Prompt instructs the agent to treat issue bodies as data, never as instructions. |
| Rate limits (GitHub) | ~120 open issues org-wide at worst; App tokens get 15k req/hr. Not a concern at this scale. |
| Cost runaway | Pre-scan short-circuit (§4.3), max-turns, weekday schedule, dedicated Anthropic workspace with a spend cap. |
| Secret leakage in logs | Actions masks registered secrets; Claude Code does not echo its key; harden-runner blocks unexpected egress. Transcript artefacts contain public issue text and nothing else (§7.2). |
| Session dies mid-run, no evidence | The agent step's own 20-minute timeout fails it inside the job ceiling; artefact upload runs `if: always()`, so snapshots and partial transcript survive. A missing after-snapshot renders the report "incomplete", never a fabricated zero diff. |
| Agent self-reports inaccurately | The report step builds the summary from the before/after snapshot diff, not the agent's claims, and flags divergence. |

<!-- markdownlint-enable MD013 -->

## 10. Open Questions

1. **App reuse vs. new App** — does `LF_RELENG_BOT` already have
   `issues: write` on all repos, and are we comfortable widening its
   use, or do we mint a dedicated `lf-releng-triage` App? (Owner:
   org admins.)
2. **Exact Opus 5 model string** — confirm against the live model
   catalogue at implementation time.
3. **Claude Code egress set** — harvest via an audit-mode run
   (§7.1).
4. **Slack in phase 1 or phase 2?** — the step summary may be enough
   given the security report already tells us daily whether the
   backlog is clear.
5. **Excluded repositories** — the security report excludes
   `project-reporting-artifacts`, `test-tags-calver`,
   `test-tags-semantic`; the triage prompt should honour the same
   exclusion list. Share it via a config file in this repo rather
   than hard-coding in the prompt.
6. **Transcript output contract** — resolved: `claude-code-action`
   v1 (verified at v1.0.193) exposes the structured execution log
   path as the `execution_file` output; the artefact step in §7.2
   consumes it.

## 11. Rollout Plan

1. **PR 1** — repo tidy-up: remove non-applicable template skeletons,
   import `actions-template` pieces, rewrite `README.md`, land this
   document.
2. **PR 2** — `prompt/triage.md` + `issues-triage.yaml` with the model
   step stubbed to dry-run; egress allow-list PR against
   `lfreleng-actions/.github` in parallel.
3. **Secrets** — create the Anthropic workspace + API key; add
   `ANTHROPIC_API_KEY` repo secret; confirm App variables/secrets are
   visible to this repo.
4. **Week 1** — scheduled dry-runs; human reviews the proposed labels
   each morning.
5. **Enable writes** — flip dry-run off; watch the security report's
   Untriaged column.
6. **Phase 2 (separate design discussion)** — Slack reporting,
   duplicate detection, stale-issue nudges, PR triage.

## 12. Second Engine: Google Gemini

**Status:** Implemented; resolved decisions recorded below.

IT direction steers most agentic work towards Google
Gemini, so the pipeline gains a second agent engine. The
architecture already isolates the provider: snapshots, exclusion
filtering, the policy prompt, the label wrapper, diff reporting,
and artefact capture are all engine-neutral. The agent session is
the single provider-specific step.

### 12.1 Engine selection

The reusable workflow gains an `engine` input (`claude` |
`gemini`, default `claude`), selecting between two mutually
exclusive agent-session steps. One reusable workflow, one
evidence pipeline, one policy prompt — two interchangeable
engines. A separate reusable per engine would duplicate the
evidence pipeline and let the copies drift; the single-workflow
design keeps every hardening decision (§5–§7) applying to both
engines by construction.

### 12.2 Gemini harness

[`google-github-actions/run-gemini-cli`](https://github.com/google-github-actions/run-gemini-cli)
(Google's official action, verified at v0.1.22) runs the Gemini
CLI non-interactively against a `prompt` input — the same shape as
`claude-code-action`. Key mappings:

<!-- markdownlint-disable MD013 -->

| Concern | Claude engine | Gemini engine |
| ------- | ------------- | ------------- |
| Harness | `anthropics/claude-code-action` | `google-github-actions/run-gemini-cli` |
| Prompt | `prompt` input (assembled §6) | `prompt` input (same assembly, unchanged) |
| Model | `--model` via `claude_args` | `gemini_model` input |
| Tool containment | `--allowedTools` grant list | `settings` JSON: `tools.core` with `run_shell_command(...)` allow-list |
| Session transcript | `execution_file` output | `upload_artifacts` / CLI telemetry log (verify exact contract) |
| Turn ceiling | `--max-turns` | `model.maxSessionTurns` in settings JSON |

<!-- markdownlint-enable MD013 -->

The tool containment translates directly: the Gemini CLI's
`tools.core` allow-list grants specific shell commands, so the
Gemini session receives the same read verbs plus the same
constrained `apply-label.sh` wrapper in live mode — the wrapper's
scope/exclusion/PR-rejection enforcement is engine-independent by
design.

### 12.3 Authentication

Resolved: **`GEMINI_API_KEY`** (AI Studio), a repository secret
mirroring the Anthropic arrangement (§3.2). Vertex AI via
Workload Identity Federation (keyless OIDC) remains available as
a later increment — the harness supports it — without breaking
the interface.

The default Gemini model is **`gemini-3.5-flash-lite`** (IT
choice); the `model` input overrides it per run.

### 12.4 Work items

Delivered: the `engine` input with mutually exclusive agent
steps, the engine-aware key guard and model resolution, Gemini
`settings` assembly (`tools.core` allow-list,
`model.maxSessionTurns`, telemetry into the artefact directory),
session-summary capture,
and the `engine` choice on the manual dry-run dispatch and the
scheduled caller's dispatch. Scheduled runs stay on the Claude
engine until the org validates Gemini output quality.

Remaining:

1. Transcript fidelity: confirm the Gemini telemetry log carries
   turn-by-turn content matching the §7.2 evidence guarantee, and
   map its cost/turn fields in `load_transcript_stats`
   (defensive parsing means unmapped fields drop out rather than
   break the report)
2. Egress: audit-mode Gemini run to harvest endpoints
   (`generativelanguage.googleapis.com:443` expected) for the org
   allow-list, as §7.1 did for Anthropic
3. README consumption example for the Gemini engine

### 12.5 Open questions (Gemini track)

1. **Transcript fidelity** — does the Gemini CLI emit a
   turn-by-turn structured log matching what Claude Code's
   `execution_file` provides? The §7.2 evidence guarantee must
   hold for both engines.
2. **Billing ownership** — which GCP project/billing account
   carries the spend, and what is the Gemini analogue of the
   per-workspace spend cap?

## 13. Third Engine: GitHub Copilot

**Status:** Implemented and opt-in. Not fit for live or scheduled
runs until the command-level enforcement in §13.4 lands — see the
containment note in §13.2 for what that gap costs.

The organisation already pays for Copilot. A Copilot engine turns
triage spend into an existing entitlement rather than a third
vendor relationship, and removes a long-lived model API key from
the pipeline. Section 3.1 records why the original design ruled
Copilot out, and what changed since.

The engine slots into the existing abstraction (§12.1): a third
mutually exclusive agent-session step behind the same `engine`
input, sharing the snapshots, exclusion filtering, policy prompt,
label wrapper, diff report, and artefact bundle.

### 13.1 Harness options

<!-- markdownlint-disable MD013 -->

| Option | Verdict |
| ------ | ------- |
| Copilot CLI programmatic mode (`copilot -p`) | ✅ **Selected** |
| [`github/gh-aw`](https://github.com/github/gh-aw) (GitHub Agentic Workflows) | ❌ Replaces this pipeline rather than plugging into it |
| A first-party Copilot CLI action | ❌ None exists |
| Copilot coding agent | ❌ Repo-scoped, PR-oriented, wrong shape |

<!-- markdownlint-enable MD013 -->

GitHub's own documentation recommends Agentic Workflows for most
automation, so the rejection needs justifying. `gh-aw` is a
compiler: it takes agentic workflows written as Markdown and
emits `.lock.yml` workflow files, carrying its own safe-output,
sandboxing, and permission models. Adopting it here would mean
re-authoring the pipeline in its idiom and displacing the parts
that stay engine-neutral and already carry review — the evidence
pipeline (§7.2), the label wrapper, the engine abstraction. That
makes it the right choice for a greenfield agentic workflow, not
for a third engine behind an established interface. Worth
revisiting if this pipeline is ever rebuilt from scratch.

No first-party action exists. Public workflows referencing
`github/copilot-cli-action` point at a repository that does not
exist. So the engine installs the CLI from npm at a pinned
version and invokes it directly, which is the pattern GitHub's
Actions documentation shows for direct use.

### 13.2 Mapping across the three engines

<!-- markdownlint-disable MD013 -->

| Concern | Claude engine | Gemini engine | Copilot engine |
| ------- | ------------- | ------------- | -------------- |
| Harness | `anthropics/claude-code-action` | `google-github-actions/run-gemini-cli` | `copilot -p`, pinned npm install |
| Prompt | `prompt` input | `prompt` input | `--prompt`, read from `artefacts/prompt.md` |
| Model | `--model` via `claude_args` | `gemini_model` input | `--model` |
| Tool containment | `--allowedTools` grant list | `settings` JSON: `tools.core` | `--available-tools` and `--deny-tool` (enforced), `--allow-tool` (approval) |
| Turn ceiling | `--max-turns` | `model.maxSessionTurns` | none — the step timeout bounds it |
| Session evidence | `execution_file` output | telemetry log plus summary output | `--log-dir` plus `--share` transcript |

<!-- markdownlint-enable MD013 -->

Containment notes specific to this engine:

- **Approval versus enforcement.** `--allow-tool` is not the
  allow-list `--allowedTools` is on the Claude side. The CLI
  auto-approves shell commands it classifies as reads, so naming
  four `gh` commands pre-approves those without denying others.
  The engine layers three mechanisms in response:
  `--available-tools` restricts the model to the shell tools and
  nothing else, which the CLI enforces outright ("the model won't
  be able to use it at all"); `--deny-tool` blocks the mutating
  `gh` and `git` verbs, and outranks every allow rule and any
  approval the CLI would otherwise infer; `--allow-tool`
  pre-approves the four commands the policy needs.

  What survives that stack is **other commands the CLI treats as
  reads**, which it auto-approves and no flag withdraws. That
  matters more than "wider reads": the agent's shell carries both
  credentials in its environment, and `--secret-env-vars` redacts
  by *value*, so a command that transforms a token (encoding it,
  say) defeats the redaction and can land it in the step log and
  the 90-day evidence bundle. The App token's one-hour lifetime
  bounds the damage, not the exposure.

  Writes remain shut regardless — the wrapper, the deny rules,
  and the token scope each enforce that independently — but this
  is a materially weaker boundary than the other two engines
  offer, and prompt-injected issue text is the threat it fails
  against. Closing it needs a `preToolUse` hook vetting each
  command against the policy's four (§13.4). Until that lands,
  treat the engine as opt-in and unsuited to live or scheduled
  runs.
- **Shell pattern matching.** The CLI reference describes the
  `:*` suffix as "the command stem followed by a space", while
  GitHub's own `gh-aw` compiler documents the bare form as a
  prefix match (`shell(jq)` matching `jq '.filter' …`). The two
  readings disagree about whether `shell(gh issue list)` covers
  `gh issue list --owner …`. The engine sidesteps the question by
  emitting both forms of every pattern: one matches under either
  reading, and neither widens the grant, since both anchor on the
  same command. The deny list carries both forms for the same
  reason — a deny rule that fails to match protects nothing.
- **Deny rules.** They outrank every allow rule and any approval
  the CLI would otherwise infer, so the engine restates the
  boundary explicitly: no `write`, no `git`, no `gh issue edit`.
  Every label still travels through the wrapper.
- **Token redaction.** The CLI redacts `GITHUB_TOKEN` and
  `COPILOT_GITHUB_TOKEN` from its output by default, but not
  `GH_TOKEN` — which here holds the App token carrying
  `issues: write`. Since this engine writes CLI logs and a
  session transcript into an artefact kept for 90 days,
  `--secret-env-vars` registers `GH_TOKEN` explicitly. The other
  two engines emit no comparable transcript of the shell
  environment.
- **Built-in MCP servers, disabled.** Copilot CLI ships a GitHub
  MCP server enabled by default, whose `label_write` tool would
  apply labels without passing through `apply-label.sh` — around
  the wrapper's scope, exclusion, and pull-request checks.
  `--disable-builtin-mcps` closes that route. Neither of the other
  two engines ships such a server, so this hardening has no
  analogue there.
- **Custom instructions, disabled.** The CLI merges `AGENTS.md`,
  `CLAUDE.md`, `GEMINI.md`, and `.github/copilot-instructions.md`
  from the working tree into its system prompt. The working tree
  holds the assets checkout, which on test runs comes from a pull
  request head. `--no-custom-instructions` leaves the policy
  prompt as the single instruction source.
- **No turn ceiling.** `max_turns` has no Copilot analogue;
  `--max-ai-credits` caps credits per response, a different unit,
  and makes no substitute. The agent step's 20-minute timeout
  bounds the session, inside the job's 30-minute ceiling.

### 13.3 Authentication and billing

Copilot CLI reads its credential from `COPILOT_GITHUB_TOKEN`,
then `GH_TOKEN`, then `GITHUB_TOKEN`. The engine sets the first
(model access) and the second (the App token, repository access)
so the two never mix: an App installation token cannot
authenticate Copilot requests, and the Copilot credential must
never carry `issues: write`. The separation guarantees the
absence of repository **write** on the model credential, not the
absence of repository access: a caller `GITHUB_TOKEN` also
carries whatever else its job grants, while a PAT scoped to
Copilot Requests reaches nothing but the model.

Two credentials work for the `copilot_token` secret:

1. **The calling job's `GITHUB_TOKEN`**, where that job grants
   `copilot-requests: write`. GitHub's recommended path: no
   stored secret, a short-lived token per run, and usage metered
   to the organisation. It depends on the organisation policy
   "Allow use of Copilot CLI billed to the organization".
2. **A fine-grained PAT** carrying the "Copilot Requests"
   permission. Works regardless of that policy, at the cost of a
   long-lived credential attributing spend to one person's seat.

The reusable workflow takes a **secret** rather than declaring
the permission itself, because a reusable-workflow chain can hold
or reduce permissions, never raise them. Declaring
`copilot-requests: write` on the triage job would fail every
existing caller, including callers that never touch this engine.
Passing the caller's token in as a secret leaves the permission
decision — and its failure mode — with the consumer that wants
the engine.

This repository's own callers use the PAT for now: neither the
pinned actionlint (1.7.12.24) nor the SchemaStore workflow schema
(check-jsonschema 0.38.0) recognises the `copilot-requests`
scope, so declaring it locally would fail the linting gate.
Revisit once both learn it.

One caveat on route 1. The secret's value comes from an
expression the **caller** evaluates, so it carries the caller
job's grant rather than the reduced grant this workflow's job
declares; the downgrade rule governs the token a called workflow
receives, not a string handed to it as a named secret. GitHub's
reusable-workflow documentation shows this exact pattern — a
caller job declaring `pull-requests: write` and passing
`${{ secrets.GITHUB_TOKEN }}` on to the called workflow — which
would serve no purpose if the hand-off dropped the grant. The
reasoning holds, but no run has confirmed it for this scope yet
(§13.5). It fails closed if wrong: the CLI rejects the token and
the step fails with the evidence bundle intact.

The default model is **`claude-sonnet-4.6`**, the CLI's own
default, pinned explicitly so an upstream change is not a silent
change here. The `model` input overrides it per run.

### 13.4 Work items

Delivered: the `copilot` engine value, its credential guard and
model default, the tool-surface restriction plus the
allow/deny translation of the shared tool grants, the pinned CLI
install, session evidence into the artefact bundle, and the
`copilot` choice on the manual dry-run dispatch and the scheduled
caller's dispatch. Scheduled runs stay on the Claude engine, and
the scheduled caller marks this engine for dry-run evaluation
alone until the enforcement below lands.

Remaining:

1. **Precondition for live use** — command-level enforcement: a
   `preToolUse` hook vetting each proposed shell command against
   the policy's four, closing the auto-approved-reads gap in
   §13.2. Needs the hook payload contract confirmed against a
   real session before it goes anywhere near a security boundary.
   An alternative worth weighing: route `gh` through a
   token-holding wrapper so no credential sits in the agent's
   shell environment at all
2. Cost telemetry: `load_transcript_stats` parses a Claude-shaped
   execution log; map the Copilot CLI's log fields so the report
   carries turns and spend for this engine too (defensive parsing
   means unmapped fields drop out rather than break the report)
3. Confirm the organisation billing policy, then move this
   repository's callers from the PAT to the caller `GITHUB_TOKEN`
   once the linting toolchain recognises `copilot-requests`
4. Egress: an audit-mode Copilot run to harvest endpoints
   (`api.githubcopilot.com:443` and `registry.npmjs.org:443`
   expected, plus the Node distribution host used by
   `setup-node`) for the org allow-list, as §7.1 did for
   Anthropic

### 13.5 Open questions (Copilot track)

1. **Shell pattern matching** — confirm in a real run which of
   the two documented readings the CLI implements for multi-word
   `gh` subcommands. The engine emits both forms, so a session
   should work either way; the run settles which form to keep.
2. **The caller `GITHUB_TOKEN` route** — confirm that a token
   passed in as a named secret reaches the CLI carrying the
   caller job's `copilot-requests: write` grant (§13.3). Blocked
   behind the linting gate, so the PAT route carries the engine
   until then.
3. **Model choice** — `claude-sonnet-4.6` against
   `claude-haiku-4.5` for what is a classification workload;
   measure quality against cost on a real backlog.
4. **Folder trust** — the CLI asks a session to confirm it
   trusts its working directory, and `--no-ask-user` disables the
   agent's `ask_user` tool rather than that startup prompt. A
   runner's config directory starts empty every run, so the
   question applies to GitHub's own documented Actions example
   too, which is the evidence that `-p` mode skips the prompt.
   Confirm on the first real run; the failure mode is a stalled
   session that the step timeout ends.
5. **Premium-request accounting** — whether per-request billing
   makes retriage runs materially more expensive here than on the
   metered API engines, and where the analogue of a per-workspace
   spend cap lives (cost centres, per §13.3).
