from __future__ import annotations

import pytest

# Which LiveContainer a push chooses is `lc_discovery.choose` now, shared with
# `--lc-status`; its cases, including the ones that used to live here, are in
# test_lc_discovery.py.


# --- listing a LiveContainer's Documents/ --------------------------------------
#
# The reconcile path's only device call. Nothing here touches a phone: the two
# pymobiledevice3 entry points the function imports at call time are replaced
# with fakes that record whether they were closed again.


class _FakeLockdown:
    def __init__(self, closed: list[str]) -> None:
        self._closed = closed

    async def close(self) -> None:
        self._closed.append("lockdown")


class _FakeHouseArrest:
    def __init__(self, closed: list[str], names: list[str] | None = None) -> None:
        self._closed = closed
        self._names = names or []

    async def listdir(self, path: str) -> list[str]:
        assert path == "Documents", "the push writes there; reconcile must look there"
        if self._names is None:  # pragma: no cover - defensive
            raise AssertionError
        return self._names

    async def close(self) -> None:
        self._closed.append("house-arrest")


def _fake_services(
    monkeypatch: pytest.MonkeyPatch,
    closed: list[str],
    *,
    names: list[str] | None = None,
    listdir_error: Exception | None = None,
    create_error: Exception | None = None,
) -> None:
    import pymobiledevice3.lockdown as lockdown_mod
    import pymobiledevice3.services.house_arrest as house_arrest_mod

    async def _create_lockdown(*, serial: str):
        assert serial == "UDID1"
        return _FakeLockdown(closed)

    async def _create_house_arrest(*, lockdown, bundle_id: str):
        assert bundle_id == "com.kdt.livecontainer.T"
        if create_error is not None:
            raise create_error
        ha = _FakeHouseArrest(closed, names)
        if listdir_error is not None:
            async def _boom(_path: str):
                raise listdir_error
            ha.listdir = _boom  # type: ignore[method-assign]
        return ha

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", _create_lockdown)
    monkeypatch.setattr(
        house_arrest_mod.HouseArrestService, "create", _create_house_arrest
    )


def test_documents_are_reported_as_bare_filenames(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller compares against the bare `remote_name` the push recorded, so
    a version of afc that answers with full paths must not read as "missing"."""
    from graft_ipa.lc_runner import list_livecontainer_documents

    closed: list[str] = []
    _fake_services(
        monkeypatch, closed, names=["example.ipa", "/private/var/mobile/Documents/bravo.ipa"]
    )

    names = list_livecontainer_documents(udid="UDID1", lc_bundle_id="com.kdt.livecontainer.T")

    assert names == ["example.ipa", "bravo.ipa"]
    assert closed == ["house-arrest", "lockdown"]


def test_a_failed_listing_still_closes_both_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graft_ipa.lc_runner import list_livecontainer_documents

    closed: list[str] = []
    _fake_services(monkeypatch, closed, listdir_error=OSError("afc went away"))

    with pytest.raises(OSError, match="afc went away"):
        list_livecontainer_documents(udid="UDID1", lc_bundle_id="com.kdt.livecontainer.T")

    assert closed == ["house-arrest", "lockdown"]


def test_a_sandbox_that_will_not_open_still_closes_the_lockdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common failure: LiveContainer isn't installed on the phone in hand.

    Reconcile runs per app, so leaking a usbmux connection here would leak one
    per app on every check.
    """
    from graft_ipa.lc_runner import list_livecontainer_documents

    closed: list[str] = []
    _fake_services(monkeypatch, closed, create_error=RuntimeError("no such bundle id"))

    with pytest.raises(RuntimeError, match="no such bundle id"):
        list_livecontainer_documents(udid="UDID1", lc_bundle_id="com.kdt.livecontainer.T")

    assert closed == ["lockdown"]
