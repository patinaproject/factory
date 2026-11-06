"""The dependabot-sweep policy: settings, alert parsing, priority, issue bodies, and `plan`.

Pure: nothing here reads the network, the clock, or a file. `dependabot-sweep` reads GitHub, CISA KEV, and Linear,
passes the snapshot to `plan`, and applies the actions it returns.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Optional, Union
from urllib.parse import quote, unquote

SETTINGS_SECTION = "dependabot_sweep"
NEEDS_TRIAGE_LABEL = "Needs triage"
STATE_FILE = "dependabot-sweep-state.json"
ORIGIN_PREFIX = "Dependabot package: "
FOOTER_PREFIX = "Dependabot sweep: "
COMMENT_MARKER = "<!-- dependabot-sweep:{kind} -->"
EPSS_RAISES_AT = 0.90
BODY_DETAIL_BUDGET = 30_000
BODY_HARD_LIMIT = 64_000
CLOSED_STATES = frozenset({"fixed", "dismissed", "auto_dismissed"})
ALERT_STATES = CLOSED_STATES | {"open"}
OPEN_LINEAR = frozenset({"triage", "backlog", "unstarted", "started"})
CANCELABLE_LINEAR = frozenset({"triage", "backlog"})

TEAM_KEY = re.compile(r"[A-Z][A-Z0-9]*")
FULL_NAME = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
ORIGIN_LINE = re.compile(r"Dependabot package: (\S+) (\S+) (\S+)")
FOOTER_LINE = re.compile(r"^Dependabot sweep: (.*)$", re.M)
COMMENT_MARKER_PATTERN = re.compile(r"<!-- dependabot-sweep:([a-z-]+) -->")


class SettingsError(Exception):
    """The settings are invalid. The command exits 2."""


class Priority(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    URGENT = 4

    @property
    def linear(self) -> int:
        return LINEAR_VALUE[self]

    @property
    def title(self) -> str:
        return self.name.capitalize()

    @classmethod
    def from_title(cls, title: str) -> "Priority":
        return cls[title.upper()]


LINEAR_VALUE = {Priority.URGENT: 1, Priority.HIGH: 2, Priority.MEDIUM: 3, Priority.LOW: 4}
SEVERITY_LEVEL = {"critical": Priority.URGENT, "high": Priority.HIGH, "medium": Priority.MEDIUM, "low": Priority.LOW}


class Buildability(Enum):
    PRIVATE_DEFAULT = "private-default"
    PUBLIC_SYNCED = "public-synced"
    UNBUILDABLE = "unbuildable"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------------------------

SETTINGS_FIELDS = (
    ("team_key", TEAM_KEY, "a Linear team key such as ABC"),
    ("label", re.compile(r".*\S.*"), "a non-empty label name"),
)


@dataclass(frozen=True)
class Settings:
    team_key: str
    label: str
    default_repository: str
    synced_repositories: frozenset
    app_login: str


def configured(factory_settings: object) -> bool:
    """Whether the factory settings ask for the sweep. An empty section does not."""
    return isinstance(factory_settings, dict) and factory_settings.get(SETTINGS_SECTION) not in (None, {})


def section_problems(factory_settings: object) -> list:
    """Every problem with the `dependabot_sweep` section's own fields. Empty when all are valid."""
    section = factory_settings.get(SETTINGS_SECTION) if isinstance(factory_settings, dict) else None
    if not isinstance(section, dict) or not section:
        return [f"{SETTINGS_SECTION} is missing from the factory settings; it must be an object"]
    problems = []
    for name, pattern, shape in SETTINGS_FIELDS:
        value = section.get(name)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            problems.append(f"{SETTINGS_SECTION}.{name} {value!r} must be {shape}")
    unknown = sorted(set(section) - {name for name, _, _ in SETTINGS_FIELDS})
    if unknown:
        problems.append(f"{SETTINGS_SECTION} has unknown fields: {', '.join(unknown)}")
    return problems


def settings_problems(factory_settings: object) -> list:
    """Every problem with the section and the other settings the sweep needs. Empty when all are valid."""
    problems = section_problems(factory_settings)
    if not isinstance(factory_settings, dict):
        return problems
    github = factory_settings.get("github") or {}
    login = github.get("login") if isinstance(github, dict) else None
    if not isinstance(login, str) or not login.endswith("[bot]"):
        problems.append(f"github.login {login!r} must be the App's <app-slug>[bot] login")
    if len(_repositories(factory_settings, "default")) != 1:
        problems.append("repositories must hold exactly one routing 'default' repository")
    return problems


