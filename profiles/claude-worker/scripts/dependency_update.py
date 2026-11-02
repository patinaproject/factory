"""The weekly dependency-update issue: its settings, title, and description.

`file-dependency-update` files the issue. The title prefix is how the job finds its own open issue
again, so the prefix and the title builder live together here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

TITLE_PREFIX = "Update npm dependencies within declared ranges (week of "
SETTINGS_SECTION = "dependency_updates"
REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    repository: str
    team_key: str
    theme_label: str
    type_label: str


def parse_settings(factory_settings: object) -> Settings:
    section = factory_settings.get(SETTINGS_SECTION) if isinstance(factory_settings, dict) else None
    if not isinstance(section, dict):
        raise SettingsError(f"{SETTINGS_SECTION} is missing from the factory settings; it must be an object")
    values = {}
    for key in ("repository", "team_key", "theme_label", "type_label"):
        value = section.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"{SETTINGS_SECTION}.{key} {value!r} must be a non-empty string")
        values[key] = value.strip()
    if not REPOSITORY.fullmatch(values["repository"]):
        raise SettingsError(f"{SETTINGS_SECTION}.repository {values['repository']!r} must be <owner>/<repo>")
    return Settings(**values)


def week_monday(today: date) -> date:
    return today - timedelta(days=today.weekday())


def title(today: date) -> str:
    return f"{TITLE_PREFIX}{week_monday(today).isoformat()})"


def description(settings: Settings) -> str:
    return f"""Update the npm dependencies of `{settings.repository}` within their declared ranges. A weekly job filed this issue.

Fix contract, for triage and for the implementation card:

- Run `pnpm update` within the declared ranges across the workspace. Do not use `--latest`. Do not raise a major version.
- Add no `minimumReleaseAgeExclude` entry. A version inside the release-age window waits for the next week.
- Add no new dependency and no `overrides` entry.
- When the native app fingerprint changes, halt for a person under the repository's fingerprint rule.
- Use the `{settings.type_label}` type and the `chore(deps)` commit type. Close this issue with `Closes {settings.team_key}-N`, where N is this issue's number.
"""
