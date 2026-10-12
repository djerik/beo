"""What the app should think this product is.

The facade serves the Mozart API; the profile says what is on the other end
of it. Nothing here knows how to answer an HTTP request.

A profile is built on a MozartType, one of the product types compiled into
the app (docs/app-protocol.md §2). The type decides what the app draws; the
profile adds what the product carries: item number, sources, menu, routes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

# (source id, display name, source type)
SourceEntry = Tuple[str, str, str]


class PlainSource(NamedTuple):
    """A source the product has that is a name and nothing to browse: the
    slot the machine knows it by, its remote-menu item and what it is called."""

    slot: str
    item_id: str
    name: str


# Remote-menu ids must be GUIDs (docs/app-protocol.md §5). This one is ours,
# fixed so the app's renamed labels survive a restart.
REMOTE_ROOT_ID = "b0000000-0000-4000-8000-000000000001"


@dataclass(frozen=True)
class MozartType:
    """One of the Mozart product types compiled into the app.

    The app resolves a discovered product by its type number against a table
    of exactly these; any other number is discarded as an unknown product.
    `type_name` is the app's own name for the type, `asks_for` the endpoints
    its capability set makes the app call beyond the common ones
    (docs/app-protocol.md §2).
    """

    key: str
    type_number: str
    type_name: str
    display_name: str
    asks_for: str
    verified: bool = False


# The Mozart products Bang & Olufsen lists as such, keyed by the name the
# configuration uses. Type numbers are the first public one in the app's
# catalogue for that type. Left out: the Google Assistant variants (same
# product, extra setup flow) (docs/app-protocol.md §2).
MOZART_TYPES: Dict[str, MozartType] = {t.key: t for t in [
    MozartType("beolab-8", "3150", "SEOUL", "Beolab 8",
               "listening modes, tone touch, proximity, speaker pairing, "
               "Powerlink settings, WiSA, logo light (firmware 5.1+)",
               verified=True),
    MozartType("beosound-a5", "2541", "BEOSOUND_A5", "Beosound A5",
               "listening modes, tone touch, battery, speaker pairing"),
    MozartType("beosound-2", "3091", "BEOSOUND_2_3GEN", "Beosound 2 3rd gen",
               "proximity, speaker pairing; listening modes from firmware 3.4"),
    MozartType("beosound-a9", "3090", "BEOSOUND_A9_5GEN", "Beosound A9 5th gen",
               "speaker pairing; listening modes from firmware 3.4"),
    MozartType("beosound-balance", "6471", "PARIS", "Beosound Balance",
               "directivity, room compensation, tone touch, proximity, speaker "
               "pairing; listening modes from firmware 3.4"),
    MozartType("beosound-level", "6476", "BEOSOUND_LEVEL", "Beosound Level",
               "placement, battery, proximity, speaker pairing; listening modes "
               "from firmware 3.4"),
    MozartType("beosound-emerge", "2738", "TOKYO", "Beosound Emerge",
               "speaker pairing; listening modes from firmware 3.4"),
    MozartType("beolab-28", "4545", "s58", "Beolab 28",
               "curtain position, listening modes, tone touch, proximity, "
               "Powerlink settings, WiSA, speaker pairing"),
    MozartType("beoconnect-core", "3058", "EZ4", "Beoconnect Core",
               "listening modes, Powerlink and speaker outputs, Beosound Shape, "
               "TV source configuration"),
    MozartType("beosound-theatre", "3100", "CA44", "Beosound Theatre",
               "a TV product: HDMI, speaker groups, Powerlink, external speakers, "
               "listening positions"),
    MozartType("beosound-premiere", "3120", "CA33", "Beosound Premiere",
               "a TV product: HDMI, speaker groups, Powerlink, LED bar"),
]}

DEFAULT_TYPE = "beolab-8"


def mozart_type(key: Optional[str]) -> MozartType:
    """The Mozart type named by `key` (case and spacing forgiven), or the default.

    Raises ValueError naming every valid key, so a configuration typo says
    what it should have been instead of becoming an unknown product.
    """
    if not key or not key.strip():
        return MOZART_TYPES[DEFAULT_TYPE]
    norm = key.strip().lower().replace(" ", "-").replace("_", "-")
    try:
        return MOZART_TYPES[norm]
    except KeyError:
        raise ValueError("unknown Mozart product %r; one of: %s"
                         % (key, ", ".join(MOZART_TYPES))) from None


class ProductProfile:
    """A plain speaker: one input, nothing to browse.

    The base every other profile narrows. Subclasses override the class
    attributes below and the methods that describe sources, content and
    routes. The MozartType given at construction sets `type_number`,
    `product_default_name`, and the defaults for `product_type`, `friendly`
    and `host_prefix`.
    """

    # Reported as production.itemNumber and in the TXT record. The app does
    # not check it for Mozart types except in the Classics Adapter check, which
    # compares it with the adapter's number (docs/app-protocol.md §3). This
    # one is a Beolab 8's; a profile for an adapter sets its own.
    item_number = "1200322"

    # Reported as fv. Must parse as a .NET System.Version, and gates features
    # by version: listening modes from 3.4 on most speakers, logo light from
    # 5.1 (docs/app-protocol.md §1 and §2).
    firmware = "1.9.0.30523"

    # None means: derive from the MozartType. `product_type` is the hardware
    # code other products see in /BeoDevice and _beoremote (§2); `friendly`
    # the name in the app; `host_prefix` the mDNS host name's first part.
    product_type: Optional[str] = None
    friendly: Optional[str] = None
    host_prefix: Optional[str] = None

    # The top of the volume scale, and with it the range of the app's slider.
    volume_maximum = 100

    # The root of the remote menu. Its label becomes every child's Subtitle,
    # and the app keeps only candidates whose Subtitle is MUSIC (§5).
    root_label = "MUSIC"
    root_source_value = "lineIn"

    # The sources that can be picked beside what the product browses. A
    # speaker has none.
    plain_sources: Tuple[PlainSource, ...] = ()

    def __init__(self, mozart: Optional[MozartType] = None) -> None:
        self.mozart = mozart or MOZART_TYPES[DEFAULT_TYPE]
        self.type_number = self.mozart.type_number
        self.product_default_name = self.mozart.display_name
        if self.product_type is None:
            self.product_type = self.mozart.type_name
        if self.friendly is None:
            self.friendly = "%s (emulated)" % self.mozart.display_name
        if self.host_prefix is None:
            self.host_prefix = self.mozart.display_name.replace(" ", "-")

    def source_entries(self, jid: str) -> List[SourceEntry]:
        """What `api/v1/playback/sources` lists."""
        return [("linein:%s" % jid, "Line-In", "lineIn")]

    def content_items(self, start_with: str = "") -> Dict[str, dict]:
        """What `api/v1/content` offers to browse. A speaker browses nothing."""
        return {}

    def remote_items(self, labels: Dict[str, str]) -> Dict[str, dict]:
        """The menu tree the app walks: roots, then first child, then siblings.

        ``labels`` carries the names the app has renamed items to, which a real
        product persists. Without that the rename runs on every connection.
        """
        return {
            REMOTE_ROOT_ID: {
                "id": REMOTE_ROOT_ID,
                "label": self.root_label,
                "available": True,
                "fixed": True,
                "disabled": False,
                "dynamicList": "none",
                "content": {"contentUri": "", "label": self.root_label,
                            "categories": ["music"],
                            "source": {"value": self.root_source_value}},
            },
        }

    def extra_routes(self, mac: str) -> Dict[str, Callable[[], dict]]:
        """GET routes only this product answers. A speaker adds none."""
        return {}
