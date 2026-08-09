"""Закрытый Telegram-бот подписок и отчетов по новым организациям."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from secrets import token_urlsafe
from time import monotonic
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.application.reporting import (
    ClientReport,
    build_client_report,
    build_report_excel,
    render_report_html,
)
from src.config import loadEnvironment, requireSetting
from src.integrations.telegram.report_sender import (
    report_download_keyboard,
    report_from_snapshot,
    report_item_from_entry,
)
from src.storage.new_clients import STATUS_LABELS, NewClientStorage, ProcessingStatus
from src.storage.reporting import BootstrapAdminRemovalError, ReportingStorage


CALLBACK_SUBSCRIBE = "sub:on"
CALLBACK_UNSUBSCRIBE = "sub:off"
CALLBACK_STATUS = "status"
CALLBACK_REPORT_DATE = "report:ask"
CALLBACK_ADMIN_PANEL = "admin:panel"
CALLBACK_ADMIN_LIST = "admin:list"
CALLBACK_ADMIN_REPORT_STATUS = "admin:status"
CALLBACK_ADMIN_PIPELINE_STATUS = "admin:pipeline_status"
CALLBACK_ADMIN_TEST_REPORT = "admin:test_report"
CALLBACK_ADMIN_PREFIX = "admin:"
CALLBACK_ADMIN_CONFIRM_PREFIX = "admin:confirm:"
CALLBACK_ADMIN_CANCEL_PREFIX = "admin:cancel:"
CALLBACK_EXCEL_PREFIX = "report:xlsx:id:"
MANUAL_REPORT_KIND = "manual"
ADMIN_CONFIRM_TTL_SECONDS = 300.0

ADMIN_ACTIONS: dict[str, tuple[str, str]] = {
    "add": ("add_user", "Добавить пользователя"),
    "remove": ("remove_user", "Удалить пользователя"),
    "admin_add": ("add_admin", "Добавить администратора"),
    "admin_remove": ("remove_admin", "Удалить администратора"),
}
SENSITIVE_ADMIN_OPERATIONS = frozenset(
    {"remove_user", "add_admin", "remove_admin"}
)


@dataclass(frozen=True)
class PendingAdminAction:
    """Одноразовое подтверждение опасного административного действия."""

    actor_id: int
    target_id: int
    operation: str
    expires_at: float


@dataclass(frozen=True)
class ReportBotSettings:
    """Проверенные настройки отчетного бота."""

    token: str
    database_path: Path
    bootstrap_admin_ids: frozenset[int]
    low_query_threshold: int
    report_retention_days: int | None


def load_report_bot_settings(database_path: str | Path) -> ReportBotSettings:
    """Загрузить настройки бота и проверить числовые ограничения."""
    loadEnvironment()
    token = requireSetting("TELEGRAM_REPORT_BOT_TOKEN")
    bootstrap_admin_ids = parse_bootstrap_admin_ids(
        requireSetting("REPORT_BOT_BOOTSTRAP_ADMIN_IDS")
    )
    threshold_text = os.getenv("REPORT_LOW_QUERY_THRESHOLD", "10").strip()
    try:
        low_query_threshold = int(threshold_text)
    except ValueError as error:
        raise ValueError(
            "REPORT_LOW_QUERY_THRESHOLD должен быть положительным целым числом"
        ) from error
    if low_query_threshold <= 0:
        raise ValueError(
            "REPORT_LOW_QUERY_THRESHOLD должен быть положительным целым числом"
        )
    retention_text = os.getenv("REPORT_RETENTION_DAYS", "").strip()
    report_retention_days: int | None = None
    if retention_text:
        try:
            retention_value = int(retention_text)
        except ValueError as error:
            raise ValueError(
                "REPORT_RETENTION_DAYS должен быть неотрицательным целым числом"
            ) from error
        if retention_value < 0:
            raise ValueError(
                "REPORT_RETENTION_DAYS должен быть неотрицательным целым числом"
            )
        if retention_value > 0:
            report_retention_days = retention_value
    return ReportBotSettings(
        token=token,
        database_path=Path(database_path),
        bootstrap_admin_ids=bootstrap_admin_ids,
        low_query_threshold=low_query_threshold,
        report_retention_days=report_retention_days,
    )


class AdminInput(StatesGroup):
    """Ожидание Telegram ID для административной операции."""

    waiting_user_id = State()


class ReportDateInput(StatesGroup):
    """Ожидание даты ручного отчета."""

    waiting_date = State()


def parse_bootstrap_admin_ids(value: str) -> frozenset[int]:
    """Разобрать список положительных Telegram ID из настройки окружения."""
    result: set[int] = set()
    for part in value.split(","):
        clean = part.strip()
        if not clean:
            continue
        try:
            user_id = int(clean)
        except ValueError as error:
            raise ValueError(
                "REPORT_BOT_BOOTSTRAP_ADMIN_IDS должен содержать Telegram ID "
                "через запятую"
            ) from error
        if user_id <= 0:
            raise ValueError("Telegram ID администратора должен быть больше нуля")
        result.add(user_id)
    if not result:
        raise ValueError("Нужно указать хотя бы одного bootstrap-администратора")
    return frozenset(result)


def user_menu(*, subscribed: bool, is_admin: bool) -> InlineKeyboardMarkup:
    """Сформировать главное inline-меню разрешенного пользователя."""
    subscription_button = InlineKeyboardButton(
        text="Отписаться" if subscribed else "Подписаться",
        callback_data=(CALLBACK_UNSUBSCRIBE if subscribed else CALLBACK_SUBSCRIBE),
    )
    rows = [
        [
            subscription_button,
            InlineKeyboardButton(text="Статус", callback_data=CALLBACK_STATUS),
        ],
        [
            InlineKeyboardButton(
                text="Отчет по дате", callback_data=CALLBACK_REPORT_DATE
            )
        ],
    ]
    if is_admin:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Администрирование", callback_data=CALLBACK_ADMIN_PANEL
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu() -> InlineKeyboardMarkup:
    """Сформировать административную inline-панель."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Добавить пользователя", callback_data="admin:add"
                ),
                InlineKeyboardButton(
                    text="Удалить пользователя", callback_data="admin:remove"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Добавить администратора", callback_data="admin:admin_add"
                ),
                InlineKeyboardButton(
                    text="Удалить администратора",
                    callback_data="admin:admin_remove",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Белые списки", callback_data=CALLBACK_ADMIN_LIST
                )
            ],
            [
                InlineKeyboardButton(
                    text="Состояние отчетов",
                    callback_data=CALLBACK_ADMIN_REPORT_STATUS,
                ),
                InlineKeyboardButton(
                    text="Состояние конвейера",
                    callback_data=CALLBACK_ADMIN_PIPELINE_STATUS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Тестовый отчет",
                    callback_data=CALLBACK_ADMIN_TEST_REPORT,
                ),
            ],
        ]
    )


