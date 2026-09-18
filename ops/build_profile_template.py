#!/usr/bin/env python3
"""Build a data-free GitHub repository template for new private profiles."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runtime.init_profile import GITIGNORE, _core_lock  # noqa: E402

_COMMIT = re.compile(r"^[0-9a-f]{40}$")

TEMPLATE_README = """# Sovereign Research private profile

This repository is a data-free installation template. The repository created
from it must be **private**.

After creating the private repository, the ChatGPT host writes exactly:

```text
bootstrap/request.json
```

with `{}` as its content. The preinstalled `Sovereign Profile Bootstrap`
workflow initializes the unique private journal, admission policy, feedback,
directories, and control files from the pinned core. The host does not create
or edit workflow files.

Do not place portfolio data, account identifiers, connector responses, or
preferences in the public template repository.
"""

BOOTSTRAP_README = """# Bootstrap request

The profile owner creates the repository from the public template and chooses
private visibility. The ChatGPT host may then create `bootstrap/request.json`
containing `{}`. The preinstalled workflow consumes and removes that request.
"""


def build_template(
    output: Path | str,
    *,
    core_commit: str,
) -> list[str]:
    if _COMMIT.fullmatch(core_commit) is None:
        raise ValueError("core_commit_must_be_full_sha")
    output = Path(output).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"template_output_not_empty:{output}")
    output.mkdir(parents=True, exist_ok=True)
    written = []
    workflows = ROOT / "profile_templates" / ".github" / "workflows"
    for name in ("host-cycle.yml", "profile-bootstrap.yml"):
        target = output / ".github" / "workflows" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(workflows / name, target)
        written.append(str(target.relative_to(output)))
    files = {
        "core.lock": _core_lock(core_commit),
        ".gitignore": GITIGNORE,
        "README.md": TEMPLATE_README,
        "bootstrap/README.md": BOOTSTRAP_README,
    }
    for name, body in files.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        written.append(name)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    parser.add_argument("--core-commit", required=True)
    args = parser.parse_args(argv)
    written = build_template(args.output, core_commit=args.core_commit)
    print(f"created {len(written)} template files in {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
