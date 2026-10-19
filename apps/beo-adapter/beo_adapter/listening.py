"""The listening side of the product: another product plays to this one.

beo.networklink's ListenerRole behind the app's join and leave, plus the port
other products open their sessions on. When a stream starts, the machine
behind the product is asked to play a network source; once it has agreed, the
stream goes out on the sound card, which is what feeds the machine. A product
with an output of its own plays the stream there as well, and needs no
machine to do it. A browser listening on the product's page hears it too.

It is off unless the configuration asks for it. With the port open a product
may choose to talk to us on a session of its own instead of the one we opened
to it as its source, and the source side answers only on its own sessions.

A source accepts a listener only if it finds it in the configuration its
network shares. When asked to, the product sees to its own entry there, the
way a product does: it reads the document and adds itself if it is missing.
That is a change to something every product on the network holds, so it has
a setting of its own.
"""
from __future__ import annotations

import html
import logging
import re
import threading
from dataclasses import replace
from typing import Callable, Iterable, Optional

from beo.mozart import facade
from beo.networklink import configuration
from beo.networklink.audio import AlsaPlayback
from beo.networklink.beocore import Server
from beo.networklink.discovery import advertiser, own_ip
from beo.networklink.sources import Catalog, SOURCES
from beo.networklink.roles.listener import ListenerRole, Listening
from beo.product.identity import ProductIdentity

log = logging.getLogger("beo-adapter.listening")

REDIAL_S = 30.0
ENROL_RETRY_S = 60.0


def describe(status: str, fallback: str) -> tuple:
    """(title, secondary) for the machine's display, out of a source's status:
    what is playing if the source says, and the source's own name."""
    def first(name):
        m = re.search(r"<%s>([^<]+)</%s>" % (name, name), status)
        return html.unescape(m.group(1)).strip() if m else ""
    source = first("friendlyName") or fallback
    playing = re.search(r"<type>NOW_PLAYING\w*</type><kind>playing</kind><data><name>([^<]+)</name>",
                        status)
    title = html.unescape(playing.group(1)).strip() if playing else ""
    return (title or source, source if title else "")


def playing_lines(xml: str) -> list:
    """What a source says is playing, as lines for the machine's display: the
    station, then the artist and the title a station gives as 'artist - title'."""
    m = re.search(r"<type>NOW_PLAYING\w*</type><kind>playing</kind><data>(.*?)</data>", xml, re.S)
    if not m:
        return []

    def first(name):
        f = re.search(r"<%s>([^<]+)</%s>" % (name, name), m.group(1))
        return html.unescape(f.group(1)).strip() if f else ""
    lines = [first("name")] + [part.strip() for part in first("liveDescription").split(" - ")]
    return [line for line in lines if line]


def playback_state(xml: str) -> str:
    """play | pause | stop, as a source's progress says it; empty if it does not."""
    m = re.search(r"<type>PROGRESS_INFORMATION</type><kind>playing</kind><data><state>(\w+)</state>",
                  xml)
    return m.group(1) if m else ""


def now_playing(xml: str) -> Optional[dict]:
    """What a source says is playing, for the product's page: the station,
    the artist and title a station gives as 'artist - title', and its picture."""
    m = re.search(r"<type>NOW_PLAYING\w*</type><kind>playing</kind><data>(.*?)</data>", xml, re.S)
    if not m:
        return None

    def first(name):
        f = re.search(r"<%s>([^<]+)</%s>" % (name, name), m.group(1))
        return html.unescape(f.group(1)).strip() if f else ""
    artist, _, title = first("liveDescription").partition(" - ")
    return {"station": first("name"), "artist": artist.strip(), "title": title.strip(),
            "image": first("url")}


class Outputs:
    """Every output one received stream is written to.

    They are opened one at a time, the machine's only once it has taken the
    source, so the set grows while the stream runs.
    """

    def __init__(self):
        self._open: tuple = ()
        self._stopped = False
        # A source that pauses goes on sending its music and asks its
        # listeners to mute. Silence in its place keeps every output's timing.
        self.muted = False
        self._lock = threading.Lock()

    def add(self, playback) -> bool:
        """False = the stream ended first, and the playback is the caller's to stop."""
        with self._lock:
            if self._stopped:
                return False
            self._open += (playback,)
            return True

    def write(self, block) -> None:
        if self.muted:
            block = replace(block, pcm=bytes(len(block.pcm)))
        for playback in self._open:
            playback.write(block)

    def stop(self) -> None:
        with self._lock:
            opened, self._open, self._stopped = self._open, (), True
        for playback in opened:
            playback.stop()


