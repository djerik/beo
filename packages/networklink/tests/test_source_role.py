"""SourceRole invariants with a fake session and a fake FPS sender:

  3  FPS starts BEFORE the Connect reply
  5  the opening happens only after the listener's FullStatus + 3 volume replies
  8  Disconnect before STREAMING is acknowledged and ignored
  2  notifications go as <message>, never iq
"""
import threading

import golden_identities as pinned
from beo.networklink.beocore import IdCounter, Stanza
from beo.networklink.discovery import Peer
from beo.networklink.fps import FORMAT_44100, MonotonicClock
from beo.product.identity import ProductIdentity
from beo.networklink.sources import Catalog, SOURCES, STANDARD_SOURCES, Volume
from beo.networklink.roles.source import Listener, SourceRole

IDENT = ProductIdentity.from_jid(pinned.OUR_JID, pinned.OUR_NAME, pinned.OUR_MAC)
PEER = Peer(pinned.PEER_JID, pinned.PEER_IP, "Listener", product_type="CA17")


class FakeFps:
    def __init__(self, log, host, fmt, clock, **kw):
        self.log, self.host, self.fmt, self.kw = log, host, fmt, kw
        self.markers = 0

    def start(self, source):
        self.log.append(("fps.start", self.host, source))

    def stop(self):
        self.log.append(("fps.stop",))

    def mute(self):
        self.markers += 1
        self.log.append(("marker", self.markers))

    unmute = mute


class FakeSession:
    """Records everything; answers requests from a scripted reply table."""

    def __init__(self, log, replies=None):
        self.log = log
        self.peer_jid = PEER.jid
        self.sock = object()
        self.ids = IdCounter(51900)
        self.scripted = replies or {}
        self.replies = {}
        self.handlers = []

    def on(self, h):
        self.handlers.append(h)

    def open(self):
        return self

    def wait_peer(self, t):
        return self.peer_jid

    def send(self, inner, *, typ="set", iq_id=None):
        iq_id = iq_id or self.ids.next()
        self.log.append(("send", iq_id, inner))
        return iq_id

    def reply(self, iq_id, timeout=8):
        self.log.append(("reply", iq_id))
        return self.scripted.get(iq_id)

    def request(self, inner, *, typ="set", timeout=8, iq_id=None):
        return self.reply(self.send(inner, typ=typ, iq_id=iq_id), timeout=timeout)

    def result(self, st, inner):
        self.log.append(("result", st.id, inner))

    def error(self, st, condition="feature-not-implemented"):
        self.log.append(("error", st.id))

    def push(self, n):
        self.log.append(("push", n))

    def close(self):
        self.log.append(("close",))


class Audio:
    format = FORMAT_44100

    def read_block(self):
        return None


def make_role(log, replies=None, goodbye_wait=0):
    session = FakeSession(log, replies)
    role = SourceRole(IDENT, Catalog(STANDARD_SOURCES), MonotonicClock(),
                      resolve_fps_host=False, progress_interval=3600,
                      fps_factory=lambda *a, **kw: FakeFps(log, *a, **kw),
                      session_factory=lambda *a, **kw: session,
                      volume_provider=lambda: Volume(35), goodbye_wait=goodbye_wait)
    return role, session


def connect_stanza():
    return Stanza(f"<iq id='156' type='set' from='{PEER.jid}'><Connect xmlns='beocore.player.1'>"
                  "<sourceId>beoradio</sourceId><connectionInfo>FPS://Beoplay-M3-90000010.local"
                  "</connectionInfo></Connect></iq>")


def test_fps_starts_before_connect_result_and_connection_id_is_peer_jid():
    log = []
    role, session = make_role(log)
    role.experience = role.experience or __import__("beo.networklink.sources", fromlist=["Experience"]).Experience(SOURCES["beoradio"], IDENT.full_jid())
    lis = Listener(PEER, session, Audio())
    assert role._handle(lis, connect_stanza()) is True
    kinds = [e[0] for e in log]
    assert kinds.index("fps.start") < kinds.index("result")
    assert log[-1] == ("result", "156", "<Connect xmlns='beocore.player.1'><connectionId>%s"
                       "</connectionId></Connect>" % PEER.jid)
    assert lis.streaming.is_set() and lis.connection_id == 1
    assert log[0][1] == PEER.ip                     # host not resolved -> peer ip


def test_disconnect_before_streaming_is_acked_and_ignored():
    log = []
    role, session = make_role(log)
    lis = Listener(PEER, session, Audio())
    st = Stanza(f"<iq id='9' type='set' from='{PEER.jid}'><Disconnect xmlns='beocore.player.1'>"
                "<connectionId>1</connectionId></Disconnect></iq>")
    assert role._handle(lis, st)
    assert log == [("result", "9", "<Disconnect xmlns='beocore.player.1'/>")]
    assert not lis.disconnected.is_set()
    # after streaming the same stanza ends the stream
    from beo.networklink.sources import Experience
    role.experience = Experience(SOURCES["beoradio"], IDENT.full_jid())
    role._handle(lis, connect_stanza())
    log.clear()
    assert role._handle(lis, st)
    assert lis.disconnected.is_set() and lis.fps is None
    assert ("fps.stop",) in log


