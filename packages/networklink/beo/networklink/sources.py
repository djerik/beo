"""Sources, catalogue, experience and every beocore XML body we emit.

Every field order lives here and only here. The builders are pure functions
(XML out), and the element order and spelling match what a product sends: a
listener compares strings, not meaning.

The module knows no product. ``STANDARD_SOURCES`` is what a networked
product lists, ``SOURCES`` is what one can render; which of them a product
lists, what it calls them and what it plays is decided where the Catalog is
built.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

from beo.product.identity import ProductIdentity
from beo.product.playback import Volume


def _x(v) -> str:
    """XML-escape. A bare & in a product name makes the stanza unreadable."""
    return escape("" if v is None else str(v))


def utc_stamp() -> str:
    """The timestamp for notifications and experiences — in UTC.

    A product's /BeoDevice/regionalSettings reports LOCAL time while its
    notifications and <lastUsed> carry UTC.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".000000"


# --------------------------------------------------------------------- data


@dataclass(frozen=True)
class IrMapping:
    """One <recommendedIrMapping> item: the remote key that selects the source
    (CD = 146, RADIO = 129)."""
    format: int
    unit: int
    command: int

    def xml(self) -> str:
        return ("<item><format>%d</format><unit>%d</unit>"
                "<command>%d</command></item>" % (self.format, self.unit, self.command))


def ir_xml(mapping: Iterable[IrMapping]) -> str:
    return "".join(m.xml() for m in mapping)


def _ir(*items: Tuple[int, int, int]) -> Tuple[IrMapping, ...]:
    return tuple(IrMapping(*i) for i in items)


@dataclass(frozen=True)
class SourceDescriptor:
    """One source as the catalogue and the status bodies describe it.

    ``key`` is the <source> of the uniqueSourceId and the first half of the
    source id ``<key>:<jid>`` on both the HTTP and beocore planes. The
    catalogue <identifier> is the Master Link slot the source sits in where
    there is one (CD, AUX_A, TP1, TP2, RADIO) and otherwise the key itself;
    a listener registers only identifiers from that namespace.
    """

    key: str
    name: str
    source_type: str
    category: str                                 # "MUSIC" | "RADIO"
    ir_mapping: Tuple[IrMapping, ...] = ()
    protection: str = "NONE"                      # NONE | PROPRIETARY | BLOCK
    linkable: bool = True
    identifier: Optional[str] = None              # catalogue <identifier>; the key when None
    in_use: bool = True
    embedded_binary: Tuple[str, ...] = ()

    def unique_id(self, jid: str) -> str:
        """``<key>:<jid>`` — the source id on both the HTTP and beocore planes."""
        return "%s:%s" % (self.key, jid)

    @property
    def beocore_identifier(self) -> str:
        return self.identifier or self.key

    @property
    def category_number(self) -> str:
        """category as a NUMBER, as beocore uses it: 2 = radio, 3 = music."""
        return "2" if self.category == "RADIO" else "3"

    @property
    def ir(self) -> str:
        return ir_xml(self.ir_mapping)

    def named(self, name: str) -> "SourceDescriptor":
        """The same source under the name a product gives it."""
        return replace(self, name=name)


