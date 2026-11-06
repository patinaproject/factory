import unittest

from patina_evals.ledger import CostUnknown, build_cost_report, load_prices, require_known_cost

PRICES = {
    "m": {"input": 1, "output": 10, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2},
    "n": {"input": 2, "output": 20, "cache_read": 0.2, "cache_write_5m": 2.5, "cache_write_1h": 4},
}


def usage(i=0, o=0, read=0, five=None, one=None, written=None):
    out = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": read}
    if five is not None or one is not None:
        out["cache_creation"] = {"ephemeral_5m_input_tokens": five or 0, "ephemeral_1h_input_tokens": one or 0}
        out["cache_creation_input_tokens"] = (five or 0) + (one or 0) if written is None else written
    elif written is not None:
        out["cache_creation_input_tokens"] = written
    return out


def event(**fields):
    base = {"scope": "worker", "agent": "main", "trial": "t1", "attempt": 1, "model": "m"}
    return base | fields


CARDS = {"c1": {"success": True, "trials": ["t1"]}}


def report(events, cards=CARDS, prices=PRICES):
    return build_cost_report(events, prices, cards)


def worker_tokens(r):
    return r["scopes"]["worker"]["tokens"]["value"]


class DedupeTest(unittest.TestCase):
    def test_repeated_stream_events_with_one_message_id_count_once_and_last_wins(self):
        r = report([
            event(message_id="msg1", usage=usage(i=100, o=1)),
            event(message_id="msg1", usage=usage(i=100, o=40)),
            event(message_id="msg1", usage=usage(i=100, o=40)),
            event(message_id="msg2", usage=usage(i=10, o=5)),
        ])
        self.assertEqual(worker_tokens(r)["input"], 110)
        self.assertEqual(worker_tokens(r)["output"], 45)
        self.assertEqual(r["scopes"]["worker"]["cost_usd"]["value"], 0.00056)

    def test_same_message_id_in_different_attempts_is_two_messages(self):
        r = report([
            event(message_id="msg1", attempt=1, usage=usage(i=100, o=1)),
            event(message_id="msg1", attempt=2, usage=usage(i=100, o=1)),
        ])
        self.assertEqual(worker_tokens(r)["input"], 200)

    def test_request_id_dedupes_when_there_is_no_message_id(self):
        r = report([event(request_id="req1", usage=usage(i=7, o=1)), event(request_id="req1", usage=usage(i=7, o=1))])
        self.assertEqual(worker_tokens(r)["input"], 7)


class CumulativeTest(unittest.TestCase):
    def test_resume_result_counts_the_delta_not_the_total(self):
        r = report([
            event(cumulative=True, session_id="s1", attempt=1, usage=usage(i=100, o=50)),
            event(cumulative=True, session_id="s1", attempt=2, usage=usage(i=180, o=90)),
        ])
        self.assertEqual(worker_tokens(r)["input"], 180)
        self.assertEqual(worker_tokens(r)["output"], 90)
        self.assertEqual(r["scopes"]["worker"]["attempts"], 2)

    def test_repeated_cumulative_result_adds_nothing(self):
        r = report([
            event(cumulative=True, session_id="s1", usage=usage(i=100, o=50)),
            event(cumulative=True, session_id="s1", usage=usage(i=100, o=50)),
        ])
        self.assertEqual(worker_tokens(r)["input"], 100)

    def test_two_sessions_do_not_share_a_baseline(self):
        r = report([
            event(cumulative=True, session_id="s1", usage=usage(i=100, o=0)),
            event(cumulative=True, session_id="s2", usage=usage(i=30, o=0)),
        ])
        self.assertEqual(worker_tokens(r)["input"], 130)

    def test_total_that_shrinks_marks_the_scope_incomplete(self):
        r = report([
            event(cumulative=True, session_id="s1", usage=usage(i=100, o=50)),
            event(cumulative=True, session_id="s1", usage=usage(i=60, o=60)),
        ])
        self.assertIsNone(r["scopes"]["worker"]["tokens"]["value"])
        self.assertIn("cumulative_decreased", r["scopes"]["worker"]["tokens"]["flags"])
        self.assertIsNone(r["scopes"]["worker"]["cost_usd"]["value"])

    def test_cumulative_event_without_session_is_rejected(self):
        with self.assertRaises(ValueError):
            report([event(cumulative=True, usage=usage(i=1, o=1))])


