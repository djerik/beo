"""ListenerRole: another product is the source; we receive its stream.

The sequence, as a listening product runs it:

    the source's ConnectRequest -> <connected>1</connected> -> our own Connect
    with the FPS address, the receiver already bound -> the source's
    connectionId -> _notification-subscribe (device, playing, source) -> its
    FullStatus -> notifications as <message>, PROGRESS every second.

Meanwhile the source subscribes to our renderer and asks the three volume
questions; they are answered whenever they arrive.

Teardown from the source: its Disconnect is answered, the receiver closed,
and then the listener says goodbye itself with a Disconnect and an
unsubscribe. Teardown from us is those same two without being asked.

The session may be one the source opened to us or one we hold open to it; a
product says what it has to say on whichever exists, so both are served alike.

The source looks the listener up in the configuration its network shares
before it accepts the Connect. A listener that is not enrolled there gets an
error with the reason in its text, which is reported through ``on_refused``.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Dict, Optional

from ..beocore import IdCounter, Session, Stanza, tag
from ..fps import AudioFormat, FORMAT_44100, FpsReceiver
from beo.product.identity import ProductIdentity
from ..sources import Catalog, Requests, StatusBuilder, Volume

log = logging.getLogger("beo.networklink.roles.listener")


class Listening:
    """The source we are attached to: its session, its stream, its counters."""

    def __init__(self, session, source_key: str, source_jid: str):
        self.session = session
        self.source_key = source_key
        self.source_jid = source_jid
        self.connection_id = 0
        self.receiver = None
        self.playing = threading.Event()
        self.status = ""                     # the source's FullStatus, as it sent it
        self.notifications: Dict[str, int] = {}


class ListenerRole:
    """Answers a source's ConnectRequest and receives what it then sends."""

    def __init__(self, ident: ProductIdentity, fps_host: str, *,
                 catalog: Optional[Catalog] = None,
                 fmt: AudioFormat = FORMAT_44100,
                 volume_provider: Optional[Callable[[], Volume]] = None,
                 receiver_factory: Callable[..., FpsReceiver] = FpsReceiver,
                 session_factory: Callable[..., Session] = Session,
                 xmpp_port: int = 5222,
                 tap=None,
                 report_interval: float = 10.0,
                 on_started: Optional[Callable[[Listening], None]] = None,
                 on_stopped: Optional[Callable[[Listening], None]] = None,
                 on_refused: Optional[Callable[[Listening, str], None]] = None,
                 on_notification: Optional[Callable[[str, str], None]] = None):
        self.ident = ident
        self.our_jid = ident.full_jid()
        # What the source is told to send to. An address needs no name
        # resolution on the source's side; the source uses it only to get one.
        self.fps_host = fps_host
        self.catalog = catalog
        self.fmt = fmt
        self.volume_provider = volume_provider or (lambda: Volume())
        self.receiver_factory = receiver_factory
        self.session_factory = session_factory
        self.xmpp_port = xmpp_port
        self.tap = tap
        self.report_interval = report_interval
        self.on_started = on_started
        self.on_stopped = on_stopped
        self.on_refused = on_refused
        self.on_notification = on_notification
        # Where the audio goes: an object with write(block), set by whoever
        # has something to play it on. With none the stream is only counted.
        self.sink = None
        self.current: Optional[Listening] = None
        self._dialled: Dict[str, Session] = {}
        self._connects: Dict[int, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- public
    def serve(self, session) -> None:
        """Answer as a listener on this session, whoever opened it."""
        session.on(self._handle)

    def dial(self, ip: str, jid: str = "") -> Optional[Session]:
        """Hold a session open to a product that may become our source.

        Nothing is asked on it. A source that wants us as its listener sends
        its ConnectRequest on a session that already exists.
        """
        with self._lock:
            held = self._dialled.get(ip)
        if held is not None and not held.disconnected.is_set():
            return held
        session = self.session_factory(self.ident, ip, port=self.xmpp_port,
                                       peer_jid=jid or None, ids=IdCounter(),
                                       tap=self.tap, name="networklink-listener-%s" % ip)
        self.serve(session)
        try:
            session.open()
        except OSError as e:
            log.warning("could not open a session to %s: %s", ip, e)
            return None
        with self._lock:
            self._dialled[ip] = session
        log.info("holding a session open to %s", ip)
        return session

    def join(self, ip: str, jid: str) -> bool:
        """Join what the product at ``ip`` is playing. Blocking.

        The join a listener starts itself: it asks the product what it has to
        play and sends its Connect for that, without waiting to be asked.
        """
        session = self.dial(ip, jid)
        if session is None:
            return False
        with self._lock:
            cur = self.current
        if cur is not None and cur.session is session and cur.playing.is_set():
            # Asked again while already there: a second Connect would make
            # the source restart the stream it is sending us.
            return True
        reply = session.request(Requests.experiences(), timeout=8)
        playing = re.search(r"<uniqueSourceId><source>([^<]*)</source><jid>([^<]*)</jid>",
                            reply or "")
        if playing is None:
            log.warning("join: %s has nothing playing to join (%s)", jid,
                        "no answer" if reply is None else reply[:300])
            return False
        log.warning("join: %s plays %s from %s", jid, playing.group(1), playing.group(2))
        self._connect(session, playing.group(1), playing.group(2))
        with self._lock:
            cur = self.current
        return cur is not None and cur.session is session and cur.playing.is_set()

    def leave(self) -> bool:
        """Stop listening, and tell the source so."""
        with self._lock:
            cur, self.current = self.current, None
        if cur is None:
            return False
        self._goodbye(cur)
        self._stop_receiver(cur)
        self._stopped(cur)
        return True

    def close(self) -> None:
        self.leave()
        with self._lock:
            dialled, self._dialled = list(self._dialled.values()), {}
        for s in dialled:
            s.close()

    # ------------------------------------------------------------ inbound
    def _handle(self, session, st: Stanza) -> bool:
        typ, xml = st.type, st.xml

        if "<ConnectRequest" in xml and typ == "set":
            key = st.tag("source") or ""
            jid = st.tag("jid") or session.peer_jid or ""
            log.warning("<< ConnectRequest: %s asks us to play %s", jid, key)
            session.result(st, StatusBuilder.connect_request_reply())
            # The Connect is a request of our own, and its reply arrives on
            # the thread this handler runs on.
            threading.Thread(target=self._connect, args=(session, key, jid), daemon=True,
                             name="networklink-connect-%s" % session.peer_ip).start()
            return True

        if "<_notification-subscribe" in xml and typ == "set":
            kinds = re.findall(r"<item>(\w+)</item>", st.tag("includeKindFilter") or "")
            log.info("<< _notification-subscribe kind=%s", kinds)
            session.result(st, StatusBuilder.subscribe_reply(
                StatusBuilder.renderer_status(self.volume_provider())))
            return True

        if "<_notification-unsubscribe" in xml and typ == "set":
            session.result(st, StatusBuilder.unsubscribe_reply())
            return True

        if "<Disconnect" in xml and typ == "set":
            log.warning("<< Disconnect connectionId=%s", st.tag("connectionId"))
            session.result(st, StatusBuilder.disconnect_reply())
            with self._lock:
                cur = self.current
                if cur is not None and cur.session is session:
                    self.current = None
                else:
                    cur = None
            if cur is not None:
                self._stop_receiver(cur)
                self._goodbye(cur)
                self._stopped(cur)
            return True

        # Three questions with one prefix, so the longer names are tried first.
        if typ == "set" and "<GetVolumeMuted" in xml:
            session.result(st, StatusBuilder.get_volume_muted_reply(self.volume_provider().muted))
            return True
        if typ == "set" and "<GetVolumeRange" in xml:
            v = self.volume_provider()
            session.result(st, StatusBuilder.get_volume_range_reply(v.minimum, v.maximum))
            return True
        if typ == "set" and "<GetVolume" in xml:
            session.result(st, StatusBuilder.get_volume_reply(self.volume_provider().level))
            return True

        if xml.startswith("<message") and "<NOTIFICATION>" in xml:
            self._notified(session, xml)
            return True

        if self.catalog is not None and typ in ("get", "set"):
            if "<Attach" in xml:
                log.info("<< Attach from %s", st.from_jid)
                session.result(st, StatusBuilder.attach_reply(
                    self.catalog.contact_info_xml(self.ident)))
                return True
            if "<GetContactInfo" in xml:
                session.result(st, "<GetContactInfo xmlns='beocore.product.1'>"
                                   + self.catalog.contact_info_xml(self.ident)
                                   + "</GetContactInfo>")
                return True
            if "<Experiences" in xml:
                session.result(st, Catalog.experiences_xml(self.ident, None))
                return True
        return False

    def _notified(self, session, xml: str) -> None:
        kind = tag(xml, "type") or "?"
        with self._lock:
            cur = self.current
        if cur is not None and cur.session is session:
            n = cur.notifications[kind] = cur.notifications.get(kind, 0) + 1
        else:
            n = 1
        # PROGRESS arrives every second; the first of each kind says what the
        # source sends, the rest only that it still does.
        if n == 1:
            log.info("<< %s: %s", kind, xml[:1500])
        if self.on_notification:
            try:
                self.on_notification(kind, xml)
            except Exception:      # noqa: BLE001
                log.exception("the notification handler failed on %s", kind)

    # ---------------------------------------------------------- handshake
    def _connect(self, session, source_key: str, source_jid: str) -> None:
        """Tell the source where to send, then ask what it is playing."""
        new = Listening(session, source_key, source_jid)
        with self._lock:
            old, self.current = self.current, new
        if old is not None:
            self._stop_receiver(old)
            self._stopped(old)

        # The source starts sending before it answers the Connect, so the
        # receiver is bound first.
        try:
            new.receiver = self.receiver_factory(self.fmt).open()
        except OSError as e:
            log.error("no FPS receiver, so no Connect: %s", e)
            self._refused(new, "could not bind the FPS port: %s" % e)
            return
        threading.Thread(target=self._receive, args=(new,), daemon=True,
                         name="networklink-fps-rx").start()

        with self._lock:
            n = self._connects[id(session)] = self._connects.get(id(session), 0) + 1
        new.connection_id = n
        reply = session.request(Requests.connect(source_key, source_jid, self.fps_host),
                                timeout=10)
        if reply is None or Stanza(reply).type == "error":
            # <text> carries a namespace attribute, which a bare tag match misses.
            m = re.search(r"<text\b[^>]*>(.*?)</text>", reply or "", re.S)
            reason = m.group(1) if m else ("error without a text" if reply else "no answer")
            log.error("the source refused our Connect: %s", reason)
            self._stop_receiver(new)
            self._refused(new, reason)
            return
        log.warning("Connect accepted by %s, connectionId=%s; listening on FPS://%s",
                    source_jid, tag(reply, "connectionId"), self.fps_host)

        status = session.request(Requests.subscribe_source(), timeout=10)
        if status is None or "FullStatus" not in status:
            log.warning("the source's FullStatus did not arrive")
        else:
            log.info("<< the source's FullStatus:\n%s", status[:4000])
            new.status = status
        new.playing.set()
        if self.on_started:
            self.on_started(new)

    def _goodbye(self, cur: Listening) -> None:
        """A listener's own farewell: Disconnect, then unsubscribe."""
        s = cur.session
        try:
            if cur.connection_id:
                s.send(Requests.disconnect(cur.connection_id))
            s.send(Requests.unsubscribe())
        except Exception as e:      # noqa: BLE001
            log.warning("goodbye to %s failed: %s", cur.source_jid, e)

    # -------------------------------------------------------------- audio
    def _receive(self, cur: Listening) -> None:
        rx = cur.receiver
        last = time.monotonic()
        first = True
        for blk in rx.blocks():
            if first:
                first = False
                log.warning("FPS: first datagram from %s, %d bytes of audio", blk.peer, len(blk.pcm))
            sink = self.sink
            if sink is not None:
                try:
                    sink.write(blk)
                except Exception:      # noqa: BLE001
                    log.exception("the audio sink failed; the stream is still received")
                    self.sink = None
            now = time.monotonic()
            if now - last >= self.report_interval:
                last = now
                st = rx.stats()
                log.info("FPS: %d datagrams, %d gaps, %d markers, heartbeat %d bytes from %s",
                         st["datagrams"], st["gaps"], st["markers"],
                         st["heartbeat_bytes"], st["tcp_peer"])

    def _stop_receiver(self, cur: Listening) -> None:
        rx, cur.receiver = cur.receiver, None
        if rx is None:
            return
        st = rx.stats()
        log.warning("FPS: closed after %d datagrams (%.1f s of audio), %d gaps",
                    st["datagrams"], st.get("audio_seconds", 0.0), st["gaps"])
        rx.close()

    # ------------------------------------------------------------ cleanup
    def _refused(self, cur: Listening, reason: str) -> None:
        with self._lock:
            if self.current is cur:
                self.current = None
        if self.on_refused:
            self.on_refused(cur, reason)

    def _stopped(self, cur: Listening) -> None:
        if cur.playing.is_set() and self.on_stopped:
            cur.playing.clear()
            self.on_stopped(cur)


__all__ = ["ListenerRole", "Listening"]
