"""`storefronts`: which App Stores exist, and which store a region shops in.

Every place Graft reads or writes a country goes through this module: the
search's `--country`, `--store-country`, a hand-written `store_country`, a URL's
country, and this Mac's region. The Mac app holds the same list
(`Storefronts.swift`), pinned against this one below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from graft_ipa import storefronts
from graft_ipa.errors import ConfigError
from tests.cli_ast import strip_swift_comments

SWIFT = Path(__file__).resolve().parent.parent / "Graft" / "Graft" / "Models" / "Storefronts.swift"


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "apple_storefronts.tsv"

#: Each storefront's ISO 3166-1 alpha-3 code, the form Apple's table uses
#: (Kosovo: XKS, Apple's own).
ALPHA3 = dict(
    AE="ARE", AF="AFG", AG="ATG", AI="AIA", AL="ALB", AM="ARM", AO="AGO", AR="ARG", AT="AUT",
    AU="AUS", AZ="AZE", BA="BIH", BB="BRB", BE="BEL", BF="BFA", BG="BGR", BH="BHR", BJ="BEN",
    BM="BMU", BN="BRN", BO="BOL", BR="BRA", BS="BHS", BT="BTN", BW="BWA", BY="BLR", BZ="BLZ",
    CA="CAN", CD="COD", CG="COG", CH="CHE", CI="CIV", CL="CHL", CM="CMR", CN="CHN", CO="COL",
    CR="CRI", CV="CPV", CY="CYP", CZ="CZE", DE="DEU", DK="DNK", DM="DMA", DO="DOM", DZ="DZA",
    EC="ECU", EE="EST", EG="EGY", ES="ESP", FI="FIN", FJ="FJI", FM="FSM", FR="FRA", GA="GAB",
    GB="GBR", GD="GRD", GE="GEO", GH="GHA", GM="GMB", GR="GRC", GT="GTM", GW="GNB", GY="GUY",
    HK="HKG", HN="HND", HR="HRV", HU="HUN", ID="IDN", IE="IRL", IL="ISR", IN="IND", IQ="IRQ",
    IS="ISL", IT="ITA", JM="JAM", JO="JOR", JP="JPN", KE="KEN", KG="KGZ", KH="KHM", KN="KNA",
    KR="KOR", KW="KWT", KY="CYM", KZ="KAZ", LA="LAO", LB="LBN", LC="LCA", LK="LKA", LR="LBR",
    LT="LTU", LU="LUX", LV="LVA", LY="LBY", MA="MAR", MD="MDA", ME="MNE", MG="MDG", MK="MKD",
    ML="MLI", MM="MMR", MN="MNG", MO="MAC", MR="MRT", MS="MSR", MT="MLT", MU="MUS", MV="MDV",
    MW="MWI", MX="MEX", MY="MYS", MZ="MOZ", NA="NAM", NE="NER", NG="NGA", NI="NIC", NL="NLD",
    NO="NOR", NP="NPL", NR="NRU", NZ="NZL", OM="OMN", PA="PAN", PE="PER", PG="PNG", PH="PHL",
    PK="PAK", PL="POL", PT="PRT", PW="PLW", PY="PRY", QA="QAT", RO="ROU", RS="SRB", RU="RUS",
    RW="RWA", SA="SAU", SB="SLB", SC="SYC", SE="SWE", SG="SGP", SI="SVN", SK="SVK", SL="SLE",
    SN="SEN", SR="SUR", ST="STP", SV="SLV", SZ="SWZ", TC="TCA", TD="TCD", TH="THA", TJ="TJK",
    TM="TKM", TN="TUN", TO="TON", TR="TUR", TT="TTO", TW="TWN", TZ="TZA", UA="UKR", UG="UGA",
    US="USA", UY="URY", UZ="UZB", VC="VCT", VE="VEN", VG="VGB", VN="VNM", VU="VUT", XK="XKS",
    YE="YEM", ZA="ZAF", ZM="ZMB", ZW="ZWE",
)
#: The territories' alpha-3 codes, to look for on Apple's table.
TERRITORY_ALPHA3 = dict(
    PR="PRI", GU="GUM", VI="VIR", AS="ASM", MP="MNP", RE="REU", GP="GLP", MQ="MTQ", GF="GUF",
    YT="MYT", PM="SPM", BL="BLM", MF="MAF", GL="GRL", FO="FRO", AX="ALA",
    MC="MCO", LI="LIE", SM="SMR", VA="VAT", GI="GIB", IM="IMN", JE="JEY", GG="GGY", SJ="SJM",
)


def _apples_table() -> dict[str, str]:
    """Apple's published countries and regions, code to name (the fixture)."""
    rows = [line.split("\t") for line in FIXTURE.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")]
    return {code: name for code, name in rows}


def test_the_list_is_apples_published_one() -> None:
    """Checked against Apple's own table on 2026-11-20 (the fixture's URL).
    Apple's other table, of release times by country, has two errors of its
    own: Bolivia printed as BO, and one row for Niger and Nigeria both."""
    table = _apples_table()
    assert len(table) == 175
    assert set(ALPHA3) == storefronts.STOREFRONTS
    assert {ALPHA3[code] for code in storefronts.STOREFRONTS} == set(table)
    assert (table["NER"], table["NGA"], table["BOL"], table["XKS"]) == ("Niger", "Nigeria", "Bolivia", "Kosovo")


def test_no_territory_is_a_store_on_apples_table() -> None:
    table = _apples_table()
    assert set(TERRITORY_ALPHA3) == set(storefronts.TERRITORIES)
    assert [t for t, code in TERRITORY_ALPHA3.items() if code in table] == []
    assert {ALPHA3[s] for s in {*storefronts.TERRITORIES.values(), *storefronts.ALIASES.values()}} <= set(table)


def test_the_list_is_apples_175_storefronts() -> None:
    assert len(storefronts.STOREFRONTS) == 175
    assert all(len(c) == 2 and c.isascii() and c.isupper() for c in storefronts.STOREFRONTS)
    # A sample from each of Apple's five regions, and the reviewers' live 200s.
    assert {"US", "CA", "GB", "DE", "FR", "JP", "CN", "BR", "ZA", "HK", "XK"} <= storefronts.STOREFRONTS


@pytest.mark.parametrize(
    "code",
    # The reviewers' live 400s, and regions Apple sells nothing in.
    ["PR", "RE", "GL", "AX", "GU", "IR", "KP", "CU", "SY", "AQ", "UK", "ZZ"],
)
def test_a_region_with_no_store_is_not_on_the_list(code: str) -> None:
    assert code not in storefronts.STOREFRONTS


def test_every_territory_maps_to_a_store_and_is_not_one() -> None:
    for region, store in {**storefronts.TERRITORIES, **storefronts.ALIASES}.items():
        assert region not in storefronts.STOREFRONTS, region
        assert store in storefronts.STOREFRONTS, (region, store)


@pytest.mark.parametrize(
    ("region", "store"),
    [
        ("PR", "US"), ("GU", "US"), ("VI", "US"), ("AS", "US"), ("MP", "US"),
        ("RE", "FR"), ("GP", "FR"), ("MQ", "FR"), ("GF", "FR"), ("YT", "FR"),
        ("PM", "FR"), ("BL", "FR"), ("MF", "FR"),
        ("GL", "DK"), ("FO", "DK"),
        ("AX", "FI"),
        ("MC", "FR"), ("LI", "CH"), ("SM", "IT"), ("VA", "IT"),
        ("GI", "GB"), ("IM", "GB"), ("JE", "GB"), ("GG", "GB"), ("SJ", "NO"),
        ("UK", "GB"),
    ],
)
def test_the_territory_map(region: str, store: str) -> None:
    assert storefronts.store_for(region) == store
    assert storefronts.store_for(region.lower()) == store


# --- what a person names ---------------------------------------------------------


@pytest.mark.parametrize(("given", "code"), [("GB", "GB"), ("gb", "GB"), (" de ", "DE")])
def test_a_store_named_is_upper_cased_with_no_note(given: str, code: str) -> None:
    assert storefronts.given(given, flag="--country") == storefronts.Store(code)


@pytest.mark.parametrize(("given", "code"), [("UK", "GB"), ("pr", "US"), ("RE", "FR")])
def test_a_mapped_code_is_accepted_with_a_note_naming_both(given: str, code: str) -> None:
    store = storefronts.given(given, flag="--country")
    assert store.code == code
    assert store.note is not None
    assert given.strip().upper() in store.note and code in store.note


@pytest.mark.parametrize("given", ["ZZ", "IR", "AQ", "kp"])
def test_a_code_with_no_store_is_refused_with_a_hint(given: str) -> None:
    with pytest.raises(ConfigError) as err:
        storefronts.given(given, flag="--store-country")
    assert given.upper() in err.value.message
    assert "App Store" in err.value.message
    assert err.value.hint and "--store-country" in err.value.hint


@pytest.mark.parametrize("given", ["", "G", "GBR", "1A", "Ü1", "g b"])
def test_a_value_that_is_not_two_letters_is_refused(given: str) -> None:
    with pytest.raises(ConfigError, match="two-letter country code"):
        storefronts.given(given, flag="--country")


# --- this Mac's region -------------------------------------------------------------


def test_a_region_with_its_own_store_is_that_store() -> None:
    assert storefronts.for_region("GB") == storefronts.Store("GB")


def test_a_territory_region_is_the_store_it_shops_in() -> None:
    assert storefronts.for_region("PR") == storefronts.Store("US")


def test_a_region_with_no_store_is_the_us_store_with_a_note() -> None:
    store = storefronts.for_region("IR")
    assert store.code == "US"
    assert store.note == storefronts.NO_STORE_NOTE == "Searching the US store: your region has no App Store."


def test_an_unreadable_region_is_the_us_store_as_before() -> None:
    assert storefronts.for_region(None) == storefronts.Store("US")


# --- the Mac app's copy ----------------------------------------------------------------


def _swift_codes(name: str) -> str:
    source = strip_swift_comments(SWIFT.read_text())
    match = re.search(rf"static let {name}[^=]*=\s*(\[.*?\])", source, re.S)
    assert match, f"Storefronts.{name} not found in {SWIFT}"
    return match.group(1)


def test_the_mac_app_holds_the_same_storefronts() -> None:
    codes = set(re.findall(r'"([A-Z]{2})"', _swift_codes("all")))
    assert codes == storefronts.STOREFRONTS


def test_the_mac_app_holds_the_same_territory_map() -> None:
    pairs = dict(re.findall(r'"([A-Z]{2})"\s*:\s*"([A-Z]{2})"', _swift_codes("territories")))
    assert pairs == {**storefronts.TERRITORIES, **storefronts.ALIASES}


def test_the_mac_app_says_the_same_no_store_note() -> None:
    source = strip_swift_comments(SWIFT.read_text())
    assert f'"{storefronts.NO_STORE_NOTE}"' in source
