"""beocore: one XMPP session to one peer (port 5222), threaded.

The wire format follows what the products do:

  * every iq we originate carries a decimal id;
  * the stream header is sent without an ``<?xml?>`` declaration, and the
    connection is kept open without whitespace keepalives;
  * attributes are written with double quotes;
  * notifications are ``<message><body><_notification><NOTIFICATION>``;
  * ``close()`` sends ``</stream:stream>`` before the socket is closed.

``SessionManager`` (one session per peer per process, asyncio) is planned;
this version gives one ``Session`` per dial.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from typing import Callable, List, Optional
from xml.sax.saxutils import escape

from beo.product.identity import DOMAIN, ProductIdentity

log = logging.getLogger("beo.networklink.beocore")

DISCO = "http://jabber.org/protocol/disco#info"
STANZA = re.compile(r"<iq\b[^>]*>.*?</iq>|<iq\b[^>]*/>|<message\b[^>]*>.*?</message>", re.S)
HEAD = re.compile(r"<(?:iq|message)\b([^>]*)>")
STREAM_HEADER = re.compile(r"<stream:stream\b([^>]*)>")
ATTR = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_DECIMAL = re.compile(r"\d+")

DISCO_FEATURES = [
    "jid\\20escaping", "beocore.player.1", "beocore.tunnel.1",
    "beocore.product.1", "beocore.configuration.1", "beocore.notifications.1",
]


def _x(v) -> str:
    return escape("" if v is None else str(v))


def head_attrs(s: str) -> dict:
    m = HEAD.match(s)
    return {a[0]: (a[1] or a[2]) for a in ATTR.findall(m.group(1))} if m else {}


def tag(xml: Optional[str], name: str) -> Optional[str]:
    m = re.search(rf"<{name}>(.*?)</{name}>", xml or "", re.S)
    return m.group(1) if m else None


_ATTRIBUT = re.compile(r"(\s[\w:.-]+)='([^']*)'")


def double_quotes(text: str) -> str:
    """Write attributes with double quotes, as every stanza from a product does.

    Only attributes are touched: element text can contain ="..." (the escaped
    contentProtectionInfo does) and the pattern requires ='.
    """
    return _ATTRIBUT.sub(lambda m: '%s="%s"' % (m.group(1), m.group(2)), text)


class Stanza:
    """One ``<iq>`` or ``<message>`` as text, with its head attributes parsed."""

    __slots__ = ("xml", "attrs")

    def __init__(self, xml: str):
        self.xml = xml
        self.attrs = head_attrs(xml)

    @classmethod
    def parse(cls, text: str) -> "Stanza":
        return cls(text)

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def type(self) -> str:
        return self.attrs.get("type", "")

    @property
    def from_jid(self) -> str:
        return self.attrs.get("from", "")

    def tag(self, name: str) -> Optional[str]:
        return tag(self.xml, name)

    def __contains__(self, s: str) -> bool:
        return s in self.xml

    def __str__(self) -> str:
        return self.xml


class IdCounter:
    """One rising sequence of decimal iq ids across every request we originate,
    as a product counts them."""

    def __init__(self, start: int = 200):
        self._n = int(start)
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            self._n += 1
            return str(self._n)

    __next__ = next

    def __iter__(self):
        return self


def check_id(iq_id) -> str:
    """The iq id as a string of decimal digits."""
    s = str(iq_id)
    if not _DECIMAL.fullmatch(s):
        raise ValueError("iq id must be decimal digits, not %r" % (iq_id,))
    return s


Handler = Callable[["Session", Stanza], bool]
Tap = Callable[[str, str, str], None]          # (direction ">>"/"<<", peer_ip, text)


class Session(threading.Thread):
    """One XMPP connection to one peer, whoever dialled: ``open`` when we do,
    ``accept`` when the peer did.

    Inbound stanzas go to ``handlers`` in order; the first returning True has
    handled it. The session itself stores replies (``type=result|error``),
    answers ``disco#info`` and finally answers any unhandled get/set with
    ``feature-not-implemented``.
    """

    daemon = True

    def __init__(self, identity: ProductIdentity, peer_ip: str, *, port: int = 5222,
                 peer_jid: Optional[str] = None, ids: Optional[IdCounter] = None,
                 tap: Optional[Tap] = None, keep_lines: bool = False,
                 name: str = "networklink-session"):
        super().__init__(name=name)
        self.identity = identity
        self.our_jid = identity.full_jid()
        self.peer_ip, self.port = peer_ip, port
        # Learned from the first stanza the peer sends, unless given.
        self.peer_jid = peer_jid
        self.sock = None
        self.replies: dict[str, str] = {}
        self.ids = ids or IdCounter()
        self.tap = tap
        self.lines: Optional[List[tuple]] = [] if keep_lines else None
        self.handlers: List[Handler] = []
        self.disconnected = threading.Event()
        self._peer_known = threading.Event()
        if peer_jid:
            self._peer_known.set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._greeting_owed = False

    # ------------------------------------------------------------ lifecycle
    def open(self, timeout: float = 8) -> "Session":
        """Connect to the peer, send the stream header and start reading."""
        self.sock = socket.create_connection((self.peer_ip, self.port), timeout=timeout)
        self.sock.settimeout(1.0)
        self._send_stream_header()
        self.start()
        return self

    @classmethod
    def accept(cls, identity: ProductIdentity, sock, peer_ip: str, **kw) -> "Session":
        """Take over a connection the peer opened and start reading.

        Our stream header is not sent here: it answers the peer's, and names
        the peer, so it goes out when the peer's own has been read.
        """
        session = cls(identity, peer_ip, **kw)
        session.sock = sock
        sock.settimeout(1.0)
        session._greeting_owed = True
        session.start()
        return session

    def _send_stream_header(self, to: str = DOMAIN) -> None:
        # The header alone, with no <?xml ...?> declaration in front, as a
        # product opens its own streams.
        self._send(f"<stream:stream to='{to}' "
                   f"from='{self.our_jid}' xmlns='jabber:client' "
                   "xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>")

    def _greet(self, text: str) -> None:
        """Answer the header of a peer that dialled us, then ask who it is.

        The peer's jid is in its header, which is the only place a caller
        states it before its first stanza. The disco#info that follows is how
        a product greets whoever connects to it.
        """
        m = STREAM_HEADER.search(text)
        if m is None:
            return
        self._greeting_owed = False
        frm = {a[0]: (a[1] or a[2]) for a in ATTR.findall(m.group(1))}.get("from", "")
        if frm and not self.peer_jid:
            self.peer_jid = frm
            self._peer_known.set()
        self._send_stream_header(to=frm or DOMAIN)
        if self.peer_jid:
            self.send(f"<query xmlns='{DISCO}'/>", typ="get")

    def _learn_from_header(self, text: str) -> None:
        m = STREAM_HEADER.search(text)
        if m is None:
            return
        frm = {a[0]: (a[1] or a[2]) for a in ATTR.findall(m.group(1))}.get("from", "")
        if frm:
            self.peer_jid = frm
            self._peer_known.set()

    def run(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self.sock.recv(32768)
            except socket.timeout:
                # Idle: keep listening. The connection lives without a whitespace
                # keepalive, as a product's own does.
                continue
            except OSError:
                return
            if not chunk:
                log.info("XMPP: the session closed")
                self.disconnected.set()
                return
            buf += chunk
            text = buf.decode("utf-8", "replace")
            if self._greeting_owed:
                self._greet(text)
            elif not self.peer_jid:
                # A peer we dialled names itself in its stream header, and a
                # peer that already knows our capabilities sends nothing after
                # it. Learning the jid here is what lets our first request go
                # out as soon as the peer has spoken, not after a timeout.
                self._learn_from_header(text)
            end = 0
            for m in STANZA.finditer(text):
                self._handle(m.group(0))
                end = m.end()
            buf = text[end:].encode()

    def close(self) -> None:
        """RFC 6120 §4.4: say goodbye before pulling the plug. Without it the
        peer keeps state and delivers a Disconnect from an earlier session
        into the next one."""
        self._stop.set()
        if self.sock:
            try:
                self.sock.sendall(b"</stream:stream>")
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    def wait_peer(self, timeout: float = 6.0) -> Optional[str]:
        """The peer jid is set when the peer sends its first stanza. A product
        sends only the stream header and then keeps quiet until it has
        something to say; the caller may fall back on the jid it dialled."""
        self._peer_known.wait(timeout)
        return self.peer_jid

    def on(self, handler: Handler, first: bool = False) -> None:
        """Add a handler; ``first`` puts it ahead of those already there."""
        if first:
            self.handlers.insert(0, handler)
        else:
            self.handlers.append(handler)

    # ---------------------------------------------------------------- wire
    def _send(self, t: str) -> None:
        t = double_quotes(t)
        with self._lock:
            try:
                self.sock.sendall(t.encode())
            except OSError:
                pass
        if self.lines is not None:
            self.lines.append((">>", t))
        if self.tap:
            self.tap(">>", self.peer_ip, t)

    def _handle(self, st: str) -> None:
        stanza = Stanza(st)
        sid, typ, frm = stanza.id, stanza.type, stanza.from_jid
        if frm and not self.peer_jid:
            self.peer_jid = frm
            self._peer_known.set()
        if self.lines is not None:
            self.lines.append(("<<", st))
        if self.tap:
            self.tap("<<", self.peer_ip, st)

        if typ in ("result", "error") and sid:
            self.replies[sid] = st
            return

        if DISCO in st and typ == "get":
            feats = "".join(f"<feature var='{f}'/>" for f in DISCO_FEATURES)
            self.result(stanza, f"<query xmlns='{DISCO}'><identity category='client' "
                                f"name='{_x(self.identity.name)}' type='product'/>{feats}</query>")
            return

        for h in list(self.handlers):
            try:
                if h(self, stanza):
                    return
            except Exception as e:
                log.warning("handler %r failed on %s: %s", h, st[:120], e)
                return

        if typ in ("get", "set"):
            log.info("<< unanswered %s: %s", typ, st[:200])
            if sid:
                self.error(stanza)

    # ------------------------------------------------------------- replies
    def result(self, stanza: Stanza, inner: str) -> None:
        self._send(f"<iq id='{stanza.id}' to='{stanza.from_jid}' from='{self.our_jid}' "
                   f"type='result'>{inner}</iq>")

    def error(self, stanza: Stanza, condition: str = "feature-not-implemented") -> None:
        self._send(f"<iq id='{stanza.id}' to='{stanza.from_jid}' from='{self.our_jid}' "
                   "type='error'><error type='cancel'>"
                   f"<{condition} "
                   "xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/></error></iq>")

    # ------------------------------------------------------------ requests
    def send(self, inner: str, *, typ: str = "set", iq_id: Optional[str] = None) -> str:
        """Send a request WITHOUT waiting for the reply; returns the id.

        A source sends its four volume stanzas in the same millisecond.
        Waiting for each reply spreads them over hundreds of milliseconds,
        and the order on the wire is then another than a product's.
        """
        iq_id = check_id(iq_id if iq_id is not None else self.ids.next())
        self.replies.pop(iq_id, None)
        self._send(f"<iq id='{iq_id}' to='{self.peer_jid}' from='{self.our_jid}' "
                   f"type='{typ}'>{inner}</iq>")
        return iq_id

    def reply(self, iq_id: str, timeout: float = 8) -> Optional[str]:
        """Fetch the reply to a request already sent (polls, like the original)."""
        iq_id = str(iq_id)
        for _ in range(int(timeout * 10)):
            if iq_id in self.replies:
                return self.replies[iq_id]
            time.sleep(0.1)
        return None

    def request(self, inner: str, *, typ: str = "set", timeout: float = 8,
                iq_id: Optional[str] = None) -> Optional[str]:
        """Send and wait. typ: beocore commands are 'set'; disco#info must be
        'get' — a disco#info as set gets no answer at all."""
        return self.reply(self.send(inner, typ=typ, iq_id=iq_id), timeout=timeout)

    # ---------------------------------------------------------------- push
    def push(self, notification: str) -> None:
        """Notifications are NOT iq. They are ``<message>``, and the element is
        NOTIFICATION in the singular inside ``_notification``. An ``iq`` with
        ``<_notification>`` is refused with «Unknown command: _notification»."""
        self._send(f"<message to='{self.peer_jid}' from='{self.our_jid}'><body>"
                   "<_notification xmlns='beocore.notifications.1'>"
                   f"{notification}</_notification></body></message>")


