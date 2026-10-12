from __future__ import annotations

from beo.masterlink.codec import Telegram
from beo.masterlink.roles.source_center import SourceCenter, N_RADIO, N_MUSIC

from bus_support import wait_until

REQ_A1 = Telegram.parse(bytes.fromhex("c2c1010ba100006c0a0100000000020201000200ae00"))
REQ_7A = Telegram.parse(bytes.fromhex("c2c1010b7a00006c0a0100000000020201000200" + "87" + "00"))
STANDBY_A1 = Telegram.parse(bytes.fromhex("c2c1010aa100001000034200"))
RELEASE_7A = Telegram.parse(bytes.fromhex("c2c1010a7a00001100021b00"))


def test_wake_sends_measured_press_and_announces(fake_bus):
    bus, tr = fake_bus
    tr.responder = lambda sent: [REQ_A1] if sent.hex == "c1c2010a0000002005020001006f93b800" else []
    sc = SourceCenter(bus, gap_s=0.0, clock_interval_s=0)
    sc.metadata[N_RADIO].text = "CLAUDE TEST 440HZ"
    sc.metadata[N_RADIO].secondary = "DK"
    sc.metadata[N_RADIO].genre = "TEST"
    sc.start()
    assert sc.wake(N_RADIO, timeout=1.0)
    assert wait_until(lambda: len(tr.sent) >= 9)
    hexes = [t.hex for t in tr.sent]
    assert hexes[0] == "c1c2010a0000002005020001006f93b800"
    assert hexes[1] == "c1c2011400a1006c010801af00"                                # the 0x6c reply
    assert hexes[2] == "83c2012c00a10006110003010100004e2e524144494f2020202020ba00"    # DISPLAY_SOURCE
    assert hexes[3].startswith("83c2011400a10087")                                   # STATUS_INFO
    assert hexes[4] == "c1c20114000000820a0106a10002000000000001cf00"                # TRACK_INFO_LONG
    # metadata: subtype 4 text, 3 country, 2 genre
    text = b"CLAUDE TEST 440"                 # the extended form takes 15 characters
    assert hexes[5] == Telegram.from_body_hex(
        "83c2012c00a1000b" + "%02x" % (14 + len(text)) + "00040003" + "01a100000003e700010001" + text.hex()).hex
    # every telegram carries exactly the payload its length byte declares
    assert all(Telegram.is_complete(bytes.fromhex(h)) for h in hexes)
    assert sc.active == N_RADIO
    sc.close()


def test_display_text_keeps_only_what_the_display_takes():
    def text(s):
        return bytes(SourceCenter.ext_source_info(0x7A, 4, s).raw)[24:-2]
    assert text("Rádio Orbital") == b"Radio Orbital"
    assert text("What? (Live) & more!") == b"What Live  more"
    assert text("Søen") == b"Sen"


def test_music_variant(fake_bus):
    bus, tr = fake_bus
    tr.responder = lambda sent: [REQ_7A] if sent.hex.endswith("6f94b900") else []
    sc = SourceCenter(bus, gap_s=0.0, clock_interval_s=0)
    sc.metadata[N_MUSIC].text = "TITLE"
    sc.metadata[N_MUSIC].secondary = "ALBUM"
    sc.start()
    assert sc.wake(N_MUSIC, timeout=1.0)
    assert wait_until(lambda: len(tr.sent) >= 8)
    hexes = [t.hex for t in tr.sent]
    # DISPLAY_SOURCE for N.MUSIC: "N.MUSIC" + 5 spaces = 12 bytes
    body = "83c2012c007a0006110003010100004e2e4d555349432020202020"
    assert hexes[2] == body + "%02x" % (sum(bytes.fromhex(body)) & 0xFF) + "00"
    assert hexes[3].startswith("83c20114007a00871f047a")
    # the title for a 12-character display, sent once because it fits
    shown = Telegram.from_body_hex("83c2012c007a000611000302010000" + b"TITLE       ".hex()).hex
    assert wait_until(lambda: shown in [t.hex for t in tr.sent])
    sc.close()


def shown_texts(tr):
    return [bytes.fromhex(t.hex)[15:27].decode() for t in tr.sent
            if t.hex.startswith("83c2012c007a000611000302")]


def test_lines_take_turns_and_a_wide_one_scrolls(fake_bus):
    bus, tr = fake_bus
    sc = SourceCenter(bus, gap_s=0.0, clock_interval_s=0, page_s=0.01, scroll_step_s=0.0, scroll_hold_s=0.01)
    sc.start()
    sc.active = N_MUSIC
    sc.show(N_MUSIC, ["Short", "Fourteen chars"])
    assert wait_until(lambda: len(shown_texts(tr)) >= 5)
    assert shown_texts(tr)[:5] == ["Short       ", "Fourteen cha", "ourteen char", "urteen chars", "Short       "]
    # new lines take over at once, and nothing is sent once the source is left
    sc.show(N_MUSIC, ["Other"])
    assert wait_until(lambda: shown_texts(tr)[-1] == "Other       ")
    sc.active = None
    n = len(tr.sent)
    sc.show(N_MUSIC, ["Unseen"])
    assert not wait_until(lambda: len(tr.sent) > n, timeout=0.2)
    sc.close()


def test_standby_and_release_callbacks(fake_bus):
    bus, tr = fake_bus
    sc = SourceCenter(bus, clock_interval_s=0)
    got = []
    sc.on_standby.append(lambda n: got.append(("standby", n)))
    sc.on_release.append(lambda n: got.append(("release", n)))
    sc.start()
    sc.active = N_RADIO
    tr.inject(STANDBY_A1, delay=0.01)
    tr.inject(RELEASE_7A, delay=0.08)
    assert wait_until(lambda: len(got) == 2)
    assert got == [("standby", N_RADIO), ("release", N_MUSIC)]
    assert sc.active is None


def test_no_request_means_wake_false(fake_bus):
    bus, tr = fake_bus
    sc = SourceCenter(bus, clock_interval_s=0)
    sc.start()
    assert sc.wake(N_RADIO, timeout=0.1) is False
