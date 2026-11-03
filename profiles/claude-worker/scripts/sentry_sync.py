"""The sentry-linear-sync policy: settings and rules parsing, classification, error groups, and `plan_sync`.

Pure: nothing here reads the network, the clock, or a file. `sentry-linear-sync` reads Linear and Sentry, passes
what it read to `plan_sync`, and applies the actions it returns.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, NewType, Optional, Union
from urllib.parse import parse_qs, urlencode, urlsplit

SETTINGS_SECTION = "sentry_sync"
ENVIRONMENT = "production"
MARKER_PREFIX = "<!-- sentry-linear-sync:"
ORIGIN_PREFIX = "Sentry error group: "
UNSTARTED = frozenset({"triage", "backlog", "unstarted"})
OPEN = UNSTARTED | {"started"}
URGENT, HIGH, MEDIUM, LOW = 1, 2, 3, 4
PRIORITY_NAMES = {0: "No priority", URGENT: "Urgent", HIGH: "High", MEDIUM: "Medium", LOW: "Low"}
TITLE_TEMPLATE_LIMIT = 80

GroupKey = NewType("GroupKey", str)
SanitizedText = NewType("SanitizedText", str)

IDENTIFIER = re.compile(r"[A-Za-z0-9_.$:/@#-]{1,120}")
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*")
TEAM_KEY = re.compile(r"[A-Z][A-Z0-9]*")
REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
ORIGIN_LINE = re.compile(r"^Sentry error group: (\S+) usersWhenCreated=(\d+)$", re.M)
MARKER = re.compile(r"<!-- sentry-linear-sync:([a-z]+)(?::([^ ]*))? -->")
MEMBER_LINK = re.compile(r"\[([A-Z0-9][A-Z0-9-]*)\]\(https://[^/]+/issues/([0-9]+)/\)")

# Order matters: a URL holds a path, an email holds a domain, a UUID holds hex and numbers.
TEMPLATE_VALUES = (
    (re.compile(r"\"[^\"]*\"|'[^']*'|`[^`]*`"), "<str>"),
    (re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+"), "<url>"),
    (re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+"), "<email>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b|\b(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"\S*/\S*"), "<path>"),
    (re.compile(r"\d+(?:\.\d+)*"), "<n>"),
)
TEMPLATE_TOKEN = re.compile(r"[A-Za-z0-9_.$#<>(),;:!?'-]{1,60}")


class SettingsError(Exception):
    """The settings or the rules file are invalid. The command exits 2."""


# ---------------------------------------------------------------------------------------------------------------
# Settings and rules
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    organization: str
    team_key: str
    repository: str
    checkout_path: str
    rules_path: str
    sentry_label: str
    theme_label: str
    mcp_server: str


SETTINGS_FIELDS = (
    ("organization", SLUG, "a Sentry organization slug"),
    ("team_key", TEAM_KEY, "a Linear team key such as ABC"),
    ("repository", REPOSITORY, "<owner>/<repo>"),
    ("checkout_path", re.compile(r"/.+"), "an absolute path to the worker-owned checkout"),
    ("rules_path", re.compile(r"(?!/)(?!.*(?:^|/)\.\.(?:/|$)).+"), "a path relative to the checkout, without .."),
    ("sentry_label", re.compile(r".*\S.*"), "a non-empty label name"),
    ("theme_label", re.compile(r".*\S.*"), "a non-empty label name"),
    ("mcp_server", re.compile(r".*\S.*"), "a claude_session.mcp_servers name"),
)


def configured(factory_settings: object) -> bool:
    """Whether the factory settings ask for the sync. An empty section, the plugin's default, does not."""
    return isinstance(factory_settings, dict) and factory_settings.get(SETTINGS_SECTION) not in (None, {})


def settings_problems(factory_settings: object) -> list:
    """Every problem with the `sentry_sync` section, one per field. Empty when the section is valid."""
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
    server = section.get("mcp_server")
    if isinstance(server, str) and server.strip() and server not in _mcp_servers(factory_settings):
        problems.append(f"{SETTINGS_SECTION}.mcp_server {server!r} is not a claude_session.mcp_servers entry")
    return problems