class ParentInclusiveTest(unittest.TestCase):
    def test_parent_total_that_includes_subagents_does_not_double_count(self):
        r = report([
            event(message_id="p", parent_inclusive=True, usage=usage(i=1000, o=200)),
            event(message_id="c1", agent="subagent", parent_message_id="p", usage=usage(i=300, o=50)),
            event(message_id="c2", agent="subagent", parent_message_id="p", usage=usage(i=200, o=30)),
        ])
        self.assertEqual(worker_tokens(r)["input"], 1000)
        self.assertEqual(worker_tokens(r)["output"], 200)
        attribution = r["scopes"]["worker"]["attribution"]
        self.assertEqual(attribution["main"]["tokens"]["value"]["input"], 500)
        self.assertEqual(attribution["subagent"]["tokens"]["value"]["input"], 500)
        self.assertEqual(attribution["main"]["tokens"]["value"]["output"], 120)

    def test_subagent_with_missing_usage_makes_the_parent_unresolved(self):
        r = report([
            event(message_id="p", parent_inclusive=True, usage=usage(i=1000, o=200)),
            event(message_id="c1", agent="subagent", parent_message_id="p"),
        ])
        flags = r["scopes"]["worker"]["tokens"]["flags"]
        self.assertIn("parent_inclusive_unresolved", flags)
        self.assertIsNone(r["scopes"]["worker"]["cost_usd"]["value"])

    def test_children_larger_than_parent_is_inconsistent(self):
        r = report([
            event(message_id="p", parent_inclusive=True, usage=usage(i=100, o=10)),
            event(message_id="c1", agent="subagent", parent_message_id="p", usage=usage(i=300, o=5)),
        ])
        self.assertIn("parent_inclusive_inconsistent", r["scopes"]["worker"]["tokens"]["flags"])
        self.assertFalse(r["complete"])


class AttributionTest(unittest.TestCase):
    def test_retries_and_auxiliary_work_are_counted_and_attributed(self):
        r = report([
            event(message_id="a", usage=usage(i=100, o=10)),
            event(message_id="b", retry=True, usage=usage(i=100, o=10)),
            event(message_id="c", agent="auxiliary", model="n", usage=usage(i=50, o=5)),
            event(message_id="d", agent="subagent", usage=usage(i=20, o=2)),
        ])
        scope = r["scopes"]["worker"]
        self.assertEqual(scope["tokens"]["value"]["input"], 270)
        self.assertEqual(scope["retries"]["events"], 1)
        self.assertEqual(scope["retries"]["tokens"]["value"]["input"], 100)
        self.assertEqual(scope["retries"]["cost_usd"]["value"], 0.0002)
        self.assertEqual(scope["attribution"]["auxiliary"]["tokens"]["value"]["input"], 50)
        self.assertEqual(scope["attribution"]["auxiliary"]["cost_usd"]["value"], 0.0002)
        self.assertEqual(scope["attribution"]["subagent"]["events"], 1)
        self.assertEqual(scope["attribution"]["main"]["events"], 2)

    def test_scopes_are_separated_and_sum_into_the_experiment_total(self):
        r = report([
            event(usage=usage(i=1_000_000, o=0)),
            event(scope="grader", trial="g1", usage=usage(i=2_000_000, o=0)),
            event(scope="other", trial="o1", usage=usage(i=3_000_000, o=0)),
        ])
        self.assertEqual(r["scopes"]["worker"]["cost_usd"]["value"], 1.0)
        self.assertEqual(r["scopes"]["grader"]["cost_usd"]["value"], 2.0)
        self.assertEqual(r["scopes"]["other"]["cost_usd"]["value"], 3.0)
        self.assertEqual(r["experiment_total"]["cost_usd"]["value"], 6.0)
        self.assertEqual(r["worker_cost_per_successful_card"]["worker_cost_usd"]["value"], 1.0)