class ReportBotService:
    """Обработчики доступа, подписок и ручных отчетов без внешнего поиска."""

    def __init__(
        self,
        access_storage: ReportingStorage,
        client_storage: NewClientStorage,
        *,
        bootstrap_admin_ids: Iterable[int],
    ) -> None:
        self.access_storage = access_storage
        self.client_storage = client_storage
        self.bootstrap_admin_ids = frozenset(int(item) for item in bootstrap_admin_ids)
        self._pending_admin_actions: dict[str, PendingAdminAction] = {}

    async def start(self, message: Message) -> None:
        """Показать отказ с ID либо главное меню пользователя."""
        user_id = await self._authorized_message(message)
        if user_id is None:
            return
        await message.answer(
            "Бот отправляет ежедневные отчеты и формирует выборки из уже "
            "собранной базы данных.",
            reply_markup=self._user_menu(user_id),
        )

    async def show_id(self, message: Message) -> None:
        """Показать собственный Telegram ID даже неизвестному пользователю."""
        user_id = await self._private_user_id(message)
        if user_id is None:
            return
        await message.answer(
            f"Ваш Telegram ID: <code>{user_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    async def subscribe(self, message: Message) -> None:
        """Подписать разрешенного пользователя на ежедневные отчеты."""
        user_id = await self._authorized_message(message)
        if user_id is None:
            return
        changed = self.access_storage.subscribe(
            user_id, bootstrap_admin_ids=self.bootstrap_admin_ids
        )
        text = "Подписка включена." if changed else "Подписка уже была включена."
        await message.answer(text, reply_markup=self._user_menu(user_id))

    async def unsubscribe(self, message: Message) -> None:
        """Отключить ежедневные отчеты для пользователя."""
        user_id = await self._authorized_message(message)
        if user_id is None:
            return
        changed = self.access_storage.unsubscribe(user_id)
        text = "Подписка отключена." if changed else "Подписка уже была отключена."
        await message.answer(text, reply_markup=self._user_menu(user_id))

    async def status(self, message: Message) -> None:
        """Показать роль и состояние подписки."""
        user_id = await self._authorized_message(message)
        if user_id is None:
            return
        await message.answer(
            self._status_text(user_id), reply_markup=self._user_menu(user_id)
        )

    async def report_command(self, message: Message) -> None:
        """Сформировать ручной отчет по аргументу команды /report YYYY-MM-DD."""
        user_id = await self._authorized_message(message)
        if user_id is None:
            return
        argument = _command_argument(message.text)
        if argument is None:
            await message.answer(
                "Укажите дату: <code>/report YYYY-MM-DD</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            target_date = _parse_date(argument)
        except ValueError as error:
            await message.answer(str(error))
            return
        await self._send_html_report(message, target_date)

    async def request_report_date(self, message: Message, state: FSMContext) -> None:
        """Перейти к вводу даты ручного отчета."""
        user_id = await self._authorized_message(message)
        if user_id is None:
            await state.clear()
            return
        await state.set_state(ReportDateInput.waiting_date)
        await message.answer("Введите дату отчета в формате YYYY-MM-DD.")

    async def receive_report_date(self, message: Message, state: FSMContext) -> None:
        """Проверить введенную дату и отправить отчет из SQLite."""
        user_id = await self._authorized_message(message)
        if user_id is None:
            await state.clear()
            return
        try:
            target_date = _parse_date(message.text or "")
        except ValueError as error:
            await message.answer(f"{error} Попробуйте еще раз или отправьте /cancel.")
            return
        await state.clear()
        await self._send_html_report(message, target_date)

    async def cancel(self, message: Message, state: FSMContext) -> None:
        """Отменить текущий диалог ввода."""
        user_id = await self._authorized_message(message)
        await state.clear()
        if user_id is not None:
            await message.answer(
                "Действие отменено.", reply_markup=self._user_menu(user_id)
            )

    async def subscription_callback(self, callback: CallbackQuery) -> None:
        """Изменить подписку после повторной проверки доступа."""
        user_id, message = await self._authorized_callback(callback)
        if user_id is None or message is None:
            return
        if callback.data == CALLBACK_SUBSCRIBE:
            changed = self.access_storage.subscribe(
                user_id, bootstrap_admin_ids=self.bootstrap_admin_ids
            )
            text = "Подписка включена." if changed else "Подписка уже была включена."
        else:
            changed = self.access_storage.unsubscribe(user_id)
            text = "Подписка отключена." if changed else "Подписка уже была отключена."
        await message.answer(text, reply_markup=self._user_menu(user_id))

    async def status_callback(self, callback: CallbackQuery) -> None:
        """Показать статус после повторной проверки доступа."""
        user_id, message = await self._authorized_callback(callback)
        if user_id is None or message is None:
            return
        await message.answer(
            self._status_text(user_id), reply_markup=self._user_menu(user_id)
        )

    async def report_date_callback(
        self, callback: CallbackQuery, state: FSMContext
    ) -> None:
        """Запросить дату после быстрого ответа callback и проверки доступа."""
        user_id, message = await self._authorized_callback(callback)
        if user_id is None or message is None:
            await state.clear()
            return
        await state.set_state(ReportDateInput.waiting_date)
        await message.answer("Введите дату отчета в формате YYYY-MM-DD.")

    async def excel_callback(self, callback: CallbackQuery) -> None:
        """Сформировать XLSX из того же неизменяемого снимка, что и HTML."""
        user_id, message = await self._authorized_callback(callback)
        if user_id is None or message is None:
            return
        raw_report_id = (callback.data or "")[len(CALLBACK_EXCEL_PREFIX) :]
        try:
            report_id = _parse_user_id(raw_report_id)
        except ValueError:
            await message.answer("Кнопка содержит некорректный ID отчета.")
            return
        report_run = self.access_storage.get_report_run(report_id)
        if report_run is None:
            await message.answer("Сохраненный отчет больше не найден.")
            return
        target_date = _parse_date(report_run.cohort_date)
        report = report_from_snapshot(
            self.access_storage.list_report_items(report_id)
        )
        document = BufferedInputFile(
            build_report_excel(report),
            filename=f"report_{report_id}_{target_date.isoformat()}.xlsx",
        )
        await message.answer_document(
            document,
            caption=f"Отчет за {target_date.strftime('%d.%m.%Y')}",
        )

    async def admin_panel(self, message: Message) -> None:
        """Показать административную панель."""
        user_id = await self._authorized_message(message, admin=True)
        if user_id is None:
            return
        await message.answer("Управление белыми списками:", reply_markup=admin_menu())

    async def report_status(self, message: Message) -> None:
        """Показать администратору состояние последнего запуска отчета."""
        actor_id = await self._authorized_message(message, admin=True)
        if actor_id is None:
            return
        await message.answer(self._report_status_text())

    async def pipeline_status(self, message: Message) -> None:
        """Показать администратору состояние ежедневного конвейера."""
        actor_id = await self._authorized_message(message, admin=True)
        if actor_id is None:
            return
        await message.answer(self._pipeline_status_text())

    async def admin_command(self, message: Message, state: FSMContext) -> None:
        """Выполнить /add, /remove, /admin_add или /admin_remove."""
        actor_id = await self._authorized_message(message, admin=True)
        if actor_id is None:
            await state.clear()
            return
        command = _command_name(message.text)
        operation = ADMIN_ACTIONS.get(command or "")
        if operation is None:  # pragma: no cover - защита регистрации фильтра
            return
        argument = _command_argument(message.text)
        if argument is None:
            await state.set_state(AdminInput.waiting_user_id)
            await state.update_data(admin_operation=operation[0])
            await message.answer(f"{operation[1]}: отправьте числовой Telegram ID.")
            return
        await self._run_admin_action(message, actor_id, operation[0], argument)

    async def receive_admin_id(self, message: Message, state: FSMContext) -> None:
        """Применить административную операцию к введенному Telegram ID."""
        actor_id = await self._authorized_message(message, admin=True)
        if actor_id is None:
            await state.clear()
            return
        data = await state.get_data()
        operation = data.get("admin_operation")
        if operation not in {item[0] for item in ADMIN_ACTIONS.values()}:
            await state.clear()
            await message.answer("Административная операция потеряна. Начните заново.")
            return
        completed = await self._run_admin_action(
            message, actor_id, str(operation), message.text or ""
        )
        if completed:
            await state.clear()

    async def admin_action_callback(
        self, callback: CallbackQuery, state: FSMContext
    ) -> None:
        """Начать изменение whitelist через inline-панель."""
        actor_id, message = await self._authorized_callback(callback, admin=True)
        if actor_id is None or message is None:
            await state.clear()
            return
        command = (callback.data or "")[len(CALLBACK_ADMIN_PREFIX) :]
        operation = ADMIN_ACTIONS.get(command)
        if operation is None:
            await message.answer("Неизвестная административная операция.")
            return
        await state.set_state(AdminInput.waiting_user_id)
        await state.update_data(admin_operation=operation[0])
        await message.answer(f"{operation[1]}: отправьте числовой Telegram ID.")

    async def admin_confirmation_callback(self, callback: CallbackQuery) -> None:
        """Подтвердить или отменить одноразовое административное действие."""
        actor_id, message = await self._authorized_callback(callback, admin=True)
        if actor_id is None or message is None:
            return
        data = callback.data or ""
        is_confirmation = data.startswith(CALLBACK_ADMIN_CONFIRM_PREFIX)
        prefix = (
            CALLBACK_ADMIN_CONFIRM_PREFIX
            if is_confirmation
            else CALLBACK_ADMIN_CANCEL_PREFIX
        )
        token = data[len(prefix) :]
        pending = self._pending_admin_actions.get(token)
        now = monotonic()
        if (
            pending is None
            or pending.actor_id != actor_id
            or now > pending.expires_at
        ):
            if pending is not None and now > pending.expires_at:
                self._pending_admin_actions.pop(token, None)
            await message.answer(
                "Подтверждение истекло, уже использовано или принадлежит "
                "другому администратору."
            )
            return

        self._pending_admin_actions.pop(token, None)
        if not is_confirmation:
            await message.answer("Административное действие отменено.")
            return
        await self._apply_admin_action(
            message,
            actor_id,
            pending.operation,
            pending.target_id,
        )

    async def admin_panel_callback(self, callback: CallbackQuery) -> None:
        """Показать панель после повторной проверки административных прав."""
        actor_id, message = await self._authorized_callback(callback, admin=True)
        if actor_id is None or message is None:
            return
        await message.answer("Управление белыми списками:", reply_markup=admin_menu())

    async def admin_list_callback(self, callback: CallbackQuery) -> None:
        """Показать администраторам текущие белые списки и подписки."""
        actor_id, message = await self._authorized_callback(callback, admin=True)
        if actor_id is None or message is None:
            return
        users = self.access_storage.list_users()
        admins = self.access_storage.list_admins(
            bootstrap_admin_ids=self.bootstrap_admin_ids
        )
        subscribers = self.access_storage.list_subscribers(
            bootstrap_admin_ids=self.bootstrap_admin_ids
        )
        await message.answer(
            "<b>Белый список пользователей</b>\n"
            f"{_ids_text(users)}\n\n"
            "<b>Администраторы</b>\n"
            f"{_ids_text(admins)}\n\n"
            "<b>Подписчики</b>\n"
            f"{_ids_text(subscribers)}",
            parse_mode=ParseMode.HTML,
        )

    async def report_status_callback(self, callback: CallbackQuery) -> None:
        """Показать состояние отчетов после повторной проверки прав."""
        actor_id, message = await self._authorized_callback(callback, admin=True)
        if actor_id is None or message is None:
            return
        await message.answer(self._report_status_text())

    async def pipeline_status_callback(self, callback: CallbackQuery) -> None:
        """Показать состояние конвейера после повторной проверки прав."""
        actor_id, message = await self._authorized_callback(callback, admin=True)
        if actor_id is None or message is None:
            return
        await message.answer(self._pipeline_status_text())

    async def admin_test_report_callback(
        self, callback: CallbackQuery, state: FSMContext
    ) -> None:
        """Запросить дату тестового отчета без запуска сбора или поиска."""
        actor_id, message = await self._authorized_callback(callback, admin=True)
        if actor_id is None or message is None:
            await state.clear()
            return
        await state.set_state(ReportDateInput.waiting_date)
        await message.answer(
            "Введите дату тестового отчета в формате YYYY-MM-DD. "
            "Будут использованы только уже сохраненные данные."
        )

    async def fallback(self, message: Message) -> None:
        """Безопасно ответить на неизвестный текст или показать ID посетителю."""
        user_id = await self._authorized_message(message)
        if user_id is None:
            return
        await message.answer(
            "Неизвестная команда. Используйте /start.",
            reply_markup=self._user_menu(user_id),
        )

    async def _send_html_report(self, message: Message, target_date: date) -> None:
        report_id, report = self._create_manual_snapshot(target_date)
        chunks = render_report_html(
            report, title=f"Отчет за {target_date.strftime('%d.%m.%Y')}"
        )
        for index, chunk in enumerate(chunks):
            reply_markup = (
                report_download_keyboard(report_id)
                if index == len(chunks) - 1
                else None
            )
            await message.answer(
                chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )

    def _load_report(self, target_date: date) -> ClientReport:
        """Прочитать отчет только из уже собранных таблиц NewClientStorage."""
        clients = self.client_storage.list_by_registration_date(target_date)
        attempts = self.client_storage.latest_attempts_for_clients(
            client.spp_id for client in clients
        )
        return build_client_report(clients, attempts)

    def _create_manual_snapshot(
        self, target_date: date
    ) -> tuple[int, ClientReport]:
        """Создать уникальный ручной запуск и перечитать его снимок из SQLite."""
        source_report = self._load_report(target_date)
        report_run = self.access_storage.create_next_report_run(
            kind=MANUAL_REPORT_KIND,
            cohort_date=target_date,
            items=(report_item_from_entry(entry) for entry in source_report.entries),
        )
        snapshot = report_from_snapshot(
            self.access_storage.list_report_items(report_run.id)
        )
        return report_run.id, snapshot

    async def _run_admin_action(
        self,
        message: Message,
        actor_id: int,
        operation: str,
        raw_target_id: str,
    ) -> bool:
        try:
            target_id = _parse_user_id(raw_target_id)
        except ValueError as error:
            await message.answer(f"{error} Попробуйте еще раз или отправьте /cancel.")
            return False

        if operation in SENSITIVE_ADMIN_OPERATIONS:
            self._prune_admin_confirmations()
            token = token_urlsafe(8)
            self._pending_admin_actions[token] = PendingAdminAction(
                actor_id=actor_id,
                target_id=target_id,
                operation=operation,
                expires_at=monotonic() + ADMIN_CONFIRM_TTL_SECONDS,
            )
            label = next(
                text
                for action, text in ADMIN_ACTIONS.values()
                if action == operation
            )
            await message.answer(
                f"{label}: <code>{target_id}</code>. Подтвердите действие.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Подтвердить",
                                callback_data=(
                                    f"{CALLBACK_ADMIN_CONFIRM_PREFIX}{token}"
                                ),
                            ),
                            InlineKeyboardButton(
                                text="Отмена",
                                callback_data=(
                                    f"{CALLBACK_ADMIN_CANCEL_PREFIX}{token}"
                                ),
                            ),
                        ]
                    ]
                ),
            )
            return True

        return await self._apply_admin_action(
            message,
            actor_id,
            operation,
            target_id,
        )

    async def _apply_admin_action(
        self,
        message: Message,
        actor_id: int,
        operation: str,
        target_id: int,
    ) -> bool:
        """Применить уже проверенное или подтвержденное изменение whitelist."""

        try:
            if operation == "add_user":
                changed = self.access_storage.add_user(
                    target_id, actor_user_id=actor_id
                )
            elif operation == "remove_user":
                changed = self.access_storage.remove_user(
                    target_id,
                    actor_user_id=actor_id,
                    bootstrap_admin_ids=self.bootstrap_admin_ids,
                )
            elif operation == "add_admin":
                changed = self.access_storage.add_admin(
                    target_id,
                    actor_user_id=actor_id,
                    bootstrap_admin_ids=self.bootstrap_admin_ids,
                )
            elif operation == "remove_admin":
                changed = self.access_storage.remove_admin(
                    target_id,
                    actor_user_id=actor_id,
                    bootstrap_admin_ids=self.bootstrap_admin_ids,
                )
            else:  # pragma: no cover - внутренний инвариант
                raise ValueError("Неизвестная административная операция")
        except BootstrapAdminRemovalError as error:
            await message.answer(str(error))
            return True

        result = "Белый список изменен." if changed else "Изменений не потребовалось."
        await message.answer(
            f"{result} Telegram ID: <code>{target_id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return True

    def _prune_admin_confirmations(self) -> None:
        """Удалить истекшие подтверждения из памяти long-polling процесса."""
        now = monotonic()
        for token, pending in tuple(self._pending_admin_actions.items()):
            if now > pending.expires_at:
                self._pending_admin_actions.pop(token, None)

    def _user_menu(self, user_id: int) -> InlineKeyboardMarkup:
        subscribers = self.access_storage.list_subscribers(
            bootstrap_admin_ids=self.bootstrap_admin_ids
        )
        return user_menu(
            subscribed=user_id in subscribers,
            is_admin=self._is_admin(user_id),
        )

    def _status_text(self, user_id: int) -> str:
        subscribers = self.access_storage.list_subscribers(
            bootstrap_admin_ids=self.bootstrap_admin_ids
        )
        return (
            "Доступ разрешен.\n"
            f"Роль: {'администратор' if self._is_admin(user_id) else 'пользователь'}.\n"
            f"Подписка: {'активна' if user_id in subscribers else 'неактивна'}."
        )

    def _report_status_text(self) -> str:
        report_run = self.access_storage.latest_deliverable_report_run()
        if report_run is None:
            return "Отчеты еще не формировались."
        counts = self.access_storage.delivery_status_counts(report_run.id)
        return (
            "Последний отчет:\n"
            f"ID: {report_run.id}\n"
            f"Тип: {report_run.kind}\n"
            f"Дата выборки: {report_run.cohort_date}\n"
            f"Ревизия: {report_run.revision}\n"
            f"Создан: {report_run.created_at}\n"
            "Доставка: "
            f"отправлено — {int(counts.get('sent', 0))}, "
            f"ожидает — {int(counts.get('pending', 0))}, "
            f"ошибок — {int(counts.get('failed', 0))}."
        )

    def _pipeline_status_text(self) -> str:
        pipeline_run = self.access_storage.latest_pipeline_run()
        if pipeline_run is None:
            return "Ежедневный конвейер еще не запускался."
        status_label = {
            "running": "запущен",
            "completed": "завершен",
            "failed": "завершен с ошибкой",
        }.get(pipeline_run.status, pipeline_run.status)
        counts = pipeline_run.processing_status_counts
        count_parts = [
            f"{STATUS_LABELS[status]} — {int(counts.get(status.value, 0))}"
            for status in ProcessingStatus
        ]
        known_statuses = {status.value for status in ProcessingStatus}
        count_parts.extend(
            f"{key} — {int(value)}"
            for key, value in sorted(counts.items())
            if key not in known_statuses
        )
        available_queries = (
            pipeline_run.available_queries
            if pipeline_run.available_queries is not None
            else "не получено"
        )
        return (
            "Последний запуск конвейера:\n"
            f"Состояние: {status_label}.\n"
            f"Целевая дата: {pipeline_run.target_date}.\n"
            f"Начат: {pipeline_run.started_at}.\n"
            f"Завершен: {pipeline_run.finished_at or '—'}.\n"
            f"Собрано карточек: {pipeline_run.collected_cards}.\n"
            f"Статусы обработки: {', '.join(count_parts)}.\n"
            f"Доступно запросов: {available_queries}.\n"
            f"Этап ошибки: {pipeline_run.error_stage or '—'}.\n"
            f"Код ошибки: {pipeline_run.error_code or '—'}."
        )

    def _is_admin(self, user_id: int) -> bool:
        return self.access_storage.is_admin(
            user_id, bootstrap_admin_ids=self.bootstrap_admin_ids
        )

    async def _authorized_message(
        self, message: Message, *, admin: bool = False
    ) -> int | None:
        user_id = await self._private_user_id(message)
        if user_id is None:
            return None
        allowed = self.access_storage.is_admin(
            user_id, bootstrap_admin_ids=self.bootstrap_admin_ids
        ) if admin else self.access_storage.is_user_allowed(
            user_id, bootstrap_admin_ids=self.bootstrap_admin_ids
        )
        if not allowed:
            await self._send_access_denied(message, user_id)
            return None
        return user_id

    async def _authorized_callback(
        self, callback: CallbackQuery, *, admin: bool = False
    ) -> tuple[int | None, Any | None]:
        # Telegram-клиент должен получить answer до SQLite, Excel и прочей работы.
        await callback.answer()
        message = callback.message
        if message is None or not callable(getattr(message, "answer", None)):
            return None, None
        user = callback.from_user
        user_id = getattr(user, "id", None)
        if not _is_private(message) or not isinstance(user_id, int) or user_id <= 0:
            if not _is_private(message):
                await message.answer("Отчетный бот работает только в личном чате.")
            return None, message
        allowed = self.access_storage.is_admin(
            user_id, bootstrap_admin_ids=self.bootstrap_admin_ids
        ) if admin else self.access_storage.is_user_allowed(
            user_id, bootstrap_admin_ids=self.bootstrap_admin_ids
        )
        if not allowed:
            await self._send_access_denied(message, user_id)
            return None, message
        return user_id, message

    async def _private_user_id(self, message: Message) -> int | None:
        if not _is_private(message):
            await message.answer("Отчетный бот работает только в личном чате.")
            return None
        user_id = getattr(getattr(message, "from_user", None), "id", None)
        if not isinstance(user_id, int) or user_id <= 0:
            await message.answer("Не удалось определить Telegram ID пользователя.")
            return None
        return user_id

    @staticmethod
    async def _send_access_denied(message: Any, user_id: int) -> None:
        await message.answer(
            "Доступ к отчетам пока не разрешен.\n"
            f"Ваш Telegram ID: <code>{user_id}</code>\n"
            "Сообщите этот ID администратору.",
            parse_mode=ParseMode.HTML,
        )


def create_report_router(service: ReportBotService) -> Router:
    """Создать изолированный Router с обработчиками отчетного бота."""
    router = Router(name="report_bot")
    router.message.register(service.start, CommandStart())
    router.message.register(service.show_id, Command("id"))
    router.message.register(service.subscribe, Command("subscribe"))
    router.message.register(service.unsubscribe, Command("unsubscribe"))
    router.message.register(service.status, Command("status"))
    router.message.register(service.report_command, Command("report"))
    router.message.register(service.report_status, Command("report_status"))
    router.message.register(service.pipeline_status, Command("pipeline_status"))
    router.message.register(service.admin_panel, Command("admin"))
    router.message.register(
        service.admin_command,
        Command("add", "remove", "admin_add", "admin_remove"),
    )
    router.message.register(service.cancel, Command("cancel"))
    router.message.register(service.receive_admin_id, AdminInput.waiting_user_id)
    router.message.register(service.receive_report_date, ReportDateInput.waiting_date)
    router.message.register(service.fallback)

    router.callback_query.register(
        service.subscription_callback,
        F.data.in_({CALLBACK_SUBSCRIBE, CALLBACK_UNSUBSCRIBE}),
    )
    router.callback_query.register(service.status_callback, F.data == CALLBACK_STATUS)
    router.callback_query.register(
        service.report_date_callback, F.data == CALLBACK_REPORT_DATE
    )
    router.callback_query.register(
        service.excel_callback, F.data.startswith(CALLBACK_EXCEL_PREFIX)
    )
    router.callback_query.register(
        service.admin_panel_callback, F.data == CALLBACK_ADMIN_PANEL
    )
    router.callback_query.register(
        service.admin_list_callback, F.data == CALLBACK_ADMIN_LIST
    )
    router.callback_query.register(
        service.report_status_callback, F.data == CALLBACK_ADMIN_REPORT_STATUS
    )
    router.callback_query.register(
        service.pipeline_status_callback,
        F.data == CALLBACK_ADMIN_PIPELINE_STATUS,
    )
    router.callback_query.register(
        service.admin_test_report_callback, F.data == CALLBACK_ADMIN_TEST_REPORT
    )
    router.callback_query.register(
        service.admin_confirmation_callback,
        F.data.startswith(CALLBACK_ADMIN_CONFIRM_PREFIX),
    )
    router.callback_query.register(
        service.admin_confirmation_callback,
        F.data.startswith(CALLBACK_ADMIN_CANCEL_PREFIX),
    )
    admin_actions = {f"{CALLBACK_ADMIN_PREFIX}{name}" for name in ADMIN_ACTIONS}
    router.callback_query.register(
        service.admin_action_callback, F.data.in_(admin_actions)
    )
    return router


def _is_private(message: Any) -> bool:
    chat_type = getattr(getattr(message, "chat", None), "type", None)
    return chat_type in (ChatType.PRIVATE, ChatType.PRIVATE.value)


def _command_name(text: str | None) -> str | None:
    if not text:
        return None
    first = text.split(maxsplit=1)[0]
    if not first.startswith("/"):
        return None
    return first[1:].split("@", maxsplit=1)[0].casefold()


def _command_argument(text: str | None) -> str | None:
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return None
    return parts[1].strip()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise ValueError("Дата должна иметь формат YYYY-MM-DD.") from error


def _parse_user_id(value: str) -> int:
    clean = value.strip()
    try:
        user_id = int(clean)
    except ValueError as error:
        raise ValueError(
            "Telegram ID должен быть положительным целым числом."
        ) from error
    if user_id <= 0:
        raise ValueError("Telegram ID должен быть положительным целым числом.")
    return user_id


def _ids_text(values: Iterable[int]) -> str:
    ordered = tuple(sorted(set(int(value) for value in values)))
    return "\n".join(f"<code>{value}</code>" for value in ordered) or "—"
