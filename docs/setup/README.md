<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Setup

The pipeline runs one agent session per run, driven by one of three
interchangeable engines. Everything either side of that session —
snapshots, exclusion filtering, the policy prompt, the label
wrapper, the diff report, the artefact bundle — is engine-neutral,
so setting up an engine means providing one credential and
choosing the `engine` input.

Pick the engine you intend to run and follow its guide:

<!-- markdownlint-disable MD013 -->

| Engine | `engine` input | Credential | Guide |
| ------ | -------------- | ---------- | ----- |
| Anthropic Claude | `claude` (default) | `anthropic_api_key` | [ANTHROPIC.md](ANTHROPIC.md) |
| Google Gemini | `gemini` | `gemini_api_key` | [GOOGLE.md](GOOGLE.md) |
| GitHub Copilot | `copilot` | `copilot_token` | [GITHUB.md](GITHUB.md) |

<!-- markdownlint-enable MD013 -->

Each guide covers the credential, the permissions it needs, where
to store it, the egress endpoints the engine reaches, and how to
verify the result.

## Shared prerequisites

### Labelling credentials (all engines)

Applying a label always travels over a GitHub App installation
token, so live runs need App credentials whichever engine drives
the session:

<!-- markdownlint-disable MD013 -->

| Setting | Value |
| ------- | ----- |
| App permissions | `issues: write`, `metadata: read` — nothing else |
| Installation | Every repository in the target organisation |
| Client id | Passed as the `github_app_client_id` input |
| Private key | Passed as the `github_app_private_key` secret |

<!-- markdownlint-enable MD013 -->

The workflow down-scopes the minted token further at mint time,
and the token expires after an hour. In this organisation the
existing bot App supplies both halves through
`vars.LF_RELENG_BOT_CLIENT_ID` and
`secrets.LF_RELENG_BOT_PRIVATE_KEY`.

Without App credentials the pipeline still reports: dry runs read
issues through the caller's `github.token`. Live runs
(`dry_run: false`) refuse to start, because the job holds
`issues: read` by design and could never apply a label.

### Where credentials live

Model credentials are repository secrets on the **calling**
repository, matching the name each guide gives. Organisation
secrets work too when more than one repository calls the
pipeline; the workflow sees nothing but the value handed to its
named secret input.

The Anthropic and Gemini keys carry no GitHub permissions at all,
and a Copilot PAT scoped to Copilot Requests reaches nothing but
the model. One route differs: the Copilot engine can reuse the
calling job's `GITHUB_TOKEN`, which carries whatever permissions
that job grants. Grant such a job reads alone — see
[GITHUB.md](GITHUB.md).

## Verifying a new engine

Run the manual dry-run before trusting an engine with anything:

```bash
gh workflow run testing.yaml -f engine=<claude|gemini|copilot>
```

That dispatch runs in dry-run mode with `retriage: true`, so the
session always has issues to classify and always produces
proposals to inspect. It reports the labels it would apply and
applies none.

Check the run's artefact bundle afterwards. It carries the
before and after snapshots, the assembled prompt, the engine's
session evidence, and the diff report — including when the
session fails, which is when it matters most.

A missing or empty credential fails the run in seconds, at the
`Resolve engine and model` guard, before any spend. That guard
checks that a credential arrived, not that it works: an invalid
credential fails later, inside the session.

## Further reading

- [Design](../development/DESIGN.md) — the architecture, the
  containment model, and the reasoning behind each engine's
  integration
- [Repository README](https://github.com/lfreleng-actions/github-issues-triage#consuming-the-reusable-workflow)
  — calling the reusable workflow from another repository
