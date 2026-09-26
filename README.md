# TaskMate AI

Telegram-first AI assistant built from the MVP technical specification v1.4 and UX specification v2.2. The API is the application layer; Telegram, AI, calendar, email, and storage are adapters around the domain services.

## Local start

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Services:

- API and OpenAPI: `http://localhost:8001/docs`
- Local S3 API (SeaweedFS): `http://localhost:9000`
- PostgreSQL, Redis, Celery worker and Celery scheduler run inside Compose.

The object store is fully local: SeaweedFS creates the `taskmate` bucket and persists files in the `seaweedfs_data` Docker volume. No cloud S3 account is required. The application uses the S3-compatible API on SeaweedFS port `8333` inside Compose.

Get a short-lived REST token with `/api_token` in a private Telegram chat with the bot. REST requests use one header:

```text
Authorization: Bearer <signed-user-token>
```

`X-Telegram-User-Id` is not trusted. Production startup rejects default API/webhook secrets and an empty encryption key or bot token. Change all example secrets before deployment.

## Development without Docker

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

SQLite is the safe default when `.env` is absent. PostgreSQL is used by the supplied Compose configuration.

## Tests and quality

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check app tests
```

Pytest fails when measured application coverage is below 80%.

## Integration modes

- `LLM_PROVIDER=local` uses deterministic rules for offline development. For hosted interpretation set `LLM_PROVIDER=openai`, `LLM_API_KEY`, and a supported `LLM_MODEL`; the response is validated against the domain schema and mutating actions still require confirmation. The adapter uses the [Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create).
- For voice set `STT_PROVIDER=openai` and `STT_API_KEY` (or reuse `LLM_API_KEY`). Set `STT_MODEL` if needed; default is `gpt-transcribe`. Telegram OGG is converted to mono WAV with ffmpeg before the [transcription API](https://developers.openai.com/api/reference/cli/resources/audio/subresources/transcriptions/methods/create). Without STT configuration, voice jobs fail visibly rather than being marked processed.
- Gmail, Yandex, Mail.ru, and Google Calendar OAuth use one-use states linked to a connecting source/calendar record. Gmail/Yandex verify the account through provider APIs; Mail.ru requires `account_email` when creating a state and verifies that mailbox through IMAP XOAUTH2. Credentials are encrypted. In a private bot chat, `/connect` shows provider buttons; Mail.ru then uses `/connect_mailru <email>`. OAuth callback shows an HTML result and sends a Telegram confirmation. Register your own client IDs/secrets and redirect URIs; Gmail needs `gmail.readonly` and `gmail.send`, Yandex needs `mail:imap_full`, `mail:smtp`, and `login:email`, and Mail.ru needs `mail.imap`.
- Google Calendar events begin as `pending`; the periodic worker writes them to Google Calendar and marks them `confirmed`. Failed writes remain pending for a later retry; cancellation deletes confirmed remote events. Live verification requires Google OAuth credentials.
- Gmail API and Yandex/Mail.ru IMAP adapters import only messages newer than the connection/checkpoint time, deduplicate by Message-ID (UIDVALIDITY:UID fallback for IMAP), and use selected folders. `/analyze` and `POST /api/v1/sources/{id}/sync` queue background sync; the scheduler also polls active mailboxes. New mail is queued for AI analysis. Provider failures do not advance the checkpoint.
- Generic IMAP uses an app password. Set `IMAP_HOST` and `SMTP_HOST` to trusted servers, create an `imap` source (initially `connecting`), then call `POST /api/v1/sources/{id}/imap-credentials` with `username` and `password`. Gmail/Yandex/Mail.ru sources cannot be activated directly without OAuth. These hosts are application-wide in this MVP; one installation therefore supports one custom IMAP provider at a time. `INBOX` is selected by default; additional-folder discovery/selection remains an open UX item.
- `POST /api/v1/inbox/{id}/reply` creates a draft/confirmation token. Sending requires a separate `POST /api/v1/inbox/replies/confirm` with JSON body `{ "token": "..." }`. The bot also supports `/reply <message-id> <text>` with Send/Edit/Cancel buttons. No message is sent during draft creation. A failed/uncertain provider response consumes the original token to prevent duplicate delivery; create a fresh draft to retry.
- Real LLM/STT calls require network access, credentials, and a live provider account; tests mock external services and do not verify live provider access.
- Mail and OAuth provider integrations are covered by mocked tests, but live end-to-end sign-in/sync/send requires your provider credentials and is not yet verified against real accounts.

## Important behavior

- Tasks use soft delete and 15-second DB-backed undo tokens.
- Projects use hard delete; task `project_id` becomes `NULL`.
- Sources and calendar connections use soft disconnect and have credentials cleared.
- Telegram updates, source messages, callbacks, notification delivery keys and OAuth state are idempotent.
- An administrator connects a Telegram group by sending `/connect` in that group. The bot must be able to read group messages (disable privacy mode if needed); group data belongs to the connecting administrator's TaskMate account. Private commands are ignored in groups.
- Reminder firing, timezone-aware daily briefings, notification delivery, and pending calendar sync run inside the periodic scheduler tick.
- The initial Alembic migration is frozen; a compatibility revision upgrades older installations with OAuth state links.
- Attachments are stored in local SeaweedFS through its S3 API under `users/{user}/messages/{message}/{attachment}.{ext}`.
