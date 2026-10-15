"""The product in a browser: its sound, and the transport beside it.

    /web/          the page. It steers the product through api/v1 and hears
                   about it on the notification socket, the way the app does,
                   so there is one way to control the product and not two.
    /web/status    what a browser hears, whom the product listens to and what
                   plays there, and how the last join went.
    /web/source/<key>
                   POST play | pause | skip | prev to the product this one
                   listens to, which is where those keys mean something then,
                   and volume, {"level": n} or {"muted": b}, to set its volume.
    /web/stream    what the product plays, encoded once for every browser
                   that listens. An <audio> element plays it, and that keeps
                   playing on a locked phone where Web Audio is stopped.

What the browser hears is what the product plays: the machine's sound,
captured, or while the product listens to another, the stream it receives.
One at a time, and a browser's connection outlives the change. ADTS and MP3
are frames that each stand alone, so a decoder carries on into the next
feed without a header to read first.

The encoder runs while somebody listens and not otherwise, and so does the
capture it reads.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

from beo.networklink.fps import AudioFormat, FORMAT_44100

log = logging.getLogger("beo-adapter.web")

PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PREFIX = "/web"

# The encoder's name in ffmpeg, the container it writes, and what a browser
# is told it is.
CODECS = {
    "aac": ("aac", "adts", "audio/aac"),
    "mp3": ("libmp3lame", "mp3", "audio/mpeg"),
}

# The transport of a product of the generation before Mozart, as its own API
# names it. A Mozart product takes the page's words in api/v1 directly.
BEOZONE_COMMANDS = {"play": "Play", "pause": "Pause", "skip": "Forward", "prev": "Backward",
                    "stop": "Stop"}

# How long a source's volume, once read, answers for it.
VOLUME_KEEP_S = 1.5

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class Encoder:
    """One ffmpeg: PCM in, encoded frames out to ``on_chunk`` as they come.

    Written to through a queue of its own, so a stalled encoder makes the
    stream lose blocks and never holds up the thread that delivers them,
    which may be the one that receives from another product.
    """

    def __init__(self, fmt: AudioFormat, codec: str, bitrate: str,
                 on_chunk: Callable[[bytes], None], *, popen=subprocess.Popen, depth: int = 64):
        self.fmt, self.codec, self.bitrate = fmt, codec, bitrate
        self.on_chunk = on_chunk
        # Told when the encoder ends without being stopped.
        self.on_end: Callable[[], None] = lambda: None
        self.popen = popen
        self.q: "queue.Queue" = queue.Queue(maxsize=depth)
        self.proc = None
        self._stop = threading.Event()

    def argv(self) -> list:
        encoder, muxer, _ = CODECS[self.codec]
        return ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(self.fmt.rate_hz), "-ac", str(self.fmt.channels),
                "-i", "pipe:0",
                "-c:a", encoder, "-b:a", self.bitrate, "-ar", str(self.fmt.rate_hz),
                "-flush_packets", "1", "-f", muxer, "pipe:1"]

    def start(self) -> "Encoder":
        """OSError when there is no ffmpeg to start."""
        self.proc = self.popen(self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
        threading.Thread(target=self._write, args=(self.proc,), daemon=True,
                         name="web-encode-in").start()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True,
                         name="web-encode-out").start()
        log.info("encoding %s at %s for the browsers", self.codec, self.bitrate)
        return self

    def write(self, pcm: bytes) -> None:
        try:
            self.q.put_nowait(pcm)
        except queue.Full:
            try:
                self.q.get_nowait()
                self.q.put_nowait(pcm)
            except (queue.Empty, queue.Full):
                pass

    def _write(self, proc) -> None:
        while not self._stop.is_set():
            try:
                pcm = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                proc.stdin.write(pcm)
                proc.stdin.flush()
            except (OSError, ValueError):
                return

    def _read(self, proc) -> None:
        while True:
            try:
                chunk = proc.stdout.read1(4096)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            self.on_chunk(chunk)
        if not self._stop.is_set():
            log.error("the encoder ended by itself; the browsers are let go")
            self.on_end()

    def stop(self) -> None:
        self._stop.set()
        proc, self.proc = self.proc, None
        if proc is None:
            return
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.error("the encoder could not be killed, pid %d", proc.pid)


class _Client:
    """One browser's share of the stream. ``read`` gives the next chunk,
    None while there is none yet, and raises EOFError once it is over."""

    END = object()

    def __init__(self, stream: "WebStream", key: int, q: "queue.Queue"):
        self.stream, self.key, self.q = stream, key, q

    def read(self, timeout: float = 1.0) -> Optional[bytes]:
        try:
            item = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is self.END:
            raise EOFError
        return item

    def close(self) -> None:
        self.stream._leave(self.key)


class _NetworkFeed:
    """The stream the product receives, as one more output of BeolinkIn's."""

    def __init__(self, stream: "WebStream"):
        self.stream = stream

    def write(self, block) -> None:
        self.stream._network_block(self, block.pcm)

    def stop(self) -> None:
        self.stream._network_ended(self)


