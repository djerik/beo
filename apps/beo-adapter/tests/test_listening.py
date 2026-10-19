"""The listening side: the machine is asked before the sound card is opened,
a stream the machine does not take is not played into it, and an output of
the product's own plays without asking anybody."""
import threading

import pytest

from beo.networklink.audio import AlsaPlayback
from beo.networklink.fps import FpsBlock
from beo.networklink.sources import Catalog, STANDARD_SOURCES
from beo.networklink.roles.listener import Listening
from beo.product.identity import ProductIdentity
from beo_adapter import listening
from beo_adapter.listening import (BeolinkIn, Outputs, describe, from_configuration, now_playing,
                                   playback_state, playing_lines)

IDENT = ProductIdentity.from_jid("3150.1454530.90000001", "Listener", "00:00:5E:00:53:01")

STATUS = ("<FullStatus><status><item><type>SOURCE</type><kind>source</kind><data>"
          "<primaryExperience><source><id>beoradio:x</id><friendlyName>B&amp;O Radio</friendlyName>"
          "<_capabilities><item><type>NOW_PLAYING_ENDED</type><kind>playing</kind></item></_capabilities>"
          "</source></primaryExperience></data></item>"
          "<item><type>NOW_PLAYING_NET_RADIO</type><kind>playing</kind><data><name>Test Station</name>"
          "</data></item></status></FullStatus>")


class Machine:
    def __init__(self, takes):
        self.takes, self.asked = takes, []
        self.on_network_source_left = None

    def network_source_started(self, title, secondary=""):
        self.asked.append((title, secondary))
        return self.takes

    def network_source_playing(self, lines):
        self.asked.append(lines)

    def network_source_stopped(self):
        self.asked.append("stopped")


class Playback:
    made = []

    def __init__(self, device, now_ns=None):
        self.device, self.stopped, self.written = device, False, []
        Playback.made.append(self)

    def start(self):
        return self

    def write(self, block):
        self.written.append(block)

    def stop(self):
        self.stopped = True


class Speaker:
    """An output of the product's own, as the listening side sees it."""

    def open(self, now_ns=None):
        return Playback("own")


class Inline:
    """A thread that has run by the time start() returns."""

    def __init__(self, target, args=(), **_):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def playback(monkeypatch):
    Playback.made = []
    monkeypatch.setattr(listening, "AlsaPlayback", Playback)
    monkeypatch.setattr(listening.threading, "Thread", Inline)
    return Playback


def listening_to(status=STATUS):
    cur = Listening(object(), "beoradio", "2730.1200322.90000010@products.bang-olufsen.com")
    cur.status = status
    return cur


def started(b, cur):
    """The stream has started, and the machine has been asked."""
    b.role.current = cur
    b._started(cur)


def played_on(b):
    return [p.device for p in b.outputs._open]


def test_the_title_is_what_plays_and_the_source_is_the_second_line():
    assert describe(STATUS, "beoradio") == ("Test Station", "B&O Radio")
    assert describe("", "beoradio") == ("beoradio", "")


def test_the_machine_is_asked_first_and_then_the_card_is_opened(playback):
    machine = Machine(takes=True)
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=machine, device="hw:9,9")
    cur = listening_to()
    started(b, cur)
    assert machine.asked == [("Test Station", "B&O Radio"), ["Test Station"]]
    assert played_on(b) == ["hw:9,9"] and b.role.sink is b.outputs
    b.role.sink.write("a block")
    assert playback.made[0].written == ["a block"]
    b.role.current = None
    b._stopped(cur)
    assert playback.made[0].stopped and b.role.sink is None
    assert machine.asked[-1] == "stopped"


def notification(live):
    return ("<message><body><_notification><NOTIFICATION><type>NOW_PLAYING_NET_RADIO</type>"
            "<kind>playing</kind><data><name>Test Station</name><genre>Dance</genre>"
            "<liveDescription>%s</liveDescription></data></NOTIFICATION></_notification></body></message>" % live)


def test_what_plays_is_the_station_then_artist_and_title():
    assert playing_lines(notification("ARTIST - A TITLE")) == ["Test Station", "ARTIST", "A TITLE"]
    assert playing_lines(notification("")) == ["Test Station"]
    assert playing_lines("<NOTIFICATION><type>PROGRESS_INFORMATION</type></NOTIFICATION>") == []


