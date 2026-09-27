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
- Gmail, Yandex, Mail.ru, and Google Calendar OAuth use one-use states linked to a connecting source/calendar record. Gmail/Yandex verify the account through provider APIs; Mail.ru requires `account_email` when creating a state and verifies that mailbox through IMAP XOAUTH2. Credentials are encrypted. In a private bot chat, `/connect` shows provider buttons, including Google Calendar; Mail.ru then uses `/connect_mailru <email>`. OAuth callback shows an HTML result and sends a Telegram confirmation. Register your own client IDs/secrets and redirect URIs; Gmail needs `gmail.readonly` and `gmail.send`, Yandex needs `mail:imap_full`, `mail:smtp`, and `login:email`, and Mail.ru needs `mail.imap`.
- Google Calendar events begin as `pending`; the periodic worker writes them to Google Calendar and marks them `confirmed`. Transient failures retry after 30, 120, and 600 seconds (at the next scheduler tick); after the fourth failure, the event stays pending and the bot offers a manual retry. `POST /api/v1/calendar/events/{id}/retry` resets the attempts. Cancellation requires confirmation and deletes confirmed remote events. Live verification requires Google OAuth credentials.
- Gmail API and Yandex/Mail.ru IMAP adapters import only messages newer than the connection/checkpoint time, deduplicate by Message-ID (UIDVALIDITY:UID fallback for IMAP), and use selected folders. Gmail labels are queried separately and combined without duplicate messages. `/analyze` and `POST /api/v1/sources/{id}/sync` queue background sync; the scheduler queues active mailboxes every minute. New mail is queued for AI analysis and actionable intents create Inbox proposals even without a separate classification. Retryable AI/STT/mail jobs use delays of 30 seconds, 2 minutes, and 10 minutes. Authentication failures move a mailbox to `error` and notify its owner.
- Generic IMAP uses an app password. Set `IMAP_HOST` and `SMTP_HOST` to trusted servers, create an `imap` source (initially `connecting`), then call `POST /api/v1/sources/{id}/imap-credentials` with `username` and `password`. Gmail/Yandex/Mail.ru sources cannot be activated directly without OAuth. These hosts are application-wide in this MVP; one installation therefore supports one custom IMAP provider at a time. `INBOX` is selected by default. `POST /api/v1/sources/{id}/folders/discover` retrieves other folders; the bot's source card exposes discovery and selection, and the OAuth success notification links to it. Newly selected folders are read from the current checkpoint, without historical backfill. IMAP credentials still require the authenticated API rather than a Telegram message, to avoid exposing an app password in chat history.
- `POST /api/v1/inbox/{id}/reply` creates a draft/confirmation token. Sending requires a separate `POST /api/v1/inbox/replies/confirm` with JSON body `{ "token": "..." }`. The bot also supports `/reply <message-id> <text>` with Send/Edit/Cancel buttons. No message is sent during draft creation. A failed/uncertain provider response consumes the original token to prevent duplicate delivery, keeps the draft visible, and offers a user-confirmed retry after checking Sent mail.
- Real LLM/STT calls require network access, credentials, and a live provider account; tests mock external services and do not verify live provider access.
- Mail and OAuth provider integrations are covered by mocked tests, but live end-to-end sign-in/sync/send requires your provider credentials and is not yet verified against real accounts.

## Important behavior

- Tasks use soft delete and 15-second DB-backed undo tokens.
- Projects use hard delete; task `project_id` becomes `NULL`.
- Sources and calendar connections use soft disconnect and have credentials cleared.
- Telegram updates, source messages, callbacks, notification enqueue keys and OAuth state are idempotent. Notification delivery locks pending rows in PostgreSQL to prevent concurrent workers from sending the same item. Telegram has no send idempotency key, so a process crash after Telegram accepts a message but before the database commit can still cause a retry; strict exactly-once delivery is not guaranteed.
- An administrator connects a Telegram group by sending `/connect` in that group. The bot must be able to read group messages (disable privacy mode if needed); group data belongs to the connecting administrator's TaskMate account. Private commands are ignored in groups.
- Reminder firing, timezone-aware daily briefings, notification delivery, and pending calendar sync run inside the periodic scheduler tick.
- Inbox, task, project, source, and settings cards in the private bot chat use owner-bound callback actions. `/timezone <IANA name>`, `/briefing_time HH:MM`, and `/stats_time HH:MM` change personal scheduling settings. Low-confidence email requests appear as questions in Inbox; `/clarify <message-id> <text>` queues a new interpretation without changing the original email.
- Manual bot commands: `/task <title>` opens a task draft with due-date, project and priority buttons; `/task_due <token> YYYY-MM-DD HH:MM` sets a draft's local due time. A task card opens the same field editor with Undo; `/task_edit <task-id> <title>` renames a task. `/project <name>` asks for creation confirmation and `/project_rename <project-id> <name>` renames a project. Task completion and project deletion require confirmation buttons. Source cards let owners select linked projects; one linked project is assigned automatically to AI-created tasks, several trigger a choice, and no linked project places the task in Inbox.
- The initial Alembic migration is frozen; later revisions add OAuth state links, calendar retry state, and partial unique indexes for active project names and task-linked calendar events. Run `alembic upgrade head` before starting an updated deployment.
- `GET /api/v1/today` and `/stats` use the user's local calendar day, not a rolling UTC window. The offline rule-based AI can search tasks, ask the user to select an ambiguous task, and interpret relative due days in the user's timezone; when no time is given it uses 18:00 local time.
- Attachments are stored in local SeaweedFS through its S3 API under `users/{user}/messages/{message}/{attachment}.{ext}`.
- Authenticated `GET /api/v1/messages/{id}/attachments` lists a user's saved attachments; `GET /api/v1/attachments/{id}/download` streams a file through the backend without exposing SeaweedFS credentials or its internal endpoint.
- `GET` and `PUT /api/v1/sources/{id}/projects` read or replace a source's active project links; the `PUT` body is `{"project_ids": ["<uuid>"]}` and ownership is checked for both source and projects.
