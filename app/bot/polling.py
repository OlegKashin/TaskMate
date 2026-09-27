import asyncio
import logging
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from app.bot.telegram import process_telegram_update
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.integrations.telegram.client import TelegramClient

logger = logging.getLogger(__name__)


def poll_once(
    client: TelegramClient,
    db: Session,
    offset: int | None = None,
    timeout: int = 30,
) -> tuple[int | None, list[dict[str, Any]]]:
    updates = client.get_updates(offset=offset, timeout=timeout)
    results = []
    new_offset = offset
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            new_offset = update_id + 1
        try:
            res = process_telegram_update(db, update)
            results.append(res)
        except Exception as exc:
            logger.exception("Error processing update %s: %s", update_id, exc)
            db.rollback()
    return new_offset, results


def run_polling(
    stop_event: threading.Event | asyncio.Event | None = None,
    max_iterations: int | None = None,
    sleep_interval: float = 0.5,
) -> None:
    settings = get_settings()
    client = TelegramClient(token=settings.telegram_bot_token)
    if not client.token:
        logger.warning("TELEGRAM_BOT_TOKEN is not configured; polling will not start.")
        return

    try:
        client.delete_webhook()
        logger.info("Deleted existing webhook (if any) to switch to Long Polling.")
    except Exception as exc:
        logger.warning("Could not delete webhook: %s", exc)

    logger.info("Starting Telegram Long Polling loop...")
    offset: int | None = None
    iterations = 0

    while True:
        if stop_event and stop_event.is_set():
            logger.info("Polling loop stop event received. Exiting.")
            break
        if max_iterations is not None and iterations >= max_iterations:
            break

        iterations += 1
        try:
            with SessionLocal() as db:
                offset, _ = poll_once(client, db, offset=offset, timeout=10)
        except Exception as exc:
            logger.error("Polling error: %s. Retrying in %s seconds...", exc, sleep_interval)
            time.sleep(sleep_interval)


async def start_polling_background(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, run_polling, stop_event)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_polling()
