#!/usr/bin/env python3
"""Re-sign a LiveContainer IPA with explicit entitlements, the way AltStore does.

A thin wrapper over `graft_ipa.lc_sign`, kept so the hand procedure in the
LiveContainer notes still works; `graft --lc-install` does this itself.
See `graft_ipa/lc_sign.py` for why zsign cannot be used here.

Usage:
  lc_resign.py IN.ipa OUT.ipa --profile MAIN.mobileprovision \
      [--appex-profile NAME.appex=PATH ...] --p12 P12 \
      [--p12-pass-env GRAFT_P12_PASS] [--verbatim-app-id]

The signing identity is the .p12's own certificate (`--identity` is accepted
and ignored, for old command lines).
"""
import argparse
import os
import plistlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from graft_ipa import lc_instance, lc_sign


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ipa_in", type=Path)
    ap.add_argument("ipa_out", type=Path)
    ap.add_argument("--profile", type=Path, required=True, help="profile for the main app")
    ap.add_argument("--appex-profile", action="append", default=[],
                    help="NAME.appex=PATH; extensions not listed use --profile")
    ap.add_argument("--identity", help="ignored; the .p12's certificate is used")
    ap.add_argument("--p12", type=Path, required=True)
    ap.add_argument("--p12-pass-env", default="GRAFT_P12_PASS")
    ap.add_argument("--verbatim-app-id", action="store_true",
                    help="keep the profile's literal application-identifier "
                         "(to upgrade a zsign-signed install in place)")
    args = ap.parse_args()
    password = os.environ.get(args.p12_pass_env)
    if password is None:
        print(f"set {args.p12_pass_env}", file=sys.stderr)
        return 2
    by_appex = dict(item.partition("=")[::2] for item in args.appex_profile)

    runner = lc_sign.Runner()
    with tempfile.TemporaryDirectory(prefix="lcsign-") as td:
        work = Path(td)
        app = lc_instance.extract_app(args.ipa_in, work / "x")
        # The old interface named extension profiles by bundle *name*; the
        # package asks by bundle *id*, so translate once.
        by_id = {
            plistlib.loads((a / "Info.plist").read_bytes())["CFBundleIdentifier"]:
                Path(by_appex[a.name])
            for a in app.glob("PlugIns/*.appex") if a.name in by_appex
        }
        with lc_sign.temp_keychain(runner, args.p12, password, work) as keychain:
            reports = lc_sign.sign_app(
                app, profile_for=lambda bid: by_id.get(bid, args.profile),
                identity=lc_sign.p12_identity(args.p12, password), keychain=keychain,
                runner=runner, workdir=work, verbatim_app_id=args.verbatim_app_id,
            )
        lc_instance.archive_payload(work / "x", args.ipa_out)
    for r in reports:
        print(r.summary())
    print(f"wrote {args.ipa_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
