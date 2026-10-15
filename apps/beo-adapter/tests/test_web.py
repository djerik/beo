"""The product's page: one encoder for every browser, fed by the machine or
by the stream the product receives, and only while somebody listens."""
import io
import json
import queue
import time

import pytest

from beo.networklink.fps import FORMAT_44100
from beo_adapter import web
from beo_adapter.web import Encoder, WebStream


def until(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class Pipe:
    """ffmpeg's stdin as the encoder writes it."""

    def __init__(self):
        self.written = []

    def write(self, b):
        self.written.append(bytes(b))

    def flush(self):
        pass


class Out:
    """ffmpeg's stdout: what the test hands it, then the end once killed."""

    def __init__(self):
        self.q = queue.Queue()

    def read1(self, n):
        return self.q.get()


class Ffmpeg:
    def __init__(self, argv, **_):
        self.argv, self.stdin, self.stdout = argv, Pipe(), Out()
        self.killed, self.pid = False, 1

    def kill(self):
        self.killed = True
        self.stdout.q.put(b"")

    def wait(self, timeout=None):
        return 0


class Popen:
    def __init__(self, fails=False):
        self.made, self.fails = [], fails

    def __call__(self, argv, **kw):
        if self.fails:
            raise FileNotFoundError("ffmpeg")
        p = Ffmpeg(argv, **kw)
        self.made.append(p)
        return p


class Capture:
    """The machine's Fanout, as far as the page uses it."""

    def __init__(self):
        self.subs, self.ended = {}, []

    def subscribe(self, key):
        q = queue.Queue()
        self.subs[key] = q

        class Sub:
            def read_block(self_):
                try:
                    return q.get(timeout=0.01)
                except queue.Empty:
                    return None
        return Sub()

    def unsubscribe(self, key):
        self.subs.pop(key, None)
        self.ended.append(key)

    def play(self, pcm):
        for q in list(self.subs.values()):
            q.put(pcm)


class Block:
    def __init__(self, pcm):
        self.pcm = pcm


def stream(**kw):
    popen = Popen()
    s = WebStream(popen=popen, **kw)
    return s, popen


def test_the_formats_a_browser_can_be_sent():
    aac = Encoder(FORMAT_44100, "aac", "256k", lambda c: None).argv()
    assert aac[aac.index("-c:a") + 1] == "aac" and aac[aac.index("-f", 5) + 1] == "adts"
    assert aac[aac.index("-ar") + 1] == "44100" and "256k" in aac
    mp3 = Encoder(FORMAT_44100, "mp3", "320k", lambda c: None).argv()
    assert "libmp3lame" in mp3 and mp3[-2:] == ["mp3", "pipe:1"]
    with pytest.raises(ValueError):
        WebStream(codec="flac")


def test_the_first_browser_starts_the_capture_and_the_last_stops_it():
    s, popen = stream()
    capture, asked = Capture(), []
    s.machine, s.on_machine = capture, lambda: asked.append(True)
    a = s.connect()
    b = s.connect()
    assert len(popen.made) == 1, "one encoder for every browser"
    assert until(lambda: capture.subs) and asked == [True]
    capture.play(b"machine")
    assert until(lambda: b"machine" in popen.made[0].stdin.written)
    a.close()
    assert capture.subs and not popen.made[0].killed
    b.close()
    assert until(lambda: not capture.subs) and popen.made[0].killed


def test_what_the_encoder_writes_reaches_every_browser():
    s, popen = stream()
    a, b = s.connect(), s.connect()
    popen.made[0].stdout.q.put(b"frame")
    assert until(lambda: not a.q.empty() and not b.q.empty())
    assert a.read() == b"frame" and b.read() == b"frame"
    a.close(), b.close()


def test_a_browser_that_does_not_keep_up_loses_the_oldest_alone():
    s, popen = stream(client_depth=2)
    slow, fast = s.connect(), s.connect()
    out = popen.made[0].stdout.q
    for chunk in (b"1", b"2", b"3"):
        out.put(chunk)
        assert until(lambda: fast.read(0.5) == chunk)
    assert until(lambda: slow.q.qsize() == 2)
    assert [slow.read(), slow.read()] == [b"2", b"3"]
    slow.close(), fast.close()


def test_the_received_stream_takes_the_machines_place_and_gives_it_back():
    s, popen = stream()
    capture = Capture()
    s.machine = capture
    client = s.connect()
    assert until(lambda: capture.subs)
    feed = s.open()
    assert s.feed == "network"
    assert until(lambda: not capture.subs), "the capture is let go while the stream plays"
    feed.write(Block(b"received"))
    assert until(lambda: b"received" in popen.made[0].stdin.written)
    feed.stop()
    assert s.feed == "machine" and until(lambda: capture.subs)
    feed.write(Block(b"late"))
    time.sleep(0.05)
    assert b"late" not in popen.made[0].stdin.written
    client.close()


def test_a_stream_received_with_nobody_listening_is_not_encoded():
    s, popen = stream()
    feed = s.open()
    feed.write(Block(b"received"))
    assert popen.made == []
    client = s.connect()
    feed.write(Block(b"now"))
    assert until(lambda: b"now" in popen.made[0].stdin.written)
    client.close()
    feed.stop()


def test_with_no_machine_the_browsers_hear_silence():
    s, popen = stream()
    client = s.connect()
    silence = bytes(FORMAT_44100.payload_bytes)
    assert until(lambda: popen.made[0].stdin.written.count(silence) >= 2)
    client.close()


def test_an_encoder_that_ends_lets_the_browsers_go_and_the_next_starts_another():
    s, popen = stream()
    client = s.connect()
    popen.made[0].stdout.q.put(b"")
    with pytest.raises(EOFError):
        while True:
            client.read(0.05)
    client.close()
    again = s.connect()
    assert len(popen.made) == 2
    again.close()


# ---- HTTP ------------------------------------------------------------------
class Request:
    def __init__(self, path):
        self.path, self.wfile = path, io.BytesIO()
        self.status, self.headers, self.close_connection = None, {}, False
        self.command = "GET"

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers[k] = v

    def end_headers(self):
        pass


def get(s, path):
    r = Request(path)
    s.serve(r, path.split("?")[0].rstrip("/") or "/")
    return r


def test_the_page_is_served_from_its_directory_and_nothing_else_is():
    s, _ = stream()
    assert get(s, "/web").status == 301 and get(s, "/web").headers["Location"] == "/web/"
    page = get(s, "/web/")
    assert page.status == 200 and page.headers["Content-Type"].startswith("text/html")
    assert b"stream" in page.wfile.getvalue()
    assert get(s, "/web/manifest.webmanifest").status == 200
    assert get(s, "/web/icon-180.png").headers["Content-Type"] == "image/png"
    assert get(s, "/web/../web.py").status == 404
    assert get(s, "/web/missing.html").status == 404


def test_without_an_encoder_the_browser_is_told_so():
    s = WebStream(popen=Popen(fails=True))
    r = get(s, "/web/stream")
    assert r.status == 503 and s._clients == {}


def test_the_stream_is_sent_until_the_browser_goes(monkeypatch):
    s, popen = stream()

    class Gone(io.BytesIO):
        def write(self, b):
            super().write(b)
            raise BrokenPipeError

    r = Request("/web/stream")
    r.wfile = Gone()
    connect = WebStream.connect

    def primed(self):
        client = connect(self)
        popen.made[0].stdout.q.put(b"frame")
        return client
    monkeypatch.setattr(web.WebStream, "connect", primed)
    s._serve_stream(r)
    assert r.status == 200 and r.headers["Content-Type"] == "audio/aac" and r.close_connection
    assert r.wfile.getvalue() == b"frame"
    assert s._clients == {} and popen.made[0].killed



def test_the_status_says_what_is_heard_whom_the_product_listens_to_and_how_a_join_went():
    s, _ = stream()

    class Role:
        current = None

    class Listening:
        role, last_join = Role(), {"jid": "a", "state": "failed"}

    assert s.status() == {"feed": "silence", "listeningTo": None, "playing": None,
                          "sourceState": "", "sourceVolume": None, "join": None}
    s.listening = Listening()
    Listening.role.current = type("Cur", (), {"source_jid": "b"})()
    r = get(s, "/web/status")
    assert r.status == 200 and r.headers["Content-Type"] == "application/json"
    assert json.loads(r.wfile.getvalue()) == {"feed": "silence", "listeningTo": "b",
                                              "playing": None, "sourceState": "",
                                              "sourceVolume": None, "join": {"jid": "a", "state": "failed"}}


class Listened:
    """BeolinkIn while it listens to the product at 203.0.113.7."""

    class role:
        current = type("Cur", (), {"source_jid": "src@x"})()


def test_the_keys_go_to_the_product_listened_to(monkeypatch):
    from beo.mozart import facade
    s, _ = stream()
    monkeypatch.setattr(facade, "peer_by_jid", lambda jid: {"ipAddress": "203.0.113.7"})
    asked = []

    class Done:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        asked.append((req.get_method(), req.full_url))
        if ":8080" in req.full_url:
            raise web.urllib.error.URLError("refused")
        return Done()

    s.urlopen = urlopen
    assert s.command_source("pause") == 409, "nothing is listened to"
    s.listening = Listened()
    assert s.command_source("dance") == 404
    assert s.command_source("skip") == 200
    assert asked == [("POST", "http://203.0.113.7:8080/BeoZone/Zone/Stream/Forward"),
                     ("POST", "http://203.0.113.7/api/v1/playback/command/skip")]
    r = Request("/web/source/play")
    r.command = "POST"
    s.serve(r, "/web/source/play")
    assert r.status == 200


def test_a_write_to_the_page_itself_is_refused():
    s, _ = stream()
    r = Request("/web/")
    r.command = "POST"
    s.serve(r, "/web")
    assert r.status == 405


def test_the_volume_set_is_the_sources(monkeypatch):
    from beo.mozart import facade
    s, _ = stream()
    s.listening = Listened()
    monkeypatch.setattr(facade, "peer_by_jid", lambda jid: {"ipAddress": "203.0.113.7"})
    asked = []

    class Done:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        asked.append((req.get_method(), req.full_url, json.loads(req.data)))
        return Done()

    s.urlopen = urlopen
    r = Request("/web/source/volume")
    r.command, r.body = "POST", b'{"level": 25}'
    s.serve(r, "/web/source/volume")
    assert r.status == 200
    assert asked == [("PUT", "http://203.0.113.7:8080/BeoZone/Zone/Sound/Volume/Speaker/Level",
                      {"level": 25})]
    assert s.set_source_volume(muted=True) == 200 and asked[-1][2] == {"muted": True}
    assert s.set_source_volume() == 400


def test_the_sources_volume_is_read_from_the_source(monkeypatch):
    from beo.mozart import facade
    s, _ = stream()
    s.listening = Listened()
    monkeypatch.setattr(facade, "peer_by_jid", lambda jid: {"ipAddress": "203.0.113.7"})
    level = [13]

    def urlopen(url, timeout):
        if not isinstance(url, str):
            level[0] = json.loads(url.data)["level"]
            return io.BytesIO(b"{}")
        if ":8080/" in url:
            return io.BytesIO(json.dumps({"speaker": {"level": level[0], "muted": False,
                                                      "range": {"minimum": 0, "maximum": 90}}}).encode())
        raise web.urllib.error.URLError("no such route")

    s.urlopen = urlopen
    assert s.source_volume() == {"level": 13, "maximum": 90, "muted": False}
    assert s.set_source_volume(level=30) == 200
    assert s.source_volume()["level"] == 30, "a level just set is read again, not kept"
