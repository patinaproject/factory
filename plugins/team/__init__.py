from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)

# Hermes rejects any other skill name, so skip it here and say why.
NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def discover(skills_dir: Path) -> List[Tuple[str, Path]]:
    found = []
    for child in sorted(skills_dir.iterdir()):
        skill = child / "SKILL.md"
        if not skill.is_file():
            continue
        if not NAME.match(child.name):
            logger.warning("team: skipping %s; a skill name may hold only letters, digits, - and _", child)
            continue
        found.append((child.name, skill))
    return found


def register(ctx: Any) -> None:
    from agent.skill_utils import extract_skill_description, parse_frontmatter

    configured = ctx.get_config("skills_dir", "")
    if not configured:
        logger.warning("team: plugins.entries.team.settings.skills_dir is not set; no team skills are registered")
        return
    skills_dir = Path(configured)
    if not skills_dir.is_dir():
        logger.error(
            "team: plugins.entries.team.settings.skills_dir %s is not a directory; no team skills are registered",
            skills_dir,
        )
        return

    skills = discover(skills_dir)
    allow = ctx.get_config("allow", []) or []
    if allow:
        missing = sorted(set(allow) - {name for name, _ in skills})
        if missing:
            raise ValueError(f"team: allow names skills that {skills_dir} lacks: {', '.join(missing)}")
        skills = [(name, path) for name, path in skills if name in allow]

    for name, path in skills:
        frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        ctx.register_skill(name, path, extract_skill_description(frontmatter), frontmatter)
