"""Telegram voice transcription through a configured speech-to-text provider."""

import subprocess

import httpx

from app.core.config import get_settings
from app.core.errors import AppError
from app.integrations.telegram.client import TelegramClient
from app.models.entities import Message

TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"


class VoiceTranscriber:
    async def transcribe(self, message: Message) -> str:
        settings = get_settings()
        if settings.stt_provider != "openai":
            raise AppError("STT_NOT_CONFIGURED", "Voice transcription is not configured", 503)
        api_key = settings.stt_api_key or settings.llm_api_key
        if not api_key:
            raise AppError("STT_NOT_CONFIGURED", "STT_API_KEY is required", 503)
        payload = (message.raw_payload or {}).get("message") or (message.raw_payload or {}).get("channel_post") or {}
        voice = payload.get("voice") or {}
        if not voice.get("file_id"):
            raise AppError("VOICE_INVALID", "Telegram voice file is missing", 422)
        if voice.get("file_size", 0) > settings.max_voice_size_bytes or voice.get("duration", 0) > settings.max_voice_duration_seconds:
            raise AppError("VOICE_TOO_LARGE", "Voice message exceeds limits", 413)
        data = TelegramClient().download_file(voice["file_id"], settings.max_voice_size_bytes)
        try:
            converted = subprocess.run(
                [settings.ffmpeg_path, "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                input=data, capture_output=True, timeout=settings.stt_timeout_seconds, check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise AppError("VOICE_CONVERSION_FAILED", "Voice conversion failed", 502) from exc
        if not converted or len(converted) > 25 * 1024 * 1024:
            raise AppError("VOICE_TOO_LARGE", "Converted voice exceeds limits", 413)
        base_url = (settings.stt_base_url or settings.base_url or settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        transcription_url = base_url if base_url.endswith("/audio/transcriptions") else f"{base_url}/audio/transcriptions"
        try:
            async with httpx.AsyncClient(timeout=settings.stt_timeout_seconds) as client:
                response = await client.post(
                    transcription_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": settings.stt_model, "response_format": "json"},
                    files={"file": ("voice.wav", converted, "audio/wav")},
                )
                response.raise_for_status()
                payload_json = response.json()
                if isinstance(payload_json, dict):
                    text = str(payload_json.get("text", "")).strip()
                else:
                    text = str(payload_json).strip()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            try:
                from app.services.metrics import MetricsService
                MetricsService().track_error("stt", "stt_error")
            except Exception:
                pass
            raise AppError("STT_REQUEST_FAILED", "Voice transcription failed", 502) from exc
        if not text:
            raise AppError("STT_EMPTY", "No speech was recognized", 422)
        return text
