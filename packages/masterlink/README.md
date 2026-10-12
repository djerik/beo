# beo-masterlink

Python library for the Bang & Olufsen **MasterLink** bus. It knows nothing
about the network side; see `beo.networklink` for that. It speaks to the ML
roles (audio master, video master, source center, link node) and is not tied
to one product: what is peculiar to a machine is a profile in
`beo.masterlink.profiles`, which is data.

| Module | What |
|---|---|
| `beo.masterlink.codec` | `Telegram` (parse / build / checksum), `Beo4` key codes, payload decoders for `0x87` STATUS_INFO, `0x40` CLOCK, `0x2c` DISPLAY, `0x50` NAK, `0x08` distributed source |
| `beo.masterlink.bus` | `MlTransport` protocol, `SerialTransport` (owns the FTDI/UART port, does the mark/space parity framing, drops our own echo), `MlBus` (one writer, paced presses, subscribers) |
| `beo.masterlink.music_system` | `MusicSystemState`, `MusicSystem`: state driven only by the machine's own `0x87`, an operation queue with per-kind policies, `select_source` / `select_disc` / `goto_track` / `next` / `prev` / `play` / `stop` / `standby`, `distributed_source()` |
| `beo.masterlink.profiles` | `MusicSystemProfile`: a machine's sources (slot, source byte, selecting key), how one of its discs is selected if it has several, and whether it was verified on the machine. `BS9000` is the verified one; `BS3000`, `BS3200`, `BS4` and `BC2` are there to be tried, as machines with one disc |
| `beo.masterlink.roles.source_center` | `SourceCenter`: be an ML SourceCenter (`0xC2`) offering N.RADIO / N.MUSIC to the audio master |

Threads, not asyncio, in this version: the measured timings live in
`time.sleep` calls. An asyncio `MlBus` is the
planned next step and does not change the public API.

## Measured rules this code encodes

- **Framing is timing + content.** The FTDI FT-X on MDT v1 flags a parity error
  on every byte whatever parity the receiver is set to, so the 9-bit mark/space
  delimiter cannot be recovered. A telegram ends on ≥ 5 ms of silence and must end
  in `00` with a valid checksum. Set the FTDI `latency_timer` to 1 ms (udev rule
  in `deploy/`), otherwise USB batches bytes and splits telegrams.
- **Sending** = first byte with MARK parity, body with SPACE, trailing `00` with
  MARK, `tcdrain` between the three writes. Our own bytes echo back on the
  half-duplex pair and are dropped by the receiver.
- **≥ 0.35 s between Beo4 presses.** The audio master confirms a press with `0x87`
  after 0.1–0.9 s. Disc selection (the `BS9000` profile): a bare digit while the 9000 listens on
  CD, confirmed by `0x87` with the new disc; when the digit goes unanswered,
  Standby → 3.5 s → CD → 0.3 s → digit opens the window in which a digit means disc.
- **State comes only from `0x87`.** A press never changes state; the machine does.
- The audio master answers only a handful of request types, among them `0x08` /
  `0x0b` (distributed source), `0x44` (track info) and `0x45` (the link-room
  request). Everything else is NAKed with `0x50`.

## Running the tests

From the repository root:

    python -m pytest packages/masterlink

There are no recordings in the tree. The tests build their bus traffic from
telegrams, and the framer is fed them cut the way a USB serial adapter cuts
them.
