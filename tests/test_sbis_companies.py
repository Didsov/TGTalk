import unittest
from datetime import date
from unittest.mock import AsyncMock, Mock, patch

from src.integrations.sbis.clients import SbisApiError
from src.integrations.sbis.companies import (
    COMPANY_PAGE_SIZE,
    _build_company_payload,
    _get_company_page,
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


class OpenCompaniesByDateTests(unittest.IsolatedAsyncioTestCase):
    def _session(self) -> Mock:
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        return session

    async def test_collects_target_day_across_pages_and_stops_on_older(self) -> None:
        pages = [
            (
                [
                    {"ID": 1, "ДатаРегистрации": "2026-08-08T01:00:00"},
                    {"ID": 2, "ДатаРегистрации": "2026-08-07T23:59:59"},
                ],
                True,
            ),
            (
                [
                    {"ID": 3, "ДатаРегистрации": "2026-08-07T00:00:01"},
                    {"ID": 4, "ДатаРегистрации": "2026-08-06T23:59:59"},
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
        ):
            result = await get_open_companies_by_date(date(2026, 8, 7))

        self.assertEqual([record["ID"] for record in result], [2, 3])
        self.assertEqual(get_page.await_count, 2)
        self.assertEqual(get_page.await_args_list[0].args[2], 0)
        self.assertEqual(get_page.await_args_list[1].args[2], 1)

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
                        [{"ID": 1, "ДатаРегистрации": "07.08.2026"}], False
                    )
                ),
            ) as get_page,
        ):
            result = await get_open_companies_by_date("2026-08-07")

        self.assertEqual([record["ID"] for record in result], [1])
        get_page.assert_awaited_once()

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


if __name__ == "__main__":
    unittest.main()
