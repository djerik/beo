"""Invariant 1: all outgoing iq ids are decimal and one rising sequence."""
import re

import pytest

from beo.networklink.beocore import IdCounter, Session, check_id
from beo.product.identity import ProductIdentity

IDENT = ProductIdentity.from_jid("2730.1200322.90000001", "Reference Source", "00:00:5E:00:53:01")


def test_counter_is_decimal_and_rising():
    c = IdCounter()
    ids = [c.next() for _ in range(50)]
    assert ids[:3] == ["201", "202", "203"]
    assert all(re.fullmatch(r"\d+", i) for i in ids)
    assert [int(i) for i in ids] == sorted(int(i) for i in ids)
    assert IdCounter(51900).next() == "51901"


@pytest.mark.parametrize("bad", ["abc", "51002x", "", "1-2", "0x10", "purple", " 5"])
def test_session_send_rejects_non_decimal(bad):
    class Sock:
        def sendall(self, b):
            raise AssertionError("must not reach the socket")
    s = Session(IDENT, "127.0.0.1", peer_jid="x@y")
    s.sock = Sock()
    with pytest.raises(ValueError):
        s.send("<x/>", iq_id=bad)
    with pytest.raises(ValueError):
        check_id(bad)


def test_session_send_accepts_decimal_and_uses_counter():
    out = []

    class Sock:
        def sendall(self, b):
            out.append(b.decode())
    s = Session(IDENT, "127.0.0.1", peer_jid="peer@x", ids=IdCounter(51900))
    s.sock = Sock()
    assert s.send("<a/>", iq_id="51002") == "51002"
    assert s.send("<b/>") == "51901"
    assert s.send("<c/>", typ="get") == "51902"
    assert out[0] == '<iq id="51002" to="peer@x" from="%s" type="set"><a/></iq>' % IDENT.full_jid()
    assert 'type="get"' in out[2]
