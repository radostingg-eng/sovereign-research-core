"""Repair one profile without rewriting valid operator state."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .init_profile import repair_profile
from .profile_health import check_profile
from .profile_paths import profile_root


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(profile_root()),
        help="private profile root",
    )
    parser.add_argument("--core-commit", required=True)
    parser.add_argument("--upgrade-core", action="store_true")
    parser.add_argument("--no-workflow", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    try:
        changed = repair_profile(
            root,
            core_commit=args.core_commit,
            upgrade_core=args.upgrade_core,
            install_workflow=not args.no_workflow,
        )
        errors = check_profile(root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"profile repair refused: {error}")
        return 1
    if errors:
        print("profile repair failed health: " + ",".join(errors))
        return 1
    print(f"profile repair healthy; {len(changed)} paths changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
