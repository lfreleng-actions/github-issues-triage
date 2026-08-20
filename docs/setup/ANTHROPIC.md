<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Anthropic (Claude) engine setup

The default engine. Runs Claude Code through Anthropic's official
`anthropics/claude-code-action`, authenticating with an Anthropic
API key.

<!-- markdownlint-disable MD013 -->

| Item | Value |
| ---- | ----- |
| `engine` input | `claude` (the default) |
| Secret | `anthropic_api_key` |
| Default model | `claude-opus-5` |
| Harness | `anthropics/claude-code-action` |
| Turn ceiling | `max_turns` input, default `80` |

<!-- markdownlint-enable MD013 -->

## 1. Create an API key

Anthropic API keys come from the Anthropic Console, not from a
Claude subscription: a Claude Pro or Max plan grants no API
access.

1. Sign in at <https://console.anthropic.com/>.
2. Create a **workspace** for this pipeline rather than issuing
   the key against the default workspace. A workspace isolates
   the spend, carries its own limits, and lets you revoke the
   pipeline's access without touching anything else.
3. Set a **spend limit** on that workspace. The pipeline runs
   unattended on a schedule, so a cap is the backstop against a
   runaway session or an unnoticed retriage loop.
4. Create an API key scoped to the workspace and copy the value.

## 2. Store the key

Add the value as a repository secret named `ANTHROPIC_API_KEY` on
the repository that calls the pipeline (**Settings** > **Secrets
and variables** > **Actions**). An organisation secret works too
when more than one repository calls it.

The callers in this repository already pass that name through:

```yaml
secrets:
  anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
```

The key carries no GitHub permissions of any kind. It authorises
model requests and nothing else; labels travel over the App token
described in [README.md](README.md).

## 3. Choose a model

`claude-opus-5` is the default. Override it per run with the
`model` input when trading quality against cost:

```yaml
with:
  engine: 'claude'
  model: 'claude-sonnet-5'
```

## 4. Verify

```bash
gh workflow run testing.yaml -f engine=claude
```

## Egress

A session reaches `api.anthropic.com:443`. The harness reaches
more than that before the session starts: the pinned
`claude-code-action` installs Bun, runs `bun install` for its own
dependencies, and installs the Claude Code CLI. Each of those
steps fetches from a distribution host of its own.

Treat the model endpoint as a starting point rather than a
finished allow-list. The dependable way to build one is a run
with `egress_policy: audit`, which records every outbound call
without blocking; harvest what it recorded, then switch to
`block`. A hand-written list that misses an install-time endpoint
fails the run before the session reaches Anthropic at all.

## Failure modes

<!-- markdownlint-disable MD013 -->

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| `Agent runs with engine 'claude' need the anthropic_api_key secret` | No secret reached the workflow | Add the secret, or pass it through from the caller |
| Session ends `is_error: true` with `401 API key is invalid` | Revoked or mistyped key, or a deleted workspace | Reissue the key and update the secret |
| Session ends on a credit or rate-limit error | Workspace spend limit reached, or org-wide rate limits | Raise the limit, or lower `max_turns` |

<!-- markdownlint-enable MD013 -->

The credential guard checks that a key arrived, not that it
works. An invalid key passes the guard and fails inside the
session instead, after the harness has retried it — around three
minutes, with no spend, and with the failure recorded in
`session-transcript.json` in the run's artefact bundle.
