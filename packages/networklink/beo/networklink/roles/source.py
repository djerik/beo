"""SourceRole: we are the source; an ASE speaker becomes our listener.

The drive sequence, as a product runs it:

    Attach(role=0 + contactInfo) -> ConnectRequest -> wait for the listener's
    Connect -> FPS start BEFORE the Connect result -> FullStatus reply on its
    _notification-subscribe -> negotiate_volume (unsub, sub renderer,
    GetVolume x3, all in one go, then WAIT for all replies) -> opening pushes
    (SEC, NOW_PLAYING, PROGRESS preparing 0, PROGRESS play 0, NOW_PLAYING)
    -> PROGRESS every second.

Teardown, in the order a source uses: unsubscribe, SEC stop,
NOW_PLAYING_ENDED, PROGRESS stop, SOURCE empty; then Disconnect, FPS stop,
</stream:stream>.

Three orderings must not break: FPS BEFORE the Connect reply; the opening
AFTER the listener's FullStatus; the four volume stanzas TOGETHER.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from ..beocore import IdCounter, Session, Stanza
from ..discovery import Peer
from ..fps import FpsClock, FpsSender, PORT, SESSION_FIELD, STREAM_START_NS
from beo.product.identity import ProductIdentity
from ..sources import (Catalog, Experience, NowPlaying, Requests, SourceDescriptor,
                       StatusBuilder, Volume)

log = logging.getLogger("beo.networklink.roles.source")


class Listener:
    """One listener: its session, its FPS stream and its progress clock."""

    def __init__(self, peer: Peer, session, audio):
        self.peer = peer
        self.session = session
        self.audio = audio
        self.fps = None
        self.connection_id = 0
        self.streaming = threading.Event()
        self.disconnected = threading.Event()
        self.goodbye = threading.Event()        # the listener's own Disconnect has arrived
        self.play_t0: Optional[float] = None
        self.position = 0
        self.joined = False
        self._progress_stop = threading.Event()

    @property
    def jid(self) -> str:
        return self.peer.jid

    def stats(self):
        return self.fps.stats() if self.fps else None


class SourceRole:
    """Owns one Session and one FpsSender per listener, plus the Experience."""

    def __init__(self, ident: ProductIdentity, catalog: Catalog, clock: FpsClock, *,
                 fps_port: int = PORT, stream_start_ns: int = STREAM_START_NS,
                 session_field: bytes = SESSION_FIELD,
                 volume_provider: Optional[Callable[[], Volume]] = None,
                 now_playing: Optional[NowPlaying] = None,
                 resolve_fps_host: bool = True,
                 progress_interval: float = 1.0,
                 xmpp_port: int = 5222,
                 tap=None,
                 fps_factory: Callable[..., FpsSender] = FpsSender,
                 session_factory: Callable[..., Session] = Session,
                 on_listener_joined: Optional[Callable[[Peer], None]] = None,
                 on_listener_left: Optional[Callable[[Peer], None]] = None,
                 on_renderer_volume: Optional[Callable[[Peer, Volume], None]] = None,
                 goodbye_wait: float = 3.0):
        self.ident = ident
        # How long unexpand() keeps the session open for the listener's own
        # goodbye after ours. 0 closes at once, as before.
        self.goodbye_wait = goodbye_wait
        self.our_jid = ident.full_jid()
        self.catalog = catalog
        self.clock = clock
        self.fps_port = fps_port
        self.stream_start_ns = stream_start_ns
        self.session_field = session_field
        # Our own volume, as reported in FullStatus/GetVolume. The application
        # reads it from its product surface; 35/unmuted is the fallback.
        self.volume_provider = volume_provider or (lambda: Volume())
        self.now_playing = now_playing
        self.resolve_fps_host = resolve_fps_host
        self.progress_interval = progress_interval
        self.xmpp_port = xmpp_port
        self.tap = tap
        self.fps_factory = fps_factory
        self.session_factory = session_factory
        self.on_listener_joined = on_listener_joined
        self.on_listener_left = on_listener_left
        self.on_renderer_volume = on_renderer_volume
        self.experience: Optional[Experience] = None
        self._listeners: Dict[str, Listener] = {}
        self._stale: List[Session] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------- public
    def listeners(self) -> List[Peer]:
        with self._lock:
            return [l.peer for l in self._listeners.values()]

    def listener(self, jid: str) -> Optional[Listener]:
        with self._lock:
            return self._listeners.get(jid)

    def expand(self, peer: Peer, source: SourceDescriptor, audio, *,
               wait_peer: float = 6.0, connect_timeout: float = 10.0) -> Optional[Listener]:
        """Make ``peer`` play ``source`` from ``audio``. Blocking; returns the
        Listener when the stream runs and the opening has been pushed, else None."""
        with self._lock:
            if peer.jid in self._listeners:
                return self._listeners[peer.jid]
            if self.experience is None:
                self.experience = Experience(source, self.our_jid, now_playing=self.now_playing)
            exp = self.experience
        if exp.source.key != source.key:
            log.warning("expand: %s asked for %s while %s is playing — a expand shares "
                        "the source, it does not switch it", peer.jid, source.key, exp.source.key)

        # One session per listener, ids 51901.. for the requests without a fixed id.
        session = self.session_factory(self.ident, peer.ip, port=self.xmpp_port,
                                       ids=IdCounter(51900), tap=self.tap,
                                       name="networklink-source-%s" % peer.ip)
        lis = Listener(peer, session, audio)
        session.on(lambda s, st: self._handle(lis, st))
        try:
            session.open()
        except OSError as e:
            log.warning("could not open a session to %s: %s", peer.ip, e)
            return None
        with self._lock:
            self._listeners[peer.jid] = lis

        # peer_jid is set only when the peer sends a STANZA. A listener sends
        # only the stream header and then keeps quiet until it has something
        # to say. We know its jid: it is the listener that was asked for, so an
        # open connection is enough to go on.
        if not session.wait_peer(wait_peer) and session.sock is not None:
            log.info("%s kept quiet — using the known jid %s", peer.ip, peer.jid)
            session.peer_jid = peer.jid
        if not session.peer_jid:
            log.warning("%s did not answer on 5222", peer.name or peer.ip)
            self._drop(lis)
            return None

        # Present the source first. Without it the receiver does not know it,
        # and a direct selection of it answers 400.
        self.send_attach(lis)
        r = session.request(Requests.connect_request(exp.source.key, exp.source_jid),
                            iq_id="51002")
        ok = r is not None and "<connected>1</connected>" in r
        log.warning("expand to %s: ConnectRequest %s", peer.name or peer.jid,
                    "accepted" if ok else "refused")
        if ok and lis.streaming.wait(connect_timeout):
            # The renderer is taken over AFTER the stream runs, not before:
            # the four stanzas follow the first audio, they do not precede it.
            try:
                self.negotiate_volume(lis)
            except Exception as e:
                log.warning("could not take over the listener's renderer: %s", e)
            # Tell the listener what is playing. Without it it reports
            # NOW_PLAYING_ENDED and never starts rendering.
            try:
                self.push_opening(lis)
            except Exception as e:
                log.warning("could not announce playback: %s", e)
            lis.joined = True
            exp.listeners.append(peer.jid)
            threading.Thread(target=self._progress_loop, args=(lis,), daemon=True,
                             name="networklink-progress-%s" % peer.ip).start()
            log.warning("%s now plays %s", peer.name or peer.jid, exp.source.name)
            if self.on_listener_joined:
                self.on_listener_joined(peer)
            return lis
        if ok:
            log.error("%s never sent <Connect> — no stream to start", peer.name or peer.jid)
        self._drop(lis)
        return None

    def unexpand(self, peer: Peer) -> bool:
        """Close the experience like a real source: nedluk(), Disconnect, FPS stop, </stream>."""
        with self._lock:
            lis = self._listeners.pop(peer.jid, None)
        if lis is None:
            return False
        lis._progress_stop.set()
        self.teardown_stanzas(lis)
        try:
            if lis.session.peer_jid:
                lis.session.request(Requests.disconnect(lis.connection_id),
                                    iq_id="51003", timeout=3)
        except Exception:
            pass
        self._stop_fps(lis)
        lis.streaming.clear()
        # The listener answers our Disconnect and then says goodbye itself: its
        # own Disconnect and an unsubscribe, within milliseconds. A real source
        # keeps the session until they have arrived; a listener whose goodbye
        # meets a closed socket wakes the source by MAC and delivers the
        # Disconnect at the start of the next session instead. Give the goodbye
        # a moment, and the unsubscribe that follows it a little more.
        if self.goodbye_wait > 0 and not lis.goodbye.is_set():
            lis.goodbye.wait(self.goodbye_wait)
        if self.goodbye_wait > 0:
            time.sleep(min(0.3, self.goodbye_wait))
        lis.session.close()
        self._left(lis)
        log.info("listener %s removed", peer.jid)
        return True

    def set_state(self, state: str) -> None:
        """play | pause | stop. pause/play push PROGRESS and send the marker
        (n+1 = MuteAllSinks, n+2 = Unmute); PCM continues unchanged. stop
        runs the full teardown for every listener."""
        if state == "stop":
            for p in self.listeners():
                self.unexpand(p)
            return
        if state not in ("play", "pause"):
            raise ValueError("state must be play, pause or stop, not %r" % state)
        exp = self.experience
        if exp is not None and exp.state == state:
            return                      # the marker is a toggle: only on a real transition
        with self._lock:
            active = [l for l in self._listeners.values() if l.streaming.is_set()]
        if exp is not None:
            exp.state = state
        for lis in active:
            try:
                self.push_progress(lis, state)
            except Exception as e:
                log.warning("PROGRESS %s to %s failed: %s", state, lis.jid, e)
            if lis.fps is not None:
                if state == "pause":
                    lis.fps.mute()
                else:
                    lis.fps.unmute()

    def set_now_playing(self, np: NowPlaying) -> None:
        self.now_playing = np
        exp = self.experience
        if exp is None:
            return
        exp.now_playing = np
        with self._lock:
            active = [l for l in self._listeners.values() if l.streaming.is_set()]
        for lis in active:
            self.push_now_playing(lis)

    def close(self) -> None:
        """Tear down every listener and close leftover sessions."""
        for p in self.listeners():
            self.unexpand(p)
        with self._lock:
            stale, self._stale = self._stale, []
        for s in stale:
            s.close()

    # --------------------------------------------------------- handshake
    def send_attach(self, lis: Listener) -> bool:
        """Present ourselves, as a product does. <role> is mandatory."""
        try:
            r = lis.session.request(
                Requests.attach(self.catalog.contact_info_xml(self.ident)),
                iq_id="50900", timeout=10)
        except Exception as e:
            log.warning("Attach failed: %s", e)
            return False
        ok = r is not None and "contactInfo" in r
        log.warning(">> Attach %s (source %s)", "accepted" if ok else "refused",
                    self.experience.source.key if self.experience else "?")
        return ok

    def negotiate_volume(self, lis: Listener) -> Optional[Volume]:
        """Take over the listener's renderer, as a source does.

        Subscription to kind=renderer and the three questions about audio,
        sent TOGETHER, in the same millisecond, and only once the stream
        runs. It only ASKS; a source never sets the listener's volume.

        All four go out first, the replies are read afterwards. First an
        UNSUBSCRIBE: a product always resets before it subscribes, both as
        source and as listener. A subscription laid on top of an old one
        would report twice.
        """
        s = lis.session
        s.send(Requests.unsubscribe(), iq_id="50909")
        s.send(Requests.subscribe_renderer(), iq_id="50910")
        s.send(Requests.get_volume(), iq_id="50911")
        s.send(Requests.get_volume_muted(), iq_id="50912")
        s.send(Requests.get_volume_range(), iq_id="50913")
        # A source pushes its opening once it has the listener's FullStatus
        # (the reply to 50910) and the three audio replies. So do we.
        s.reply("50909")
        full = s.reply("50910") or ""
        vol = s.reply("50911") or ""
        mut = s.reply("50912") or ""
        rng = s.reply("50913") or ""
        if "FullStatus" not in full:
            log.warning("the listener's FullStatus did not arrive (%d chars)", len(full))

        def val(x, t):
            m = re.search("<" + t + ">([^<]*)</" + t + ">", x)
            return m.group(1) if m else "?"
        log.warning("the listener's audio: level=%s mute=%s range=%s-%s",
                    val(vol, "volume"), val(mut, "muted"),
                    val(rng, "minimum"), val(rng, "maximum"))
        try:
            v = Volume(int(val(vol, "volume")), val(mut, "muted") == "1",
                       int(val(rng, "minimum")), int(val(rng, "maximum")))
        except ValueError:
            return None
        if self.on_renderer_volume:
            self.on_renderer_volume(lis.peer, v)
        return v

    # ------------------------------------------------------------ inbound
    def _handle(self, lis: Listener, st: Stanza) -> bool:
        """The source-side stanza handler (``BeolinkSource._handle``)."""
        session = lis.session
        typ, xml = st.type, st.xml

        # 1) The receiver reports its FPS endpoint. Start the audio BEFORE the
        #    reply — a source sends before it acks.
        if "<Connect" in xml and "<ConnectRequest" not in xml and typ == "set":
            info = st.tag("connectionInfo") or ""
            host = info.replace("FPS://", "").strip()
            log.warning("<< the listener's own Connect, verbatim:\n%s", xml[:2000])
            log.warning("<< Connect: sourceId=%s connectionInfo=%s", st.tag("sourceId"), info)
            self._start_fps(lis, host)
            lis.connection_id += 1
            cid = session.peer_jid or self.our_jid
            session.result(st, StatusBuilder.connect_reply(cid))
            log.info(">> Connect answered with connectionId=%s", cid)
            return True

        # 2) The receiver wants to know what the source plays. Answer with a
        #    real FullStatus; without it the receiver never renders.
        if "<_notification-subscribe" in xml and typ == "set":
            kinds = re.findall(r"<item>(\w+)</item>", st.tag("includeKindFilter") or "")
            log.warning("<< _notification-subscribe kind=%s — answering with FullStatus", kinds)
            exp = self.experience
            session.result(st, StatusBuilder.subscribe_reply(
                StatusBuilder.full_status(self.ident, exp, self.volume_provider())))
            return True

        if "<_notification-unsubscribe" in xml and typ == "set":
            session.result(st, StatusBuilder.unsubscribe_reply())
            return True

        if "<Disconnect" in xml and typ == "set":
            # A Disconnect from an EARLIER session can arrive after we opened
            # the next: the receiver cleans up only when it sees the new
            # connection. connectionId does not help — both are 1, because
            # the counter is per session. The order does: if we have not
            # started streaming yet, it does not concern us.
            if not lis.streaming.is_set():
                log.info("<< Disconnect before our stream — left over from the previous "
                         "session, acknowledged and ignored")
                session.result(st, StatusBuilder.disconnect_reply())
                lis.goodbye.set()
                return True
            log.warning("<< Disconnect connectionId=%s", st.tag("connectionId"))
            session.result(st, StatusBuilder.disconnect_reply())
            lis.goodbye.set()
            self._stop_fps(lis)
            lis.streaming.clear()
            lis.disconnected.set()
            self._peer_disconnected(lis)
            return True

        # Three questions with one prefix. "<GetVolume" alone also matched
        # GetVolumeMuted and GetVolumeRange, and both got a <GetVolume> answer.
        # The shapes are the ones a product uses in its own replies:
        # <muted>0</muted> and <range><minimum>/<maximum>.
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

        # The receiver's own notifications. STREAMING_STATUS is the one we
        # chase: it is the only thing it reports about actually rendering.
        if "notification" in xml.lower() and typ == "set":
            types = re.findall(r"<type>(\w+)</type>", xml)
            log.warning("<< NOTIFICATION %s", types or xml[:200])
            for t in ("STREAMING_STATUS", "SOURCE", "PROGRESS_INFORMATION"):
                if f"<type>{t}</type>" in xml:
                    log.warning("   %s", xml[:500])
                    break
            session.result(st, StatusBuilder.notification_ack())
            return True

        # GetContactInfo is the ONLY way a product fetches another product's
        # source catalogue. Without a branch here the asker got
        # feature-not-implemented, and its record of us said source: [].
        if "<GetContactInfo" in xml and typ in ("get", "set"):
            log.warning("<< GetContactInfo — someone asks for our catalogue")
            exp = self.experience
            if exp is None:
                session.result(st, "<GetContactInfo xmlns='beocore.product.1'>"
                                   + self.catalog.contact_info_xml(self.ident)
                                   + "</GetContactInfo>")
            else:
                session.result(st, Catalog.get_contact_info_reply_xml(self.ident, exp))
            return True

        # OneWayJoin: the speaker asks every known device what it has to play.
        if "<Experiences" in xml and typ in ("get", "set"):
            log.warning("<< Experiences — answering with our experience")
            session.result(st, Catalog.experiences_xml(self.ident, self.experience))
            return True
        return False

    # -------------------------------------------------------------- audio
    def _start_fps(self, lis: Listener, host: str) -> None:
        ip = lis.peer.ip
        if host and self.resolve_fps_host:
            try:
                ip = socket.gethostbyname(host)
                log.info("FPS: %s -> %s", host, ip)
            except OSError:
                # .local needs mDNS, and Windows does not always have it. The
                # receiver is the same machine we already have a session to,
                # so the name is a convenience, not information we lack.
                log.info("FPS: could not resolve %s — using %s", host, ip)
        # The stream clock starts with a small lead, not at 9 s (STREAM_START_NS).
        lis.fps = self.fps_factory(ip, lis.audio.format, self.clock, port=self.fps_port,
                                   stream_start_ns=self.stream_start_ns,
                                   session_field=self.session_field)
        lis.play_t0 = time.time()
        lis.fps.start(lis.audio)
        lis.streaming.set()

    def _stop_fps(self, lis: Listener) -> None:
        fps, lis.fps = lis.fps, None
        if fps is not None:
            try:
                fps.stop()
            except Exception:
                pass

    # ------------------------------------------------------------- pushes
    def push_experience_changed(self, lis: Listener, state: str = "play",
                                listeners: Optional[List[str]] = None) -> None:
        """SOURCE_EXPERIENCE_CHANGED. Default listener list: the listener
        FIRST, the source itself LAST."""
        if listeners is None:
            listeners = [lis.session.peer_jid, self.our_jid]
        lis.session.push(StatusBuilder.experience_changed(self.ident, self.experience,
                                                          state, listeners))

    def push_now_playing(self, lis: Listener) -> None:
        """NOW_PLAYING_NET_RADIO — only a radio source sends it."""
        if self.experience.source.key != "beoradio":
            return
        lis.session.push(StatusBuilder.now_playing(self.experience))

    def push_progress(self, lis: Listener, state: str = "play",
                      position: Optional[int] = None) -> None:
        if lis.play_t0 is None:
            lis.play_t0 = time.time()
        lis.position = (int(time.time() - lis.play_t0)
                        if position is None else position)
        lis.session.push(StatusBuilder.progress(state, lis.position, self.experience.source.key))

    def push_opening(self, lis: Listener) -> None:
        """The source's opening towards the listener, in a source's own order.

        All within the same second and ONLY after its FullStatus has been sent:

            SOURCE_EXPERIENCE_CHANGED   the whole source, listener = [the listener, us]
            NOW_PLAYING_NET_RADIO
            PROGRESS_INFORMATION        preparing, position 0
            PROGRESS_INFORMATION        play,      position 0
            NOW_PLAYING_NET_RADIO       again
            PROGRESS_INFORMATION        play, 1 — and then every second

        The source restarts its stream at join (same in FullStatus), and the
        position counts from zero from there — so play_t0 is reset here.
        No empty SOURCE at the opening: a source sends it only at stop.
        """
        lis.play_t0 = time.time()
        self.experience.state = "play"
        self.push_experience_changed(lis)
        self.push_now_playing(lis)
        self.push_progress(lis, "preparing", position=0)
        self.push_progress(lis, "play", position=0)
        self.push_now_playing(lis)

    def teardown_stanzas(self, lis: Listener) -> None:
        """Close the experience like a source does, in its order:

            _notification-unsubscribe      (iq set)
            SOURCE_EXPERIENCE_CHANGED      (the experience as it is now, state stop,
                                            listener list = ONLY the listener told)
            NOW_PLAYING_ENDED              (empty data)
            PROGRESS_INFORMATION           (only <state>stop</state>)
            SOURCE                         (empty data)

        and only then </stream:stream>. A product that just vanishes leaves
        the listener with an experience it has to find out is dead by itself.
        """
        s = lis.session

        def unsubscribe():
            s.request(Requests.unsubscribe(), timeout=4)          # id 51901..

        def sec_stop():
            self.push_experience_changed(lis, state="stop", listeners=[s.peer_jid])

        def ended():
            s.push(StatusBuilder.ended())

        def progress_stop():
            s.push(StatusBuilder.progress_stop())

        def source_empty():
            s.push(StatusBuilder.source_empty())

        for step in (unsubscribe, sec_stop, ended, progress_stop, source_empty):
            try:
                step()
            except Exception as e:
                log.warning("teardown: %s failed: %s", getattr(step, "__name__", "?"), e)

    def _progress_loop(self, lis: Listener) -> None:
        """PROGRESS_INFORMATION every second with playback time — not uptime.
        The number starts low and counts one at a time."""
        while not lis._progress_stop.wait(self.progress_interval):
            if not lis.streaming.is_set():
                continue
            try:
                self.push_progress(lis, self.experience.state if self.experience else "play")
            except Exception:
                return

    # ------------------------------------------------------------ cleanup
    def _drop(self, lis: Listener) -> None:
        """A failed expand: close the session and forget the listener."""
        with self._lock:
            self._listeners.pop(lis.jid, None)
        lis._progress_stop.set()
        self._stop_fps(lis)
        lis.session.close()
        with self._lock:
            if not self._listeners:
                self.experience = None

    def _peer_disconnected(self, lis: Listener) -> None:
        """The listener sent Disconnect after our stream had started: it left.
        The session is kept open until the peer closes it (it may still send
        its unsubscribe); ``close()`` reaps it."""
        with self._lock:
            self._listeners.pop(lis.jid, None)
            self._stale.append(lis.session)
        lis._progress_stop.set()
        self._left(lis)

    def _left(self, lis: Listener) -> None:
        exp = self.experience
        if exp is not None and lis.jid in exp.listeners:
            exp.listeners.remove(lis.jid)
        with self._lock:
            if not self._listeners:
                # Last listener gone. A real product goes idle and reports no
                # source; the next expand starts a new experience (new lastUsed).
                self.experience = None
        if lis.joined and self.on_listener_left:
            self.on_listener_left(lis.peer)


__all__ = ["SourceRole", "Listener"]
