import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.bot.actions import handle_action
from app.core.config import get_settings
from app.core.timezones import (
    find_timezone_by_coordinates,
    get_timezone_display,
    resolve_timezone,
    timezone_picker_menu,
)
from app.models.entities import UIAction
from app.services.domain import UserService


def test_resolve_timezone_iana():
    assert resolve_timezone("Europe/Moscow") == "Europe/Moscow"
    assert resolve_timezone("Asia/Yekaterinburg") == "Asia/Yekaterinburg"
    assert resolve_timezone("UTC") == "UTC"
    assert resolve_timezone("America/New_York") == "America/New_York"


def test_resolve_timezone_city_names():
    assert resolve_timezone("москва") == "Europe/Moscow"
    assert resolve_timezone("Москва") == "Europe/Moscow"
    assert resolve_timezone("мск") == "Europe/Moscow"
    assert resolve_timezone("Питер") == "Europe/Moscow"
    assert resolve_timezone("СПб") == "Europe/Moscow"
    assert resolve_timezone("санкт-петербург") == "Europe/Moscow"
    assert resolve_timezone("екатеринбург") == "Asia/Yekaterinburg"
    assert resolve_timezone("Екб") == "Asia/Yekaterinburg"
    assert resolve_timezone("самара") == "Europe/Samara"
    assert resolve_timezone("новосибирск") == "Asia/Novosibirsk"
    assert resolve_timezone("калининград") == "Europe/Kaliningrad"
    assert resolve_timezone("владивосток") == "Asia/Vladivostok"
    assert resolve_timezone("красноярск") == "Asia/Krasnoyarsk"
    assert resolve_timezone("иркутск") == "Asia/Irkutsk"
    assert resolve_timezone("минск") == "Europe/Minsk"
    assert resolve_timezone("алматы") == "Asia/Almaty"
    assert resolve_timezone("астана") == "Asia/Almaty"
    assert resolve_timezone("ташкент") == "Asia/Tashkent"
    assert resolve_timezone("тбилиси") == "Asia/Tbilisi"
    assert resolve_timezone("лондон") == "Europe/London"
    assert resolve_timezone("London") == "Europe/London"
    assert resolve_timezone("берлин") == "Europe/Berlin"
    assert resolve_timezone("париж") == "Europe/Paris"
    assert resolve_timezone("нью-йорк") == "America/New_York"
    assert resolve_timezone("дубай") == "Asia/Dubai"


def test_resolve_timezone_offsets():
    assert resolve_timezone("UTC+3") == "Europe/Moscow"
    assert resolve_timezone("UTC+4") == "Europe/Samara"
    assert resolve_timezone("UTC+5") == "Asia/Yekaterinburg"
    assert resolve_timezone("UTC+7") == "Asia/Novosibirsk"
    assert resolve_timezone("+3") == "Europe/Moscow"
    assert resolve_timezone("+04:00") == "Europe/Samara"
    assert resolve_timezone("+5") == "Asia/Yekaterinburg"
    assert resolve_timezone("-5") == "America/New_York"
    assert resolve_timezone("GMT+3") == "Europe/Moscow"
    assert resolve_timezone("GMT-8") == "America/Los_Angeles"


def test_resolve_timezone_invalid_and_empty():
    assert resolve_timezone("") is None
    assert resolve_timezone("   ") is None
    assert resolve_timezone("Mars_City_9999") is None
    assert resolve_timezone("неизвестный_город_12345") is None


def test_find_timezone_by_coordinates():
    # Moscow coordinates
    assert find_timezone_by_coordinates(55.75, 37.61) == "Europe/Moscow"
    # Ekaterinburg coordinates
    assert find_timezone_by_coordinates(56.84, 60.61) == "Asia/Yekaterinburg"
    # Vladivostok coordinates
    assert find_timezone_by_coordinates(43.12, 131.89) == "Asia/Vladivostok"
    # London coordinates
    assert find_timezone_by_coordinates(51.51, -0.13) == "Europe/London"
    # New York coordinates
    assert find_timezone_by_coordinates(40.71, -74.01) == "America/New_York"


