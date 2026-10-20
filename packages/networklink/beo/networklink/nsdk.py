"""NSDK API on port 80, and the listener's fpsd log as an oracle.

ASE products have an NSDK API on port 80 (the one the web page «Submit
logfiles» uses). An activate on systemManager:createLogFile packs all logs
into a tgz, fetched at /api/stream//tmp/temp_data_logs.tgz. In tmp/log-fpsd*
the FPS daemon writes what it thinks of every stream:

  «Not valid PTS in first sequence N, PTS=P, ... pipeline time now T»
        P is OUR stamp on packet N (offset 8..15 in the FPS header), T is the
        listener's PTP clock when it received it. T-P is the offset between
        our monotonic clock and the network's PTP time — what
        OffsetPtpClock.offset_ns must be. If these lines exist our PTSs are
        invalid and there is silence.
  «buf=N, PTS=P, now=T, ptsToPlayMs=a/b/c»
        every 1000th buffer of a RUNNING reception. P-T is how far ahead our
        packets lie; a product's own lie 1.11 s ahead. a/b/c are now/mean/max
        in ms. Positive numbers = the packets are played.
  «Sink dropout», «Buffer level critically low»
        the receiver gets no usable audio.

In operation ``ptp.PtpSlaveClock`` follows the network's PTP directly, so the
offset is never measured for a normal start. ``estimate_offset_ns`` is for the
fallback offset the slave uses until it hears a master: stream once (the PTSs
are rejected), ``FpsdLog.parse(client.fetch_logs()).estimate_offset_ns()``,
store the result. The health check after any change is ptsToPlayMs ~1080.
"""
from __future__ import annotations

import io
import json
import logging
import re
import statistics
import tarfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

log = logging.getLogger("beo.networklink.nsdk")


class NsdkClient:
    """``/api/getData``, ``/api/setData``, ``/api/stream/`` on port 80."""

    def __init__(self, ip: str, *, timeout: float = 180):
        self.base = "http://%s" % ip
        self.timeout = timeout

    def api(self, op: str, **q) -> str:
        q["_nocache"] = "1"
        url = self.base + "/api/" + op + "?" + urllib.parse.urlencode(q)
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "replace")

    def get(self, path: str, roles: str = "value") -> Any:
        """getData. Paths must be whitelisted by the product ("not whitelisted"
        comes back as text). Returns the decoded JSON, or the raw text."""
        t = self.api("getData", path=path, roles=roles)
        try:
            return json.loads(t)
        except ValueError:
            return t

    def set(self, path: str, roles: str, value: Any) -> Any:
        """setData. ``value`` is JSON-encoded unless it is already a string."""
        v = value if isinstance(value, str) else json.dumps(value)
        t = self.api("setData", path=path, roles=roles, value=v)
        try:
            return json.loads(t)
        except ValueError:
            return t

    def stream(self, path: str, timeout: float = 300) -> bytes:
        """GET /api/stream/<path>. The web page builds host + "/api/stream/" +
        name — with a double slash for an absolute name."""
        with urllib.request.urlopen(self.base + "/api/stream/" + path, timeout=timeout) as s:
            return s.read()

    def fetch_logs(self) -> bytes:
        """Ask the product to pack its logs and fetch the package. ~1 MB, 10-20 s."""
        name = self.set("systemManager:createLogFile", "activate",
                        {"type": "logCreate", "logCreate": {"type": "beoWeb"}})
        return self.stream(name)          # "/tmp/temp_data_logs.tgz"


def _s(t: str) -> float:
    """'H:MM:SS.fff' -> seconds."""
    h, m, rest = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


@dataclass(frozen=True)
class InvalidPts:
    stamp: str          # log time "HH:MM:SS.mmm"
    sequence: int
    pts_s: float        # our PTS, seconds
    now_s: float        # the listener's pipeline (PTP) time, seconds


@dataclass(frozen=True)
class RunningBuf:
    stamp: str
    buf: int
    pts_s: float
    now_s: float
    pts_to_play_ms: int
    mean_ms: int
    max_ms: int


