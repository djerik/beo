"""A listener that asks for a datagram again gets it, as it went out."""
import socket
import struct
import time

from beo.networklink.audio import ToneSource
from beo.networklink.fps import (FORMAT_44100, FpsSender, MonotonicClock, REQUEST_MAGIC, parse_request,
                                 retransmitter)

PORT = 23456


def test_parse_request_reads_the_sequences_and_rejects_the_rest():
    assert parse_request(bytes.fromhex("1235000115f1")) == [0x15F1]
    assert parse_request(struct.pack(">HHHH", REQUEST_MAGIC, 2, 7, 9)) == [7, 9]
    assert parse_request(bytes.fromhex("12340001")) is None
    assert parse_request(bytes.fromhex("123500020001")) is None


def test_a_requested_datagram_is_sent_again_and_an_unkept_one_is_counted():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", PORT))
    rx.settimeout(2.0)
    tx = FpsSender("127.0.0.1", FORMAT_44100, MonotonicClock(), port=PORT)
    tx.start(ToneSource(FORMAT_44100, 440.0, 0.2))
    try:
        seen = {}
        deadline = time.monotonic() + 3.0
        while len(seen) < 12 and time.monotonic() < deadline:
            data, _ = rx.recvfrom(8192)
            if len(data) > 32:
                seen[struct.unpack(">H", data[2:4])[0]] = data
        assert len(seen) >= 12
        wanted = sorted(seen)[2]
        ask = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ask.sendto(struct.pack(">HHH", REQUEST_MAGIC, 1, wanted), ("127.0.0.1", PORT + 1))
        again = None
        deadline = time.monotonic() + 3.0
        while again is None and time.monotonic() < deadline:
            data, _ = rx.recvfrom(8192)
            if len(data) > 32 and struct.unpack(">H", data[2:4])[0] == wanted:
                again = data
        assert again == seen[wanted]
        assert tx.resent == 1 and retransmitter(PORT + 1).requests == 1
        ask.sendto(struct.pack(">HHH", REQUEST_MAGIC, 1, 60000), ("127.0.0.1", PORT + 1))
        deadline = time.monotonic() + 2.0
        while tx.not_kept == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert tx.not_kept == 1
        ask.close()
    finally:
        tx.stop()
        rx.close()
