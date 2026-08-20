<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Google (Gemini) engine setup

Runs the Gemini CLI through Google's official
`google-github-actions/run-gemini-cli` action, authenticating with
an AI Studio API key.

<!-- markdownlint-disable MD013 -->

| Item | Value |
| ---- | ----- |
| `engine` input | `gemini` |
| Secret | `gemini_api_key` |
| Default model | `gemini-3.5-flash-lite` |
| Harness | `google-github-actions/run-gemini-cli` |
| Turn ceiling | `max_turns` input, default `80` |

<!-- markdownlint-enable MD013 -->

## 1. Create an API key

1. Sign in at <https://aistudio.google.com/apikey>.
2. Create an API key against the Google Cloud project that should
   carry the spend. AI Studio will create a project for you if
   none exists; prefer an existing project with billing and
   quotas already established.
3. Copy the value.

Note which project you chose. Gemini API usage bills to that
project, and its quotas — not the pipeline — decide what happens
when a scheduled run meets a busy day.

## 2. Store the key

Add the value as a repository secret named `GEMINI_API_KEY` on
the repository that calls the pipeline (**Settings** > **Secrets
and variables** > **Actions**). An organisation secret works too.

The callers in this repository already pass that name through:

```yaml
secrets:
  gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
```

The key carries no GitHub permissions. It authorises model
requests alone; labels travel over the App token described in
[README.md](README.md).

## 3. Choose a model

`gemini-3.5-flash-lite` is the default, chosen for a
classification workload. Override it per run with the `model`
input:

```yaml
with:
  engine: 'gemini'
  model: 'gemini-3.5-flash'
```

## 4. Verify

```bash
gh workflow run testing.yaml -f engine=gemini
```

## Egress

A session reaches `generativelanguage.googleapis.com:443`. The
pinned `run-gemini-cli` action installs the CLI first, with
`npm install --global @google/gemini-cli@<version>`, which
reaches `registry.npmjs.org:443`.

Treat those two as a starting point rather than a finished
allow-list. The dependable way to build one is a run with
`egress_policy: audit`, which records every outbound call without
blocking; harvest what it recorded, then switch to `block`. A
hand-written list that misses an install-time endpoint fails the
run before the session starts.

## Known gaps

Session evidence for this engine is telemetry plus the CLI's
summary output rather than the turn-by-turn transcript the Claude
engine produces. Whether that telemetry meets the pipeline's
evidence guarantee is still open, and the report's cost and turn
figures may come out blank for Gemini runs — the report parses
defensively, so unmapped fields drop out rather than break it.
See section 12 of
[`../development/DESIGN.md`](../development/DESIGN.md).

## Failure modes

<!-- markdownlint-disable MD013 -->

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| `Agent runs with engine 'gemini' need the gemini_api_key secret` | No secret reached the workflow | Add the secret, or pass it through from the caller |
| Session fails authenticating | Key revoked, mistyped, or restricted to other APIs | Reissue the key with the Generative Language API enabled |
| Session fails on quota | Project quota exhausted | Raise the quota, or lower `max_turns` |

<!-- markdownlint-enable MD013 -->

The credential guard checks that a key arrived, not that it
works, so an invalid key fails inside the session rather than at
the guard.

## Alternative: Vertex AI

The harness also supports Vertex AI through Workload Identity
Federation, which removes the long-lived key in favour of keyless
OIDC. The pipeline does not wire that path today; adding it would
not change the `engine` interface.
