#!/usr/bin/env python3
"""
Minimal WebSocket notification server — the channel a Mozart product pushes
state changes on.

The app opens ws://{ip}:9339 the moment it connects to a product, with a
5 s keep-alive. Without it the REST answers are a snapshot the app can never
learn has changed. Messages are JSON envelopes dispatched on `eventType`:

    {"eventType": "WebSocketEventSourceChange", "eventData": { ... }}

What the app expects on this channel is in docs/app-protocol.md §8.

Implemented by hand rather than with the `websockets` package so the facade
keeps its single dependency (zeroconf). Only what the app needs: the RFC 6455
handshake, masked client frames, ping/pong (required, or .NET drops the
socket after its keep-alive timeout) and server text frames.
"""
import base64
import hashlib
import json
import logging
import socket
import struct
import threading

log = logging.getLogger("beo.mozart.ws")

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x8, 0x9, 0xA


class NotificationServer:
    """Accepts WebSocket clients and broadcasts JSON events to them."""

    def __init__(self, port=9339):
        self.port = port
        self._clients = []
        self._lock = threading.Lock()
        self._sock = None

    # ---- lifecycle ---------------------------------------------------------
    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.listen(8)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        log.info("WS:   notifications on 0.0.0.0:%d", self.port)

    def _accept_loop(self):
        while True:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True).start()

    # ---- one client --------------------------------------------------------
    def _serve(self, conn, addr):
        try:
            if not self._handshake(conn):
                conn.close()
                return
            with self._lock:
                self._clients.append(conn)
            log.info("WS:   client connected from %s (%d total)", addr[0], len(self._clients))
            self._send_snapshot(conn, addr)
            self._read_loop(conn)
        except Exception as e:
            log.info("WS:   client %s dropped (%s)", addr[0], e)
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _handshake(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return False
            data += chunk
            if len(data) > 65536:
                return False
        headers = {}
        for line in data.split(b"\r\n")[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        key = headers.get(b"sec-websocket-key")
        if not key:
            return False
        accept = base64.b64encode(hashlib.sha1(key + WS_GUID).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                     b"Upgrade: websocket\r\n"
                     b"Connection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        return True

    def _recv_exactly(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _read_loop(self, conn):
        while True:
            b1, b2 = self._recv_exactly(conn, 2)
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exactly(conn, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exactly(conn, 8))[0]
            mask = self._recv_exactly(conn, 4) if masked else b""
            payload = self._recv_exactly(conn, length) if length else b""
            if masked:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))

            if opcode == OP_CLOSE:
                self._send_frame(conn, OP_CLOSE, b"")
                return
            if opcode == OP_PING:
                # Mandatory: .NET's KeepAliveTimeout closes the socket without it.
                self._send_frame(conn, OP_PONG, payload)
            # Text/pong from the client needs no action — the app only listens.

    def _send_frame(self, conn, opcode, payload: bytes):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack("!H", n)
        else:
            header.append(127)
            header += struct.pack("!Q", n)
        conn.sendall(bytes(header) + payload)      # server frames are unmasked

    # ---- the snapshot a new client gets -------------------------------------
    #: () -> iterable of (event_type, event_data); set by the facade. The app
    #: fetches nothing on connect and renders from the last event of each type,
    #: so every state is pushed once here (docs/app-protocol.md §8).
    on_connect = None

    def _send_snapshot(self, conn, addr):
        if self.on_connect is None:
            return
        try:
            events = list(self.on_connect())
        except Exception as e:        # the snapshot must never cost the connection
            log.warning("WS:   snapshot failed: %s", e)
            return
        for event_type, event_data in events:
            msg = json.dumps({"eventType": event_type, "eventData": event_data}).encode()
            self._send_frame(conn, OP_TEXT, msg)
        log.info("WS:   -> snapshot of %d events to %s", len(events), addr[0])

    # ---- broadcasting ------------------------------------------------------
    def send(self, event_type, event_data):
        """Push one {"eventType", "eventData"} envelope to every client."""
        msg = json.dumps({"eventType": event_type, "eventData": event_data}).encode()
        with self._lock:
            targets = list(self._clients)
        if not targets:
            return 0
        sent = 0
        for conn in targets:
            try:
                self._send_frame(conn, OP_TEXT, msg)
                sent += 1
            except Exception:
                with self._lock:
                    if conn in self._clients:
                        self._clients.remove(conn)
        log.info("WS:   -> %s to %d client(s)", event_type, sent)
        return sent
