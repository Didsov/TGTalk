import unittest
from datetime import date
from unittest.mock import AsyncMock, Mock, patch

from src.integrations.sbis.clients import SbisApiError
from src.integrations.sbis.companies import (
    COMPANY_PAGE_SIZE,
    _build_company_card_payload,
    _build_company_payload,
    _get_company_page,
    _save_companies,
    get_company_card,
    get_company_uuids,
    get_open_companies_by_date,
)


class CompanyPayloadTests(unittest.IsolatedAsyncioTestCase):
    def test_changes_only_page_number_in_navigation(self) -> None:
        first = _build_company_payload(0)
        second = _build_company_payload(1)

        self.assertEqual(first["params"]["Навигация"]["d"], [True, 40, 0])
        self.assertEqual(second["params"]["Навигация"]["d"], [True, 40, 1])
        self.assertEqual(COMPANY_PAGE_SIZE, 40)
        first["params"]["Навигация"]["d"][2] = 1
        self.assertEqual(first, second)

    async def test_page_uses_converter_and_result_n(self) -> None:
        response = Mock(status=200)
        response.json = AsyncMock(
            return_value={
                "result": {
                    "d": [[1, "2026-08-07"]],
                    "s": [
                        {"n": "ИдентификаторСПП", "t": "Число целое"},
                        {"n": "ДатаРегистрации", "t": "Дата"},
                    ],
                    "n": True,
                }
            }
        )
        response.release = Mock()
        session = Mock()
        session.post = AsyncMock(return_value=response)

        records, has_more = await _get_company_page(session, "sid=test", 2)

        self.assertEqual(
            records,
            [{"ИдентификаторСПП": 1, "ДатаРегистрации": "2026-08-07"}],
        )
        self.assertTrue(has_more)
        request = session.post.await_args
        self.assertEqual(request.kwargs["headers"]["Cookie"], "sid=test")
        self.assertEqual(request.kwargs["json"]["params"]["Навигация"]["d"][2], 2)

    def test_builds_company_card_payload_from_list_fields(self) -> None:
        payload = _build_company_card_payload(
            92043200,
            "40bc4f3e-92a4-11f1-81b4-057c77c03283",
        )

        self.assertEqual(payload["method"], "ContractorCard.Read")
        self.assertEqual(payload["params"]["ИдО"], -92043200)
        self.assertEqual(
            payload["params"]["ДопПоля"]["ContractorUUID"],
            "40bc4f3e-92a4-11f1-81b4-057c77c03283",
        )

    def test_rejects_invalid_company_card_identifiers(self) -> None:
        with self.assertRaises(ValueError):
            _build_company_card_payload(0, "40bc4f3e-92a4-11f1-81b4-057c77c03283")
        with self.assertRaises(ValueError):
            _build_company_card_payload(92043200, "not-a-uuid")


class CompanyCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_and_converts_company_card(self) -> None:
        response = Mock(status=200)
        response.json = AsyncMock(
            return_value={
                "result": {
                    "d": ["1234567890", 92043200],
                    "s": [
                        {"n": "ИНН", "t": "Строка"},
                        {"n": "ИдентификаторСПП", "t": "Число целое"},
                    ],
                    "_type": "record",
                }
            }
        )
        response.release = Mock()
        session = Mock()
        session.post = AsyncMock(return_value=response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch(
                "src.integrations.sbis.companies.requireSetting",
                return_value="sid=test",
            ),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            result = await get_company_card(
                92043200,
                "40bc4f3e-92a4-11f1-81b4-057c77c03283",
            )

        self.assertEqual(
            result,
            {"ИНН": "1234567890", "ИдентификаторСПП": 92043200},
        )
        request = session.post.await_args.kwargs
        self.assertEqual(request["headers"]["X-CalledMethod"], "ContractorCard.Read")
        self.assertEqual(
            request["headers"]["X-OriginalMethodName"],
            "Q29udHJhY3RvckNhcmQuUmVhZA==",
        )
        self.assertEqual(request["json"]["params"]["ИдО"], -92043200)
        response.release.assert_called_once_with()

    async def test_recursively_converts_spp_and_personalised_contacts(self) -> None:
        nested_record = {
            "d": ["Иванов", "500100732259"],
            "s": [
                {"n": "Директор.Фамилия"},
                {"n": "Директор.ИНН"},
            ],
            "_type": "record",
        }
        contacts = {
            "d": [[["+79990000001"], ["mail@example.test"]]],
            "s": [{"n": "Phones"}, {"n": "Emails"}],
            "_type": "recordset",
        }
        response = Mock(status=200)
        response.json = AsyncMock(
            return_value={
                "result": {
                    "d": [nested_record, {"d": [contacts], "s": [
                        {"n": "Контрагент.GetPersonalisedContacts"}
                    ], "_type": "record"}],
                    "s": [{"n": "spp_data"}, {"n": "extra_data"}],
                    "_type": "record",
                }
            }
        )
        response.release = Mock()
        session = Mock()
        session.post = AsyncMock(return_value=response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch("src.integrations.sbis.companies.requireSetting", return_value="sid"),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            result = await get_company_card(
                92043200,
                "40bc4f3e-92a4-11f1-81b4-057c77c03283",
            )

        self.assertEqual(result["spp_data"]["Директор.Фамилия"], "Иванов")
        self.assertEqual(
            result["extra_data"]["Контрагент.GetPersonalisedContacts"][0]["Phones"],
            ["+79990000001"],
        )

    async def test_retries_once_after_http_429(self) -> None:
        limited = Mock(status=429, headers={"Retry-After": "3"})
        limited.release = Mock()
        success = Mock(status=200)
        success.json = AsyncMock(
            return_value={
                "result": {
                    "d": ["1234567890"],
                    "s": [{"n": "ИНН"}],
                    "_type": "record",
                }
            }
        )
        success.release = Mock()
        session = Mock()
        session.post = AsyncMock(side_effect=[limited, success])
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch("src.integrations.sbis.companies.requireSetting", return_value="sid"),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            result = await get_company_card(
                92043200,
                "40bc4f3e-92a4-11f1-81b4-057c77c03283",
            )

        self.assertEqual(result["ИНН"], "1234567890")
        self.assertEqual(session.post.await_count, 2)
        sleep.assert_awaited_once_with(60.0)
        limited.release.assert_called_once_with()

    async def test_waits_full_minute_for_each_repeated_429(self) -> None:
        limited_responses = []
        for _ in range(4):
            response = Mock(status=429, headers={})
            response.release = Mock()
            limited_responses.append(response)
        success = Mock(status=200)
        success.json = AsyncMock(
            return_value={
                "result": {
                    "d": ["1234567890"],
                    "s": [{"n": "ИНН"}],
                    "_type": "record",
                }
            }
        )
        success.release = Mock()
        session = Mock()
        session.post = AsyncMock(
            side_effect=[*limited_responses, success]
        )
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch("src.integrations.sbis.companies.requireSetting", return_value="sid"),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            result = await get_company_card(
                92043200,
                "40bc4f3e-92a4-11f1-81b4-057c77c03283",
            )

        self.assertEqual(result["ИНН"], "1234567890")
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [60.0, 60.0, 60.0, 60.0],
        )


class CompanyUuidLookupTests(unittest.IsolatedAsyncioTestCase):
    UUID = "40bc4f3e-92a4-11f1-81b4-057c77c03283"

    async def test_stops_before_page_rows_older_than_oldest_target(self) -> None:
        pages = [
            (
                [
                    {
                        "ИдентификаторСПП": 1,
                        "UUID": self.UUID,
                        "ДатаРегистрации": "2026-08-09",
                    },
                    {
                        "ИдентификаторСПП": 2,
                        "UUID": self.UUID,
                        "ДатаРегистрации": "2026-08-08",
                    },
                ],
                True,
            ),
            (
                [
                    {
                        "ИдентификаторСПП": 999,
                        "UUID": self.UUID,
                        "ДатаРегистрации": "2026-08-06",
                    }
                ],
                True,
            ),
        ]
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch(
                "src.integrations.sbis.companies.requireSetting",
                return_value="sid",
            ),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies._get_company_page",
                new=AsyncMock(side_effect=pages),
            ) as get_page,
        ):
            result = await get_company_uuids(
                [1, 2, 3],
                oldest_registration_date=date(2026, 8, 7),
            )

        self.assertEqual(result, {1: self.UUID, 2: self.UUID})
        self.assertEqual(get_page.await_count, 2)

    async def test_stops_immediately_after_all_targets_are_found(self) -> None:
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        get_page = AsyncMock(
            return_value=(
                [
                    {
                        "ИдентификаторСПП": 1,
                        "UUID": self.UUID,
                        "ДатаРегистрации": "2026-08-09",
                    }
                ],
                True,
            )
        )
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch(
                "src.integrations.sbis.companies.requireSetting",
                return_value="sid",
            ),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies._get_company_page",
                new=get_page,
            ),
        ):
            result = await get_company_uuids(
                [1],
                oldest_registration_date=date(2026, 8, 7),
            )

        self.assertEqual(result, {1: self.UUID})
        get_page.assert_awaited_once()


