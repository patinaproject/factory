from __future__ import annotations

import json
import unittest

from tests import load_plugin
from tests.relations_fake import STATES, RelationsLinear
from tests.test_linear import make_client

plugin = load_plugin()
tools = plugin.tools

ISSUES = {"ABC-1": {}, "ABC-2": {}, "ABC-3": {}, "ABC-4": {}}


def relate(fake, **args):
    return json.loads(tools.run_relate(lambda: make_client(fake), {"issue": "ABC-1", **args}))


class DuplicateTest(unittest.TestCase):
    def test_a_duplicate_is_created_and_the_issue_moves_to_the_duplicate_status(self):
        fake = RelationsLinear(ISSUES)
        output = relate(fake, duplicate_of="ABC-2")
        self.assertEqual(
            output,
            {"identifier": "ABC-1", "related_issue": "ABC-2", "relation": "duplicate", "created": True,
             "removed_related": False, "state": "Duplicate"},
        )
        self.assertEqual(fake.stored(), [("duplicate", "ABC-1", "ABC-2")])
        self.assertEqual(
            fake.mutations,
            [
                ("create", {"issueId": "uuid-ABC-1", "relatedIssueId": "uuid-ABC-2", "type": "duplicate"}),
                ("update", "uuid-ABC-1", {"stateId": "state-Duplicate"}),
            ],
        )
        self.assertEqual(fake.state_of("ABC-1"), "Duplicate")

    def test_a_linear_that_moves_the_issue_itself_gets_no_status_update(self):
        fake = RelationsLinear(ISSUES, create_moves_to_duplicate=True)
        output = relate(fake, duplicate_of="ABC-2")
        self.assertEqual(output["state"], "Duplicate")
        self.assertEqual([m[0] for m in fake.mutations], ["create"])

    def test_a_related_relation_is_deleted_when_linear_keeps_it(self):
        fake = RelationsLinear(ISSUES, relations=[("related", "ABC-2", "ABC-1")])
        output = relate(fake, duplicate_of="ABC-2")
        self.assertEqual((output["created"], output["removed_related"]), (True, True))
        self.assertEqual(fake.stored(), [("duplicate", "ABC-1", "ABC-2")])
        self.assertIn(("delete", "rel-0"), fake.mutations)

    def test_a_related_relation_that_linear_removes_is_not_deleted_again(self):
        fake = RelationsLinear(ISSUES, relations=[("related", "ABC-1", "ABC-2")], create_removes_related=True)
        output = relate(fake, duplicate_of="ABC-2")
        self.assertEqual((output["created"], output["removed_related"]), (True, False))
        self.assertEqual(fake.stored(), [("duplicate", "ABC-1", "ABC-2")])
        self.assertNotIn("delete", [m[0] for m in fake.mutations])

    def test_every_other_relation_on_both_issues_survives(self):
        others = [
            ("related", "ABC-1", "ABC-3"),
            ("related", "ABC-4", "ABC-1"),
            ("blocks", "ABC-3", "ABC-1"),
            ("blocks", "ABC-2", "ABC-4"),
            ("related", "ABC-2", "ABC-3"),
            ("duplicate", "ABC-4", "ABC-2"),
        ]
        fake = RelationsLinear(ISSUES, relations=[("related", "ABC-1", "ABC-2"), *others])
        relate(fake, duplicate_of="ABC-2")
        self.assertEqual(fake.stored(), [*others, ("duplicate", "ABC-1", "ABC-2")])

    def test_an_existing_duplicate_creates_no_second_one_and_still_converges(self):
        fake = RelationsLinear(ISSUES, relations=[("duplicate", "ABC-1", "ABC-2"), ("related", "ABC-2", "ABC-1")])
        output = relate(fake, duplicate_of="ABC-2")
        self.assertEqual(
            output,
            {"identifier": "ABC-1", "related_issue": "ABC-2", "relation": "duplicate", "created": False,
             "removed_related": True, "state": "Duplicate"},
        )
        self.assertEqual(fake.stored(), [("duplicate", "ABC-1", "ABC-2")])
        self.assertNotIn("create", [m[0] for m in fake.mutations])

    def test_an_issue_already_in_the_duplicate_status_is_not_updated(self):
        fake = RelationsLinear({**ISSUES, "ABC-1": {"state": "Duplicate"}})
        output = relate(fake, duplicate_of="ABC-2")
        self.assertEqual(output["state"], "Duplicate")
        self.assertEqual([m[0] for m in fake.mutations], ["create"])

    def test_a_team_without_a_duplicate_status_fails_and_says_the_relation_exists(self):
        fake = RelationsLinear(ISSUES, states=[s for s in STATES if s["type"] != "duplicate"])
        self.assertEqual(
            relate(fake, duplicate_of="ABC-2"),
            {"error": "the duplicate relation exists, but ABC-1's team has no status of type duplicate, so its status did not change"},
        )
        self.assertEqual(fake.stored(), [("duplicate", "ABC-1", "ABC-2")])

    def test_a_team_with_two_duplicate_statuses_fails_and_names_them(self):
        fake = RelationsLinear(ISSUES, states=[*STATES, {"id": "state-Dupe", "name": "Dupe", "type": "duplicate"}])
        self.assertEqual(
            relate(fake, duplicate_of="ABC-2"),
            {"error": "the duplicate relation exists, but ABC-1's team has more than one status of type duplicate: Dupe, Duplicate, so its status did not change"},
        )
        self.assertEqual(fake.state_of("ABC-1"), "Todo")


