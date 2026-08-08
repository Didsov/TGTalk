"""Последовательный поиск контактов одного клиента через Telegram-бота."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from telethon.errors import FloodWaitError

from src.domain import is_valid_inn
from src.integrations.telegram.bot_client import (
    BotResponseKind,
    classifyBotResponse,
    clickRussiaAndWait,
    extractReportUrlAsync,
    getAvailableQueries,
    sendQueryAndWait,
)
from src.integrations.telegram.report_downloader import (
    ReportDownloadError,
    download_report_text,
)
from src.integrations.telegram.report_parser import (
    CandidateSelectionStatus,
    parse_report,
)
from src.storage.new_clients import NewClient, NewClientStorage, ProcessingStatus


ReportLoader = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class EnrichmentOutcome:
    client_spp_id: int
    status: ProcessingStatus
    phones: tuple[str, ...]
    emails: tuple[str, ...]
    stage: str
    result_code: str
    error_code: str | None = None
    retry_after_seconds: int | None = None
    requests_spent: int = 0


class AvailableQueriesExhausted(RuntimeError):
    """Платный запрос не отправлен из-за исчерпанного баланса."""


async def enrich_client(
    client: NewClient,
    conversation: Any,
    *,
    timeout: float = 30,
    report_loader: ReportLoader = download_report_text,
    available_queries: int | None = None,
) -> EnrichmentOutcome:
    """Обогатить клиента и вернуть фактическое число платных запросов."""
    requests_spent = 0

    async def paid_query(
        current_conversation: Any,
        text: str,
        *,
        timeout: float,
    ) -> Any:
        nonlocal requests_spent
        if available_queries is not None and requests_spent >= available_queries:
            raise AvailableQueriesExhausted
        requests_spent += 1
        return await sendQueryAndWait(
            current_conversation,
            text,
            timeout=timeout,
        )

    outcome = await _enrich_client(
        client,
        conversation,
        timeout=timeout,
        report_loader=report_loader,
        query_sender=paid_query,
    )
    return replace(outcome, requests_spent=requests_spent)


async def _enrich_client(
    client: NewClient,
    conversation: Any,
    *,
    timeout: float,
    report_loader: ReportLoader,
    query_sender: Callable[..., Awaitable[Any]],
) -> EnrichmentOutcome:
    """Искать по ИНН директора, почтам СБИС, затем ФИО и дате рождения."""
    phones = list(client.telegram_phones)
    emails = list(client.telegram_emails)

    if client.director_inn is None:
        return _outcome(
            client,
            ProcessingStatus.SKIPPED,
            phones,
            emails,
            "director_inn_validation",
            "director_inn_missing",
        )
    if not is_valid_inn(client.director_inn):
        return _outcome(
            client,
            ProcessingStatus.SKIPPED,
            phones,
            emails,
            "director_inn_validation",
            "invalid_director_inn",
        )

    director_name = _director_name(client)
    if director_name is None:
        return _outcome(
            client,
            ProcessingStatus.SKIPPED,
            phones,
            emails,
            "director_name_validation",
            "director_name_missing",
        )

    try:
        inn_response = await query_sender(
            conversation, f"/inn {client.director_inn}", timeout=timeout
        )
    except AvailableQueriesExhausted:
        return _queries_exhausted(client, phones, emails, "inn_query")
    except FloodWaitError as error:
        return _flood_wait_outcome(
            client, phones, emails, "inn_query", error
        )
    except TimeoutError:
        return _outcome(
            client,
            ProcessingStatus.RETRY_REQUIRED,
            phones,
            emails,
            "inn_query",
            "temporary_error",
            "telegram_timeout",
        )

    response_kind = classifyBotResponse(inn_response)
    first_report = None
    if response_kind is BotResponseKind.RETRYABLE_ERROR:
        return _temporary_bot_response(client, phones, emails, "inn_query")
    if response_kind is BotResponseKind.UNKNOWN:
        return _manual_review(client, phones, emails, "inn_query", "unknown_response")
    if response_kind is not BotResponseKind.NOT_FOUND:
        first_url = await extractReportUrlAsync(inn_response)
        if first_url is None:
            return _manual_review(
                client, phones, emails, "inn_query", "report_url_missing"
            )

        first_text, failure = await _load_report(
            client, first_url, phones, emails, "inn_report_download", report_loader
        )
        if failure is not None:
            return failure
        if first_text is None or not first_text.strip():
            return _manual_review(
                client, phones, emails, "inn_report_parse", "empty_report"
            )

        try:
            first_report = parse_report(
                first_text,
                source_inn=client.director_inn,
                expected_director_name=director_name,
            )
        except (TypeError, ValueError):
            return _manual_review(
                client, phones, emails, "inn_report_parse", "invalid_report"
            )
        _extend_unique(phones, first_report.phones)
        _extend_unique(emails, first_report.emails)
        if first_report.phones:
            return _outcome(
                client,
                ProcessingStatus.PROCESSED,
                phones,
                emails,
                "inn_report_parse",
                "phone_found_by_inn",
            )

    personalised_email = (
        client.personalised_emails[0] if client.personalised_emails else None
    )
    if personalised_email is not None:
        try:
            email_response = await query_sender(
                conversation, personalised_email, timeout=timeout
            )
        except AvailableQueriesExhausted:
            return _queries_exhausted(client, phones, emails, "email_query")
        except FloodWaitError as error:
            return _flood_wait_outcome(
                client, phones, emails, "email_query", error
            )
        except TimeoutError:
            return _outcome(
                client,
                ProcessingStatus.RETRY_REQUIRED,
                phones,
                emails,
                "email_query",
                "temporary_error",
                "telegram_timeout",
            )

        response_kind = classifyBotResponse(email_response)
        if response_kind is BotResponseKind.RETRYABLE_ERROR:
            return _temporary_bot_response(client, phones, emails, "email_query")
        if response_kind is BotResponseKind.UNKNOWN:
            return _manual_review(
                client, phones, emails, "email_query", "unknown_response"
            )
        if response_kind is not BotResponseKind.NOT_FOUND:
            email_url = await extractReportUrlAsync(email_response)
            if email_url is None:
                return _manual_review(
                    client, phones, emails, "email_query", "report_url_missing"
                )
            email_text, failure = await _load_report(
                client,
                email_url,
                phones,
                emails,
                "email_report_download",
                report_loader,
            )
            if failure is not None:
                return failure
            if email_text is None or not email_text.strip():
                return _manual_review(
                    client, phones, emails, "email_report_parse", "empty_report"
                )
            try:
                email_report = parse_report(
                    email_text,
                    source_inn=client.director_inn,
                    expected_director_name=director_name,
                )
            except (TypeError, ValueError):
                return _manual_review(
                    client, phones, emails, "email_report_parse", "invalid_report"
                )
            _extend_unique(phones, email_report.phones)
            _extend_unique(emails, email_report.emails)
            if email_report.phones:
                return _outcome(
                    client,
                    ProcessingStatus.PROCESSED,
                    phones,
                    emails,
                    "email_report_parse",
                    "phone_found_by_email",
                )

    if first_report is None:
        return _outcome(
            client,
            ProcessingStatus.SKIPPED,
            phones,
            emails,
            "person_candidate",
            "person_not_available",
        )

    selection = first_report.candidate_selection
    if selection.status is CandidateSelectionStatus.AMBIGUOUS:
        return _manual_review(
            client, phones, emails, "person_candidate", "ambiguous_person"
        )
    if selection.status is CandidateSelectionStatus.NONE:
        return _outcome(
            client,
            ProcessingStatus.SKIPPED,
            phones,
            emails,
            "person_candidate",
            "person_not_available",
        )

    candidate = selection.candidate
    if candidate is None:  # pragma: no cover - защищает контракт парсера
        return _manual_review(
            client, phones, emails, "person_candidate", "invalid_candidate"
        )
    person_query = (
        f"{candidate.full_name} {candidate.date_of_birth.strftime('%d.%m.%Y')}"
    )

    try:
        country_response = await query_sender(
            conversation, person_query, timeout=timeout
        )
    except AvailableQueriesExhausted:
        return _queries_exhausted(client, phones, emails, "person_query")
    except FloodWaitError as error:
        return _flood_wait_outcome(
            client, phones, emails, "person_query", error
        )
    except TimeoutError:
        return _outcome(
            client,
            ProcessingStatus.RETRY_REQUIRED,
            phones,
            emails,
            "person_query",
            "temporary_error",
            "telegram_timeout",
        )

    response_kind = classifyBotResponse(country_response)
    if response_kind is BotResponseKind.NOT_FOUND:
        return _outcome(
            client,
            ProcessingStatus.SKIPPED,
            phones,
            emails,
            "person_query",
            "person_not_found",
        )
    if response_kind is BotResponseKind.RETRYABLE_ERROR:
        return _temporary_bot_response(client, phones, emails, "person_query")
    if response_kind is BotResponseKind.UNKNOWN:
        return _manual_review(
            client, phones, emails, "person_query", "unknown_response"
        )

    try:
        person_response = await clickRussiaAndWait(
            conversation, country_response, timeout=timeout
        )
    except FloodWaitError as error:
        return _flood_wait_outcome(
            client, phones, emails, "country_selection", error
        )
    except TimeoutError:
        return _outcome(
            client,
            ProcessingStatus.RETRY_REQUIRED,
            phones,
            emails,
            "country_selection",
            "temporary_error",
            "telegram_timeout",
        )
    except (LookupError, ValueError):
        return _manual_review(
            client, phones, emails, "country_selection", "russia_button_missing"
        )

    response_kind = classifyBotResponse(person_response)
    if response_kind is BotResponseKind.NOT_FOUND:
        return _outcome(
            client,
            ProcessingStatus.SKIPPED,
            phones,
            emails,
            "person_result",
            "person_not_found",
        )
    if response_kind is BotResponseKind.RETRYABLE_ERROR:
        return _temporary_bot_response(client, phones, emails, "person_result")
    if response_kind is BotResponseKind.UNKNOWN:
        return _manual_review(
            client, phones, emails, "person_result", "unknown_response"
        )

    second_url = await extractReportUrlAsync(person_response)
    if second_url is None:
        return _manual_review(
            client, phones, emails, "person_result", "report_url_missing"
        )
    second_text, failure = await _load_report(
        client,
        second_url,
        phones,
        emails,
        "person_report_download",
        report_loader,
    )
    if failure is not None:
        return failure
    if second_text is None or not second_text.strip():
        return _manual_review(
            client, phones, emails, "person_report_parse", "empty_report"
        )

    try:
        second_report = parse_report(
            second_text,
            source_inn=client.director_inn,
            expected_director_name=director_name,
        )
    except (TypeError, ValueError):
        return _manual_review(
            client, phones, emails, "person_report_parse", "invalid_report"
        )
    _extend_unique(phones, second_report.phones)
    _extend_unique(emails, second_report.emails)
    if phones:
        return _outcome(
            client,
            ProcessingStatus.PROCESSED,
            phones,
            emails,
            "person_report_parse",
            "phone_found_by_person",
        )
    return _outcome(
        client,
        ProcessingStatus.SKIPPED,
        phones,
        emails,
        "person_report_parse",
        "phone_not_found",
    )


async def process_first_clients(
    storage: NewClientStorage,
    telegram_client: Any,
    bot_username: str,
    *,
    limit: int,
    write: bool = False,
    timeout: float = 30,
    report_loader: ReportLoader = download_report_text,
) -> list[EnrichmentOutcome]:
    """Последовательно обработать первые N записей очереди."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit должен быть положительным целым числом")
    available_queries = await getAvailableQueries(
        telegram_client,
        bot_username,
        timeout=timeout,
    )
    dry_run_clients = (
        iter(storage.list_for_processing(limit=limit)) if not write else None
    )
    claim_token = uuid4().hex if write else None
    outcomes: list[EnrichmentOutcome] = []
    for _ in range(limit):
        if available_queries <= 0:
            notify_queries_exhausted()
            break
        if write:
            assert claim_token is not None
            claimed = storage.claim_for_processing(1, claim_token)
            if not claimed:
                break
            client = claimed[0]
        else:
            assert dry_run_clients is not None
            try:
                client = next(dry_run_clients)
            except StopIteration:
                break

        try:
            async with telegram_client.conversation(
                bot_username, timeout=timeout
            ) as conversation:
                outcome = await enrich_client(
                    client,
                    conversation,
                    timeout=timeout,
                    report_loader=report_loader,
                    available_queries=available_queries,
                )
        except FloodWaitError as error:
            outcome = _flood_wait_outcome(
                client,
                list(client.telegram_phones),
                list(client.telegram_emails),
                "conversation",
                error,
            )
        except TimeoutError:
            outcome = _outcome(
                client,
                ProcessingStatus.RETRY_REQUIRED,
                list(client.telegram_phones),
                list(client.telegram_emails),
                "conversation",
                "temporary_error",
                "telegram_timeout",
            )
        except Exception as error:
            outcome = _outcome(
                client,
                ProcessingStatus.RETRY_REQUIRED,
                list(client.telegram_phones),
                list(client.telegram_emails),
                "unexpected_error",
                "temporary_error",
                type(error).__name__,
            )
        pause_batch = _should_pause_batch(outcome)
        if write:
            try:
                storage.save_telegram_result(
                    client.spp_id,
                    phones=outcome.phones,
                    emails=outcome.emails,
                    status=outcome.status,
                    stage=outcome.stage,
                    result_code=outcome.result_code,
                    error_code=outcome.error_code,
                    claim_token=claim_token,
                )
            except Exception as error:
                # Не освобождаем claim сразу: иначе эта же первая запись будет
                # повторно выбрана в текущем запуске и платный запрос повторится.
                # Claim автоматически станет доступен после stale timeout.
                outcome = _outcome(
                    client,
                    ProcessingStatus.RETRY_REQUIRED,
                    list(outcome.phones),
                    list(outcome.emails),
                    "storage_save",
                    "persistence_failed",
                    type(error).__name__,
                )
        outcomes.append(outcome)
        available_queries = max(
            0,
            available_queries - outcome.requests_spent,
        )
        if available_queries == 0:
            notify_queries_exhausted()
            break
        if pause_batch:
            break
    return outcomes


