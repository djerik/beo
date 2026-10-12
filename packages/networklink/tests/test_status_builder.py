"""Golden tests: the XML the library puts on the wire equals tests/golden/*.xml,
pinned under the identities in golden_identities.py. A difference is a wire change and
must be meant; golden_identities.py re-pins the files.

Covers invariants 4, 6, 7, 11 (stream header), 14, 15.
"""
import os

import pytest

import golden_identities as pinned
from beo.networklink.beocore import DISCO, DISCO_FEATURES, Session, _x
from beo.product.identity import ProductIdentity
from beo.networklink.sources import (Catalog, Experience, NowPlaying, SOURCES, STANDARD,
                                     STANDARD_SOURCES, SourceDescriptor, StatusBuilder, Volume)

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
IDENT = ProductIdentity.from_jid(pinned.OUR_JID, pinned.OUR_NAME, pinned.OUR_MAC)


def golden(name, key):
    with open(os.path.join(GOLDEN, "%s-%s.xml" % (name, key)), encoding="utf-8") as f:
        return f.read()


def experience(key):
    exp = Experience(SOURCES[key], IDENT.full_jid())
    exp.started_at = pinned.LAST_USED
    exp.now_playing = NowPlaying(**pinned.STATION)
    return exp


def library(key):
    """Every wire form for one source, by the golden file's name."""
    exp = experience(key)
    cat = Catalog(STANDARD_SOURCES)
    vol = Volume(*pinned.VOLUME[:1], bool(pinned.VOLUME[1]))
    out = {}
    out["contact_info"] = cat.contact_info_xml(IDENT)
    out["source_xml"] = StatusBuilder.source_xml(IDENT, exp)
    out["full_status"] = StatusBuilder.full_status(IDENT, exp, vol)
    out["now_playing_data"] = StatusBuilder.now_playing_data(exp)
    out["source_body"] = Catalog._source_body(IDENT, exp)
    sec = StatusBuilder.experience_changed(IDENT, exp, "play", [pinned.PEER_JID, IDENT.full_jid()])
    np = StatusBuilder.now_playing(exp)
    opening = [sec]
    if key == "beoradio":
        opening.append(np)
    opening += [StatusBuilder.progress("preparing", 0, key), StatusBuilder.progress("play", 0, key)]
    if key == "beoradio":
        opening.append(np)
    out["opening"] = "\n".join(opening)
    out["progress_pause_7"] = StatusBuilder.progress("pause", 7, key)
    out["teardown"] = "\n".join([
        StatusBuilder.experience_changed(IDENT, exp, "stop", [pinned.PEER_JID]),
        StatusBuilder.ended(), StatusBuilder.progress_stop(), StatusBuilder.source_empty()])
    out["get_contact_info_reply"] = Catalog.get_contact_info_reply_xml(IDENT, exp)
    out["experiences_reply"] = Catalog.experiences_xml(IDENT, exp)
    feats = "".join(f"<feature var='{f}'/>" for f in DISCO_FEATURES)
    out["disco_reply"] = (f"<query xmlns='{DISCO}'><identity category='client' "
                          f"name='{_x(IDENT.name)}' type='product'/>{feats}</query>")
    return out


NAMES = ["contact_info", "source_xml", "full_status", "now_playing_data", "source_body",
         "opening", "progress_pause_7", "teardown", "get_contact_info_reply",
         "experiences_reply", "disco_reply"]


@pytest.mark.parametrize("key", ["beoradio", "CD"])
@pytest.mark.parametrize("name", NAMES)
def test_against_golden(name, key):
    assert library(key)[name] == golden(name, key)


def test_full_status_preparing_without_position_and_only_us_as_listener():
    fs = StatusBuilder.full_status(IDENT, experience("beoradio"), Volume())
    assert "<state>preparing</state><playQueueItemId>beoradio</playQueueItemId>" in fs
    i = fs.index("PROGRESS_INFORMATION")
    assert "<position>" not in fs[i:i + 200]
    assert "<listener><item>%s</item></listener>" % IDENT.full_jid() in fs