# The catalogue a networked product lists in Attach and in its reply to
# GetContactInfo, in the order it lists them. The old Master Link slots carry
# the modern services: CD is Spotify, AUX_A is Line-In, TP1 is Music, TP2 is
# QPlay; B&O Radio is RADIO.
STANDARD_SOURCES: Tuple[SourceDescriptor, ...] = (
    SourceDescriptor("airplay", "AirPlay", "AIRPLAY", "MUSIC", _ir((11, 2, 148)),
                     protection="BLOCK", linkable=False),
    SourceDescriptor("deezer", "Deezer", "DEEZER", "MUSIC", protection="PROPRIETARY"),
    SourceDescriptor("tidal", "Tidal", "TIDAL", "MUSIC", protection="PROPRIETARY"),
    SourceDescriptor("spotify", "Spotify", "SPOTIFY", "MUSIC", _ir((0, 0, 146), (11, 0, 150)),
                     protection="PROPRIETARY", linkable=False, identifier="CD",
                     embedded_binary=("SPOTIFY_EMBEDDED_SDK",)),
    SourceDescriptor("radio", "TuneIn", "TUNEIN", "RADIO", linkable=False, in_use=False),
    SourceDescriptor("googlecast", "Chromecast built-in", "GC4A", "MUSIC",
                     protection="BLOCK", linkable=False),
    SourceDescriptor("linein", "Line-In", "LINE IN", "MUSIC", _ir((0, 0, 131), (11, 0, 131)),
                     protection="PROPRIETARY", identifier="AUX_A"),
    SourceDescriptor("dlna", "Music stream", "DLNA_DMR", "MUSIC", _ir((11, 1, 132), (11, 1, 148)),
                     protection="PROPRIETARY", linkable=False),
    SourceDescriptor("alarm", "Alarm", "UNDEFINED", "MUSIC", protection="PROPRIETARY", linkable=False),
    SourceDescriptor("beoradio", "B&O Radio", "BEO RADIO", "RADIO",
                     _ir((0, 0, 129), (11, 0, 129), (11, 1, 129), (0, 0, 147), (11, 0, 147)),
                     identifier="RADIO"),
    SourceDescriptor("qplay", "QPlay", "QPLAY", "MUSIC", _ir((0, 0, 148), (11, 2, 150)),
                     protection="PROPRIETARY", identifier="TP2"),
    SourceDescriptor("bluetooth", "Bluetooth", "BLUETOOTH", "MUSIC", _ir((11, 2, 131)),
                     protection="PROPRIETARY"),
    SourceDescriptor("music", "Music", "MUSIC", "MUSIC",
                     _ir((0, 0, 145), (11, 0, 145), (11, 0, 148), (0, 0, 150), (11, 1, 150)),
                     protection="PROPRIETARY", identifier="TP1"),
)
STANDARD: Dict[str, SourceDescriptor] = {s.key: s for s in STANDARD_SOURCES}


# The sources a product can render over the link, keyed by Master Link slot
# (CD, not cd1..cd6): the slot is what a listener's catalogue knows. The CD
# slot is a real CD here, not the Spotify that fills it in the standard
# catalogue, and it carries no name of its own: the product names it
# (``SOURCES["CD"].named(...)``). RADIO likewise is a tuner, not the B&O
# Radio that fills the slot there.
SOURCES: Dict[str, SourceDescriptor] = {
    "CD": SourceDescriptor("CD", "CD", "CD", "MUSIC"),
    "RADIO": SourceDescriptor("RADIO", "RADIO", "RADIO", "RADIO"),
    "linein": STANDARD["linein"],
    "beoradio": STANDARD["beoradio"],
}

KNOWN_SOURCE_KEYS = (frozenset(SOURCES)
                     | frozenset(s.key for s in STANDARD_SOURCES)
                     | frozenset(s.beocore_identifier for s in STANDARD_SOURCES))


@dataclass(frozen=True)
class NowPlaying:
    """Content of NOW_PLAYING_NET_RADIO — the same in push and in FullStatus.

    Empty until the product says what plays; the shape is what a listener
    knows, the content is the product's.
    """
    name: str = ""
    genre: str = ""
    country: str = ""
    languages: str = ""
    image_url: str = ""
    image_size: str = ""
    image_mediatype: str = ""
    live_description: str = ""
    station_id: str = ""


@dataclass
class Experience:
    """A source that is playing: source + lastUsed + state + listeners.

    ``started_at`` (``<lastUsed>``) is set ONCE at the first push and reused
    at stop. It is a field, not a clock lookup.
    """
    source: SourceDescriptor
    source_jid: str
    state: str = "preparing"                     # preparing | play | pause | stop
    started_at: Optional[str] = None
    listeners: List[str] = field(default_factory=list)
    now_playing: Optional[NowPlaying] = None

    def touch_started_at(self) -> str:
        if self.started_at is None:
            self.started_at = utc_stamp()
        return self.started_at

    def unique_id(self) -> str:
        return self.source.unique_id(self.source_jid)


# ------------------------------------------------------------------ catalog


