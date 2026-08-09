"""Локальный защищённый архив ответов поискового Telegram-бота."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


_SAFE_PART = re.compile(r"[^a-zA-Z0-9@._-]+")


class TelegramReportArchive:
    """Сохранять отчёт и метаданные запроса вне Git.

    Полный запрос находится в JSON-метаданных. В имени файла используются только
    тип запроса, SPP ID, маскированная подсказка и короткий хеш запроса.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def record(
        self,
        *,
        client_spp_id: int,
        client_name: str,
        query_kind: str,
        query_text: str,
        outcome: str,
        report_text: str | None = None,
        response_text: str | None = None,
    ) -> Path:
        requested_at = datetime.now().astimezone()
        directory = self.directory / requested_at.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            # На Windows права определяются ACL; chmod может быть недоступен.
            pass

        query_hash = hashlib.sha256(query_text.encode("utf-8")).hexdigest()[:10]
        hint = _query_hint(query_kind, query_text)
        stem = (
            f"{requested_at.strftime('%Y%m%dT%H%M%S%f%z')}_"
            f"spp-{client_spp_id}_{query_kind}_{hint}_{query_hash}_{uuid4().hex[:6]}"
        )
        report_name = f"{stem}.txt" if report_text is not None else None
        response_name = (
            f"{stem}.response.txt"
            if report_text is None and response_text and response_text.strip()
            else None
        )
        metadata: dict[str, Any] = {
            "requested_at": requested_at.isoformat(),
            "client_spp_id": client_spp_id,
            "client_name": client_name,
            "query_kind": query_kind,
            "query": query_text,
            "outcome": outcome,
            "report_file": report_name,
            "response_file": response_name,
        }

        if report_name is not None:
            _write_private(directory / report_name, report_text or "")
        if response_name is not None:
            _write_private(directory / response_name, response_text or "")
        metadata_path = directory / f"{stem}.json"
        _write_private(
            metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )
        return metadata_path


def _query_hint(query_kind: str, query_text: str) -> str:
    clean = query_text.strip()
    if query_kind == "inn":
        digits = "".join(character for character in clean if character.isdigit())
        return f"inn-xxx{digits[-4:]}" if digits else "inn"
    if query_kind == "email" and "@" in clean:
        local, domain = clean.rsplit("@", 1)
        visible = local[:1] if local else "x"
        return _safe_part(f"email-{visible}xxx@{domain}")
    if query_kind == "person":
        date_part = clean.rsplit(" ", 1)[-1]
        return _safe_part(f"person-{date_part}")
    return _safe_part(query_kind)


def _safe_part(value: str) -> str:
    return _SAFE_PART.sub("-", value).strip("-._")[:80] or "query"


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