class OpenCompaniesByDateTests(unittest.IsolatedAsyncioTestCase):
    UUID = "40bc4f3e-92a4-11f1-81b4-057c77c03283"

    def setUp(self) -> None:
        self.delay_patcher = patch(
            "src.integrations.sbis.companies.COMPANY_CARD_REQUEST_DELAY_SECONDS",
            0,
        )
        self.delay_patcher.start()
        self.storage = Mock()
        self.storage.get.return_value = None
        self.storage_patcher = patch(
            "src.integrations.sbis.companies.NewClientStorage",
            return_value=self.storage,
        )
        self.storage_patcher.start()

    def tearDown(self) -> None:
        self.storage_patcher.stop()
        self.delay_patcher.stop()

    def _record(self, spp_id: int, registration_date: str) -> dict:
        return {
            "ИдентификаторСПП": spp_id,
            "UUID": self.UUID,
            "ДатаРегистрации": registration_date,
        }

    def _session(self) -> Mock:
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        return session

    async def test_collects_target_day_across_pages_and_stops_on_older(self) -> None:
        pages = [
            (
                [
                    self._record(1, "2026-08-08T01:00:00"),
                    self._record(2, "2026-08-07T23:59:59"),
                ],
                True,
            ),
            (
                [
                    self._record(3, "2026-08-07T00:00:01"),
                    self._record(4, "2026-08-06T23:59:59"),
                ],
                True,
            ),
        ]
        session = self._session()
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch(
                "src.integrations.sbis.companies.requireSetting",
                return_value="sid=test",
            ),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies._get_company_page",
                new=AsyncMock(side_effect=pages),
            ) as get_page,
            patch(
                "src.integrations.sbis.companies.get_company_card",
                new=AsyncMock(side_effect=lambda spp_id, uuid: {"ID": spp_id}),
            ) as get_card,
        ):
            result = await get_open_companies_by_date(date(2026, 8, 7))

        self.assertEqual([record["ID"] for record in result], [2, 3])
        self.assertEqual(get_page.await_count, 2)
        self.assertEqual(get_page.await_args_list[0].args[2], 0)
        self.assertEqual(get_page.await_args_list[1].args[2], 1)
        self.assertEqual(get_card.await_count, 2)
        self.assertEqual(self.storage.upsert_from_company_card.call_count, 2)
        self.storage.upsert_from_company_card.assert_any_call(
            {"ID": 2}, contractor_uuid=self.UUID
        )
        self.storage.upsert_from_company_card.assert_any_call(
            {"ID": 3}, contractor_uuid=self.UUID
        )

    async def test_stops_naturally_when_result_n_is_false(self) -> None:
        session = self._session()
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch("src.integrations.sbis.companies.requireSetting", return_value="sid"),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies._get_company_page",
                new=AsyncMock(
                    return_value=(
                        [self._record(1, "07.08.2026")], False
                    )
                ),
            ) as get_page,
            patch(
                "src.integrations.sbis.companies.get_company_card",
                new=AsyncMock(return_value={"ID": 1}),
            ),
        ):
            result = await get_open_companies_by_date("2026-08-07")

        self.assertEqual([record["ID"] for record in result], [1])
        get_page.assert_awaited_once()
        self.storage.upsert_from_company_card.assert_called_once_with(
            {"ID": 1}, contractor_uuid=self.UUID
        )

    async def test_uses_today_when_date_is_omitted(self) -> None:
        today = date.today()
        session = self._session()
        record = self._record(1, today.isoformat())
        card = {"ID": 1}
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch("src.integrations.sbis.companies.requireSetting", return_value="sid"),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies._get_company_page",
                new=AsyncMock(return_value=([record], False)),
            ),
            patch(
                "src.integrations.sbis.companies.get_company_card",
                new=AsyncMock(return_value=card),
            ),
        ):
            result = await get_open_companies_by_date()

        self.assertEqual(result, [card])
        self.storage.upsert_from_company_card.assert_called_once_with(
            card, contractor_uuid=self.UUID
        )

    async def test_pauses_one_minute_after_twenty_cards(self) -> None:
        records = [self._record(index, "2026-08-07") for index in range(1, 22)]
        session = self._session()
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch("src.integrations.sbis.companies.requireSetting", return_value="sid"),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies._get_company_page",
                new=AsyncMock(return_value=(records, False)),
            ),
            patch(
                "src.integrations.sbis.companies.get_company_card",
                new=AsyncMock(side_effect=lambda spp_id, uuid: {"ID": spp_id}),
            ),
            patch(
                "src.integrations.sbis.companies.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            result = await get_open_companies_by_date("2026-08-07")

        self.assertEqual(len(result), 21)
        self.assertIn(
            60.0,
            [call.args[0] for call in sleep.await_args_list],
        )

    async def test_skips_company_card_already_present_in_database(self) -> None:
        existing = self._record(1, "2026-08-07")
        missing = self._record(2, "2026-08-07")
        session = self._session()
        self.storage.get.side_effect = [Mock(spp_id=1), None]
        get_card = AsyncMock(return_value={"ID": 2})
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch("src.integrations.sbis.companies.requireSetting", return_value="sid"),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies._get_company_page",
                new=AsyncMock(return_value=([existing, missing], False)),
            ),
            patch(
                "src.integrations.sbis.companies.get_company_card",
                new=get_card,
            ),
            patch("builtins.print") as output,
        ):
            result = await get_open_companies_by_date("2026-08-07")

        self.assertEqual(result, [{"ID": 2}])
        get_card.assert_awaited_once_with(2, self.UUID)
        self.storage.upsert_from_company_card.assert_called_once_with(
            {"ID": 2}, contractor_uuid=self.UUID
        )
        self.storage.set_contractor_uuid_if_missing.assert_called_once_with(
            1, self.UUID
        )
        self.assertTrue(
            any(
                "уже есть в БД; пропущена" in str(call.args[0])
                for call in output.call_args_list
            )
        )

    async def test_rejects_broken_descending_sort_order(self) -> None:
        session = self._session()
        with (
            patch("src.integrations.sbis.companies.loadEnvironment"),
            patch("src.integrations.sbis.companies.requireSetting", return_value="sid"),
            patch(
                "src.integrations.sbis.companies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.companies._get_company_page",
                new=AsyncMock(
                    return_value=(
                        [
                            {"ДатаРегистрации": "2026-08-07"},
                            {"ДатаРегистрации": "2026-08-08"},
                        ],
                        False,
                    )
                ),
            ),
        ):
            with self.assertRaises(SbisApiError):
                await get_open_companies_by_date("2026-08-07")

        self.storage.upsert_from_company_card.assert_not_called()


class SaveCompaniesTests(unittest.TestCase):
    def test_initializes_database_and_saves_records(self) -> None:
        records = [{"ИдентификаторСПП": 1}]
        storage = Mock()
        with patch(
            "src.integrations.sbis.companies.NewClientStorage",
            return_value=storage,
        ) as storage_class:
            _save_companies(records, "test-clients.db")

        storage_class.assert_called_once_with("test-clients.db")
        storage.initialize.assert_called_once_with()
        storage.save_company_cards.assert_called_once_with(records)


if __name__ == "__main__":
    unittest.main()
