"""Turn a private failure into something safe to post in a public issue.

A host that hits a real bug has the evidence for it sitting in its own
profile: a cycle ID, an instrument, a position size, a journal record ID.
Pasting that into a public issue is how a portfolio leaks while somebody
is trying to be helpful.

So a report is built rather than copied. The host describes what broke in
terms of the contract that was violated, supplies a reproduction using
placeholder values, and this module refuses the report if anything that
looks like private evidence survived. The scan is deliberately blunt:
false positives cost one rewrite, false negatives are permanent and
public.

What a report may NOT contain is the whole design:

  cycle IDs          they timestamp the operator's activity
  record IDs         they expose the private causal graph
  money amounts      they are the portfolio
  account numbers    they identify the operator
  profile paths      they name the machine and the user
  credentials        they are credentials
  real tickers       only when paired with a size or a price; a bare
                     ticker in a schema example is not private

The host owns the judgement of what the bug IS. This only refuses to let
it say so using private evidence.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

# A cycle ID is "cycle-20260918T073638Z-goala3": the timestamp is the leak,
# not the prefix, so the pattern requires it.
_CYCLE_ID = re.compile(r"cycle-\d{8}T\d{6}Z", re.IGNORECASE)
# Record IDs from the journal: "opportunity-event:...", "cycle-receipt:...".
_RECORD_ID = re.compile(
    r"\b(?:cycle-receipt|cycle-stage|opportunity-event|forecast|"
    r"learning-disposition|goal-event|tool-provenance):[\w.:/-]+",
    re.IGNORECASE,
)
# Interactive Brokers account numbers, and bare long digit runs that are
# almost always a position value or an account.
_ACCOUNT = re.compile(r"\bU\d{6,}\b")
_MONEY = re.compile(r"[$€£]\s?\d[\d,]{3,}(?:\.\d+)?")
_BIG_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?\b")
# Absolute paths naming a user or a profile directory.
_HOME_PATH = re.compile(r"/(?:Users|home)/[\w.-]+")
_PROFILE_PATH = re.compile(r"\bsovereign-research-(?!core\b)[\w-]+")
# Credential shapes. Cheap to check, catastrophic to miss.
_CREDENTIAL = re.compile(
    r"\b(?:ghp_|gho_|ghs_|github_pat_|sk-|AKIA|ASIA)[A-Za-z0-9_-]{8,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|[?&](?:token|signature|sig|api_key|access_token)=[^\s&\"']+",
    re.IGNORECASE,
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cycle_id", _CYCLE_ID),
    ("record_id", _RECORD_ID),
    ("account_number", _ACCOUNT),
    ("money_amount", _MONEY),
    ("position_size", _BIG_NUMBER),
    ("home_path", _HOME_PATH),
    ("profile_path", _PROFILE_PATH),
    ("credential", _CREDENTIAL),
)

REQUIRED_FIELDS: tuple[str, ...] = (
    # What contract broke, in the vocabulary of the shared code.
    "violated_contract",
    # What the runtime did.
    "observed_behavior",
    # What the contract says it should have done.
    "expected_behavior",
    # A reproduction using placeholder values only.
    "synthetic_reproduction",
)

OPTIONAL_FIELDS: tuple[str, ...] = (
    "core_version",
    "suggested_test",
    "notes",
)


class PrivateEvidenceFound(ValueError):
    """The report still contains something that identifies an operator."""


def scan(text: str) -> list[str]:
    """Which private-evidence patterns a string matches."""
    return sorted({
        f"{name}:{match.group(0)[:40]}"
        for name, pattern in _PATTERNS
        for match in pattern.finditer(text)
    })


def check_report(report: Mapping[str, Any]) -> list[str]:
    """Every problem with a report: missing fields, then private evidence."""
    problems = [
        f"missing_field:{field}"
        for field in REQUIRED_FIELDS
        if not str(report.get(field, "")).strip()
    ]
    unexpected = sorted(
        set(report) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    )
    problems.extend(f"unexpected_field:{name}" for name in unexpected)
    problems.extend(scan(json.dumps(report, ensure_ascii=False)))
    return problems


def render_issue(report: Mapping[str, Any]) -> str:
    """The markdown body to post, or raise if it is not safe to post.

    Fails closed. A report carrying private evidence is refused rather
    than redacted, because a redaction that silently drops the one field
    that mattered produces a bug report nobody can act on, and a
    redaction that misses something is published forever.
    """
    problems = check_report(report)
    if problems:
        raise PrivateEvidenceFound("; ".join(problems))

    lines = [
        f"**Violated contract:** {report['violated_contract']}",
        "",
        "**Expected**",
        "",
        str(report["expected_behavior"]),
        "",
        "**Observed**",
        "",
        str(report["observed_behavior"]),
        "",
        "**Synthetic reproduction**",
        "",
        "```json",
        json.dumps(report["synthetic_reproduction"], indent=2),
        "```",
    ]
    if report.get("suggested_test"):
        lines += ["", "**Suggested test**", "", str(report["suggested_test"])]
    if report.get("core_version"):
        lines += ["", f"Core version: `{report['core_version']}`"]
    if report.get("notes"):
        lines += ["", str(report["notes"])]
    return "\n".join(lines)
