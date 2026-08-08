"""Pure parser for text reports returned by the contact-search bot.

The parser deliberately knows nothing about Telegram, HTTP, or the database.  It
only accepts report text and returns immutable values which can be tested and
used by the enrichment workflow.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from itertools import product
from typing import Iterable


_SECTION_PATTERN = re.compile(r"^\s*===\s*(.*?)\s*===\s*$")
_FIELD_PATTERN = re.compile(r"^\s*([^:\r\n]+?)\s*:\s*(.*?)\s*$")
_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])"
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?![\w.-])",
    re.IGNORECASE,
)
_PHONE_SEPARATOR_PATTERN = re.compile(r"\s*(?:;|,|\||/)\s*")
_PHONE_EXTENSION_PATTERN = re.compile(
    r"\s*(?:доб(?:авочный)?\.?|ext\.?)\s*\d+\s*$", re.IGNORECASE
)

_PHONE_FIELDS = frozenset({"телефон"})
_EMAIL_FIELDS = frozenset({"email", "e-mail", "почта"})
_INN_FIELDS = frozenset({"инн"})
_FIO_FIELDS = frozenset({"фио", "ф.и.о."})
_BIRTH_DATE_FIELDS = frozenset({"дата рождения", "день рождения"})
_SUMMARY_SECTION_TITLE = "общая сводка"


@dataclass(frozen=True)
class ReportField:
    """One ``Key: value`` pair from a report section."""

    name: str
    value: str


@dataclass(frozen=True)
class ReportSection:
    """A report block introduced by an ``=== section ===`` header."""

    title: str
    fields: tuple[ReportField, ...]

    def values(self, *field_names: str) -> tuple[str, ...]:
        """Return values of exact, case-insensitive field names."""

        expected = {_normalize_field_name(name) for name in field_names}
        return tuple(
            field.value
            for field in self.fields
            if _normalize_field_name(field.name) in expected
        )


@dataclass(frozen=True)
class PersonCandidate:
    """A FIO and date-of-birth pair found beside the requested INN."""

    full_name: str
    date_of_birth: date
    source_sections: tuple[str, ...]


class CandidateSelectionStatus(StrEnum):
    """Safety result of selecting a person for the second bot request."""

    SELECTED = "selected"
    NONE = "none"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class CandidateSelection:
    """Selected candidate, absence of one, or an ambiguity for manual review."""

    status: CandidateSelectionStatus
    candidate: PersonCandidate | None
    candidates: tuple[PersonCandidate, ...]

    @property
    def requires_manual_review(self) -> bool:
        return self.status is CandidateSelectionStatus.AMBIGUOUS


@dataclass(frozen=True)
class ParsedReport:
    """Structured values extracted from a complete bot report."""

    sections: tuple[ReportSection, ...]
    phones: tuple[str, ...]
    emails: tuple[str, ...]
    person_candidates: tuple[PersonCandidate, ...]
    candidate_selection: CandidateSelection


def parse_report_sections(report_text: str) -> tuple[ReportSection, ...]:
    """Parse only explicit ``=== section ===`` blocks from *report_text*."""

    if not isinstance(report_text, str):
        raise TypeError("report_text must be a string")

    sections: list[ReportSection] = []
    current_title: str | None = None
    current_fields: list[ReportField] = []

    for line in report_text.splitlines():
        section_match = _SECTION_PATTERN.fullmatch(line)
        if section_match:
            if current_title is not None:
                sections.append(
                    ReportSection(current_title, tuple(current_fields))
                )
            current_title = _clean_text(section_match.group(1))
            current_fields = []
            continue

        if current_title is None:
            continue
        field_match = _FIELD_PATTERN.fullmatch(line)
        if field_match:
            current_fields.append(
                ReportField(
                    name=_clean_text(field_match.group(1)),
                    value=field_match.group(2).strip(),
                )
            )

    if current_title is not None:
        sections.append(ReportSection(current_title, tuple(current_fields)))

    return tuple(sections)


def normalize_phone(value: str) -> str | None:
    """Normalize a plausible phone to an E.164-like value.

    Ten-digit numbers and Russian numbers beginning with ``8`` are normalized
    to country code ``+7``. Values outside the E.164 length range are rejected.
    """

    if not isinstance(value, str):
        return None
    clean_value = _PHONE_EXTENSION_PATTERN.sub("", value.strip())
    digits = "".join(
        character for character in clean_value if character in "0123456789"
    )

    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]

    if not 10 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return "+" + digits


def normalize_email(value: str) -> str | None:
    """Return a lower-case email when the complete value is valid."""

    if not isinstance(value, str):
        return None
    clean_value = value.strip().lower()
    if len(clean_value) > 254 or _EMAIL_PATTERN.fullmatch(clean_value) is None:
        return None
    local_part, domain = clean_value.rsplit("@", maxsplit=1)
    if len(local_part) > 64 or any(len(label) > 63 for label in domain.split(".")):
        return None
    return clean_value


def select_person_candidate(
    candidates: Iterable[PersonCandidate],
    expected_director_name: str | None = None,
) -> CandidateSelection:
    """Select safely, never taking an arbitrary first candidate.

    When *expected_director_name* is supplied, only candidates whose normalized
    full name matches it are considered. More than one distinct FIO/date pair is
    explicitly returned as ambiguous so the caller can request manual review.
    """

    unique_candidates = _merge_candidates(candidates)
    if expected_director_name is not None and expected_director_name.strip():
        expected_name = normalize_person_name(expected_director_name)
        unique_candidates = tuple(
            candidate
            for candidate in unique_candidates
            if normalize_person_name(candidate.full_name) == expected_name
        )

    if not unique_candidates:
        return CandidateSelection(CandidateSelectionStatus.NONE, None, ())
    if len(unique_candidates) == 1:
        return CandidateSelection(
            CandidateSelectionStatus.SELECTED,
            unique_candidates[0],
            unique_candidates,
        )
    return CandidateSelection(
        CandidateSelectionStatus.AMBIGUOUS,
        None,
        unique_candidates,
    )


def parse_report(
    report_text: str,
    *,
    source_inn: str,
    expected_director_name: str | None = None,
) -> ParsedReport:
    """Parse contacts and safe fallback-person candidates from a report."""

    clean_inn = _normalize_source_inn(source_inn)
    sections = parse_report_sections(report_text)

    phones: list[str] = []
    emails: list[str] = []
    seen_phones: set[str] = set()
    seen_emails: set[str] = set()

    for section in _contact_sections(sections, clean_inn):
        for field in section.fields:
            field_name = _normalize_field_name(field.name)
            if field_name in _PHONE_FIELDS:
                for part in _PHONE_SEPARATOR_PATTERN.split(field.value):
                    phone = normalize_phone(part)
                    if phone is not None and phone not in seen_phones:
                        seen_phones.add(phone)
                        phones.append(phone)
            elif field_name in _EMAIL_FIELDS:
                for match in _EMAIL_PATTERN.finditer(field.value):
                    email = normalize_email(match.group(0))
                    if email is not None and email not in seen_emails:
                        seen_emails.add(email)
                        emails.append(email)

    candidates = _person_candidates(sections, clean_inn)
    selection = select_person_candidate(candidates, expected_director_name)
    return ParsedReport(
        sections=sections,
        phones=tuple(phones),
        emails=tuple(emails),
        person_candidates=candidates,
        candidate_selection=selection,
    )


def _contact_sections(
    sections: tuple[ReportSection, ...], source_inn: str
) -> tuple[ReportSection, ...]:
    summary_sections = tuple(
        section
        for section in sections
        if _normalize_field_name(section.title) == _SUMMARY_SECTION_TITLE
    )
    if summary_sections:
        return summary_sections

    return tuple(
        section
        for section in sections
        if source_inn
        in {
            value.strip()
            for value in section.values(*_INN_FIELDS)
            if re.fullmatch(r"[0-9]{10}|[0-9]{12}", value.strip()) is not None
        }
    )


def normalize_person_name(value: str) -> str:
    """Normalize case, Unicode, ``Е/Ё``, punctuation, and whitespace in a FIO."""

    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = re.sub(r"[‐‑‒–—―]", "-", normalized)
    normalized = re.sub(r"[^\w\-]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _person_candidates(
    sections: tuple[ReportSection, ...], source_inn: str
) -> tuple[PersonCandidate, ...]:
    candidates: list[PersonCandidate] = []
    for section in sections:
        section_inns = {
            value.strip()
            for value in section.values(*_INN_FIELDS)
            if re.fullmatch(r"[0-9]{10}|[0-9]{12}", value.strip()) is not None
        }
        if source_inn not in section_inns:
            continue

        names = _section_person_names(section)
        birth_dates = _section_birth_dates(section)
        for full_name, date_of_birth in product(names, birth_dates):
            candidates.append(
                PersonCandidate(full_name, date_of_birth, (section.title,))
            )
    return _merge_candidates(candidates)


def _section_person_names(section: ReportSection) -> tuple[str, ...]:
    direct_names = _dedupe_text_values(section.values(*_FIO_FIELDS))
    if direct_names:
        return direct_names

    last_names = _dedupe_text_values(section.values("Фамилия"))
    first_names = _dedupe_text_values(section.values("Имя"))
    middle_names = _dedupe_text_values(section.values("Отчество")) or ("",)
    result = (
        _clean_text(" ".join(part for part in parts if part))
        for parts in product(last_names, first_names, middle_names)
    )
    return _dedupe_text_values(result)


def _section_birth_dates(section: ReportSection) -> tuple[date, ...]:
    result: list[date] = []
    seen: set[date] = set()
    for value in section.values(*_BIRTH_DATE_FIELDS):
        parsed = _parse_date(value)
        if parsed is not None and parsed not in seen:
            seen.add(parsed)
            result.append(parsed)
    return tuple(result)


def _parse_date(value: str) -> date | None:
    for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), date_format).date()
        except ValueError:
            continue
    return None


def _merge_candidates(
    candidates: Iterable[PersonCandidate],
) -> tuple[PersonCandidate, ...]:
    merged: dict[tuple[str, date], PersonCandidate] = {}
    for candidate in candidates:
        key = (normalize_person_name(candidate.full_name), candidate.date_of_birth)
        previous = merged.get(key)
        if previous is None:
            merged[key] = candidate
            continue
        source_sections = tuple(
            dict.fromkeys(previous.source_sections + candidate.source_sections)
        )
        merged[key] = PersonCandidate(
            previous.full_name,
            previous.date_of_birth,
            source_sections,
        )
    return tuple(merged.values())


def _dedupe_text_values(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean_value = _clean_text(value)
        normalized = normalize_person_name(clean_value)
        if clean_value and normalized and normalized not in seen:
            seen.add(normalized)
            result.append(clean_value)
    return tuple(result)


def _normalize_source_inn(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("source_inn must be a string")
    clean_value = value.strip()
    if re.fullmatch(r"[0-9]{10}|[0-9]{12}", clean_value) is None:
        raise ValueError("source_inn must contain 10 or 12 digits")
    return clean_value


def _normalize_field_name(value: str) -> str:
    return _clean_text(unicodedata.normalize("NFKC", value).casefold())


def _clean_text(value: str) -> str:
    return " ".join(value.strip().split())


__all__ = [
    "CandidateSelection",
    "CandidateSelectionStatus",
    "ParsedReport",
    "PersonCandidate",
    "ReportField",
    "ReportSection",
    "normalize_email",
    "normalize_person_name",
    "normalize_phone",
    "parse_report",
    "parse_report_sections",
    "select_person_candidate",
]