def parse_settings(factory_settings: object) -> Settings:
    problems = settings_problems(factory_settings)
    if problems:
        raise SettingsError("; ".join(problems))
    section = factory_settings[SETTINGS_SECTION]
    return Settings(**{name: section[name] for name, _, _ in SETTINGS_FIELDS})


def _mcp_servers(factory_settings: dict) -> dict:
    transport = factory_settings.get("claude_session")
    servers = transport.get("mcp_servers") if isinstance(transport, dict) else None
    return servers if isinstance(servers, dict) else {}


@dataclass(frozen=True)
class Match:
    exception_type: Optional[str] = None
    message_includes: tuple = ()
    package: Optional[str] = None

    def matches(self, issue: "SentryIssue") -> bool:
        return (
            (self.exception_type is None or issue.exception_type == self.exception_type)
            and all(text in issue.raw_message for text in self.message_includes)
            and (self.package is None or issue.package == self.package)
        )


@dataclass(frozen=True)
class ExcludeRule:
    id: str
    match: Match


@dataclass(frozen=True)
class MergeRule:
    id: str
    match: Match
    key: GroupKey


@dataclass(frozen=True)
class Rules:
    projects: Mapping[str, str]
    exclude: tuple
    merge: tuple
    window_hours: int
    min_users: int
    min_events: int
    urgent_users: int
    high_users: int
    medium_users: int
    delegation: str
    max_open: int
    stale_days: int
    verify_days: int
    suppress_days: int


RULE_NUMBERS = {
    "impact": ("windowHours", "minUsers", "minEvents"),
    "priority": ("urgentUsers", "highUsers", "mediumUsers"),
}
TOP_NUMBERS = ("maxOpen", "staleDays", "verifyDays", "suppressDays")
RULES_FIELDS = frozenset({"projects", "exclude", "merge", "delegation", *RULE_NUMBERS, *TOP_NUMBERS})


def parse_rules(document: object) -> Rules:
    """The rules file's content, validated. Raises SettingsError naming the first bad field."""
    if not isinstance(document, dict):
        raise SettingsError("rules must be a JSON object")
    unknown = sorted(set(document) - RULES_FIELDS)
    if unknown:
        raise SettingsError(f"rules has unknown fields: {', '.join(unknown)}")
    projects = document.get("projects")
    if not (isinstance(projects, dict) and projects and all(
        isinstance(k, str) and SLUG.fullmatch(k) and isinstance(v, str) and SLUG.fullmatch(v) for k, v in projects.items()
    )):
        raise SettingsError("rules.projects must map Sentry project slugs to package names")
    packages = frozenset(projects.values())
    numbers = {}
    for section, names in RULE_NUMBERS.items():
        values = document.get(section)
        if not isinstance(values, dict) or set(values) != set(names):
            raise SettingsError(f"rules.{section} must hold exactly {', '.join(names)}")
        for name in names:
            numbers[name] = _count(values[name], f"rules.{section}.{name}", minimum=1 if name == "windowHours" else 0)
    for name in TOP_NUMBERS:
        numbers[name] = _count(document.get(name), f"rules.{name}", minimum=1)
    if document.get("delegation") not in ("off", "agent-fixable"):
        raise SettingsError(f"rules.delegation {document.get('delegation')!r} must be \"off\" or \"agent-fixable\"")
    ids: set = set()
    exclude = tuple(
        ExcludeRule(_rule_id(rule, f"rules.exclude[{i}]", ids), _match(rule, f"rules.exclude[{i}]", packages))
        for i, rule in enumerate(_list(document.get("exclude", []), "rules.exclude"))
    )
    merge = []
    for i, rule in enumerate(_list(document.get("merge", []), "rules.merge")):
        where = f"rules.merge[{i}]"
        rule_id, match = _rule_id(rule, where, ids), _match(rule, where, packages)
        key = rule.get("key")
        if not (isinstance(key, str) and IDENTIFIER.fullmatch(key) and key.split(":", 1)[0] in packages and ":" in key):
            raise SettingsError(f"{where}.key {key!r} must be <package>:<name> for a package that rules.projects maps")
        merge.append(MergeRule(rule_id, match, GroupKey(key)))
    return Rules(
        projects=dict(projects), exclude=exclude, merge=tuple(merge),
        window_hours=numbers["windowHours"], min_users=numbers["minUsers"], min_events=numbers["minEvents"],
        urgent_users=numbers["urgentUsers"], high_users=numbers["highUsers"], medium_users=numbers["mediumUsers"],
        delegation=document["delegation"], max_open=numbers["maxOpen"], stale_days=numbers["staleDays"],
        verify_days=numbers["verifyDays"], suppress_days=numbers["suppressDays"],
    )


