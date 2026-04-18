#!/usr/bin/env python3
"""End-to-end contract check: import the function and run known inputs.

The mission.yaml runs this with cwd = workspace, then cd's into app/. The
check is invoked relative to the mission dir. The check runs from
$workspace/app when we call `python3 ../checks/verify_function.py`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_fizzbuzz():
    path = Path("fizzbuzz.py").resolve()
    if not path.exists():
        # When invoked from workspace root instead of app/
        alt = Path("app/fizzbuzz.py").resolve()
        if alt.exists():
            path = alt
        else:
            print(f"fizzbuzz.py not found (looked at {path})", file=sys.stderr)
            sys.exit(2)
    spec = importlib.util.spec_from_file_location("fizzbuzz", path)
    if spec is None or spec.loader is None:
        print("could not load fizzbuzz module", file=sys.stderr)
        sys.exit(2)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = _load_fizzbuzz()
    if not hasattr(mod, "fizzbuzz"):
        print("no fizzbuzz function exported", file=sys.stderr)
        return 1
    cases = {
        1: "1",
        2: "2",
        3: "Fizz",
        5: "Buzz",
        6: "Fizz",
        10: "Buzz",
        15: "FizzBuzz",
        30: "FizzBuzz",
        7: "7",
    }
    failures: list[str] = []
    for n, expected in cases.items():
        got = mod.fizzbuzz(n)
        if got != expected:
            failures.append(f"fizzbuzz({n}) = {got!r}, expected {expected!r}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"{len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
