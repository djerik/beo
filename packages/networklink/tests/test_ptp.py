"""PtpSlaveClock against synthetic PTPv2 datagrams, laid out as IEEE 1588-2008
says and as the products send them.

Invariants:
  1. the clockIdentity is the MAC in EUI-64 form (00:00:5e:00:53:a6 ->
     00005efffe0053a6);
  2. a Delay_Req is 44 bytes, type 1, version 2, domain 0, our identity, and
     the sequence we remember T3 under;
  3. multicast Sync+Follow_Up: offset = MAX(T1 - T2) over the window, since
     multicast is only ever late;
  4. Delay_Resp: offset = T4 - T3 - RTT/2, median of the quick answers; an
     answer to someone else's request is ignored; unicast outranks multicast;
  5. a new grandmaster drops everything and the next estimate is taken at once;
  6. in use, the offset slews at most MAX_SLEW_NS_PER_S; idle, it jumps;
  7. until the network answers, the fallback offset is used, and at_ns/now_ns
     carry the lead like OffsetPtpClock.
"""
import struct

from beo.networklink import ptp
from beo.networklink.fps import OffsetPtpClock, PTP_LEAD_NS
from beo.networklink.ptp import (ANNOUNCE, DELAY_RESP, FOLLOW_UP, SYNC, PtpSlaveClock,
                                 build_delay_req, clock_identity, header, pack_timestamp)

GM_MAC = "00:00:5e:00:53:a6"
GM_ID = clock_identity(GM_MAC)
OUR_MAC = "00:00:5e:00:53:01"
OUR_ID = clock_identity(OUR_MAC)
MASTER = "203.0.113.10"
NS = 1_000_000_000


def sync(seq, gm=GM_ID):
    return header(SYNC, 44, gm, seq, flags=0x0200, control=0) + b"\x00" * 10     # two-step


def follow_up(seq, t1_ns, gm=GM_ID):
    return header(FOLLOW_UP, 44, gm, seq, control=2) + pack_timestamp(t1_ns)


def delay_resp(seq, t4_ns, requester=OUR_ID, gm=GM_ID):
    return header(DELAY_RESP, 54, gm, seq, control=3) + pack_timestamp(t4_ns) + requester + struct.pack(">H", 1)


def announce(seq, gm=GM_ID):
    body = (pack_timestamp(0) + struct.pack(">h", 0) + b"\x00" + bytes([128])
            + bytes([0xFE, 0xFF, 0xFF, 0xFF]) + bytes([128]) + gm
            + struct.pack(">H", 0) + bytes([0xA0]))
    return header(ANNOUNCE, 64, gm, seq, control=5, log_interval=1) + body


def clock(**kw):
    return PtpSlaveClock(mac=OUR_MAC, **kw)


# ------------------------------------------------------------ 1, 2: messages


def test_clock_identity_is_eui64_of_mac():
    assert GM_ID.hex() == "00005efffe0053a6"
    assert clock_identity("00-00-5e-00-53-01") == OUR_ID


def test_delay_req_layout():
    pkt = build_delay_req(OUR_ID, 0x1234)
    assert len(pkt) == 44
    assert pkt[0] & 0x0F == 1 and pkt[1] & 0x0F == 2
    assert struct.unpack(">H", pkt[2:4])[0] == 44
    assert pkt[4] == 0                                   # domain
    assert pkt[20:28] == OUR_ID and pkt[28:30] == b"\x00\x01"
    assert struct.unpack(">H", pkt[30:32])[0] == 0x1234
    assert pkt[32] == 1 and pkt[33] == 0x7F              # controlField Delay_Req, logMessageInterval
    assert pkt[34:44] == b"\x00" * 10


def test_timestamp_roundtrip_over_48_bit_seconds():
    t = (190 * 86400 + 12345) * NS + 987_654_321
    assert ptp.ptp_timestamp(pack_timestamp(t), 0) == t


# ------------------------------------------------------------ 3: multicast


