"""Apple's App Store storefronts, and the store a region with none shops in.

Every country Graft reads or writes goes through here: the search's
`--country`, `--store-country`, a hand-written `store_country`, the country in
an App Store URL, and this Mac's region (`host.mac_region`). Apple answers a
country it has no store for with HTTP 400, so a code that is not a storefront
must never reach a request:

- a code that is one is used as it is;
- a territory with no store of its own (Puerto Rico, Réunion, Greenland) is
  the store its people use, and the alias `UK` is `GB`;
- anything else is refused, except for this Mac's region, which falls back to
  the US store with a note (`NO_STORE_NOTE`).

The Mac app's store picker holds the same list (`Graft/Graft/Models/
Storefronts.swift`), and tests/test_storefronts.py pins the two together.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ConfigError

#: Apple's App Store storefronts, as ISO 3166-1 alpha-2 codes: the 175
#: countries and regions of the table in App Store Connect Help, "App Store
#: localizations", https://developer.apple.com/help/app-store-connect/reference/app-store-localizations,
#: checked code by code on 2026-11-20 (tests/fixtures/apple_storefronts.tsv
#: is that table, and tests/test_storefronts.py compares the two). Grouped
#: here by Apple's regions. Kosovo is XK (Apple's XKS), which ISO has not
#: assigned. Apple's other table, of release times by country, lists 174:
#: it prints Bolivia as BO, and has one row ("NER Nigeria") for Niger and
#: Nigeria, both of which this one lists.
STOREFRONTS: frozenset[str] = frozenset(
    # Africa, the Middle East, and India
    "AE AM AO AZ BF BH BJ BW CD CG CI CM CV DZ EG GA GH GM GW IL IN IQ JO KE KW "
    "LB LR LY MA MG ML MR MU MW MZ NA NE NG OM QA RW SA SC SL SN ST SZ TD TN TZ "
    "UG YE ZA ZM ZW "
    # Asia Pacific
    "AF AU BN BT CN FJ FM HK ID JP KG KH KR KZ LA LK MM MN MO MV MY NP NR NZ PG "
    "PH PK PW SB SG TH TJ TM TO TW UZ VN VU "
    # Europe
    "AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FR GB GE GR HR HU IE IS IT LT LU "
    "LV MD ME MK MT NL NO PL PT RO RS RU SE SI SK TR UA XK "
    # Latin America and the Caribbean
    "AG AI AR BB BM BO BR BS BZ CL CO CR DM DO EC GD GT GY HN JM KN KY LC MS MX "
    "NI PA PE PY SR SV TC TT UY VC VE VG "
    # The United States and Canada
    "CA US".split()
)

#: Regions with no storefront of their own, and the store their people use.
#: Every one is a macOS region; none is on Apple's list, which is why Apple
#: answers it with HTTP 400 (the 2.0 review saw PR, GU, RE, GL and AX do so).
TERRITORIES: dict[str, str] = {
    # US territories
    "PR": "US", "GU": "US", "VI": "US", "AS": "US", "MP": "US",
    # France's overseas departments and collectivities
    "RE": "FR", "GP": "FR", "MQ": "FR", "GF": "FR", "YT": "FR",
    "PM": "FR", "BL": "FR", "MF": "FR",
    # Denmark's
    "GL": "DK", "FO": "DK",
    # Finland's
    "AX": "FI",
    # Neighbours' stores (combined fix check #24): Monaco shops in France's,
    # Liechtenstein in Switzerland's, San Marino and the Vatican in Italy's,
    # Gibraltar and the Crown Dependencies in the UK's, Svalbard and Jan
    # Mayen in Norway's. Andorra, the Dutch Caribbean and the French Pacific
    # are left out: which store their people use isn't settled here.
    "MC": "FR", "LI": "CH", "SM": "IT", "VA": "IT",
    "GI": "GB", "IM": "GB", "JE": "GB", "GG": "GB",
    "SJ": "NO",
}

#: Codes people write that are not ISO codes.
ALIASES: dict[str, str] = {"UK": "GB"}

DEFAULT = "US"

#: What a search says when this Mac's region has no store. The Mac app shows
#: the same sentence (`Storefronts.noStoreNote`).
NO_STORE_NOTE = f"Searching the {DEFAULT} store: your region has no App Store."


@dataclass(frozen=True)
class Store:
    """The store to ask, and what to tell the person about how it was chosen."""

    code: str
    #: Set when the store isn't the code given: a territory, an alias, or a
    #: region with no store. None when there is nothing to say.
    note: str | None = None


def _two_letters(text: str) -> bool:
    return len(text) == 2 and text.isascii() and text.isalpha()


def store_for(code: str) -> str | None:
    """The storefront `code` names or shops in, upper-case, or None when there is none."""
    upper = code.strip().upper()
    if upper in STOREFRONTS:
        return upper
    return TERRITORIES.get(upper) or ALIASES.get(upper)


def _mapped_note(given: str, store: str) -> str:
    if given in ALIASES:
        return f"{given} is written {store} on the App Store: using the {store} store"
    return f"{given} has no App Store of its own: using the {store} store, which serves it"


def given(value: str, *, flag: str) -> Store:
    """A store a person named (`--country`, `--store-country`), checked and mapped.

    A territory or an alias is accepted, with a note naming both codes. A code
    with no store is refused, since Apple would only answer it with HTTP 400.
    """
    text = value.strip()
    if not _two_letters(text):
        raise ConfigError(
            f"{flag} takes a two-letter country code, not {value!r}",
            hint=f"for example {flag} US, {flag} GB or {flag} DE",
        )
    upper = text.upper()
    store = store_for(upper)
    if store is None:
        raise ConfigError(
            f"there is no App Store for {upper}",
            hint=f"{flag} takes the code of a country that has one, such as US, GB or DE",
        )
    return Store(store) if store == upper else Store(store, note=_mapped_note(upper, store))


def for_region(region: str | None) -> Store:
    """The store this Mac's region shops in.

    None, a region that couldn't be read, is the US store with nothing to say,
    as it always was. A region with no store at all is the US store, with
    `NO_STORE_NOTE`.
    """
    if region is None:
        return Store(DEFAULT)
    store = store_for(region)
    return Store(store) if store else Store(DEFAULT, note=NO_STORE_NOTE)
