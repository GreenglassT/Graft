"""What a free Apple ID is told comes from one place, and every copy agrees.

The setup steps and their URLs, and the way to become a paid account, were
written out by hand four times: the free starter's header, `--init`'s closing
steps, the "no LiveContainer found" hint and the paid-only refusal. They are
now constants in `config`, formatted by the others. These tests check that
each place still says what the constants say, and that no other module keeps
a copy of its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from graft_ipa import config, init_flow, lc_discovery
from graft_ipa.config import write_default_config

PACKAGE = Path(config.__file__).parent
README = PACKAGE.parent / "README.md"
URLS = (config.SIDESTORE_URL, config.ILOADER_URL, config.LIVECONTAINER_SETUP_URL)


def _flat(text: str) -> str:
    """Comment markers and line wrapping out, so wrapped copies compare."""
    return " ".join(re.sub(r"(?m)^# ?", "", text).split())


@pytest.fixture
def starter(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    write_default_config(path, free_account=True)
    return path


@pytest.fixture
def header(starter: Path) -> str:
    return starter.read_text().split("cache_dir:")[0]


def test_every_url_is_in_the_steps() -> None:
    steps = " ".join(config.FREE_DEVICE_STEPS)

    for url in URLS:
        assert url in steps


def test_the_starter_header_has_the_device_steps_in_order(header: str) -> None:
    flat = _flat(header)

    positions = [flat.find(_flat(step)) for step in config.FREE_DEVICE_STEPS]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions)
    assert config.FREE_INSTALLERS in flat


def test_the_no_livecontainer_hint_has_the_device_steps() -> None:
    for step in config.FREE_DEVICE_STEPS:
        assert _flat(step) in _flat(lc_discovery.FREE_SETUP_HINT)


def test_init_prints_the_shared_steps() -> None:
    assert init_flow.FREE_SETUP_STEPS is config.FREE_SETUP_STEPS


def test_the_refusal_and_the_header_give_the_same_way_to_go_paid(
    starter: Path, header: str
) -> None:
    hint = config.paid_account_required("--lc-install", starter).hint

    # The refusal has the short form (it has to fit the app's error banner),
    # the header the long one; both name the same command.
    assert _flat(config.paid_upgrade(starter, short=True)) in _flat(hint)
    assert _flat(config.paid_upgrade(starter, config="this file")) in _flat(header)
    assert config.FREE_ALTERNATIVE in hint
    command = config.paid_upgrade(starter, short=True).split("`")[1]
    assert command in _flat(header)


def test_the_check_row_for_a_sideload_app_gives_the_same_way(starter: Path) -> None:
    from graft_ipa import preflight
    from graft_ipa.config import add_app, load_config

    add_app(starter, "side", {"bundle_id": "com.example.side", "delivery": "sideload"})
    [row] = preflight.check_app_deliveries(load_config(starter))

    assert _flat(config.paid_upgrade(starter, short=True)) in _flat(row.hint)


def test_no_other_module_keeps_its_own_copy_of_a_url() -> None:
    for source in PACKAGE.glob("*.py"):
        if source.name == "config.py":
            continue
        text = source.read_text()
        for url in URLS:
            assert url not in text, f"{source.name} has its own copy of {url}"


def test_the_readme_points_at_the_same_places() -> None:
    text = README.read_text()

    for url in URLS:
        assert url in text


def test_the_certificate_step_says_what_ios_26_does_without_it() -> None:
    """On iOS 26 a free Apple ID's LiveContainer opens nothing until its
    certificate is imported from SideStore, and iloader does not do that step
    although its README says it does. The step says both, in the words the
    phone and LiveContainer use."""
    step = config.FREE_DEVICE_STEPS[-1]

    assert "open Settings and tap Import Certificate from SideStore" in step
    assert "iloader doesn't do this for you" in step
    assert '"JITLess mode is required since iOS 26"' in step


def test_the_free_device_steps_send_nobody_to_terminal() -> None:
    """The app's first-run done screen shows these as they are (F3 of the 2.0
    review): no `graft` command, no Terminal, no shell quoting."""
    import re

    from graft_ipa.config import FREE_DEVICE_STEPS

    for step in FREE_DEVICE_STEPS:
        assert not re.search(r"\bgraft\s+--|terminal|`", step, re.IGNORECASE), step
