# Set up Sovereign Research

Paste this to your ChatGPT host, once:

> **Set up Sovereign Research. Read
> `radostingg-eng/sovereign-research-core/START_HERE.md` and follow it.**

Everything below is addressed to the host, not to you. You will be asked
for two things: to install a connector if one is missing, and to confirm
the name of your private repository before anything is written to it.

---

## Host: read this in order and stop where it says stop

You are setting up a research system for **one operator**. Two places
exist and they are not interchangeable:

- **the core repo**, `radostingg-eng/sovereign-research-core` — shared
  code. Read it. Never write to it.
- **their profile repo**, which does not exist yet — their portfolio,
  journal, preferences and cycles. Read and write it freely.

Nothing from the profile is ever committed, quoted, or referenced in the
core repo, in a core issue, or in a pull request. If you are ever unsure
which side a piece of information belongs on, it belongs in the profile.

### 1. Check what you can actually call

List the connectors available to you in this session. Do not assume a
connector exists because this file mentions it. You need:

| Capability | Why | Required |
|---|---|---|
| GitHub | create and commit the profile repo | yes |
| Interactive Brokers | read the portfolio the research is about | yes |
| Web search | evidence that is not in the portfolio | strongly recommended |

If **GitHub** is missing, stop and say exactly this:

> I need the GitHub connector to continue. Please enable it in Settings →
> Connectors, then tell me to continue.

If **Interactive Brokers** is missing, stop and say exactly this:

> I need the Interactive Brokers connector to read your portfolio. Please
> enable it in Settings → Connectors and connect your IBKR account, then
> tell me to continue. Paper trading is fine.

Do not work around a missing connector, and do not substitute a different
data source for the portfolio. Report what you can genuinely call,
including anything useful the list above does not mention.

### 2. Confirm the profile repository name

Propose a name, defaulting to `sovereign-research-profile`, and say
plainly that it will be **private** and will contain their portfolio.

Stop and wait for confirmation. Do not create it before they answer.

### 3. Create the profile repository

Create it **private**, owned by the operator. Then commit this structure,
with the exact files below and nothing else:

```
audit/                  host_input/         portfolio/      theses/
audit_archive/          host_staging/       recommendations/
coordination/           goals/              reviews/
experiments/            runs/               strategies/
OPERATOR_PREFERENCES.md  PARAMETERS.json    STATE.json
core.lock                .gitignore          README.md
```

Empty directories need a `.gitkeep`.

`audit/<today>-genesis.jsonl`, one line, which starts the hash chain:

```json
{"created_at":"<now, ISO 8601 UTC>","payload":{"note":"Empty profile. No portfolio, preferences, theses or goals are implied.","schema_version":1},"prev_hash":null,"record_id":"profile-genesis","record_type":"profile_genesis"}
```

`core.lock`, pinning the exact core commit you read today:

```json
{"core_repo":"radostingg-eng/sovereign-research-core","commit":"<full 40-char SHA of core main>"}
```

This pin is the point. The operator moves to a newer core when they
choose to, having looked at what changed. Never bump it silently, and
never run a core commit other than the pinned one.

`.gitignore`:

```
var/
FEEDBACK.json
__pycache__/
*.pyc
.DS_Store
```

`STATE.json` and `PARAMETERS.json` are `{"schema_version": 1}`.
`OPERATOR_PREFERENCES.md` is the template from step 4.
`README.md` says this directory is private operator state and must never
be copied into the shared core.

### 4. Ask for preferences, then stop guessing

Read the portfolio from IBKR. Report what you see: positions, cash, and
anything that looks like a standing instruction or a resting order.

Then ask for durable preferences, and write only what they tell you into
`OPERATOR_PREFERENCES.md`:

- Is a large single-name allocation acceptable, or is concentration a
  defect to correct?
- Which instrument types are in scope? Options, futures, non-US listings?
- Anything you must never do regardless of how good it looks?
- Any tax or account constraint that changes what is actionable?

Write these as decisions with a falsifying condition, not as opinions. A
preference nothing could contradict will quietly shape every later cycle
and can never be reviewed.

Do not infer a preference from the portfolio. A large position is
evidence of a past decision, not permission for the next one.

### 5. Say what happens next, and stop

Report:

- which connectors you confirmed, and any you asked for
- the profile repository, and that it is private
- the core commit you pinned
- the positions you read, and the preferences they gave you
- that no order was created, modified or transmitted, and that
  `order_submission_used` is false

Then stop. Do not run a research cycle in the same turn as setup. The
first cycle reads `prompts/host-standing-schedule.md` from the pinned
core commit, and it deserves its own run.

---

## Boundaries that hold from here on

You stage; the operator transmits. You may create an order *instruction*,
which stages a proposal in IBKR for them to review, and you must never
transmit a live order. `order_submission_used` stays false in every cycle
you commit.

You write candidates to `host_staging/`, never directly to `host_input/`.
The runtime validates a candidate and promotes the exact bytes it
accepted. A staging commit means you published, not that you succeeded.

When you find a bug in the shared code, do not paste your cycle into a
public issue. Your cycle IDs timestamp the operator's activity, your
record IDs expose their causal graph, and your position sizes are their
portfolio. Build the report with `runtime/contribution.py`, which uses
placeholder values and refuses to publish private evidence. See
`CONTRIBUTING.md`.
