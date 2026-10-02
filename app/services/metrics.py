"""Lightweight Redis-backed metrics tracking for DAU (HyperLogLog), interactions, and errors."""

import logging
from datetime import UTC, datetime, timedelta

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class MetricsService:
    def __init__(self, redis_client: redis.Redis | None = None):
        self._client = redis_client

    @property
    def client(self) -> redis.Redis | None:
        if self._client is not None:
            return self._client
        try:
            self._client = redis.from_url(get_settings().redis_url, decode_responses=True)
            return self._client
        except Exception as exc:
            logger.warning("Could not connect to Redis for metrics: %s", exc)
            return None

    @client.setter
    def client(self, val: redis.Redis | None) -> None:
        self._client = val

    def track_interaction(self, user_id: int | str, action_type: str = "text_messages") -> None:
        """
        Record user interaction for DAU and activity frequency.
        - DAU uses HyperLogLog (PFADD) -> 12KB per day, O(1), distinct user tracking.
        - Activity frequency uses Redis Hash (HINCRBY) per day.
        """
        r = self.client
        if not r:
            return
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        dau_key = f"metrics:dau:{today}"
        activity_key = f"metrics:activity:{today}"
        try:
            pipe = r.pipeline(transaction=False)
            pipe.pfadd(dau_key, str(user_id))
            pipe.hincrby(activity_key, action_type, 1)
            pipe.hincrby(activity_key, "total", 1)
            # Retain keys for 90 days
            pipe.expire(dau_key, 86400 * 90)
            pipe.expire(activity_key, 86400 * 90)
            pipe.execute()
        except Exception as exc:
            logger.debug("Failed to record user interaction metric: %s", exc)

    def track_error(self, service: str, error_type: str = "error") -> None:
        """
        Record service error in daily hash.
        Fields: 'llm_error', 'stt_error', 'telegram_error', etc.
        """
        r = self.client
        if not r:
            return
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        errors_key = f"metrics:errors:{today}"
        try:
            pipe = r.pipeline(transaction=False)
            pipe.hincrby(errors_key, error_type, 1)
            if service and error_type != service:
                pipe.hincrby(errors_key, f"{service}:{error_type}", 1)
            pipe.expire(errors_key, 86400 * 90)
            pipe.execute()
        except Exception as exc:
            logger.debug("Failed to record error metric: %s", exc)

    def get_summary(self, target_date: str | None = None) -> dict:
        """
        Calculate summary metrics for a given date (defaults to UTC today)
        including DAU, WAU (7-day unique users), interactions breakdown, and errors.
        """
        r = self.client
        today_date = datetime.now(UTC).date()
        date_str = target_date or today_date.strftime("%Y-%m-%d")
        if not r:
            return {
                "date": date_str,
                "dau": 0,
                "wau_7d": 0,
                "activity": {},
                "errors": {},
                "available": False,
            }

        dau_key = f"metrics:dau:{date_str}"
        activity_key = f"metrics:activity:{date_str}"
        errors_key = f"metrics:errors:{date_str}"

        try:
            dau = r.pfcount(dau_key) if r.exists(dau_key) else 0
            raw_activity = r.hgetall(activity_key) if r.exists(activity_key) else {}
            raw_errors = r.hgetall(errors_key) if r.exists(errors_key) else {}

            activity = {k: int(v) for k, v in raw_activity.items()}
            errors = {k: int(v) for k, v in raw_errors.items()}

            # Calculate 7-day WAU
            ref_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            keys_7d = [
                f"metrics:dau:{(ref_date - timedelta(days=i)).strftime('%Y-%m-%d')}"
                for i in range(7)
            ]
            existing_7d = [k for k in keys_7d if r.exists(k)]
            wau = 0
            if existing_7d:
                temp_union_key = f"temp:wau:{date_str}:{datetime.now(UTC).timestamp()}"
                try:
                    r.pfmerge(temp_union_key, *existing_7d)
                    wau = r.pfcount(temp_union_key)
                finally:
                    r.delete(temp_union_key)

            return {
                "date": date_str,
                "dau": dau,
                "wau_7d": wau,
                "activity": activity,
                "errors": errors,
                "available": True,
            }
        except Exception as exc:
            logger.warning("Error fetching metrics summary: %s", exc)
            return {
                "date": date_str,
                "dau": 0,
                "wau_7d": 0,
                "activity": {},
                "errors": {},
                "available": False,
            }

    @staticmethod
    def format_metrics_text(summary: dict) -> str:
        """Format metrics summary dictionary as a readable Telegram message."""
        date_str = summary.get("date", "")
        dau = summary.get("dau", 0)
        wau = summary.get("wau_7d", 0)
        activity = summary.get("activity", {})
        errors = summary.get("errors", {})

        total_interactions = activity.get("total", 0)
        text_msgs = activity.get("text_messages", 0)
        voice_msgs = activity.get("voice_messages", 0)
        buttons = activity.get("buttons", 0)
        commands = activity.get("commands", 0)

        avg_freq = round(total_interactions / dau, 1) if dau > 0 else 0.0

        llm_errors = errors.get("llm_error", 0)
        stt_errors = errors.get("stt_error", 0)
        tg_errors = errors.get("telegram_error", 0)
        total_errors = sum(v for k, v in errors.items() if ":" not in k)

        lines = [
            "📊 <b>Метрики сервиса TaskMate AI</b>",
            f"📅 Дата: <code>{date_str}</code>",
            "",
            "👥 <b>Пользователи:</b>",
            f"• <b>DAU (активные сегодня):</b> {dau} чел.",
            f"• <b>WAU (за 7 дней):</b> {wau} чел.",
            "",
            f"🔄 <b>Взаимодействия ({total_interactions} всего):</b>",
            f"• 💬 Текстовые сообщения: {text_msgs}",
            f"• 🎙 Голосовые (Whisper): {voice_msgs}",
            f"• 🔘 Нажатия кнопок: {buttons}",
            f"• ⌨️ Команды: {commands}",
            f"• ⚡ Частотность: {avg_freq} действ./чел.",
            "",
            f"⚠️ <b>Ошибки сервисов ({total_errors} всего):</b>",
            f"• 🧠 LLM (DeepSeek / GigaChat): {llm_errors}",
            f"• 🎙 STT (Whisper): {stt_errors}",
            f"• 🤖 Telegram / бот: {tg_errors}",
        ]
        return "\n".join(lines)