class BeolinkIn:
    """Owns the listener role, the inbound port and the ways out: to the
    machine, and to an output of the product's own."""

    def __init__(self, ident: ProductIdentity, catalog: Catalog, *, dial: Iterable[str] = (),
                 host: str = "", machine=None, device: str = "hw:0,0", speaker=None,
                 web=None, now_ns=None, enrol: bool = False, wireless: bool = True,
                 source=None):
        self.ident = ident
        self.enrol, self.wireless = enrol, wireless
        # What the product offers other products to borrow: the catalogue's
        # sources that the product can play to a listener, by the same rule
        # the source side plays by.
        self.offered = [s for s in catalog.sources if SOURCES.get(s.key) is s]
        self.dial = [ip for ip in dial if ip]
        ip = own_ip()
        self.role = ListenerRole(ident, ip, catalog=catalog,
                                 on_started=self._started, on_stopped=self._stopped,
                                 on_refused=self._refused, on_notification=self._notified)
        # The product's own source side: a product that has borrowed one of
        # our sources opens a session to us and asks for it with a Connect,
        # and that question is the source side's to answer.
        self.source = source
        self.server = Server(ident, self._serve)
        # A source offers as listeners only the products it has found on the
        # product-to-product plane, and it finds them by these records. The
        # host is the one the app-facing records already carry, so both sets
        # describe one machine.
        self.advertiser = advertiser(replace(ident, host=host), ip) if host else None
        # The machine is asked through network_source_started(title, secondary)
        # and tells us through on_network_source_left when it turns away. A
        # product with nothing behind it has neither, and only counts.
        self.machine = machine
        self.device, self.now_ns = device, now_ns
        # The product's own output: open(now_ns) gives a playback that is
        # already at the product's volume. It asks nobody before it plays.
        self.speaker = speaker
        # The browsers, opened the same way: they hear the stream while it runs.
        self.web = web
        self.outputs: Optional[Outputs] = None
        # How the last join went, for the product's page: the app has a
        # timer of its own and is told only of success.
        self.last_join: Optional[dict] = None
        # What the source says is playing while we listen, and whether it
        # plays or pauses.
        self.playing: Optional[dict] = None
        self.source_state = ""
        self._lines: list = []
        if hasattr(machine, "on_network_source_left"):
            machine.on_network_source_left = self.leave
        self._stop = threading.Event()

    def _serve(self, session) -> None:
        """A session a product opened to us. Its first word says what it is:
        a source's ConnectRequest, which the listener role answers, or a
        borrower's Connect for one of our sources, which the source side
        does. The borrower's handler goes first, so that its Connect is not
        left for the listener role to decline."""
        if self.source is not None:
            def borrow(s, st):
                xml = st.xml
                if "<Connect" in xml and "<ConnectRequest" not in xml and st.type == "set":
                    return self.source.accept(s, st)
                return False
            session.on(borrow)
        self.role.serve(session)

    def start(self) -> "BeolinkIn":
        try:
            self.server.open()
            log.info("listening for other products on port %d", self.server.port)
        except OSError as e:
            log.error("port %d is not ours to listen on (%s); a source can reach us "
                      "only on a session we open to it", self.server.port, e)
        if self.advertiser is not None:
            threading.Thread(target=self._advertise, name="listening-mdns", daemon=True).start()
        if self.dial:
            threading.Thread(target=self._keep_dialled, name="listening-dial", daemon=True).start()
        if self.enrol:
            threading.Thread(target=self._enrol, name="listening-enrol", daemon=True).start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self.advertiser is not None:
            self.advertiser.stop()
        self.server.close()
        self.role.close()

    def _advertise(self) -> None:
        """Registration waits out our own leftovers, so it runs beside the start."""
        try:
            self.advertiser.start()
        except Exception as e:      # noqa: BLE001
            log.error("not announced to other products (%s); no source will offer us", e)

    def _enrol(self) -> None:
        """See that the network's configuration has our entry: what we are
        and the one source we offer. Tried until it is known to be there; the
        leader changes, and is at times nobody."""
        # The sources under their own names; the entry names the product
        # beside them, and a product that borrows one shows the two apart.
        entry = configuration.device_entry(self.ident, self.offered,
                                           wireless=self.wireless)
        while True:
            try:
                result = configuration.ensure_enrolled(self.ident, entry)
            except Exception as e:      # noqa: BLE001
                result = "failed: %s" % e
            if result == configuration.PRESENT:
                log.info("already in the network's configuration")
                return
            if result == configuration.ENROLLED:
                log.warning("added to the network's configuration; sources will now offer us")
                return
            if result == configuration.REWRITTEN:
                log.warning("our entry in the network's configuration was sent again as written "
                            "now; a product linked to us may need linking again")
                return
            log.warning("not yet in the network's configuration (%s); trying again in %.0f s",
                        result, ENROL_RETRY_S)
            if self._stop.wait(ENROL_RETRY_S):
                return

    def _keep_dialled(self) -> None:
        """A product closes a session nothing is said on, so it is reopened."""
        while True:
            for ip in self.dial:
                self.role.dial(ip)
            if self._stop.wait(REDIAL_S):
                return

    # ---- what the facade asks for ---------------------------------------
    @property
    def listening_to(self) -> Optional[str]:
        """The jid of the product this one follows, or None."""
        cur = self.role.current
        return cur.source_jid if cur is not None else None

    def join(self, jid: str, on_result: Optional[Callable[[bool], None]] = None) -> bool:
        """The app asked us to join the product with this jid.

        The join waits for the source's answers, so it runs beside the
        request; ``on_result`` is told how it ended.
        """
        self.last_join = {"jid": jid, "state": "joining"}
        ip = facade.peer_by_jid(jid).get("ipAddress")
        if not ip:
            self.last_join = {"jid": jid, "state": "failed"}
            log.warning("join %s: nothing by that jid has been heard on the network", jid)
            if on_result:
                on_result(False)
            return False

        def drive():
            joined = False
            try:
                joined = self.role.join(ip, jid)
            except Exception:      # noqa: BLE001
                log.exception("joining %s crashed", jid)
            self.last_join = {"jid": jid, "state": "joined" if joined else "failed"}
            if on_result:
                on_result(joined)

        threading.Thread(target=drive, daemon=True, name="listening-join").start()
        return True

    def leave(self) -> None:
        self.role.leave()

    # ---- what the role reports ------------------------------------------
    def _started(self, cur: Listening) -> None:
        log.warning("listening to %s from %s", cur.source_key, cur.source_jid)
        self.playing = now_playing(cur.status)
        self.source_state = playback_state(cur.status) or "play"
        outputs = Outputs()
        outputs.muted = self.source_state != "play"
        self.outputs = outputs
        self.role.sink = outputs
        if self.speaker is not None:
            try:
                self._add(outputs, self.speaker.open(self.now_ns))
            except OSError as e:
                log.error("the product's own output could not be opened: %s", e)
        if self.web is not None:
            self._add(outputs, self.web.open(self.now_ns))
        if getattr(self.machine, "network_source_started", None) is None:
            return
        # Beside the join, not inside it: the stream is ours from this moment,
        # and the machine may take seconds to come round to it.
        threading.Thread(target=self._play_into_machine, args=(cur, outputs), daemon=True,
                         name="listening-machine").start()

    @staticmethod
    def _add(outputs: Outputs, playback) -> None:
        if not outputs.add(playback):
            # The stream ended in the instant the card was opened, and the
            # one who stops the cards had nothing of this one to stop.
            playback.stop()

    def _play_into_machine(self, cur: Listening, outputs: Outputs) -> None:
        title, secondary = describe(cur.status, cur.source_key)
        self._lines = playing_lines(cur.status)
        # The machine first, the sound card after: until the machine has
        # asked for the source it is not listening to the audio pairs, and
        # they are shared with everything else on the bus.
        taken = self.machine.network_source_started(title, secondary)
        if self.role.current is not cur:
            return                      # the stream ended while the machine was asked
        if not taken:
            log.error("the machine did not take the network source; the stream is %s",
                      "played on the product's own output only" if self.speaker is not None
                      else "received and not played")
            return
        self._show_lines()
        self._add(outputs, AlsaPlayback(self.device, now_ns=self.now_ns).start())

    def _notified(self, kind: str, xml: str) -> None:
        """The source says what is playing again whenever it changes."""
        playing = now_playing(xml)
        if playing is not None:
            self.playing = playing
        state = playback_state(xml)
        if state:
            self.source_state = state
            outputs = self.outputs
            if outputs is not None and outputs.muted != (state != "play"):
                outputs.muted = state != "play"
                log.info("the source %s: the outputs are %s", "plays" if state == "play" else
                         "pauses", "muted" if outputs.muted else "playing again")
        lines = playing_lines(xml)
        if lines and lines != self._lines:
            self._lines = lines
            self._show_lines()

    def _show_lines(self) -> None:
        show = getattr(self.machine, "network_source_playing", None)
        if show is not None and self._lines and self.role.current is not None:
            show(list(self._lines))

    def _stopped(self, cur: Listening) -> None:
        log.warning("no longer listening to %s", cur.source_jid)
        if self.role.current is None:
            self.playing = None
        self.role.sink = None
        outputs, self.outputs = self.outputs, None
        if outputs is not None:
            outputs.stop()
        # Not when another stream is already taking this one's place: the
        # machine stays on what it is playing.
        ended = getattr(self.machine, "network_source_stopped", None)
        if ended is not None and self.role.current is None:
            ended()

    @staticmethod
    def _refused(cur: Listening, reason: str) -> None:
        log.error("%s will not play to us: %s", cur.source_jid, reason)


def from_configuration(value: Optional[str]) -> Optional[list]:
    """BEO_LISTEN: empty or off = no listening; on = listen; addresses,
    comma separated = listen and also hold a session open to each of them."""
    v = (value or "").strip()
    if v.lower() in ("", "0", "no", "off", "false", "none"):
        return None
    if v.lower() in ("1", "yes", "on", "true"):
        return []
    return [p.strip() for p in v.split(",") if p.strip()]


__all__ = ["BeolinkIn", "describe", "from_configuration", "now_playing", "playback_state"]
