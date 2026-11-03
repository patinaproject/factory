"""The read-only Sentry REST reader for sentry-linear-sync.

Every read returns all of its pages or raises SentryError: a missing page must never read as "no events".
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Mapping, Optional
from urllib.parse import quote, urlencode

import sentry_sync
from sentry_sync import Observation, ReleaseCounts, Rules, SentryIssue

API_BASE = "https://sentry.io"
ISSUE_QUERY = "is:unresolved issue.category:error"
PAGE = 100
NEXT_PAGE = re.compile(r'<([^>]+)>;\s*rel="next";\s*results="true"')


class SentryError(Exception):
    """A Sentry read failed, so the run must write nothing."""


class SentryReader:
    def __init__(
        self, organization: str, token: str, *, base_url: str = API_BASE,
        urlopen: Callable = urllib.request.urlopen, timeout: float = 30.0,
    ) -> None:
        self._org = organization
        # The REST API takes Bearer whatever scheme the MCP server's settings name for the same token.
        self._authorization = f"Bearer {token}"
        self._base = base_url.rstrip("/")
        self._urlopen = urlopen
        self._timeout = timeout

    def _get(self, url: str, *, missing_ok: bool = False):
        request = urllib.request.Request(url, headers={"Authorization": self._authorization})
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read()), response.headers.get("Link") or ""
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None, ""
            raise SentryError(f"Sentry returned HTTP {error.code} for {url.split('?')[0]}") from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise SentryError(f"Sentry read failed for {url.split('?')[0]}: {error}") from None

    def _pages(self, path: str, params: list, *, until: Optional[Callable] = None) -> list:
        """Every row, or the rows up to the first page whose last row meets `until`."""
        url = f"{self._base}/api/0/organizations/{self._org}/{path}?{urlencode(params)}"
        rows: list = []
        while url:
            body, link = self._get(url)
            page = body["data"] if isinstance(body, dict) else body
            if not isinstance(page, list):
                raise SentryError(f"Sentry returned no list for {path}")
            rows.extend(page)
            found = NEXT_PAGE.search(link)
            url = found[1] if found and not (until and page and until(page[-1])) else ""
        return rows

    def projects(self) -> dict:
        return {p["slug"]: str(p["id"]) for p in self._pages("projects/", [("per_page", PAGE)])}

    def issues(self, rules: Rules, index: Mapping) -> tuple:
        rows = self._pages("issues/", [
            ("query", ISSUE_QUERY), ("environment", sentry_sync.ENVIRONMENT),
            ("statsPeriod", f"{rules.window_hours}h"), ("limit", PAGE),
        ])
        return tuple(self._parse(row, rules, index) for row in rows)

    def issue(self, sentry_id: str, rules: Rules, index: Mapping) -> Optional[SentryIssue]:
        row, _ = self._get(f"{self._base}/api/0/organizations/{self._org}/issues/{quote(sentry_id)}/", missing_ok=True)
        if row is None or row["project"]["slug"] not in rules.projects:
            return None
        return self._parse(row, rules, index)

    def _parse(self, row: dict, rules: Rules, index: Mapping) -> SentryIssue:
        metadata = row.get("metadata") or {}
        exception_type = metadata.get("type")
        frame = _frame(metadata.get("filename"), metadata.get("function"), index)
        if frame is None and exception_type:
            frame = self._latest_frame(str(row["id"]), index)
        return SentryIssue(
            id=str(row["id"]), short_id=row["shortId"], package=rules.projects[row["project"]["slug"]],
            exception_type=exception_type, raw_message=metadata.get("value") or row.get("title") or "",
            frame=frame, unhandled=bool(row.get("isUnhandled")),
            first_seen=sentry_sync.parse_time(row["firstSeen"]), users=int(row.get("userCount") or 0),
            events=int(row.get("count") or 0),
        )

    def _latest_frame(self, sentry_id: str, index: Mapping):
        """The innermost in-repo frame of the latest event, for an issue whose metadata names no repository file,
        such as an app bundle that Sentry did not source-map."""
        event, _ = self._get(f"{self._base}/api/0/organizations/{self._org}/issues/{sentry_id}/events/latest/", missing_ok=True)
        for entry in (event or {}).get("entries", []):
            if entry.get("type") != "exception":
                continue
            for value in reversed(entry["data"].get("values") or []):
                for frame in reversed((value.get("stacktrace") or {}).get("frames") or []):
                    found = _frame(frame.get("filename") or frame.get("absPath"), frame.get("function"), index)
                    if found:
                        return found
        return None

    def events_by_issue(self, sentry_ids, days: int) -> dict:
        """Events in the last `days` for each issue, zero for an issue with none."""
        counts = {sentry_id: 0 for sentry_id in sentry_ids}
        ids = sorted(counts)
        for start in range(0, len(ids), PAGE):
            for row in self._pages("events/", [
                ("field", "issue.id"), ("field", "count()"), ("query", f"issue.id:[{','.join(ids[start:start + PAGE])}]"),
                ("statsPeriod", f"{days}d"), ("dataset", "errors"), ("environment", sentry_sync.ENVIRONMENT),
                ("per_page", PAGE),
            ]):
                counts[str(row["issue.id"])] = int(row["count()"])
        return counts

    def release_counts(self, sentry_ids, project_ids, since: datetime, now: datetime) -> ReleaseCounts:
        """Events of the issues since `since` by release, and each release of the projects created after `since`."""
        created: dict = {}
        for project_id in project_ids:
            # Sentry lists releases newest first, so paging stops at the first page that reaches `since`.
            releases = self._pages(
                "releases/", [("project", project_id), ("sort", "date"), ("per_page", PAGE)],
                until=lambda release: sentry_sync.parse_time(release["dateCreated"]) <= since,
            )
            for release in releases:
                when = sentry_sync.parse_time(release["dateCreated"])
                if when > since:
                    created[release["version"]] = when
        by_release: dict = {}
        ids = sorted(sentry_ids)
        for start in range(0, len(ids), PAGE):
            for row in self._pages("events/", [
                ("field", "release"), ("field", "count()"), ("query", f"issue.id:[{','.join(ids[start:start + PAGE])}]"),
                ("start", since.isoformat()), ("end", now.isoformat()), ("dataset", "errors"),
                ("environment", sentry_sync.ENVIRONMENT), ("per_page", PAGE),
            ]):
                if row.get("release"):
                    by_release[row["release"]] = by_release.get(row["release"], 0) + int(row["count()"])
        return ReleaseCounts(by_release, created)


def _frame(filename, function, index: Mapping):
    path = sentry_sync.resolve_repo_path(filename, index)
    name = sentry_sync.sanitize_identifier(function)
    return sentry_sync.InRepoFrame(path, name) if path and name and sentry_sync.sanitize_identifier(path) else None


def observe(reader: SentryReader, rules: Rules, read: sentry_sync.TrackedRead, index: Mapping, now: datetime) -> Observation:
    """Everything one run reads from Sentry. Raises SettingsError when Sentry's projects differ from the rules."""
    projects = reader.projects()
    if set(projects) != set(rules.projects):
        raise sentry_sync.SettingsError(
            f"Sentry returns projects {', '.join(sorted(projects)) or 'none'}, "
            f"but rules.projects maps {', '.join(sorted(rules.projects))}"
        )
    issues = reader.issues(rules, index)
    legacy = {i.sentry_id: reader.issue(i.sentry_id, rules, index) for i in read.legacy}
    stale = {m for issue in sentry_sync.stale_candidates(read) for m in issue.members}
    recent = reader.events_by_issue(stale, rules.stale_days) if stale else {}
    counts = {}
    for key, issue in sorted(sentry_sync.done_unverified(read).items()):
        package = key.split(":", 1)[0]
        project_ids = [projects[slug] for slug, mapped in sorted(rules.projects.items()) if mapped == package]
        counts[key] = reader.release_counts(issue.members, project_ids, issue.completed_at, now)
    return Observation(issues=issues, recent_events=recent, release_counts=counts, legacy_sources=legacy)
