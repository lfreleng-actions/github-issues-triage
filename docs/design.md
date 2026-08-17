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

Two candidate routes to Claude models exist; one is viable.

### 3.1 GitHub Copilot subscription — not usable ❌

The org-level Copilot subscription exposes Anthropic models **within
Copilot surfaces** (IDE chat, Copilot CLI, the Copilot coding
agent) and nowhere else. There is no general-purpose API
entitlement: Copilot terms do
not permit driving arbitrary automation with the underlying models,
and no supported harness exists for "run Claude Code against the
Copilot backend". GitHub Models (the `models` permission on
`GITHUB_TOKEN`) offers API access to a catalogue of models, but
Anthropic models are not part of that catalogue and its rate limits
target experimentation, not production batch jobs.

**Conclusion:** do not build on the Copilot entitlement.

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
| Copilot coding agent | ❌ Repo-scoped, PR-oriented, wrong entitlement (§3.1) |
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

`.github/workflows/issues-triage.yaml`:

```text
Triggers:
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

The org allow-list
(`.github/harden-runner/lfreleng-actions/allow_list.txt`) contains
**no Anthropic endpoints** today. Required additions:

```text
api.anthropic.com:443
```

plus whatever telemetry/statsig endpoints the pinned Claude Code
release requires (discover these by first running the workflow with
`egress-policy: audit` and harvesting the report — the same procedure
used for other org tooling). Whether these belong in the shared
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
- The workflow tees Claude Code's human-readable stdout to
  `artefacts/session-console.log` as the quick-scan companion.
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
the transcript:

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
| `session-console.log` | Human-readable session output |
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

## 8. Repository Layout

This repository started from `workflows-template`, which
targets *reusable-workflow* repositories. This pipeline is a **caller**
workflow, not a reusable one, so much of the skeleton does not apply.

### Keep (from `workflows-template`)

- `.pre-commit-config.yaml`, `.gitlint`, `.yamllint`, `.editorconfig`,
  `.ruff.toml`, `.gitignore` — org-standard linting
- `.github/workflows/openssf-scorecard.yaml`, `release-drafter.yaml`,
  `testing.yaml`, `clear-action-cache.yaml`
- `.github/actionlint.yaml`, `.github/dependabot.yml`
- `LICENSES/`, `SECURITY.md`

### Remove (template skeletons that do not apply)

- `.github/workflows/build-test.yaml`
- `.github/workflows/build-test-release.yaml`
- `.github/workflows/merge.yaml`
- `.github/workflows/release.yaml`
- `examples/` (build-test-release / merge examples)
- `.readthedocs.yml` (unless we later publish these docs)

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
docs/design.md                          # this document
prompt/triage.md                        # the agent's triage policy prompt
.github/workflows/issues-triage.yaml    # the scheduled pipeline
README.md                               # rewritten for this repo
```

## 9. Failure Modes and Mitigations

<!-- markdownlint-disable MD013 -->

| Failure | Mitigation |
| ------- | ---------- |
| Anthropic API outage / 529s | Job fails visibly; next weekday run catches up. No retry storm: one run per day. |
| Agent mislabels an issue | Labels are reversible; step summary makes every action reviewable; humans can relabel (rule 3 stops the agent undoing them). |
| Agent goes off-script | `--allowedTools` denies every verb except issue read/label; App token holds `issues: write` and nothing more; `--max-turns` and `timeout-minutes` bound the session. |
| Prompt-injection via issue body | Real risk: issue text arrives as untrusted input. Containment as above — worst case within the sandbox is a wrong label on some issue, not code execution or data exfiltration (egress is allow-listed). Prompt instructs the agent to treat issue bodies as data, never as instructions. |
| Rate limits (GitHub) | ~120 open issues org-wide at worst; App tokens get 15k req/hr. Not a concern at this scale. |
| Cost runaway | Pre-scan short-circuit (§4.3), max-turns, weekday schedule, dedicated Anthropic workspace with a spend cap. |
| Secret leakage in logs | Actions masks registered secrets; Claude Code does not echo its key; harden-runner blocks unexpected egress. Transcript artefacts contain public issue text and nothing else (§7.2). |
| Session dies mid-run, no evidence | Artefact upload runs `if: always()`; snapshots and partial transcript survive timeouts and failures. |
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
6. **Transcript output contract** — confirm the pinned
   `claude-code-action` release's output name/path for the structured
   execution log (`execution_file` in earlier releases); the artefact
   step in §7.2 depends on it.

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
