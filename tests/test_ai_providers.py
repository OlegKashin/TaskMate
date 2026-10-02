import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.ai.openai_provider import OpenAIProvider
from app.ai.service import AIService
from app.ai.transcription import VoiceTranscriber
from app.core.config import get_settings
from app.core.errors import AppError
from app.models.entities import AIProcessingJob, Message, Source
from app.services.domain import UserService


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeAsyncClient:
    result = None
    last_kwargs = None

    def __init__(self, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def post(self, url, **kwargs):
        self.__class__.last_kwargs = {"url": url, **kwargs}
        return FakeResponse(self.__class__.result)


def test_openai_provider_chat_completions(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("LLM_BASE_URL", "https://shared1.multitool.works:4000/v1")
    get_settings.cache_clear()
    monkeypatch.setattr("app.ai.openai_provider.httpx.AsyncClient", FakeAsyncClient)
    valid = {
        "intent": "create_task", "confidence": 0.95, "entities": {"title": "Купить молоко", "due_at": "2026-10-02T18:00:00+03:00"},
        "action": {"type": "create_task", "requires_confirmation": False},
        "reason": "Task requested", "inbox_classification": None,
    }
    # Test markdown fence stripping
    FakeAsyncClient.result = {
        "choices": [
            {"message": {"role": "assistant", "content": f"```json\n{json.dumps(valid)}\n```"}}
        ]
    }
    result = asyncio.run(OpenAIProvider(timezone="Europe/Moscow").interpret("Купить молоко"))
    assert result.intent == "create_task"
    assert result.entities["title"] == "Купить молоко"
    assert FakeAsyncClient.last_kwargs["url"] == "https://shared1.multitool.works:4000/v1/chat/completions"
    assert FakeAsyncClient.last_kwargs["headers"]["Authorization"] == "Bearer test-key"
    messages = FakeAsyncClient.last_kwargs["json"]["messages"]
    assert any("Europe/Moscow" in m["content"] for m in messages if m["role"] == "system")
    assert FakeAsyncClient.last_kwargs["json"]["model"] == "deepseek-v4-flash"
    assert FakeAsyncClient.last_kwargs["json"]["response_format"] == {"type": "json_object"}
    get_settings.cache_clear()


def test_openai_provider_responses_api(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1/responses")
    get_settings.cache_clear()
    monkeypatch.setattr("app.ai.openai_provider.httpx.AsyncClient", FakeAsyncClient)
    valid = {
        "intent": "create_task", "confidence": 0.9, "entities": {"title": "Report"},
        "action": {"type": "create_task", "requires_confirmation": False},
        "reason": "Task requested", "inbox_classification": None,
    }
    FakeAsyncClient.result = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(valid)}]}],
    }
    result = asyncio.run(OpenAIProvider(timezone="Europe/Moscow").interpret("Create task"))
    assert result.intent == "create_task"
    assert FakeAsyncClient.last_kwargs["url"] == "https://api.openai.com/v1/responses"
    assert FakeAsyncClient.last_kwargs["json"]["store"] is False
    assert FakeAsyncClient.last_kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert "Europe/Moscow" in FakeAsyncClient.last_kwargs["json"]["instructions"]
    get_settings.cache_clear()