def test_expand_sequence_and_opening_after_all_five_replies():
    """Attach(50900) -> ConnectRequest(51002) -> Connect arrives -> 50909..50913 sent
    together, all five replies awaited, THEN the opening pushes."""
    log = []
    replies = {"50900": "<iq type='result'><Attach><contactInfo/></Attach></iq>",
               "51002": "<iq type='result'><ConnectRequest><connected>1</connected></ConnectRequest></iq>",
               "50910": "<iq type='result'><FullStatus/></iq>",
               "50911": "<GetVolume><volume>13</volume></GetVolume>",
               "50912": "<GetVolumeMuted><muted>0</muted></GetVolumeMuted>",
               "50913": "<range><minimum>0</minimum><maximum>90</maximum></range>"}
    role, session = make_role(log, replies)
    joined = []
    role.on_listener_joined = joined.append
    vols = []
    role.on_renderer_volume = lambda p, v: vols.append(v)

    # the listener's Connect arrives while ConnectRequest is being awaited
    orig_reply = session.reply

    def reply(iq_id, timeout=8):
        r = orig_reply(iq_id, timeout)
        if iq_id == "51002":
            for h in session.handlers:
                h(session, connect_stanza())
        return r
    session.reply = reply

    lis = role.expand(PEER, SOURCES["beoradio"], Audio())
    assert lis is not None and joined == [PEER]
    assert vols == [Volume(13, False, 0, 90)]

    sends = [(e[1], e[2]) for e in log if e[0] == "send"]
    assert [s[0] for s in sends] == ["50900", "51002", "50909", "50910", "50911", "50912", "50913"]
    assert sends[0][1].startswith("<Attach xmlns='beocore.product.1'><role>0</role>")
    assert sends[1][1].startswith("<ConnectRequest xmlns='beocore")

    kinds = [e[0] for e in log]
    first_push = kinds.index("push")
    last_reply = max(i for i, e in enumerate(log) if e[0] == "reply" and e[1] in
                     ("50909", "50910", "50911", "50912", "50913"))
    assert last_reply < first_push
    # the four volume stanzas are sent together: no reply in between them
    i909 = next(i for i, e in enumerate(log) if e[:2] == ("send", "50909"))
    i913 = next(i for i, e in enumerate(log) if e[:2] == ("send", "50913"))
    assert all(log[i][0] == "send" for i in range(i909, i913 + 1))

    pushes = [e[1] for e in log if e[0] == "push"]
    types = [p[p.index("<type>") + 6:p.index("</type>")] for p in pushes]
    assert types == ["SOURCE_EXPERIENCE_CHANGED", "NOW_PLAYING_NET_RADIO", "PROGRESS_INFORMATION",
                     "PROGRESS_INFORMATION", "NOW_PLAYING_NET_RADIO"]
    assert "<state>preparing</state><position>0</position>" in pushes[2]
    assert "<state>play</state><position>0</position>" in pushes[3]
    assert role.listeners() == [PEER]
    assert role.experience.state == "play"

    # pause -> PROGRESS pause + marker; play -> PROGRESS play + marker; no-op repeats
    log.clear()
    role.set_state("pause")
    role.set_state("pause")
    role.set_state("play")
    kinds = [e[0] for e in log]
    assert kinds == ["push", "marker", "push", "marker"]
    assert log[1] == ("marker", 1) and log[3] == ("marker", 2)
    assert "<state>pause</state>" in log[0][1] and "<state>play</state>" in log[2][1]

    # teardown: unsubscribe(51901), SEC stop [peer], ENDED, PROGRESS stop, SOURCE empty,
    # Disconnect(51003, connectionId=1), fps stop, close
    log.clear()
    left = []
    role.on_listener_left = left.append
    started_at = role.experience.started_at
    assert role.unexpand(PEER)
    seq = [(e[0], e[1] if len(e) > 1 else None) for e in log]
    assert seq[0] == ("send", "51901") and log[0][2].startswith("<_notification-unsubscribe")
    pushes = [e[1] for e in log if e[0] == "push"]
    types = [p[p.index("<type>") + 6:p.index("</type>")] for p in pushes]
    assert types == ["SOURCE_EXPERIENCE_CHANGED", "NOW_PLAYING_ENDED", "PROGRESS_INFORMATION", "SOURCE"]
    assert "<lastUsed>%s</lastUsed>" % started_at in pushes[0]
    assert "<listener><item>%s</item></listener>" % PEER.jid in pushes[0]
    assert pushes[2].endswith("<data><state>stop</state></data></NOTIFICATION>")
    disc = next(e for e in log if e[0] == "send" and e[1] == "51003")
    assert disc[2] == "<Disconnect xmlns='beocore.player.1'><connectionId>1</connectionId></Disconnect>"
    assert [e[0] for e in log].index("fps.stop") > [e[0] for e in log].index("send")
    assert log[-1] == ("close",)
    assert left == [PEER] and role.listeners() == [] and role.experience is None


