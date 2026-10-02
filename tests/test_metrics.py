from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.bot.telegram import process_telegram_update
from app.bot.views import section_view
from app.core.config import get_settings
from app.services.domain import UIActionService, UserService
from app.services.metrics import MetricsService


class FakePipeline:
    def __init__(self, fake_redis):
        self.fake_redis = fake_redis
        self.commands = []

    def pfadd(self, key, value):
        self.commands.append(("pfadd", key, value))
        return self

    def hincrby(self, key, field, amount=1):
        self.commands.append(("hincrby", key, field, amount))
        return self

    def expire(self, key, ttl):
        self.commands.append(("expire", key, ttl))
        return self

    def execute(self):
        for cmd in self.commands:
            op = cmd[0]
            if op == "pfadd":
                self.fake_redis.pfadd(cmd[1], cmd[2])
            elif op == "hincrby":
                self.fake_redis.hincrby(cmd[1], cmd[2], cmd[3])
            elif op == "expire":
                self.fake_redis.expire(cmd[1], cmd[2])
        self.commands.clear()
        return True


class FakeRedis:
    def __init__(self):
        self.hll_sets = {}
        self.hashes = {}
        self.ttls = {}

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    def pfadd(self, key, value):
        if key not in self.hll_sets:
            self.hll_sets[key] = set()
        self.hll_sets[key].add(str(value))
        return 1

    def pfcount(self, key):
        return len(self.hll_sets.get(key, set()))

    def pfmerge(self, dest_key, *source_keys):
        merged = set()
        for k in source_keys:
            merged.update(self.hll_sets.get(k, set()))
        self.hll_sets[dest_key] = merged
        return True

    def hincrby(self, key, field, amount=1):
        if key not in self.hashes:
            self.hashes[key] = {}
        curr = int(self.hashes[key].get(field, 0))
        self.hashes[key][field] = str(curr + amount)
        return curr + amount

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def exists(self, key):
        has_hll = key in self.hll_sets and bool(self.hll_sets[key])
        has_hash = key in self.hashes and bool(self.hashes[key])
        return has_hll or has_hash

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return 1

    def delete(self, *keys):
        for k in keys:
            self.hll_sets.pop(k, None)
            self.hashes.pop(k, None)
            self.ttls.pop(k, None)
        return len(keys)


def test_metrics_hyperloglog_dau_and_activity():
    fake = FakeRedis()
    service = MetricsService(redis_client=fake)

    # User 101 sends 2 messages
    service.track_interaction(101, "text_messages")
    service.track_interaction(101, "text_messages")

    # User 102 sends 1 voice message and 1 button click
    service.track_interaction(102, "voice_messages")
    service.track_interaction(102, "buttons")

    summary = service.get_summary()
    assert summary["available"] is True
    # HyperLogLog distinct users: 101 and 102 -> DAU = 2
    assert summary["dau"] == 2
    # Total activity: 4 interactions
    assert summary["activity"]["total"] == 4
    assert summary["activity"]["text_messages"] == 2
    assert summary["activity"]["voice_messages"] == 1
    assert summary["activity"]["buttons"] == 1


def test_metrics_errors_tracking():
    fake = FakeRedis()
    service = MetricsService(redis_client=fake)

    service.track_error("llm", "llm_error")
    service.track_error("llm", "llm_error")
    service.track_error("stt", "stt_error")
    service.track_error("telegram", "telegram_error")

    summary = service.get_summary()
    assert summary["errors"]["llm_error"] == 2
    assert summary["errors"]["stt_error"] == 1
    assert summary["errors"]["telegram_error"] == 1


def test_metrics_wau_calculation():
    fake = FakeRedis()
    service = MetricsService(redis_client=fake)
    today = datetime.now(UTC).date()

    # User 101 active on day 0, 1, 2
    for day in range(3):
        d_str = (today - timedelta(days=day)).strftime("%Y-%m-%d")
        fake.pfadd(f"metrics:dau:{d_str}", "101")

    # User 202 active on day 3
    d3_str = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    fake.pfadd(f"metrics:dau:{d3_str}", "202")

    # User 303 active on day 5
    d5_str = (today - timedelta(days=5)).strftime("%Y-%m-%d")
    fake.pfadd(f"metrics:dau:{d5_str}", "303")

    summary = service.get_summary()
    # Today's DAU is 1 (user 101)
    assert summary["dau"] == 1
    # 7-day WAU is 3 unique users (101, 202, 303)
    assert summary["wau_7d"] == 3


def test_metrics_unavailable_redis():
    with patch("redis.from_url", side_effect=Exception("Redis connection error")):
        service = MetricsService()
        # Tracking methods must not crash when redis is down
        service.track_interaction(101, "text")
        service.track_error("llm", "llm_error")
        summary = service.get_summary()
        assert summary["available"] is False
        assert summary["dau"] == 0


def test_format_metrics_text():
    summary = {
        "date": "2026-10-02",
        "dau": 15,
        "wau_7d": 45,
        "activity": {
            "total": 120,
            "text_messages": 80,
            "voice_messages": 20,
            "buttons": 15,
            "commands": 5,
        },
        "errors": {
            "llm_error": 1,
            "stt_error": 0,
            "telegram_error": 0,
        },
    }
    text = MetricsService.format_metrics_text(summary)
    assert "2026-10-02" in text
    assert "DAU (активные сегодня):</b> 15 чел." in text
    assert "WAU (за 7 дней):</b> 45 чел." in text
    assert "Текстовые сообщения: 80" in text
    assert "Голосовые (Whisper): 20" in text
    assert "LLM (DeepSeek / GigaChat): 1" in text