def test_openai_provider_error_handling(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")
    get_settings.cache_clear()
    monkeypatch.setattr("app.ai.openai_provider.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.result = {"choices": []}
    with pytest.raises(AppError) as exc:
        asyncio.run(OpenAIProvider().interpret("bad"))
    assert exc.value.code == "LLM_REQUEST_FAILED"

    monkeypatch.setenv("LLM_MODEL", "local-rules")
    get_settings.cache_clear()
    with pytest.raises(AppError) as exc:
        asyncio.run(OpenAIProvider().interpret("test"))
    assert exc.value.code == "LLM_NOT_CONFIGURED"
    get_settings.cache_clear()


def test_voice_transcription_pipeline(db, monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "openai")
    monkeypatch.setenv("STT_API_KEY", "test-key")
    get_settings.cache_clear()
    from app.integrations.telegram.client import TelegramClient

    monkeypatch.setattr(TelegramClient, "download_file", lambda self, file_id, limit: b"ogg")
    monkeypatch.setattr("app.ai.transcription.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=b"wav"))
    monkeypatch.setattr("app.ai.transcription.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.result = {"text": "Prepare a report"}
    user = UserService(db).get_or_create(990)
    source = Source(user_id=user.id, type="telegram_chat", name="Voice", status="active", external_source_id="990")
    db.add(source)
    db.flush()
    message = Message(
        user_id=user.id, source_id=source.id, external_message_id="1", message_type="voice",
        received_at=datetime.now(UTC), raw_payload={"message": {"voice": {"file_id": "abc", "duration": 3}}},
    )
    db.add(message)
    db.flush()
    assert asyncio.run(VoiceTranscriber().transcribe(message)) == "Prepare a report"
    assert FakeAsyncClient.last_kwargs["files"]["file"][0] == "voice.wav"
    assert FakeAsyncClient.last_kwargs["data"]["model"] == "whisper-large-v3"
    assert FakeAsyncClient.last_kwargs["data"]["response_format"] == "json"
    assert FakeAsyncClient.last_kwargs["url"] == "https://shared1.multitool.works:4000/v1/audio/transcriptions"

    class Provider:
        async def interpret(self, text):
            from app.schemas.domain import AIAction, AIResult

            assert text == "Prepare a report"
            return AIResult(intent="general_query", confidence=0.9, action=AIAction(type="none"))

    job = AIProcessingJob(user_id=user.id, message_id=message.id, job_type="stt")
    db.add(job)
    db.commit()
    assert asyncio.run(AIService(db, Provider()).process(job, message, user))["state"] == "informational"
    assert job.status == "completed" and message.text == "Prepare a report"
    get_settings.cache_clear()


def test_voice_rejects_missing_or_oversized_file(db, monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "openai")
    monkeypatch.setenv("STT_API_KEY", "test-key")
    get_settings.cache_clear()
    user = UserService(db).get_or_create(991)
    source = Source(user_id=user.id, type="telegram_chat", name="Voice", status="active", external_source_id="991")
    db.add(source)
    db.flush()
    message = Message(user_id=user.id, source_id=source.id, external_message_id="2", message_type="voice", raw_payload={"message": {"voice": {}}})
    db.add(message)
    db.commit()
    with pytest.raises(AppError) as exc:
        asyncio.run(VoiceTranscriber().transcribe(message))
    assert exc.value.code == "VOICE_INVALID"
    message.raw_payload = {"message": {"voice": {"file_id": "x", "file_size": 999999999}}}
    db.commit()
    with pytest.raises(AppError) as exc:
        asyncio.run(VoiceTranscriber().transcribe(message))
    assert exc.value.code == "VOICE_TOO_LARGE"
    get_settings.cache_clear()


def test_voice_transcriber_errors(db, monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "openai")
    monkeypatch.setenv("STT_API_KEY", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    message = Message(
        user_id=1, source_id=1, external_message_id="3", message_type="voice",
        raw_payload={"message": {"voice": {"file_id": "abc", "duration": 3}}},
    )
    with pytest.raises(AppError) as exc:
        asyncio.run(VoiceTranscriber().transcribe(message))
    assert exc.value.code == "STT_NOT_CONFIGURED"

    monkeypatch.setenv("STT_API_KEY", "test-key")
    get_settings.cache_clear()
    from app.integrations.telegram.client import TelegramClient
    monkeypatch.setattr(TelegramClient, "download_file", lambda self, file_id, limit: b"ogg")
    monkeypatch.setattr("app.ai.transcription.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=b"wav"))
    monkeypatch.setattr("app.ai.transcription.httpx.AsyncClient", FakeAsyncClient)

    # Empty text case
    FakeAsyncClient.result = {"text": "   "}
    with pytest.raises(AppError) as exc:
        asyncio.run(VoiceTranscriber().transcribe(message))
    assert exc.value.code == "STT_EMPTY"

    # Request failure case
    class FailingClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr("app.ai.transcription.httpx.AsyncClient", FailingClient)
    with pytest.raises(AppError) as exc:
        asyncio.run(VoiceTranscriber().transcribe(message))
    assert exc.value.code == "STT_REQUEST_FAILED"
    get_settings.cache_clear()
