from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, NamedTuple

SETTINGS_SECTION = "recurring_issues"
REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


class SettingsError(Exception):
    pass


class Cadence(NamedTuple):
    start: Callable[[date], date]
    after: Callable[[date], date]


def _next_month(day: date) -> date:
    first = day.replace(day=1)
    return (first + timedelta(days=31)).replace(day=1)


CADENCES = {
    "daily": Cadence(lambda day: day, lambda day: day + timedelta(days=1)),
    "weekly": Cadence(lambda day: day - timedelta(days=day.weekday()), lambda day: day - timedelta(days=day.weekday()) + timedelta(days=7)),
    "monthly": Cadence(lambda day: day.replace(day=1), _next_month),
}
STRING_FIELDS = ("repository", "team_key", "title_prefix", "cadence", "description")


@dataclass(frozen=True)
class Entry:
    index: int
    repository: str
    team_key: str
    title_prefix: str
    cadence: str
    labels: tuple[str, ...]
    description: str

    def slot_start(self, now: datetime) -> date:
        return CADENCES[self.cadence].start(now.astimezone(timezone.utc).date())

    def next_slot_start(self, now: datetime) -> date:
        return CADENCES[self.cadence].after(self.slot_start(now))

    def title(self, now: datetime) -> str:
        return f"{self.title_prefix}{self.slot_start(now).isoformat()}"


def _parse_entry(index: int, raw: object) -> Entry:
    if not isinstance(raw, dict):
        raise ValueError(f"must be an object, not {raw!r}")
    values: dict = {}
    for key in STRING_FIELDS:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} {value!r} must be a non-empty string")
        values[key] = value if key in ("title_prefix", "description") else value.strip()
    if not REPOSITORY.fullmatch(values["repository"]):
        raise ValueError(f"repository {values['repository']!r} must be <owner>/<repo>")
    if values["cadence"] not in CADENCES:
        raise ValueError(f"cadence {values['cadence']!r} must be one of {', '.join(CADENCES)}")
    labels = raw.get("labels")
    if not isinstance(labels, list) or not all(isinstance(label, str) and label.strip() for label in labels):
        raise ValueError(f"labels {labels!r} must be a list of non-empty strings")
    return Entry(index=index, labels=tuple(label.strip() for label in labels), **values)


def parse_entries(settings: object) -> tuple[list[Entry], list[str]]:
    """The valid entries and one error per malformed entry, each naming `recurring_issues[<index>]`.

    A non-list section raises SettingsError. Two entries on one team whose title prefixes are equal, or one of
    which starts with the other, would find each other's issues, so both are rejected.
    """
    if not isinstance(settings, dict):
        raise SettingsError("the factory settings must be an object")
    section = settings.get(SETTINGS_SECTION)
    if section is None:
        return [], []
    if not isinstance(section, list):
        raise SettingsError(f"{SETTINGS_SECTION} {section!r} must be a list")
    entries: list[Entry] = []
    errors: dict[int, str] = {}
    for index, raw in enumerate(section):
        try:
            entries.append(_parse_entry(index, raw))
        except ValueError as exc:
            errors[index] = f"{SETTINGS_SECTION}[{index}] {exc}"
    for entry in entries:
        for other in entries:
            if other.index != entry.index and other.team_key == entry.team_key and (
                other.title_prefix.startswith(entry.title_prefix) or entry.title_prefix.startswith(other.title_prefix)
            ):
                errors[entry.index] = (
                    f"{SETTINGS_SECTION}[{entry.index}] title_prefix {entry.title_prefix!r} collides with "
                    f"{SETTINGS_SECTION}[{other.index}] title_prefix {other.title_prefix!r} on team {entry.team_key}"
                )
    return [entry for entry in entries if entry.index not in errors], [errors[index] for index in sorted(errors)]