def _count(value: object, where: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SettingsError(f"{where} {value!r} must be an integer of at least {minimum}")
    return value


def _list(value: object, where: str) -> list:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SettingsError(f"{where} must be a list of objects")
    return value


def _rule_id(rule: dict, where: str, seen: set) -> str:
    rule_id = rule.get("id")
    if not (isinstance(rule_id, str) and SLUG.fullmatch(rule_id)):
        raise SettingsError(f"{where}.id {rule_id!r} must be a lowercase slug")
    if rule_id in seen:
        raise SettingsError(f"{where}.id {rule_id!r} is used by another rule")
    if not (isinstance(rule.get("why"), str) and rule["why"].strip()):
        raise SettingsError(f"{where}.why must say why the rule exists")
    seen.add(rule_id)
    return rule_id


def _match(rule: dict, where: str, packages: frozenset) -> Match:
    match = rule.get("match")
    if not isinstance(match, dict) or not match or not set(match) <= {"exceptionType", "messageIncludes", "package"}:
        raise SettingsError(f"{where}.match must hold one or more of exceptionType, messageIncludes, package")
    exception_type, includes, package = match.get("exceptionType"), match.get("messageIncludes", []), match.get("package")
    if exception_type is not None and not (isinstance(exception_type, str) and exception_type):
        raise SettingsError(f"{where}.match.exceptionType must be a non-empty string")
    if not (isinstance(includes, list) and all(isinstance(text, str) and text for text in includes)):
        raise SettingsError(f"{where}.match.messageIncludes must be a list of non-empty strings")
    if package is not None and package not in packages:
        raise SettingsError(f"{where}.match.package {package!r} is not a package that rules.projects maps")
    return Match(exception_type, tuple(includes), package)


# ---------------------------------------------------------------------------------------------------------------
# Sentry observation
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InRepoFrame:
    path: str
    function: str


@dataclass(frozen=True)
class SentryIssue:
    """One Sentry issue. `raw_message` is used for matching and template normalization and is never written."""

    id: str
    short_id: str
    package: str
    exception_type: Optional[str]
    raw_message: str
    frame: Optional[InRepoFrame]
    unhandled: bool
    first_seen: datetime
    users: int
    events: int


@dataclass(frozen=True)
class ReleaseCounts:
    """Events of one error group's members since its issue completed, by release, and the package's releases
    created after that completion."""

    by_release: Mapping[str, int]
    created_after: Mapping[str, datetime]


@dataclass(frozen=True)
class Observation:
    issues: tuple
    # Sentry issue id to events in the last staleDays, for the members of every unstarted tracked issue.
    recent_events: Mapping[str, int]
    release_counts: Mapping[GroupKey, ReleaseCounts]
    # Sentry issue id to the issue each legacy Linear issue names, or None when Sentry no longer has it.
    legacy_sources: Mapping[str, Optional[SentryIssue]]


def repo_path_index(paths) -> dict:
    """Every suffix of two or more components of each repository path, to the paths that end with it."""
    index: dict = {}
    for path in paths:
        parts = tuple(path.split("/"))
        for start in range(0, len(parts) - 1):
            index.setdefault(parts[start:], set()).add(path)
    return index


def resolve_repo_path(filename: Optional[str], index: Mapping) -> Optional[str]:
    """The one repository path that a Sentry frame's filename names, by its longest matching suffix."""
    if not filename:
        return None
    parts = [p for p in re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*:/*", "", filename).split("/") if p not in ("", ".", "..", "~")]
    if "node_modules" in parts:
        return None
    for start in range(0, len(parts) - 1):
        found = index.get(tuple(parts[start:]))
        if found:
            return next(iter(found)) if len(found) == 1 else None
    return None


# ---------------------------------------------------------------------------------------------------------------
# Sanitized text
# ---------------------------------------------------------------------------------------------------------------


def sanitize_identifier(value: Optional[str]) -> Optional[SanitizedText]:
    return SanitizedText(value) if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def normalize_template(message: str) -> Optional[SanitizedText]:
    """The message with every value replaced by a placeholder and every other unsafe token dropped. None when no
    word remains."""
    text = message
    for pattern, placeholder in TEMPLATE_VALUES:
        text = pattern.sub(placeholder, text)
    tokens = [token for token in text.split() if TEMPLATE_TOKEN.fullmatch(token)]
    if not any(re.search(r"[A-Za-z]", re.sub(r"<[a-z]+>", "", token)) for token in tokens):
        return None
    return SanitizedText(" ".join(tokens))


# ---------------------------------------------------------------------------------------------------------------
# Classification and error groups
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Eligible:
    key: GroupKey
    title: SanitizedText
    behavior: SanitizedText


@dataclass(frozen=True)
class Excluded:
    rule_id: str


@dataclass(frozen=True)
class NoKey:
    pass


Classification = Union[Eligible, Excluded, NoKey]


def classify(issue: SentryIssue, rules: Rules) -> Classification:
    """Exclude rules first, then merge rules, then the in-repo frame, then the message template."""
    for rule in rules.exclude:
        if rule.match.matches(issue):
            return Excluded(rule.id)
    exception = sanitize_identifier(issue.exception_type) or SanitizedText("Error")
    for rule in rules.merge:
        if rule.match.matches(issue):
            package = rule.key.split(":", 1)[0]
            return Eligible(rule.key, SanitizedText(f"{package}: {rule.id}"), SanitizedText(f"Error group `{rule.id}`, merged by rule."))
    if issue.frame is not None:
        key = GroupKey(f"{issue.package}:{exception}:{issue.frame.path}:{issue.frame.function}")
        return Eligible(
            key,
            SanitizedText(f"{issue.package}: {exception} in {issue.frame.function}"),
            SanitizedText(f"`{exception}` thrown in `{issue.frame.function}` at `{issue.frame.path}` in {ENVIRONMENT}."),
        )
    template = normalize_template(issue.raw_message)
    if template is None:
        return NoKey()
    digest = hashlib.sha256(template.encode()).hexdigest()[:12]
    short = template if len(template) <= TITLE_TEMPLATE_LIMIT else template[:TITLE_TEMPLATE_LIMIT - 3].rstrip() + "..."
    return Eligible(
        GroupKey(f"{issue.package}:msg:{digest}"),
        SanitizedText(f"{issue.package}: {short}"),
        SanitizedText(f"`{exception}` reported with the message template `{template}` in {ENVIRONMENT}."),
    )


def group_search_url(key: GroupKey, organization: str) -> str:
    """A Sentry issue search for the key's members. The `groupKey` parameter keeps the URL unique for each key."""
    package, kind, rest = (key.split(":", 2) + ["", ""])[:3]
    if kind == "msg" or not rest or ":" not in rest:
        query = "issue.category:error"
    else:
        path, function = rest.rsplit(":", 1)
        query = f"error.type:{kind} stack.filename:*{path.rsplit('/', 1)[-1]} stack.function:{function}"
    return f"https://{organization}.sentry.io/issues/?" + urlencode(
        [("query", query), ("environment", ENVIRONMENT), ("groupKey", key)]
    )


def key_of_url(url: str, organization: str) -> Optional[GroupKey]:
    parts = urlsplit(url)
    if parts.netloc != f"{organization}.sentry.io" or parts.path != "/issues/":
        return None
    keys = parse_qs(parts.query).get("groupKey")
    return GroupKey(keys[0]) if keys and IDENTIFIER.fullmatch(keys[0]) else None


def sentry_issue_id_of(url: str, organization: str) -> Optional[str]:
    """The Sentry issue id in a link that Sentry's Linear integration attached."""
    found = re.match(
        rf"https://(?:sentry\.io/organizations/{re.escape(organization)}|{re.escape(organization)}\.sentry\.io)/issues/([0-9]+)(?:/|$)",
        url,
    )
    return found[1] if found else None


@dataclass(frozen=True)
class ErrorGroup:
    key: GroupKey
    url: str
    package: str
    title: SanitizedText
    behavior: SanitizedText
    members: tuple
    users: int
    events: int
    unhandled: bool
    first_seen: datetime
    priority: int


def priority_of(users: int, unhandled: bool, rules: Rules) -> int:
    level = URGENT if users >= rules.urgent_users else HIGH if users >= rules.high_users else MEDIUM if users >= rules.medium_users else LOW
    return max(URGENT, level - 1) if unhandled else level


def description(group: ErrorGroup, rules: Rules, organization: str) -> SanitizedText:
    members = "\n".join(f"- {member_link(m, organization)}" for m in group.members)
    return SanitizedText(f"""## Reported behavior

{group.behavior}

## Impact (last {rules.window_hours} hours)

{group.users} users, {group.events} events. First seen {group.first_seen.date().isoformat()}.

## Sentry issues in this error group

{members}

## Fix contract

- The reporter is this Sentry error group. Write a test that fails the way the reported behavior describes.
- The fix is done when that test passes. The sync job then checks {ENVIRONMENT}: zero events from releases after the fix for {rules.verify_days} days.
- Copy no event value (message, URL, tag, user, breadcrumb) into code, fixtures, commits, or the pull request body.

{ORIGIN_PREFIX}{group.key} usersWhenCreated={group.users}
""")


def member_link(issue: SentryIssue, organization: str) -> str:
    return f"[{issue.short_id}](https://{organization}.sentry.io/issues/{issue.id}/)"


def marker(kind: str, value: str = "") -> str:
    return f"{MARKER_PREFIX}{kind}{':' + value if value else ''} -->"


# ---------------------------------------------------------------------------------------------------------------
# Tracked issues
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackedIssue:
    """A Linear issue the job created or took over. Its keys come from its group search URL attachments."""

    issue_id: str
    identifier: str
    keys: tuple
    attached: frozenset
    state_type: str
    created_at: datetime
    completed_at: Optional[datetime]
    canceled_at: Optional[datetime]
    duplicate_of: Optional[str]
    priority: int
    users_when_created: int
    members: frozenset  # Sentry issue ids
    markers: frozenset  # "<kind>:<value>" of the job's own comments
    created_by_me: bool
    priority_owned: bool

    @property
    def open(self) -> bool:
        return self.state_type in OPEN

    @property
    def verified(self) -> bool:
        return any(m.startswith("verify:") for m in self.markers)

    def has_member(self, issue: "SentryIssue") -> bool:
        return issue.id in self.members or f"member:{issue.short_id}" in self.markers


@dataclass(frozen=True)
class LegacyIssue:
    """An open issue Sentry's Linear integration created, which the job has not taken over."""

    issue_id: str
    identifier: str
    sentry_id: str
    created_at: datetime
    started: bool
    assigned: bool


@dataclass(frozen=True)
class TrackedRead:
    tracked: tuple
    legacy: tuple


def parse_time(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def read_tracked(issues: list, organization: str, priority_owned: frozenset) -> TrackedRead:
    """Sort Linear's labeled issues into the job's tracked issues and the legacy issues it may take over.

    `issues` is `LinearClient.list_issues_by_label`'s list. `priority_owned` holds the ids of the job's issues whose
    priority only the agent app has changed.
    """
    tracked, legacy = [], []
    own = {issue["identifier"]: _members(issue, organization) for issue in issues}
    # A duplicate's Sentry issues belong to its canonical issue, whether the job or a person marked it.
    merged: dict = {}
    for issue in issues:
        if issue["duplicateOf"]:
            merged.setdefault(issue["duplicateOf"], set()).update(own[issue["identifier"]])
    for issue in issues:
        attached = frozenset(k for k in (key_of_url(a["url"], organization) for a in issue["attachments"]) if k)
        sentry_ids = _integration_ids(issue, organization)
        origin = ORIGIN_LINE.search(issue["description"]) if issue["createdByMe"] else None
        keys = sorted(attached | ({GroupKey(origin[1])} if origin else set()))
        if keys and (issue["createdByMe"] or (sentry_ids and attached)):
            markers = frozenset(
                f"{m[1]}:{m[2] or ''}".rstrip(":") for body in issue["comments"] for m in MARKER.finditer(body)
            )
            tracked.append(TrackedIssue(
                issue_id=issue["id"], identifier=issue["identifier"], keys=tuple(keys), attached=attached,
                state_type=issue["stateType"], created_at=parse_time(issue["createdAt"]),
                completed_at=parse_time(issue["completedAt"]), canceled_at=parse_time(issue["canceledAt"]),
                duplicate_of=issue["duplicateOf"], priority=issue["priority"],
                users_when_created=int(origin[2]) if origin else 0,
                members=frozenset(own[issue["identifier"]] | merged.get(issue["identifier"], set())),
                markers=markers, created_by_me=issue["createdByMe"], priority_owned=issue["id"] in priority_owned,
            ))
        elif sentry_ids and issue["creator"] is None and issue["stateType"] in OPEN:
            legacy.append(LegacyIssue(
                issue_id=issue["id"], identifier=issue["identifier"], sentry_id=sentry_ids[0],
                created_at=parse_time(issue["createdAt"]), started=issue["stateType"] == "started", assigned=issue["assigned"],
            ))
    return TrackedRead(tuple(tracked), tuple(legacy))


def _integration_ids(issue: dict, organization: str) -> list:
    """The Sentry issue ids in the links that Sentry's Linear integration attached."""
    ids = (sentry_issue_id_of(a["url"], organization) for a in issue["attachments"] if a["sourceType"] == "sentry")
    return [i for i in ids if i]


def _members(issue: dict, organization: str) -> set:
    """The Sentry issue ids the issue names: its description's member list, the job's member comments, and the
    integration's links."""
    members = {m[2] for m in MEMBER_LINK.finditer(issue["description"])}
    members |= {m[2] for body in issue["comments"] if MARKER_PREFIX in body for m in MEMBER_LINK.finditer(body)}
    return members | set(_integration_ids(issue, organization))


def current_issues(read: TrackedRead) -> dict:
    """Each key's current tracked issue: the newest open one, else the newest one. A duplicate whose canonical
    issue already carries the key does not count."""
    by_key: dict = {}
    for issue in read.tracked:
        for key in issue.keys:
            by_key.setdefault(key, []).append(issue)
    current = {}
    for key, issues in by_key.items():
        identifiers = {i.identifier for i in issues}
        candidates = [i for i in issues if not (i.state_type == "duplicate" and i.duplicate_of in identifiers)]
        current[key] = max(candidates, key=lambda i: (i.open, i.created_at, i.identifier))
    return current


def done_unverified(read: TrackedRead) -> dict:
    """Key to its current issue, for each key whose current issue is Done and not yet verified."""
    return {
        key: issue for key, issue in current_issues(read).items()
        if issue.state_type == "completed" and not issue.verified and issue.members
    }


def stale_candidates(read: TrackedRead) -> tuple:
    """Unstarted tracked issues, the ones the stale cancel can apply to."""
    return tuple(i for i in read.tracked if i.state_type in UNSTARTED and i.members)


# ---------------------------------------------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Repair:
    issue: TrackedIssue
    key: GroupKey
    url: str


@dataclass(frozen=True)
class Create:
    group: ErrorGroup
    delegate: bool
    rank: int
    previous: Optional[str] = None


@dataclass(frozen=True)
class TakeOver:
    legacy: LegacyIssue
    key: GroupKey
    url: str


@dataclass(frozen=True)
class MarkDuplicate:
    legacy: LegacyIssue
    canonical: str


@dataclass(frozen=True)
class AddMembers:
    issue: TrackedIssue
    added: tuple


@dataclass(frozen=True)
class SetPriority:
    issue: TrackedIssue
    to: int


@dataclass(frozen=True)
class LinkKey:
    canonical: TrackedIssue
    duplicate: str
    key: GroupKey
    url: str


@dataclass(frozen=True)
class LinkRefused:
    duplicate: str
    canonical: str
    key: GroupKey


@dataclass(frozen=True)
class CloseInactive:
    issue: TrackedIssue
    quiet_days: int


@dataclass(frozen=True)
class Verify:
    issue: TrackedIssue
    fix_release: str
    days: int


@dataclass(frozen=True)
class OpenRegression:
    group: ErrorGroup
    previous: TrackedIssue
    release: str


@dataclass(frozen=True)
class Hold:
    group: ErrorGroup
    rank: int
    cap: int


Action = Union[
    Repair, Create, TakeOver, MarkDuplicate, AddMembers, SetPriority, LinkKey, LinkRefused, CloseInactive, Verify,
    OpenRegression, Hold,
]


@dataclass(frozen=True)
class Report:
    excluded: Mapping[str, int]
    no_key: int
    below_impact: int
    suppressed: int
    legacy_untouched: int
    legacy_kept: int


@dataclass(frozen=True)
class Plan:
    actions: tuple
    report: Report


@dataclass
class _Groups:
    eligible: dict = field(default_factory=dict)
    excluded: dict = field(default_factory=dict)
    no_key: int = 0


def _classify_all(issues, rules: Rules) -> _Groups:
    groups = _Groups({}, {rule.id: 0 for rule in rules.exclude})
    for issue in issues:
        verdict = classify(issue, rules)
        if isinstance(verdict, Excluded):
            groups.excluded[verdict.rule_id] += 1
        elif isinstance(verdict, NoKey):
            groups.no_key += 1
        else:
            groups.eligible.setdefault(verdict.key, (verdict, []))[1].append(issue)
    return groups


def _group(verdict: Eligible, members: list, rules: Rules, organization: str) -> ErrorGroup:
    members = sorted(members, key=lambda m: m.short_id)
    users = max(m.users for m in members)
    unhandled = any(m.unhandled for m in members)
    return ErrorGroup(
        key=verdict.key, url=group_search_url(verdict.key, organization), package=members[0].package,
        title=verdict.title, behavior=verdict.behavior, members=tuple(members), users=users,
        events=sum(m.events for m in members), unhandled=unhandled, first_seen=min(m.first_seen for m in members),
        priority=priority_of(users, unhandled, rules),
    )


def plan_sync(observed: Observation, read: TrackedRead, rules: Rules, organization: str, now: datetime) -> Plan:
    """The whole policy, as one pure function. It never acts on an issue absent from `read`, and never reopens one."""
    actions: list = []
    current = current_issues(read)

    for issue in read.tracked:
        for key in issue.keys:
            if key not in issue.attached:
                actions.append(Repair(issue, key, group_search_url(key, organization)))

    classified = _classify_all(observed.issues, rules)
    groups = {key: _group(verdict, members, rules, organization) for key, (verdict, members) in classified.eligible.items()}
    impactful = {k: g for k, g in groups.items() if g.users >= rules.min_users or g.events >= rules.min_events}

    taken, legacy_untouched, legacy_kept = _take_over(read.legacy, observed, current, rules, organization, actions)

    creates: list = []
    suppressed = 0
    for key, group in sorted(impactful.items()):
        issue = current.get(key)
        if key in taken:
            continue
        if issue is None:
            creates.append((group, None))
        elif issue.open:
            _update_open(issue, group, actions)
        elif issue.state_type == "duplicate":
            continue
        elif issue.state_type == "canceled":
            recent = issue.canceled_at and now - issue.canceled_at < timedelta(days=rules.suppress_days)
            if recent and group.users < 10 * max(issue.users_when_created, 1):
                suppressed += 1
            else:
                creates.append((group, issue.identifier))
        elif issue.verified:
            actions.append(OpenRegression(group, issue, ""))

    for key, issue in sorted(current.items()):
        if issue.state_type == "duplicate":
            _link(key, issue, read, organization, actions)
        elif issue.state_type == "completed" and not issue.verified and key in observed.release_counts:
            _verify(key, issue, observed.release_counts[key], groups.get(key), rules, now, actions)

    active = {i.id for i in observed.issues if i.events}
    for issue in read.tracked:
        if issue.state_type in UNSTARTED and issue.members and not issue.members & active:
            if not any(observed.recent_events[m] for m in issue.members):
                actions.append(CloseInactive(issue, rules.stale_days))

    open_unstarted = len({i.identifier for i in read.tracked if i.state_type in UNSTARTED})
    open_unstarted += len([a for a in actions if isinstance(a, TakeOver)])
    _rank(creates, rules, open_unstarted, actions)

    return Plan(tuple(actions), Report(
        excluded=classified.excluded, no_key=classified.no_key, below_impact=len(groups) - len(impactful),
        suppressed=suppressed, legacy_untouched=legacy_untouched, legacy_kept=legacy_kept,
    ))


def _update_open(issue: TrackedIssue, group: ErrorGroup, actions: list) -> None:
    added = tuple(m for m in group.members if not issue.has_member(m))
    if added:
        actions.append(AddMembers(issue, added))
    if issue.created_by_me and issue.state_type == "triage" and issue.priority_owned and issue.priority != group.priority:
        actions.append(SetPriority(issue, group.priority))


def _link(key: GroupKey, issue: TrackedIssue, read: TrackedRead, organization: str, actions: list) -> None:
    canonical = next((i for i in read.tracked if i.identifier == issue.duplicate_of), None)
    if canonical is None:
        if issue.duplicate_of:
            actions.append(LinkRefused(issue.identifier, issue.duplicate_of, key))
        return
    if key not in canonical.keys:
        actions.append(LinkKey(canonical, issue.identifier, key, group_search_url(key, organization)))


def _verify(key, issue: TrackedIssue, counts: ReleaseCounts, group, rules: Rules, now: datetime, actions: list) -> None:
    if not counts.created_after:
        return
    with_events = sorted((counts.created_after[r], r) for r, n in counts.by_release.items() if n and r in counts.created_after)
    if with_events:
        if group is not None:
            actions.append(OpenRegression(group, issue, with_events[-1][1]))
        return
    if now - issue.completed_at >= timedelta(days=rules.verify_days):
        fix = min((created, release) for release, created in counts.created_after.items())[1]
        actions.append(Verify(issue, fix, rules.verify_days))


def _take_over(legacy, observed: Observation, current: dict, rules: Rules, organization: str, actions: list) -> tuple:
    by_key: dict = {}
    untouched = kept = 0
    for issue in legacy:
        source = observed.legacy_sources.get(issue.sentry_id)
        verdict = classify(source, rules) if source is not None else NoKey()
        if isinstance(verdict, Eligible):
            by_key.setdefault(verdict.key, []).append(issue)
        else:
            untouched += 1
    taken = set()
    for key, issues in sorted(by_key.items()):
        owner = current.get(key)
        if owner is not None and owner.open:
            canonical, rest = owner.identifier, issues
        else:
            first = min(issues, key=lambda i: (not (i.started or i.assigned), i.created_at, i.identifier))
            canonical, rest = first.identifier, [i for i in issues if i is not first]
            actions.append(TakeOver(first, key, group_search_url(key, organization)))
            taken.add(key)
        for issue in sorted(rest, key=lambda i: (i.created_at, i.identifier)):
            if issue.started or issue.assigned:
                kept += 1
            else:
                actions.append(MarkDuplicate(issue, canonical))
    return taken, untouched, kept


def _rank(creates: list, rules: Rules, open_unstarted: int, actions: list) -> None:
    ordered = sorted(creates, key=lambda c: (c[0].priority, -c[0].users, -c[0].events, c[0].key))
    for rank, (group, previous) in enumerate(ordered, start=1):
        if group.priority == URGENT or open_unstarted < rules.max_open:
            delegate = rules.delegation == "agent-fixable"
            actions.append(Create(group, delegate, rank, previous))
            open_unstarted += 1
        else:
            actions.append(Hold(group, rank, rules.max_open))