class Catalog:
    """The sources a product lists; the answer to GetContactInfo and Experiences.

    A product lists its whole catalogue in Attach, rendered sources or not;
    ``roles`` is what it offers to be in an integrated setup.
    """

    def __init__(self, sources: Iterable[SourceDescriptor], *,
                 roles: Tuple[str, ...] = ("integratedMaster",), strict: bool = True):
        self.sources = list(sources)
        self.roles = tuple(roles)
        if strict:
            for s in self.sources:
                if s.key not in KNOWN_SOURCE_KEYS:
                    raise ValueError("unknown source key %r; sources are keyed by ML "
                                     "source slot (%s)" % (s.key, ", ".join(sorted(KNOWN_SOURCE_KEYS))))

    def get(self, key: str) -> SourceDescriptor:
        for s in self.sources:
            if s.key == key:
                return s
        raise KeyError(key)

    @staticmethod
    def catalog_item(src: SourceDescriptor, ident: ProductIdentity) -> str:
        """One <item> of <sources>.

        Field order: identifier, name, sourceType, category, inUse, profile,
        sourceKey, linkable, productFriendlyName, contentProtection,
        embeddedBinary, uniqueSourceId, recommendedIrMapping.
        """
        schemes = "".join(f"<item>{s}</item>" for s in src.embedded_binary)
        return ("<item>"
                f"<identifier>{src.beocore_identifier}</identifier>"
                f"<name>{_x(src.name)}</name>"
                f"<sourceType><type>{src.source_type}</type></sourceType>"
                f"<category>{src.category_number}</category>"
                f"<inUse>{1 if src.in_use else 0}</inUse>"
                "<profile></profile><sourceKey></sourceKey>"
                f"<linkable>{1 if src.linkable else 0}</linkable>"
                f"<productFriendlyName>{_x(ident.name)}</productFriendlyName>"
                f"<contentProtection><schemeList><item>{src.protection}"
                "</item></schemeList></contentProtection>"
                f"<embeddedBinary><schemeList>{schemes}</schemeList></embeddedBinary>"
                # <identifier> above is the slot name (RADIO); here the key
                # goes (beoradio).
                f"<uniqueSourceId>"
                f"<source>{src.key}</source>"
                f"<jid>{ident.full_jid()}</jid></uniqueSourceId>"
                f"<recommendedIrMapping>{src.ir}</recommendedIrMapping>"
                "</item>")

    def sources_xml(self, ident: ProductIdentity) -> str:
        return "".join(self.catalog_item(s, ident) for s in self.sources)

    def contact_info_xml(self, ident: ProductIdentity) -> str:
        """Our catalogue, shaped like a listener's own reply to GetContactInfo.

        The sources, then roleCapability. The MAC is upper case.
        """
        mac = ident.mac.upper()
        roles = "".join(f"<item>{r}</item>" for r in self.roles)
        return ("<contactInfo>"
                f"<name>{_x(ident.name)}</name>"
                f"<macs><item>{mac}</item></macs>"
                f"<sources>{self.sources_xml(ident)}</sources>"
                f"<roleCapability><roles>{roles}</roles>"
                "</roleCapability></contactInfo>")

    @staticmethod
    def _source_body(ident: ProductIdentity, exp: Experience) -> str:
        """The playing source as GetContactInfo and Experiences describe it.

        Whatever the source, this body says type CD, category 3 and the CD
        key: the one form a listener is known to join. The source's own type
        and keys are untried on the wire here, so they are not used.
        """
        src = exp.source
        return ("<identifier>" + src.key + "</identifier>"
                "<name>" + _x(src.name) + "</name>"
                "<sourceType><type>CD</type></sourceType>"
                "<category>3</category><inUse>1</inUse>"
                "<profile></profile><sourceKey></sourceKey>"
                "<linkable>1</linkable>"
                "<productFriendlyName>" + _x(ident.name) + "</productFriendlyName>"
                f"<contentProtection><schemeList><item>{src.protection}"
                "</item></schemeList></contentProtection>"
                "<embeddedBinary><schemeList></schemeList></embeddedBinary>"
                "<uniqueSourceId><source>" + src.key + "</source>"
                "<jid>" + exp.source_jid + "</jid></uniqueSourceId>"
                "<recommendedIrMapping>"
                "<item><format>0</format><unit>0</unit>"
                "<command>146</command></item>"
                "<item><format>11</format><unit>0</unit>"
                "<command>146</command></item>"
                "</recommendedIrMapping>")

    @classmethod
    def get_contact_info_reply_xml(cls, ident: ProductIdentity, exp: Experience) -> str:
        """The source session's reply to a peer's GetContactInfo (one source).

        GetContactInfo is the ONLY way a product fetches another product's
        source catalogue. The MAC here has no colons while the one in Attach
        has; a listener takes both, and each is kept as it has seen it.
        """
        mac = ident.mac.replace(":", "").replace("-", "").upper()
        return ("<GetContactInfo xmlns='beocore.product.1'>"
                "<contactInfo>"
                "<name>" + _x(ident.name) + "</name>"
                "<macs><item>" + mac + "</item></macs>"
                "<sources><item>" + cls._source_body(ident, exp) + "</item>"
                "</sources>"
                "</contactInfo></GetContactInfo>")

    @classmethod
    def experiences_xml(cls, ident: ProductIdentity, exp: Optional[Experience]) -> str:
        """Reply to <Experiences> (OneWayJoin: the speaker asks every known
        device what it has to play and joins what it gets an answer for).

        The form a speaker uses in its own Experiences reply, with the
        experience's own <lastUsed>. With no experience, the idle form: an
        empty list.
        """
        if exp is None:
            return ("<Experiences xmlns='beocore.player.1'><experienceInfo>"
                    "<list></list></experienceInfo></Experiences>")
        return ("<Experiences xmlns='beocore.player.1'><experienceInfo>"
                "<list><item>"
                f"<source>{cls._source_body(ident, exp)}</source>"
                f"<lastUsed>{exp.touch_started_at()}</lastUsed>"
                "<state>play</state>"
                f"<listener><item>{ident.full_jid()}</item></listener>"
                "</item></list></experienceInfo></Experiences>")


