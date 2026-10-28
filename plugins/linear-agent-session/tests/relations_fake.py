"""A stateful Linear for relation tests: issues, team statuses, and a relation store that every query reads.

It is not `staged_linear.py`, which patches `urllib.request.urlopen` at import for subprocess tests.
This one is a urlopen like `FakeLinear`, so tests use it through `make_client`.
"""
from __future__ import annotations

import json

from . import FakeLinear, FakeResponse

QUERY_MARKERS = ("IssueRelations", "IssueRelationCreate", "IssueRelationDelete", "IssueUpdate", "IssueBlockers")

DUPLICATE = {"id": "state-Duplicate", "name": "Duplicate", "type": "duplicate"}
STATES = [
    {"id": "state-Todo", "name": "Todo", "type": "unstarted"},
    {"id": "state-Done", "name": "Done", "type": "completed"},
    DUPLICATE,
]


class RelationsLinear(FakeLinear):
    def __init__(
        self,
        issues,
        relations=(),
        states=STATES,
        create_moves_to_duplicate=False,
        create_removes_related=False,
        page_size=50,
    ) -> None:
        super().__init__()
        self.issues = {i: {"id": f"uuid-{i}", "identifier": i, "state": "Todo", **extra} for i, extra in dict(issues).items()}
        self.states = [dict(s) for s in states]
        self.relations = [
            {"id": f"rel-{n}", "type": t, "issue": f"uuid-{a}", "relatedIssue": f"uuid-{b}"}
            for n, (t, a, b) in enumerate(relations)
        ]
        self.create_moves_to_duplicate = create_moves_to_duplicate
        self.create_removes_related = create_removes_related
        self.page_size = page_size
        self.mutations = []

    def stored(self):
        """Every relation as (type, issue identifier, related identifier), in creation order."""
        by_uuid = {i["id"]: i["identifier"] for i in self.issues.values()}
        return [(r["type"], by_uuid[r["issue"]], by_uuid[r["relatedIssue"]]) for r in self.relations]

    def state_of(self, identifier):
        return self.issues[identifier]["state"]

    def __call__(self, request, timeout=None):
        if request.full_url.endswith("/oauth/token"):
            return super().__call__(request, timeout)
        body = json.loads(request.data.decode())
        self.graphql_requests.append(body)
        query, variables = body["query"], body["variables"]
        marker = next(m for m in QUERY_MARKERS if f"{m}(" in query)
        return FakeResponse(200, json.dumps({"data": getattr(self, f"_{marker}")(variables)}).encode())

    def _find(self, key):
        return next((i for i in self.issues.values() if key in (i["id"], i["identifier"])), None)

    def _node(self, relation):
        by_uuid = {i["id"]: i for i in self.issues.values()}
        end = lambda uuid: {"id": uuid, "identifier": by_uuid[uuid]["identifier"]}  # noqa: E731
        return {"id": relation["id"], "type": relation["type"], "issue": end(relation["issue"]), "relatedIssue": end(relation["relatedIssue"])}

    def _page(self, relations):
        return {
            "pageInfo": {"hasNextPage": len(relations) > self.page_size},
            "nodes": [self._node(r) for r in relations[: self.page_size]],
        }

    def _issue(self, key):
        issue = self._find(key)
        if issue is None:
            return None
        state = next(s for s in self.states if s["name"] == issue["state"])
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "state": {"name": state["name"], "type": state["type"]},
            "team": {"states": {"nodes": self.states}},
            "relations": self._page([r for r in self.relations if r["issue"] == issue["id"]]),
            "inverseRelations": self._page([r for r in self.relations if r["relatedIssue"] == issue["id"]]),
        }

    def _IssueRelations(self, variables):
        return {"a": self._issue(variables["a"]), "b": self._issue(variables["b"])}

    def _IssueRelationCreate(self, variables):
        spec = variables["input"]
        self.mutations.append(("create", spec))
        self.relations.append(
            {"id": f"rel-new-{len(self.relations)}", "type": spec["type"], "issue": spec["issueId"], "relatedIssue": spec["relatedIssueId"]}
        )
        if spec["type"] == "duplicate":
            if self.create_moves_to_duplicate:
                self._find(spec["issueId"])["state"] = DUPLICATE["name"]
            if self.create_removes_related:
                ends = {spec["issueId"], spec["relatedIssueId"]}
                self.relations = [r for r in self.relations if not (r["type"] == "related" and {r["issue"], r["relatedIssue"]} == ends)]
        return {"issueRelationCreate": {"success": True}}

    def _IssueRelationDelete(self, variables):
        self.mutations.append(("delete", variables["id"]))
        self.relations = [r for r in self.relations if r["id"] != variables["id"]]
        return {"issueRelationDelete": {"success": True}}

    def _IssueUpdate(self, variables):
        self.mutations.append(("update", variables["id"], variables["input"]))
        state = next(s for s in self.states if s["id"] == variables["input"]["stateId"])
        self._find(variables["id"])["state"] = state["name"]
        return {"issueUpdate": {"success": True}}

    def _IssueBlockers(self, variables):
        issue = self._find(variables["id"])
        return {
            "viewer": {"id": "app-user"},
            "issue": {
                "inverseRelations": {
                    "nodes": [
                        {
                            "type": r["type"],
                            "issue": {
                                "id": r["issue"],
                                "identifier": self._node(r)["issue"]["identifier"],
                                "branchName": "branch",
                                "state": {"type": "started"},
                                "delegate": None,
                            },
                        }
                        for r in self.relations
                        if r["relatedIssue"] == issue["id"]
                    ]
                }
            },
        }
