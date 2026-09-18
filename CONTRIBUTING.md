# Contributing

You do not need write access. Fork, or just open an issue.

## The one hard rule

Nothing from your profile goes in a public issue or pull request.

Your profile holds the evidence for whatever bug you found, which is
exactly what makes it tempting to paste. A cycle ID timestamps your
activity. A journal record ID exposes your causal graph. A position size
is your portfolio. A path names your machine.

So a report gets **rebuilt**, not copied:

```python
from runtime.contribution import render_issue

print(render_issue({
    "violated_contract": "rediscovered candidates must carry rediscovery_of",
    "expected_behavior": "the second proposal is refused without a reference",
    "observed_behavior": "the second proposal was accepted as new",
    "synthetic_reproduction": {
        "cycle_one": {"candidate_id": "scout-acme-one"},
        "cycle_two": {"candidate_id": "scout-acme-two"},
    },
}))
```

`render_issue` scans for cycle IDs, record IDs, account numbers, money
amounts, home paths, profile repository names and credentials. If it finds
any it raises and names what it found. It does not redact and continue: a
redaction that misses one field is published forever, and one that drops
the wrong field produces a report nobody can act on.

A bare ticker is fine. A ticker with a size is not.

## What makes a good report

State the **contract** that broke, in the vocabulary the code uses, not
the symptom you noticed. "The opportunity ledger accepted a duplicate
identity" is actionable. "It did something weird with my positions" is
not, and tempts you toward pasting the positions.

Then: what you expected, what happened, and a reproduction built from
placeholders. A suggested test is welcome and usually settles the
argument faster than prose.

## Code

- Every bug fix carries a regression test that would have caught it.
- Tests assert behaviour: call the function, check the result. No
  `hasattr` probes, no asserting that source text contains a string.
- Tests must not read a real profile. `python3 -m pytest runtime/` has to
  pass on a fresh clone with no `SOVEREIGN_PROFILE_DIR` set.
- Nothing in the runtime decides what to invest in. If a change adds a
  threshold, a ranking or a default that shapes an investment outcome, it
  belongs in the host's judgement, not here.
- No state path derived from `__file__` or the working directory. State
  goes through `runtime.profile_paths`.

## Review

Changes to `prompts/`, `runtime/prompt_invariants.py` and `schemas/` alter
what every operator's host is instructed to do, so they get read closely
and merged slowly. That is not distrust; it is that a prompt change is a
behaviour change for people who are not in the conversation.

Operators pin a reviewed commit rather than tracking `main`, so nothing
merged here reaches a running system until its operator chooses to move.