# ------------------------------------------------------------ status bodies


class StatusBuilder:
    """Pure functions, XML out; field order as a product's own."""

    @staticmethod
    def source_xml(ident: ProductIdentity, exp: Experience) -> str:
        """Our source, shaped exactly as the receiver describes its own.
        Field order as in a product's own FullStatus — a parser reading
        sequentially would otherwise trip."""
        src = exp.source
        sid = f"{src.key}:{exp.source_jid}"
        return (f"<source><id>{sid}</id>"
                f"<friendlyName>{_x(src.name)}</friendlyName>"
                f"<sourceType><type>{src.source_type}</type></sourceType>"
                f"<category>{src.category}</category>"
                "<inUse>1</inUse><profile></profile>"
                "<linkable>1</linkable>"
                f"<recommendedIrMapping>{src.ir}</recommendedIrMapping>"
                f"<contentProtection><schemeList><item>{src.protection}"
                "</item></schemeList></contentProtection>"
                "<embeddedBinary><schemeList></schemeList></embeddedBinary>"
                f"<product><jid>{ident.full_jid()}</jid>"
                f"<friendlyName>{_x(ident.name)}</friendlyName></product></source>")

    @staticmethod
    def now_playing_data(exp: Experience) -> str:
        """Content of NOW_PLAYING_NET_RADIO — the same in push and in FullStatus."""
        r = exp.now_playing or NowPlaying()
        return (f"<name>{_x(r.name)}</name>"
                f"<genre>{_x(r.genre)}</genre>"
                f"<country>{_x(r.country)}</country>"
                f"<languages>{_x(r.languages)}</languages>"
                f"<image><item><url>{_x(r.image_url)}</url>"
                f"<size>{r.image_size}</size>"
                f"<mediatype>{r.image_mediatype}</mediatype></item></image>"
                f"<liveDescription>{_x(r.live_description)}</liveDescription>"
                f"<stationId>{r.station_id}</stationId>"
                f"<playQueueItemId>{exp.source.key}</playQueueItemId>")

    @staticmethod
    def full_status(ident: ProductIdentity, exp: Experience, volume: Volume) -> str:
        """The reply to the listener's subscription — the SOURCE's FullStatus.

        Five items in this order:

            SOURCE                 the whole experience + 7 capabilities
            NOW_PLAYING_NET_RADIO  what is playing
            PROGRESS_INFORMATION   state, playQueueItemId
            VOLUME                 speaker: level, muted, range
            SOFTWARE_UPDATE_STATE  idle

        The capability list belongs HERE — STREAMING_STATUS and
        PLAY_QUEUE_CHANGED included — not in the SOURCE push, which a source
        sends with empty data.
        """
        our_jid = ident.full_jid()
        sid = f"{exp.source.key}:{exp.source_jid}"
        last_used = exp.touch_started_at()
        # In FullStatus the source lists ONLY itself as listener. In
        # SOURCE_EXPERIENCE_CHANGED it lists both. Two different snapshots of
        # the same experience.
        listeners = [our_jid]
        level, mute = volume.level, 1 if volume.muted else 0
        return (
            "<FullStatus><status>"
            "<item><type>SOURCE</type><kind>source</kind><data>"
            f"<primary>{sid}</primary><primaryJid>{our_jid}</primaryJid>"
            "<primaryExperience>"
            f"{StatusBuilder.source_xml(ident, exp)}"
            f"<lastUsed>{last_used}</lastUsed>"
            "<state>play</state>"
            "<listener>" + "".join(f"<item>{j}</item>" for j in listeners) + "</listener>"
            "<_capabilities><supportedNotifications>"
            "<item><type>SOURCE</type><kind>source</kind></item>"
            "<item><type>SOURCE_EXPERIENCE_CHANGED</type><kind>source</kind></item>"
            "<item><type>PLAY_QUEUE_CHANGED</type><kind>playing</kind></item>"
            "<item><type>NOW_PLAYING_ENDED</type><kind>playing</kind></item>"
            "<item><type>STREAMING_STATUS</type><kind>streaming</kind></item>"
            "<item><type>PROGRESS_INFORMATION</type><kind>playing</kind></item>"
            "<item><type>NOW_PLAYING_NET_RADIO</type><kind>playing</kind></item>"
            "</supportedNotifications></_capabilities>"
            "</primaryExperience></data></item>"
            "<item><type>NOW_PLAYING_NET_RADIO</type><kind>playing</kind><data>"
            f"{StatusBuilder.now_playing_data(exp)}</data></item>"
            # PROGRESS in FullStatus is "preparing" WITHOUT position, even
            # when the source has played for seconds before the listener
            # joined. The source restarts its stream when a listener joins;
            # that is the snapshot the listener gets.
            "<item><type>PROGRESS_INFORMATION</type><kind>playing</kind><data>"
            "<state>preparing</state>"
            f"<playQueueItemId>{exp.source.key}</playQueueItemId></data></item>"
            "<item><type>VOLUME</type><kind>renderer</kind><data>"
            f"<speaker><level>{level}</level><muted>{mute}</muted>"
            f"<range><minimum>{volume.minimum}</minimum><maximum>{volume.maximum}</maximum></range>"
            "</speaker></data></item>"
            "<item><type>SOFTWARE_UPDATE_STATE</type><kind>device</kind>"
            "<data><state>idle</state></data></item>"
            "</status></FullStatus>")

    @staticmethod
    def experience_changed(ident: ProductIdentity, exp: Experience, state: str,
                           listeners: List[str]) -> str:
        """SOURCE_EXPERIENCE_CHANGED — the FIRST thing a real source pushes to
        its listener, and what it pushes again at stop.

          opening: the whole source incl. <product>, <lastUsed>,
                   <state>play</state>, listener = [the listener, the source]
          stop:    same source, SAME lastUsed as at the opening,
                   <state>stop</state>, listener = [the listener]
        lastUsed is when the experience began, not "now". No capabilities, no <primary>.
        Listener FIRST, the source itself LAST.
        """
        last_used = exp.touch_started_at()
        return ("<NOTIFICATION><type>SOURCE_EXPERIENCE_CHANGED</type>"
                "<kind>source</kind><data><primaryExperience>"
                f"{StatusBuilder.source_xml(ident, exp)}"
                f"<lastUsed>{last_used}</lastUsed>"
                f"<state>{state}</state>"
                "<listener>"
                + "".join(f"<item>{j}</item>" for j in listeners) +
                "</listener>"
                "</primaryExperience></data></NOTIFICATION>")

    @staticmethod
    def source_empty() -> str:
        """SOURCE — with EMPTY <data>, as a source sends it. The content
        lives in SOURCE_EXPERIENCE_CHANGED. A source sends it only at stop."""
        return ("<NOTIFICATION><type>SOURCE</type><kind>source</kind>"
                "<data></data></NOTIFICATION>")

    @staticmethod
    def now_playing(exp: Experience) -> str:
        """NOW_PLAYING_NET_RADIO — the notification a radio source sends."""
        return ("<NOTIFICATION><type>NOW_PLAYING_NET_RADIO</type>"
                "<kind>playing</kind><data>"
                f"{StatusBuilder.now_playing_data(exp)}"
                "</data></NOTIFICATION>")

    @staticmethod
    def ended() -> str:
        """NOW_PLAYING_ENDED with empty data — as a source sends it at stop."""
        return ("<NOTIFICATION><type>NOW_PLAYING_ENDED</type>"
                "<kind>playing</kind><data></data></NOTIFICATION>")

    @staticmethod
    def progress_stop() -> str:
        """PROGRESS_INFORMATION at stop: ONLY <state>stop</state>.

        No position, no playQueueItemId. A stop with a position looks like a
        playback standing still; this one looks like one that has ended."""
        return ("<NOTIFICATION><type>PROGRESS_INFORMATION</type>"
                "<kind>playing</kind><data><state>stop</state></data>"
                "</NOTIFICATION>")

    @staticmethod
    def progress(state: str, position: int, item_id: str) -> str:
        """PROGRESS_INFORMATION. <position> is PLAYBACK TIME — it starts low
        and counts one second at a time (7, 8, 9, 10 ...). Not uptime."""
        return ("<NOTIFICATION><type>PROGRESS_INFORMATION</type>"
                "<kind>playing</kind><data>"
                f"<state>{state}</state><position>{position}</position>"
                f"<playQueueItemId>{item_id}</playQueueItemId>"
                "</data></NOTIFICATION>")

    @staticmethod
    def connect_reply(connection_id: str) -> str:
        return ("<Connect xmlns='beocore.player.1'>"
                f"<connectionId>{connection_id}"
                "</connectionId>"
                "</Connect>")

    @staticmethod
    def renderer_status(volume: Volume) -> str:
        """A listener's reply to the source's subscription to kind=renderer.

        The VOLUME item, in the shape a FullStatus carries it. A source reads
        the listener's level from here and from the three volume questions.
        """
        return ("<FullStatus><status>"
                "<item><type>VOLUME</type><kind>renderer</kind><data>"
                f"<speaker><level>{volume.level}</level><muted>{1 if volume.muted else 0}</muted>"
                f"<range><minimum>{volume.minimum}</minimum><maximum>{volume.maximum}</maximum></range>"
                "</speaker></data></item>"
                "</status></FullStatus>")

    @staticmethod
    def connect_request_reply() -> str:
        return ("<ConnectRequest xmlns='beocore.player.1'>"
                "<connected>1</connected></ConnectRequest>")

    @staticmethod
    def attach_reply(contact_info_xml: str) -> str:
        """The answer to Attach is the answerer's own contact info."""
        return f"<Attach xmlns='beocore.product.1'>{contact_info_xml}</Attach>"

    @staticmethod
    def subscribe_reply(full_status_xml: str) -> str:
        return ("<_notification-subscribe xmlns='beocore.notifications.1'>"
                f"{full_status_xml}</_notification-subscribe>")

    @staticmethod
    def unsubscribe_reply() -> str:
        return ("<_notification-unsubscribe "
                "xmlns='beocore.notifications.1'/>")

    @staticmethod
    def disconnect_reply() -> str:
        return "<Disconnect xmlns='beocore.player.1'/>"

    @staticmethod
    def notification_ack() -> str:
        return "<_notification xmlns='beocore.notifications.1'/>"

    @staticmethod
    def get_volume_reply(level: int) -> str:
        return ("<GetVolume xmlns='beocore.product.1'>"
                f"<volume>{level}</volume></GetVolume>")

    @staticmethod
    def get_volume_muted_reply(muted: bool) -> str:
        return ("<GetVolumeMuted xmlns='beocore.product.1'>"
                f"<muted>{1 if muted else 0}</muted></GetVolumeMuted>")

    @staticmethod
    def get_volume_range_reply(minimum: int, maximum: int) -> str:
        return ("<GetVolumeRange xmlns='beocore.product.1'><range>"
                f"<minimum>{minimum}</minimum><maximum>{maximum}</maximum>"
                "</range></GetVolumeRange>")


