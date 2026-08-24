<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# GitHub (Copilot) engine setup

Runs the GitHub Copilot CLI, installed from npm at a pinned
version and invoked directly — no first-party action exists.
Authenticates with a GitHub token rather than a model API key, so
this engine adds no third vendor relationship.

> **⚠️ Not for live or scheduled runs.**
> This engine holds a weaker containment boundary than the other
> two. Its tool allow-list is an approval policy rather than a
> filter, and the CLI keeps auto-approving shell commands it
> treats as reads. The reusable workflow **refuses a live run on
> this engine**: pair it with `dry_run: true` or the run fails
> fast. That stands until the command-level enforcement in
> section 13.4 of
> [`../development/DESIGN.md`](../development/DESIGN.md) lands.

<!-- markdownlint-disable MD013 -->

| Item | Value |
| ---- | ----- |
| `engine` input | `copilot` |
| Secret | `copilot_token` |
| Default model | `claude-sonnet-5` |
| Harness | `@github/copilot` CLI, pinned, installed with npm |
| Turn ceiling | None — `max_turns` has no analogue; the 20-minute step timeout bounds the session |
| Label writes | Refused; the engine never applies labels |

<!-- markdownlint-enable MD013 -->

## Choosing an authentication route

The Copilot CLI takes its credential from `COPILOT_GITHUB_TOKEN`,
which the workflow fills from the `copilot_token` secret. Two
kinds of credential work, and they differ in who pays and what
else the credential can reach.

<!-- markdownlint-disable MD013 -->

| | Route A: caller `GITHUB_TOKEN` | Route B: fine-grained PAT |
| --- | --- | --- |
| Stored secret | None | Long-lived PAT |
| Lifetime | One run | Until expiry or revocation |
| Billed to | The organisation, metered directly | The PAT owner's Copilot seat |
| Needs a Copilot seat | No | Yes, held by the PAT owner |
| Needs an org policy | Yes | No |
| Other repository access | Whatever else the calling job grants | None |
| GitHub's recommendation | Preferred for automation | Fallback |

<!-- markdownlint-enable MD013 -->

Route A is the better credential and the one GitHub recommends.
Route B exists for organisations that cannot enable the policy,
and for evaluating the engine before committing to it.

Neither route *needs* repository write, and route B cannot gain
it: a PAT scoped to Copilot Requests alone reaches nothing but
the model. Route A is not self-limiting in the same way. It hands
the calling job's `GITHUB_TOKEN` to the CLI, so that credential
carries every permission the job grants — grant that job nothing
but reads plus `copilot-requests: write`, as the example below
does, or the model credential becomes write-capable.

Labels travel over the GitHub App token described in
[README.md](README.md) either way, and the two credentials never
mix: an App installation token cannot authenticate Copilot
requests in any case.

## Route A: the caller's `GITHUB_TOKEN`

### 1. Confirm the organisation policy

Copilot CLI usage billed to an organisation depends on one
policy. GitHub enables that policy by default for organisations
that allow Copilot CLI.

1. Open the organisation's Copilot policy settings.
2. Under **Copilot CLI**, look for **Allow use of Copilot CLI
   billed to the organization** and turn it on.

This policy is separate from licensing. An organisation needs no
Copilot seats for this route: usage meters to the organisation
directly rather than drawing on anyone's entitlement. Enterprises
that license through one organisation and work in others need the
policy in the working organisation alone.

### 2. Grant the permission and pass the token

The permission goes on the **calling** job, and the token passes
in as the secret:

```yaml
jobs:
  triage:
    permissions:
      issues: read
      contents: read
      # Authorises Copilot model requests; carries no repository
      # access of its own.
      copilot-requests: write
    uses: lfreleng-actions/github-issues-triage/.github/workflows/issues-triage.yaml@<commit-sha>
    with:
      org: 'your-org'
      engine: 'copilot'
      dry_run: true
    secrets:
      copilot_token: ${{ secrets.GITHUB_TOKEN }}
```

By design, the reusable workflow does not declare
`copilot-requests` itself. A called workflow can narrow the
caller's permissions but never widen them, so declaring it there
would fail every caller running a different engine. Passing the
token in as a named secret leaves the decision with the consumer
that wants this engine.

> **⚠️ Route A remains unproven.**
> The reusable workflow's own job declares `issues: read` and
> `contents: read`, which narrows the `GITHUB_TOKEN` that job
> receives. The reasoning that route A survives anyway is that
> the secret carries a string the **caller** evaluated, and the
> downgrade rule governs the token a called workflow receives
> rather than a value handed to it as a named secret — the
> pattern GitHub's own reusable-workflow documentation shows.
> Sound, but no run has confirmed it for this scope. Route B
> avoids the question entirely. See section 13.5 of
> [`../development/DESIGN.md`](../development/DESIGN.md).

