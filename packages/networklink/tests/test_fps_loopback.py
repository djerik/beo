"""Step 1 proof (architecture §9): FpsReceiver against FpsSender over real
sockets on loopback — 0 gaps, PTS cadence exact, heartbeat received."""
import threading
import time


from beo.networklink.audio import ToneSource
from beo.networklink.fps import FpsReceiver, FpsSender, FORMAT_44100, MonotonicClock

PORT = 12340


def test_loopback_cadence_and_no_gaps():
    rx = FpsReceiver(FORMAT_44100, port=PORT, bind="127.0.0.1").open()
    got = []

    def pull():
        for b in rx.blocks(seconds=1.6):
            got.append(b)
    t = threading.Thread(target=pull, daemon=True)
    t.start()
    time.sleep(0.2)

    tx = FpsSender("127.0.0.1", FORMAT_44100, MonotonicClock(), port=PORT)
    tx.start(ToneSource(FORMAT_44100))
    time.sleep(1.2)
    tx.stop()
    t.join(3)
    rx.close()

    st = rx.stats()
    assert st["datagrams"] >= 40, st
    assert st["gaps"] == 0, st
    assert st["short"] == 0
    assert st["markers"] == 1 and st["marker_counters"] == [2]   # after datagram #1
    assert st["payload_sizes"] == [4248]
    assert st["header_constants"] == [("00016100", "ffffffffffffffff")]
    assert st["device_clock_exact"] == st["deltas"] == st["stream_clock_exact"]
    assert got[0].stream_ns == 300_000_000
    assert 38 <= st["arrival_rate"] <= 45           # 41.5/s nominal at 44100
    assert st["heartbeat_bytes"] >= 1 and st["tcp_peer"] == "127.0.0.1"
    assert tx.stats().datagrams_sent == st["datagrams"]
