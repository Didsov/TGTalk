import unittest
from os import environ
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.integrations.telegram.report_downloader import (
    ReportDownloadError,
    allowed_report_hosts,
    download_report_text,
    report_txt_url,
)


class ReportUrlTests(unittest.TestCase):
    def test_appends_txt_and_preserves_query(self) -> None:
        result = report_txt_url(
            "https://dc6.sherlock-report.at/r/token?x=1",
            ("*.sherlock-report.at",),
        )

        self.assertEqual(
            result,
            "https://dc6.sherlock-report.at/r/token/txt?x=1",
        )

    def test_rejects_host_outside_allowlist(self) -> None:
        with self.assertRaises(ReportDownloadError) as context:
            report_txt_url("https://example.com/r/token", ("localhost",))

        self.assertEqual(context.exception.code, "report_host_not_allowed")

    def test_rejects_plain_http_for_remote_host(self) -> None:
        with self.assertRaises(ReportDownloadError) as context:
            report_txt_url(
                "http://dc6.sherlock-report.at/r/token",
                ("dc6.sherlock-report.at:80",),
            )

        self.assertEqual(context.exception.code, "unsafe_report_url")

    def test_local_http_requires_exact_host_and_port(self) -> None:
        result = report_txt_url(
            "http://127.0.0.1:8081/r/token",
            ("127.0.0.1:8081",),
        )

        self.assertEqual(result, "http://127.0.0.1:8081/r/token/txt")

        for allowed_hosts in (
            ("127.0.0.1",),
            ("127.0.0.1:8082",),
            ("localhost:8081",),
            ("*.0.0.1:8081",),
        ):
            with self.subTest(allowed_hosts=allowed_hosts):
                with self.assertRaises(ReportDownloadError) as context:
                    report_txt_url(
                        "http://127.0.0.1:8081/r/token",
                        allowed_hosts,
                    )
                self.assertEqual(
                    context.exception.code, "report_host_not_allowed"
                )

    def test_remote_custom_https_port_requires_exact_host_port(self) -> None:
        report_url = "https://dc6.sherlock-report.at:8443/r/token"
        for allowed_hosts in (
            ("dc6.sherlock-report.at",),
            ("*.sherlock-report.at",),
            ("*.sherlock-report.at:8443",),
            ("dc6.sherlock-report.at:443",),
        ):
            with self.subTest(allowed_hosts=allowed_hosts):
                with self.assertRaises(ReportDownloadError) as context:
                    report_txt_url(report_url, allowed_hosts)
                self.assertEqual(
                    context.exception.code, "report_host_not_allowed"
                )

        result = report_txt_url(
            report_url,
            ("dc6.sherlock-report.at:8443",),
        )
        self.assertEqual(
            result,
            "https://dc6.sherlock-report.at:8443/r/token/txt",
        )

    def test_allowlist_must_be_explicitly_configured(self) -> None:
        with patch.dict(environ, {}, clear=True):
            with self.assertRaisesRegex(
                RuntimeError, "TELEGRAM_REPORT_ALLOWED_HOSTS"
            ):
                allowed_report_hosts()

        with patch.dict(
            environ,
            {"TELEGRAM_REPORT_ALLOWED_HOSTS": " , "},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "не может быть пустым"):
                allowed_report_hosts()

    def test_reads_explicit_allowlist_entries(self) -> None:
        with patch.dict(
            environ,
            {
                "TELEGRAM_REPORT_ALLOWED_HOSTS": (
                    "127.0.0.1:8081, *.sherlock-report.at"
                )
            },
            clear=True,
        ):
            result = allowed_report_hosts()

        self.assertEqual(
            result,
            ("127.0.0.1:8081", "*.sherlock-report.at"),
        )


class ReportDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        app = web.Application()
        app.router.add_get("/r/good/txt", self._good)
        app.router.add_get("/r/large/txt", self._large)
        app.router.add_get("/r/retry/txt", self._retry)
        app.router.add_get("/r/redirect/txt", self._redirect)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.base_url = str(self.client.make_url("/")).rstrip("/")
        parsed_base_url = urlsplit(self.base_url)
        self.allowed_hosts = (
            f"{parsed_base_url.hostname}:{parsed_base_url.port}",
        )

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def _good(self, request: web.Request) -> web.Response:
        return web.Response(text="=== Тест ===\nТелефон: 79990000001\n")

    async def _large(self, request: web.Request) -> web.Response:
        return web.Response(text="x" * 100)

    async def _retry(self, request: web.Request) -> web.Response:
        raise web.HTTPServiceUnavailable()

    async def _redirect(self, request: web.Request) -> web.Response:
        parsed_base_url = urlsplit(self.base_url)
        raise web.HTTPFound(
            location=(
                f"http://localhost:{parsed_base_url.port}/r/good/txt"
            )
        )

    async def test_downloads_local_test_report(self) -> None:
        result = await download_report_text(
            f"{self.base_url}/r/good",
            allowed_hosts=self.allowed_hosts,
        )

        self.assertIn("Телефон:", result)

    async def test_rejects_report_larger_than_limit(self) -> None:
        with self.assertRaises(ReportDownloadError) as context:
            await download_report_text(
                f"{self.base_url}/r/large",
                allowed_hosts=self.allowed_hosts,
                max_bytes=10,
            )

        self.assertEqual(context.exception.code, "report_too_large")
        self.assertFalse(context.exception.retryable)

    async def test_marks_server_error_as_retryable(self) -> None:
        with self.assertRaises(ReportDownloadError) as context:
            await download_report_text(
                f"{self.base_url}/r/retry",
                allowed_hosts=self.allowed_hosts,
            )

        self.assertEqual(context.exception.code, "report_http_503")
        self.assertTrue(context.exception.retryable)

    async def test_revalidates_redirect_host_and_port(self) -> None:
        with self.assertRaises(ReportDownloadError) as context:
            await download_report_text(
                f"{self.base_url}/r/redirect",
                allowed_hosts=self.allowed_hosts,
            )

        self.assertEqual(context.exception.code, "report_host_not_allowed")


if __name__ == "__main__":
    unittest.main()
