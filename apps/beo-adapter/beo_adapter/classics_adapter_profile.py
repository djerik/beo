"""What the app sees in front of a classic machine: a Mozart product carrying a Classics Adapter.

It is the one disc player the app can draw, and it was made for a BeoSound
9000: six discs. A machine with one disc is shown with the same six, for want
of another. Which Mozart product is the MozartType passed in; the default, a
Beolab 8, is the only one ever verified against a real app. This is the only
part of the Mozart surface that knows a disc player is on the other end.
beo.mozart serves the protocol; this says what the protocol is describing.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from beo.mozart.profile import (REMOTE_ROOT_ID, MozartType, PlainSource, ProductProfile,
                                SourceEntry)


class ClassicsAdapterProfile(ProductProfile):
    """A Mozart product carrying a Classics Adapter, in front of a classic machine.

    Verified against the real app as a Beolab 8 only. The item number is what
    makes the app draw a disc player instead of a speaker, and the six discs
    are what the app expects a Classics Adapter to have. The adapter check
    does not look at the product type, so any MozartType can carry it.
    """

    item_number = "1454530"        # gates the adapter check - do not change
    # The product type in /BeoDevice and in the _beoremote record must agree.
    # With the TypeName for 3150 a listening product ignores us completely,
    # while this hardware code makes it open the connection itself. A peer
    # that does not recognise the product type does not find us worth talking
    # to, so consistency gives way to being known.
    product_type = "CA17"
    friendly = "Classics Adapter (emu)"
    root_source_value = "classicsAdapter"

    # The 9000's own scale: 00 to 72 on its display, two per step. With the
    # same top in the app, the slider and the display show the same number.
    volume_maximum = 72

    REMOTE_SOURCE_ID = "b0000000-0000-4000-8000-000000000002"
    DISC_COUNT = 6                 # the app requires exactly six
    PREFIX = "classicsAdapter://CD/"

    # The machine's sources other than the discs, by slot: the menu item's id
    # and the name it starts with. A slot that is not here has no menu item.
    PLAIN_SOURCES = {"RADIO": ("b0000000-0000-4000-8000-000000000003", "Radio")}

    adapter_serial = "12345678"    # the adapter's own serial, not the product's
    adapter_firmware = "1.0.5"
    adapter_protocol_version = 1

    def __init__(self, mozart: Optional[MozartType] = None,
                 sources: Iterable[str] = ("CD", "RADIO")) -> None:
        super().__init__(mozart)
        # The slots the machine has, in its own order. The discs are the
        # product itself and are always shown; the rest follow the machine.
        self.plain_sources = tuple(PlainSource(s, *self.PLAIN_SOURCES[s])
                                   for s in sources if s in self.PLAIN_SOURCES)

    def disc_uri(self, disc: int) -> str:
        return "%s%d" % (self.PREFIX, disc)

    def source_entries(self, jid: str) -> List[SourceEntry]:
        out = [(self.disc_uri(i), "CD %d" % i, "classicsAdapter")
               for i in range(1, self.DISC_COUNT + 1)]
        out.append(("linein:%s" % jid, "Line-In", "lineIn"))
        return out

    def content_items(self, start_with: str = "") -> Dict[str, dict]:
        out = {}
        for i in range(1, self.DISC_COUNT + 1):
            uri = self.disc_uri(i)
            if start_with and not uri.startswith(start_with):
                continue
            out[uri] = {
                "id": uri,
                "contentUri": uri,
                "label": "CD %d" % i,
                "originalLabel": "CD %d" % i,
                "available": True,
                "categories": ["music"],
                "source": {"id": "classicsAdapter", "value": "classicsAdapter"},
            }
        return out

    def remote_items(self, labels: Dict[str, str]) -> Dict[str, dict]:
        items = super().remote_items(labels)
        label = labels.get(self.REMOTE_SOURCE_ID, "Classics Adapter")
        items[REMOTE_ROOT_ID]["firstChildMenuItemId"] = self.REMOTE_SOURCE_ID
        items[self.REMOTE_SOURCE_ID] = {
            "id": self.REMOTE_SOURCE_ID,
            # Starts as something other than "CD" so the adapter check takes
            # its rename path; afterwards the new label is remembered.
            "label": label,
            "available": True,
            "fixed": False,
            "disabled": False,
            "dynamicList": "none",
            "parentMenuItemId": REMOTE_ROOT_ID,
            # Exactly "classicsAdapter": the check matches this by equality,
            # while the source whitelist matches it by prefix.
            "content": {"contentUri": "classicsAdapter", "label": label,
                        "categories": ["music"],
                        "source": {"value": "classicsAdapter"}},
            # What runs when the source is picked. Without it the cell is inert.
            "actionList": [{"type": "triggerContent",
                            "contentId": "classicsAdapter"}],
        }
        previous = self.REMOTE_SOURCE_ID
        for _slot, item_id, name in self.plain_sources:
            name = labels.get(item_id, name)
            items[previous]["nextSiblingMenuItemId"] = item_id
            items[item_id] = {
                "id": item_id,
                "label": name,
                "available": True,
                "fixed": False,
                "disabled": False,
                "dynamicList": "none",
                "parentMenuItemId": REMOTE_ROOT_ID,
                # The app lists a source only when its content URI starts with
                # a name on its whitelist, and a tuner is not on it. Line-in
                # is: a plain source with a name and nothing to browse.
                "content": {"contentUri": "lineIn", "label": name,
                            "categories": ["music"],
                            "source": {"value": "lineIn"}},
                "actionList": [{"type": "triggerContent", "contentId": "lineIn"}],
            }
            previous = item_id
        return items

    def extra_routes(self, mac: str) -> Dict[str, Callable[[], dict]]:
        def classics_adapter() -> dict:
            """GET api/v1/classics-adapter -> MozartClassicsAdapter

            The field names and the state enum are the app's own:
            booting, ready, firmwareUpdate, error. Only a 2xx matters for
            detection, but the body is well formed so deserialization cannot
            fail.
            """
            return {
                "firmwareVersion": self.adapter_firmware,
                "protocolVersion": self.adapter_protocol_version,
                "serialNumber": self.adapter_serial,
                "state": "ready",
                "wifiMacAddress": mac,
            }

        return {"/api/v1/classics-adapter": classics_adapter}
