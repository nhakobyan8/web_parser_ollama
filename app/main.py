from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from app.bot_ui import BotContext, create_router
from app.config import Settings
from app.exporters import CsvExporter, TelegramPublisher
from app.fetcher import SafeHtmlFetcher
from app.monitor import MonitorService
from app.ollama_client import OllamaClient
from app.pipeline import NewsPipeline
from app.storage import JsonStorage


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )


async def run() -> None:
    configure_logging()
    logger = logging.getLogger(__name__)
    load_dotenv()
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)

    storage = JsonStorage(settings.database_path, settings.default_interval_seconds)
    await storage.initialize(settings.admin_ids)

    bot = Bot(token=settings.telegram_bot_token)
    fetcher = SafeHtmlFetcher(
        timeout_seconds=settings.request_timeout_seconds,
        attempts=settings.request_attempts,
        max_html_bytes=settings.max_html_bytes,
    )
    ollama = OllamaClient(
        base_url=settings.ollama_url,
        model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
        num_ctx=settings.ollama_num_ctx,
        keep_alive=settings.ollama_keep_alive,
    )
    csv_exporter = CsvExporter(settings.exports_dir)
    publisher = TelegramPublisher(bot)
    pipeline = NewsPipeline(
        settings=settings,
        storage=storage,
        fetcher=fetcher,
        ollama=ollama,
        csv_exporter=csv_exporter,
        telegram_publisher=publisher,
    )
    monitor = MonitorService(
        settings=settings,
        storage=storage,
        pipeline=pipeline,
        bot=bot,
    )
    context = BotContext(
        settings=settings,
        storage=storage,
        monitor=monitor,
        csv_exporter=csv_exporter,
        publisher=publisher,
        bot=bot,
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(create_router(context))

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        logger.info("Waiting for Ollama and model %s", settings.ollama_model)
        await ollama.wait_until_ready()
        await monitor.start()
        logger.info("Bot started")
        polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
                handle_signals=False,
            ),
            name="telegram-polling",
        )
        stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-waiter")
        done, pending = await asyncio.wait(
            {polling_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task is polling_task:
                task.result()
    finally:
        await monitor.stop()
        await dispatcher.storage.close()
        await fetcher.close()
        await ollama.close()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(run())