class Server(threading.Thread):
    """Accept the sessions other products open to us.

    Each one is handed to ``on_session`` as a running Session. Which role it
    serves is the caller's business: a product opens one connection and says
    on it whatever it has to say.
    """

    daemon = True

    def __init__(self, identity: ProductIdentity, on_session: Callable[[Session], None], *,
                 port: int = 5222, bind: str = "0.0.0.0", tap: Optional[Tap] = None):
        super().__init__(name="networklink-server")
        self.identity = identity
        self.on_session = on_session
        self.port, self.bind = port, bind
        self.tap = tap
        self._stop = threading.Event()
        self._sock = None

    def open(self) -> "Server":
        """Bind and start accepting. Raises OSError if the port is taken."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind, self.port))
        s.listen(8)
        s.settimeout(1.0)
        self._sock = s
        self.start()
        return self

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            log.info("XMPP: %s opened a session to us", addr[0])
            session = Session.accept(self.identity, conn, addr[0], tap=self.tap,
                                     name="networklink-inbound-%s" % addr[0])
            try:
                self.on_session(session)
            except Exception:      # noqa: BLE001
                log.exception("the session from %s could not be served", addr[0])
                session.close()

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


__all__ = ["Session", "Server", "Stanza", "IdCounter", "check_id", "double_quotes", "head_attrs",
           "tag", "DISCO", "DISCO_FEATURES", "STANZA"]
