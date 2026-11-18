#!/usr/bin/env python3
"""Prepare a LiveContainer IPA as the primary instance or as LiveContainer2/3.

A thin wrapper over `graft_ipa.lc_instance`, kept so the hand procedure in
the LiveContainer notes still works. `graft --lc-install` does all of
this (and the signing and installing) itself; prefer that.

  lc_instance.py IN.ipa OUT.ipa --team TEAM --primary
  lc_instance.py IN.ipa OUT.ipa --team TEAM --name LiveContainer2

Output is unsigned; sign it with lc_resign.py.
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from graft_ipa import lc_instance


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ipa_in", type=Path)
    ap.add_argument("ipa_out", type=Path)
    ap.add_argument("--team", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--primary", action="store_true")
    g.add_argument("--name", choices=["LiveContainer2", "LiveContainer3"])
    args = ap.parse_args()
    instance = 1 if args.primary else int(args.name[-1])

    with tempfile.TemporaryDirectory(prefix="lcinst-") as td:
        app = lc_instance.extract_app(args.ipa_in, Path(td))
        prepared = lc_instance.prepare_app(app, team=args.team, instance=instance)
        lc_instance.archive_payload(Path(td), args.ipa_out)
    print(f"{prepared.bundle_id} -> {args.ipa_out}")
    for name, bid in prepared.appex_ids.items():
        print(f"  {name}: {bid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
