from beo.networklink.nsdk import FpsdLog


FPSD = """\
Sep 22 2 01:02:03.100 fps:Pipeline clock is: GstPTPClock
Sep 22 2 01:02:03.200 fps:Not valid PTS in first sequence 1, PTS=126:23:22.000, ms to play -14998138920 pipeline time now 4292:32:20.921
Sep 22 2 01:02:03.300 fps:Not valid PTS in first sequence 2, PTS=126:23:22.024, ms to play -14998138920 pipeline time now 4292:32:20.945
Sep 22 2 01:02:03.400 fps:Not valid PTS in first sequence 3, PTS=126:23:22.048, ms to play -14998138920 pipeline time now 4292:32:20.969
Sep 22 2 01:02:03.500 fps:Not valid PTS in first sequence 4, PTS=126:23:22.072, ms to play -14998138920 pipeline time now 4292:32:20.993
Sep 22 2 01:02:03.600 fps:Not valid PTS in first sequence 5, PTS=126:23:22.096, ms to play -14998138920 pipeline time now 4292:32:21.017
Sep 22 2 01:05:00.000 fps:buf=1000, PTS=4292:33:00.000, now=4292:32:58.920, ptsToPlayMs=1080/1082/1126
Sep 22 2 01:05:22.000 fps:buf=2000, PTS=4292:33:22.000, now=4292:33:20.930, ptsToPlayMs=1070/1081/1126
Sep 22 2 01:05:23.000 fps:Sink dropout [/__w/x.c:12]
"""


def test_fpsd_log_parse_and_offset():
    lg = FpsdLog.from_text(FPSD)
    assert len(lg.invalid) == 5 and len(lg.running) == 2
    assert lg.pts_to_play_ms() == [1080, 1070]
    assert lg.dropouts == ["01:05:23.000 Sink dropout"]
    assert lg.ptp == ["01:02:03.100 Pipeline clock is: GstPTPClock"]
    off = lg.estimate_offset_ns()
    expected = (4292 * 3600 + 32 * 60 + 20.921) - (126 * 3600 + 23 * 60 + 22.0)
    assert abs(off - expected * 1e9) < 2_000_000       # within 2 ms (float in the log)
    assert FpsdLog.from_text("nothing").estimate_offset_ns() is None


def test_estimate_offset_from_running_buffer_line_when_no_invalid_pts():
    """Debug logging at default level writes no 'Not valid PTS' lines, only
    the first running-buffer line with PTS and now."""
    line = ("20260926 19:10:44.450 [I] GST.fpsreceiver: buf=0, PTS=1105:25:54.023373912, "
            "now=4384:32:06.977590809, ptsToPlayMs=-11804773059/0/0, maxIdleMs=138588, heapKb=20575  [/__w/x")
    fl = FpsdLog.from_text(line)
    off = fl.estimate_offset_ns()
    assert off is not None
    assert abs(off / 1e9 - 11_804_772.954) < 0.01


def test_health_counts_holes_resyncs_and_the_lead_in_the_last_window():
    from beo.networklink.nsdk import FpsdLog
    text = "\n".join([
        "20261018 23:20:10.160 [W] GST.audioresample<sink_audioresample>: encountered timestamp discontinuity of 7421 samples = 0:00:00.168276644  [x]",
        "20261018 23:20:19.811 [W] GST.audioresample<sink_audioresample>: encountered timestamp discontinuity of 2126 samples = 0:00:00.048208617  [x]",
        "20261018 23:21:13.926 [W] GST.balsasink: PLC: OFF, Skew weight limit reached: 4000.000000 ms. 71 buffers in a row have been skewed. Resync'ing.  Buffer PTS=1:2:3.4, Now=1:2:3.3  [x]",
        "20261018 23:21:16.167 [I] GST.fpsl_sink: Sink dropout, remSrc=UNDEFINED(5), sinks=1, SinkMs{0}, TimeMs={0}  [x]",
        "20261018 23:22:00.328 [I] GST.fpsreceiver: buf=5001, PTS=4916:43:24.986928318, now=4916:43:23.974090581, ptsToPlayMs=982/1012/1015, maxIdleMs=60, heapKb=1711  [x]",
        "20261018 23:22:24.409 [I] GST.fpsreceiver: buf=6001, PTS=4916:43:49.068230640, now=4916:43:48.043977400, ptsToPlayMs=874/1006/1043, maxIdleMs=168, heapKb=1732  [x]",
        "20261018 23:22:24.409 [I] GST.fpsreceiver: retrans asked=[4,4,4,7] dropped=0, dup=0, i={40,0,40,1,153,0}  [x]",
    ])
    h = FpsdLog.from_text(text).health(600)
    assert (h["discontinuities"], h["holes_by_datagrams"]) == (2, {2: 1, 7: 1})
    assert h["resyncs"] == 1 and h["dropouts"] == 1 and h["retransmissions_asked"] == 7
    assert h["ptsToPlayMs"] == {"min": 874, "mean": 928, "max": 982} and h["reports"] == 2
    assert FpsdLog.from_text(text).health(60)["discontinuities"] == 0
    assert FpsdLog.from_text("").health() == {"lines": 0}
    # a log that runs past midnight counts back across it
    late = text + chr(10) + "20261019 00:03:16.739 [I] SomeComponent: idle  [x]"
    assert FpsdLog.from_text(late).health(45 * 60)["resyncs"] == 1
    assert FpsdLog.from_text(late).health(60)["resyncs"] == 0


def test_the_products_own_log_is_read_from_the_package_oldest_first():
    import io
    import tarfile
    from beo.networklink.nsdk import FpsdLog
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in (("tmp/log-nSDK", "20261019 21:51:41 INFO later\n"),
                           ("tmp/log-nSDK.0", "20261018 09:00:00 INFO earlier\n"),
                           ("tmp/log-fpsd", "nothing\n")):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    assert FpsdLog.nsdk_lines(buf.getvalue()) == ["20261018 09:00:00 INFO earlier", "20261019 21:51:41 INFO later"]