def parse_settings(factory_settings: object) -> Settings:
    problems = settings_problems(factory_settings)
    if problems:
        raise SettingsError("; ".join(problems))
    section = factory_settings[SETTINGS_SECTION]
    return Settings(
        team_key=section["team_key"], label=section["label"],
        default_repository=_repositories(factory_settings, "default")[0],
        synced_repositories=frozenset(_repositories(factory_settings, "synced_github")),
        app_login=factory_settings["github"]["login"],
    )


def _repositories(factory_settings: dict, routing: str) -> list:
    repositories = factory_settings.get("repositories")
    if not isinstance(repositories, list):
        return []
    return [
        r["full_name"].lower() for r in repositories
        if isinstance(r, dict) and r.get("routing") == routing and isinstance(r.get("full_name"), str)
    ]


# ---------------------------------------------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Repo:
    full_name: str
    visibility: str
    buildability: Buildability
    reason: str

    @property
    def buildable(self) -> bool:
        return self.buildability in (Buildability.PRIVATE_DEFAULT, Buildability.PUBLIC_SYNCED)

    @property
    def tracker(self) -> str:
        return "linear" if self.buildability is Buildability.PRIVATE_DEFAULT else "github"


def classify_repo(full_name: str, visibility: str, settings: Settings) -> Repo:
    """The adapter's tracker rule: public repositories use GitHub Issues, private ones use Linear."""
    name = full_name.lower()
    if visibility == "private":
        if name == settings.default_repository:
            return Repo(full_name, visibility, Buildability.PRIVATE_DEFAULT, "")
        return Repo(full_name, visibility, Buildability.UNBUILDABLE, "private repository that is not the default repository")
    if visibility == "public":
        if name in settings.synced_repositories:
            return Repo(full_name, visibility, Buildability.PUBLIC_SYNCED, "")
        return Repo(full_name, visibility, Buildability.UNBUILDABLE, "public repository that is not a synced_github repository")
    return Repo(full_name, visibility, Buildability.UNKNOWN, f"visibility {visibility!r} is neither public nor private")


# ---------------------------------------------------------------------------------------------------------------
# Alerts and priority
# ---------------------------------------------------------------------------------------------------------------


class Key(tuple):
    """The repository (lowercase), the ecosystem, and the package."""

    def __new__(cls, repo: str, ecosystem: str, package: str) -> "Key":
        return super().__new__(cls, (repo.lower(), ecosystem, package))

    repo = property(lambda self: self[0])
    ecosystem = property(lambda self: self[1])
    package = property(lambda self: self[2])


@dataclass(frozen=True)
class Alert:
    number: int
    state: str
    ecosystem: str
    package: str
    severity: str
    ghsa: str
    cves: tuple
    summary: str
    references: tuple
    scope: Optional[str]
    relationship: str
    manifest_path: str
    vulnerable_range: str
    first_patched: Optional[str]
    epss: Optional[float]
    url: str

    @property
    def open(self) -> bool:
        return self.state == "open"


def parse_alert(raw: dict) -> Alert:
    """An Alert from one Dependabot alert object. Raises ValueError for a shape the sweep does not know."""
    try:
        advisory = raw["security_advisory"]
        vulnerability = raw["security_vulnerability"]
        dependency = raw["dependency"]
        severity = advisory["severity"]
        if severity not in SEVERITY_LEVEL:
            raise ValueError(f"severity {severity!r}")
        if raw["state"] not in ALERT_STATES:
            raise ValueError(f"state {raw['state']!r}")
        cves = {i["value"] for i in advisory.get("identifiers") or [] if i.get("type") == "CVE"}
        if advisory.get("cve_id"):
            cves.add(advisory["cve_id"])
        epss = advisory.get("epss")
        if isinstance(epss, list):
            epss = epss[0] if epss else None
        patched = vulnerability.get("first_patched_version")
        return Alert(
            number=int(raw["number"]), state=raw["state"], ecosystem=dependency["package"]["ecosystem"],
            package=dependency["package"]["name"], severity=severity, ghsa=advisory["ghsa_id"], cves=tuple(sorted(cves)),
            summary=advisory.get("summary") or "", references=tuple(r["url"] for r in advisory.get("references") or []),
            scope=dependency.get("scope"), relationship=dependency.get("relationship") or "unknown",
            manifest_path=dependency.get("manifest_path") or "", vulnerable_range=vulnerability.get("vulnerable_version_range") or "",
            first_patched=patched["identifier"] if patched else None,
            epss=float(epss["percentile"]) if epss and epss.get("percentile") is not None else None,
            url=raw["html_url"],
        )
    except (KeyError, TypeError, IndexError) as error:
        raise ValueError(f"unexpected Dependabot alert shape: {error!r}") from None


