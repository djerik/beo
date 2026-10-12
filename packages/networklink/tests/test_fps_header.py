"""Invariant 10 and the 32-byte header: byte goldens."""
from beo.networklink.fps import AudioFormat, FORMAT_44100, RATE_HZ, build_header, build_marker


def test_header_is_32_bytes_golden():
    h = build_header(1, 0x0102030405060708, 300_000_000)
    assert len(h) == 32
    assert h.hex() == ("1234" "0001" "00016100"
                       "0102030405060708"
                       "ffffffffffffffff"
                       "0000000011e1a300")


def test_header_session_field_and_wrap():
    # a format-2 (48 kHz) session field; we never send it, the field just passes through
    h = build_header(0x1_0005, 2**64 + 7, -1, bytes.fromhex("00024900"))
    assert h[2:4] == b"\x00\x05"                 # sequence wraps at 65536
    assert h[4:8] == bytes.fromhex("00024900")
    assert h[8:16] == (7).to_bytes(8, "big")     # 64-bit, masked
    assert h[24:32] == (-1).to_bytes(8, "big", signed=True)


def test_marker_golden_from_a_live_capture():
    # start  1234 0001 0002 0200 00..
    assert build_marker(1).hex() == "12340001000202000000000000000000"
    # pause  1234 028a 0003 0200 00..
    assert build_marker(0x028a, 3).hex() == "1234028a000302000000000000000000"
    # play   1234 05df 0004 0200 00..
    assert build_marker(0x05df, 4).hex() == "123405df000402000000000000000000"
    assert len(build_marker(0)) == 16


def test_audio_format_numbers():
    assert FORMAT_44100.payload_bytes == 4248
    assert FORMAT_44100.interval_ns == 24_081_633
    assert AudioFormat().rate_hz == RATE_HZ == 44100          # the only rate, for every product
    assert abs(AudioFormat(44100, frames_per_datagram=1062).interval_s * 44100 / 1062 - 1.0) < 1e-6