def test_get_timezone_display():
    assert "Москва" in get_timezone_display("Europe/Moscow")
    assert "Екатеринбург" in get_timezone_display("Asia/Yekaterinburg")
    assert get_timezone_display("Nonexistent/Zone") == "Nonexistent/Zone"


def test_timezone_picker_menu(db):
    user = UserService(db).get_or_create(9101, telegram_username="tz_tester")
    menu = timezone_picker_menu(db, user)
    assert "inline_keyboard" in menu
    rows = menu["inline_keyboard"]
    all_buttons = [b for row in rows for b in row]
    texts = [b["text"] for b in all_buttons]

    # Verify key cities are present
    assert any("Москва" in t for t in texts)
    assert any("Екатеринбург" in t for t in texts)
    assert any("Самара" in t for t in texts)
    assert any("Новосибирск" in t for t in texts)
    assert any("Определить по геолокации" in t for t in texts)
    assert any("В настройки" in t for t in texts)


def test_setting_timezone_action(db):
    user = UserService(db).get_or_create(9102, telegram_username="tz_user_action")
    user.timezone = "Europe/Moscow"
    db.commit()

    text, markup = handle_action(db, user, SimpleNamespace(action="setting_timezone", payload={}))
    assert "/timezone" in text
    assert "Москва" in text
    assert markup and "inline_keyboard" in markup


def test_setting_set_timezone_action(db):
    user = UserService(db).get_or_create(9103, telegram_username="tz_set_action")
    user.timezone = "Europe/Moscow"
    db.commit()

    text, markup = handle_action(
        db, user, SimpleNamespace(action="setting_set_timezone", payload={"timezone": "Asia/Yekaterinburg"})
    )
    assert user.timezone == "Asia/Yekaterinburg"
    assert "Екатеринбург" in text
    assert "Часовой пояс изменён" in text
    assert markup and "inline_keyboard" in markup


def test_setting_request_location_action(db):
    user = UserService(db).get_or_create(9104, telegram_username="tz_loc_req")
    text, markup = handle_action(
        db, user, SimpleNamespace(action="setting_request_location", payload={})
    )
    assert "Отправить моё местоположение" in text
    assert markup and "keyboard" in markup
    assert markup.get("resize_keyboard") is True
    buttons = [b for row in markup["keyboard"] for b in row]
    assert any(b.get("request_location") is True for b in buttons)
    assert any("Отмена" in b.get("text", "") for b in buttons)


