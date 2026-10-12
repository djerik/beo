#!/usr/bin/env python3
"""Play a 440 Hz tone on an ASE speaker (or an ALSA capture on the Pi).

    python examples/tone_to_speaker.py --peer 203.0.113.10 --peer-jid 2730.1200322.90000010 \
        --jid 2730.1200322.90000001 --name "Tone Source" --mac 00:00:5E:00:53:01 \
        --ptp-offset-ns 14998138921000000 --seconds 30 [--alsa hw:0,0]

The PTP offset is per machine (measure it with NsdkClient + FpsdLog, see
beo.networklink.nsdk); without it the listener rejects every PTS and stays silent.
"""
import argparse
import logging
import time

from beo.networklink import (AlsaCapture, Catalog, Fanout, OffsetPtpClock, Peer, ProductIdentity,
                             SOURCES, STANDARD_SOURCES, SourceRole, ToneSource, FORMAT_44100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", required=True, help="listener IP")
    ap.add_argument("--peer-jid", required=True)
    ap.add_argument("--jid", required=True)
    ap.add_argument("--name", default="Tone Source")
    ap.add_argument("--mac", required=True)
    ap.add_argument("--ptp-offset-ns", type=int, default=0)
    ap.add_argument("--source", default="beoradio", choices=sorted(SOURCES))
    ap.add_argument("--alsa", default=None, help="ALSA device, e.g. hw:0,0 (Pi only)")
    ap.add_argument("--seconds", type=float, default=30)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    ident = ProductIdentity.from_jid(a.jid, a.name, a.mac)
    peer = Peer(jid=a.peer_jid if "@" in a.peer_jid else a.peer_jid + "@products.bang-olufsen.com",
                ip=a.peer, product_type="CA17")
    fmt = FORMAT_44100                                   # 44.1 kHz, the only rate (fps.RATE_HZ)

    # (a) capture
    if a.alsa:
        fanout = Fanout(AlsaCapture(a.alsa, fmt))    # started on first subscribe
        audio = fanout.subscribe(peer.jid)
    else:
        fanout = None
        audio = ToneSource(fmt, 440.0, 0.35)

    # (b) expand
    role = SourceRole(ident, Catalog(STANDARD_SOURCES),
                      OffsetPtpClock(a.ptp_offset_ns),
                      on_listener_joined=lambda p: print("joined:", p.jid),
                      on_listener_left=lambda p: print("left:", p.jid))
    lis = role.expand(peer, SOURCES[a.source], audio)
    if lis is None:
        print("expand failed")
        return 1
    try:
        time.sleep(a.seconds)
    except KeyboardInterrupt:
        pass
    print("sent:", lis.stats())
    role.unexpand(peer)
    if fanout:
        fanout.unsubscribe(peer.jid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
