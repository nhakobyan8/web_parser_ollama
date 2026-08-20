from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import Settings
from app.defaults import (
    DEFAULT_DISCOVERY_PROMPT,
    DEFAULT_EXTRACTION_PROMPT,
    DEFAULT_PROCESSING_PROMPT,
)
from app.exporters import CsvExporter, TelegramPublisher, split_telegram_text
from app.fetcher import FetchError, validate_public_url
from app.models import UserProfile
from app.monitor import MonitorService
from app.storage import JsonStorage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BotContext:
    settings: Settings
    storage: JsonStorage
    monitor: MonitorService
    csv_exporter: CsvExporter
    publisher: TelegramPublisher
    bot: Bot


class InputStates(StatesGroup):
    add_source = State()
    interval = State()
    edit_prompt = State()
    channel_id = State()
    publisher_token = State()
    add_user = State()


class AccessMiddleware(BaseMiddleware):
    def __init__(self, storage: JsonStorage) -> None:
        self.storage = storage

    async def __call__(self, handler, event: TelegramObject, data: dict):
        telegram_user = data.get("event_from_user")
        if telegram_user is None:
            return await handler(event, data)
        profile = await self.storage.get_user(telegram_user.id)
        if profile and profile.active:
            data["profile"] = profile
            return await handler(event, data)

        text = (
            "Access to this bot has not been granted yet.\n"
            f"Your Telegram ID: {telegram_user.id}\n"
            "Send this ID to the administrator."
        )
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer("Access denied", show_alert=True)
        return None


def main_menu(profile: UserProfile) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Sources", callback_data="menu:sources")
    builder.button(text="⏱ Interval", callback_data="menu:interval")
    builder.button(text="🧠 Prompts", callback_data="menu:prompts")
    builder.button(text="📤 Export and channel", callback_data="menu:outputs")
    builder.button(text="📊 Status", callback_data="menu:status")
    builder.button(text="▶️ Check now", callback_data="run:all")
    builder.button(
        text="▶️ Resume" if profile.settings.paused else "⏸ Pause",
        callback_data="settings:pause",
    )
    if profile.role == "admin":
        builder.button(text="👥 Users", callback_data="menu:users")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def sources_menu(profile: UserProfile) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Add source", callback_data="source:add")
    for source in profile.sources:
        name = source.name or (urlsplit(source.url).hostname or source.url)
        icon = "🟢" if source.enabled else "⚪️"
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {name}"[:48],
                callback_data=f"source:toggle:{source.id}",
            )
        )
        builder.row(
            InlineKeyboardButton(text="▶️ Check", callback_data=f"source:run:{source.id}"),
            InlineKeyboardButton(text="🗑 Remove", callback_data=f"source:remove:{source.id}"),
        )
    builder.row(InlineKeyboardButton(text="⬅️ Main menu", callback_data="menu:main"))
    return builder.as_markup()


def prompts_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 Discovery prompt", callback_data="prompt:edit:discovery"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📄 Extraction prompt", callback_data="prompt:edit:extraction"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Processing prompt", callback_data="prompt:edit:processing"
                )
            ],
            [InlineKeyboardButton(text="♻️ Reset all prompts", callback_data="prompt:reset:all")],
            [InlineKeyboardButton(text="⬅️ Main menu", callback_data="menu:main")],
        ]
    )


def outputs_menu(profile: UserProfile) -> InlineKeyboardMarkup:
    output = profile.settings.outputs
    csv_icon = "✅" if output.csv_enabled else "❌"
    telegram_icon = "✅" if output.telegram_enabled else "❌"
    first_item_icon = "✅" if profile.settings.process_first_item else "❌"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{csv_icon} Save CSV", callback_data="output:toggle:csv")],
            [
                InlineKeyboardButton(
                    text=f"{telegram_icon} Publish to Telegram",
                    callback_data="output:toggle:telegram",
                )
            ],
            [InlineKeyboardButton(text="📢 Set channel", callback_data="output:set:channel")],
            [InlineKeyboardButton(text="🔑 Publisher bot token", callback_data="output:set:token")],
            [InlineKeyboardButton(text="🧹 Use control bot", callback_data="output:clear:token")],
            [InlineKeyboardButton(text="🧪 Test publication", callback_data="output:test")],
            [InlineKeyboardButton(text="📥 Download CSV", callback_data="output:export:csv")],
            [
                InlineKeyboardButton(
                    text=f"{first_item_icon} Process first item",
                    callback_data="settings:first_item",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Main menu", callback_data="menu:main")],
        ]
    )


