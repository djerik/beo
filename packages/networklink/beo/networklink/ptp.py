"""The products' clock, read off the network: an IEEE 1588 PTPv2 slave.

The PTS in every FPS datagram (fps.py, offset 8..15) is a moment on the
network's PTP clock, and a listener plays what it can place on that clock.
This slave follows the clock live, so the offset stays right through reboots
and a change of grandmaster (the products elect a new one after a power cut).
A stored offset, derived once from a listener's own log (nsdk.FpsdLog), is
the fallback until the network answers.

The products run ordinary PTPv2 (version 2, domain 0, two-step, Sync every
second, Announce every two) and elect a grandmaster among themselves, exactly
as the standard says. Two things differ from the standard:

  * the ports are 3319 (event: Sync, Delay_Req) and 3320 (general:
    Follow_Up, Announce, Delay_Resp), not 319/320;
  * the group is 224.0.0.107, the standard's peer-delay group, not 224.0.1.129.

A WiFi interface receives only the groups it has joined, so the slave joins
224.0.0.107 on both ports. The grandmasterIdentity is the master's MAC in
EUI-64 form (aa:bb:cc:dd:ee:ff -> aabbccfffeddeeff) and timeSource is 0xA0,
internal oscillator: PTP time is the master's own free-running counter, not
wall-clock time.

Over WiFi:

  multicast Sync+Follow_Up   The multicast path is only ever LATE, so the
                             truth is the MAXIMUM of a window, never the
                             mean.
  unicast Delay_Req/Resp     Every request is answered; the offset from the
                             quickest answers is good to a few ms, and agrees
                             with the multicast maximum to ~10 ms.

A REQUEST_UNICAST_TRANSMISSION signaling message is not granted, so the
master is never asked for unicast Sync. Delay_Resp alone fixes the offset;
multicast Follow_Up gives a first fix within a second and tracks the master's
identity.

    python3 -m beo.networklink.ptp              watch the master and the offset
    python3 -m beo.networklink.ptp --compare FILE   ... against a stored offset
"""
from __future__ import annotations

import argparse
import logging
import select
import socket
import struct
import sys
import threading
import time
import uuid
from typing import Dict, List, Optional, Tuple

from .fps import PTP_LEAD_NS

log = logging.getLogger("beo.networklink.ptp")

PTP_GROUP = "224.0.0.107"
PTP_EVENT_PORT = 3319
PTP_GENERAL_PORT = 3320
PTP_DOMAIN = 0

SYNC, DELAY_REQ, FOLLOW_UP, DELAY_RESP, ANNOUNCE = 0x0, 0x1, 0x8, 0x9, 0xB
HEADER_BYTES = 34
NS = 1_000_000_000


# ------------------------------------------------------------------ messages


def ptp_timestamp(b: bytes, at: int = HEADER_BYTES) -> int:
    """The 10-byte PTP Timestamp at ``at``: 48-bit seconds, 32-bit nanoseconds."""
    return int.from_bytes(b[at:at + 6], "big") * NS + struct.unpack(">I", b[at + 6:at + 10])[0]


def pack_timestamp(ns: int) -> bytes:
    s, n = divmod(int(ns), NS)
    return s.to_bytes(6, "big") + struct.pack(">I", n)


def clock_identity(mac: str) -> bytes:
    """A MAC address as an EUI-64 clockIdentity, which is what the products use."""
    o = [int(x, 16) for x in mac.replace("-", ":").split(":")]
    if len(o) != 6:
        raise ValueError("not a MAC address: %r" % mac)
    return bytes(o[:3]) + b"\xff\xfe" + bytes(o[3:])


def header(kind: int, length: int, identity: bytes, sequence: int, *, domain: int = PTP_DOMAIN,
           flags: int = 0, control: int = 5, log_interval: int = 0x7F) -> bytes:
    return (bytes([kind & 0x0F, 0x02]) + struct.pack(">H", length) + bytes([domain, 0])
            + struct.pack(">H", flags)
            + b"\x00" * 8                               # correctionField
            + b"\x00" * 4                               # reserved
            + identity + struct.pack(">H", 1)           # sourcePortIdentity
            + struct.pack(">H", sequence & 0xFFFF)
            + bytes([control & 0xFF]) + struct.pack("b", log_interval))


def build_delay_req(identity: bytes, sequence: int, domain: int = PTP_DOMAIN) -> bytes:
    """A 44-byte Delay_Req. The master answers with Delay_Resp carrying the time
    it received this: the one measurement that does not suffer the multicast delay."""
    return header(DELAY_REQ, 44, identity, sequence, domain=domain, control=1) + b"\x00" * 10


