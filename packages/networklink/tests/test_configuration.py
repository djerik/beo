"""A product's entry in the shared configuration, and the request that enrols it."""
import golden_identities as pinned
from beo.networklink import configuration
from beo.networklink.sources import SOURCES, STANDARD
from beo.product.identity import ProductIdentity

IDENT = ProductIdentity.from_jid(pinned.OUR_JID, "Reference & Source", "00:00:5e:00:53:01")
KEY = "2730%2E1200322%2E90000001@products%2Ebang-olufsen%2Ecom"


def entry(**kw):
    return configuration.device_entry(IDENT, [SOURCES["CD"]], **kw)


def test_the_entry_is_named_by_the_jid_with_its_dots_written_out():
    e = entry()
    assert e.startswith('<item name="%s"><item name="Name#1"><data>Reference &amp; Source</data></item>'
                        '<item name="Macs#1"><item name="Mac#1"><data>00:00:5E:00:53:01</data></item></item>'
                        '<item name="Sources#1"><item name="CD">' % KEY)
    assert e.count("<item") == e.count("</item>")


def test_the_one_source_is_a_cd_under_the_slot_a_listener_knows():
    e = entry()
    assert e.count('<item name="UniqueSourceId#1">') == 1
    assert ('<item name="SourceType#1"><item name="Type#1"><data>CD</data></item></item>'
            '<item name="Category#1"><data>MUSIC</data></item><item name="InUse#1"><data>true</data></item>'
            '<item name="Profile#1"><item name="^emptyValue"><data>true</data></item></item>'
            '<item name="Linkable#1"><data>true</data></item>') in e
    assert ('<item name="UniqueSourceId#1"><item name="Source#1"><data>CD</data></item>'
            '<item name="Jid#1"><data>%s</data></item></item>' % KEY) in e
    assert '<item name="Index#1"><data>0</data></item>' in e


def test_a_list_repeats_its_name_and_numbers_from_the_second():
    e = entry()
    assert ('<item name="Scheme#1"><data>NONE</data></item><item name="Scheme#1_1"><data>DECIMATE</data></item>'
            '<item name="Scheme#1_2"><data>PROPRIETARY</data></item>') in e
    assert ('<item name="SupportedExperience#1"><data>join</data></item>'
            '<item name="SupportedExperience#1_1"><data>expand</data></item>') in e
    src = configuration.source_entry(STANDARD["spotify"], IDENT, 3)
    assert src.startswith('<item name="CD"><item name="Name#1"><data>Spotify</data>')
    assert ('<item name="IrCode#1"><item name="Format#1"><data>0</data></item><item name="Unit#1"><data>0</data></item>'
            '<item name="Command#1"><data>146</data></item></item><item name="IrCode#1_1">') in src


def test_the_tail_says_how_the_product_is_connected_and_what_it_can_be():
    assert '<item name="ConnectionType#1"><data>WIRELESS</data></item>' in entry()
    assert '<item name="ConnectionType#1"><data>WIRED</data></item>' in entry(wireless=False)
    assert entry().endswith('<item name="RoleList#1"><item name="Roles#1"><item name="Role#1">'
                            '<data>integratedMaster</data></item></item></item>'
                            '<item name="Integrate#1"><item name="Role#1"><data>none</data></item>'
                            '<item name="Jid#1"><item name="^emptyValue"><data>true</data></item></item></item></item>')


def test_the_request_carries_no_revision_and_the_check_ignores_deletion_marks():
    req = configuration.insert_request(entry())
    assert req.startswith("<Insert xmlns='beocore.configuration.1'><config><item name=\"Devices#1\">"
                          '<item name="%s">' % KEY)
    assert req.endswith("</item></config><version>1</version></Insert>") and "_revision" not in req
    doc = "<config>%s</config><deleted></deleted>" % configuration.item("Devices#1", entry())
    assert configuration.enrolled(doc, pinned.OUR_JID)
    gone = ('<config><item name="Devices#1"></item></config><deleted><item name="Devices#1">'
            '<item name="%s"><data>7</data></item></item></deleted>' % KEY)
    assert not configuration.enrolled(gone, pinned.OUR_JID)