def in_kev(alert: Alert, kev: frozenset) -> bool:
    return any(cve in kev for cve in alert.cves)


def alert_priority(alert: Alert, kev: frozenset) -> Priority:
    runtime = alert.scope != "development"
    if runtime and in_kev(alert, kev):
        return Priority.URGENT
    level = SEVERITY_LEVEL[alert.severity]
    if alert.epss is not None and alert.epss >= EPSS_RAISES_AT:
        level = max(level, Priority.HIGH)
    if not runtime:
        level = Priority(max(level - 1, Priority.LOW))
    return level


def issue_priority(open_alerts: list, kev: frozenset) -> Priority:
    return max(alert_priority(a, kev) for a in open_alerts)


def fingerprint(alerts: list, priority: Priority, kev: frozenset) -> str:
    rows = sorted(
        (a.number, a.state, a.severity, in_kev(a, kev), a.first_patched or "", a.epss is not None and a.epss >= EPSS_RAISES_AT)
        for a in alerts
    )
    return hashlib.sha256(json.dumps([rows, priority.name]).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------------------------
# Issue text
# ---------------------------------------------------------------------------------------------------------------


def origin_line(key: Key, repo_full_name: str) -> str:
    parts = (quote(repo_full_name, safe=""), quote(key.ecosystem, safe=""), quote(key.package, safe=""))
    return ORIGIN_PREFIX + " ".join(parts)


def parse_origin(body: str) -> Optional[Key]:
    match = ORIGIN_LINE.fullmatch(body.split("\n", 1)[0].strip())
    return Key(*(unquote(part) for part in match.groups())) if match else None


@dataclass(frozen=True)
class Footer:
    fingerprint: str
    priority: Priority
    alerts: frozenset
    follows: Optional[str] = None
    flags: frozenset = frozenset()

    def render(self) -> str:
        fields = [f"fingerprint={self.fingerprint}", f"priority={self.priority.title}", "alerts=" + ",".join(map(str, sorted(self.alerts)))]
        if self.follows:
            fields.append(f"follows={self.follows}")
        if self.flags:
            fields.append("flags=" + ",".join(sorted(self.flags)))
        return FOOTER_PREFIX + " ".join(fields)

    def with_flags(self, *, add: frozenset = frozenset(), drop: frozenset = frozenset()) -> "Footer":
        return Footer(self.fingerprint, self.priority, self.alerts, self.follows, (self.flags | add) - drop)


def parse_footer(body: str) -> Optional[Footer]:
    found = FOOTER_LINE.findall(body)
    if not found:
        return None
    fields = dict(part.split("=", 1) for part in found[-1].split() if "=" in part)
    try:
        return Footer(
            fingerprint=fields["fingerprint"], priority=Priority.from_title(fields["priority"]),
            alerts=frozenset(int(n) for n in fields["alerts"].split(",") if n), follows=fields.get("follows"),
            flags=frozenset(f for f in fields.get("flags", "").split(",") if f),
        )
    except (KeyError, ValueError):
        return None


def replace_footer(body: str, footer: Footer) -> str:
    lines = body.rstrip("\n").split("\n")
    index = max((n for n, line in enumerate(lines) if line.startswith(FOOTER_PREFIX)), default=None)
    if index is None:
        return "\n".join([*lines, "", footer.render()]) + "\n"
    lines[index] = footer.render()
    return "\n".join(lines) + "\n"


def title(key: Key, repo: Repo) -> str:
    return f"Remediate {key.package} Dependabot alerts in {repo.full_name}"


HOW_TO_FIX = (
    "## How to fix\n\n"
    "Triage uses the bundled Triage playbook. The fix uses the Bug fix playbook, with the dependency audit as the "
    "reproduction and the verification."
)


def _alert_lines(alert: Alert, kev: frozenset) -> list:
    cves = ", ".join(alert.cves) if alert.cves else "no CVE"
    epss = f"{alert.epss:g}" if alert.epss is not None else "unknown"
    patched = alert.first_patched or "none"
    line = (
        f"- {alert.ghsa} ({cves}): severity {alert.severity}, EPSS percentile {epss}, KEV {'yes' if in_kev(alert, kev) else 'no'}, "
        f"scope {alert.scope or 'unknown'}, relationship {alert.relationship}, manifest `{alert.manifest_path}`, "
        f"vulnerable `{alert.vulnerable_range}`, first patched `{patched}`, [alert {alert.number}]({alert.url})"
    )
    lines = [line]
    if alert.first_patched is None:
        lines.append(f"  - Summary: {alert.summary}")
        lines.append("  - References: " + (", ".join(alert.references) if alert.references else "none"))
    return lines


def render_body(key: Key, repo: Repo, open_alerts: list, footer: Footer, kev: frozenset) -> str:
    head = [origin_line(key, repo.full_name)]
    if repo.tracker == "github" and footer.follows:
        head.append(f"Follows {footer.follows}")
    tail = ["", HOW_TO_FIX, "", footer.render(), ""]
    lines = [*head, "", "## Open alerts", ""]
    used = sum(len(line) + 1 for line in [*lines, *tail])
    remaining = 0
    for alert in sorted(open_alerts, key=lambda a: a.number):
        if remaining == 0 and used < BODY_DETAIL_BUDGET:
            detail = _alert_lines(alert, kev)
            lines.extend(detail)
            used += sum(len(line) + 1 for line in detail)
            continue
        short = f"- {alert.ghsa}: [alert {alert.number}]({alert.url})"
        if used + len(short) + 1 > BODY_HARD_LIMIT:
            remaining += 1
            continue
        lines.append(short)
        used += len(short) + 1
    if remaining:
        lines.append(f"- {remaining} more open alerts, listed in the repository's Dependabot alerts.")
    return "\n".join([*lines, *tail])


# ---------------------------------------------------------------------------------------------------------------
# What the job read
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Copy:
    """The Linear copy that GitHub-to-Linear intake made of a GitHub issue."""

    id: str
    identifier: str
    state_type: str
    assigned: bool
    delegated: bool
    priority: int
    priority_owned: bool


@dataclass(frozen=True)
class ExistingIssue:
    tracker: str
    key: Key
    ref: str
    identifier: str
    url: str
    is_open: bool
    state_type: str
    assigned: bool
    created_at: str
    body: str
    priority: int = 0
    priority_owned: bool = False
    markers: frozenset = frozenset()
    related: frozenset = frozenset()
    copy: Optional[Copy] = None

    @property
    def closed_by_app(self) -> bool:
        return not self.is_open and "canceled" in self.markers

    @property
    def notified(self) -> bool:
        return bool(self.markers & {"notified", "canceled"})


def linear_issue(raw: dict, priority_owned: bool) -> Optional[ExistingIssue]:
    """The sweep's own issue from one `list_issues_by_label` entry, or None for an issue it did not create."""
    key = parse_origin(raw["description"])
    if key is None or not raw["createdByMe"]:
        return None
    markers = frozenset(m for body in raw["comments"] for m in COMMENT_MARKER_PATTERN.findall(body))
    return ExistingIssue(
        tracker="linear", key=key, ref=raw["id"], identifier=raw["identifier"], url="", is_open=raw["stateType"] in OPEN_LINEAR,
        state_type=raw["stateType"], assigned=raw["assigned"], created_at=raw["createdAt"], body=raw["description"],
        priority=raw["priority"], priority_owned=priority_owned, markers=markers, related=frozenset(raw["related"]),
    )


def github_issue(raw: dict, app_login: str, copy: Optional[Copy]) -> Optional[ExistingIssue]:
    """The sweep's own issue from one GitHub issue object, or None for a pull request or an issue it did not create."""
    if "pull_request" in raw or (raw.get("user") or {}).get("login") != app_login:
        return None
    body = raw.get("body") or ""
    key = parse_origin(body)
    if key is None:
        return None
    footer = parse_footer(body)
    return ExistingIssue(
        tracker="github", key=key, ref=str(raw["number"]), identifier=f"#{raw['number']}", url=raw["html_url"],
        is_open=raw["state"] == "open", state_type=raw["state"], assigned=bool(raw.get("assignees")), created_at=raw["created_at"],
        body=body, markers=footer.flags if footer else frozenset(), copy=copy,
    )


def copy_from(raw: dict, priority_owned: bool) -> Copy:
    return Copy(raw["id"], raw["identifier"], raw["stateType"], raw["assigned"], raw["delegated"], raw["priority"], priority_owned)


@dataclass(frozen=True)
class RepoRead:
    repo: Repo
    alerts: tuple = ()
    skipped: str = ""


# ---------------------------------------------------------------------------------------------------------------
# Actions and the plan
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Create:
    repo: Repo
    key: Key
    title: str
    body: str
    priority: Priority
    previous: Optional[str]


@dataclass(frozen=True)
class UpdateBody:
    repo: Repo
    issue: ExistingIssue
    body: str


@dataclass(frozen=True)
class SetPriority:
    repo: Repo
    issue: ExistingIssue
    target: str
    to: Priority


@dataclass(frozen=True)
class Comment:
    repo: Repo
    issue: ExistingIssue
    text: str
    kind: str


@dataclass(frozen=True)
class Cancel:
    repo: Repo
    issue: ExistingIssue


@dataclass(frozen=True)
class Relate:
    repo: Repo
    issue: ExistingIssue
    previous: str


@dataclass(frozen=True)
class DelegateCopy:
    repo: Repo
    issue: ExistingIssue
    copy: Copy
    priority: Optional[Priority]


Action = Union[Create, UpdateBody, SetPriority, Comment, Cancel, Relate, DelegateCopy]


@dataclass(frozen=True)
class ReportEntry:
    repository: str
    kind: str
    open_alerts: int
    reason: str


@dataclass(frozen=True)
class Plan:
    actions: tuple
    report: tuple


def plan(reads: list, issues: list, kev: frozenset) -> Plan:
    actions: list = []
    report: list = []
    for read in reads:
        repo = read.repo
        open_count = sum(1 for a in read.alerts if a.open)
        if read.skipped:
            report.append(ReportEntry(repo.full_name, "skipped", open_count, read.skipped))
        elif repo.buildability is Buildability.UNKNOWN:
            report.append(ReportEntry(repo.full_name, "unknown", open_count, repo.reason))
        elif not repo.buildable:
            if open_count:
                report.append(ReportEntry(repo.full_name, "unbuildable", open_count, repo.reason))
        else:
            actions.extend(_plan_repo(read, [i for i in issues if i.key.repo == repo.full_name.lower() and i.tracker == repo.tracker], kev))
    return Plan(tuple(actions), tuple(report))


def _plan_repo(read: RepoRead, issues: list, kev: frozenset) -> list:
    by_key: dict = {}
    for alert in read.alerts:
        by_key.setdefault(Key(read.repo.full_name, alert.ecosystem, alert.package), []).append(alert)
    existing: dict = {}
    for issue in issues:
        existing.setdefault(issue.key, []).append(issue)
    actions: list = []
    for key in sorted(set(by_key) | set(existing)):
        history = sorted(existing.get(key, []), key=lambda i: (i.created_at, i.ref))
        actions.extend(_plan_key(read.repo, key, by_key.get(key, []), history, kev))
    return actions


def _plan_key(repo: Repo, key: Key, alerts: list, history: list, kev: frozenset) -> list:
    open_alerts = [a for a in alerts if a.open]
    current = next((i for i in reversed(history) if i.is_open), None)
    if current is not None:
        if open_alerts:
            return _update(repo, key, current, alerts, open_alerts, kev)
        return _retire(repo, current, alerts) if alerts else []
    if not open_alerts:
        return []
    previous = history[-1] if history else None
    if previous is not None and not previous.closed_by_app:
        held = parse_footer(previous.body)
        if held is not None and all(a.number in held.alerts for a in open_alerts):
            return []
    priority = issue_priority(open_alerts, kev)
    follows = previous.identifier if previous else None
    footer = Footer(fingerprint(alerts, priority, kev), priority, frozenset(a.number for a in alerts), follows)
    return [Create(repo, key, title(key, repo), render_body(key, repo, open_alerts, footer, kev), priority, follows)]


def _update(repo: Repo, key: Key, current: ExistingIssue, alerts: list, open_alerts: list, kev: frozenset) -> list:
    priority = issue_priority(open_alerts, kev)
    footer = parse_footer(current.body)
    actions: list = []
    if current.tracker == "linear" and footer and footer.follows and footer.follows not in current.related:
        actions.append(Relate(repo, current, footer.follows))

    flags = footer.flags - {"notified", "canceled"} if footer else frozenset()
    delegate = current.copy is not None and "copy-delegated" not in flags
    if delegate:
        copy = current.copy
        wanted = priority if copy.priority_owned and copy.priority != priority.linear else None
        actions.append(DelegateCopy(repo, current, copy, wanted))
        flags |= {"copy-delegated"}

    changed = footer is None or footer.fingerprint != fingerprint(alerts, priority, kev)
    if not delegate and (footer is None or footer.priority != priority):
        target = _priority_target(current)
        if target is not None and target[1] != priority.linear:
            actions.append(SetPriority(repo, current, target[0], priority))

    if changed:
        fresh = Footer(fingerprint(alerts, priority, kev), priority, frozenset(a.number for a in alerts), footer.follows if footer else None, flags)
        actions.append(UpdateBody(repo, current, render_body(key, repo, open_alerts, fresh, kev)))
    elif delegate:
        actions.append(UpdateBody(repo, current, replace_footer(current.body, footer.with_flags(add=frozenset({"copy-delegated"})))))
    return actions


def _priority_target(issue: ExistingIssue) -> Optional[tuple]:
    """The Linear issue whose priority the sweep may write, and that priority: the issue, or a GitHub issue's copy."""
    if issue.tracker == "linear":
        return (issue.ref, issue.priority) if issue.state_type == "triage" and issue.priority_owned else None
    copy = issue.copy
    return (copy.id, copy.priority) if copy and copy.state_type == "triage" and copy.priority_owned else None


def _retire(repo: Repo, current: ExistingIssue, alerts: list) -> list:
    if current.tracker == "linear":
        cancelable = current.state_type in CANCELABLE_LINEAR and not current.assigned
    else:
        cancelable = not current.assigned and (current.copy is None or current.copy.state_type in CANCELABLE_LINEAR)
    states = ", ".join(sorted({a.state.replace("_", "-") for a in alerts}))
    kind = "canceled" if cancelable else "notified"
    text = (
        f"Every Dependabot alert for this package is closed (states seen: {states})."
        + (" This job canceled the issue." if cancelable else " The issue stays open because someone has started or assigned it.")
        + "\n\n" + COMMENT_MARKER.format(kind=kind)
    )
    actions: list = []
    if not current.notified:
        actions.append(Comment(repo, current, text, kind))
    footer = parse_footer(current.body)
    if current.tracker == "github" and footer is not None:
        wanted = frozenset({"notified"}) | ({"canceled"} if cancelable else frozenset())
        if not wanted <= footer.flags:
            actions.append(UpdateBody(repo, current, replace_footer(current.body, footer.with_flags(add=wanted))))
    if cancelable:
        actions.append(Cancel(repo, current))
    return actions


# ---------------------------------------------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------------------------------------------


def state_document(report: tuple, run_at: str) -> dict:
    return {
        "run_at": run_at,
        "repositories": [
            {"repository": e.repository, "kind": e.kind, "open_alerts": e.open_alerts, "reason": e.reason} for e in report
        ],
    }


def advisory_lines(state: object) -> list:
    """One line for each repository the sweep reported, from the parsed state file."""
    entries = state.get("repositories") if isinstance(state, dict) else None
    if not isinstance(entries, list):
        return []
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        count = entry.get("open_alerts")
        count_text = f"{count} open Dependabot alerts" if isinstance(count, int) and count else "no open Dependabot alerts read"
        verb = "was skipped" if entry.get("kind") == "skipped" else "is not swept"
        lines.append(f"{entry.get('repository')} {verb}: {count_text}; {entry.get('reason')}")
    return lines