class BlockedByTest(unittest.TestCase):
    def test_blocking_a_on_b_makes_b_one_of_as_blockers(self):
        fake = RelationsLinear(ISSUES)
        output = relate(fake, blocked_by="ABC-2")
        self.assertEqual(
            output,
            {"identifier": "ABC-1", "related_issue": "ABC-2", "relation": "blocks", "created": True,
             "removed_related": False, "state": "Todo"},
        )
        self.assertEqual(fake.stored(), [("blocks", "ABC-2", "ABC-1")])
        self.assertEqual(
            fake.mutations, [("create", {"issueId": "uuid-ABC-2", "relatedIssueId": "uuid-ABC-1", "type": "blocks"})]
        )
        blockers = make_client(fake).blockers("ABC-1")
        self.assertEqual([b["identifier"] for b in blockers], ["ABC-2"])

    def test_an_existing_block_creates_no_second_one(self):
        fake = RelationsLinear(ISSUES, relations=[("blocks", "ABC-2", "ABC-1")])
        output = relate(fake, blocked_by="ABC-2")
        self.assertEqual((output["created"], fake.mutations), (False, []))

    def test_a_block_leaves_a_related_relation_and_the_status_alone(self):
        fake = RelationsLinear(ISSUES, relations=[("related", "ABC-1", "ABC-2")])
        relate(fake, blocked_by="ABC-2")
        self.assertEqual(fake.stored(), [("related", "ABC-1", "ABC-2"), ("blocks", "ABC-2", "ABC-1")])
        self.assertEqual(fake.state_of("ABC-1"), "Todo")


class RefusalTest(unittest.TestCase):
    def refused(self, relations, message, **args):
        fake = RelationsLinear(ISSUES, relations=relations)
        self.assertEqual(relate(fake, **args), {"error": message})
        self.assertEqual(fake.mutations, [])

    def test_a_duplicate_of_a_different_issue_is_a_conflict(self):
        self.refused(
            [("duplicate", "ABC-1", "ABC-3")],
            "ABC-1 is already a duplicate of ABC-3 (relation rel-0), so nothing was changed",
            duplicate_of="ABC-2",
        )

    def test_an_original_that_is_already_a_duplicate_of_the_issue_is_a_conflict(self):
        self.refused(
            [("duplicate", "ABC-2", "ABC-1")],
            "ABC-2 is already a duplicate of ABC-1 (relation rel-0), so nothing was changed",
            duplicate_of="ABC-2",
        )

    def test_a_blocker_that_is_already_blocked_on_the_issue_is_a_conflict(self):
        self.refused(
            [("blocks", "ABC-1", "ABC-2")],
            "ABC-2 is already blocked on ABC-1 (relation rel-0), so nothing was changed",
            blocked_by="ABC-2",
        )

    def test_an_issue_cannot_relate_to_itself(self):
        for args in ({"duplicate_of": "ABC-1"}, {"blocked_by": "uuid-ABC-1"}):
            with self.subTest(args):
                self.refused([], "ABC-1 cannot be related to itself", **args)

    def test_exactly_50_relations_still_take_a_duplicate(self):
        fake = RelationsLinear(ISSUES, relations=[("related", "ABC-1", "ABC-3")] * 49 + [("related", "ABC-1", "ABC-2")])
        output = relate(fake, duplicate_of="ABC-2")
        self.assertEqual((output["created"], output["removed_related"], output["state"]), (True, True, "Duplicate"))
        self.assertEqual(len(fake.stored()), 50)

    def test_51_relations_change_nothing(self):
        fake = RelationsLinear(ISSUES, relations=[("related", "ABC-1", "ABC-3")] * 51)
        self.assertEqual(
            relate(fake, duplicate_of="ABC-2"),
            {"error": "ABC-1 has more than 50 relations, more than the agent reads, so nothing was changed"},
        )
        self.assertEqual(fake.mutations, [])

    def test_a_missing_issue_is_named(self):
        fake = RelationsLinear(ISSUES)
        self.assertEqual(
            relate(fake, blocked_by="ABC-9"), {"error": "Linear has no issue ABC-9 that the agent app can read"}
        )
        self.assertEqual(fake.mutations, [])

    def test_assignee_and_delegate_arguments_are_refused_before_any_request(self):
        for name in ("assignee", "delegate", "delegate_id"):
            with self.subTest(name):
                fake = RelationsLinear(ISSUES)
                self.assertEqual(
                    relate(fake, duplicate_of="ABC-2", **{name: "someone"}),
                    {"error": f"this tool never changes assignee or delegate; drop {name}"},
                )
                self.assertEqual((fake.graphql_requests, fake.token_requests), ([], []))

    def test_exactly_one_relation_argument_is_required_before_any_request(self):
        for args in ({}, {"duplicate_of": "ABC-2", "blocked_by": "ABC-3"}):
            with self.subTest(args):
                fake = RelationsLinear(ISSUES)
                self.assertEqual(relate(fake, **args), {"error": "pass exactly one of duplicate_of or blocked_by"})
                self.assertEqual(fake.graphql_requests, [])


if __name__ == "__main__":
    unittest.main()
