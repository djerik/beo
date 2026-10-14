from __future__ import annotations

import time
from dataclasses import replace

from beo.masterlink.codec import Beo4, PlayState, Telegram, checksum, decode_beo4
from beo.masterlink.music_system import MusicSystem, OperationFailed
from beo.masterlink.profiles import BS9000, MusicSystemProfile, Source

from bus_support import wait_until

STATUS = bytearray.fromhex("83c10114008d008715048d0100000008010100ffff0201ffffffffffffffff1700")


def status(disc: int, track, state: int, mask: int) -> Telegram:
    raw = bytearray(STATUS)
    raw[15] = mask
    raw[17] = disc
    raw[19] = 0xFF if track is None else track
    raw[21] = state
    raw[-2] = checksum(bytes(raw[:-2]))
    return Telegram.parse(bytes(raw))


class Machine:
    """A tiny BeoSound 9000: answers presses with 0x87 the way the real one does.

    A bare digit changes the disc while the machine is listening on CD. When it
    is not, a digit counts only in the window that opens when CD is pressed
    from standby. Slot 3 is empty and is skipped silently; slot 6 is empty and
    the machine goes to disc 1 instead.
    """

    def __init__(self, listening: bool = True, slow_mechanics: bool = False):
        self.disc, self.track, self.state = 1, 3, 0x02
        self.listening = listening
        self.slow_mechanics = slow_mechanics      # the track comes later than any wait
        self.cd_window = False

    def __call__(self, sent: Telegram):
        k = decode_beo4(sent)
        if not k:
            return []
        key = k[0]
        if key == Beo4.STANDBY:
            self.state = 0x06
            self.cd_window = False
            return [status(self.disc, self.track, 0x06, 0x20)]
        if key == Beo4.CD:
            self.cd_window = self.state == 0x06
            self.state = 0x02
            return [status(self.disc, self.track, 0x02, 0x20)]
        if key in (Beo4.STEP_UP, Beo4.STEP_DOWN):
            self.track += 1 if key == Beo4.STEP_UP else -1
            return [status(self.disc, self.track, self.state, 0x08),
                    status(self.disc, self.track, self.state, 0x00)]     # the echo
        if key == Beo4.STOP:
            self.state = 0x01
            return [status(self.disc, self.track, 0x01, 0x20)]
        if key == Beo4.PLAY:
            self.state = 0x02
            return [status(self.disc, self.track, 0x02, 0x20)]
        if 1 <= int(key) <= 6 and (self.cd_window or (self.listening and self.state == 0x02)):
            direct = not self.cd_window
            self.cd_window = False
            if int(key) == 3:
                return []
            if int(key) == 6:
                self.disc, self.track = 1, 1
                return [status(1, None, 0x02, 0x02), status(1, 1, 0x02, 0x08)]
            old_track = self.track
            self.disc, self.track = int(key), 1
            if self.slow_mechanics:
                return [status(self.disc, None, 0x02, 0x0A), status(self.disc, None, 0x02, 0x00)]
            if direct:
                # new disc with the old track first, the first track once the mechanics are done
                return [status(self.disc, old_track, 0x02, 0x02), status(self.disc, 1, 0x02, 0x08)]
            return [status(self.disc, None, 0x02, 0x02), status(self.disc, 1, 0x02, 0x08)]
        return []


def quick(standby_delay_s: float = 0.02) -> MusicSystemProfile:
    """The 9000 with its waits cut down to what a test can sit through."""
    return replace(BS9000, discs=replace(BS9000.discs, standby_delay_s=standby_delay_s, digit_delay_s=0.01,
                                         confirm_timeout_s=1.0, digit_timeout_s=0.1))


def make(fake_bus, listening: bool = True, standby_delay_s: float = 0.02, slow_mechanics: bool = False):
    bus, tr = fake_bus
    m = Machine(listening, slow_mechanics)
    tr.responder = m
    p = MusicSystem(bus, quick(standby_delay_s), digit_gap_s=0.0, press_timeout_s=1.0)
    p.start()
    return p, m, tr


def test_next_confirmed_by_status_and_echo_deduped(fake_bus):
    p, m, tr = make(fake_bus)
    changes = []
    p.on_state_changed.append(lambda o, n: changes.append(n))
    st = p.next().result(timeout=2)
    assert st.track == 4 and st.disc == 1
    assert len([c for c in changes if c.track == 4]) == 1      # echo did not fire twice


def keys_sent(tr):
    return [decode_beo4(t)[0] for t in tr.sent if decode_beo4(t)]


def test_select_disc_by_a_bare_digit(fake_bus):
    p, m, tr = make(fake_bus)
    st = p.select_disc(2).result(timeout=3)
    assert st.disc == 2
    assert keys_sent(tr) == [Beo4.DIGIT_2]


def test_a_disc_named_before_its_track_is_selected(fake_bus):
    p, m, tr = make(fake_bus, slow_mechanics=True)
    st = p.select_disc(2).result(timeout=3)
    assert st.disc == 2
    assert keys_sent(tr) == [Beo4.DIGIT_2]          # no standby route