def test_telegram_location_update(db, client):
    user = UserService(db).get_or_create(9105, telegram_username="geo_user")
    user.timezone = "UTC"
    db.commit()

    secret = get_settings().telegram_webhook_secret
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    with patch("app.bot.telegram.TelegramClient.send_message") as mock_send:
        res = client.post(
            "/webhooks/telegram",
            headers=headers,
            json={
                "update_id": 9901,
                "message": {
                    "message_id": 1001,
                    "from": {"id": 9105, "username": "geo_user"},
                    "chat": {"id": 9105, "type": "private"},
                    "date": 1727800000,
                    "location": {
                        "latitude": 56.84,
                        "longitude": 60.61,
                    },
                },
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data.get("ok") is True
        assert data.get("location_timezone") == "Asia/Yekaterinburg"
        assert user.timezone == "Asia/Yekaterinburg"

        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == 9105
        assert "Екатеринбург" in args[1]
        assert args[2] == {"remove_keyboard": True}


def test_telegram_location_cancel(db, client):
    user = UserService(db).get_or_create(9106, telegram_username="geo_cancel_user")
    secret = get_settings().telegram_webhook_secret
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    with patch("app.bot.telegram.TelegramClient.send_message") as mock_send:
        res = client.post(
            "/webhooks/telegram",
            headers=headers,
            json={
                "update_id": 9902,
                "message": {
                    "message_id": 1002,
                    "from": {"id": 9106, "username": "geo_cancel_user"},
                    "chat": {"id": 9106, "type": "private"},
                    "date": 1727800000,
                    "text": "❌ Отмена",
                },
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data.get("location_cancelled") is True
        mock_send.assert_called_once()
        args, _ = mock_send.call_args
        assert "отменено" in args[1]
        assert args[2] == {"remove_keyboard": True}


def test_telegram_timezone_command_city_name(db, client):
    user = UserService(db).get_or_create(9107, telegram_username="cmd_tz_user")
    user.timezone = "UTC"
    db.commit()

    secret = get_settings().telegram_webhook_secret
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    with patch("app.bot.telegram.TelegramClient.send_message") as mock_send:
        res = client.post(
            "/webhooks/telegram",
            headers=headers,
            json={
                "update_id": 9903,
                "message": {
                    "message_id": 1003,
                    "from": {"id": 9107, "username": "cmd_tz_user"},
                    "chat": {"id": 9107, "type": "private"},
                    "date": 1727800000,
                    "text": "/timezone Самара",
                },
            },
        )
        assert res.status_code == 200
        assert user.timezone == "Europe/Samara"
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        assert "Самара" in sent_text
        assert "Europe/Samara" in sent_text


def test_telegram_timezone_command_unknown(db, client):
    user = UserService(db).get_or_create(9108, telegram_username="cmd_tz_user2")
    secret = get_settings().telegram_webhook_secret
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    with patch("app.bot.telegram.TelegramClient.send_message") as mock_send:
        res = client.post(
            "/webhooks/telegram",
            headers=headers,
            json={
                "update_id": 9904,
                "message": {
                    "message_id": 1004,
                    "from": {"id": 9108, "username": "cmd_tz_user2"},
                    "chat": {"id": 9108, "type": "private"},
                    "date": 1727800000,
                    "text": "/timezone NonExistentCity99",
                },
            },
        )
        assert res.status_code == 200
        mock_send.assert_called_once()
        assert "Неизвестный часовой пояс" in mock_send.call_args[0][1]


def test_telegram_timezone_command_no_args(db, client):
    user = UserService(db).get_or_create(9109, telegram_username="cmd_tz_user3")
    secret = get_settings().telegram_webhook_secret
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    with patch("app.bot.telegram.TelegramClient.send_message") as mock_send:
        res = client.post(
            "/webhooks/telegram",
            headers=headers,
            json={
                "update_id": 9905,
                "message": {
                    "message_id": 1005,
                    "from": {"id": 9109, "username": "cmd_tz_user3"},
                    "chat": {"id": 9109, "type": "private"},
                    "date": 1727800000,
                    "text": "/timezone",
                },
            },
        )
        assert res.status_code == 200
        mock_send.assert_called_once()
        assert "Укажите часовой пояс" in mock_send.call_args[0][1]


def test_callback_location_keyboard_sends_message(db, client):
    """Verify that when action returns a reply keyboard, send_message is called instead of edit_message."""
    from app.services.domain import UIActionService

    user = UserService(db).get_or_create(9110, telegram_username="cb_loc_user")
    token = UIActionService(db).create(user, "setting_request_location", {})
    secret = get_settings().telegram_webhook_secret
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    with patch("app.bot.telegram.TelegramClient.send_message") as mock_send, \
         patch("app.bot.telegram.TelegramClient.edit_message") as mock_edit, \
         patch("app.bot.telegram.TelegramClient.answer_callback") as mock_ans:
        res = client.post(
            "/webhooks/telegram",
            headers=headers,
            json={
                "update_id": 9906,
                "callback_query": {
                    "id": "cb_loc_1",
                    "from": {"id": 9110},
                    "message": {"message_id": 888, "chat": {"id": 9110, "type": "private"}},
                    "data": token,
                },
            },
        )
        assert res.status_code == 200
        assert res.json().get("action") == "setting_request_location"
        # Since markup has "keyboard", send_message MUST be called, and edit_message MUST NOT be called!
        mock_send.assert_called_once()
        mock_edit.assert_not_called()
        mock_ans.assert_called_once_with("cb_loc_1")
