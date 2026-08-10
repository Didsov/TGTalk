import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.cli.backfill_contractor_uuids import run_backfill
from src.storage import NewClientStorage

from tests.test_new_client_storage import sbis_client


class ContractorUuidBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_only_clients_found_in_company_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "clients.db"
            storage = NewClientStorage(database)
            storage.initialize()
            storage.upsert_from_sbis(sbis_client(ИдентификаторСПП=1))
            storage.upsert_from_sbis(sbis_client(ИдентификаторСПП=2))
            contractor_uuid = "40bc4f3e-92a4-11f1-81b4-057c77c03283"

            with patch(
                "src.cli.backfill_contractor_uuids.get_company_uuids",
                new=AsyncMock(return_value={1: contractor_uuid}),
            ) as get_uuids:
                result = await run_backfill(database)

            get_uuids.assert_awaited_once_with(
                [1, 2],
                oldest_registration_date=date(2012, 4, 10),
            )
            self.assertEqual(result.requested, 2)
            self.assertEqual(result.found, 1)
            self.assertEqual(result.updated, 1)
            self.assertEqual(storage.get(1).contractor_uuid, contractor_uuid)
            self.assertIsNone(storage.get(2).contractor_uuid)