def test_telegram_metrics_tracking(db):
    fake = FakeRedis()
    with patch("app.services.metrics.MetricsService.client", fake):
        # 1. Incoming text message
        payload = {
            "update_id": 1101,
            "message": {
                "message_id": 1,
                "from": {"id": 8801, "username": "u8801"},
                "chat": {"id": 8801, "type": "private"},
                "date": 1727800000,
                "text": "Привет",
            },
        }
        res = process_telegram_update(db, payload)
        assert res.get("ok") is True

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert fake.pfcount(f"metrics:dau:{today}") == 1
        assert int(fake.hashes[f"metrics:activity:{today}"]["text_messages"]) == 1

        # 2. Incoming voice message
        payload_voice = {
            "update_id": 1102,
            "message": {
                "message_id": 2,
                "from": {"id": 8802, "username": "u8802"},
                "chat": {"id": 8802, "type": "private"},
                "date": 1727800000,
                "voice": {"file_id": "v123", "duration": 5},
            },
        }
        with patch("app.workers.tasks.process_message.delay"):
            process_telegram_update(db, payload_voice)
        assert fake.pfcount(f"metrics:dau:{today}") == 2
        assert int(fake.hashes[f"metrics:activity:{today}"]["voice_messages"]) == 1


def test_admin_metrics_command_and_guard(db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "7701,7702")
    get_settings.cache_clear()
    fake = FakeRedis()

    with patch("app.services.metrics.MetricsService.client", fake):
        # Non-admin user tries to access /metrics
        non_admin_payload = {
            "update_id": 1201,
            "message": {
                "message_id": 10,
                "from": {"id": 8899, "username": "regular"},
                "chat": {"id": 8899, "type": "private"},
                "date": 1727800000,
                "text": "/metrics",
            },
        }
        res_non_admin = process_telegram_update(db, non_admin_payload)
        # Non-admin receives unknown command response (command hidden)
        assert res_non_admin.get("command") == "unknown"

        # Admin user calls /metrics
        admin_payload = {
            "update_id": 1202,
            "message": {
                "message_id": 11,
                "from": {"id": 7701, "username": "boss"},
                "chat": {"id": 7701, "type": "private"},
                "date": 1727800000,
                "text": "/metrics",
            },
        }
        with patch("app.bot.telegram.TelegramClient.send_message") as mock_send:
            res_admin = process_telegram_update(db, admin_payload)
            assert res_admin.get("command") == "metrics"
            mock_send.assert_called_once()
            call_text = mock_send.call_args[0][1]
            assert "Метрики сервиса TaskMate AI" in call_text


def test_admin_metrics_callback_action(db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "7701")
    get_settings.cache_clear()
    fake = FakeRedis()

    admin_user = UserService(db).get_or_create(7701)
    regular_user = UserService(db).get_or_create(8800)

    with patch("app.services.metrics.MetricsService.client", fake):
        # 1. Admin triggers admin_metrics_view callback
        action = UIActionService(db).create(admin_user, "admin_metrics_view", {"date": "2026-10-02"})
        callback_payload = {
            "update_id": 1301,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 7701},
                "data": action,
                "message": {"message_id": 99, "chat": {"id": 7701, "type": "private"}},
            },
        }
        with patch("app.bot.telegram.TelegramClient.edit_message") as mock_edit:
            res = process_telegram_update(db, callback_payload)
            assert res.get("action") == "admin_metrics_view"
            mock_edit.assert_called_once()

        # 2. Non-admin triggers admin_metrics_view callback
        non_admin_action = UIActionService(db).create(regular_user, "admin_metrics_view", {"date": "2026-10-02"})
        callback_forbidden = {
            "update_id": 1302,
            "callback_query": {
                "id": "cb2",
                "from": {"id": 8800},
                "data": non_admin_action,
                "message": {"message_id": 100, "chat": {"id": 8800, "type": "private"}},
            },
        }
        with patch("app.bot.telegram.TelegramClient.answer_callback") as mock_answer:
            res = process_telegram_update(db, callback_forbidden)
            assert res.get("forbidden") is True
            mock_answer.assert_called_with("cb2", "Доступ разрешен только администраторам")


def test_section_view_settings_admin_button(db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "7701")
    get_settings.cache_clear()

    admin_user = UserService(db).get_or_create(7701)
    regular_user = UserService(db).get_or_create(8800)

    # Regular user doesn't see admin metrics button
    _text, reg_markup = section_view(db, regular_user, "settings")
    reg_buttons = [btn["text"] for row in reg_markup["inline_keyboard"] for btn in row]
    assert not any("Метрики сервиса" in b for b in reg_buttons)

    # Admin user sees admin metrics button
    _text, admin_markup = section_view(db, admin_user, "settings")
    admin_buttons = [btn["text"] for row in admin_markup["inline_keyboard"] for btn in row]
    assert any("Метрики сервиса (Admin)" in b for b in admin_buttons)


def test_api_metrics_summary_route(db, client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "7701")
    get_settings.cache_clear()
    fake = FakeRedis()

    from app.core.security import issue_user_token

    admin_token = issue_user_token(7701)
    regular_token = issue_user_token(8800)

    with patch("app.services.metrics.MetricsService.client", fake):
        # Non-admin gets 403
        resp = client.get("/api/v1/metrics/summary", headers={"Authorization": f"Bearer {regular_token}"})
        assert resp.status_code == 403

        # Admin gets 200
        resp_admin = client.get("/api/v1/metrics/summary", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp_admin.status_code == 200
        data = resp_admin.json()["data"]
        assert "dau" in data
        assert "wau_7d" in data
        assert "activity" in data
        assert "errors" in data