class WebStream:
    """The product's sound for the browsers, from whichever feed plays now.

    ``machine`` is the Fanout of the machine's capture, or None where there
    is none; the browsers then hear silence until another product plays to
    this one. ``on_machine`` asks the machine to put its sound on the bus,
    as an expand does, when the capture is started for a browser.
    """

    def __init__(self, fmt: AudioFormat = FORMAT_44100, *, codec: str = "aac",
                 bitrate: str = "256k", popen=subprocess.Popen, client_depth: int = 256):
        if codec not in CODECS:
            raise ValueError("no such format %r; the choices are %s" % (codec, ", ".join(CODECS)))
        self.fmt, self.codec, self.bitrate = fmt, codec, bitrate
        self.popen = popen
        self.client_depth = client_depth
        self.machine = None
        self.on_machine: Optional[Callable[[], object]] = None
        # BeolinkIn, where the product can listen to another.
        self.listening = None
        self.urlopen = urllib.request.urlopen
        # The source's volume, as last read from it, and when.
        self._volume: Optional[dict] = None
        self._volume_at = 0.0
        self._clients: Dict[int, "queue.Queue"] = {}
        self._ids = itertools.count(1)
        self._network: Optional[_NetworkFeed] = None
        self._encoder: Optional[Encoder] = None
        self._feeding: Optional[threading.Event] = None
        self._lock = threading.Lock()

    @property
    def content_type(self) -> str:
        return CODECS[self.codec][2]

    @property
    def feed(self) -> str:
        """network | machine | silence: what a browser would hear now."""
        if self._network is not None:
            return "network"
        return "machine" if self.machine is not None else "silence"

    # ---- browsers ---------------------------------------------------------
    def connect(self) -> _Client:
        """OSError when the encoder cannot be started."""
        q: "queue.Queue" = queue.Queue(maxsize=self.client_depth)
        with self._lock:
            key = next(self._ids)
            if self._encoder is None:
                encoder = Encoder(self.fmt, self.codec, self.bitrate, self._distribute,
                                  popen=self.popen)
                encoder.on_end = lambda: self._encoder_ended(encoder)
                self._encoder = encoder.start()
                self._feed()
            self._clients[key] = q
        log.info("a browser listens (%d now), to the %s", len(self._clients), self.feed)
        return _Client(self, key, q)

    def _leave(self, key: int) -> None:
        with self._lock:
            if self._clients.pop(key, None) is None:
                return
            remaining = len(self._clients)
            encoder = None
            if not remaining:
                encoder, self._encoder = self._encoder, None
                self._feed()
        log.info("a browser stopped listening (%d now)", remaining)
        if encoder is not None:
            encoder.stop()

    def _distribute(self, chunk: bytes) -> None:
        with self._lock:
            qs = list(self._clients.values())
        for q in qs:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                # A browser that does not keep up loses the oldest frames and
                # nobody else's. The decoder finds the next frame by itself.
                try:
                    q.get_nowait()
                    q.put_nowait(chunk)
                except (queue.Empty, queue.Full):
                    pass

    def _encoder_ended(self, encoder: Encoder) -> None:
        """The browsers are let go, and the next to connect starts a new one."""
        with self._lock:
            if self._encoder is not encoder:
                return
            self._encoder = None
            self._feed()
            qs = list(self._clients.values())
        encoder.stop()
        for q in qs:
            while True:
                try:
                    q.put_nowait(_Client.END)
                    break
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass

    # ---- the feeds ----------------------------------------------------------
    def open(self, now_ns=None) -> _NetworkFeed:
        """The product receives a stream from now on: the browsers hear it.
        Shaped like Speaker.open, so BeolinkIn adds the browsers as an output
        beside its sound cards. The network's clock is not used: a browser
        plays what arrives, as it arrives."""
        sink = _NetworkFeed(self)
        with self._lock:
            self._network = sink
            self._feed()
        log.info("the browsers hear the stream the product receives")
        return sink

    def _network_block(self, sink: _NetworkFeed, pcm: bytes) -> None:
        encoder = self._encoder
        if self._network is sink and encoder is not None:
            encoder.write(pcm)

    def _network_ended(self, sink: _NetworkFeed) -> None:
        with self._lock:
            if self._network is not sink:
                return
            self._network = None
            self._feed()
        log.info("the browsers hear the %s again", self.feed)

    def _feed(self) -> None:
        """Start the feed that plays now and end the one that did. Called
        under the lock. Only the machine's needs a thread: the received
        stream is pushed in by the receiver."""
        if self._feeding is not None:
            self._feeding.set()
            self._feeding = None
        if self._encoder is None or self._network is not None:
            return
        stop = self._feeding = threading.Event()
        threading.Thread(target=self._feed_machine, args=(self._encoder, stop), daemon=True,
                         name="web-machine").start()

    def _feed_machine(self, encoder: Encoder, stop: threading.Event) -> None:
        # A key of its own per run: the run that ends unsubscribes after the
        # next has subscribed, and must not take the next one's queue with it.
        fanout, key = self.machine, "web-%d" % id(stop)
        sub = fanout.subscribe(key) if fanout is not None else None
        if sub is not None and self.on_machine is not None:
            try:
                self.on_machine()
            except Exception:      # noqa: BLE001
                log.exception("asking the machine for its sound failed")
        try:
            if sub is None:
                # Silence keeps the browsers' connections open until there is
                # something to hear.
                silence = bytes(self.fmt.payload_bytes)
                next_at = time.perf_counter()
                while not stop.is_set():
                    encoder.write(silence)
                    next_at += self.fmt.interval_s
                    stop.wait(max(0.0, next_at - time.perf_counter()))
                return
            while not stop.is_set():
                pcm = sub.read_block()
                if pcm is not None:
                    encoder.write(pcm)
        finally:
            if sub is not None:
                fanout.unsubscribe(key)

    def status(self) -> dict:
        listening = self.listening
        current = getattr(getattr(listening, "role", None), "current", None)
        return {"feed": self.feed,
                "listeningTo": current.source_jid if current is not None else None,
                "playing": getattr(listening, "playing", None) if current is not None else None,
                "sourceState": getattr(listening, "source_state", "") if current is not None else "",
                "sourceVolume": self.source_volume() if current is not None else None,
                "join": getattr(listening, "last_join", None)}

    def command_source(self, command: str) -> int:
        """Send a transport key to the product this one listens to. The HTTP
        status to answer the browser with: 409 when nothing is listened to."""
        if command not in BEOZONE_COMMANDS:
            return 404
        return self._to_source(command, [
            ("POST", ":8080/BeoZone/Zone/Stream/" + BEOZONE_COMMANDS[command], {}),
            ("POST", "/api/v1/playback/command/" + command, {})])

    def source_volume(self) -> Optional[dict]:
        """The volume of the product this one listens to: level, top, muted.

        Asked of the source itself, because it tells its listeners its volume
        once, on joining, and not when it changes. Kept a moment, since every
        browser asks every few seconds.
        """
        if time.monotonic() - self._volume_at < VOLUME_KEEP_S:
            return self._volume
        ip = self._source_ip()
        volume = None
        if ip:
            body = self._read(ip, ":8080/BeoZone/Zone/Sound/Volume/Speaker")
            if body and "speaker" in body:
                sp = body["speaker"]
                volume = {"level": sp["level"], "maximum": sp["range"]["maximum"],
                          "muted": bool(sp["muted"])}
            else:
                body = self._read(ip, "/api/v1/sound/volume")
                if body and "level" in body:
                    volume = {"level": body["level"]["level"], "maximum": body["maximum"]["level"],
                              "muted": bool(body["muted"]["muted"])}
        self._volume, self._volume_at = volume, time.monotonic()
        return volume

    def _read(self, ip: str, route: str) -> Optional[dict]:
        try:
            with self.urlopen("http://%s%s" % (ip, route), timeout=2) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            return None

    def set_source_volume(self, level=None, muted=None) -> int:
        """Set the volume of the product this one listens to: the room that
        plays, which is what a listener in a browser turns up and down."""
        self._volume_at = 0.0
        if level is not None:
            return self._to_source("volume %s" % level, [
                ("PUT", ":8080/BeoZone/Zone/Sound/Volume/Speaker/Level", {"level": int(level)}),
                ("POST", "/api/v1/sound/volume/level", {"level": int(level)})])
        if muted is not None:
            return self._to_source("mute %s" % muted, [
                ("PUT", ":8080/BeoZone/Zone/Sound/Volume/Speaker/Muted", {"muted": bool(muted)}),
                ("POST", "/api/v1/sound/volume/mute", {"muted": bool(muted)})])
        return 400

    def _to_source(self, what: str, routes) -> int:
        """The first route the source takes. The older products answer on
        8080 and a Mozart product on 80, and only one of them has the route."""
        ip = self._source_ip()
        if not ip:
            return 409
        for method, route, body in routes:
            url = "http://%s%s" % (ip, route)
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                with self.urlopen(req, timeout=3):
                    log.info("%s sent to %s", what, url)
                    return 200
            except (urllib.error.URLError, OSError) as e:
                log.info("%s not taken at %s (%s)", what, url, e)
        return 502

    def _source_ip(self) -> Optional[str]:
        from beo.mozart import facade
        current = getattr(getattr(self.listening, "role", None), "current", None)
        if current is None:
            return None
        return facade.peer_by_jid(current.source_jid).get("ipAddress")

    # ---- HTTP ---------------------------------------------------------------
    def serve(self, request, path: str) -> None:
        """Every GET under /web, through the facade's request handler."""
        if path == PREFIX and not request.path.split("?", 1)[0].endswith("/"):
            # The page's own links are relative to the directory.
            request.send_response(301)
            request.send_header("Location", PREFIX + "/")
            request.send_header("Content-Length", "0")
            request.end_headers()
            return
        name = path[len(PREFIX):].lstrip("/") or "index.html"
        if request.command != "GET":
            if name == "source/volume":
                try:
                    wanted = json.loads(getattr(request, "body", b"") or b"{}")
                except ValueError:
                    return _send(request, 400, "text/plain", b"")
                code = self.set_source_volume(wanted.get("level"), wanted.get("muted"))
                return _send(request, code, "application/json", b"{}")
            if name.startswith("source/"):
                code = self.command_source(name[len("source/"):])
                return _send(request, code, "application/json", b"{}")
            return _send(request, 405, "text/plain", b"")
        if name == "stream":
            return self._serve_stream(request)
        if name == "status":
            return _send(request, 200, "application/json", json.dumps(self.status()).encode())
        return _serve_file(request, name)

    def _serve_stream(self, request) -> None:
        try:
            client = self.connect()
        except OSError as e:
            log.error("no stream for the browser: the encoder did not start (%s). "
                      "Is ffmpeg installed?", e)
            return _send(request, 503, "text/plain", b"the encoder did not start")
        # A stream has no length: it ends when the connection does.
        request.close_connection = True
        try:
            request.send_response(200)
            request.send_header("Content-Type", self.content_type)
            request.send_header("Cache-Control", "no-cache, no-store")
            request.send_header("Connection", "close")
            request.end_headers()
            while True:
                chunk = client.read()
                if chunk is not None:
                    request.wfile.write(chunk)
        except (EOFError, OSError):
            pass
        finally:
            client.close()


def _serve_file(request, name: str) -> None:
    """One of the page's own files, and nothing outside them."""
    ext = os.path.splitext(name)[1]
    found = "/" not in name and ext in STATIC_TYPES and os.path.isfile(os.path.join(PAGE_DIR, name))
    if not found:
        return _send(request, 404, "text/plain", b"")
    with open(os.path.join(PAGE_DIR, name), "rb") as f:
        _send(request, 200, STATIC_TYPES[ext], f.read())


def _send(request, code: int, content_type: str, body: bytes) -> None:
    request.send_response(code)
    request.send_header("Content-Type", content_type)
    request.send_header("Content-Length", str(len(body)))
    # Small, and changed with every release or every second: always asked for again.
    request.send_header("Cache-Control", "no-cache")
    request.end_headers()
    request.wfile.write(body)


__all__ = ["CODECS", "Encoder", "WebStream"]