def test_follow_up_offset_is_max_over_window():
    c = clock()
    t1 = 16_467_952 * NS
    local = 1_000 * NS
    # four pairs; the multicast arrival is late by 0, 296, 36 and 120 ms
    for i, late in enumerate((0, 296, 36, 120)):
        c.handle(sync(i), MASTER, local + i * NS + late * 1_000_000)
        c.handle(follow_up(i, t1 + i * NS), MASTER, local + i * NS + late * 1_000_000 + 5_000_000)
    assert c.fixed and c.master_ip == MASTER
    assert c.status()["target_ns"] == t1 - local              # the undelayed pair, not the mean
    assert c.at_ns(local + 10 * NS) == t1 + 10 * NS + PTP_LEAD_NS


def test_follow_up_without_its_sync_is_ignored():
    c = clock()
    c.handle(follow_up(7, 5 * NS), MASTER, 1 * NS)
    assert not c.fixed and c.follow_ups == 0


# ------------------------------------------------------------ 4: unicast


def test_delay_resp_offset_uses_half_round_trip_and_quick_answers():
    c = clock(master_ip=MASTER)
    offset = 16_467_952 * NS + 500_000_000
    t3 = 2_000 * NS
    # five requests; round trips 8, 9, 80 (a WiFi outlier), 10, 8 ms
    for seq, rtt_ms in enumerate((8, 9, 80, 10, 8), start=1):
        rtt = rtt_ms * 1_000_000
        c._sent[seq] = t3
        c.handle(delay_resp(seq, t3 + offset + rtt // 2), MASTER, t3 + rtt)
        t3 += NS
    assert c.responses == 5
    assert c.status()["target_ns"] == offset                  # the outlier's wrong half is not in the median


def test_delay_resp_for_another_requester_is_ignored():
    c = clock(master_ip=MASTER)
    c._sent[1] = 0
    c.handle(delay_resp(1, 10 * NS, requester=clock_identity("00:11:22:33:44:55")), MASTER, 8_000_000)
    assert c.responses == 0 and not c.fixed
    assert 1 in c._sent


def test_unicast_outranks_multicast():
    c = clock()
    c.handle(sync(1), MASTER, 1_000 * NS + 200_000_000)          # 200 ms late multicast
    c.handle(follow_up(1, 5_000 * NS), MASTER, 1_000 * NS + 205_000_000)
    assert c.status()["target_ns"] == 4_000 * NS - 200_000_000
    c._sent[1] = 1_001 * NS
    c.handle(delay_resp(1, 5_001 * NS + 4_000_000), MASTER, 1_001 * NS + 8_000_000)
    assert c.status()["target_ns"] == 4_000 * NS
    # later multicast pairs no longer move the target
    c.handle(sync(2), MASTER, 1_002 * NS + 300_000_000)
    c.handle(follow_up(2, 5_002 * NS), MASTER, 1_002 * NS + 305_000_000)
    assert c.status()["target_ns"] == 4_000 * NS


# ------------------------------------------------------------ 5: master change


def test_new_grandmaster_drops_the_offset_and_refixes_at_once():
    c = clock()
    c.handle(announce(1), MASTER, 0)
    c.handle(sync(1), MASTER, 1_000 * NS)
    c.handle(follow_up(1, 5_000 * NS), MASTER, 1_000 * NS + 5_000_000)
    assert c.at_ns(1_000 * NS) == 5_000 * NS + PTP_LEAD_NS
    c.at_ns(1_001 * NS)                                        # in use

    other = clock_identity("00:09:a7:11:22:33")
    c.handle(announce(1, gm=other), "203.0.113.20", 1_002 * NS)
    assert c.master_changes == 1 and c.master_ip == "203.0.113.20" and c.grandmaster == other
    assert not c.fixed and c.offset_ns is None
    c.handle(sync(1, gm=other), "203.0.113.20", 1_003 * NS)
    c.handle(follow_up(1, 100 * NS, gm=other), "203.0.113.20", 1_003 * NS + 5_000_000)
    # 903 s away from the old offset, taken at once, not slewed at 20 ms/s
    assert c.at_ns(1_003 * NS) == 100 * NS + PTP_LEAD_NS


def test_same_grandmaster_announced_again_changes_nothing():
    c = clock()
    c.handle(announce(1), MASTER, 0)
    c.handle(announce(2), MASTER, 2 * NS)
    assert c.master_changes == 0


def test_a_fix_before_the_first_announce_does_not_name_the_grandmaster():
    c = clock()
    c.handle(sync(1), MASTER, 1_000 * NS)
    c.handle(follow_up(1, 5_000 * NS), MASTER, 1_000 * NS + 5_000_000)
    assert c.wait_for_fix(0) and not c.wait_for_grandmaster(0)
    c.handle(announce(1), MASTER, 1_500 * NS)
    assert c.wait_for_grandmaster(0) and c.grandmaster == GM_ID


# ------------------------------------------------------------ 6: slew


def test_in_use_the_offset_slews_at_most_max_slew_per_second():
    c = clock()
    c.handle(sync(1), MASTER, 1_000 * NS)
    c.handle(follow_up(1, 5_000 * NS), MASTER, 1_000 * NS + 5_000_000)
    assert c.at_ns(1_000 * NS) == 5_000 * NS + PTP_LEAD_NS
    # a better estimate 1 s later, 300 ms higher, while being read every 100 ms
    c.handle(sync(2), MASTER, 1_001 * NS - 300_000_000)
    c.handle(follow_up(2, 5_001 * NS), MASTER, 1_001 * NS - 295_000_000)
    assert c.status()["target_ns"] == 4_000 * NS + 300_000_000
    t = 1_000 * NS
    prev = c.at_ns(t) - t
    for _ in range(10):
        t += 100_000_000
        cur = c.at_ns(t) - t
        assert 0 <= cur - prev <= PtpSlaveClock.MAX_SLEW_NS_PER_S // 10 + 1
        prev = cur
    assert cur - PTP_LEAD_NS == 4_000 * NS + 20_000_000           # 20 ms after one second


def test_idle_the_offset_jumps():
    c = clock()
    c.handle(sync(1), MASTER, 1_000 * NS)
    c.handle(follow_up(1, 5_000 * NS), MASTER, 1_000 * NS + 5_000_000)
    c.at_ns(1_000 * NS)
    c.handle(sync(2), MASTER, 1_001 * NS - 300_000_000)
    c.handle(follow_up(2, 5_001 * NS), MASTER, 1_001 * NS - 295_000_000)
    t = 1_000 * NS + PtpSlaveClock.IDLE_NS + 1
    assert c.at_ns(t) - t - PTP_LEAD_NS == 4_000 * NS + 300_000_000


# ------------------------------------------------------------ 7: fallback, lead


def test_fallback_offset_until_the_network_answers():
    stored = 11_804_772_922_374_556
    c = clock(fallback_offset_ns=stored)
    ref = OffsetPtpClock(stored)
    assert c.at_ns(123 * NS) == ref.at_ns(123 * NS)
    assert not c.fixed
    c.handle(sync(1), MASTER, 1_000 * NS)
    c.handle(follow_up(1, 5_000 * NS), MASTER, 1_000 * NS + 5_000_000)
    assert c.at_ns(1_000 * NS) == 5_000 * NS + PTP_LEAD_NS


def test_no_fallback_and_no_network_is_a_bare_monotonic_clock_plus_lead():
    c = clock()
    assert c.at_ns(42) == 42 + PTP_LEAD_NS


def test_foreign_domain_and_non_ptp_datagrams_are_ignored():
    c = clock()
    other_domain = bytearray(sync(1)); other_domain[4] = 1
    c.handle(bytes(other_domain), MASTER, 0)
    c.handle(b"\x12\x34" + b"\x00" * 30, MASTER, 0)              # an FPS datagram, say
    c.handle(b"", MASTER, 0)
    assert c.master_ip is None and not c._t2
