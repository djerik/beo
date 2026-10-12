# -*- coding: utf-8 -*-
"""The Mozart facade's HTTP surface, guarded.

Two kinds of test here, and the difference matters.

The golden test says *that* an answer changed. The named tests below say *why
it matters*, and each one is a bug that actually happened against the real app.
A golden diff alone would tell you the JSON moved; it would not tell you that
the app is about to throw a NullReferenceException and show an empty source
list. So when a golden fails, read the named failures first.

Re-record the goldens with tests/capture_golden.py, deliberately, and read the
diff before committing it.
"""
from __future__ import annotations

import importlib
import json
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

os.environ.setdefault("BEO_SERIAL", "99000100")

from beo.mozart import facade  # noqa: E402

# A synthetic network. The recorded answers must never carry the addresses or
# serial numbers of anybody's actual products.
with open(os.path.join(HERE, "peers.synthetic.json"), encoding="utf-8") as _f:
    SYNTHETIC_PEERS = json.load(_f)
from beo_adapter.classics_adapter_profile import ClassicsAdapterProfile  # noqa: E402

SYNTHETIC_MAC = "00:00:5E:00:53:01"


@pytest.fixture(autouse=True, scope="module")
def pristine_facade():
    """Start from a facade nobody has driven.

    These tests assert what the product answers at rest. The facade keeps
    playback state in module-level dicts, so any other test that drives it,
    such as the Master Link bridge test, leaves power on and a disc loaded, and
    eight of the recorded answers would differ for a reason that has nothing to
    do with what is being tested.
    """
    importlib.reload(facade)
    # The facade reports the machine's own hardware address, which would make
    # three recorded answers differ on every machine the tests run on.
    facade.MAC = SYNTHETIC_MAC
    # Both of these are injected by run() on the real service, and a reload
    # puts the module back to its bare defaults, so they go in here together.
    facade._apply_profile(ClassicsAdapterProfile())
    facade.set_peers_provider(lambda: SYNTHETIC_PEERS)
    yield

GOLDEN = os.path.join(HERE, "golden")


def answer(path):
    """The route's answer, with the two sentinels named.

    The sentinels are looked up here rather than cached, because reloading the
    facade makes new ones and a cached identity map would quietly stop
    recognising them.
    """
    v = facade.GET_ROUTES[path]()
    if v is facade.NOT_FOUND:
        return "<NOT_FOUND: answer 404>"
    if v is facade.NO_CONTENT:
        return "<NO_CONTENT: answer 204>"
    return v


def load_index():
    with open(os.path.join(GOLDEN, "_index.json"), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------- the net
@pytest.mark.parametrize("path", sorted(load_index()))
def test_route_matches_its_golden(path):
    with open(os.path.join(GOLDEN, load_index()[path]), encoding="utf-8") as f:
        expected = json.load(f)
    assert answer(path) == expected, (
        "%s changed. If that was intended, re-record with capture_golden.py "
        "and read the diff; if not, the app is about to see something new." % path)


def test_every_route_has_a_golden():
    """A new route with no golden is a route nobody is watching."""
    missing = sorted(set(facade.GET_ROUTES) - set(load_index()))
    assert not missing, "no golden recorded for: %s" % missing


# ------------------------------------------------- what each scar protects
def test_source_type_is_an_object_not_a_bare_string():
    """A bare string left the source without a `type`, which crashed the
    app's source page."""
    for item in answer("/api/v1/playback/sources")["items"]:
        assert isinstance(item["type"], dict), item
        assert item["type"]["value"], item


def test_sources_use_id_and_name():
    """`sourceId` and `friendlyName` are not in the DTO. Using them produced
    sources whose every field was null."""
    for item in answer("/api/v1/playback/sources")["items"]:
        assert "id" in item and "name" in item, item
        assert "sourceId" not in item and "friendlyName" not in item, item


def test_remote_root_is_labelled_music():
    """The children's Subtitle is the PARENT's label, and the app keeps only
    candidates whose Subtitle is MUSIC. Calling the root "Sources" silently
    filtered every source out of the list."""
    menu = answer("/api/v1/remote")
    roots = [m for m in menu.values() if not m.get("parentMenuItemId")]
    assert roots, "no root item: the app walks the tree from the roots down"
    assert any(r["label"] == "MUSIC" for r in roots), [r["label"] for r in roots]


def test_every_remote_item_is_reachable_from_a_root():
    """The app walks roots, then firstChildMenuItemId, then the
    nextSiblingMenuItemId chain. Anything off that path is never seen."""
    menu = answer("/api/v1/remote")
    seen, stack = set(), [k for k, m in menu.items() if not m.get("parentMenuItemId")]
    while stack:
        k = stack.pop()
        if k in seen or k not in menu:
            continue
        seen.add(k)
        for link in ("firstChildMenuItemId", "nextSiblingMenuItemId"):
            nxt = menu[k].get(link)
            if nxt:
                stack.append(nxt)
    assert seen == set(menu), "unreachable menu items: %s" % sorted(set(menu) - seen)


def test_firmware_version_parses_the_way_dotnet_parses_it():
    """The app reads `fv` as a version number with no error handling, so a
    build-hash suffix took the whole app down."""
    assert re.fullmatch(r"\d+(\.\d+){1,3}", facade.FIRMWARE), facade.FIRMWARE


def test_product_info_carries_the_classics_adapter_item_number():
    """The app's Classics Adapter check compares the item number against 1454530.
    Without it the app draws a speaker, not a disc player."""
    info = json.dumps(answer("/api/v1/product/info"))
    assert "1454530" in info, info


def test_power_starts_in_standby():
    """It reported `on` at boot, so the app showed a machine that was switched
    off as playing."""
    assert answer("/api/v1/state/power") == {"value": "standby"}


def test_volume_reports_a_maximum_not_a_range():
    """The level is scaled against this field. Reporting it in the wrong one
    doubled the volume on a linked speaker."""
    v = facade.volume_state()
    assert "maximum" in v and "level" in v["maximum"], v
    assert v["maximum"]["level"] >= v["level"]["level"], v


def test_the_two_sentinels_are_distinct_objects():
    """404 and 204 are different answers, and an empty 200 is neither: it
    deserializes into a DTO whose fields are all null, which the app then
    dereferences without checking."""
    assert facade.NOT_FOUND is not facade.NO_CONTENT
    assert not isinstance(facade.NOT_FOUND, dict)
    assert not isinstance(facade.NO_CONTENT, dict)


def test_another_mozart_type_changes_type_number_but_not_the_adapter():
    """BEO_PRODUCT picks the product the app draws; the adapter check stays."""
    from beo.mozart.profile import mozart_type
    try:
        facade._apply_profile(ClassicsAdapterProfile(mozart_type("beosound-a5")))
        info = facade.product_info()
        assert info["production"]["typeNumber"] == "2541"
        assert info["production"]["itemNumber"] == "1454530"
        assert info["production"]["productDefaultName"] == "Beosound A5"
        assert "/api/v1/classics-adapter" in facade.GET_ROUTES
        assert facade.playback_sources()["items"][0]["id"].startswith("classicsAdapter://CD/")
    finally:
        facade._apply_profile(ClassicsAdapterProfile())
