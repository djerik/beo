from __future__ import annotations

import pytest

from beo.masterlink.codec import (Beo4, PlayState, Telegram, TelegramError, beo4_press, checksum,
                              decode_beo4, decode_clock, decode_distributed_source, decode_status_info,
                              encode_clock, request, HEARTBEAT, SRC_CD, SRC_N_RADIO, SRC_N_MUSIC)


def test_heartbeat_parses():
    t = Telegram.parse(HEARTBEAT)
    assert t.is_heartbeat and t.typ == 0x5E and t.payload_type == 0x14 and t.payload == b""


def test_checksum_example_from_mdt_docs():
    body = bytes.fromhex("c106010a0000000d02016f1f")
    assert checksum(body) == 0x70


@pytest.mark.parametrize("hx", [
    "c1c2010a0000002005020001006f93b800",       # our N.RADIO wake as c2
    "c106010a0000002005020001008d0c9300",       # our Standby as link node
    "c106010b000000080001dc00",                 # REQUEST 0x08
    "c2c1010ba100006c0a0100000000020201000200ae00",   # the audio master's 0x6c request for a1
    "06c10114000000080506020100a1019400",       # 0x08 answer: distributing a1
    "06c10114000000080004e800",                 # 0x08 answer: no source
    "c2c1010aa100001000034200",                 # STANDBY for a1
    "c2c1010a7a00001100021b00",                 # RELEASE for 7a
    "83c10114008d008715048d0100000008010100ffff0201ffffffffffffffff1700",   # 0x87 CD status
    "06c10114000000820a01066f0102010000ffff01e100",          # 0x82 track info, long
    "06c10114000000440b05026f0002010001000000ffa400",        # 0x44 track info
    "06c10114000000440d07026f000201000201000000008d3800",    # 0x44, the longer form
    "06c101140000005005030b000400014400",                    # 0x50 NAK
    "06c10114000000080004e800",                              # 0x08 answer: no source
    "06c101140000005008030b00450305028d011f00",              # 0x50 NAK of a 0x45 request
])
def test_measured_telegrams_parse(hx):
    t = Telegram.parse(bytes.fromhex(hx))
    assert t.hex == hx


def test_bad_checksum_rejected():
    raw = bytearray.fromhex("c106010b000000080001dc00")
    raw[-2] ^= 1
    with pytest.raises(TelegramError):
        Telegram.parse(bytes(raw))


def test_wrong_length_rejected():
    with pytest.raises(TelegramError):
        Telegram.parse(bytes.fromhex("c1"))
    # right checksum but payload length byte disagrees
    body = bytes.fromhex("c106010b000000080101")
    with pytest.raises(TelegramError):
        Telegram.parse(body + bytes([checksum(body), 0]))


def test_beo4_press_matches_measured_bytes():
    assert beo4_press(Beo4.STANDBY).hex == "c106010a0000002005020001008d0c9300"
    assert beo4_press(Beo4.N_RADIO, source=0x6F, frm=0xC2).hex == "c1c2010a0000002005020001006f93b800"
    assert beo4_press(Beo4.N_MUSIC, source=0x6F, frm=0xC2).hex == "c1c2010a0000002005020001006f94b900"
    assert beo4_press(Beo4.CD).hex == "c106010a0000002005020001008d921900"


def test_link_room_request_is_byte_exact():
    from beo.masterlink.codec import link_room_source_request
    assert link_room_source_request().body.hex() == "c106010b000000450701028d0002010000"


def test_request_08_matches_measured_bytes():
    assert request(0x08).hex == "c106010b000000080001dc00"
    assert request(0x0B).hex == "c106010b0000000b0001df00"


def test_decode_distributed_source():
    assert decode_distributed_source(Telegram.parse(bytes.fromhex("06c10114000000080506020100a1019400"))) == SRC_N_RADIO
    assert decode_distributed_source(Telegram.parse(bytes.fromhex("06c101140000000805060201007a016d00"))) == SRC_N_MUSIC
    assert decode_distributed_source(Telegram.parse(bytes.fromhex("06c10114000000080004e800"))) == 0
    assert decode_distributed_source(Telegram.parse(HEARTBEAT)) is None


def test_decode_status_info_cd():
    t = Telegram.parse(bytes.fromhex("83c10114008d008715048d0100000008010100ffff0201ffffffffffffffff1700"))
    si = decode_status_info(t)
    assert si.source == SRC_CD
    assert si.disc == 1                             # layout: byte 17
    assert si.changed_track and not si.changed_disc
    assert si.track is None                         # 0xff = unknown yet
    assert si.state == PlayState.PLAY               # byte 21 = 0x02


def test_status_echo_flag():
    # from ml_to_adapter: every status is repeated with mask 0x00
    raw = bytearray.fromhex("83c10114008d008715048d0100000008010100ffff0201ffffffffffffffff1700")
    raw[15] = 0
    raw[-2] = checksum(bytes(raw[:-2]))
    assert decode_status_info(Telegram.parse(bytes(raw))).is_echo


def test_decode_beo4_key():
    t = beo4_press(Beo4.STEP_UP)
    assert decode_beo4(t) == (Beo4.STEP_UP, SRC_CD)


def test_clock_roundtrip():
    t = encode_clock(18, 8, 6, 26, 9, 26)
    c = decode_clock(t)
    assert (c.hour, c.minute, c.second, c.day, c.month, c.year) == (18, 8, 6, 26, 9, 26)
    # the audio master's own clock telegram
    c2 = decode_clock(Telegram.parse(bytes.fromhex("80c10114000000400b0b0a000310300000280604063100")))
    assert (c2.hour, c2.minute) == (10, 30)


def test_global_release_signature():
    from beo.masterlink.codec import is_global_release
    assert is_global_release(Telegram.parse(bytes.fromhex("80c1010a0000001100015e00")))
    assert not is_global_release(Telegram.parse(bytes.fromhex("c2c1010a7a00001100021b00")))   # RELEASE to the source center
    assert beo4_press(Beo4.STANDBY, frm=0xF0).body.hex() == "c1f0010a0000002005020001008d0c"


def test_release_that_switches_the_audio_master_off():
    from beo.masterlink.codec import release_audio_master
    assert release_audio_master().hex == "c106010a000000110001e400"      # source byte 00
