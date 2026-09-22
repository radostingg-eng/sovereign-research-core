"""Account schedule slots while treating observed incidents as publishable."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.schedule_ledger import run_watchdog


def account_schedule(
    profile_root: Path | str,
    *,
    workflow_version: int,
) -> Mapping[str, Any]:
    return run_watchdog(
        profile_root,
        workflow_version=workflow_version,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-root", default=".")
    parser.add_argument("--workflow-version", required=True, type=int)
    args = parser.parse_args(
        sys.argv[1:] if argv is None else list(argv)
    )
    try:
        result = account_schedule(
            args.profile_root,
            workflow_version=args.workflow_version,
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("configuration_problems") else 0


if __name__ == "__main__":
    raise SystemExit(main())
