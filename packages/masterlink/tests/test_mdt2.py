"""The frames between the Pi and a MasterDataTool v2's controller."""
from __future__ import annotations

import sys
import threading
import types

import pytest

from beo.masterlink import mdt2
from beo.masterlink.codec import Telegram
from beo.masterlink.mdt2 import CHAN_LOG, CHAN_ML, CHAN_PING, FrameParser, Mdt2Transport, crc16, describe_pong, frame
from bus_support import wait_until

PRESS = Telegram.from_body_hex("c1c2010a0000002005020001006f93")       # a Beo4 press, 15 bytes of body
STATUS = Telegram.from_body_hex("83c1011400a100871a04a10100001fbe01000000ff020100030101010300020000000001")


def test_crc_is_ccitt_false():
    assert crc16(b"123456789") == 0x29B1


def test_frame_layout():
    f = frame(CHAN_ML, b"\x01\x02")
    assert f[:2] == b"\x55\xaa"
    assert f[2] == CHAN_ML and f[3] == 2 and f[4:6] == b"\x01\x02"
    assert f[6] | (f[7] << 8) == crc16(b"\x00\x02\x01\x02")
    assert frame(CHAN_PING) == b"\x55\xaa\xff\x00" + bytes([crc16(b"\xff\x00") & 0xFF, crc16(b"\xff\x00") >> 8])


def test_frame_refuses_more_than_the_controller_takes():
    frame(CHAN_ML, bytes(128))
    with pytest.raises(ValueError):
        frame(CHAN_ML, bytes(129))


def test_parser_takes_frames_in_any_chunks():
    stream = frame(CHAN_ML, PRESS.raw) + frame(CHAN_LOG, b"hello") + frame(CHAN_ML, STATUS.raw)
    for size in (1, 3, 7, 64, len(stream)):
        p = FrameParser()
        got = []
        for i in range(0, len(stream), size):
            got.extend(p.feed(stream[i:i + size]))
        assert got == [(CHAN_ML, PRESS.raw), (CHAN_LOG, b"hello"), (CHAN_ML, STATUS.raw)], size


def test_parser_skips_noise_and_corrupt_frames():
    good = frame(CHAN_ML, PRESS.raw)
    bad = bytearray(frame(CHAN_ML, STATUS.raw))
    bad[10] ^= 0xFF                                 # a payload byte flipped: the crc no longer matches
    p = FrameParser()
    assert p.feed(b"\x00\x55\x01" + bytes(bad) + good) == [(CHAN_ML, PRESS.raw)]
    # a length byte no frame can have is not a frame start either
    assert p.feed(b"\x55\xaa\x00\xff" + good) == [(CHAN_ML, PRESS.raw)]


def test_parser_keeps_a_sync_cut_between_chunks():
    f = frame(CHAN_ML, PRESS.raw)
    p = FrameParser()
    assert p.feed(f[:1]) == []
    assert p.feed(f[1:]) == [(CHAN_ML, PRESS.raw)]


def test_pong_in_words():
    assert describe_pong(b"PONG" + bytes([1, 5, 5, 0x01, 0x00]) + b"abc123") == "firmware 1.5.5, ML, slave, build abc123"
    assert describe_pong(b"PONG" + bytes([1, 3, 0, 0x0f])) == "firmware 1.3.0, ML/DL86/DL80/GPIO"
    assert describe_pong(b"PONG") == "firmware without a version"
    assert describe_pong(b"nope").startswith("unexpected")


class FakeSerial:
    """What the transport uses of a serial port: reads block on a queue of
    chunks the test puts in, writes are collected."""

    def __init__(self, port, baud, timeout=None):
        self.port, self.baudrate = port, baud
        self.written = bytearray()
        self._incoming = []
        self._ready = threading.Condition()

    def reset_input_buffer(self):
        pass

    def read(self, n):
        with self._ready:
            self._ready.wait(0.02)
            if not self._incoming:
                return b""
            return self._incoming.pop(0)

    def arrive(self, data: bytes):
        with self._ready:
            self._incoming.append(data)
            self._ready.notify()

    def write(self, data):
        self.written.extend(data)

    def flush(self):
        pass

    def close(self):
        pass


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=FakeSerial))
    tr = Mdt2Transport("/dev/serial0")
    received = []
    tr.start(received.append)
    yield tr, tr._ser, received
    tr.close()


def test_start_pings_the_controller(transport):
    tr, ser, _ = transport
    assert bytes(ser.written) == frame(CHAN_PING)
    assert ser.port == "/dev/serial0" and ser.baudrate == 115200


def test_send_frames_the_body_without_checksum(transport):
    tr, ser, _ = transport
    ser.written.clear()
    tr.send(PRESS)
    assert bytes(ser.written) == frame(CHAN_ML, PRESS.body)
    assert PRESS.raw[-2:] not in bytes(ser.written)[4:-2]


def test_received_telegrams_reach_the_subscriber(transport):
    tr, ser, received = transport
    ser.arrive(frame(CHAN_LOG, b"boot") + frame(CHAN_ML, STATUS.raw)[:10])
    ser.arrive(frame(CHAN_ML, STATUS.raw)[10:])
    assert wait_until(lambda: received == [STATUS])


def test_an_echo_of_our_own_telegram_is_dropped(transport):
    tr, ser, received = transport
    tr.send(PRESS)
    ser.arrive(frame(CHAN_ML, PRESS.raw) + frame(CHAN_ML, STATUS.raw))
    assert wait_until(lambda: received == [STATUS])
    # the same telegram from the bus, not sent by us, does come through
    ser.arrive(frame(CHAN_ML, PRESS.raw))
    assert wait_until(lambda: received == [STATUS, PRESS])


def test_a_telegram_the_controller_mangled_is_ignored(transport, caplog):
    tr, ser, received = transport
    ser.arrive(frame(CHAN_ML, STATUS.raw[:-3]) + frame(CHAN_ML, PRESS.raw))
    assert wait_until(lambda: received == [PRESS])
    assert len(received) == 1