### 3. Cost control

Organisation-metered usage bypasses per-user Copilot budgets,
because the spend attaches to no individual. Attach the
organisation to a
**cost centre** and budget against that instead, and watch the
organisation's billing dashboard while the engine is new.

## Route B: a fine-grained PAT

### 1. Create the token

1. Go to
   <https://github.com/settings/personal-access-tokens/new>.
2. Grant the **Copilot Requests** permission.
3. Grant **no repository permissions**. This engine needs the
   token for model access alone, and withholding repository
   access is the point of the split.
4. Set the shortest expiry you can live with, and copy the value.

The token has to be fine-grained. The CLI inspects the value and
refuses a classic PAT outright, whatever scopes it carries:
`The COPILOT_GITHUB_TOKEN environment variable contains a classic
PAT.`

The token's owner must hold an active Copilot seat or
subscription: requests draw on that person's entitlement, and
their licence decides which models the session can reach.

### 2. Store it

Add the value as a repository secret named `COPILOT_CLI_TOKEN`
(**Settings** > **Secrets and variables** > **Actions**), then
pass it through without granting `copilot-requests`:

```yaml
secrets:
  copilot_token: ${{ secrets.COPILOT_CLI_TOKEN }}
```

The callers in this repository use this route today. Route A
remains the better choice, but neither the pinned actionlint nor
the workflow schema used by `check-jsonschema` recognises the
`copilot-requests` scope yet, so declaring it locally fails the
repository's own linting gate. Revisit once both learn it.

## Choose a model

The default is **`claude-sonnet-5`**. Its predecessor,
`claude-sonnet-4.6`, retires from Copilot Business and Enterprise
on 2026-09-01. Override the default per run with the `model`
input:

```yaml
with:
  engine: 'copilot'
  model: 'claude-haiku-4.5'
```

## Verify

```bash
gh workflow run testing.yaml -f engine=copilot
```

## Egress

A session reaches the Copilot backend at
`api.githubcopilot.com:443`. Installing the CLI beforehand reaches
`registry.npmjs.org:443` and the Node distribution host that
`actions/setup-node` uses.

Treat that list as a starting point rather than a finished
allow-list. The dependable way to build one is a run with
`egress_policy: audit`, which records every outbound call without
blocking; harvest the recorded endpoints, then switch to
`block`. A hand-written list that misses an install-time endpoint
fails the run before the session starts.

## Failure modes

<!-- markdownlint-disable MD013 -->

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| `Agent runs with engine 'copilot' need the copilot_token secret` | No secret reached the workflow | Add the secret, or pass the caller's token through |
| `The copilot engine cannot apply labels` | The run set `dry_run: false` | Set `dry_run: true`, or choose `claude` or `gemini` |
| CLI rejects the token on route A | The organisation policy is off, or the grant did not survive the hand-off | Enable the policy; otherwise fall back to route B |
| CLI rejects the token on route B | The PAT lacks Copilot Requests, has expired, or its owner holds no Copilot seat | Reissue the PAT, or assign its owner a seat |
| `COPILOT_GITHUB_TOKEN … contains a classic PAT` | A classic PAT reached the CLI, which refuses them regardless of scope | Issue a fine-grained PAT instead |
| Session stalls until the step timeout | The CLI waited on an interactive prompt | Capture the run's artefact bundle and report it |
| Every agent command reports `Permission denied and could not request permission from user` | The tool grants no longer match what the CLI expects | Compare against section 13.2 of [`../development/DESIGN.md`](../development/DESIGN.md); the CLI approves `gh` on its first-level subcommand |
| Agent `gh` commands report missing authentication | The seeded `gh` configuration directory did not reach the session | Check the `GH_CONFIG_DIR` set-up in the agent step |

<!-- markdownlint-enable MD013 -->

No run has yet confirmed route A end to end. The reasoning that a
token handed over as a named secret keeps the caller's grant
holds, and matches GitHub's own reusable-workflow examples, but no
run has proved it for this scope. It fails closed — the CLI
rejects the token, the step fails, and the artefact bundle
survives.

## Session evidence

The engine writes CLI logs and a shared session transcript into
the artefact bundle. Cost and turn figures in the report may come
out blank, because the report parses a Claude-shaped execution
log; the parser drops unmapped fields rather than failing. See
section 13 of
[`../development/DESIGN.md`](../development/DESIGN.md).
