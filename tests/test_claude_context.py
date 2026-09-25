"""Project-local Claude context stays valid and portable."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS = sorted((ROOT / ".claude" / "agents").glob("*.md"))
SKILLS = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, f"{path} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_expected_agents_and_skills_exist():
    assert {p.stem for p in AGENTS} >= {
        "trading-architect", "execution-safety-auditor", "quant-methodology-auditor", "release-gate-reviewer",
    }
    assert {p.parent.name for p in SKILLS} >= {
        "knt-trading-core", "ibkr-execution-safety", "strategy-validation", "paper-to-live-gate",
        "context-efficiency",
    }


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.stem)
def test_agent_frontmatter(path):
    fm = _frontmatter(path)
    assert fm.get("name") == path.stem
    assert len(fm.get("description", "")) > 40
    tools = {t.strip() for t in fm.get("tools", "").split(",") if t.strip()}
    # Reviewers/investigators are read-only: no shell, no editing.
    assert tools and tools <= {"Read", "Grep", "Glob"}


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter(path):
    fm = _frontmatter(path)
    assert fm.get("name") == path.parent.name
    assert len(fm.get("description", "")) > 40


def test_context_files_contain_no_absolute_user_paths():
    for path in [ROOT / "CLAUDE.md", *AGENTS, *SKILLS]:
        text = path.read_text(encoding="utf-8")
        assert "C:\\Users" not in text and "/Users/" not in text, path
