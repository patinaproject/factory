import unittest

from patina_evals.speed import Recorder, build_speed_report


def span(kind, start, end, trial=None, ident=None):
    return {"id": ident or f"{kind}{start}", "kind": kind, "trial": trial, "parent": None, "start_ns": start, "end_ns": end}


class FakeClock:
    def __init__(self, *ticks):
        self.ticks = iter(ticks)

    def __call__(self):
        return next(self.ticks)


class SpeedReportTest(unittest.TestCase):
    def test_parallel_children_are_not_added_to_wall_time(self):
        spans = [
            span("run", 0, 100),
            span("trial", 10, 90, "a"),
            span("trial", 10, 90, "b"),
            span("trial", 10, 90, "c"),
        ]
        report = build_speed_report(spans, [])
        self.assertEqual(report["end_to_end_wall_ns"], 100)
        self.assertEqual(report["wall_source"], "run")

    def test_wall_time_without_run_span_is_trial_union(self):
        spans = [span("trial", 0, 50, "a"), span("trial", 30, 80, "b"), span("trial", 200, 210, "c")]
        report = build_speed_report(spans, [])
        self.assertEqual(report["end_to_end_wall_ns"], 90)
        self.assertEqual(report["wall_source"], "trial_union")

    def test_no_spans_gives_unknown_wall_time(self):
        report = build_speed_report([], [])
        self.assertIsNone(report["end_to_end_wall_ns"])
        self.assertEqual(report["flags"], ["no_run_or_trial_span"])
        self.assertIsNone(report["trials"]["median_ns"])

    def test_parts_report_sum_and_union_separately(self):
        spans = [span("setup", 0, 10, "a"), span("setup", 0, 10, "b"), span("grading", 20, 25, "a"), span("wait", 5, 6, "a")]
        parts = build_speed_report(spans, [])["parts"]
        self.assertEqual(parts["setup"], {"spans": 2, "sum_ns": 20, "union_ns": 10})
        self.assertEqual(parts["grading"], {"spans": 1, "sum_ns": 5, "union_ns": 5})
        self.assertEqual(parts["wait"], {"spans": 1, "sum_ns": 1, "union_ns": 1})
        self.assertEqual(parts["solve"], {"spans": 0, "sum_ns": 0, "union_ns": 0})

    def test_median_and_p95_of_trial_durations(self):
        spans = [span("trial", 0, d, f"t{d}", f"t{d}") for d in (10, 20, 30, 40, 50, 60, 70, 80, 90, 1000)]
        trials = build_speed_report(spans, [])["trials"]
        self.assertEqual(trials, {"count": 10, "median_ns": 55.0, "p95_ns": 1000})

    def test_counts_sum_across_trials(self):
        counts = [
            {"trial": "a", "calls": 3, "turns": 2, "retries": 1, "retry_delay_ns": 500},
            {"trial": "b", "calls": 4, "turns": 1, "retries": 0, "retry_delay_ns": 0},
        ]
        report = build_speed_report([span("run", 0, 1)], counts)
        self.assertEqual((report["calls"], report["turns"], report["retries"], report["retry_delay_ns"]), (7, 3, 1, 500))

    def test_unfinished_span_is_rejected(self):
        with self.assertRaises(ValueError):
            build_speed_report([{"id": "x", "kind": "run", "start_ns": 5, "end_ns": None}], [])


class RecorderTest(unittest.TestCase):
    def test_spans_use_the_injected_monotonic_clock(self):
        recorder = Recorder(clock=FakeClock(100, 110, 120, 150))
        with recorder.span("run") as run_id:
            with recorder.span("setup", trial="a", parent=run_id):
                pass
        self.assertEqual(
            recorder.spans(),
            [
                {"id": "s2", "kind": "setup", "trial": "a", "parent": "s1", "start_ns": 110, "end_ns": 120},
                {"id": "s1", "kind": "run", "trial": None, "parent": None, "start_ns": 100, "end_ns": 150},
            ],
        )

    def test_span_closes_when_the_body_raises(self):
        recorder = Recorder(clock=FakeClock(1, 9))
        with self.assertRaises(RuntimeError):
            with recorder.span("trial", trial="a"):
                raise RuntimeError("boom")
        self.assertEqual(recorder.spans()[0]["end_ns"], 9)

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            with Recorder().span("sleep"):
                pass

    def test_default_clock_is_monotonic(self):
        recorder = Recorder()
        with recorder.span("run"):
            pass
        recorded = recorder.spans()[0]
        self.assertGreaterEqual(recorded["end_ns"], recorded["start_ns"])

    def test_recorder_feeds_the_report(self):
        recorder = Recorder(clock=FakeClock(0, 10, 20, 30, 40, 100))
        with recorder.span("run"):
            with recorder.span("trial", trial="a"):
                with recorder.span("wait", trial="a"):
                    pass
        recorder.count("a", calls=2, turns=1, retries=1, retry_delay_ns=7)
        report = build_speed_report(recorder.spans(), recorder.counts())
        self.assertEqual(report["end_to_end_wall_ns"], 100)
        self.assertEqual(report["parts"]["wait"]["sum_ns"], 10)
        self.assertEqual(report["calls"], 2)
        self.assertEqual(report["retry_delay_ns"], 7)


if __name__ == "__main__":
    unittest.main()