class Leader:
    """A leader that answers from a script and records what it was asked."""

    def __init__(self, document, insert_reply="<iq type='result'><Insert><accepted>1</accepted></Insert></iq>"):
        self.document, self.insert_reply, self.asked = document, insert_reply, []

    def session(self, ident, ip, peer_jid=None, name=""):
        leader = self

        class S:
            def open(self):
                return self

            def request(self, body, timeout=8):
                leader.asked.append(body)
                return leader.insert_reply if body.startswith("<Insert") else leader.document

            def close(self):
                pass
        return S()


def ensure(leader, monkeypatch, who=("203.0.113.10", pinned.PEER_JID)):
    monkeypatch.setattr(configuration.time, "sleep", lambda s: None)
    return configuration.ensure_enrolled(IDENT, entry(), leader=lambda: who,
                                         session_factory=leader.session)


def test_an_entry_that_is_there_is_left_alone(monkeypatch):
    there = Leader("<config>%s</config><deleted></deleted>" % configuration.item("Devices#1", entry()))
    assert ensure(there, monkeypatch) == configuration.PRESENT
    assert len(there.asked) == 1 and there.asked[0].startswith("<_notification-subscribe")


def test_a_missing_entry_is_read_for_first_and_then_sent(monkeypatch):
    missing = Leader('<config><item name="Devices#1"></item></config><deleted></deleted>')
    assert ensure(missing, monkeypatch) == configuration.ENROLLED
    assert [a[:7] for a in missing.asked] == ["<_notif", "<Insert"]
    assert missing.asked[1] == configuration.insert_request(entry())


def test_nothing_is_sent_without_a_leader_or_without_having_read_the_document(monkeypatch):
    silent = Leader(None)
    assert ensure(silent, monkeypatch) == configuration.NO_ANSWER and len(silent.asked) == 1
    assert ensure(silent, monkeypatch, who=None) == configuration.NO_LEADER and len(silent.asked) == 1
    refusing = Leader('<config></config>', insert_reply="<iq type='error'/>")
    assert ensure(refusing, monkeypatch) == configuration.REFUSED


def old_shape():
    """Our entry as an earlier version wrote it: the sources' jid in literal dots."""
    return entry().replace('<item name="Jid#1"><data>%s</data>' % KEY,
                           '<item name="Jid#1"><data>%s</data>' % pinned.OUR_JID)


def stamped(entry_xml):
    """As the leader stores it: a revision under every leaf."""
    return entry_xml.replace("</data>", '</data><item name="_revision"><data>3640</data></item>')


def test_a_stored_entry_differs_only_when_it_says_something_else():
    ours = entry()
    same = "<config>%s</config><deleted></deleted>" % configuration.item("Devices#1", stamped(ours))
    assert not configuration.entry_differs(same, pinned.OUR_JID, ours)
    dots = "<config>%s</config><deleted></deleted>" % configuration.item("Devices#1", stamped(old_shape()))
    assert configuration.entry_differs(dots, pinned.OUR_JID, ours)
    renamed = configuration.device_entry(IDENT, [SOURCES["CD"].named("Reference & Source")])
    doc = "<config>%s</config><deleted></deleted>" % configuration.item("Devices#1", stamped(renamed))
    assert configuration.entry_differs(doc, pinned.OUR_JID, ours)
    other = ProductIdentity.from_jid(pinned.PEER_JID, "Other", "00:00:5e:00:53:02")
    other_old = configuration.device_entry(other, [SOURCES["CD"]]).replace(
        '<data>%s</data>' % key_of(pinned.PEER_JID), '<data>%s</data>' % pinned.PEER_JID)
    doc = "<config>%s</config><deleted></deleted>" % configuration.item("Devices#1", other_old + stamped(ours))
    assert not configuration.entry_differs(doc, pinned.OUR_JID, ours)
    assert configuration.own_entry(doc, pinned.OUR_JID).startswith('<item name="%s">' % KEY)
    assert configuration.leaves(stamped(ours)) == configuration.leaves(ours)


def key_of(jid):
    return configuration.key(jid)


def test_an_entry_that_no_longer_says_what_we_write_is_sent_again(monkeypatch):
    stale = Leader("<config>%s</config><deleted></deleted>" % configuration.item("Devices#1", old_shape()))
    assert ensure(stale, monkeypatch) == configuration.REWRITTEN
    assert [a[:7] for a in stale.asked] == ["<_notif", "<Insert"]
    assert stale.asked[1] == configuration.insert_request(entry())
