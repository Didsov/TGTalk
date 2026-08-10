import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.cli.backfill_director_inn import run_backfill
from src.storage import NewClientStorage

from tests.test_new_client_storage import company_card


class DirectorInnBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_by_spp_id_and_uuid_and_saves_head_director_inn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "clients.db"
            storage = NewClientStorage(database)
            storage.initialize()
            card = company_card()
            card["spp_data"] = dict(card["spp_data"])
            for field in (
                "Директор.Фамилия",
                "Директор.Имя",
                "Директор.Отчество",
                "Директор.ИНН",
            ):
                card["spp_data"].pop(field)
            contractor_uuid = "40bc4f3e-92a4-11f1-81b4-057c77c03283"
            storage.upsert_from_company_card(
                card, contractor_uuid=contractor_uuid
            )
            response = company_card(
                head_data={
                    "spp_data": {
                        "Директор.Фамилия": "Петров",
                        "Директор.Имя": "Пётр",
                        "Директор.Отчество": "Петрович",
                        "Директор.ИНН": "500100732259",
                    }
                }
            )

            with (
                patch(
                    "src.cli.backfill_director_inn.get_company_card",
                    new=AsyncMock(return_value=response),
                ) as get_card,
                patch(
                    "src.cli.backfill_director_inn.asyncio.sleep",
                    new=AsyncMock(),
                ),
            ):
                result = await run_backfill(database, limit=10)

            get_card.assert_awaited_once_with(30852759, contractor_uuid)
            self.assertEqual(result.updated, 1)
            self.assertEqual(result.not_found, 0)
            self.assertEqual(result.failed, 0)
            saved = storage.get(30852759)
            self.assertEqual(saved.director_last_name, "Петров")
            self.assertEqual(saved.director_first_name, "Пётр")
            self.assertEqual(saved.director_middle_name, "Петрович")
            self.assertEqual(saved.director_inn, "500100732259")