async def _load_report(
    client: NewClient,
    url: str,
    phones: list[str],
    emails: list[str],
    stage: str,
    report_loader: ReportLoader,
) -> tuple[str | None, EnrichmentOutcome | None]:
    try:
        return await report_loader(url), None
    except ReportDownloadError as error:
        status = (
            ProcessingStatus.RETRY_REQUIRED
            if error.retryable
            else ProcessingStatus.NEEDS_REVIEW
        )
        return None, _outcome(
            client,
            status,
            phones,
            emails,
            stage,
            "report_download_failed",
            error.code,
        )


def _director_name(client: NewClient) -> str | None:
    parts = (
        client.director_last_name,
        client.director_first_name,
        client.director_middle_name,
    )
    clean_parts = [part.strip() for part in parts if part and part.strip()]
    return " ".join(clean_parts) if len(clean_parts) >= 2 else None


def _extend_unique(target: list[str], values: tuple[str, ...]) -> None:
    known = set(target)
    for value in values:
        if value not in known:
            target.append(value)
            known.add(value)


def _manual_review(
    client: NewClient,
    phones: list[str],
    emails: list[str],
    stage: str,
    result_code: str,
) -> EnrichmentOutcome:
    return _outcome(
        client,
        ProcessingStatus.NEEDS_REVIEW,
        phones,
        emails,
        stage,
        result_code,
    )


