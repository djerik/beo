"""ListenerRole invariants with a fake session and a fake FPS receiver, and the
inbound half of a Session over a real loopback socket:

  the receiver is bound BEFORE our Connect is sent
  a refused Connect reports the source's own reason and closes the receiver
  the source's Disconnect is answered, then we say goodbye ourselves
  a session the peer opened is greeted with a header naming the peer
"""
import socket
import threading

from beo.networklink.beocore import Server, Stanza
from beo.networklink.sources import CP_CAPABILITIES, Catalog, STANDARD_SOURCES, Volume
from beo.networklink.roles.listener import ListenerRole

from test_source_role import IDENT, PEER, FakeSession

FPS_HOST = "203.0.113.2"
CONNECT_ID, SUBSCRIBE_ID = "51901", "51902"


class Session(FakeSession):
    peer_ip = PEER.ip


class FakeReceiver:
    def __init__(self, log, fmt):
        self.log = log

    def open(self):
        self.log.append(("rx.open",))
        return self

    def blocks(self):
        return iter(())

    def stats(self):
        return {"datagrams": 0, "gaps": 0, "markers": 0, "heartbeat_bytes": 0, "tcp_peer": None}

    def close(self):
        self.log.append(("rx.close",))


def make_role(log, replies=None, **callbacks):
    session = Session(log, replies)
    role = ListenerRole(IDENT, FPS_HOST, catalog=Catalog(STANDARD_SOURCES),
                        volume_provider=lambda: Volume(35),
                        receiver_factory=lambda fmt: FakeReceiver(log, fmt), **callbacks)
    role.serve(session)
    return role, session


def connect_request():
    return Stanza(f"<iq id='1255' type='set' from='{PEER.jid}'><ConnectRequest xmlns='beocore.player.1'>"
                  f"<uniqueSourceId><source>linein</source><jid>{PEER.jid}</jid></uniqueSourceId>"
                  "</ConnectRequest></iq>")


def disconnect():
    return Stanza(f"<iq id='9' type='set' from='{PEER.jid}'><Disconnect xmlns='beocore.player.1'>"
                  "<connectionId>1</connectionId></Disconnect></iq>")


ACCEPTED = {CONNECT_ID: f"<iq id='{CONNECT_ID}' type='result'><Connect xmlns='beocore.player.1'>"
                        f"<connectionId>{IDENT.full_jid()}</connectionId></Connect></iq>",
            SUBSCRIBE_ID: f"<iq id='{SUBSCRIBE_ID}' type='result'><_notification-subscribe>"
                          "<FullStatus><status></status></FullStatus></_notification-subscribe></iq>"}


def test_connect_request_is_accepted_and_the_receiver_is_bound_before_our_connect():
    log = []
    started = threading.Event()
    role, session = make_role(log, ACCEPTED, on_started=lambda cur: started.set())
    assert session.handlers[0](session, connect_request()) is True
    assert log[0] == ("result", "1255", "<ConnectRequest xmlns='beocore.player.1'>"
                                        "<connected>1</connected></ConnectRequest>")
    assert started.wait(2)

    sends = [(e[1], e[2]) for e in log if e[0] == "send"]
    assert [s[0] for s in sends] == [CONNECT_ID, SUBSCRIBE_ID]
    assert sends[0][1] == ("<Connect xmlns='beocore.player.1'><sourceId>linein</sourceId>"
                           f"<connectionInfo>FPS://{FPS_HOST}</connectionInfo>"
                           f"<contentProtectionInfo>{CP_CAPABILITIES}</contentProtectionInfo>"
                           f"<uniqueSourceId><source>linein</source><jid>{PEER.jid}</jid>"
                           "</uniqueSourceId></Connect>")
    assert "<item>device</item><item>playing</item><item>source</item>" in sends[1][1]
    assert log.index(("rx.open",)) < log.index(("send",) + sends[0])
    cur = role.current
    assert cur.source_key == "linein" and cur.source_jid == PEER.jid and cur.connection_id == 1


def test_a_refused_connect_reports_the_sources_reason_and_closes_the_receiver():
    log = []
    refused = []
    done = threading.Event()

    def on_refused(cur, reason):
        refused.append(reason)
        done.set()
    error = (f"<iq id='{CONNECT_ID}' type='error'><error type='cancel'><service-unavailable "
             "xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'></service-unavailable>"
             "<text xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'>Connect failed, "
             "error=Device not found in config</text></error></iq>")
    role, session = make_role(log, {CONNECT_ID: error}, on_refused=on_refused,
                              on_started=lambda cur: refused.append("started"))
    session.handlers[0](session, connect_request())
    assert done.wait(2)
    assert refused == ["Connect failed, error=Device not found in config"]
    assert ("rx.close",) in log and role.current is None
    assert [e[1] for e in log if e[0] == "send"] == [CONNECT_ID]


def test_the_sources_disconnect_is_answered_and_then_we_say_goodbye():
    log = []
    started, stopped = threading.Event(), threading.Event()
    role, session = make_role(log, ACCEPTED, on_started=lambda cur: started.set(),
                              on_stopped=lambda cur: stopped.set())
    session.handlers[0](session, connect_request())
    assert started.wait(2)
    log.clear()
    assert session.handlers[0](session, disconnect())
    assert log[0] == ("result", "9", "<Disconnect xmlns='beocore.player.1'/>")
    sends = [e[2] for e in log if e[0] == "send"]
    assert sends[0] == "<Disconnect xmlns='beocore.player.1'><connectionId>1</connectionId></Disconnect>"
    assert sends[1].startswith("<_notification-unsubscribe")
    assert ("rx.close",) in log and stopped.is_set() and role.current is None
    # A second Disconnect is a leftover: answered, and nothing else happens.
    log.clear()
    assert session.handlers[0](session, disconnect())
    assert log == [("result", "9", "<Disconnect xmlns='beocore.player.1'/>")]


