"""Framing of bytes as a USB serial adapter delivers them."""
from __future__ import annotations

import pytest

from beo.masterlink.bus import Framer
from beo.masterlink.codec import HEARTBEAT, Telegram

SILENCE_S = 0.005
USB_GAP_S = 0.020     # what the adapter leaves between the pieces of a telegram it has cut


def replay(arrivals):
    """arrivals: (seconds, bytes) as the port delivers them -> the telegrams framed."""
    fr = Framer(silence_s=SILENCE_S)
    out = []
    last = None
    for t, chunk in arrivals:
        if last is not None and t - last > SILENCE_S:
            out.extend(fr.flush(t))
        out.extend(fr.feed(chunk, t))
        last = t
    out.extend(fr.flush((last or 0) + 1.0))
    return out


def cut(hx: str, at, t: float):
    """One telegram arriving in pieces: cut at the given offsets, USB_GAP_S apart."""
    raw = bytes.fromhex(hx)
    edges = [0, *(a % len(raw) for a in at), len(raw)]
    return [(t + i * USB_GAP_S, raw[a:b]) for i, (a, b) in enumerate(zip(edges, edges[1:]))]


def test_session_frames_every_telegram():
    # What we send echoes back with its closing 00 a USB frame late, or with
    # only its first byte ahead of the rest; the audio master's answers arrive
    # whole or cut in the middle.
    too_short = "c1c20114000000820a0106a100020000000001cf00"    # length byte says 22, has 21
    session = [
        ("c1c2010a0000002005020001006f93b800", [-1], 1.50),
        (HEARTBEAT.hex(), [], 1.87),
        ("c2c1010ba100006c0a0100000000020201000200ae00", [], 1.99),
        ("c1c2011400a1006c010801af00", [-1], 2.25),
        (too_short, [-1], 3.10),
        ("83c2012c00a1000b150004000301a100000003e700010001416972506c61797a00", [-1], 3.40),
        ("c106010b000000080001dc00", [], 5.17),
        ("06c10114000000080506020100a1019400", [], 5.23),
        ("c106010b0000000b0001df00", [1], 6.29),
        (HEARTBEAT.hex(), [7], 7.86),
        ("c2c1010aa100001000034200", [], 11.61),
        ("83c10114008d008715048d0100000008010100ffff0201ffffffffffffffff1700", [11], 11.73),
        ("06c10114000000080004e800", [], 14.42),
        ("c2c1010a7a00001100021b00", [], 17.47),
        ("c2c1010aa100001100024200", [], 17.53),
    ]
    out = replay([piece for hx, at, t in session for piece in cut(hx, at, t)])
    # A malformed telegram is never accepted, and the good one after it still
    # comes through.
    assert [t.hex for t in out] == [hx for hx, _, _ in session if hx != too_short]


def test_junk_before_good_telegram_is_dropped_on_silence():
    fr = Framer()
    junk = bytes.fromhex("c1c20114000000820a0106a100020000000001cf00")     # length byte says 22, has 21
    good = bytes.fromhex("83c2012c00a1000b150004000301a1000000" + "03e700010001416972506c61797a00")
    out = fr.feed(junk, 0.0)
    out += fr.feed(good, 0.300)
    out += fr.flush(0.400)
    assert [t.raw for t in out] == [good]
    # every framed telegram is a valid one
    for t in out:
        Telegram.parse(t.raw)


def test_split_echo_is_reassembled():
    fr = Framer(silence_s=0.005)
    raw = bytes.fromhex("c1c2010a0000002005020001006f93b800")
    out = fr.feed(raw[:1], 1.000)
    out += fr.flush(1.010)          # silence, but "c1" alone is not a telegram: keep it
    out += fr.feed(raw[1:], 1.023)  # the rest 23 ms later
    out += fr.flush(1.040)
    assert [t.raw for t in out] == [raw]


def test_two_telegrams_back_to_back_with_gap():
    fr = Framer()
    a = bytes.fromhex("8001015e000000140001f500")
    b = bytes.fromhex("c106010b000000080001dc00")
    out = fr.feed(a, 0.0)
    out += fr.feed(b, 0.050)
    out += fr.flush(0.100)
    assert [t.raw for t in out] == [a, b]


def test_garbage_is_resynced_by_scan():
    fr = Framer(max_buffer=20)
    good = bytes.fromhex("8001015e000000140001f500")
    out = fr.feed(b"\x05\xf2" + good + b"\x99" * 10, 0.0)
    out += fr.flush(1.0)
    assert [t.raw for t in out] == [good]


@pytest.mark.parametrize("hx", [
    "c2c1010ba100006c0a0100000000020201000200ae00",    # the 0x6c request
    "c2c1010a7a00001100021b00",                        # RELEASE 7a
    "83c2012c00a10006110003010100004e2e524144494f2020202020ba00",
])
def test_telegram_cut_at_any_byte_comes_out_whole(hx):
    """With the FTDI latency timer at 16 ms a telegram arrives cut at an
    arbitrary point, and the silence between the pieces is longer than the
    one that ends a telegram."""
    for at in range(1, len(bytes.fromhex(hx))):
        assert [t.hex for t in replay(cut(hx, [at], 1.0))] == [hx], at


def test_cut_telegram_right_after_a_whole_one():
    first, second = "c2c1010a7a00001100021b00", "c2c1010aa100001100024200"
    out = replay(cut(first, [], 17.523) + cut(second, [2], 17.543))
    assert [t.hex for t in out] == [first, second]
