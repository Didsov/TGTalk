"""Создание и проверка резервных копий SQLite и архивов ответов."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from src.config import PROJECT_ROOT


DEFAULT_DATABASE = PROJECT_ROOT / "data" / "clients.db"
DEFAULT_RESPONSES = PROJECT_ROOT / "data" / "telegram_reports"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "backups"
BACKUP_PREFIX = "inntophone-backup-"
MANIFEST_NAME = "manifest.json"
DATABASE_MEMBER = "database/clients.db"


class BackupError(RuntimeError):
    """Архив не может быть создан или не прошёл проверку целостности."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: str
    files: int
    size: int


def create_backup(
    database_path: str | Path,
    responses_path: str | Path,
    output_directory: str | Path,
    *,
    keep: int = 7,
) -> BackupInfo:
    """Создать атомарный backup и оставить последние ``keep`` копий на сервере."""
    database = Path(database_path).resolve(strict=True)
    if not database.is_file():
        raise BackupError("Путь базы данных не является файлом")
    responses = Path(responses_path).resolve(strict=False)
    output = Path(output_directory).resolve(strict=False)
    if isinstance(keep, bool) or not isinstance(keep, int) or keep <= 0:
        raise ValueError("keep должен быть положительным целым числом")
    _validate_output_directory(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    _set_private_permissions(output, directory=True)

    created = datetime.now(timezone.utc)
    archive_name = (
        f"{BACKUP_PREFIX}{created.strftime('%Y%m%dT%H%M%S%fZ')}-"
        f"{uuid4().hex[:8]}.zip"
    )
    final_path = output / archive_name
    partial_path = output / f".{archive_name}.partial"

    try:
        with tempfile.TemporaryDirectory(prefix="inntophone-backup-") as temporary:
            staging = Path(temporary)
            snapshot = staging / "clients.db"
            _backup_sqlite(database, snapshot)
            staged_responses = staging / "responses"
            _copy_responses(responses, staged_responses)
            manifest = _build_manifest(created, snapshot, staged_responses)
            _write_archive(partial_path, snapshot, staged_responses, manifest)
            verify_backup(partial_path)
        os.replace(partial_path, final_path)
        _set_private_permissions(final_path, directory=False)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise

    _remove_expired_backups(output, keep=keep, preserve=final_path)
    return BackupInfo(
        path=final_path,
        created_at=created.isoformat(),
        files=len(manifest["files"]),
        size=final_path.stat().st_size,
    )


def verify_backup(archive_path: str | Path) -> BackupInfo:
    """Проверить структуру, SHA-256 всех файлов и SQLite integrity_check."""
    archive = Path(archive_path).resolve(strict=True)
    if not archive.is_file():
        raise BackupError("Backup не является файлом")
    with zipfile.ZipFile(archive, "r") as bundle:
        infos = bundle.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise BackupError("Архив содержит повторяющиеся пути")
        for item in infos:
            _validate_member(item)
        if MANIFEST_NAME not in names:
            raise BackupError("Архив не содержит manifest.json")
        try:
            manifest = json.loads(bundle.read(MANIFEST_NAME).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BackupError("manifest.json повреждён") from error
        entries = _validated_manifest(manifest)
        expected_names = {MANIFEST_NAME, *(entry["path"] for entry in entries)}
        if set(names) != expected_names:
            raise BackupError("Состав архива не совпадает с manifest.json")
        for entry in entries:
            content = bundle.read(entry["path"])
            if len(content) != entry["size"]:
                raise BackupError(f"Размер файла {entry['path']} не совпадает")
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise BackupError(f"Контрольная сумма {entry['path']} не совпадает")
        if DATABASE_MEMBER not in expected_names:
            raise BackupError("Архив не содержит SQLite-снимок")
        with tempfile.TemporaryDirectory(prefix="inntophone-verify-") as temporary:
            database_copy = Path(temporary) / "clients.db"
            with database_copy.open("wb") as stream:
                stream.write(bundle.read(DATABASE_MEMBER))
            _check_sqlite(database_copy)
    return BackupInfo(
        path=archive,
        created_at=str(manifest["created_at"]),
        files=len(entries),
        size=archive.stat().st_size,
    )


def _backup_sqlite(source: Path, destination: Path) -> None:
    try:
        with closing(
            sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        ) as source_db:
            with closing(sqlite3.connect(destination)) as destination_db:
                source_db.backup(destination_db)
        _check_sqlite(destination)
    except sqlite3.Error as error:
        raise BackupError("Не удалось создать согласованный SQLite-снимок") from error


def _check_sqlite(database: Path) -> None:
    try:
        with closing(
            sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        ) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise BackupError("SQLite-снимок не открывается") from error
    if result is None or result[0] != "ok":
        raise BackupError("SQLite integrity_check завершился ошибкой")


def _copy_responses(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    if not source.is_dir():
        raise BackupError("Каталог ответов не является директорией")
    source_root = source.resolve(strict=True)
    for item in sorted(source_root.rglob("*")):
        if item.is_symlink():
            raise BackupError("Каталог ответов содержит символическую ссылку")
        if not item.is_file():
            continue
        resolved = item.resolve(strict=True)
        try:
            relative = resolved.relative_to(source_root)
        except ValueError as error:  # pragma: no cover - защита от гонки путей
            raise BackupError("Файл ответов вышел за пределы каталога") from error
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved, target)


def _build_manifest(
    created: datetime,
    database: Path,
    responses: Path,
) -> dict[str, Any]:
    files = [_file_entry(database, DATABASE_MEMBER)]
    for item in sorted(path for path in responses.rglob("*") if path.is_file()):
        relative = item.relative_to(responses).as_posix()
        files.append(_file_entry(item, f"responses/{relative}"))
    return {
        "format": "inntophone-backup",
        "version": 1,
        "created_at": created.isoformat(),
        "files": files,
    }


def _file_entry(path: Path, member: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"path": member, "size": size, "sha256": digest.hexdigest()}


def _write_archive(
    destination: Path,
    database: Path,
    responses: Path,
    manifest: dict[str, Any],
) -> None:
    with zipfile.ZipFile(
        destination,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as bundle:
        bundle.write(database, DATABASE_MEMBER)
        for item in sorted(path for path in responses.rglob("*") if path.is_file()):
            bundle.write(item, f"responses/{item.relative_to(responses).as_posix()}")
        bundle.writestr(
            MANIFEST_NAME,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )


def _validated_manifest(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        raise BackupError("Некорректная структура manifest.json")
    if value.get("format") != "inntophone-backup" or value.get("version") != 1:
        raise BackupError("Неподдерживаемый формат backup")
    if not isinstance(value.get("created_at"), str):
        raise BackupError("manifest не содержит дату создания")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise BackupError("manifest не содержит файлов")
    paths: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise BackupError("Некорректная запись manifest")
        path = entry.get("path")
        size = entry.get("size")
        digest = entry.get("sha256")
        if not isinstance(path, str) or not _safe_member_name(path):
            raise BackupError("Некорректный путь в manifest")
        if path in paths:
            raise BackupError("Manifest содержит повторяющийся путь")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BackupError("Некорректный размер в manifest")
        if not isinstance(digest, str) or len(digest) != 64:
            raise BackupError("Некорректная контрольная сумма в manifest")
        paths.add(path)
    return files


def _validate_member(info: zipfile.ZipInfo) -> None:
    if not _safe_member_name(info.filename) or info.is_dir():
        raise BackupError("Архив содержит небезопасный путь")
    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == 0o120000:
        raise BackupError("Архив содержит символическую ссылку")


def _safe_member_name(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _remove_expired_backups(output: Path, *, keep: int, preserve: Path) -> None:
    candidates = sorted(
        (
            item
            for item in output.glob(f"{BACKUP_PREFIX}*.zip")
            if item.is_file() and item.parent.resolve() == output
        ),
        key=lambda item: item.name,
        reverse=True,
    )
    for old_backup in candidates[keep:]:
        if old_backup.resolve() != preserve.resolve():
            old_backup.unlink()


def _validate_output_directory(output: Path) -> None:
    if output == Path(output.anchor) or output == Path.home().resolve():
        raise BackupError("Нельзя использовать корневой или домашний каталог")


def _set_private_permissions(path: Path, *, directory: bool) -> None:
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        # На Windows доступ ограничивается ACL учетной записи.
        pass


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Резервные копии INNtoPhone")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="создать и проверить backup")
    create.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    create.add_argument("--responses", type=Path, default=DEFAULT_RESPONSES)
    create.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    create.add_argument("--keep", type=int, default=7)
    verify = commands.add_parser("verify", help="проверить скачанный backup")
    verify.add_argument("--archive", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parse_arguments(argv)
    try:
        if arguments.command == "create":
            info = create_backup(
                arguments.database,
                arguments.responses,
                arguments.output,
                keep=arguments.keep,
            )
        else:
            info = verify_backup(arguments.archive)
    except (BackupError, OSError, ValueError) as error:
        print(
            json.dumps(
                {"ok": False, "error": type(error).__name__},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    print(
        json.dumps(
            {
                "ok": True,
                "backup_path": str(info.path),
                "created_at": info.created_at,
                "files": info.files,
                "size": info.size,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
