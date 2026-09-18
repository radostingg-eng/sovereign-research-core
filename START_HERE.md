# Set up Sovereign Research

Paste this to your ChatGPT host, once:

> **Set up Sovereign Research. Read
> `radostingg-eng/sovereign-research-core/START_HERE.md` and follow it.**

Everything below is addressed to the host, not to you. Setup is autonomous.
Do not ask the operator configuration or preference questions. Continue from
the beginning through verified hourly scheduling unless one of the explicit
blockers below makes progress impossible.

---

## Host: read this in order

You are setting up a research system for **one operator**. Two places
exist and they are not interchangeable:

- **the core repo**, `radostingg-eng/sovereign-research-core` — shared
  code. Read it. Never write to it during operator setup or research cycles.
- **their profile repo**, which should be created automatically as a
  private repository — their portfolio, journal, preferences and cycles.
  Read and write it freely.

Nothing from the profile is ever committed, quoted, or referenced in the
core repo, in a core issue, or in a pull request. If you are ever unsure
which side a piece of information belongs on, it belongs in the profile.

### Ask nothing unless setup is truly blocked

Do not ask for:

- a repository name;
- portfolio or instrument preferences;
- concentration, risk, tax, or account policy;
- confirmation before creating the private profile;
- confirmation before creating the hourly task;
- permission to continue to the next setup step.

An absent preference means **no explicit preference has been recorded yet**.
It is valid state, not a setup failure. Never infer a preference from holdings.
If the operator has already volunteered a durable preference in the
conversation, capture it. Otherwise leave the explicit-preferences list empty
and continue. Future conversations and hourly cycles capture preferences when
the operator naturally mentions them.

The only setup blockers are:

1. GitHub is unavailable;
2. Interactive Brokers is unavailable;
3. the profile repository name collides with a repository that cannot be
   safely identified as this operator's Sovereign Research profile;
4. the private profile repository cannot be created or written;
5. an existing non-empty profile has invalid journal or archive history that
   cannot be repaired without rewriting operator records;
6. scheduled tasks are unavailable or the hourly task cannot be verified.

For anything else, make the safest reversible choice, record what you did, and
continue.

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
including anything useful the list above does not mention. A missing optional
connector such as web search is not a blocker; record it as unavailable and
continue with the required capabilities.

### 2. Create the profile automatically

Use the default profile repository name `sovereign-research-profile`
without asking the operator to choose or confirm a name. Create it as a
**private** repository owned by the operator.

Before creating it, confirm that the exact repository name does not
already resolve to an existing operator repository. If it already exists,
use it only when it is the operator's private profile repository. Repair that
profile in place without asking whether to continue:

1. preserve every portfolio, preference, thesis, goal, recommendation, review,
   strategy, experiment, run, feedback, and coordination file;
2. update `core.lock` to the full SHA of the core commit being used for this
   explicit setup or repair;
3. create any missing directories from the structure below;
4. replace `.github/workflows/host-cycle.yml` byte-for-byte with the workflow
   template from that pinned core commit;
5. if the journal contains exactly one hand-written genesis record and no
   later records, replace it byte-for-byte with
   `profile_templates/audit/genesis.jsonl`; if the journal has later operator
   records, never replace, truncate, or rebuild it;
6. if `audit_archive/` has no archived journal records, install the empty
   manifest template; if it has archived records, preserve them and their
   manifest;
7. verify the repaired profile, commit it, and continue through hourly task
   creation in this same setup turn.

The sole-record genesis replacement is safe because there is no operator
history after it. A malformed journal or archive containing later records is a
true blocker: report the integrity failure and stop rather than inventing a
chain. Do not ask preference questions or ask whether to perform the repair.
If the existing repository is not this operator's private profile, stop and
report the collision rather than overwriting or reusing it.

Then create the profile with exactly this structure and nothing else:

```
audit/                  host_input/         portfolio/      theses/
audit_archive/          host_staging/       recommendations/
coordination/           goals/              reviews/
experiments/            runs/               strategies/
feedback_signals/       feedback_staging/
tool_artifacts/
.github/workflows/
OPERATOR_PREFERENCES.md  PARAMETERS.json    STATE.json
core.lock               .gitignore          README.md
```

Create `feedback_signals/pending/` and `feedback_signals/shared/`. Empty
directories need a `.gitkeep`.

Copy these template files byte-for-byte from the pinned core commit:

```text
profile_templates/audit/genesis.jsonl
  -> audit/genesis.jsonl

profile_templates/audit_archive/manifest.json
  -> audit_archive/manifest.json

profile_templates/.github/workflows/host-cycle.yml
  -> .github/workflows/host-cycle.yml
```

Do not reconstruct these files from prose. The genesis template includes the
computed `record_hash`; a hand-written object without it is not a valid
journal root. The empty archive manifest makes archive integrity explicit
rather than treating an absent file as an empty archive. The workflow reads
`core.lock`, checks out that exact core commit, validates each future
`host_staging/` candidate, executes only accepted bytes, verifies the private
journal, and commits the result back to the private profile.