def test_select_disc_falls_back_to_the_standby_route(fake_bus):
    p, m, tr = make(fake_bus, listening=False)
    st = p.select_disc(2).result(timeout=3)
    assert st.disc == 2 and st.track == 1
    assert keys_sent(tr) == [Beo4.DIGIT_2, Beo4.STANDBY, Beo4.CD, Beo4.DIGIT_2]


def test_select_empty_slot_reports_failure(fake_bus):
    p, m, tr = make(fake_bus)
    f = p.select_disc(3)
    try:
        f.result(timeout=3)
        assert False, "expected OperationFailed"
    except OperationFailed as e:
        assert "disc 3" in str(e)
    assert keys_sent(tr) == [Beo4.DIGIT_3, Beo4.STANDBY, Beo4.CD, Beo4.DIGIT_3]


def test_a_skipped_slot_does_not_take_the_standby_route(fake_bus):
    p, m, tr = make(fake_bus)
    f = p.select_disc(6)
    try:
        f.result(timeout=3)
        assert False, "expected OperationFailed"
    except OperationFailed as e:
        assert "went to disc 1" in str(e)
    assert keys_sent(tr) == [Beo4.DIGIT_6]


def test_last_wins_for_absolute_operations(fake_bus):
    # the first route is slow enough to be superseded
    p, m, tr = make(fake_bus, listening=False, standby_delay_s=0.3)
    f2 = p.select_disc(2)
    f5 = p.select_disc(5)
    assert f2.cancelled() or f2.exception(timeout=3) is not None
    assert f5.result(timeout=3).disc == 5


def test_stop_clears_queue(fake_bus):
    p, m, tr = make(fake_bus, listening=False, standby_delay_s=0.3)
    fdisc = p.select_disc(2)
    fnext = p.next()
    fstop = p.stop()
    assert fstop.result(timeout=3).play_state == PlayState.STOP
    assert fnext.cancelled()
    assert fdisc.cancelled() or fdisc.exception(timeout=3) is not None


def test_no_confirmation_is_a_failure(fake_bus):
    bus, tr = fake_bus
    tr.responder = lambda sent: []     # a silent machine
    p = MusicSystem(bus, BS9000, press_timeout_s=0.2)
    p.start()
    try:
        p.play().result(timeout=2)          # play needs a confirming 0x87
        assert False
    except OperationFailed:
        pass
    # next/prev are different: silence means "the 9000 did not change track"
    assert p.next().result(timeout=2) == p.state


def test_distributed_source_query(fake_bus):
    bus, tr = fake_bus
    tr.responder = lambda sent: [Telegram.parse(bytes.fromhex("06c10114000000080506020100a1019400"))] \
        if sent.payload_type == 0x08 else []
    p = MusicSystem(bus, BS9000)
    p.start()
    assert p.distributed_source(timeout=1) == 0xA1


def test_state_follows_machine_not_keys(fake_bus):
    p, m, tr = make(fake_bus)
    tr.inject(status(4, 7, 0x02, 0x2A))
    assert wait_until(lambda: p.state.disc == 4 and p.state.track == 7 and p.state.is_playing)
    tr.inject(status(4, None, 0x07, 0x22))      # slot reported empty: pointer stays
    time.sleep(0.05)
    assert p.state.track == 7 and 4 in p.state.empty_discs


def test_a_profile_names_its_sources_by_slot_and_by_byte():
    assert BS9000.main.key == "CD" and BS9000.discs.count == 6
    assert BS9000.source("radio").code == 0x6F and BS9000.source("tape") is None
    assert BS9000.source_coded(0x8D) is BS9000.main
    assert BS9000.source_coded(0x7A) is None and BS9000.source_coded(None) is None


def test_a_machine_without_discs_has_none_to_select(fake_bus):
    bus, tr = fake_bus
    single = MusicSystemProfile("synthetic", "Synthetic player", False, (Source("CD", 0x8D, Beo4.CD),))
    p = MusicSystem(bus, single)
    p.start()
    try:
        p.select_disc(1).result(timeout=2)
        assert False, "expected OperationFailed"
    except OperationFailed as e:
        assert "no discs" in str(e)
    assert keys_sent(tr) == []


def test_the_keys_of_a_queued_operation_go_to_the_first_source(fake_bus):
    bus, tr = fake_bus
    other = MusicSystemProfile("synthetic", "Synthetic machine", False,
                               (Source("CD", 0x79, Beo4.A_MEM), Source("RADIO", 0x6F, Beo4.RADIO)))
    p = MusicSystem(bus, other, press_timeout_s=0.1)
    p.start()
    p.next().result(timeout=2)
    assert tr.sent[-1].body[-2:] == bytes([0x79, Beo4.STEP_UP])
    p.request_distribution()
    assert wait_until(lambda: tr.sent[-1].body.hex() == "c106010b00000045070102790002010000")