class FpsdLog:
    """The parsed fpsd log of an ASE listener."""

    def __init__(self, lines: List[str]):
        self.lines = lines
        self.invalid: List[InvalidPts] = []
        self.running: List[RunningBuf] = []
        self.dropouts: List[str] = []
        self.ptp: List[str] = []
        self.discontinuities: List[Tuple[str, int]] = []     # (stamp, samples)
        self.resyncs: List[str] = []
        self.retrans: List[Tuple[str, int]] = []              # (stamp, asked)
        self._assess()

    @staticmethod
    def fpsd_lines(tgz: bytes) -> List[str]:
        t = tarfile.open(fileobj=io.BytesIO(tgz), mode="r:*")
        out: List[str] = []
        for name in ("tmp/log-fpsd.0", "tmp/log-fpsd"):
            try:
                f = t.extractfile(name)
            except KeyError:
                continue
            if f:
                out += f.read().decode("utf-8", "replace").splitlines()
        return out

    @staticmethod
    def nsdk_lines(tgz: bytes) -> List[str]:
        """The product's nSDK log, oldest file first: what it did on the
        product-to-product plane, where the fpsd log has only the sound."""
        t = tarfile.open(fileobj=io.BytesIO(tgz), mode="r:*")
        out: List[str] = []
        for name in ("tmp/log-nSDK.0", "tmp/log-nSDK"):
            try:
                f = t.extractfile(name)
            except KeyError:
                continue
            if f:
                out += f.read().decode("utf-8", "replace").splitlines()
        return out

    @classmethod
    def parse(cls, tar_bytes: bytes) -> "FpsdLog":
        return cls(cls.fpsd_lines(tar_bytes))

    @classmethod
    def from_text(cls, text: str) -> "FpsdLog":
        return cls(text.splitlines())

    def _assess(self) -> None:
        for l in self.lines:
            m = re.search(r"first sequence (\d+), PTS=([\d:.]+), ms to play (-?\d+) "
                          r"pipeline time now ([\d:.]+)", l)
            if m:
                self.invalid.append(InvalidPts(l[9:21], int(m.group(1)), _s(m.group(2)),
                                               _s(m.group(4))))
                continue
            m = re.search(r"buf=(\d+), PTS=([\d:.]+), now=([\d:.]+), "
                          r"ptsToPlayMs=(-?\d+)/(-?\d+)/(-?\d+)", l)
            if m:
                self.running.append(RunningBuf(l[9:21], int(m.group(1)), _s(m.group(2)),
                                               _s(m.group(3)), int(m.group(4)),
                                               int(m.group(5)), int(m.group(6))))
                continue
            if "Sink dropout" in l or "critically low" in l or "Drop buffer" in l:
                self.dropouts.append(l[9:21] + " " + re.sub(r"\s+\[/__w.*$", "", l[26:])[:150])
            m = re.search(r"timestamp discontinuity of (-?\d+) samples", l)
            if m:
                self.discontinuities.append((l[9:21], int(m.group(1))))
            if "Resync'ing" in l:
                self.resyncs.append(l[9:21])
            m = re.search(r"retrans asked=\[(\d+),(\d+),(\d+),(\d+)\]", l)
            if m:
                self.retrans.append((l[9:21], int(m.group(4))))
            if "ptpd" in l or "PTPClock" in l or "RAWClock" in l:
                self.ptp.append(l[9:21] + " " + re.sub(r"\s+\[/__w.*$", "", l[26:])[:150])

    def health(self, window_s: float = 600.0) -> dict:
        """How the latest listening went, as counts: the last ``window_s``
        seconds of the log up to its last line.

        A discontinuity is a hole in the stream the listener had to bridge, in
        samples; 1062 is one datagram. A resync is the listener starting its
        player over, which is heard as a glitch. ptsToPlayMs is how far ahead
        the stream arrives; around 1000 is a source's own.
        """
        import datetime as _dt

        def moment(line: str) -> Optional[float]:
            # "YYYYMMDD HH:MM:SS.mmm ..." — with the day, so a night's log that
            # runs past midnight still counts back across it.
            try:
                return _dt.datetime.strptime(line[:21], "%Y%m%d %H:%M:%S.%f").timestamp()
            except ValueError:
                return None
        stamped = [(t, l) for l in self.lines if (t := moment(l)) is not None]
        if not stamped:
            return {"lines": 0}
        end = stamped[-1][0]
        recent = [l for t, l in stamped if end - t <= window_s]
        holes, sizes, resyncs, dropouts, retrans, pts = [], {}, 0, 0, 0, []
        for l in recent:
            m = re.search(r"timestamp discontinuity of (-?\d+) samples", l)
            if m:
                n = int(m.group(1))
                holes.append(n)
                k = max(1, round(abs(n) / 1062))
                sizes[k] = sizes.get(k, 0) + 1
            if "Resync'ing" in l:
                resyncs += 1
            if "Sink dropout" in l or "critically low" in l or "Drop buffer" in l:
                dropouts += 1
            m = re.search(r"retrans asked=\[(\d+),(\d+),(\d+),(\d+)\]", l)
            if m:
                retrans += int(m.group(4))
            m = re.search(r"ptsToPlayMs=(-?\d+)/", l)
            if m:
                pts.append(int(m.group(1)))
        return {
            "lines": len(stamped),
            "from": stamped[0][1][:21], "to": stamped[-1][1][:21],
            "window_from": recent[0][:21],
            "discontinuities": len(holes),
            "holes_by_datagrams": dict(sorted(sizes.items())),
            "resyncs": resyncs,
            "dropouts": dropouts,
            "retransmissions_asked": retrans,
            "ptsToPlayMs": {"min": min(pts), "mean": round(statistics.mean(pts)), "max": max(pts)} if pts else None,
            "reports": len(pts),
        }

    def pts_to_play_ms(self) -> List[int]:
        """ptsToPlayMs (now) per running-buffer line. ~1080 = healthy."""
        return [r.pts_to_play_ms for r in self.running]

    def estimate_offset_ns(self, min_pairs: int = 5) -> Optional[int]:
        """PTP time minus our PTS, median over the LATEST session of rejected
        packets (same minute block as the last line). None if too few pairs.

        Only sessions with invalid PTS can be used, so stream to the listener
        first. The pairs spread ~10 ms. Holds as long as this machine and the
        PTP master both stay up.
        """
        if self.invalid:
            last = self.invalid[-1].stamp[:5]
            d = [e.now_s - e.pts_s for e in self.invalid if e.stamp[:5] == last]
            if len(d) >= min_pairs:
                return round(statistics.median(d) * 1e9)
        # At fpsd's default log level there are no per-packet "Not valid PTS"
        # warnings; the running-buffer line of the first packet still pairs
        # our PTS with its PTP "now":
        #   buf=0, PTS=1105:25:54.023, now=4384:32:06.977, ptsToPlayMs=-11804773059/0/0
        # One pair is enough, the spread is ~10 ms; use the latest session
        # whose PTS is hours off.
        far = [r for r in self.running if abs(r.pts_to_play_ms) > 3_600_000]
        if not far:
            return None
        last = far[-1].stamp[:5]
        d = [r.now_s - r.pts_s for r in far if r.stamp[:5] == last]
        return round(statistics.median(d) * 1e9) if d else None


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m beo.networklink.nsdk <ip> [--minutes N]``: fetch the
    listener's logs and say how the latest listening went."""
    import argparse
    ap = argparse.ArgumentParser(description="How a product's last minutes of listening went, "
                                             "from its own log.")
    ap.add_argument("ip", help="the product that listened, by address")
    ap.add_argument("--minutes", type=float, default=10.0,
                    help="how far back from the log's last line to count (default 10)")
    ap.add_argument("--save", help="also write the fetched log package here")
    ap.add_argument("--find", metavar="REGEX",
                    help="instead of the counts, print the lines of the product's own log that "
                         "match, the last 200 of them: what it did with a join, a link or a source")
    a = ap.parse_args(argv)
    tgz = NsdkClient(a.ip).fetch_logs()
    if a.save:
        with open(a.save, "wb") as f:
            f.write(tgz)
    if a.find:
        pattern = re.compile(a.find, re.I)
        hits = [l for l in FpsdLog.nsdk_lines(tgz) if pattern.search(l)]
        for l in hits[-200:]:
            print(re.sub(r"\s+\[/__w.*$", "", l)[:300])
        print("-- %d matching line(s), %d shown" % (len(hits), min(len(hits), 200)))
        return 0
    h = FpsdLog.parse(tgz).health(a.minutes * 60)
    print(json.dumps(h, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["NsdkClient", "FpsdLog", "InvalidPts", "RunningBuf", "main"]
