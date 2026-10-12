# -*- coding: utf-8 -*-
"""Record what the Mozart facade answers today, as golden files.

The facade is the component verified against the real app, and every field
name and status code in it matters to the app. This records the current
answers so a change can be checked on this machine instead of on the Pi.

Run it deliberately, never from the test suite:

    python apps/beo-adapter/tests/capture_golden.py

Re-record ONLY when a change to the facade is intended, and read the diff
before committing it. A golden file that changes without anyone meaning it is
the bug this exists to catch.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Runnable straight from a checkout, with nothing installed.
_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
for _p in ("packages/product", "packages/masterlink", "packages/networklink",
           "packages/mozart", "apps/beo-adapter"):
    sys.path.insert(0, os.path.join(_ROOT, *_p.split("/")))

# A fixed identity, so the goldens do not move with the machine they were taken on.
os.environ.setdefault("BEO_SERIAL", "99000100")

from beo.mozart import facade  # noqa: E402

# A synthetic network, injected the same way the service injects the real one.
# The recorded answers must never carry the addresses or serial numbers of
# anybody's actual products.
with open(os.path.join(HERE, "peers.synthetic.json"), encoding="utf-8") as _f:
    SYNTHETIC_PEERS = json.load(_f)
facade.set_peers_provider(lambda: SYNTHETIC_PEERS)
# A fixed hardware address too: the facade reports the machine's own.
facade.MAC = "00:00:5E:00:53:01"
from beo_adapter.classics_adapter_profile import ClassicsAdapterProfile  # noqa: E402

# The answers below are this product's, so the profile has to be in place
# before anything is asked. run() does this for the service.
facade._apply_profile(ClassicsAdapterProfile())

GOLDEN = os.path.join(HERE, "golden")

# The two sentinels are answers, not failures: the facade uses them to say
# "404" and "204". An empty 200 is the dangerous answer, so which routes are
# sentinels is itself worth freezing.
SENTINEL = {id(facade.NOT_FOUND): "<NOT_FOUND: answer 404>",
            id(facade.NO_CONTENT): "<NO_CONTENT: answer 204>"}


def encode(value):
    if id(value) in SENTINEL:
        return SENTINEL[id(value)]
    return value


def safe_name(path: str) -> str:
    return (path.strip("/") or "root").replace("/", "_") + ".json"


def main() -> int:
    os.makedirs(GOLDEN, exist_ok=True)
    taken, failed = 0, []
    index = {}
    for path in sorted(facade.GET_ROUTES):
        try:
            value = encode(facade.GET_ROUTES[path]())
            body = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False)
        except Exception as e:                      # noqa: BLE001 - report, do not hide
            failed.append((path, "%s: %s" % (type(e).__name__, e)))
            continue
        name = safe_name(path)
        with open(os.path.join(GOLDEN, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(body + "\n")
        index[path] = name
        taken += 1

    with open(os.path.join(GOLDEN, "_index.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(index, indent=2, sort_keys=True) + "\n")

    print("recorded %d of %d routes into %s" % (taken, len(facade.GET_ROUTES), GOLDEN))
    for path, err in failed:
        print("  FAILED %-44s %s" % (path, err))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
