"""Безопасная загрузка TXT-отчётов по URL из Telegram-кнопки."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp


DEFAULT_MAX_REPORT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ReportDownloadError(RuntimeError):
    code: str
    retryable: bool

    def __str__(self) -> str:
        return self.code


def allowed_report_hosts() -> tuple[str, ...]:
    """Прочитать явно заданный allowlist адресов отчётов."""
    raw_value = os.getenv("TELEGRAM_REPORT_ALLOWED_HOSTS")
    if raw_value is None:
        raise RuntimeError(
            "Не задана переменная окружения TELEGRAM_REPORT_ALLOWED_HOSTS"
        )
    hosts = tuple(
        item.strip().casefold()
        for item in raw_value.split(",")
        if item.strip()
    )
    if not hosts:
        raise RuntimeError("TELEGRAM_REPORT_ALLOWED_HOSTS не может быть пустым")
    return hosts


def report_txt_url(report_url: str, allowed_hosts: tuple[str, ...]) -> str:
    """Проверить недоверенный URL и добавить к его пути `/txt`."""
    parsed = _validated_url(report_url, allowed_hosts)
    path = parsed.path.rstrip("/") + "/txt"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


async def download_report_text(
    report_url: str,
    *,
    allowed_hosts: tuple[str, ...] | None = None,
    timeout: float = 30,
    max_bytes: int = DEFAULT_MAX_REPORT_BYTES,
    max_redirects: int = 3,
) -> str:
    """Скачать небольшой текстовый отчёт, проверяя каждый redirect."""
    hosts = allowed_report_hosts() if allowed_hosts is None else allowed_hosts
    current_url = report_txt_url(report_url, hosts)
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            for redirect_number in range(max_redirects + 1):
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    headers={"Accept": "text/plain, */*; q=0.1"},
                ) as response:
                    if 300 <= response.status < 400:
                        location = response.headers.get("Location")
                        if not location:
                            raise ReportDownloadError(
                                "redirect_without_location", False
                            )
                        if redirect_number >= max_redirects:
                            raise ReportDownloadError("too_many_redirects", False)
                        next_url = urljoin(current_url, location)
                        _validated_url(next_url, hosts)
                        current_url = next_url
                        continue

                    if response.status == 429 or response.status >= 500:
                        raise ReportDownloadError(
                            f"report_http_{response.status}", True
                        )
                    if response.status >= 400:
                        raise ReportDownloadError(
                            f"report_http_{response.status}", False
                        )

                    content_length = response.content_length
                    if content_length is not None and content_length > max_bytes:
                        raise ReportDownloadError("report_too_large", False)

                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise ReportDownloadError("report_too_large", False)
                    charset = response.charset or "utf-8"
                    try:
                        return bytes(body).decode(charset)
                    except (LookupError, UnicodeDecodeError) as error:
                        raise ReportDownloadError(
                            "report_invalid_encoding", False
                        ) from error
    except ReportDownloadError:
        raise
    except (aiohttp.ClientError, TimeoutError) as error:
        raise ReportDownloadError("report_network_error", True) from error

    raise ReportDownloadError("report_download_failed", True)  # pragma: no cover


def _validated_url(report_url: str, allowed_hosts: tuple[str, ...]):
    try:
        parsed = urlsplit(report_url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError as error:
        raise ReportDownloadError("unsafe_report_url", False) from error

    if parsed.username or parsed.password or not host:
        raise ReportDownloadError("unsafe_report_url", False)
    if parsed.scheme not in {"http", "https"}:
        raise ReportDownloadError("unsafe_report_url", False)
    is_local = _is_local_host(host)
    if parsed.scheme == "http" and not is_local:
        raise ReportDownloadError("unsafe_report_url", False)
    if port is not None and not 1 <= port <= 65535:
        raise ReportDownloadError("unsafe_report_url", False)

    effective_port = port or (443 if parsed.scheme == "https" else 80)
    if not _endpoint_allowed(host, effective_port, is_local, allowed_hosts):
        raise ReportDownloadError("report_host_not_allowed", False)
    return parsed


def _is_local_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _endpoint_allowed(
    host: str,
    port: int,
    is_local: bool,
    allowed_hosts: tuple[str, ...],
) -> bool:
    for allowed in allowed_hosts:
        parsed_allowed = _parse_allowed_endpoint(allowed)
        if parsed_allowed is None:
            continue
        allowed_host, allowed_port = parsed_allowed
        is_wildcard = allowed_host.startswith("*.")

        # Доступ к loopback всегда требует точного host:port без wildcard.
        if is_local:
            if is_wildcard or allowed_port is None:
                continue
            if allowed_host == host and allowed_port == port:
                return True
            continue

        # Bare host и wildcard разрешают только стандартный HTTPS-порт.
        if allowed_port is None:
            if port != 443:
                continue
        elif allowed_port != port:
            continue

        # Нестандартный порт требует точного host:port.
        if port != 443 and is_wildcard:
            continue
        if _host_matches(host, allowed_host):
            return True
    return False


def _parse_allowed_endpoint(value: str) -> tuple[str, int | None] | None:
    candidate = str(value).strip().casefold()
    if not candidate:
        return None
    try:
        parsed = urlsplit(f"//{candidate}")
        host = (parsed.hostname or "").rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (
        not host
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return host, port


def _host_matches(host: str, allowed_host: str) -> bool:
    if allowed_host.startswith("*."):
        return host.endswith(allowed_host[1:])
    return host == allowed_host