def test_the_sources_questions_about_our_renderer_are_answered():
    log = []
    role, session = make_role(log)
    h = session.handlers[0]

    def ask(inner):
        log.clear()
        assert h(session, Stanza(f"<iq id='7' type='set' from='{PEER.jid}'>{inner}</iq>"))
        return log[-1][2]

    sub = ask("<_notification-subscribe xmlns='beocore.notifications.1'><_notification-filter>"
              "<Filter><Filter><includeKindFilter><item>renderer</item></includeKindFilter>"
              "</Filter></Filter></_notification-filter></_notification-subscribe>")
    assert "<type>VOLUME</type><kind>renderer</kind><data><speaker><level>35</level>" in sub
    assert ask("<GetVolume xmlns='beocore.product.1'></GetVolume>").endswith("<volume>35</volume></GetVolume>")
    assert "<muted>0</muted>" in ask("<GetVolumeMuted xmlns='beocore.product.1'></GetVolumeMuted>")
    assert "<range><minimum>" in ask("<GetVolumeRange xmlns='beocore.product.1'></GetVolumeRange>")
    assert ask("<Attach xmlns='beocore.product.1'><role>0</role></Attach>").startswith(
        "<Attach xmlns='beocore.product.1'><contactInfo>")
    assert not h(session, Stanza(f"<iq id='5' type='set' from='{PEER.jid}'><Foo/></iq>"))


def test_notifications_are_taken_without_a_reply():
    log = []
    seen = []
    role, session = make_role(log, on_notification=lambda kind, xml: seen.append(kind))
    msg = Stanza(f"<message to='{IDENT.full_jid()}' from='{PEER.jid}'><body><_notification "
                 "xmlns='beocore.notifications.1'><NOTIFICATION><type>PROGRESS_INFORMATION</type>"
                 "<kind>playing</kind><data><state>play</state></data></NOTIFICATION>"
                 "</_notification></body></message>")
    assert session.handlers[0](session, msg)
    assert seen == ["PROGRESS_INFORMATION"] and log == []


def test_a_session_the_peer_opened_is_greeted_with_a_header_naming_the_peer():
    sessions = []
    server = Server(IDENT, sessions.append, port=0, bind="127.0.0.1").open()
    try:
        c = socket.create_connection(("127.0.0.1", server._sock.getsockname()[1]), timeout=3)
        c.settimeout(3)
        c.sendall(("<stream:stream to='products.bang-olufsen.com' from='%s' xmlns='jabber:client' "
                   "xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>" % PEER.jid).encode())
        got = b""
        while b"</iq>" not in got and b"/></iq>" not in got:
            got += c.recv(4096)
        text = got.decode()
        assert text.startswith('<stream:stream to="%s" from="%s"' % (PEER.jid, IDENT.full_jid()))
        assert 'type="get"><query xmlns="http://jabber.org/protocol/disco#info"/></iq>' in text
        assert sessions and sessions[0].wait_peer(1) == PEER.jid
        c.close()
    finally:
        server.close()
        for s in sessions:
            s.close()


def test_join_asks_what_is_playing_and_connects_to_that():
    log = []
    experiences = (f"<iq id='201' type='result'><Experiences xmlns='beocore.player.1'><experienceInfo>"
                   "<list><item><source><identifier>AUX_A</identifier><uniqueSourceId>"
                   f"<source>linein</source><jid>{PEER.jid}</jid></uniqueSourceId></source>"
                   "<state>play</state></item></list></experienceInfo></Experiences></iq>")
    replies = {"51901": experiences,
               "51902": ACCEPTED[CONNECT_ID].replace(CONNECT_ID, "51902"),
               "51903": ACCEPTED[SUBSCRIBE_ID].replace(SUBSCRIBE_ID, "51903")}
    session = Session(log, replies)
    session.disconnected = threading.Event()
    role = ListenerRole(IDENT, FPS_HOST, receiver_factory=lambda fmt: FakeReceiver(log, fmt),
                        session_factory=lambda *a, **kw: session)
    assert role.join(PEER.ip, PEER.jid) is True
    sends = [e[2] for e in log if e[0] == "send"]
    assert sends[0] == "<Experiences xmlns='beocore.player.1'></Experiences>"
    assert sends[1].startswith("<Connect xmlns='beocore.player.1'><sourceId>linein</sourceId>")
    assert role.current.source_jid == PEER.jid

    # nothing playing: no Connect is sent
    log.clear()
    idle = Session(log, {"51901": "<iq type='result'><Experiences><experienceInfo><list></list>"
                                  "</experienceInfo></Experiences></iq>"})
    idle.disconnected = threading.Event()
    role = ListenerRole(IDENT, FPS_HOST, session_factory=lambda *a, **kw: idle)
    assert role.join(PEER.ip, PEER.jid) is False
    assert [e[0] for e in log if e[0] == "send"] == ["send"]
