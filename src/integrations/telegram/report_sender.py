"""Отправка сохраненных отчетов и служебных уведомлений через Bot API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from src.application.reporting import (
    ClientReport,
    ReportEntry,
    build_report_excel,
    build_client_report,
    load_client_report,
    render_report_html,
)
from src.storage.new_clients import (
    NewClient,
    NewClientStorage,
    ProcessingStatus,
    TelegramSearchAttempt,
)
from src.storage.reporting import ReportItem, ReportItemDraft, ReportingStorage


DAILY_REPORT_KIND = "daily_cohort"
LATE_UPDATE_REPORT_KIND = "late_update"
BALANCE_STATE_KEY_PREFIX = "telegram_search_balance"
BALANCE_CHECK_FAILURE_STATE_KEY_PREFIX = "telegram_search_balance_check"


@dataclass(frozen=True)
class ReportDispatchResult:
    """Итог одной идемпотентной рассылки."""

    report_id: int
    cohort_date: date
    clients_count: int
    sent: int
    failed: int


def report_download_keyboard(report_id: int) -> InlineKeyboardMarkup:
    """Создать callback-кнопку Excel для неизменяемого снимка отчета."""
    if isinstance(report_id, bool) or report_id <= 0:
        raise ValueError("ID отчета должен быть больше нуля")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Скачать Excel",
                    callback_data=f"report:xlsx:id:{report_id}",
                )
            ]
        ]
    )


async def send_daily_report(
    bot: Any,
    client_storage: NewClientStorage,
    reporting_storage: ReportingStorage,
    target_date: date,
    *,
    bootstrap_admin_ids: tuple[int, ...] = (),
) -> ReportDispatchResult:
    """Создать отчет D−7 из SQLite и доставить его активным подписчикам."""
    report = load_client_report(client_storage, target_date)
    subscribers = reporting_storage.list_subscribers(
        bootstrap_admin_ids=bootstrap_admin_ids
    )
    report_run, created = reporting_storage.get_or_create_report_run(
        kind=DAILY_REPORT_KIND,
        cohort_date=target_date,
        items=(report_item_from_entry(entry) for entry in report.entries),
        delivery_user_ids=subscribers,
    )
    if not created:
        reporting_storage.retry_failed_deliveries(
            report_run.id,
            retryable_only=True,
        )

    if created:
        client_storage.mark_reported(
            (entry.client.spp_id for entry in report.entries),
            str(report_run.id),
            expected_revisions={
                entry.client.spp_id: entry.client.data_revision
                for entry in report.entries
            },
            expected_reported_revisions={
                entry.client.spp_id: entry.client.reported_revision
                for entry in report.entries
            },
        )
    snapshot_report = report_from_snapshot(
        reporting_storage.list_report_items(report_run.id)
    )
    sent, failed = await _dispatch_report(
        bot,
        reporting_storage,
        report_run.id,
        snapshot_report,
        cohort_date=target_date,
        title=(
            "Организации, открытые ровно 7 дней назад — "
            f"{target_date.strftime('%d.%m.%Y')}"
        ),
        bootstrap_admin_ids=bootstrap_admin_ids,
    )
    return ReportDispatchResult(
        report_id=report_run.id,
        cohort_date=target_date,
        clients_count=len(snapshot_report.entries),
        sent=sent,
        failed=failed,
    )


async def send_late_update_reports(
    bot: Any,
    client_storage: NewClientStorage,
    reporting_storage: ReportingStorage,
    *,
    bootstrap_admin_ids: tuple[int, ...] = (),
    eligible_through: date | None = None,
) -> tuple[ReportDispatchResult, ...]:
    """Отправить дополнения для карточек, измененных после прошлого отчета."""
    candidate_by_id = {
        client.spp_id: client for client in client_storage.list_report_updates()
    }
    if eligible_through is not None:
        current_main_run = reporting_storage.find_report_run(
            kind=DAILY_REPORT_KIND,
            cohort_date=eligible_through,
        )
        for client in client_storage.list_unreported_through(eligible_through):
            registration_date = date.fromisoformat(
                (client.registration_date or "")[:10]
            )
            if registration_date < eligible_through or current_main_run is not None:
                candidate_by_id.setdefault(client.spp_id, client)
    candidates = list(candidate_by_id.values())
    attempts = client_storage.latest_attempts_for_clients(
        client.spp_id for client in candidates
    )
    candidate_report = build_client_report(candidates, attempts)
    grouped: dict[date, list[Any]] = {}
    for entry in candidate_report.entries:
        client = entry.client
        if not client.registration_date:
            continue
        try:
            cohort_date = date.fromisoformat(client.registration_date[:10])
        except ValueError:
            continue
        previous_item = reporting_storage.latest_report_item_for_client(
            client.spp_id,
            kinds=(DAILY_REPORT_KIND, LATE_UPDATE_REPORT_KIND),
        )
        draft = report_item_from_entry(entry)
        if previous_item is not None and _same_report_content(draft, previous_item):
            client_storage.mark_reported(
                [client.spp_id],
                str(previous_item.report_id),
                expected_revisions={client.spp_id: client.data_revision},
                expected_reported_revisions={
                    client.spp_id: client.reported_revision
                },
            )
            continue
        grouped.setdefault(cohort_date, []).append(entry)

    results: list[ReportDispatchResult] = []
    for cohort_date, entries in grouped.items():
        report_run = reporting_storage.create_next_report_run_for_client_revisions(
            kind=LATE_UPDATE_REPORT_KIND,
            cohort_date=cohort_date,
            items=(report_item_from_entry(entry) for entry in entries),
            client_revisions={
                entry.client.spp_id: (
                    entry.client.data_revision,
                    entry.client.reported_revision,
                )
                for entry in entries
            },
            delivery_user_ids=reporting_storage.list_subscribers(
                bootstrap_admin_ids=bootstrap_admin_ids
            ),
        )
        if report_run is None:
            continue
        snapshot_report = report_from_snapshot(
            reporting_storage.list_report_items(report_run.id)
        )
        sent, failed = await _dispatch_report(
            bot,
            reporting_storage,
            report_run.id,
            snapshot_report,
            cohort_date=cohort_date,
            title=(
                "Дополнение к отчету за "
                f"{cohort_date.strftime('%d.%m.%Y')}"
            ),
            bootstrap_admin_ids=bootstrap_admin_ids,
        )
        results.append(
            ReportDispatchResult(
                report_id=report_run.id,
                cohort_date=cohort_date,
                clients_count=len(snapshot_report.entries),
                sent=sent,
                failed=failed,
            )
        )
    return tuple(results)


async def retry_failed_report_deliveries(
    bot: Any,
    reporting_storage: ReportingStorage,
    *,
    bootstrap_admin_ids: tuple[int, ...] = (),
    exclude_daily_date: date | None = None,
) -> tuple[ReportDispatchResult, ...]:
    """Повторить временно неудачные доставки прошлых запусков отчетов."""
    results: list[ReportDispatchResult] = []
    for report_run in reporting_storage.list_report_runs_with_open_deliveries():
        if (
            report_run.kind == DAILY_REPORT_KIND
            and exclude_daily_date is not None
            and report_run.cohort_date == exclude_daily_date.isoformat()
        ):
            continue
        reporting_storage.retry_failed_deliveries(
            report_run.id,
            retryable_only=True,
        )
        items = reporting_storage.list_report_items(report_run.id)
        report = report_from_snapshot(items)
        cohort_date = date.fromisoformat(report_run.cohort_date)
        sent, failed = await _dispatch_report(
            bot,
            reporting_storage,
            report_run.id,
            report,
            cohort_date=cohort_date,
            title=_stored_report_title(report_run.kind, cohort_date),
            bootstrap_admin_ids=bootstrap_admin_ids,
        )
        results.append(
            ReportDispatchResult(
                report_id=report_run.id,
                cohort_date=cohort_date,
                clients_count=len(items),
                sent=sent,
                failed=failed,
            )
        )
    return tuple(results)


async def _dispatch_report(
    bot: Any,
    reporting_storage: ReportingStorage,
    report_id: int,
    report: ClientReport,
    *,
    cohort_date: date,
    title: str,
    bootstrap_admin_ids: tuple[int, ...],
) -> tuple[int, int]:
    chunks = reporting_storage.ensure_report_rendered_parts(
        report_id,
        render_report_html(
            report,
            title=title,
        ),
    )
    claim_token = uuid4().hex
    pending_count = len(
        reporting_storage.list_pending_deliveries(report_id, limit=100_000)
    )
    deliveries = reporting_storage.claim_pending_deliveries(
        report_id,
        claim_token=claim_token,
        limit=max(1, pending_count),
    )
    current_subscribers = set(
        reporting_storage.list_subscribers(
            bootstrap_admin_ids=bootstrap_admin_ids
        )
    )
    sent = 0
    failed = 0
    for delivery in deliveries:
        access_revoked = (
            delivery.user_id not in current_subscribers
            or not reporting_storage.is_user_allowed(
                delivery.user_id,
                bootstrap_admin_ids=bootstrap_admin_ids,
            )
        )
        if access_revoked:
            reporting_storage.mark_delivery_failed(
                report_id,
                delivery.user_id,
                claim_token=claim_token,
                error_code="access_revoked",
            )
            failed += 1
            continue
        try:
            reporting_storage.ensure_delivery_parts(
                report_id,
                delivery.user_id,
                len(chunks) + 1,
                allow_single_append=True,
            )
            sent_parts = reporting_storage.sent_delivery_part_indexes(
                report_id,
                delivery.user_id,
            )
            last_message = None
            for index, chunk in enumerate(chunks):
                if index in sent_parts:
                    continue
                last_message = await bot.send_message(
                    delivery.user_id,
                    chunk,
                    parse_mode="HTML",
                )
                reporting_storage.mark_delivery_part_sent(
                    report_id,
                    delivery.user_id,
                    index,
                    message_id=getattr(last_message, "message_id", None),
                )
            excel_part_index = len(chunks)
            if excel_part_index not in sent_parts:
                document = BufferedInputFile(
                    build_report_excel(report),
                    filename=f"report_{report_id}_{cohort_date.isoformat()}.xlsx",
                )
                last_message = await bot.send_document(
                    delivery.user_id,
                    document,
                    caption="Полный отчет в Excel",
                )
                reporting_storage.mark_delivery_part_sent(
                    report_id,
                    delivery.user_id,
                    excel_part_index,
                    message_id=getattr(last_message, "message_id", None),
                )
            reporting_storage.mark_delivery_sent(
                report_id,
                delivery.user_id,
                claim_token=claim_token,
                telegram_message_id=getattr(last_message, "message_id", None),
            )
            sent += 1
        except Exception as error:
            error_code = type(error).__name__[:100]
            if error_code == "TelegramForbiddenError":
                reporting_storage.unsubscribe(delivery.user_id)
                error_code = "telegram_forbidden"
            reporting_storage.mark_delivery_failed(
                report_id,
                delivery.user_id,
                claim_token=claim_token,
                error_code=error_code,
            )
            failed += 1

    return sent, failed


async def notify_admins(
    bot: Any,
    reporting_storage: ReportingStorage,
    bootstrap_admin_ids: tuple[int, ...],
    text: str,
) -> int:
    """Отправить безопасное служебное сообщение всем администраторам."""
    delivered = 0
    for user_id in reporting_storage.list_admins(
        bootstrap_admin_ids=bootstrap_admin_ids
    ):
        try:
            await bot.send_message(user_id, text)
            delivered += 1
        except Exception:
            continue
    return delivered


async def observe_query_balance(
    bot: Any,
    reporting_storage: ReportingStorage,
    bootstrap_admin_ids: tuple[int, ...],
    available_queries: int,
    *,
    low_threshold: int,
) -> None:
    """Уведомить администраторов только при смене уровня доступного баланса."""
    if isinstance(low_threshold, bool) or low_threshold <= 0:
        raise ValueError("Порог остатка запросов должен быть больше нуля")
    if isinstance(available_queries, bool) or available_queries < 0:
        raise ValueError("Остаток запросов должен быть неотрицательным")

    level = (
        "empty"
        if available_queries == 0
        else "low"
        if available_queries < low_threshold
        else "normal"
    )
    for admin_id in reporting_storage.list_admins(
        bootstrap_admin_ids=bootstrap_admin_ids
    ):
        reporting_storage.set_notification_state(
            f"{BALANCE_CHECK_FAILURE_STATE_KEY_PREFIX}:{admin_id}",
            "ok",
        )
        state_key = f"{BALANCE_STATE_KEY_PREFIX}:{admin_id}"
        previous = reporting_storage.get_notification_state(state_key)
        if previous is not None and previous.value == level:
            continue
        if level == "normal":
            reporting_storage.set_notification_state(state_key, level)
            continue
        text = (
            "Доступные запросы поискового Telegram-бота закончились."
            if level == "empty"
            else (
                "Осталось мало запросов поискового Telegram-бота: "
                f"{available_queries}. Порог предупреждения: {low_threshold}."
            )
        )
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            # Состояние не фиксируется: следующая проверка повторит уведомление
            # только для администратора, которому оно не было доставлено.
            continue
        reporting_storage.set_notification_state(state_key, level)


async def notify_balance_check_failed(
    bot: Any,
    reporting_storage: ReportingStorage,
    bootstrap_admin_ids: tuple[int, ...],
) -> int:
    """Один раз уведомить каждого администратора до следующей успешной проверки."""
    delivered = 0
    for admin_id in reporting_storage.list_admins(
        bootstrap_admin_ids=bootstrap_admin_ids
    ):
        state_key = f"{BALANCE_CHECK_FAILURE_STATE_KEY_PREFIX}:{admin_id}"
        previous = reporting_storage.get_notification_state(state_key)
        if previous is not None and previous.value == "failed":
            continue
        try:
            await bot.send_message(
                admin_id,
                "Не удалось получить остаток запросов поискового "
                "Telegram-бота. Код ошибки: TelegramBalanceCheckError.",
            )
        except Exception:
            continue
        reporting_storage.set_notification_state(state_key, "failed")
        delivered += 1
    return delivered


def report_item_from_entry(entry: ReportEntry) -> ReportItemDraft:
    """Преобразовать строку отчета в полный безопасный снимок для SQLite."""
    client = entry.client
    return ReportItemDraft(
        client_spp_id=client.spp_id,
        company_name=client.name,
        director_name=entry.director_name or None,
        status=client.status.value,
        registration_date=client.registration_date,
        sbis_phones=client.sbis_phones,
        sbis_emails=client.sbis_emails,
        personalised_phones=client.personalised_phones,
        personalised_emails=client.personalised_emails,
        telegram_phones=client.telegram_phones,
        telegram_emails=client.telegram_emails,
        result_code=entry.result_code,
        error_code=entry.error_code,
    )


def _stored_report_title(kind: str, cohort_date: date) -> str:
    if kind == DAILY_REPORT_KIND:
        return (
            "Организации, открытые ровно 7 дней назад — "
            f"{cohort_date.strftime('%d.%m.%Y')}"
        )
    if kind == LATE_UPDATE_REPORT_KIND:
        return f"Дополнение к отчету за {cohort_date.strftime('%d.%m.%Y')}"
    return f"Отчет за {cohort_date.strftime('%d.%m.%Y')}"


def _same_report_content(draft: ReportItemDraft, item: ReportItem) -> bool:
    fields = (
        "client_spp_id",
        "company_name",
        "director_name",
        "status",
        "registration_date",
        "sbis_phones",
        "sbis_emails",
        "personalised_phones",
        "personalised_emails",
        "telegram_phones",
        "telegram_emails",
    )
    return all(getattr(draft, field) == getattr(item, field) for field in fields)


def report_from_snapshot(items: Sequence[ReportItem]) -> ClientReport:
    """Восстановить отображаемый отчет из зафиксированных строк доставки."""
    clients: list[NewClient] = []
    attempts: dict[int, TelegramSearchAttempt] = {}
    for item in items:
        clients.append(
            NewClient(
                spp_id=item.client_spp_id,
                name=item.company_name,
                region=None,
                ogrn=None,
                inn="",
                kpp=None,
                is_entrepreneur=False,
                registration_date=item.registration_date,
                liquidation_date=None,
                director_last_name=item.director_name,
                director_first_name=None,
                director_middle_name=None,
                sbis_phones=item.sbis_phones,
                telegram_phones=item.telegram_phones,
                sbis_emails=item.sbis_emails,
                telegram_emails=item.telegram_emails,
                status=ProcessingStatus(item.status),
                personalised_phones=item.personalised_phones,
                personalised_emails=item.personalised_emails,
            )
        )
        if item.result_code or item.error_code:
            attempts[item.client_spp_id] = TelegramSearchAttempt(
                client_spp_id=item.client_spp_id,
                attempt_number=1,
                stage="report_snapshot",
                result_code=item.result_code or "unknown",
                error_code=item.error_code,
                created_at="",
            )
    return build_client_report(clients, attempts)
