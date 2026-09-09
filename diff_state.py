#!/usr/bin/env python3
"""Diff committed state.json against the freshly scanned one and print any
newly seen showtime sids as JSON. Used by the workflow to decide whether to
open a 'new showtimes' issue. Prints [] when there is nothing new or when
state.json is new this run (first-run seeding is silent)."""

import json
import subprocess
import sys

STATE = "state.json"


def committed_state():
    try:
        out = subprocess.run(
            ["git", "show", f"HEAD:{STATE}"],
            capture_output=True, text=True, check=True,
        ).stdout
        return json.loads(out).get("seen", {})
    except Exception:
        return None  # state.json not committed yet -> first run


def main() -> None:
    old = committed_state()
    try:
        with open(STATE) as f:
            new = json.load(f).get("seen", {})
    except FileNotFoundError:
        print("[]")
        return
    if old is None:
        print("[]")  # first run seeds silently
        return
    added = [
        {"sid": sid, **{k: v for k, v in info.items() if k != "first_seen"}}
        for sid, info in new.items()
        if sid not in old
    ]
    print(json.dumps(added, indent=2))


if __name__ == "__main__":
    main()
