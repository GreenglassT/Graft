"""The golden check: sign the real LiveContainer the real way, and look.

Opt-in (`GRAFT_LC_GOLDEN=1`), because it needs this machine's cached
upstream IPA, the account's cached profiles, the signing `.p12` and its
password, and it really runs `codesign` and `security` — through a throwaway
keychain whose addition to the search list is reverted, which this test
verifies byte for byte. It reads `~/.config/graft/` and writes nothing
there; everything it produces is under `tmp_path`.

Everything else in the suite runs against fakes. This is the one place the
claim "these are the entitlements LiveContainer's keychain hook needs" is
checked against what `codesign` actually wrote into the binaries. Run it with
`-s` to see the per-instance summary.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from graft_ipa import lc_instance, lc_sign

pytestmark = pytest.mark.skipif(
    os.environ.get("GRAFT_LC_GOLDEN") != "1",
    reason="golden check: set GRAFT_LC_GOLDEN=1 (needs the real profiles, .p12 and IPA)",
)

TEAM = "ABCDE12345"
IPA = Path.home() / ".cache/graft/ipas/com.kdt.livecontainer_3.8.14.ipa"
PROFILES = Path.home() / ".config/graft/profiles"
P12 = Path.home() / ".config/graft/AppleDevelopment.p12"
WILDCARD = PROFILES / "com_kdt_x.mobileprovision"
GROUP = f"group.com.SideStore.SideStore.{TEAM}"


def _p12_password() -> str:
    """The `.p12` password, from whichever variable `config.yaml` names.

    Not a fixed name: `signing.cert_password_env` is the user's choice, and a
    config written before the rename still names its old variable.
    """
    import yaml

    raw = yaml.safe_load((Path.home() / ".config/graft/config.yaml").read_text())
    return os.environ[(raw.get("signing") or {}).get("cert_password_env") or "GRAFT_P12_PASS"]


def _profile_for(bundle_id: str) -> Path:
    exact = PROFILES / f"{bundle_id}.mobileprovision"
    # ShareExtension has no App ID of its own and rides the wildcard.
    return exact if exact.exists() else WILDCARD


def _search_list() -> bytes:
    return subprocess.run(
        ["security", "list-keychains", "-d", "user"], check=True, capture_output=True
    ).stdout


def _signed_entitlements(binary: Path) -> dict:
    out = subprocess.run(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(binary)],
        check=True, capture_output=True,
    ).stdout
    return plistlib.loads(out)


def test_all_three_instances_sign_with_the_entitlements_the_hook_needs(tmp_path: Path) -> None:
    password = _p12_password()
    identity = lc_sign.p12_identity(P12, password)
    runner = lc_sign.Runner()
    before = _search_list()
    summary: list[str] = []

    try:
        with lc_sign.temp_keychain(runner, P12, password, tmp_path) as keychain:
            for instance in (1, 2, 3):
                work = tmp_path / f"i{instance}"
                app = lc_instance.extract_app(IPA, work)
                prepared = lc_instance.prepare_app(app, team=TEAM, instance=instance)
                reports = lc_sign.sign_app(
                    app, profile_for=_profile_for, identity=identity, keychain=keychain,
                    runner=runner, workdir=work,
                )
                summary += [f"instance {instance}: {r.summary()}" for r in reports]

                ents = _signed_entitlements(lc_instance.bundle_file(
                    app, plistlib.loads((app / "Info.plist").read_bytes())
                ))
                groups = ents["keychain-access-groups"]
                assert ents["application-identifier"] == f"{TEAM}.{prepared.bundle_id}"
                assert len(groups) == 128
                assert groups[0] == f"{TEAM}.com.kdt.livecontainer.shared"
                assert all(g.startswith(f"{TEAM}.com.kdt.livecontainer.shared") for g in groups)
                assert ents["com.apple.security.application-groups"] == [GROUP]
                # sign_app already ran `--verify --deep --strict`; once more,
                # here, so this test does not depend on that line staying put.
                subprocess.run(
                    ["codesign", "--verify", "--deep", "--strict", str(app)],
                    check=True, capture_output=True,
                )
                if instance == 1:
                    live = app / "PlugIns" / "LiveProcess.appex" / "LiveProcess"
                    live_ents = _signed_entitlements(live)
                    assert live_ents["application-identifier"] == (
                        f"{TEAM}.{prepared.bundle_id}.LiveProcess"
                    )
                    assert len(live_ents["keychain-access-groups"]) == 128
    finally:
        after = _search_list()
        print("\n" + "\n".join(summary))
        print(f"search list identical: {after == before}")

    assert after == before
