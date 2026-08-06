import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.integrations.sbis.clients import (
    SbisApiError,
    _buildPayload,
    _buildReadCardPayload,
    _decodeTyped,
    _getPage,
    _isValidInn,
    _navigation,
    PAGE_SIZE,
    extractContacts,
    getClientByInn,
    getClientsByListId,
    getContactsByInn,
)


class ProtocolDecoderTests(unittest.TestCase):
    def test_decodes_recordset_to_client_dictionaries(self) -> None:
        raw = {
            "_type": "recordset",
            "s": [
                {"n": "ID", "t": "Число целое"},
                {"n": "ИНН", "t": "Строка"},
            ],
            "d": [[101, "2536000000"], [102, "2536000001"]],
        }

        self.assertEqual(
            _decodeTyped(raw),
            [
                {"ID": 101, "ИНН": "2536000000"},
                {"ID": 102, "ИНН": "2536000001"},
            ],
        )

    def test_builds_list_id_and_position(self) -> None:
        position = {
            "d": ["cursor-2"],
            "s": [{"t": "Строка", "n": "CompositeKey"}],
            "_type": "record",
            "f": 1,
        }
        payload = _buildPayload(100451, position)

        self.assertEqual(payload["method"], "CrmClients.ListClients")
        self.assertEqual(payload["params"]["Фильтр"]["d"][2], 100451)
        self.assertEqual(payload["params"]["Навигация"]["d"][3], position)
        self.assertEqual(
            payload["params"]["Навигация"]["s"][3],
            {"t": "Запись", "n": "Position"},
        )

    def test_builds_initial_position_as_nullable_string(self) -> None:
        payload = _buildPayload(100451, None)

        self.assertIsNone(payload["params"]["Навигация"]["d"][3])
        self.assertEqual(
            payload["params"]["Навигация"]["s"][3],
            {"t": "Строка", "n": "Position"},
        )

    def test_builds_read_card_payload(self) -> None:
        payload = _buildReadCardPayload("500100732259")

        self.assertEqual(payload["method"], "BillingContractor.ReadCard")
        self.assertEqual(payload["params"]["Requisites"]["d"][0], "500100732259")

    def test_reads_next_position_json_from_metadata(self) -> None:
        position = (
            '{"d":[32123002],"s":[{"n":"cursor",'
            '"t":"Число целое"}],"f":0,"_type":"record"}'
        )
        result = {
            "n": True,
            "m": {
                "f": 0,
                "d": [[position]],
                "s": [{"n": "nextPosition", "t": "JSON-объект"}],
                "_type": "record",
            },
            "r": {
                "f": 0,
                "d": [31882],
                "s": [{"n": "cursor", "t": "Число целое"}],
                "_type": "record",
            },
        }

        self.assertEqual(
            _navigation(result),
            (
                True,
                {
                    "d": [position],
                    "s": [{"t": "Строка", "n": "CompositeKey"}],
                    "_type": "record",
                    "f": 1,
                },
            ),
        )


class InnValidationTests(unittest.TestCase):
    def test_accepts_valid_organization_and_person_inn(self) -> None:
        self.assertTrue(_isValidInn("7707083893"))
        self.assertTrue(_isValidInn("500100732259"))

    def test_rejects_invalid_inn(self) -> None:
        for inn in ("", "123", "7707083894", "500100732250", "abcdefghij"):
            with self.subTest(inn=inn):
                self.assertFalse(_isValidInn(inn))


class GetClientByInnTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_browser_request_and_decodes_card(self) -> None:
        response = Mock(status=200)
        response.json = AsyncMock(
            return_value={
                "result": {
                    "_type": "record",
                    "s": [
                        {"n": "ИНН", "t": "Строка"},
                        {"n": "Название", "t": "Строка"},
                    ],
                    "d": ["500100732259", "Тестовая организация"],
                }
            }
        )
        response.release = Mock()
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.post = AsyncMock(return_value=response)

        with (
            patch("src.integrations.sbis.clients.loadEnvironment"),
            patch(
                "src.integrations.sbis.clients.requireSetting",
                side_effect=lambda name: {
                    "SBIS_RPC_URL": "https://rpc.test/",
                    "SBIS_BROWSER_COOKIE": "sid=test-session",
                }[name],
            ),
            patch(
                "src.integrations.sbis.clients.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            result = await getClientByInn("500100732259")

        request = session.post.await_args
        self.assertEqual(
            request.kwargs["headers"]["X-CalledMethod"],
            "BillingContractor.ReadCard",
        )
        self.assertEqual(request.kwargs["headers"]["Cookie"], "sid=test-session")
        self.assertEqual(
            request.kwargs["json"]["params"]["Requisites"]["d"][0],
            "500100732259",
        )
        self.assertEqual(
            result,
            {"ИНН": "500100732259", "Название": "Тестовая организация"},
        )

    async def test_returns_none_for_empty_result(self) -> None:
        response = Mock(status=200)
        response.json = AsyncMock(return_value={"result": None})
        response.release = Mock()
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.post = AsyncMock(return_value=response)

        with (
            patch("src.integrations.sbis.clients.loadEnvironment"),
            patch(
                "src.integrations.sbis.clients.requireSetting",
                side_effect=lambda name: {
                    "SBIS_RPC_URL": "https://rpc.test/",
                    "SBIS_BROWSER_COOKIE": "sid=test-session",
                }[name],
            ),
            patch(
                "src.integrations.sbis.clients.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            result = await getClientByInn("500100732259")

        self.assertIsNone(result)

    async def test_rejects_invalid_inn_before_request(self) -> None:
        with self.assertRaises(ValueError):
            await getClientByInn("123")


class ContactExtractionTests(unittest.TestCase):
    def test_extracts_phone_and_email_from_nested_contact_rows(self) -> None:
        card = {
            "Раздел": {
                "Контакты": [
                    {
                        "ID": "1.contact.10.contractor",
                        "ContactType": "contact",
                        "RowTitle": "+7 (900) 000-00-01",
                        "Actions": ["tel_link", "copy_phone"],
                        "Masked": False,
                    },
                    {
                        "ID": "2.contact.10.contractor",
                        "ContactType": "contact",
                        "RowTitle": "example@example.test",
                        "Actions": ["mail_client", "copy_email"],
                        "Masked": False,
                    },
                ]
            }
        }

        self.assertEqual(
            extractContacts(card),
            [
                {
                    "id": "1.contact.10.contractor",
                    "type": "phone",
                    "value": "+7 (900) 000-00-01",
                    "masked": False,
                },
                {
                    "id": "2.contact.10.contractor",
                    "type": "email",
                    "value": "example@example.test",
                    "masked": False,
                },
            ],
        )

    def test_deduplicates_contacts_and_skips_empty_rows(self) -> None:
        card = {
            "Контакты": [
                {
                    "ContactType": "contact",
                    "RowTitle": "DUPLICATE@example.test",
                    "Actions": ["copy_email"],
                },
                {
                    "ContactType": "contact",
                    "RowTitle": "duplicate@example.test",
                    "Actions": ["copy_email"],
                },
                {"ContactType": "contact", "RowTitle": ""},
            ]
        }

        self.assertEqual(len(extractContacts(card)), 1)


class GetContactsByInnTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_contacts_from_received_card(self) -> None:
        card = {
            "Контакты": [
                {
                    "ContactType": "contact",
                    "RowTitle": "+7 (900) 000-00-02",
                    "Actions": ["copy_phone"],
                }
            ]
        }

        with patch(
            "src.integrations.sbis.clients.getClientByInn",
            new=AsyncMock(return_value=card),
        ) as get_client:
            result = await getContactsByInn("500100732259")

        get_client.assert_awaited_once_with("500100732259")
        self.assertEqual(result[0]["type"], "phone")

    async def test_returns_empty_list_when_card_not_found(self) -> None:
        with patch(
            "src.integrations.sbis.clients.getClientByInn",
            new=AsyncMock(return_value=None),
        ):
            self.assertEqual(await getContactsByInn("500100732259"), [])


class GetClientsByListIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_cookie_in_request_header(self) -> None:
        response = Mock(status=200)
        response.json = AsyncMock(
            return_value={
                "result": {
                    "_type": "recordset",
                    "s": [{"n": "ID", "t": "Число целое"}],
                    "d": [[101]],
                    "n": False,
                }
            }
        )
        response.release = Mock()
        session = Mock()
        session.post = AsyncMock(return_value=response)

        clients, has_more, position = await _getPage(
            session,
            "https://rpc.test/",
            "sid=test-session",
            100451,
            None,
        )

        request = session.post.await_args
        self.assertEqual(request.kwargs["headers"]["Cookie"], "sid=test-session")
        self.assertNotIn("Cookie", request.kwargs["json"])
        self.assertEqual(clients, [{"ID": 101}])
        self.assertFalse(has_more)
        self.assertIsNone(position)

    async def test_rejects_invalid_list_id(self) -> None:
        for value in (0, -1, True, "100451"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    await getClientsByListId(value)

    async def test_collects_all_pages_with_browser_cookie(self) -> None:
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("src.integrations.sbis.clients.loadEnvironment"),
            patch(
                "src.integrations.sbis.clients.requireSetting",
                side_effect=lambda name: {
                    "SBIS_RPC_URL": "https://rpc.test/",
                    "SBIS_BROWSER_COOKIE": "sid=test-session",
                }[name],
            ),
            patch(
                "src.integrations.sbis.clients.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.clients._getPage",
                new=AsyncMock(
                    side_effect=[
                        ([{"ID": 1}], True, "next"),
                        ([{"ID": 2}], False, None),
                    ]
                ),
            ) as get_page,
        ):
            result = await getClientsByListId(100451)

        self.assertEqual(result, [{"ID": 1}, {"ID": 2}])
        self.assertEqual(get_page.await_count, 2)
        self.assertEqual(get_page.await_args_list[0].args[2], "sid=test-session")

    async def test_stops_when_full_page_has_no_next_position(self) -> None:
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("src.integrations.sbis.clients.loadEnvironment"),
            patch(
                "src.integrations.sbis.clients.requireSetting",
                side_effect=lambda name: {
                    "SBIS_RPC_URL": "https://rpc.test/",
                    "SBIS_BROWSER_COOKIE": "sid=test-session",
                }[name],
            ),
            patch(
                "src.integrations.sbis.clients.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.clients._getPage",
                new=AsyncMock(return_value=([{}] * PAGE_SIZE, True, None)),
            ),
        ):
            result = await getClientsByListId(100451)

        self.assertEqual(result, [{}] * PAGE_SIZE)

    async def test_accepts_short_last_page_when_has_more_is_stale(self) -> None:
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        page = [{"ID": index} for index in range(11)]

        with (
            patch("src.integrations.sbis.clients.loadEnvironment"),
            patch(
                "src.integrations.sbis.clients.requireSetting",
                side_effect=lambda name: {
                    "SBIS_RPC_URL": "https://rpc.test/",
                    "SBIS_BROWSER_COOKIE": "sid=test-session",
                }[name],
            ),
            patch(
                "src.integrations.sbis.clients.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "src.integrations.sbis.clients._getPage",
                new=AsyncMock(return_value=(page, True, None)),
            ) as get_page,
        ):
            result = await getClientsByListId(99788)

        self.assertEqual(result, page)
        get_page.assert_awaited_once()
