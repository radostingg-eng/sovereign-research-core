# Sovereign Research

A research system where an LLM host owns the judgement and a deterministic
runtime owns the evidence.

The host has connector access: it reads a portfolio, searches, and decides
what is worth researching. It cannot execute anything. The runtime has the
repository and can execute, but has no connectors and makes no investment
decisions. Neither closes the loop alone, so the host commits what it
observed and concluded, and the runtime validates it, persists a receipt
into an append-only journal, and writes back what the next cycle needs to
know.

What the runtime checks is identity, evidence, lineage, accounting, time
and safety. What it never does is rank an instrument, pick a strategy, or
grade a forecast on the host's behalf. When the two disagree about whether
something happened, the journal wins.

## Starting from nothing

Tell your ChatGPT host, once:

> Set up Sovereign Research. Read
> `radostingg-eng/sovereign-research-core/START_HERE.md` and follow it.

It will check which connectors it can actually call, ask you to enable
GitHub or Interactive Brokers if either is missing, create your private
profile repository, pin the core commit it read, capture any preferences you
already volunteered without interviewing you, and create one enabled hourly
ChatGPT task using Part B of the pinned standing prompt. Missing preferences
are valid state. Setup asks nothing unless a required connector, safe profile
creation, or scheduling is genuinely blocked. It verifies the task and its
next run, but deliberately does not execute the first cycle.

Hourly hosts may automatically share fixed-schema compatibility signals with
the core issue tracker. Signals contain only a known error code, pinned core
SHA, kind, occurrence bucket, and a boolean test-needed flag. Rich feedback
and reproductions remain private. See `HOST_FEEDBACK.md`.

See `START_HERE.md`.

## Your data does not live here

This repository is code. One operator's portfolio, journal, preferences,
theses and goals live in a **profile directory** that you create and that
nobody else sees.

```bash
git clone https://github.com/radostingg-eng/sovereign-research-core.git
cd sovereign-research-core

python3 -m runtime.init_profile ~/sovereign-data
export SOVEREIGN_PROFILE_DIR=~/sovereign-data
```

`init_profile` writes an empty skeleton: directories, a genesis journal
record, a preferences template, and the profile-owned validation/execution
workflow. It does not invent a portfolio, and it refuses to touch a directory
that already holds a journal.

For GitHub-only setup, `START_HERE.md` tells the host to copy the valid
pre-hashed genesis and workflow templates byte-for-byte. The hourly ChatGPT
task owns connector calls and judgement; the private profile workflow validates
and executes what the host stages. Shared-core Actions never receive profile
write access.

Private profiles install only `Sovereign Profile Host Cycle`, not the core
repository's full code-test workflow. Expected candidate refusals publish
correction feedback and complete successfully. Unexpected integrity,
execution, checkout, or publication failures remain failed. GitHub Actions
email delivery is controlled by each operator's account notification settings
and can be disabled without disabling either workflow execution or the Actions
history.

Schema-v4 connector results are preserved as canonical, content-addressed
response artifacts under the private profile's `tool_artifacts/` directory.
Hash-chained provenance records bind each artifact to its exact action,
normalized request, observation time, and separate host interpretation.
Feedback exposes only bounded opaque references and hashes, never response
bodies, request arguments, private paths, or credential-bearing URLs.

Everything the runtime writes resolves under `SOVEREIGN_PROFILE_DIR`.
Paths that would escape it raise rather than falling back, so a second
operator on the same machine cannot reach the first one's state.

If you never set the variable, the profile defaults to the checkout and
behaves exactly as it did before the split.

## Running the tests

```bash
python3 -m pytest runtime/          # synthetic only, no profile needed
SOVEREIGN_PROFILE_DIR=~/sovereign-data python3 -m pytest runtime/
```

The first form is what a new clone runs. Tests that read live operator
state skip with a reason rather than inventing a journal to read.

## Reporting a bug

Open an issue. You do not need write access to this repository.

The one rule: **do not paste your own cycles into a public issue.** A real
cycle ID timestamps your activity, a record ID exposes your causal graph,
and a position size is your portfolio. `runtime/contribution.py` builds a
report from placeholder values and refuses to render one that still
carries private evidence:

```python
from runtime.contribution import render_issue

body = render_issue({
    "violated_contract": "<the rule that broke, in the code's vocabulary>",
    "expected_behavior": "<what the contract says should happen>",
    "observed_behavior": "<what happened instead>",
    "synthetic_reproduction": {"...": "placeholder values only"},
})
```

It fails closed. A refusal names what it found so you can rewrite that
part, rather than silently publishing a redaction that dropped the field
that mattered.

See `CONTRIBUTING.md`.

## Layout

```
runtime/     validators, orchestration, journal, feedback assembly
schemas/     the contract a host cycle must satisfy
prompts/     the generic standing instruction for the host
ops/         operational scripts
*.md         contracts and protocols the runtime enforces
```

Personal state is never in this tree. If you find any, that is a bug worth
an issue on its own.