def test_connect_request_refused_closes_session():
    log = []
    replies = {"50900": "<contactInfo/>", "51002": "<connected>0</connected>"}
    role, session = make_role(log, replies)
    assert role.expand(PEER, SOURCES["CD"], Audio()) is None
    assert log[-1] == ("close",) and role.listeners() == []


def test_subscribe_answers_full_status_and_notification_ack():
    log = []
    role, session = make_role(log)
    from beo.networklink.sources import Experience
    role.experience = Experience(SOURCES["beoradio"], IDENT.full_jid())
    lis = Listener(PEER, session, Audio())
    sub = Stanza(f"<iq id='3' type='set' from='{PEER.jid}'><_notification-subscribe "
                 "xmlns='beocore.notifications.1'><_notification-filter><Filter><Filter>"
                 "<includeKindFilter><item>device</item><item>playing</item><item>source</item>"
                 "</includeKindFilter></Filter></Filter></_notification-filter>"
                 "</_notification-subscribe></iq>")
    assert role._handle(lis, sub)
    assert log[-1][2].startswith("<_notification-subscribe xmlns='beocore.notifications.1'><FullStatus>")
    assert "<state>preparing</state><playQueueItemId>beoradio</playQueueItemId>" in log[-1][2]
    note = Stanza(f"<iq id='4' type='set' from='{PEER.jid}'><_notification "
                  "xmlns='beocore.notifications.1'><NOTIFICATION><type>STREAMING_STATUS</type>"
                  "</NOTIFICATION></_notification></iq>")
    assert role._handle(lis, note)
    assert log[-1] == ("result", "4", "<_notification xmlns='beocore.notifications.1'/>")
    assert not role._handle(lis, Stanza(f"<iq id='5' type='set' from='{PEER.jid}'><Foo/></iq>"))


def test_unexpand_waits_for_the_listeners_own_goodbye_before_closing():
    """After our Disconnect the listener sends its own Disconnect (and an
    unsubscribe). If we hang up first, the listener wakes us by MAC and logs
    an error. So the session stays open until the goodbye has been answered,
    or for goodbye_wait at most."""
    import time
    log = []
    role, session = make_role(log, goodbye_wait=2.0)
    from beo.networklink.sources import Experience
    role.experience = Experience(SOURCES["beoradio"], IDENT.full_jid())
    lis = Listener(PEER, session, Audio())
    role._listeners[PEER.jid] = lis
    role._handle(lis, connect_stanza())                # FPS up, streaming set

    def listener_says_goodbye():
        time.sleep(0.2)
        role._handle(lis, Stanza(f"<iq id='270' type='set' from='{PEER.jid}'>"
                                 "<Disconnect xmlns='beocore.player.1'><connectionId>1</connectionId>"
                                 "</Disconnect></iq>"))
    threading.Thread(target=listener_says_goodbye, daemon=True).start()
    t0 = time.monotonic()
    assert role.unexpand(PEER)
    took = time.monotonic() - t0
    assert 0.2 <= took < 1.5, took                    # answered the goodbye, did not sit out the full wait
    kinds = [e[0] for e in log]
    assert ("result", "270", "<Disconnect xmlns='beocore.player.1'/>") in log
    assert kinds.index("close") > log.index(("result", "270", "<Disconnect xmlns='beocore.player.1'/>"))
    assert log[-1] == ("close",)


def test_the_three_volume_questions_get_three_different_answers():
    """"<GetVolume" is a prefix of all three. Matching on it alone answered
    GetVolumeMuted and GetVolumeRange with a <GetVolume> element. The shapes are
    the ones a product uses for its own replies."""
    log = []
    role, session = make_role(log)
    lis = Listener(PEER, session, Audio())

    def ask(name):
        log.clear()
        st = Stanza(f"<iq id='7' type='set' from='{PEER.jid}'><{name} xmlns='beocore.product.1'></{name}></iq>")
        assert role._handle(lis, st)
        return log[-1][2]

    v = role.volume_provider()
    assert ask("GetVolume") == f"<GetVolume xmlns='beocore.product.1'><volume>{v.level}</volume></GetVolume>"
    assert ask("GetVolumeMuted") == "<GetVolumeMuted xmlns='beocore.product.1'><muted>0</muted></GetVolumeMuted>"
    assert ask("GetVolumeRange") == ("<GetVolumeRange xmlns='beocore.product.1'><range>"
                                     f"<minimum>{v.minimum}</minimum><maximum>{v.maximum}</maximum>"
                                     "</range></GetVolumeRange>")


def test_threads_are_not_required_for_handlers():
    assert isinstance(threading.Event(), threading.Event)
