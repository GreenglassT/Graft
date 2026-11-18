#!/usr/bin/env python3
"""Create (or reuse) development profiles for exact, already-registered App IDs.

A thin wrapper over `graft_ipa.lc_manage.AscProfiles`, kept so the hand
procedure in the LiveContainer notes still works; `graft --lc-install`
resolves these profiles itself.

  lc_profiles.py com.kdt.livecontainer.TEAM com.kdt.LiveContainer2.TEAM ...

An App ID that is not registered is reported and skipped, never created: its
App Group cannot be assigned through the App Store Connect API. Profiles are
cached under app_store_connect.profile_cache_dir with the same naming every
auto-provisioned profile uses.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from graft_ipa import config, mobileprovision, signer
from graft_ipa.apple_portal import AppIdNotRegistered
from graft_ipa.lc_manage import AscProfiles


def main() -> int:
    cfg = config.load_config(config.default_config_path())
    password = signer.resolve_p12_password(cfg)
    if not password:
        print(f"set {cfg.signing.cert_password_env}", file=sys.stderr)
        return 2
    profiles = AscProfiles(cfg, password)
    for ident in sys.argv[1:]:
        try:
            path = profiles.exact(ident)
        except AppIdNotRegistered as e:
            print(f"{ident}: {e.message}")
            continue
        pl = mobileprovision.decode(path)
        ents = pl["Entitlements"]
        print(f"{ident}: {path.name} | devices={len(pl.get('ProvisionedDevices', []))} "
              f"app-id={ents.get('application-identifier')} "
              f"groups={ents.get('com.apple.security.application-groups')} "
              f"memlimit={ents.get('com.apple.developer.kernel.increased-memory-limit')} "
              f"expires={pl['ExpirationDate']:%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