def test_the_machine_is_told_when_what_plays_changes(playback):
    machine = Machine(takes=True)
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=machine)
    started(b, listening_to())
    b._notified("NOW_PLAYING_NET_RADIO", notification("ARTIST - A TITLE"))
    b._notified("NOW_PLAYING_NET_RADIO", notification("ARTIST - A TITLE"))
    b._notified("PROGRESS_INFORMATION", "<NOTIFICATION><type>PROGRESS_INFORMATION</type></NOTIFICATION>")
    assert machine.asked[2:] == [["Test Station", "ARTIST", "A TITLE"]]


def test_the_machine_is_not_told_the_stream_ended_while_another_takes_its_place(playback):
    machine = Machine(takes=True)
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=machine)
    old = listening_to()
    started(b, old)
    b.role.current = listening_to()
    b._stopped(old)
    assert "stopped" not in machine.asked


def test_a_stream_the_machine_does_not_take_is_not_played(playback):
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=Machine(takes=False))
    started(b, listening_to())
    assert playback.made == [] and played_on(b) == []


def test_a_stream_that_ended_while_the_machine_was_asked_is_not_played(playback):
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=Machine(takes=True))
    cur = listening_to()
    b.role.current = None
    b._started(cur)
    assert playback.made == []


def test_an_output_of_its_own_plays_with_nothing_behind_the_product(playback):
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), speaker=Speaker())
    cur = listening_to()
    started(b, cur)
    assert played_on(b) == ["own"]
    b.role.current = None
    b._stopped(cur)
    assert playback.made[0].stopped and b.role.sink is None


def test_the_machine_and_an_output_of_its_own_play_the_same_blocks(playback):
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=Machine(takes=True),
                  device="hw:9,9", speaker=Speaker())
    started(b, listening_to())
    assert played_on(b) == ["own", "hw:9,9"]
    b.role.sink.write("a block")
    assert [p.written for p in playback.made] == [["a block"], ["a block"]]


def test_its_own_output_plays_a_stream_the_machine_does_not_take(playback):
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=Machine(takes=False), speaker=Speaker())
    started(b, listening_to())
    assert played_on(b) == ["own"]


def test_an_output_opened_after_the_stream_ended_is_stopped_again(playback):
    outputs = Outputs()
    outputs.stop()
    late = Playback("late")
    BeolinkIn._add(outputs, late)
    assert late.stopped and outputs._open == ()


def test_the_machine_turning_away_makes_us_leave():
    machine = Machine(takes=True)
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=machine)
    assert machine.on_network_source_left == b.leave


def test_configuration_values():
    assert from_configuration("") is None and from_configuration("off") is None
    assert from_configuration("1") == []
    assert from_configuration("203.0.113.1, 203.0.113.2") == ["203.0.113.1", "203.0.113.2"]


def block(pts_ns):
    return FpsBlock(1, b"", pts_ns, 0, b"\x00\x00\x00\x00", 0.0, "203.0.113.10")


def test_a_block_is_held_until_its_time_and_dropped_when_too_old():
    now = [10_000_000_000]
    p = AlsaPlayback(now_ns=lambda: now[0], latency_ms=0)
    waited = []
    p._stop = threading.Event()
    p._stop.wait = lambda s: waited.append(round(s, 3))
    assert p._due(block(10_500_000_000).device_ns) and waited == [0.5]
    assert p._due(block(9_900_000_000).device_ns) and waited == [0.5]      # a little late: played
    assert not p._due(block(9_000_000_000).device_ns)                      # a second late: dropped
    assert p._due(block(90_000_000_000).device_ns) and waited == [0.5]     # another clock: not held
    assert AlsaPlayback()._due(0)                                          # no clock: as it arrives


class Browsers:
    """The page's stream, as the listening side sees it."""

    def open(self, now_ns=None):
        return Playback("web")


def test_the_browsers_hear_the_stream_beside_the_cards(playback):
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=Machine(takes=True),
                  device="hw:9,9", speaker=Speaker(), web=Browsers())
    cur = listening_to()
    started(b, cur)
    assert played_on(b) == ["own", "web", "hw:9,9"]
    b.role.current = None
    b._stopped(cur)
    assert all(p.stopped for p in playback.made)


