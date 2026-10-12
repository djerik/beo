"""The streaming side of the product: this one plays to other products.

beo.networklink's SourceRole behind the app's expand and unexpand (add /
remove / clear / progress / listeners). main hands it to the facade, which
builds it when started with --beolink.

Audio: one ALSA capture of the machine behind the product (the ADC on the
Master Link audio pairs), fanned out to one FPS stream per listener. A classic
machine only puts audio on the bus after the link-room request the facade
sends on expand (request_ml_audio).

PTP: the listener plays only when the PTS in the FPS header is in the network's
PTP time, which beo.networklink.PtpSlaveClock reads off the network. An offset
derived from the listener's own fpsd log and stored in PTP_OFFSET_FILE is the
fallback.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Iterable, List, Optional

from beo.networklink.audio import AlsaCapture, Fanout, ToneSource
from beo.networklink.discovery import Peer
from beo.networklink.fps import AudioFormat, OffsetPtpClock, MonotonicClock, RATE_HZ
from beo.product.identity import ProductIdentity
from beo.networklink.sources import (Catalog, NowPlaying, SOURCES, STANDARD_SOURCES,
                                     SourceDescriptor, Volume)
from beo.networklink.roles.source import SourceRole

log = logging.getLogger("beo-adapter.streaming")

PTP_OFFSET_FILE = os.environ.get("BEO_PTP_OFFSET", "/etc/beo/ptp_offset.ns")


def read_ptp_offset(path: str = PTP_OFFSET_FILE) -> Optional[int]:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def make_clock(path: str = PTP_OFFSET_FILE, ptp_wait_s: float = 4.0):
    """The clock the FPS sender stamps every datagram with.

    First choice: the products' own PTP, read off the network continuously
    (beo.networklink.ptp): a slave clock follows the master through reboots
    and elections, where a stored offset is a photograph of one moment.

    The stored offset, if any, is the fallback until the network answers, and
    for as long as it never does. BEO_PTP=0 restores the old scheme outright.
    """
    off = read_ptp_offset(path)
    if os.environ.get("BEO_PTP", "1").strip().lower() not in ("0", "no", "off", "false"):
        from beo.networklink.ptp import PtpSlaveClock
        clock = PtpSlaveClock(fallback_offset_ns=off).start()
        if clock.wait_for_fix(ptp_wait_s):
            clock.wait_for_grandmaster()
            st = clock.status()
            log.info("PTP: following grandmaster %s at %s, offset %.3f s", st["grandmaster"] or "?",
                     st["master_ip"], (st["target_ns"] or 0) / 1e9)
            if off is not None:
                log.info("PTP: stored offset %.3f s differs from the network by %+.3f s",
                         off / 1e9, (off - (st["target_ns"] or 0)) / 1e9)
        elif off is None:
            log.error("PTP: no master heard in %.0f s and no stored offset in %s — listeners will "
                      "accept the stream but play SILENCE until the network's PTP appears", ptp_wait_s, path)
        else:
            log.warning("PTP: no master heard in %.0f s; using the stored offset %.3f s from %s "
                        "until the network's PTP appears", ptp_wait_s, off / 1e9, path)
        return clock
    if off is None:
        log.error("no PTP offset in %s and BEO_PTP is off — listeners will accept the stream but "
                  "play SILENCE. Stream once, then store "
                  "FpsdLog.parse(NsdkClient(ip).fetch_logs()).estimate_offset_ns() there", path)
        return MonotonicClock()
    log.info("PTP offset %.3f s from %s", off / 1e9, path)
    return OffsetPtpClock(off)


class BeolinkOut:
    """The listeners, as the app sees them, with real audio behind."""

    def __init__(self, jid: str, name: str, peers: List[dict], device: str = "hw:0,0", *,
                 mac: Optional[str] = None, product_type: str = "",
                 tone: bool = False, clock=None,
                 sources: Iterable[SourceDescriptor] = STANDARD_SOURCES):
        from beo.mozart import facade
        self.jid, self.name = jid, name
        self.by_jid: Dict[str, dict] = {p["jid"]: p for p in peers}
        # What the product lists, and of that what it can play to a listener:
        # the first of those is what plays when nothing says otherwise.
        self.sources = tuple(sources)
        self.playable = [s for s in self.sources if SOURCES.get(s.key) is s]
        if not self.playable:
            raise ValueError("none of the product's sources can be played to a listener: %s"
                             % ", ".join(s.key for s in self.sources))
        self.ident = ProductIdentity.from_jid(jid, name, mac or facade.MAC, product_type=product_type)
        self.fmt = AudioFormat(RATE_HZ)         # the ADC delivers the link's one rate directly
        self.tone = tone
        self.capture = None if tone else Fanout(AlsaCapture(device, self.fmt))
        self.role = SourceRole(self.ident, Catalog(self.sources), clock or make_clock(),
                               volume_provider=lambda: Volume(),
                               on_listener_joined=lambda p: log.warning("%s plays %s now", p.name or p.jid,
                                                                        self.name),
                               on_listener_left=lambda p: log.info("%s left", p.name or p.jid))
        self._lock = threading.Lock()
        self._pending: Dict[str, threading.Thread] = {}
        # What the listeners are told is playing. Without this the
        # listeners get an empty now-playing item.
        self.role.now_playing = self._now_playing()
        # callable(jid, joined). add() answers at once, but whether the speaker
        # ends up playing is known seconds later, in the expand thread. The
        # facade sets this so the app is told "joined" when it is true.
        self.on_expand_result = None

    def _report(self, listener_jid: str, joined: bool) -> None:
        if self.on_expand_result is None:
            return
        try:
            self.on_expand_result(listener_jid, joined)
        except Exception:      # noqa: BLE001
            log.exception("reporting the expand to %s failed", listener_jid)

    def source(self) -> SourceDescriptor:
        """The source that plays now, under the product's name: the one the
        machine is on, or the first the product can play."""
        from beo.mozart import facade
        slot = facade._active.get("source")
        for s in self.playable:
            if s.key == slot:
                return s.named(self.name)
        return self.playable[0].named(self.name)

    def _peer(self, listener_jid: str) -> Optional[dict]:
        """The peer as the network knows it NOW, not as it did at startup.

        The list handed to the constructor is whatever mDNS had answered in the
        first instant of the process, which is nothing. The facade's
        directory is asked each time; what it said
        last is kept, so a listener that has dropped out of mDNS can still be
        told to stop.
        """
        from beo.mozart import facade
        live = facade.peer_by_jid(listener_jid)
        if live and live.get("ipAddress"):
            self.by_jid[listener_jid] = live
        return self.by_jid.get(listener_jid)

    # ---- the interface facade uses -------------------------------------
    def listeners(self) -> List[str]:
        return sorted(p.jid for p in self.role.listeners())

    def add(self, listener_jid: str) -> bool:
        peer_d = self._peer(listener_jid)
        if peer_d is None:
            log.warning("unknown listener %s: nothing by that jid has been heard on "
                        "the network", listener_jid)
            return False
        # SourceRole lists a listener from the moment its session opens, which
        # is before it plays: a second tap while the first is still under way
        # must not be reported as joined. The thread already running will.
        with self._lock:
            under_way = listener_jid in self._pending
        if under_way:
            return True
        if listener_jid in self.listeners():
            self._report(listener_jid, True)
            return True
        peer = Peer(jid=listener_jid, ip=peer_d["ipAddress"], name=peer_d.get("friendlyName", ""),
                    product_type=peer_d.get("productType", ""))
        # No check of the listener's own rate: the rate a product SENDS as a
        # source is not what it accepts, and every listener accepts 44 100.
        audio = ToneSource(self.fmt, 440.0, 0.2) if self.tone else self.capture.subscribe(listener_jid)

        def drive():
            joined = False
            try:
                joined = self.role.expand(peer, self.source(), audio) is not None
                if not joined:
                    log.warning("expand to %s failed", peer.name)
                    if self.capture:
                        self.capture.unsubscribe(listener_jid)
            except Exception:
                log.exception("expand to %s crashed", peer.name)
            finally:
                with self._lock:
                    self._pending.pop(listener_jid, None)
            self._report(listener_jid, joined)

        t = threading.Thread(target=drive, name="expand-" + peer.ip, daemon=True)
        with self._lock:
            self._pending[listener_jid] = t
        t.start()
        return True

    def remove(self, listener_jid: str) -> bool:
        peer_d = self._peer(listener_jid)
        if peer_d is None:
            return False
        peer = Peer(jid=listener_jid, ip=peer_d["ipAddress"], name=peer_d.get("friendlyName", ""))
        ok = self.role.unexpand(peer)
        if self.capture:
            self.capture.unsubscribe(listener_jid)
        return ok

    def clear(self) -> None:
        for jid in self.listeners():
            self.remove(jid)

    def _now_playing(self) -> NowPlaying:
        """Our own words for the now-playing item: the product's name and the
        disc and track the facade shows. No genre, image or station id — the
        shape stays the one the listeners know, the radio content does not."""
        from beo.mozart import facade
        cd, track = facade._active.get("cd"), facade._active.get("track")
        plain = facade.plain_source(facade._active.get("source"))
        if plain is not None:
            text = plain.name
        else:
            text = "CD %s · track %s" % (cd, track) if cd and track else "CD"
        return NowPlaying(name=self.name, genre="", country="", languages="",
                          image_url="", image_size="", image_mediatype="",
                          live_description=text, station_id="")

    def progress(self) -> None:
        """Called by the facade's ticker. PROGRESS itself is pushed by the role
        every second; here the now-playing text follows disc and track."""
        np = self._now_playing()
        if np != self.role.now_playing:
            self.role.set_now_playing(np)

    def set_state(self, state: str) -> None:
        """play | pause — mirrors the app's transport commands to the listeners."""
        if self.role.listeners():
            self.role.set_state(state)
