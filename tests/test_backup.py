import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from src.cli.backup import (
    BACKUP_PREFIX,
    BackupError,
    create_backup,
    verify_backup,
)


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "data" / "clients.db"
        self.database.parent.mkdir()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO clients (name) VALUES (?)", ("Тест",))
            connection.commit()
        self.responses = self.root / "data" / "telegram_reports"
        day = self.responses / "2026-08-09"
        day.mkdir(parents=True)
        (day / "report.txt").write_text("обезличенный ответ", encoding="utf-8")
        self.output = self.root / "server-backups"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_creates_verified_sqlite_and_response_archive(self) -> None:
        info = create_backup(
            self.database,
            self.responses,
            self.output,
            keep=3,
        )

        verified = verify_backup(info.path)
        self.assertEqual(verified.path, info.path)
        self.assertEqual(info.files, 2)
        with zipfile.ZipFile(info.path) as bundle:
            self.assertEqual(
                bundle.read("responses/2026-08-09/report.txt").decode("utf-8"),
                "обезличенный ответ",
            )
            database_copy = self.root / "restored.db"
            database_copy.write_bytes(bundle.read("database/clients.db"))
        with closing(sqlite3.connect(database_copy)) as connection:
            self.assertEqual(
                connection.execute("SELECT name FROM clients").fetchone()[0],
                "Тест",
            )

    def test_missing_responses_directory_still_backs_up_database(self) -> None:
        info = create_backup(
            self.database,
            self.root / "missing-responses",
            self.output,
        )

        self.assertEqual(verify_backup(info.path).files, 1)

    def test_detects_modified_file_even_when_zip_is_readable(self) -> None:
        info = create_backup(self.database, self.responses, self.output)
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(info.path) as source, zipfile.ZipFile(tampered, "w") as target:
            for item in source.infolist():
                content = source.read(item.filename)
                if item.filename.endswith("report.txt"):
                    content = b"x" * len(content)
                target.writestr(item, content)

        with self.assertRaisesRegex(BackupError, "Контрольная сумма"):
            verify_backup(tampered)

    def test_remote_retention_keeps_only_requested_number(self) -> None:
        for _ in range(4):
            create_backup(self.database, self.responses, self.output, keep=2)

        backups = list(self.output.glob(f"{BACKUP_PREFIX}*.zip"))
        self.assertEqual(len(backups), 2)
        for backup in backups:
            verify_backup(backup)

    def test_does_not_include_adjacent_secrets(self) -> None:
        (self.root / ".env").write_text("TOKEN=secret", encoding="utf-8")
        (self.root / "telegram.session").write_bytes(b"secret")

        info = create_backup(self.database, self.responses, self.output)

        with zipfile.ZipFile(info.path) as bundle:
            names = bundle.namelist()
        self.assertFalse(any(".env" in name for name in names))
        self.assertFalse(any("session" in name for name in names))

    def test_manifest_contains_no_source_absolute_paths(self) -> None:
        info = create_backup(self.database, self.responses, self.output)

        with zipfile.ZipFile(info.path) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
        serialized = json.dumps(manifest)
        self.assertNotIn(str(self.root), serialized)
