"""MasterLink telegrams: parse, build, checksum, and the payload decoders.

There is no official specification; the layout below is the one the bus
carries, with names from the interface vendor's public protocol notes.

Telegram layout:

    byte 00  TO           c0 video master, c1 audio master, c2 source center,
                          80 all, 83 all link devices, 06 link node (us)
    byte 01  FROM
    byte 02  01           always
    byte 03  TYPE         0a command, 0b request, 14 response, 2c display, 5e config
    byte 04  destination source (or 00)
    byte 05  origin source (or 00)
    byte 06  00           always
    byte 07  PAYLOAD TYPE
    byte 08  payload length
    byte 09  header flag  not a constant: 01 on requests, 02 on Beo4 presses,
                          the reply subtype on 0x08 answers
    byte 10..n-3  payload
    byte n-2 checksum     sum of all preceding bytes & 0xff
    byte n-1 00           end of telegram

The checksum and trailing 00 are part of `Telegram.raw`; `Telegram.body` is
everything before them.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from beo.product.playback import PlayState
from typing import Optional

HEADER_LEN = 10
MIN_LEN = HEADER_LEN + 2

# Addresses
TO_ALL = 0x80
VIDEO_MASTER = 0xC0
AUDIO_MASTER = 0xC1
SOURCE_CENTER = 0xC2
LINK_NODE = 0x06          # the address we use when acting as a link room node

ADDR_NAMES = {0x80: "ALL", 0x81: "ALL AUDIO LINK", 0x82: "ALL VIDEO LINK",
              0x83: "ALL LINK DEVICES", 0xC0: "VIDEO MASTER", 0xC1: "AUDIO MASTER",
              0xC2: "SOURCE CENTER", 0x06: "LINK NODE", 0x01: "?", 0xF0: "MLGW"}

# Telegram types (byte 03)
CMD, REQ, RESP, DISPLAY = 0x0A, 0x0B, 0x14, 0x2C
TYPE_NAMES = {0x0A: "COMMAND", 0x0B: "REQUEST", 0x14: "RESPONSE", 0x2C: "DISPLAY", 0x5E: "CONFIG"}

# Payload types (byte 07)
PL_DISTRIBUTED_SOURCE = 0x08
PL_BEO4_KEY = 0x0D
PL_STANDBY = 0x10
PL_RELEASE = 0x11
PL_MLGW_BEO4 = 0x20
PL_CLOCK = 0x40
PL_NAK = 0x50
PL_DISTRIBUTION_REQUEST = 0x6C
PL_STATUS_INFO = 0x87

# Sources
SRC_CD, SRC_N_MUSIC, SRC_N_RADIO, SRC_RADIO = 0x8D, 0x7A, 0xA1, 0x6F

HEARTBEAT = bytes.fromhex("8001015e000000140001f500")


class Beo4(enum.IntEnum):
    """Beo4 key codes as they appear in the 0x20 / 0x0d telegrams."""
    DIGIT_0 = 0x00
    DIGIT_1 = 0x01
    DIGIT_2 = 0x02
    DIGIT_3 = 0x03
    DIGIT_4 = 0x04
    DIGIT_5 = 0x05
    DIGIT_6 = 0x06
    DIGIT_7 = 0x07
    DIGIT_8 = 0x08
    DIGIT_9 = 0x09
    STORE = 0x0B
    STANDBY = 0x0C
    SELECT = 0x13
    STEP_UP = 0x1E
    STEP_DOWN = 0x1F
    GOTO = 0x20
    EJECT = 0x2D
    REWIND = 0x32
    RETURN = 0x33
    WIND = 0x34
    PLAY = 0x35
    STOP = 0x36
    RECORD = 0x37
    SLEEP = 0x47
    VOLUME_UP = 0x60
    VOLUME_DOWN = 0x64
    KEY_RELEASE = 0x7E
    MUTE = 0x6C
    TV = 0x80
    RADIO = 0x81
    A_AUX = 0x83
    A_MEM = 0x91
    CD = 0x92
    N_RADIO = 0x93      # same code as PHONO on older remotes
    N_MUSIC = 0x94
    CD2_JOIN = 0x97
    SHIFT_0 = 0xC0
    SHIFT_1_RANDOM = 0xC1
    SHIFT_3_REPEAT = 0xC3


# The bus reports play state as one byte. The byte is Master Link's; the state
# is the product's, and lives in beo.product so the app side and the BeoLink
# side read the same member instead of each being told separately.
#
#   0x01 covers stop AND pause: the audio master does not distinguish on the bus.
PLAY_STATE_FROM_BYTE = {
    0x00: PlayState.UNKNOWN,
    0x01: PlayState.STOP,
    0x02: PlayState.PLAY,
    0x03: PlayState.WIND,
    0x04: PlayState.REWIND,
    0x06: PlayState.STANDBY,
    0x07: PlayState.NO_MEDIA,   # "Load / No Media": an empty tray
}


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def add_checksum(body: bytes) -> bytes:
    """body = everything up to and including the payload → complete telegram."""
    return bytes(body) + bytes([checksum(body), 0x00])


class TelegramError(ValueError):
    pass


@dataclass(frozen=True)
class Telegram:
    raw: bytes                     # complete: header + payload + checksum + 00

    # ---- construction --------------------------------------------------
    @classmethod
    def parse(cls, raw: bytes) -> "Telegram":
        raw = bytes(raw)
        if len(raw) < MIN_LEN:
            raise TelegramError("too short: %d bytes" % len(raw))
        if raw[-1] != 0x00:
            raise TelegramError("does not end in 00")
        if raw[-2] != checksum(raw[:-2]):
            raise TelegramError("bad checksum %02x, expected %02x" % (raw[-2], checksum(raw[:-2])))
        if len(raw) != MIN_LEN + raw[8]:
            raise TelegramError("length %d does not match payload length byte %d" % (len(raw), raw[8]))
        return cls(raw)

    @classmethod
    def is_complete(cls, raw: bytes) -> bool:
        try:
            cls.parse(raw)
            return True
        except TelegramError:
            return False

    @classmethod
    def build(cls, to: int, frm: int, typ: int, payload_type: int, payload: bytes = b"",
              *, dst_source: int = 0, src_source: int = 0, flag: int = 0x01) -> "Telegram":
        body = bytes([to, frm, 0x01, typ, dst_source, src_source, 0x00,
                      payload_type, len(payload), flag]) + bytes(payload)
        return cls(add_checksum(body))

    @classmethod
    def from_body_hex(cls, hexstr: str) -> "Telegram":
        """From the checksum-less hex form (header + payload only)."""
        return cls(add_checksum(bytes.fromhex(hexstr)))

    # ---- fields ----------------------------------------------------------
    @property
    def to(self) -> int: return self.raw[0]
    @property
    def frm(self) -> int: return self.raw[1]
    @property
    def typ(self) -> int: return self.raw[3]
    @property
    def dst_source(self) -> int: return self.raw[4]
    @property
    def src_source(self) -> int: return self.raw[5]
    @property
    def payload_type(self) -> int: return self.raw[7]
    @property
    def flag(self) -> int: return self.raw[9]
    @property
    def payload(self) -> bytes: return self.raw[HEADER_LEN:-2]
    @property
    def body(self) -> bytes: return self.raw[:-2]
    @property
    def hex(self) -> str: return self.raw.hex()
    @property
    def is_heartbeat(self) -> bool: return self.raw == HEARTBEAT

    def __str__(self) -> str:
        return "%s -> %s %s pl=%02x %s" % (
            ADDR_NAMES.get(self.frm, "%02x" % self.frm), ADDR_NAMES.get(self.to, "%02x" % self.to),
            TYPE_NAMES.get(self.typ, "%02x" % self.typ), self.payload_type, self.raw.hex(" "))


# What a 0x20 key press is aimed at: an audio product. 0x00 is a video
# product and 0x0F every product.
DEST_AUDIO = 0x01

# ---------------------------------------------------------------- builders
def beo4_press(key: int, *, source: int = SRC_CD, frm: int = LINK_NODE, to: int = AUDIO_MASTER,
               dest: int = DEST_AUDIO) -> Telegram:
    """A virtual Beo4 press, in the 0x20 form the MLGW uses.

    Layout:  c1 06 01 0a 00 00 00 20 05 02 | 00 <dest> 00 <source> <key> | CS 00
    As a source center (frm=0xC2) the wake press carries source=0x6f with the
    N.* keys; both forms wake the audio master. ``dest`` says what kind of
    product the key is meant for.
    """
    return Telegram.build(to, frm, CMD, PL_MLGW_BEO4,
                          bytes([0x00, dest & 0xFF, 0x00, source & 0xFF, int(key) & 0xFF]),
                          flag=0x02)


def link_room_source_request(source: int = SRC_CD) -> Telegram:
    """Ask the audio master, as a link room, to DISTRIBUTE a source on the bus.

    A Beo4 source press selects the source locally on the audio master; only a link-room
    request makes it put the audio onto the ML pairs. Without it the ADC sees
    silence and, because the HiFiBerry card is clocked by the MDT, arecord
    delivers no samples at all.

        c1 06 01 0b 00 00 00 45 07 01 | 02 <source> 00 02 01 00 00
    """
    return Telegram.build(AUDIO_MASTER, LINK_NODE, REQ, 0x45,
                          bytes([0x02, source & 0xFF, 0x00, 0x02, 0x01, 0x00, 0x00]), flag=0x01)


def standby_all_sources() -> Telegram:
    """STANDBY (0x10) for all sources, sent as the video master.

    The one form that stops the audio master from the bus. A link node's
    STANDBY, RELEASE or Beo4 STANDBY key leaves it playing, because a link
    room's standby is the link room's own.

        c1 c0 01 0a ff 00 00 10 00 03
    """
    return Telegram.build(AUDIO_MASTER, VIDEO_MASTER, CMD, PL_STANDBY, b"", dst_source=0xFF, flag=0x03)


def request(payload_type: int, *, frm: int = LINK_NODE, to: int = AUDIO_MASTER) -> Telegram:
    """An empty 0x0b request. The audio master answers 0x08/0x0b/0x44 and NAKs the rest.

    Never send 0x44 with an empty payload to the audio master: it switches the
    source to RADIO.
    """
    return Telegram.build(to, frm, REQ, payload_type, b"", flag=0x01)


# ---------------------------------------------------------------- decoders
@dataclass(frozen=True)
class StatusInfo:
    """0x87 STATUS_INFO as the audio master broadcasts it (33 bytes for CD)."""
    source: int
    disc: Optional[int]
    track: Optional[int]        # None = 0xff = "not known yet" (carry the old value)
    state: PlayState
    changed_disc: bool
    changed_track: bool
    changed_state: bool
    media_loaded: bool

    @property
    def is_echo(self) -> bool:
        """The audio master repeats every status ~0.1 s later with change mask 0x00."""
        return not (self.changed_disc or self.changed_track or self.changed_state)


def decode_status_info(t: Telegram) -> Optional[StatusInfo]:
    """CD layout:

        idx 10  source          idx 15  change mask: 0x02 disc, 0x08 track, 0x20 state
        idx 17  disc 1..6       idx 19  track, 0xff = unknown
        idx 21  play state      idx 11  0x09 = media
    """
    if t.payload_type != PL_STATUS_INFO or len(t.raw) < 23:
        return None
    r = t.raw
    mask = r[15]
    state = PLAY_STATE_FROM_BYTE.get(r[21], PlayState.UNKNOWN)
    return StatusInfo(source=r[10],
                      disc=r[17] if r[10] == SRC_CD else None,
                      track=None if r[19] == 0xFF else r[19],
                      state=state,
                      changed_disc=bool(mask & 0x02), changed_track=bool(mask & 0x08),
                      changed_state=bool(mask & 0x20), media_loaded=r[11] == 0x09)


def _bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


@dataclass(frozen=True)
class Clock:
    hour: int
    minute: int
    second: int
    day: int
    month: int
    year: int      # two digits


def decode_clock(t: Telegram) -> Optional[Clock]:
    """0x40 CLOCK, BCD. The audio master broadcasts it and accepts it from a source center."""
    if t.payload_type != PL_CLOCK or len(t.raw) < 21:
        return None
    p = t.raw[9:21]
    return Clock(_bcd(p[4]), _bcd(p[5]), _bcd(p[6]), _bcd(p[8]), _bcd(p[9]), _bcd(p[10]))


def encode_clock(hour: int, minute: int, second: int, day: int, month: int, year2: int,
                 *, frm: int = SOURCE_CENTER) -> Telegram:
    """The clock telegram a source center sends every 30 minutes (fields are BCD)."""
    def bcd(v: int) -> int:
        return ((v // 10) << 4) | (v % 10)
    body = bytes.fromhex("80") + bytes([frm]) + bytes.fromhex("011400000040" + "0b0b0a0003") + \
        bytes([bcd(hour), bcd(minute), bcd(second), 0x00, bcd(day), bcd(month), bcd(year2)])
    return Telegram(add_checksum(body))


def decode_display(t: Telegram) -> Optional[str]:
    """0x2c DISPLAY: the audio master sends its display text three ASCII characters at a time."""
    if t.typ != DISPLAY or len(t.raw) < 14:
        return None
    return "".join(chr(b) if 32 <= b < 127 else "." for b in t.raw[11:14])


def decode_beo4(t: Telegram) -> Optional[tuple]:
    """(key, source) for a 0x20 (MLGW form) or 0x0d (link node form) key telegram."""
    if t.payload_type not in (PL_MLGW_BEO4, PL_BEO4_KEY) or len(t.raw) < 12:
        return None
    return t.raw[-3], t.raw[-4]


def decode_nak(t: Telegram) -> Optional[bytes]:
    """0x50: the audio master echoes the rejected request. Returns the echoed bytes."""
    if t.payload_type != PL_NAK:
        return None
    return t.raw[9:-2]


def is_global_release(t: Telegram) -> bool:
    """The audio master's own "all off": `80 c1 01 0a 00 00 00 11 00 01`.

    It follows a Beo4 STANDBY sent from the MLGW address (0xF0), and
    distribution stops at the same moment. A STANDBY sent from a link node
    (0x06) only produces RELEASE telegrams to the source center and the master
    keeps playing — a link room's standby is the link room's, not the master's.
    """
    return t.to == TO_ALL and t.frm == AUDIO_MASTER and t.typ == CMD and t.payload_type == PL_RELEASE


def decode_distributed_source(t: Telegram) -> Optional[int]:
    """Answer to a 0x08 / 0x0b request:

        06 c1 01 14 00 00 00 08 00 04                    flag 04 = no distributed source
        06 c1 01 14 00 00 00 08 05 06 02 01 00 a1 01     flag 06 = has source; byte 13 = source

    Returns the source byte, 0 for "no source", None if this is not such an answer.
    """
    if t.payload_type != PL_DISTRIBUTED_SOURCE or t.typ != RESP:
        return None
    if t.flag == 0x04:
        return 0
    if t.flag == 0x06 and len(t.raw) >= 15:
        return t.raw[13]
    return None