def test_sec_listener_order_and_same_last_used_at_stop():
    exp = Experience(SOURCES["CD"], IDENT.full_jid())
    opening = StatusBuilder.experience_changed(IDENT, exp, "play", [pinned.PEER_JID, IDENT.full_jid()])
    assert exp.started_at is not None
    stop = StatusBuilder.experience_changed(IDENT, exp, "stop", [pinned.PEER_JID])
    lu = "<lastUsed>%s</lastUsed>" % exp.started_at
    assert lu in opening and lu in stop
    assert ("<listener><item>%s</item><item>%s</item></listener>"
            % (pinned.PEER_JID, IDENT.full_jid())) in opening
    assert "<listener><item>%s</item></listener>" % pinned.PEER_JID in stop


def test_experiences_reply_carries_the_experience_last_used():
    exp = experience("CD")
    assert "<lastUsed>%s</lastUsed>" % pinned.LAST_USED in Catalog.experiences_xml(IDENT, exp)
    assert Catalog.experiences_xml(IDENT, None).endswith("<list></list></experienceInfo></Experiences>")


def test_catalog_rejects_unknown_keys():
    with pytest.raises(ValueError):
        Catalog([SourceDescriptor("cd1", "CD 1", "CD", "MUSIC")])
    Catalog([SourceDescriptor("cd1", "CD 1", "CD", "MUSIC")], strict=False)


def test_contact_info_matches_the_capture_form():
    """Invariant 15: the same fields in the same order as a product replies.

    The sample has had every identity replaced, because the shape is the
    finding and the hardware it came from is not.
    """
    fx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                      "contact-info-reply.xml")
    with open(fx, encoding="utf-8") as f:
        real = f.read()
    real_ci = real[real.index("<contactInfo>"):real.index("</contactInfo>") + len("</contactInfo>")]
    bry = ProductIdentity.from_jid("2730.1200322.90000010", "Listener", "00:00:5E:00:53:02")
    assert Catalog(STANDARD_SOURCES).contact_info_xml(bry) == real_ci


def test_slot_sources_are_the_standard_ones_and_cd_takes_the_products_name():
    assert SOURCES["linein"] is STANDARD["linein"]
    assert SOURCES["beoradio"] is STANDARD["beoradio"]
    assert SOURCES["beoradio"].beocore_identifier == "RADIO"
    cd = SOURCES["CD"].named("Living room")
    assert (cd.key, cd.name, cd.beocore_identifier) == ("CD", "Living room", "CD")
    assert "<friendlyName>Living room</friendlyName>" in StatusBuilder.source_xml(
        IDENT, Experience(cd, IDENT.full_jid()))


def test_now_playing_is_empty_until_the_product_speaks():
    exp = Experience(SOURCES["CD"], IDENT.full_jid())
    data = StatusBuilder.now_playing_data(exp)
    assert data.startswith("<name></name><genre></genre>")
    assert "<stationId></stationId><playQueueItemId>CD</playQueueItemId>" in data


def test_stream_header_has_no_xml_declaration_and_double_quotes():
    """Invariant 11, and the double-quote fix on the wire."""
    class Sock:
        def __init__(self):
            self.data = b""

        def sendall(self, b):
            self.data += b
    s = Session(IDENT, "127.0.0.1", peer_jid=pinned.PEER_JID)
    s.sock = Sock()
    s._send_stream_header()
    head = s.sock.data.decode()
    assert not head.startswith("<?xml")
    assert head == ('<stream:stream to="products.bang-olufsen.com" from="%s" '
                    'xmlns="jabber:client" xmlns:stream="http://etherx.jabber.org/streams" '
                    'version="1.0">' % IDENT.full_jid())
    s.sock.data = b""
    s.push("<NOTIFICATION><type>SOURCE</type><kind>source</kind><data></data></NOTIFICATION>")
    assert s.sock.data.decode() == (
        '<message to="%s" from="%s"><body><_notification xmlns="beocore.notifications.1">'
        '<NOTIFICATION><type>SOURCE</type><kind>source</kind><data></data></NOTIFICATION>'
        '</_notification></body></message>' % (pinned.PEER_JID, IDENT.full_jid()))