`tool_artifacts/` is private content-addressed evidence created by schema-v4
cycles. It stores canonical connector response bodies. Never copy it into the
shared core, automatic feedback, issues, or pull requests.

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
`OPERATOR_PREFERENCES.md` says that no explicit preferences are recorded yet
and that future explicit operator statements are appended without prompting.
`README.md` says this directory is private operator state and must never
be copied into the shared core.

After the first profile commit, verify GitHub lists
`Sovereign Profile Host Cycle` under the private repository's Actions. Do not
trigger it during setup because there is no staged candidate yet. If the
workflow file cannot be created or Actions is unavailable, that is a true
setup blocker:

> I created the private profile, but its validation and execution workflow is
> unavailable. I have not scheduled research that cannot execute. Please
> enable GitHub Actions workflow access, then tell me to continue setup.

### Profile workflow failures and notifications

The private profile installs only `Sovereign Profile Host Cycle`. It does not
install the shared code repository's `Runtime Contract` workflow.

A candidate refusal is a normal, recoverable result. The profile workflow
archives the refused bytes, writes exact correction guidance to
`host_staging/FEEDBACK.json`, publishes those changes, and completes
successfully with a `Candidate refused safely` notice. The next hourly host
cycle reads that feedback and submits a corrected new candidate without asking
the operator what to do.

Unexpected failures remain failed: an invalid `core.lock`, unavailable pinned
core, invalid journal or archive, executor failure, or publication failure
after all retries. Do not convert those failures into success-shaped output.

GitHub Actions email delivery is an account preference, not a repository or
host setting. An operator who wants no workflow email can choose:

```text
GitHub Settings -> Notifications -> System -> Actions -> Don't notify
```

This changes notification delivery only. It does not disable the hourly
ChatGPT task, the profile workflow, validation, execution, or the Actions tab.
Do not ask the operator to choose a notification preference during setup.

### 3. Capture available state without interviewing the operator

Read the portfolio from IBKR. Report what you see: positions, cash, and
anything that looks like a standing instruction or a resting order.

Do not ask preference questions. Check the conversation for durable statements
the operator already volunteered. If one exists, record the exact meaning and
what would change it in `OPERATOR_PREFERENCES.md`. If none exists, leave:

```text
No explicit operator preferences recorded yet.
```

and continue immediately. A large position is evidence of a past decision,
not permission for the next one. Instrument use is evidence of prior activity,
not a declaration that the instrument is always in scope. The normal system
already observes future operator statements; when one clearly expresses a
durable preference, append it then without turning every conversation into an
intake interview.

### 4. Create and verify the hourly host task

Read `prompts/host-standing-schedule.md` from the exact core commit in
`core.lock`. Create one enabled recurring ChatGPT task named:

```text
Sovereign Research hourly cycle
```

Its cadence is hourly. Its standing instruction is **Part B only**, beginning
with:

```text
## PART B — every scheduled run
```

Do not include Part A in the recurring instruction. Part A is the one-time
bootstrap instruction and would make every hourly run try to recreate its own
schedule instead of doing research.

The scheduled task must have access to:

- the operator's private `sovereign-research-profile` repository;
- the pinned `sovereign-research-core` commit in `core.lock`;
- Interactive Brokers;
- web search and any other connectors confirmed in step 1.

The Part B instruction points to `HOST_FEEDBACK.md`. Automatic feedback uses
only its fixed five-field host-signal envelope. It asks the operator nothing,
deduplicates by core commit and error code, and posts at most one signal per
cycle. Rich feedback remains under private `feedback_staging/` and is never
posted automatically.

After creating it, list the operator's scheduled tasks and verify all of these
before claiming setup is complete:

- exactly one enabled task is named `Sovereign Research hourly cycle`;
- its cadence is hourly;
- its instruction starts with Part B from the pinned core commit;
- its next run is in the future;
- it has not run as a side effect of setup.
- the private profile also lists the `Sovereign Profile Host Cycle` GitHub
  Action that will process each staged candidate.

If scheduled tasks are unavailable, stop and say exactly this:

> I created your private Sovereign Research profile, but I cannot create the
> hourly task in this session. Please enable scheduled tasks, then tell me to
> continue setup. I have not started a research cycle.

Do not substitute a GitHub Actions cron for the ChatGPT host task. The host
task owns connector calls and investment judgement. Validator and executor
automation process what the host commits; they do not replace the host.

### 5. Finish setup, but do not start research

Report:

- which connectors you confirmed, and any you asked for
- the profile repository, and that it is private
- the core commit you pinned
- the scheduled task name, cadence, next run, and that it is enabled
- the positions you read
- the durable preferences captured from statements already volunteered, or
  `none explicitly recorded`
- that no order was created, modified or transmitted, and that
  `order_submission_used` is false

Do not ask a follow-up question. Then stop. Do not run a research cycle in the
same turn as setup. The first cycle starts automatically at the verified next
run of `Sovereign Research hourly cycle` and reads
`prompts/host-standing-schedule.md` from the pinned core commit.

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
