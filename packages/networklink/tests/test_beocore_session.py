"""A session we dialled learns the peer's jid from its stream header."""
import socket
import time

import golden_identities as pinned
from beo.networklink.beocore import Session
from beo.product.identity import ProductIdentity

IDENT = ProductIdentity.from_jid(pinned.OUR_JID, pinned.OUR_NAME, pinned.OUR_MAC)


def dialled(peer_side_bytes: bytes):
    ours, theirs = socket.socketpair()
    session = Session(IDENT, "127.0.0.1", name="test-session")
    session.sock = ours
    ours.settimeout(1.0)
    session.start()
    theirs.sendall(peer_side_bytes)
    return session, theirs


def test_the_peers_stream_header_names_it_so_nothing_waits_for_a_stanza():
    header = ("<stream:stream to='%s' from='%s' xmlns='jabber:client' "
              "xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>"
              % (pinned.OUR_JID, pinned.PEER_JID)).encode()
    session, theirs = dialled(header)
    t0 = time.monotonic()
    assert session.wait_peer(3.0) == pinned.PEER_JID
    assert time.monotonic() - t0 < 1.0
    session.close()
    theirs.close()


def test_a_header_without_a_sender_leaves_the_jid_to_the_first_stanza():
    header = (b"<stream:stream xmlns='jabber:client' "
              b"xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>")
    session, theirs = dialled(header)
    assert session.wait_peer(0.3) is None
    theirs.sendall(("<iq id='1' type='get' from='%s'><query xmlns='http://jabber.org/protocol/disco#info'/></iq>"
                    % pinned.PEER_JID).encode())
    assert session.wait_peer(3.0) == pinned.PEER_JID
    session.close()
    theirs.close()