def _temporary_bot_response(
    client: NewClient,
    phones: list[str],
    emails: list[str],
    stage: str,
) -> EnrichmentOutcome:
    return _outcome(
        client,
        ProcessingStatus.RETRY_REQUIRED,
        phones,
        emails,
        stage,
        "temporary_error",
        "bot_temporary_error",
    )


def _queries_exhausted(
    client: NewClient,
    phones: list[str],
    emails: list[str],
    stage: str,
) -> EnrichmentOutcome:
    return _outcome(
        client,
        ProcessingStatus.RETRY_REQUIRED,
        phones,
        emails,
        stage,
        "available_queries_exhausted",
        "available_queries_exhausted",
    )


def notify_queries_exhausted() -> None:
    """Временная заглушка уведомления об исчерпании платных запросов."""
    print("Доступные запросы Telegram-бота закончились; обработка остановлена.")


def _flood_wait_outcome(
    client: NewClient,
    phones: list[str],
    emails: list[str],
    stage: str,
    error: FloodWaitError,
) -> EnrichmentOutcome:
    retry_after = max(1, int(getattr(error, "seconds", 1)))
    return _outcome(
        client,
        ProcessingStatus.RETRY_REQUIRED,
        phones,
        emails,
        stage,
        "temporary_error",
        "telegram_flood_wait",
        retry_after,
    )


def _should_pause_batch(outcome: EnrichmentOutcome) -> bool:
    return outcome.error_code in {
        "telegram_flood_wait",
        "bot_temporary_error",
        "report_http_429",
    }


def _outcome(
    client: NewClient,
    status: ProcessingStatus,
    phones: list[str],
    emails: list[str],
    stage: str,
    result_code: str,
    error_code: str | None = None,
    retry_after_seconds: int | None = None,
) -> EnrichmentOutcome:
    return EnrichmentOutcome(
        client_spp_id=client.spp_id,
        status=status,
        phones=tuple(phones),
        emails=tuple(emails),
        stage=stage,
        result_code=result_code,
        error_code=error_code,
        retry_after_seconds=retry_after_seconds,
    )