def users_menu(users: list[UserProfile]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Add user", callback_data="user:add")
    for user in users:
        if user.role == "admin":
            continue
        label = user.display_name or str(user.telegram_id)
        state = "🟢" if user.active else "🔴"
        builder.row(
            InlineKeyboardButton(
                text=f"{state} {label}"[:52],
                callback_data=f"user:toggle:{user.telegram_id}",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Main menu", callback_data="menu:main"))
    return builder.as_markup()


async def replace_or_send(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:
            await callback.message.answer(text, reply_markup=markup)


def parse_interval(value: str) -> int:
    raw = value.strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d+)(s|m|h)?", raw)
    if not match:
        raise ValueError("Use the format 300, 30s, 5m, or 1h")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return amount * multiplier


def interval_label(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hr"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} sec"


def format_outputs(profile: UserProfile) -> str:
    output = profile.settings.outputs
    if output.publisher_bot_token:
        token_state = f"separate token …{output.publisher_bot_token[-6:]}"
    else:
        token_state = "control bot"
    first_item_state = (
        "process" if profile.settings.process_first_item else "save only as the initial state"
    )
    return (
        "Export and publication\n\n"
        f"CSV: {'enabled' if output.csv_enabled else 'disabled'}\n"
        f"Telegram: {'enabled' if output.telegram_enabled else 'disabled'}\n"
        f"Channel: {output.telegram_channel_id or 'not configured'}\n"
        f"Publisher: {token_state}\n"
        f"First discovered item: {first_item_state}"
    )


def format_status(profile: UserProfile, model: str) -> str:
    enabled = sum(source.enabled for source in profile.sources)
    processed = len(profile.processed)
    lines = [
        "Monitoring status",
        "",
        f"Model: {model}",
        f"Monitoring: {'paused' if profile.settings.paused else 'running'}",
        f"Interval: {interval_label(profile.settings.interval_seconds)}",
        f"Sources: {enabled} active out of {len(profile.sources)}",
        f"Processed items: {processed}",
    ]
    for source in profile.sources:
        name = source.name or (urlsplit(source.url).hostname or source.url)
        state = "enabled" if source.enabled else "disabled"
        lines.append(f"\n• {name} — {state}")
        if source.last_checked_at:
            lines.append(f"  last check: {source.last_checked_at}")
        if source.last_seen_url:
            lines.append(f"  latest URL: {source.last_seen_url}")
        if source.last_error:
            lines.append(f"  error: {source.last_error[:250]}")
    return "\n".join(lines)


def create_router(context: BotContext) -> Router:
    router = Router(name="control-bot")
    router.message.middleware(AccessMiddleware(context.storage))
    router.callback_query.middleware(AccessMiddleware(context.storage))

    @router.message(Command("cancel"))
    async def cancel_input(message: Message, state: FSMContext, profile: UserProfile) -> None:
        await state.clear()
        fresh = await context.storage.get_user(profile.telegram_id)
        await message.answer("Input cancelled.", reply_markup=main_menu(fresh or profile))

    @router.message(CommandStart())
    @router.message(Command("menu"))
    async def start(message: Message, state: FSMContext, profile: UserProfile) -> None:
        await state.clear()
        full_name = message.from_user.full_name if message.from_user else None
        await context.storage.add_user(
            profile.telegram_id,
            added_by=profile.added_by or profile.telegram_id,
            display_name=full_name,
            role=profile.role,
        )
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await message.answer(
            "The source monitoring bot is ready. All settings and results are stored "
            "separately for your Telegram ID.",
            reply_markup=main_menu(fresh),
        )

    @router.callback_query(F.data == "menu:main")
    async def show_main(callback: CallbackQuery, state: FSMContext, profile: UserProfile) -> None:
        await state.clear()
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await replace_or_send(callback, "Main menu", main_menu(fresh))
        await callback.answer()

    @router.callback_query(F.data == "menu:sources")
    async def show_sources(callback: CallbackQuery, profile: UserProfile) -> None:
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        text = "Monitoring sources" if fresh.sources else "No sources have been added yet."
        await replace_or_send(callback, text, sources_menu(fresh))
        await callback.answer()

    @router.callback_query(F.data == "source:add")
    async def source_add(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(InputStates.add_source)
        if callback.message:
            await callback.message.answer(
                "Send the complete URL of the page containing the general publication stream.\n"
                "Example: https://example.com/news\n\n/cancel — cancel"
            )
        await callback.answer()

    @router.message(StateFilter(InputStates.add_source), F.text)
    async def source_add_value(message: Message, state: FSMContext, profile: UserProfile) -> None:
        url = message.text.strip()
        try:
            normalized = await validate_public_url(url)
            name = urlsplit(normalized).hostname
            await context.storage.add_source(profile.telegram_id, normalized, name=name)
        except (FetchError, ValueError) as exc:
            await message.answer(f"Could not add the source: {exc}\nSend another URL or /cancel.")
            return
        await state.clear()
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await message.answer("Source added.", reply_markup=sources_menu(fresh))

    @router.callback_query(F.data.startswith("source:toggle:"))
    async def source_toggle(callback: CallbackQuery, profile: UserProfile) -> None:
        source_id = callback.data.rsplit(":", 1)[-1]
        source = await context.storage.toggle_source(profile.telegram_id, source_id)
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await replace_or_send(callback, "Source settings updated.", sources_menu(fresh))
        await callback.answer("Source enabled" if source and source.enabled else "Source disabled")

    @router.callback_query(F.data.startswith("source:remove:"))
    async def source_remove(callback: CallbackQuery, profile: UserProfile) -> None:
        source_id = callback.data.rsplit(":", 1)[-1]
        removed = await context.storage.remove_source(profile.telegram_id, source_id)
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await replace_or_send(
            callback, "Source removed." if removed else "Source not found.", sources_menu(fresh)
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("source:run:"))
    async def source_run(callback: CallbackQuery, profile: UserProfile) -> None:
        source_id = callback.data.rsplit(":", 1)[-1]
        asyncio.create_task(
            context.monitor.run_source(profile.telegram_id, source_id, force=True, notify=True)
        )
        await callback.answer("Check started", show_alert=False)

    @router.callback_query(F.data == "run:all")
    async def run_all(callback: CallbackQuery, profile: UserProfile) -> None:
        asyncio.create_task(context.monitor.run_all_for_user(profile.telegram_id, force=True))
        await callback.answer("All source checks have started", show_alert=True)

    @router.callback_query(F.data == "menu:interval")
    async def interval_menu(
        callback: CallbackQuery, state: FSMContext, profile: UserProfile
    ) -> None:
        await state.set_state(InputStates.interval)
        if callback.message:
            await callback.message.answer(
                f"Current interval: {interval_label(profile.settings.interval_seconds)}\n"
                "Send a new interval: 300, 30s, 5m, or 1h.\n\n/cancel — cancel"
            )
        await callback.answer()

    @router.message(StateFilter(InputStates.interval), F.text)
    async def interval_value(message: Message, state: FSMContext, profile: UserProfile) -> None:
        try:
            seconds = parse_interval(message.text)
            if seconds < context.settings.min_interval_seconds:
                raise ValueError(
                    f"The minimum interval is {context.settings.min_interval_seconds} seconds"
                )
            if seconds > 7 * 24 * 3600:
                raise ValueError("The maximum interval is 7 days")
        except ValueError as exc:
            await message.answer(f"Invalid interval: {exc}")
            return
        fresh = await context.storage.set_user_field(
            profile.telegram_id, "interval_seconds", seconds
        )
        await state.clear()
        await message.answer(
            f"Interval set to {interval_label(seconds)}",
            reply_markup=main_menu(fresh),
        )

    @router.callback_query(F.data == "menu:prompts")
    async def show_prompts(callback: CallbackQuery) -> None:
        await replace_or_send(callback, "Select a prompt to edit.", prompts_menu())
        await callback.answer()

    @router.callback_query(F.data.startswith("prompt:edit:"))
    async def edit_prompt(callback: CallbackQuery, state: FSMContext, profile: UserProfile) -> None:
        prompt_type = callback.data.rsplit(":", 1)[-1]
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        current = getattr(fresh.settings.prompts, prompt_type)
        await state.update_data(prompt_type=prompt_type)
        await state.set_state(InputStates.edit_prompt)
        if callback.message:
            for chunk in split_telegram_text(
                f"Current {prompt_type} prompt:\n\n{current}\n\n"
                "Send the complete new prompt text. /cancel — cancel"
            ):
                await callback.message.answer(chunk)
        await callback.answer()

    @router.message(StateFilter(InputStates.edit_prompt), F.text)
    async def edit_prompt_value(message: Message, state: FSMContext, profile: UserProfile) -> None:
        data = await state.get_data()
        prompt_type = data.get("prompt_type")
        value = message.text.strip()
        if prompt_type not in {"discovery", "extraction", "processing"}:
            await state.clear()
            await message.answer("The editing state was lost. Repeat the action from the menu.")
            return
        if len(value) < 10:
            await message.answer("The prompt is too short. Send the full text or /cancel.")
            return
        if len(value) > 20_000:
            await message.answer("The prompt must not exceed 20,000 characters.")
            return
        await context.storage.set_prompt(profile.telegram_id, prompt_type, value)
        await state.clear()
        await message.answer(f"{prompt_type} prompt saved.", reply_markup=prompts_menu())

    @router.callback_query(F.data == "prompt:reset:all")
    async def reset_prompts(callback: CallbackQuery, profile: UserProfile) -> None:
        await context.storage.set_prompt(profile.telegram_id, "discovery", DEFAULT_DISCOVERY_PROMPT)
        await context.storage.set_prompt(
            profile.telegram_id, "extraction", DEFAULT_EXTRACTION_PROMPT
        )
        await context.storage.set_prompt(
            profile.telegram_id, "processing", DEFAULT_PROCESSING_PROMPT
        )
        await replace_or_send(
            callback, "All prompts have been reset to their defaults.", prompts_menu()
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:outputs")
    async def show_outputs(callback: CallbackQuery, profile: UserProfile) -> None:
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await replace_or_send(callback, format_outputs(fresh), outputs_menu(fresh))
        await callback.answer()

    @router.callback_query(F.data == "output:toggle:csv")
    async def toggle_csv(callback: CallbackQuery, profile: UserProfile) -> None:
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await context.storage.set_output_field(
            profile.telegram_id, "csv_enabled", not fresh.settings.outputs.csv_enabled
        )
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await replace_or_send(callback, format_outputs(fresh), outputs_menu(fresh))
        await callback.answer()

    @router.callback_query(F.data == "output:toggle:telegram")
    async def toggle_telegram(callback: CallbackQuery, profile: UserProfile) -> None:
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        new_value = not fresh.settings.outputs.telegram_enabled
        if new_value and not fresh.settings.outputs.telegram_channel_id:
            await callback.answer("Configure a channel first", show_alert=True)
            return
        await context.storage.set_output_field(profile.telegram_id, "telegram_enabled", new_value)
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await replace_or_send(callback, format_outputs(fresh), outputs_menu(fresh))
        await callback.answer()

    @router.callback_query(F.data == "output:set:channel")
    async def set_channel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(InputStates.channel_id)
        if callback.message:
            await callback.message.answer(
                "Send a channel ID in the form -1001234567890 or a public @channel name.\n\n"
                "/cancel — cancel"
            )
        await callback.answer()

    @router.message(StateFilter(InputStates.channel_id), F.text)
    async def set_channel_value(message: Message, state: FSMContext, profile: UserProfile) -> None:
        value = message.text.strip()
        if not (value.startswith("@") or value.lstrip("-").isdigit()):
            await message.answer("Enter a numeric channel ID or @username.")
            return
        await context.storage.set_output_field(profile.telegram_id, "telegram_channel_id", value)
        await state.clear()
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await message.answer("Channel saved.", reply_markup=outputs_menu(fresh))

    @router.callback_query(F.data == "output:set:token")
    async def set_token(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(InputStates.publisher_token)
        if callback.message:
            await callback.message.answer(
                "Send the token of a separate Telegram bot that is an administrator "
                "of the channel.\n"
                "To publish through the control bot, select “Use control bot”.\n\n"
                "/cancel — cancel"
            )
        await callback.answer()

    @router.message(StateFilter(InputStates.publisher_token), F.text)
    async def set_token_value(message: Message, state: FSMContext, profile: UserProfile) -> None:
        token = message.text.strip()
        if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", token):
            await message.answer("The token format is invalid. Send the token again or /cancel.")
            return
        await context.storage.set_output_field(profile.telegram_id, "publisher_bot_token", token)
        await state.clear()
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await message.answer("Publisher token saved.", reply_markup=outputs_menu(fresh))

    @router.callback_query(F.data == "output:clear:token")
    async def clear_token(callback: CallbackQuery, profile: UserProfile) -> None:
        await context.storage.set_output_field(profile.telegram_id, "publisher_bot_token", None)
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await replace_or_send(callback, format_outputs(fresh), outputs_menu(fresh))
        await callback.answer("The control bot will be used")

    @router.callback_query(F.data == "output:test")
    async def test_publication(callback: CallbackQuery, profile: UserProfile) -> None:
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        try:
            await context.publisher.test(fresh)
        except Exception as exc:
            await callback.answer(f"Error: {str(exc)[:150]}", show_alert=True)
            return
        await callback.answer("Test publication sent", show_alert=True)

    @router.callback_query(F.data == "output:export:csv")
    async def export_csv(callback: CallbackQuery, profile: UserProfile) -> None:
        path = context.csv_exporter.path_for(profile.telegram_id)
        if not path.exists() or path.stat().st_size == 0:
            await callback.answer("The CSV file is empty", show_alert=True)
            return
        if callback.message:
            await callback.message.answer_document(FSInputFile(path), caption="Current CSV report")
        await callback.answer()

    @router.callback_query(F.data == "settings:first_item")
    async def toggle_first_item(callback: CallbackQuery, profile: UserProfile) -> None:
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await context.storage.set_user_field(
            profile.telegram_id,
            "process_first_item",
            not fresh.settings.process_first_item,
        )
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        await replace_or_send(callback, format_outputs(fresh), outputs_menu(fresh))
        await callback.answer()

    @router.callback_query(F.data == "settings:pause")
    async def toggle_pause(callback: CallbackQuery, profile: UserProfile) -> None:
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        fresh = await context.storage.set_user_field(
            profile.telegram_id, "paused", not fresh.settings.paused
        )
        await replace_or_send(
            callback,
            "Monitoring paused." if fresh.settings.paused else "Monitoring resumed.",
            main_menu(fresh),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:status")
    async def show_status(callback: CallbackQuery, profile: UserProfile) -> None:
        fresh = await context.storage.get_user(profile.telegram_id) or profile
        text = format_status(fresh, context.settings.ollama_model)
        if callback.message:
            for chunk in split_telegram_text(text):
                await callback.message.answer(chunk)
        await callback.answer()

    @router.message(Command("admin"))
    async def admin_command(message: Message, profile: UserProfile) -> None:
        if profile.role != "admin":
            await message.answer("This command is available only to administrators.")
            return
        users = await context.storage.list_users()
        await message.answer("User management", reply_markup=users_menu(users))

    @router.callback_query(F.data == "menu:users")
    async def show_users(callback: CallbackQuery, profile: UserProfile) -> None:
        if profile.role != "admin":
            await callback.answer("Insufficient permissions", show_alert=True)
            return
        users = await context.storage.list_users()
        await replace_or_send(callback, "User management", users_menu(users))
        await callback.answer()

    @router.callback_query(F.data == "user:add")
    async def add_user(callback: CallbackQuery, state: FSMContext, profile: UserProfile) -> None:
        if profile.role != "admin":
            await callback.answer("Insufficient permissions", show_alert=True)
            return
        await state.set_state(InputStates.add_user)
        if callback.message:
            await callback.message.answer(
                "Send the user's Telegram ID. You may add a name after a space.\n"
                "Example: 123456789 Ivan\n\n/cancel — cancel"
            )
        await callback.answer()

    @router.message(StateFilter(InputStates.add_user), F.text)
    async def add_user_value(message: Message, state: FSMContext, profile: UserProfile) -> None:
        if profile.role != "admin":
            await state.clear()
            return
        parts = message.text.strip().split(maxsplit=1)
        try:
            telegram_id = int(parts[0])
            if telegram_id <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Telegram ID must be a positive integer.")
            return
        name = parts[1].strip() if len(parts) > 1 else None
        added, created = await context.storage.add_user(
            telegram_id,
            added_by=profile.telegram_id,
            display_name=name,
        )
        await state.clear()
        users = await context.storage.list_users()
        action = "added" if created else "reactivated"
        await message.answer(
            f"User {added.telegram_id} {action}.",
            reply_markup=users_menu(users),
        )

    @router.callback_query(F.data.startswith("user:toggle:"))
    async def toggle_user(callback: CallbackQuery, profile: UserProfile) -> None:
        if profile.role != "admin":
            await callback.answer("Insufficient permissions", show_alert=True)
            return
        telegram_id = int(callback.data.rsplit(":", 1)[-1])
        target = await context.storage.get_user(telegram_id)
        if not target or target.role == "admin":
            await callback.answer("User not found", show_alert=True)
            return
        if target.active:
            await context.storage.deactivate_user(telegram_id)
            message_text = "User disabled."
        else:
            await context.storage.add_user(
                telegram_id,
                added_by=profile.telegram_id,
                display_name=target.display_name,
            )
            message_text = "User enabled."
        users = await context.storage.list_users()
        await replace_or_send(callback, message_text, users_menu(users))
        await callback.answer()

    return router
