"""MlBridge: the machine's status reaches the facade, app commands reach the bus."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beo.masterlink.bus import MlTransport                      # noqa: E402
from beo.masterlink.codec import Telegram, checksum, decode_beo4, Beo4   # noqa: E402
from beo.mozart import facade                           # noqa: E402
from beo.masterlink.profiles import BS9000                        # noqa: E402
from beo_adapter.ml_bridge import MlBridge                        # noqa: E402
from beo_adapter.classics_adapter_profile import ClassicsAdapterProfile     # noqa: E402

import pytest                                              # noqa: E402


@pytest.fixture(autouse=True)
def classics_adapter_product():
    """The facade defaults to a plain speaker, which has no discs.

    A speaker would reject disc 3 as out of range, and rightly so. The service
    sets the product in run(); a test that drives disc state has to do the same
    rather than rely on whatever another test left behind.
    """
    facade._apply_profile(ClassicsAdapterProfile())

STATUS = bytearray.fromhex("83c10114008d008715048d0100000008010100ffff0201ffffffffffffffff1700")


def status(disc, track, state, mask):
    raw = bytearray(STATUS)
    raw[15], raw[17], raw[19], raw[21] = mask, disc, (0xFF if track is None else track), state
    raw[-2] = checksum(bytes(raw[:-2]))
    return Telegram.parse(bytes(raw))


class Fake(MlTransport):
    def __init__(self):
        self.sent = []
        self._on = None

    def start(self, on):
        self._on = on

    def inject(self, tg, delay=0.01):
        threading.Timer(delay, self._on, args=(tg,)).start()

    def send(self, tg):
        self.sent.append(tg)
        k = decode_beo4(tg)
        if k and k[0] == Beo4.STEP_UP:
            self.inject(status(3, 5, 0x02, 0x08))


def wait(pred, t=2.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_status_reaches_facade_and_skip_reaches_bus():
    fake = Fake()
    br = MlBridge(BS9000, transport=fake, source_center=False)
    br.start()
    fake.inject(status(3, 4, 0x02, 0x2A))
    assert wait(lambda: facade._active["cd"] == 3 and facade._active["track"] == 4)
    assert facade._active["state"] == "started"
    br.press("skip")
    assert wait(lambda: facade._active["track"] == 5)
    assert any(decode_beo4(t) and decode_beo4(t)[0] == Beo4.STEP_UP for t in fake.sent)
    br.close()


def test_request_audio_sends_link_room_request():
    fake = Fake()
    br = MlBridge(BS9000, transport=fake, source_center=False)
    br.start()
    assert br.request_audio() is True
    assert wait(lambda: any(t.body.hex() == "c106010b000000450701028d0002010000" for t in fake.sent))
    br.close()


def radio_status():
    raw = bytearray(STATUS)
    raw[10], raw[15], raw[21] = 0x6F, 0x20, 0x02
    raw[-2] = checksum(bytes(raw[:-2]))
    return Telegram.parse(bytes(raw))


def test_the_radio_is_picked_with_its_key_and_shown_when_the_machine_says_so(monkeypatch):
    class RadioFake(Fake):
        def send(self, tg):
            self.sent.append(tg)
            k = decode_beo4(tg)
            if k and k[0] == Beo4.RADIO:
                self.inject(radio_status())
    monkeypatch.setattr(facade, "_active", dict(facade._active, source=None, state="stopped"))
    monkeypatch.setattr(facade, "_power", {"value": "standby"})
    fake = RadioFake()
    br = MlBridge(BS9000, transport=fake, source_center=False)
    br.start()
    br.select_source("RADIO")
    assert wait(lambda: facade._active["source"] == "RADIO" and facade._power["value"] == "on")
    assert fake.sent[0].body[-2:] == bytes([0x6F, Beo4.RADIO])
    assert facade.current_source()["name"] == "Radio"
    assert facade.now_metadata()["title"] == "Radio"
    # a key on the radio is addressed to the radio, and audio is asked of it
    br.press("skip")
    assert wait(lambda: fake.sent[-1].body[-2:] == bytes([0x6F, Beo4.STEP_UP]))
    br.request_audio()
    assert wait(lambda: fake.sent[-1].body.hex() == "c106010b000000450701026f0002010000")
    # and a disc status takes the app back to the CD
    fake.inject(status(2, 1, 0x02, 0x2A))
    assert wait(lambda: facade._active["source"] is None and facade._active["cd"] == 2)
    assert facade.current_source()["type"] == {"value": "classicsAdapter"}
    br.close()


def test_the_app_picks_the_radio_from_the_source_list(monkeypatch):
    chosen = []

    class Machine(facade.MlBackend):
        def select_source(self, name):
            chosen.append(name)
    events = []
    monkeypatch.setattr(facade.notifier, "send", lambda name, data: events.append((name, data)))
    monkeypatch.setattr(facade, "_active", dict(facade._active, source=None))
    monkeypatch.setattr(facade, "_ml", Machine())
    menu = facade.remote_menu()
    radio = menu[facade.plain_source("RADIO").item_id]
    assert menu[ClassicsAdapterProfile.REMOTE_SOURCE_ID]["nextSiblingMenuItemId"] == radio["id"]
    assert radio["label"] == "Radio" and radio["content"]["contentUri"] == "lineIn"
    h = object.__new__(facade.Handler)
    sent = []
    h._send = lambda obj, code=200: sent.append(code)
    h._activate(radio["id"])
    assert sent == [200] and wait(lambda: chosen == ["RADIO"])
    assert ("WebSocketEventSourceChange", facade.mozart_source("lineIn", "Radio", "lineIn")) in events


def test_the_app_picking_the_discs_or_power_wakes_the_machine_on_its_first_source(monkeypatch):
    """The key and the byte it is addressed to are the machine's own, and a
    machine left on another source is taken back to its discs."""
    monkeypatch.setattr(facade.notifier, "send", lambda name, data: None)
    monkeypatch.setattr(facade, "_active", dict(facade._active, source="RADIO"))
    fake = Fake()
    br = MlBridge(BS9000, transport=fake, source_center=False)
    monkeypatch.setattr(facade, "_ml", br)
    h = object.__new__(facade.Handler)
    h._send = lambda obj, code=200: None
    h._activate(ClassicsAdapterProfile.REMOTE_SOURCE_ID)
    assert fake.sent[-1].body[-2:] == bytes([BS9000.main.code, BS9000.main.select])
    assert facade._active["source"] is None
    assert facade.current_source()["type"] == {"value": "classicsAdapter"}


class Presses:
    """Stands in for the bus: records the keys a bridge sends."""

    def __init__(self):
        self.sent = []

    def press(self, key, *, source=0x8D, dest=0x01, gap_s=None):
        self.sent.append((int(key), source, dest))


def volume_bridge():
    br = MlBridge(BS9000, transport=Fake(), source_center=False)
    br.bus = Presses()
    return br


def walk(br):
    """One pass of the walker, without its thread."""
    br._volume_wake.set()
    stop = br._stop

    class Once:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            # outer loop once, inner loop until it breaks by itself
            self.calls += 1
            return self.calls > 200
    br._stop = Once()
    import threading
    t = threading.Thread(target=br._volume_walker, daemon=True)
    t.start()
    t.join(0.5)
    br._stop = stop


def test_the_first_volume_is_where_counting_starts_and_the_next_is_walked_to():
    br = volume_bridge()
    br.volume(30)
    walk(br)
    assert br.bus.sent == []
    br.volume(36)
    walk(br)
    assert br.bus.sent == [(0x60, 0x8D, 0x01)] * 3 and br._volume_at == 36
    br.volume(33)
    walk(br)
    assert br.bus.sent[3:] == [(0x64, 0x8D, 0x01)] and br._volume_at == 34


def test_the_form_of_a_volume_key_can_be_spelled_out():
    br = volume_bridge()
    assert br.set_volume_form("src=0x7a,dest=0x0f,rel") == {"src": 0x7A, "dest": 0x0F, "rel": True}
    assert br.raw_key("0x60,src=0x7a,rel") == "0x60 {'src': 122, 'rel': True}"
    assert br.bus.sent == [(0x60, 0x7A, 0x01), (0x7E, 0x7A, 0x01)]
    assert br.set_volume_form("src=auto,rel=0")["src"] is None
    import pytest
    with pytest.raises(ValueError):
        br.set_volume_form("speed=3")


def test_the_default_volume_is_kept_and_is_where_a_machine_switched_on_stands(tmp_path, monkeypatch):
    kept = tmp_path / "volume-default"
    monkeypatch.setattr(facade, "VOLUME_FILE", str(kept))
    before = dict(facade._volume)
    br = volume_bridge()
    monkeypatch.setattr(facade, "_ml", br)
    try:
        facade.set_default_volume(44)
        assert kept.read_text().split() == ["44"] and facade._stored_default() == 44
        facade.set_default_volume(500)                 # never past the top of the scale
        assert facade._volume["default"] == facade._volume["maximum"]
        facade.set_default_volume(30)
        facade.machine_switched_on()
        assert facade._volume["level"] == 30 and br._volume_at == 30 and br.bus.sent == []
        facade.set_volume(level=34)                    # counted from the start volume
        walk(br)
        assert [k for k, _, _ in br.bus.sent] == [0x60, 0x60]
    finally:
        facade._volume.update(before)


def test_a_machine_with_one_disc_plays_it_whichever_disc_the_app_picks(monkeypatch):
    from beo.masterlink.profiles import BS3000

    class OneDisc(Fake):
        def send(self, tg):
            self.sent.append(tg)
            k = decode_beo4(tg)
            if k and k[0] == Beo4.CD:
                self.inject(status(0, 2, 0x02, 0x20))      # no disc number where a changer has one

    def keys():
        return [decode_beo4(t)[0] for t in fake.sent if decode_beo4(t)]
    monkeypatch.setattr(facade, "_active", dict(facade._active, cd=1, track=1, state="stopped"))
    fake = OneDisc()
    br = MlBridge(BS3000, transport=fake, source_center=False)
    br.start()
    br.select_disc(4)
    assert wait(lambda: facade._active["track"] == 2 and facade._active["state"] == "started")
    assert facade._active["cd"] == 1 and keys() == [Beo4.CD]
    assert fake.sent[0].body[-2:] == bytes([0x8D, Beo4.CD])
    # on the disc and playing, another pick has nothing to send
    br.select_disc(2)
    time.sleep(0.1)
    assert keys() == [Beo4.CD]
    br.close()