# What a listener says it can decode, as escaped XML inside the Connect. The
# line break after the declaration is part of it: the content is text, so a
# parser does not smooth over a missing one.
CP_CAPABILITIES = (
    "&lt;?xml version=\"1.0\" encoding=\"utf-8\"?&gt;\n"
    "&lt;CpCapabilities&gt;&lt;ProtectionSchemes&gt;"
    "&lt;Scheme&gt;NONE&lt;/Scheme&gt;"
    "&lt;Scheme&gt;DECIMATE&lt;/Scheme&gt;"
    "&lt;Scheme&gt;PROPRIETARY&lt;/Scheme&gt;"
    "&lt;/ProtectionSchemes&gt;&lt;/CpCapabilities&gt;")


class Requests:
    """iq bodies one side sends the other (all type='set')."""

    @staticmethod
    def connect(source_key: str, source_jid: str, fps_host: str) -> str:
        """The listener tells the source where to send the audio."""
        return ("<Connect xmlns='beocore.player.1'>"
                f"<sourceId>{source_key}</sourceId>"
                f"<connectionInfo>FPS://{fps_host}</connectionInfo>"
                f"<contentProtectionInfo>{CP_CAPABILITIES}</contentProtectionInfo>"
                f"<uniqueSourceId><source>{source_key}</source>"
                f"<jid>{source_jid}</jid></uniqueSourceId>"
                "</Connect>")

    @staticmethod
    def experiences() -> str:
        """A product asks another what it is playing, before it joins it."""
        return "<Experiences xmlns='beocore.player.1'></Experiences>"

    @staticmethod
    def subscribe_source() -> str:
        """The listener's subscription to what the source is playing."""
        return ("<_notification-subscribe "
                "xmlns='beocore.notifications.1'>"
                "<_notification-filter><Filter><Filter>"
                "<includeKindFilter><item>device</item><item>playing</item>"
                "<item>source</item></includeKindFilter>"
                "<excludeTypeFilter></excludeTypeFilter>"
                "</Filter></Filter></_notification-filter>"
                "</_notification-subscribe>")

    @staticmethod
    def attach(contact_info_xml: str) -> str:
        """Present ourselves, as a product does. <role> is mandatory: without
        it the peer answers "bad-request: Cannot read XML tag: role". 0 goes
        through, but records us as the peer's integrated-setup slave; what a
        product itself sends here is not known."""
        return ("<Attach xmlns='beocore.product.1'>"
                f"<role>0</role>{contact_info_xml}"
                "</Attach>")

    @staticmethod
    def connect_request(source_key: str, source_jid: str) -> str:
        return ("<ConnectRequest xmlns='beocore.player.1'>"
                f"<uniqueSourceId><source>{source_key}"
                f"</source><jid>{source_jid}</jid>"
                "</uniqueSourceId></ConnectRequest>")

    @staticmethod
    def disconnect(connection_id) -> str:
        return ("<Disconnect xmlns='beocore.player.1'>"
                f"<connectionId>{connection_id}"
                "</connectionId></Disconnect>")

    @staticmethod
    def unsubscribe() -> str:
        return ("<_notification-unsubscribe "
                "xmlns='beocore.notifications.1'>"
                "</_notification-unsubscribe>")

    @staticmethod
    def subscribe_renderer() -> str:
        return ("<_notification-subscribe "
                "xmlns='beocore.notifications.1'>"
                "<_notification-filter><Filter><Filter>"
                "<includeKindFilter><item>renderer</item>"
                "</includeKindFilter>"
                "<excludeTypeFilter></excludeTypeFilter>"
                "</Filter></Filter></_notification-filter>"
                "</_notification-subscribe>")

    @staticmethod
    def get_volume() -> str:
        return "<GetVolume xmlns='beocore.product.1'></GetVolume>"

    @staticmethod
    def get_volume_muted() -> str:
        return "<GetVolumeMuted xmlns='beocore.product.1'></GetVolumeMuted>"

    @staticmethod
    def get_volume_range() -> str:
        return "<GetVolumeRange xmlns='beocore.product.1'></GetVolumeRange>"
