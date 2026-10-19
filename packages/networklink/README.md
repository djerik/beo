# beo-networklink

Bang & Olufsen NetworkLink for network-generation products, as the products
speak it on the wire: the beocore XMPP control plane on port 5222, the FPS
audio transport on UDP/TCP 1234 with PTP-timed PTS, mDNS presence, the
products' PTP clock, and the NSDK side door on port 80.

The protocol is undocumented. Every XML body, field order, iq id, timing
constant and byte layout here is the one shipping products use.

Version 0.1.0 — Python >= 3.9, runtime dependency: `zeroconf`; `dbus` (python3-dbus) where
the product's records are to go through an Avahi daemon, which is where one runs.

## Module map

| Module | What | Threading |
|---|---|---|
| `beo.networklink.sources` | `SourceDescriptor`, `Catalog`, `Experience`, `NowPlaying`, `Volume`, `StatusBuilder`, `Requests`; `STANDARD_SOURCES` (what a networked product lists), `SOURCES` (what one can render) | pure functions |
| `beo.networklink.beocore` | `Session` (one XMPP connection to one peer, opened by us or accepted from the peer; a dialled peer's jid is read from its stream header), `Server` (the port peers open sessions on), `Stanza`, `IdCounter` (decimal ids only) | one reader thread per session; one accept thread |
| `beo.networklink.roles.source` | `SourceRole`: expand / unexpand / set_state / set_now_playing, the handshake and teardown; `adopt` for a listener that dialled us for a source it borrowed or joined, a Connect that names another of our sources switches the experience, the transport keys a listener sends through the tunnel go to `on_key`, and `experience_provider` says what plays while nobody listens | blocking `expand()`; one progress thread per listener |
| `beo.networklink.roles.listener` | `ListenerRole`: answer a source's ConnectRequest, send our Connect, receive its stream and notifications, leave. The stream goes to whatever sink is set, and is only counted without one | one thread per connect; one receiver thread |
| `beo.networklink.fps` | `AudioFormat`, `FpsClock` / `OffsetPtpClock` / `MonotonicClock`, `build_header`, `build_marker`, `FpsSender` (keeps its last six seconds for a listener that asks for a datagram again), `Retransmitter` (one per process on the stream port plus one, where those requests arrive), `FpsReceiver` | sender: pump + heartbeat threads; one request thread per port |
| `beo.networklink.ptp` | `PtpSlaveClock`: an IEEE 1588 PTPv2 slave that reads the products' own clock off the network | one listener thread |
| `beo.networklink.audio` | `AudioSource` protocol, `ToneSource`, `Silence`, `Fanout` (counts the blocks it drops for a slow subscriber), `AlsaCapture` (arecord), `AlsaPlayback` (aplay, each received block at its network time) | reader threads; one writer thread |
| `beo.networklink.configuration` | the configuration a network of products shares: build a product's entry, find the leader, read the document, add the entry if it is missing or send it again when it no longer says what we write | blocking; one session per request |
| `beo.networklink.nsdk` | `NsdkClient` (port 80 getData/setData/stream/fetch_logs), `FpsdLog` (parse a listener's fpsd log, estimate the PTP offset, `health()` for how its last minutes of listening went; `python -m beo.networklink.nsdk <ip>` prints it) | blocking urllib |
| `beo.networklink.discovery` | `Peer`, `Browser`, `PeerDirectory`, `Advertiser` (the four mDNS records through zeroconf, TTL 120/4500), `AvahiAdvertiser` (the same through the machine's Avahi daemon, which writes the jid as the one name label the products ask for), `advertiser()` (picks Avahi where a daemon answers) | zeroconf's own threads; Avahi's daemon |

`ProductIdentity` (jid, name, MAC, type/item/serial, host) lives in
`beo.product.identity` and is re-exported here.

### Threads now, asyncio planned

Everything in 0.1.0 is thread-based.
`FpsSender` will stay a thread (FPS pacing does not tolerate event-loop
jitter; 0.5 ms/s drift was measured from a sloppy schedule). The control
plane — `Session`, a `SessionManager` holding **one session per peer per
process** (the listener allows two sockets per peer and closes the oldest),
and `ListenerRole` — is planned to move to asyncio in a later version. The
`AudioSource` / `FpsClock` interfaces will not change.

## Usage

```python
from beo.networklink import (Catalog, Peer, ProductIdentity, SOURCES, STANDARD_SOURCES,
                             SourceRole, ToneSource, AlsaCapture, Fanout, FORMAT_44100)
from beo.networklink.ptp import PtpSlaveClock

ident = ProductIdentity.from_jid("2730.1200322.90000001", "Reference Source", "00:00:5E:00:53:01")
peer = Peer(jid="2730.1200322.90000010@products.bang-olufsen.com", ip="203.0.113.10",
            product_type="CA17")

# audio in: one ALSA capture shared by every listener (Pi) …
fanout = Fanout(AlsaCapture("hw:0,0", FORMAT_44100))
audio = fanout.subscribe(peer.jid)
# … or a test tone
audio = ToneSource(FORMAT_44100, 440.0, 0.35)

clock = PtpSlaveClock().start()    # follows the products' own PTP on the network
clock.wait_for_fix(4.0)
role = SourceRole(ident, Catalog(STANDARD_SOURCES), clock)
listener = role.expand(peer, SOURCES["beoradio"], audio)   # Attach -> ConnectRequest -> Connect -> FPS -> opening
role.set_state("pause")    # PROGRESS pause + marker (MuteAllSinks)
role.set_state("play")
role.unexpand(peer)        # unsubscribe, SEC stop, ENDED, PROGRESS stop, SOURCE, Disconnect, FPS stop, </stream>
fanout.unsubscribe(peer.jid)
```

A listener plays only what carries a PTS in the network's PTP time, which is
what `PtpSlaveClock` reads off the network. Where no master can be heard, the
fallback is `OffsetPtpClock` with an offset measured once from a listener's own
log: stream once (the listener rejects the PTSs), then

```python
from beo.networklink.nsdk import NsdkClient, FpsdLog
log = FpsdLog.parse(NsdkClient("203.0.113.10").fetch_logs())
offset_ns = log.estimate_offset_ns()      # holds until this machine reboots or the grandmaster changes
```

`examples/tone_to_speaker.py` is an end-to-end example using that fallback.

## Tests

From the repository root:

    python -m pytest packages/networklink

`tests/golden/*.xml` pin the wire forms under the identities in
`tests/golden_identities.py`; a difference is a wire change and must be meant, and
running `tests/golden_identities.py` re-pins them. Every identity in the tests is
synthetic. `test_fps_loopback.py` runs a real sender/receiver pair on
127.0.0.1:12340.
