"""BeolinkOut: a listener is looked up when the app asks, not when we started."""
from __future__ import annotations

import threading

import pytest

from beo.mozart import facade
from beo_adapter.streaming import BeolinkOut
from beo.networklink.fps import MonotonicClock
from beo.networklink.sources import SOURCES

OWN = "3150.1454530.99000100@products.bang-olufsen.com"
SPEAKER = {"jid": "2730.1200322.99000002@products.bang-olufsen.com",
           "friendlyName": "Synthetic Listener", "ipAddress": "192.0.2.10",
           "productType": "CA17", "audioTransport": "wireless"}


@pytest.fixture
def directory():
    """A peer directory that starts empty, as mDNS does."""
    heard = []
    facade.set_peers_provider(lambda: heard)
    yield heard
    facade.set_peers_provider(None)


@pytest.fixture
def out(directory):
    """Built while the directory is still empty — the order the service runs in."""
    o = BeolinkOut(OWN, "Synthetic 9000", facade.peers(), tone=True, clock=MonotonicClock())
    o.expanded = []
    o.done = threading.Event()

    def expand(peer, source, audio):
        o.expanded.append(peer)
        o.done.set()
        return object()
    o.role.expand = expand
    o.role.unexpand = lambda peer: True
    return o


def test_listener_heard_after_startup_can_be_expanded_to(out, directory):
    directory.append(SPEAKER)
    assert out.add(SPEAKER["jid"]) is True
    assert out.done.wait(2)
    assert (out.expanded[0].jid, out.expanded[0].ip) == (SPEAKER["jid"], "192.0.2.10")


def test_the_catalogue_is_the_sources_it_is_built_with(directory):
    o = BeolinkOut(OWN, "Synthetic 9000", facade.peers(), tone=True, clock=MonotonicClock(),
                   sources=(SOURCES["RADIO"], SOURCES["CD"]))
    assert [s.key for s in o.role.catalog.sources] == ["RADIO", "CD"]


def test_what_is_streamed_is_the_source_the_machine_is_on(directory, monkeypatch):
    """A listener is told what plays, so a radio described as the CD shows
    the wrong source on every speaker in the group."""
    o = BeolinkOut(OWN, "Synthetic 9000", facade.peers(), tone=True, clock=MonotonicClock(),
                   sources=(SOURCES["CD"], SOURCES["RADIO"]))
    monkeypatch.setattr(facade, "_active", dict(facade._active, source=None))
    assert (o.source().key, o.source().name) == ("CD", "Synthetic 9000")
    facade._active["source"] = "RADIO"
    assert o.source().key == "RADIO"
    # with nothing behind the product, the one input a speaker has
    assert BeolinkOut(OWN, "Synthetic", facade.peers(), tone=True,
                      clock=MonotonicClock()).source().key == "linein"


def test_listener_never_heard_is_refused(out):
    assert out.add(SPEAKER["jid"]) is False
    assert out.expanded == []


def test_listener_gone_from_mdns_can_still_be_removed(out, directory):
    directory.append(SPEAKER)
    assert out.add(SPEAKER["jid"]) is True
    assert out.done.wait(2)
    directory.clear()
    assert out.remove(SPEAKER["jid"]) is True


def test_listeners_are_told_our_disc_and_track_not_a_radio_station(out, monkeypatch):
    """The role's default now-playing is a radio station; without an
    override every listener describes our CD as that station."""
    monkeypatch.setattr(facade, "_active", {"cd": 2, "track": 5, "progress": 0, "state": "started"})
    out.progress()
    np = out.role.now_playing
    assert np.name == "Synthetic 9000"
    assert np.live_description == "CD 2 · track 5"
    assert np.station_id == "" and np.image_url == "" and np.genre == ""
    facade._active["track"] = 6
    out.progress()
    assert out.role.now_playing.live_description == "CD 2 · track 6"


# ------------------------------------------- the app hears what really happened
@pytest.fixture
def app(out, monkeypatch):
    """The facade with this BeolinkOut behind it, wired the way run() wires it,
    and everything it tells the app collected instead of sent."""
    events = []
    monkeypatch.setattr(facade.notifier, "send", lambda name, data: events.append((name, data)))
    monkeypatch.setattr(facade, "request_ml_audio", lambda: True)
    monkeypatch.setattr(facade, "_beolink_listeners", [])
    monkeypatch.setattr(facade, "_beolink_out", out)
    out.reported = threading.Event()

    def result(jid, joined):
        facade._expand_result(jid, joined)
        out.reported.set()
    out.on_expand_result = result

    def expand(jid):
        h = object.__new__(facade.Handler)
        h.path = "/api/v1/beolink/expand/" + jid.replace("@", "%40")
        h._read_body = lambda: b""
        sent = []
        h._send = lambda obj, code=200: sent.append(code)
        h._write_like("POST")
        return sent[-1]

    app = type("App", (), {})()
    app.expand, app.events, app.out = expand, events, out
    app.joined = lambda: [d["jid"] for n, d in events
                          if n == "WebSocketEventBeolinkJoinResult" and d["status"] == "joined"]
    return app


def test_unreachable_listener_is_a_404_and_never_joined(app):
    """The app's client reads 404 as "Remote device not found" and reports the
    failure at once. Answering 202 and "joined" leaves a silent speaker in the
    group."""
    assert app.expand(SPEAKER["jid"]) == 404
    assert app.joined() == []
    assert facade._beolink_listeners == []


def test_joined_is_sent_when_the_speaker_plays_not_when_the_app_asks(app, directory):
    directory.append(SPEAKER)
    release = threading.Event()
    played = app.out.role.expand
    app.out.role.expand = lambda *a: (release.wait(2), played(*a))[1]

    assert app.expand(SPEAKER["jid"]) == 202
    assert app.joined() == [] and facade._beolink_listeners == []
    release.set()
    assert app.out.reported.wait(2)
    assert app.joined() == [SPEAKER["jid"]]
    assert facade._beolink_listeners == [SPEAKER["jid"]]


def test_failed_expand_is_never_reported_as_joined(app, directory):
    """No event at all: the app's own 25 s timer reports the failure."""
    directory.append(SPEAKER)
    app.out.role.expand = lambda *a: None

    assert app.expand(SPEAKER["jid"]) == 202
    assert app.out.reported.wait(2)
    assert app.joined() == []
    assert facade._beolink_listeners == []


def test_without_a_beolink_side_the_app_flow_still_completes(app, monkeypatch):
    monkeypatch.setattr(facade, "_beolink_out", None)
    assert app.expand(SPEAKER["jid"]) == 202
    assert app.joined() == [SPEAKER["jid"]]
