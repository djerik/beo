"""The identities, clock and station pinned in tests/golden/*.xml.

The golden files are the wire forms the library emits for one source under
these fixed identities; a difference is a wire change and must be meant.
Run this module to re-pin them after a deliberate change:

    PYTHONPATH=packages/product:packages/networklink python packages/networklink/tests/golden_identities.py
"""
from __future__ import annotations

import os

OUR_JID = "2730.1200322.90000001@products.bang-olufsen.com"
OUR_NAME = "Reference Source"
OUR_MAC = "00:00:5E:00:53:01"
PEER_JID = "2730.1200322.90000010@products.bang-olufsen.com"
PEER_IP = "203.0.113.10"
LAST_USED = "2026-09-21T23:38:55.000000"
VOLUME = (35, 0)

# Synthetic station: every field filled so the golden shows each one.
STATION = dict(name="Test Station", genre="Test genre", country="", languages="",
               image_url="http://203.0.113.1/station.png", image_size="large",
               image_mediatype="image/png", live_description="/ Artist - Title",
               station_id="1000000000000001")


if __name__ == "__main__":
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import test_status_builder as t
    golden = os.path.join(here, "golden")
    os.makedirs(golden, exist_ok=True)
    for key in ("beoradio", "CD"):
        for name, xml in t.library(key).items():
            p = os.path.join(golden, "%s-%s.xml" % (name, key))
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(xml)
            print("wrote", p, len(xml))