def test_how_a_join_went_is_kept_for_the_page(monkeypatch):
    monkeypatch.setattr(listening.threading, "Thread", Inline)
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES))
    monkeypatch.setattr(listening.facade, "peer_by_jid", lambda jid: {})
    b.join("nobody")
    assert b.last_join == {"jid": "nobody", "state": "failed"}
    monkeypatch.setattr(listening.facade, "peer_by_jid", lambda jid: {"ipAddress": "203.0.113.5"})
    monkeypatch.setattr(b.role, "join", lambda ip, jid: True)
    b.join("somebody")
    assert b.last_join == {"jid": "somebody", "state": "joined"}


def test_what_plays_at_the_source_is_kept_for_the_page(playback):
    assert now_playing(notification("ARTIST - A TITLE")) == {
        "station": "Test Station", "artist": "ARTIST", "title": "A TITLE", "image": ""}
    assert now_playing("<NOTIFICATION><type>PROGRESS_INFORMATION</type></NOTIFICATION>") is None
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES))
    cur = listening_to()
    started(b, cur)
    assert b.playing["station"] == "Test Station" and b.source_state == "play"
    b._notified("PROGRESS_INFORMATION", "<NOTIFICATION><type>PROGRESS_INFORMATION</type>"
                "<kind>playing</kind><data><state>pause</state></data></NOTIFICATION>")
    assert b.source_state == "pause"
    b.role.current = None
    b._stopped(cur)
    assert b.playing is None


def test_a_source_says_whether_it_plays():
    xml = ("<type>PROGRESS_INFORMATION</type><kind>playing</kind><data><state>pause</state>"
           "<position>3</position></data>")
    assert playback_state(xml) == "pause" and playback_state("<x/>") == ""


def test_a_source_that_pauses_is_heard_as_silence(playback):
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), speaker=Speaker())
    started(b, listening_to())
    progress = ("<NOTIFICATION><type>PROGRESS_INFORMATION</type><kind>playing</kind>"
                "<data><state>%s</state></data></NOTIFICATION>")
    b._notified("PROGRESS_INFORMATION", progress % "pause")
    b.role.sink.write(FpsBlock(1, b"", 0, 0, b"\x01\x02\x03\x04", 0.0, "203.0.113.10"))
    b._notified("PROGRESS_INFORMATION", progress % "play")
    b.role.sink.write(FpsBlock(2, b"", 0, 0, b"\x01\x02\x03\x04", 0.0, "203.0.113.10"))
    assert [blk.pcm for blk in playback.made[0].written] == [bytes(4), b"\x01\x02\x03\x04"]


class BorrowSession:
    def __init__(self):
        self.handlers, self.peer_jid, self.peer_ip = [], "2730.1200322.90000010@products.bang-olufsen.com", "203.0.113.9"

    def on(self, h, first=False):
        self.handlers.insert(0, h) if first else self.handlers.append(h)

    def deliver(self, xml):
        from beo.networklink.beocore import Stanza
        st = Stanza(xml)
        return [h(self, st) for h in self.handlers]


class BorrowSide:
    def __init__(self):
        self.asked = []

    def accept(self, session, stanza):
        self.asked.append(stanza.tag("sourceId"))
        return True


def test_a_borrowers_connect_goes_to_the_source_side_and_a_connectrequest_does_not():
    side = BorrowSide()
    b = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=None, source=side)
    session = BorrowSession()
    b._serve(session)
    assert len(session.handlers) == 2
    jid = session.peer_jid
    session.deliver(f"<iq id='1' type='set' from='{jid}'><Connect xmlns='beocore.player.1'>"
                    "<sourceId>CD</sourceId><connectionInfo>FPS://203.0.113.9</connectionInfo></Connect></iq>")
    assert side.asked == ["CD"]
    assert session.handlers[0](session, __import__("beo.networklink.beocore", fromlist=["Stanza"]).Stanza(
        f"<iq id='2' type='set' from='{jid}'><ConnectRequest xmlns='beocore.player.1'>"
        "<sourceId>CD</sourceId></ConnectRequest></iq>")) is False
    assert side.asked == ["CD"]
    plain = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=None)
    plain._serve(session := BorrowSession())
    assert len(session.handlers) == 1


def test_the_entry_offers_the_sources_the_product_can_play_to_a_listener():
    from beo.networklink.sources import SOURCES
    b = BeolinkIn(IDENT, Catalog([SOURCES["CD"], SOURCES["RADIO"]]), machine=None)
    assert [s.key for s in b.offered] == ["CD", "RADIO"]
    plain = BeolinkIn(IDENT, Catalog(STANDARD_SOURCES), machine=None)
    assert [s.key for s in plain.offered] == ["linein", "beoradio"]
