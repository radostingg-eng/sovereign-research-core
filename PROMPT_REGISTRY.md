# Research Prompt Registry v1

Prompts are research instruments, not hidden permanent rules. Every material prompt has a version, purpose, adoption state and rollback path.

## Seed prompt families

### P-PORTFOLIO-001 - Portfolio-first scan
Purpose: reconstruct exposure and identify what deserves research before generating new ideas.

Required sequence: current account state -> concentration/correlation -> short-option and covered-call exposure -> recurring theses -> cash/margin constraints -> best alternative use of capital.

### P-STANDING-001 - Hourly host cycle
Purpose: the host's permanent standing instruction. One valid cycle input committed per hour, unattended.

Full text: [`prompts/host-standing-schedule.md`](prompts/host-standing-schedule.md)

Required sequence: read FEEDBACK.json -> use accepted-cycle deltas -> source-backed EU/US market sessions -> fresh IBKR snapshot -> research rows with their own tool_calls -> decision or complete experiment contract -> commit a new file -> concise cycle delta and conditional morning brief -> stop.

Adoption state: issued 2026-09-16. Encodes the three failures that cost real cycles (malformed JSON, bare-date as_of, drifted field names). Rollback: none needed; superseding it means editing this file.

### P-EXECUTION-001 - First attributable outcome (RETIRED 2026-09-16)
Purpose: examine a WAIT that has repeated on every cycle, and reach a concrete order suggestion if the evidence already gathered supports one.

Full text: [`prompts/2026-09-16-first-execution.md`](prompts/2026-09-16-first-execution.md)

Required sequence: fresh IBKR snapshot -> the documented concentration -> the single highest-conviction risk-REDUCING action -> recommend with a complete instruction, or WAIT stating what would change it.

Adoption state: RETIRED the same day. It broke a perpetual WAIT once, then produced three identical recommendations because it encodes a thesis (that concentration should be reduced) rather than asking the host to judge. Superseded by P-STANDING-001.

### P-DISCOVERY-001 - Open strategy discovery
Purpose: discover return sources and risk controls outside a fixed menu.

Search transformations: mutate, recombine, invert, substitute, neutralize, hedge, cross-market, event-shift, volatility, source-arbitrage. Stop when candidates become duplicates or unsupported stories.

### P-THESIS-001 - Thesis validation
Purpose: turn an idea into a falsifiable thesis.

Require mispricing, mechanism, evidence, counter-evidence, invalidation, horizon, catalyst, portfolio fit and best counterfactual.

### P-INSTRUMENT-001 - Expression selection
Purpose: choose among shares, options, spreads, ETFs, hedges or waiting.

Compare expected after-tax return, downside, liquidity, capital usage, assignment and opportunity cost.

### P-ADV-001 - Adversarial re-derivation
Purpose: independently rebuild a recommendation from ledger findings and current state, then classify divergence from the original reasoning.

### P-POSTMORTEM-001 - Outcome learning
Purpose: compare prediction with execution/outcome and counterfactual, determine what was learned, and identify process changes.

## Adoption protocol

A prompt variant may be proposed when there is a measurable hypothesis that it will improve decision quality, evidence completeness, source use or calibration. A variant starts as `candidate`, then `testing`, then `adopted` or `retired`.

Adoption requires:
- explicit hypothesis
- baseline prompt version
- evaluation window
- success metric
- counter-metric for unwanted behavior
- supporting and contradictory evidence
- rollback version

Do not promote a prompt because it produces more text, more candidates or more tool calls.

## Current adoption

All seed prompts above are adopted as process templates. They can be revised through the experiment protocol. No prompt may alter the fixed safety rule that live IBKR orders are never submitted automatically.