class CacheWriteTtlTest(unittest.TestCase):
    def test_known_ttl_split_is_priced(self):
        r = report([event(usage=usage(five=1_000_000, one=1_000_000))])
        tokens = worker_tokens(r)
        self.assertEqual((tokens["cache_write_5m"], tokens["cache_write_1h"], tokens["cache_write_unknown_ttl"]), (1_000_000, 1_000_000, 0))
        self.assertEqual(r["scopes"]["worker"]["cost_usd"]["value"], 3.25)

    def test_cache_write_without_a_breakdown_has_unknown_ttl_and_unknown_cost(self):
        r = report([event(usage=usage(i=0, o=0, written=1_000_000))])
        cost = r["scopes"]["worker"]["cost_usd"]
        self.assertEqual(worker_tokens(r)["cache_write_unknown_ttl"], 1_000_000)
        self.assertEqual(worker_tokens(r)["cache_write_5m"], 0)
        self.assertIsNone(cost["value"])
        self.assertFalse(cost["complete"])
        self.assertIn("cache_write_unknown_ttl", cost["flags"])
        self.assertEqual(cost["known_lower_bound"], 0.0)
        self.assertFalse(r["complete"])

    def test_mixed_known_and_unknown_ttl_keeps_both_and_prices_the_known_part_as_a_bound(self):
        r = report([
            event(message_id="a", usage=usage(five=1_000_000, one=0)),
            event(message_id="b", usage=usage(written=500_000)),
        ])
        tokens = worker_tokens(r)
        self.assertEqual((tokens["cache_write_5m"], tokens["cache_write_unknown_ttl"]), (1_000_000, 500_000))
        cost = r["scopes"]["worker"]["cost_usd"]
        self.assertIsNone(cost["value"])
        self.assertEqual(cost["known_lower_bound"], 1.25)

    def test_breakdown_smaller_than_total_leaves_the_remainder_unknown(self):
        r = report([event(usage=usage(five=100, one=0, written=300))])
        self.assertEqual(worker_tokens(r)["cache_write_unknown_ttl"], 200)


class UnpricedAndMissingTest(unittest.TestCase):
    def test_unpriced_model_is_unknown_not_zero_and_priced_models_keep_their_cost(self):
        r = report([
            event(message_id="a", usage=usage(i=1_000_000, o=0)),
            event(message_id="b", model="mystery", usage=usage(i=1_000_000, o=0)),
        ])
        cost = r["scopes"]["worker"]["cost_usd"]
        self.assertIsNone(cost["value"])
        self.assertIn("unpriced_model:mystery", cost["flags"])
        self.assertEqual(cost["known_lower_bound"], 1.0)
        rows = {row["model"]: row for row in r["scopes"]["worker"]["models"]}
        self.assertEqual(rows["m"]["cost_usd"]["value"], 1.0)
        self.assertIsNone(rows["mystery"]["cost_usd"]["value"])
        self.assertEqual(rows["mystery"]["tokens"]["value"]["input"], 1_000_000)

    def test_event_without_model_is_unpriced(self):
        r = report([event(model=None, usage=usage(i=5, o=5))])
        self.assertIn("unpriced_model:unknown", r["scopes"]["worker"]["cost_usd"]["flags"])

    def test_interrupted_run_with_missing_usage_is_unknown_not_zero(self):
        r = report([
            event(message_id="a", usage=usage(i=100, o=10)),
            event(message_id="b", interrupted=True),
        ])
        scope = r["scopes"]["worker"]
        self.assertIsNone(scope["tokens"]["value"])
        self.assertEqual(scope["tokens"]["known_lower_bound"]["input"], 100)
        self.assertIsNone(scope["cost_usd"]["value"])
        self.assertEqual(scope["cost_usd"]["flags"], ["interrupted", "missing_usage"])
        self.assertFalse(r["complete"])

    def test_usage_without_output_tokens_is_missing(self):
        r = report([event(usage={"input_tokens": 5})])
        self.assertIsNone(r["scopes"]["worker"]["cost_usd"]["value"])

    def test_interrupted_event_with_partial_usage_is_incomplete(self):
        r = report([event(interrupted=True, usage=usage(i=100, o=3))])
        self.assertIsNone(r["scopes"]["worker"]["cost_usd"]["value"])
        self.assertEqual(r["scopes"]["worker"]["cost_usd"]["known_lower_bound"], 0.00013)

    def test_no_events_at_all_is_unknown(self):
        r = report([], cards={})
        self.assertIsNone(r["experiment_total"]["cost_usd"]["value"])
        self.assertIn("no_events", r["flags"])
        with self.assertRaises(CostUnknown):
            require_known_cost(r, "experiment_total.cost_usd")

    def test_trial_without_any_events_makes_worker_cost_unknown(self):
        cards = {"c1": {"success": True, "trials": ["t1", "t2"]}}
        r = report([event(usage=usage(i=1, o=1))], cards=cards)
        self.assertIsNone(r["scopes"]["worker"]["cost_usd"]["value"])
        self.assertIn("trial_without_events:t2", r["flags"])
        self.assertEqual(r["worker_cost_per_successful_card"]["status"], "unknown")