def message_kind(data: bytes) -> Optional[int]:
    """The messageType of a PTPv2 datagram, or None for anything else."""
    if len(data) < HEADER_BYTES or (data[1] & 0x0F) != 2:
        return None
    return data[0] & 0x0F


# ------------------------------------------------------------------ the clock


class PtpSlaveClock:
    """PTP time read off the network, continuously, instead of stored once.

    Same two methods as :class:`fps.OffsetPtpClock`, so the sender does not
    know the difference. What differs is where the offset comes from: a thread
    joins the products' group, learns who the master is from Sync/Announce,
    sends it a Delay_Req every second and keeps the offset from the answers.

    Only ever late: a Sync that arrives through WiFi multicast was delayed,
    never advanced, so the multicast estimate is the maximum of a window. A
    Delay_Resp gives offset = T4 - T3 - round trip / 2; only the quickest
    answers in the window are believed, and their median is used.

    While someone is reading the clock the offset is slewed, never jumped: a
    stream is running and a sudden correction would move every PTS at once.
    Idle, it jumps, so the first stream after a correction starts right. A
    change of grandmaster is another clock entirely: every estimate is
    dropped and the next one is taken at once.

    ``fallback_offset_ns`` is the stored offset of the old scheme. It is used
    only until the network has answered, so an installation that worked keeps
    working while the master is found, and keeps working when no PTP is seen
    at all. Without either, PTS = our uptime: accepted by a listener, played
    as silence.
    """

    #: how far the offset may move per second of monotonic time while in use
    MAX_SLEW_NS_PER_S = 20_000_000
    #: nobody has read the clock for this long -> not in use -> jump, don't slew
    IDLE_NS = 2 * NS

    def __init__(self, master_ip: Optional[str] = None, *, local_ip: Optional[str] = None,
                 mac: Optional[str] = None, lead_ns: int = PTP_LEAD_NS,
                 fallback_offset_ns: Optional[int] = None, domain: int = PTP_DOMAIN,
                 group: str = PTP_GROUP, event_port: int = PTP_EVENT_PORT,
                 general_port: int = PTP_GENERAL_PORT, delay_req_interval_s: float = 1.0,
                 window: int = 16):
        self.lead_ns = int(lead_ns)
        self.master_ip = master_ip
        self.local_ip = local_ip
        self.domain = domain
        self.group, self.event_port, self.general_port = group, event_port, general_port
        self.delay_req_interval_s = delay_req_interval_s
        self.window = window
        self.identity = clock_identity(mac or own_mac())
        self.fallback_offset_ns = None if fallback_offset_ns is None else int(fallback_offset_ns)
        self.grandmaster: Optional[bytes] = None
        self.offset_ns: Optional[int] = None          # what the sender sees, slewed
        self._target_ns: Optional[int] = None         # latest filtered estimate
        self._slew_ref: Optional[int] = None          # monotonic ns of the last slew step
        self._last_read: Optional[int] = None         # monotonic ns of the last at_ns()
        self._unicast: List[Tuple[int, int]] = []     # (rtt_ns, offset_ns), newest last
        self._mcast: List[int] = []                   # T1 - T2 per Sync/Follow_Up pair
        self._t2: Dict[int, int] = {}                 # Sync seq -> local arrival ns
        self._sent: Dict[int, int] = {}               # Delay_Req seq -> T3 local ns
        self._seq = 0
        self._fix = threading.Event()
        self._announced = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.responses = 0
        self.follow_ups = 0
        self.master_changes = 0

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "PtpSlaveClock":
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="ptp-slave", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.0)
        self._thread = None

    def __enter__(self) -> "PtpSlaveClock":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def wait_for_fix(self, timeout: float = 5.0) -> bool:
        """True once the network has given at least one estimate."""
        return self._fix.wait(timeout)

    def wait_for_grandmaster(self, timeout: float = 2.5) -> bool:
        """True once an Announce has named the grandmaster.

        A fix can arrive before the first Announce (Sync comes every second,
        Announce every two), so a caller that wants the identity waits here.
        """
        return self._announced.wait(timeout)

    @property
    def fixed(self) -> bool:
        return self._target_ns is not None

    def status(self) -> dict:
        with self._lock:
            rtts = sorted(r for r, _ in self._unicast)
            return {
                "master_ip": self.master_ip,
                "grandmaster": self.grandmaster.hex() if self.grandmaster else None,
                "fixed": self._target_ns is not None,
                "offset_ns": self.offset_ns,
                "target_ns": self._target_ns,
                "fallback_offset_ns": self.fallback_offset_ns,
                "unicast_samples": len(self._unicast),
                "rtt_ms": (rtts[len(rtts) // 2] / 1e6) if rtts else None,
                "multicast_samples": len(self._mcast),
                "responses": self.responses,
                "follow_ups": self.follow_ups,
                "master_changes": self.master_changes,
            }

    # ------------------------------------------------------------- the clock
    def now_ns(self) -> int:
        return self.at_ns(time.monotonic_ns())

    def at_ns(self, monotonic_ns: int) -> int:
        """PTP time of a moment given in time.monotonic_ns(), plus the lead."""
        return monotonic_ns + self._current_offset(monotonic_ns) + self.lead_ns

    def _current_offset(self, monotonic_ns: int) -> int:
        """Move offset_ns toward the target: at once when idle, no faster than
        MAX_SLEW_NS_PER_S while the clock is being read."""
        with self._lock:
            idle = self._last_read is None or monotonic_ns - self._last_read > self.IDLE_NS
            self._last_read = monotonic_ns
            if self._target_ns is None:
                return self.fallback_offset_ns or 0
            if self.offset_ns is None or self._slew_ref is None or idle:
                self.offset_ns, self._slew_ref = self._target_ns, monotonic_ns
                return self.offset_ns
            elapsed = max(0, monotonic_ns - self._slew_ref)
            self._slew_ref = monotonic_ns
            limit = self.MAX_SLEW_NS_PER_S * elapsed // NS
            diff = self._target_ns - self.offset_ns
            if abs(diff) <= limit:
                self.offset_ns = self._target_ns
            else:
                self.offset_ns += limit if diff > 0 else -limit
            return self.offset_ns

    # -------------------------------------------------------- the estimators
    def _on_follow_up(self, t1_ns: int, t2_local_ns: int) -> None:
        """Multicast pair. Only ever late, so keep a window and trust its maximum."""
        with self._lock:
            self._mcast.append(t1_ns - t2_local_ns)
            del self._mcast[:-self.window]
            self.follow_ups += 1
            if not self._unicast:
                self._target_ns = max(self._mcast)
        self._fix.set()

    def _on_delay_resp(self, seq: int, t4_ns: int, now_local_ns: int) -> None:
        """Unicast answer: T4 is the master's clock when our Delay_Req arrived.
        offset = T4 - T3 - one way, with one way taken as half the round trip.
        A long round trip carries a wrong half, so only the quickest answers in
        the window are believed, and their median is used."""
        t3 = self._sent.pop(seq, None)
        if t3 is None:
            return
        rtt = now_local_ns - t3
        with self._lock:
            self._unicast.append((rtt, t4_ns - t3 - rtt // 2))
            del self._unicast[:-self.window]
            best = min(r for r, _ in self._unicast)
            good = sorted(o for r, o in self._unicast if r <= 2 * best)
            self._target_ns = good[len(good) // 2]
            self.responses += 1
        self._fix.set()

    def _on_announce(self, grandmaster: bytes, src_ip: str) -> None:
        if grandmaster == self.grandmaster:
            return
        with self._lock:
            if self.grandmaster is None:
                log.info("PTP: grandmaster %s at %s", grandmaster.hex(), src_ip)
            else:
                # another clock: every estimate so far is void, and the next
                # one must be taken at once, not slewed toward
                self.master_changes += 1
                log.warning("PTP: grandmaster changed %s -> %s at %s; dropping the offset",
                            self.grandmaster.hex(), grandmaster.hex(), src_ip)
                self._unicast.clear()
                self._mcast.clear()
                self._sent.clear()
                self._target_ns = None
                self.offset_ns = None
                self._fix.clear()
            self.grandmaster = grandmaster
            self.master_ip = src_ip
        self._announced.set()

    # ----------------------------------------------------------------- wire
    def handle(self, data: bytes, src_ip: str, now_local_ns: int) -> None:
        """One datagram, as received at ``now_local_ns`` (time.monotonic_ns)."""
        kind = message_kind(data)
        if kind is None or data[4] != self.domain:
            return
        seq = struct.unpack(">H", data[30:32])[0]
        if kind == SYNC:
            if self.master_ip is None:
                self.master_ip = src_ip
            self._t2[seq] = now_local_ns
            if len(self._t2) > 64:
                for k in sorted(self._t2)[:-32]:
                    self._t2.pop(k, None)
        elif kind == FOLLOW_UP and len(data) >= 44:
            t2 = self._t2.pop(seq, None)
            if t2 is not None:
                self._on_follow_up(ptp_timestamp(data), t2)
        elif kind == DELAY_RESP and len(data) >= 54:
            if data[44:52] == self.identity:
                self._on_delay_resp(seq, ptp_timestamp(data), now_local_ns)
        elif kind == ANNOUNCE and len(data) >= 64:
            self._on_announce(data[53:61], src_ip)

    def _send_delay_req(self, sock: socket.socket) -> None:
        self._seq = (self._seq + 1) & 0xFFFF
        for k in [k for k in self._sent if (self._seq - k) & 0xFFFF > 8]:
            self._sent.pop(k, None)
        pkt = build_delay_req(self.identity, self._seq, self.domain)
        self._sent[self._seq] = time.monotonic_ns()
        try:
            sock.sendto(pkt, (self.master_ip, self.event_port))
        except OSError as e:
            log.warning("PTP: Delay_Req to %s failed: %s", self.master_ip, e)

    def _open(self) -> List[socket.socket]:
        local = self.local_ip or own_ip_toward(self.master_ip or self.group)
        socks = []
        for port in (self.event_port, self.general_port):
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sk.bind(("", port))
            sk.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                          struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton(local)))
            socks.append(sk)
        log.info("PTP: listening on %s:%d/%d via %s as %s", self.group, self.event_port,
                 self.general_port, local, self.identity.hex())
        return socks

    def _run(self) -> None:
        try:
            socks = self._open()
        except OSError as e:
            log.error("PTP: cannot join %s on %d/%d: %s", self.group, self.event_port,
                      self.general_port, e)
            return
        event = socks[0]
        next_req = time.monotonic()
        try:
            while not self._stop.is_set():
                if self.master_ip and time.monotonic() >= next_req:
                    self._send_delay_req(event)
                    next_req = time.monotonic() + self.delay_req_interval_s
                ready, _, _ = select.select(socks, [], [], 0.2)
                for sk in ready:
                    try:
                        data, (ip, _) = sk.recvfrom(2048)
                    except OSError:
                        continue
                    self.handle(data, ip, time.monotonic_ns())
        finally:
            for sk in socks:
                sk.close()


def own_mac() -> str:
    n = uuid.getnode()
    return ":".join("%02x" % ((n >> b) & 0xFF) for b in range(40, -8, -8))


def own_ip_toward(host: str) -> str:
    """The local address a packet to ``host`` would leave from. Nothing is sent."""
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sk.connect((host, 9))
        return sk.getsockname()[0]
    finally:
        sk.close()


# ------------------------------------------------------------------ the tool


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="watch the products' PTP master and this machine's offset to it")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--master", help="the master's IP, if Announce is not to be waited for")
    ap.add_argument("--compare", metavar="FILE", help="a stored offset file (nanoseconds) to compare with")
    ap.add_argument("--mac", help="MAC to derive the clockIdentity from (default: this machine's)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s %(message)s")
    stored = None
    if a.compare:
        with open(a.compare, encoding="utf-8") as f:
            stored = int(f.read().split()[0])
    clock = PtpSlaveClock(a.master, mac=a.mac).start()
    if not clock.wait_for_fix(10.0):
        print("nothing heard on %s:%d/%d in 10 s: no PTP master on this network segment, "
              "or multicast does not reach this interface" % (PTP_GROUP, PTP_EVENT_PORT, PTP_GENERAL_PORT))
        clock.close()
        return 1
    t_end = time.monotonic() + a.seconds
    try:
        while time.monotonic() < t_end:
            time.sleep(2.0)
            st = clock.status()
            off = clock.now_ns() - clock.lead_ns - time.monotonic_ns()
            line = "master %s gm %s  offset %.6f s (%.2f d)  unicast %d/%d rtt %s ms  mcast %d" % (
                st["master_ip"], st["grandmaster"], off / 1e9, off / 1e9 / 86400,
                st["responses"], st["unicast_samples"],
                "%.1f" % st["rtt_ms"] if st["rtt_ms"] is not None else "-", st["follow_ups"])
            if stored is not None:
                line += "  stored-live %+.3f s" % ((stored - off) / 1e9)
            print(line)
    finally:
        clock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
