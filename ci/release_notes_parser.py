"""Parse Keep a Changelog markdown into structured release-item dicts.

Input shape (release_notes/<version>.md):

    # v1.1.0 — 2026-06-01

    ### Added
    - Wishlist sharing via email (#PROJ-123)
    - Apple Pay at checkout (#PROJ-145)

    ### Changed
    - Registration now requires phone verification (#PROJ-130)

    ### Removed
    - Legacy /old-cart route (#PROJ-150)

Output (per item):

    {
      "section": "Added",
      "text": "Wishlist sharing via email",
      "issue": "PROJ-123"
    }

The parser is intentionally permissive: it accepts `##` or `###` for section
headings, tolerates `### Added`, `## Added` and `### Added (new)` variants,
strips Markdown emphasis (*bold*, `inline code`), and extracts trailing
issue refs in either `(#NNN)` or `(PROJ-123)` form.

Section names not in the four canonical Keep a Changelog buckets (Added,
Changed, Removed, Fixed) are kept verbatim in the output but ignored by the
diff engine — that lets authors include `### Notes` sections without
breaking the pipeline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Regex pieces ---------------------------------------------------------------

# A section heading is `##` or `###` followed by a Keep a Changelog name.
# We intentionally accept extra trailing text so authors can write
# `### Added (cart improvements)` without a parser error.
_SECTION_RE = re.compile(
    r"^\s{0,3}#{2,3}\s+(?P<name>[A-Za-z][A-Za-z _-]*?)\s*(\(.*\))?\s*$",
)

# A list item starts with `-`, `*`, or `+`.
_LIST_RE = re.compile(r"^\s*[-*+]\s+(?P<body>.+)$")

# Issue reference in trailing parens: (#PROJ-123), (PROJ-123), (#123).
_ISSUE_RE = re.compile(r"\(#?([A-Z][A-Z0-9]+-\d+|\d+)\)\s*$")

# Markdown emphasis we strip from item text so the matcher gets clean tokens.
_EMPHASIS_RE = re.compile(r"[*_`]+")

# Canonical Keep a Changelog sections. Anything else is preserved verbatim
# but the diff engine treats it as advisory only.
CANONICAL_SECTIONS = {"Added", "Changed", "Removed", "Fixed", "Deprecated", "Security"}


@dataclass(frozen=True)
class ReleaseItem:
    section: str          # canonical section name (Added/Changed/Removed/Fixed/...)
    text: str             # cleaned item text, no issue ref, no markdown emphasis
    issue: str | None     # extracted issue ref, or None
    raw_line: str         # original markdown line (for error messages)

    def to_dict(self) -> dict:
        return {
            "section": self.section,
            "text": self.text,
            "issue": self.issue,
        }


def _normalize_section(name: str) -> str | None:
    """Return the canonical section name, or None if the heading is not a
    section we care about (e.g. `## Migration notes`)."""
    title = name.strip().title()
    if title in CANONICAL_SECTIONS:
        return title
    return None


def _strip_emphasis(text: str) -> str:
    return _EMPHASIS_RE.sub("", text).strip()


def parse_lines(lines: Iterable[str]) -> list[ReleaseItem]:
    """Parse an iterable of lines into ReleaseItems. The list ordering matches
    the source file so callers can preserve author intent."""
    items: list[ReleaseItem] = []
    current_section: str | None = None
    for raw in lines:
        line = raw.rstrip("\n")
        section_match = _SECTION_RE.match(line)
        if section_match:
            current_section = _normalize_section(section_match.group("name"))
            continue
        if current_section is None:
            continue  # text outside any section we recognize
        list_match = _LIST_RE.match(line)
        if not list_match:
            continue
        body = list_match.group("body").strip()
        issue: str | None = None
        issue_match = _ISSUE_RE.search(body)
        if issue_match:
            issue = issue_match.group(1)
            body = _ISSUE_RE.sub("", body).strip()
        text = _strip_emphasis(body)
        if not text:
            continue
        items.append(ReleaseItem(
            section=current_section, text=text, issue=issue, raw_line=line,
        ))
    return items


def parse_file(path: Path) -> list[ReleaseItem]:
    return parse_lines(path.read_text(encoding="utf-8").splitlines())


def parse_version_from_filename(path: Path) -> str:
    """Convention: release_notes/<version>.md. Returns "<version>" without
    extension. Used for ordering and lock-file naming."""
    return path.stem
