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