class CardCostTest(unittest.TestCase):
    def test_worker_cost_per_successful_card_includes_failed_attempts_and_failed_cards(self):
        cards = {
            "win": {"success": True, "trials": ["w1", "w2"]},
            "lose": {"success": False, "trials": ["l1"]},
            "win2": {"success": True, "trials": ["x1"]},
        }
        events = [
            event(trial="w1", attempt=1, usage=usage(i=1_000_000, o=0)),
            event(trial="w2", attempt=2, usage=usage(i=1_000_000, o=0)),
            event(trial="l1", usage=usage(i=2_000_000, o=0)),
            event(trial="x1", usage=usage(i=2_000_000, o=0)),
            event(scope="grader", trial="g", usage=usage(i=9_000_000, o=0)),
        ]
        r = report(events, cards=cards)
        per = r["worker_cost_per_successful_card"]
        self.assertEqual(per["status"], "known")
        self.assertEqual(per["worker_cost_usd"]["value"], 6.0)
        self.assertEqual(per["successful_cards"], 2)
        self.assertEqual(per["value"], 3.0)
        self.assertEqual(r["experiment_total"]["cost_usd"]["value"], 15.0)
        self.assertEqual(r["cards"]["win"]["cost_usd"]["value"], 2.0)
        self.assertEqual(r["cards"]["win"]["attempts"], 2)
        self.assertEqual(require_known_cost(r, "worker_cost_per_successful_card"), 3.0)

    def test_zero_successful_cards_reports_no_success_never_zero(self):
        cards = {"c1": {"success": False, "trials": ["t1"]}}
        r = report([event(usage=usage(i=1_000_000, o=0))], cards=cards)
        per = r["worker_cost_per_successful_card"]
        self.assertEqual(per["status"], "no_success")
        self.assertIsNone(per["value"])
        self.assertEqual(per["flags"], ["no_success"])
        self.assertEqual(per["worker_cost_usd"]["value"], 1.0)
        with self.assertRaises(CostUnknown):
            require_known_cost(r, "worker_cost_per_successful_card")

    def test_unknown_worker_cost_gives_unknown_status(self):
        r = report([event(model="mystery", usage=usage(i=1, o=1))])
        per = r["worker_cost_per_successful_card"]
        self.assertEqual(per["status"], "unknown")
        self.assertIsNone(per["value"])

    def test_worker_event_with_no_card_is_flagged(self):
        r = report([event(trial="orphan", usage=usage(i=1, o=1))])
        self.assertIn("worker_event_without_card:orphan", r["flags"])
        self.assertFalse(r["complete"])


class RequireKnownCostTest(unittest.TestCase):
    def test_returns_the_value_when_known_and_complete(self):
        r = report([event(usage=usage(i=1_000_000, o=0))])
        self.assertEqual(require_known_cost(r, "experiment_total.cost_usd"), 1.0)
        self.assertEqual(require_known_cost(r, "scopes.worker.models.0.cost_usd"), 1.0)
        self.assertTrue(r["complete"])

    def test_raises_for_unknown_cost_and_names_the_flag(self):
        r = report([event(model="mystery", usage=usage(i=1, o=1))])
        with self.assertRaisesRegex(CostUnknown, "unpriced_model:mystery"):
            require_known_cost(r, "scopes.worker.cost_usd")

    def test_raises_for_missing_accounting_instead_of_returning_zero(self):
        r = report([event(usage=usage(i=1, o=1))])
        for path in ("scopes.nope.cost_usd", "experiment_total.missing", "scopes.worker.models.9.cost_usd", "complete"):
            with self.subTest(path=path), self.assertRaises(CostUnknown):
                require_known_cost(r, path)

    def test_invalid_event_is_rejected_at_the_boundary(self):
        with self.assertRaises(ValueError):
            report([event(scope="judge", usage=usage(i=1, o=1))])


class PinnedPricesTest(unittest.TestCase):
    def test_default_prices_are_the_pinned_token_spend_table(self):
        usage_record = usage(i=1_000_000, o=1_000_000, read=1_000_000, five=1_000_000, one=1_000_000)
        r = build_cost_report([event(model="claude-sonnet-5-5", usage=usage_record)], None, CARDS)
        self.assertEqual(r["scopes"]["worker"]["cost_usd"]["value"], 2 + 10 + 0.2 + 2.5 + 4)
        self.assertIn("claude-sonnet-5-5", load_prices())


if __name__ == "__main__":
    unittest.main()
