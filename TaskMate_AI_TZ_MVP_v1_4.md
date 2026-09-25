# TaskMate AI — Техническое задание на разработку MVP

**Версия:** 1.4 — исправлены замечания четвёртого технического ревью: специфицировано отключение Source по аналогии с Calendar (§7, §13.1) вместе с решением проблемы обязательного FK `messages.source_id`, добавлена сущность `reminders` как недостающее хранилище для intent `reminder`, который был в контракте AI без какой-либо таблицы/API (§12.1, §20, §25), добавлен periodic job очистки `oauth_states` по аналогии с `cleanup_ui_actions`, устранена рассинхронизация `ON DELETE` для `waiting_for.message_id`/`ai_processing_jobs.message_id` с уже принятым в v1.2/v1.3 правилом, уточнено поведение переподключения Source после отключения. Полный список изменений — см. §60 «Changelog v1.3 → v1.4». Предыдущие правки — см. §59 «Changelog v1.2 → v1.3» и §58 «Changelog v1.1 → v1.2». Локальная поправка окружения: S3-compatible storage для MVP зафиксирован как SeaweedFS, запускаемый в Docker Compose с локальным persistent volume (§34, §38, §39, §55).

# 1. Назначение документа

Документ описывает технические требования к MVP TaskMate AI — персональному AI-ассистенту, работающему через Telegram.

Базовые бизнес-требования предусматривают Telegram как MVP-платформу, создание профиля при авторизации, работу с источниками Telegram и email, задачами, проектами, Inbox, календарём, голосовыми командами, проактивным анализом сообщений, утренними сводками и вечерней статистикой.

Технологический стек MVP:

- Python 3.12+
- FastAPI
- PostgreSQL
- SQLAlchemy 2.x
- Alembic
- Telegram Bot API
- Pydantic 2.x
- HTTPX
- Redis
- Celery
- SeaweedFS (локальный S3-compatible object storage)
- FFmpeg
- Docker / Docker Compose
- pytest

> Важное архитектурное решение: HTTP API и Telegram webhook должны быть stateless. Долгие операции — AI, STT, синхронизация источников, proactive analysis и scheduled jobs — выполняются асинхронно через очередь.

---

# 2. Scope MVP

## Входит в MVP

1. Telegram bot.
2. Авторизация/создание профиля по Telegram.
3. Главная навигация.
4. Today.
5. Tasks:
   - создание;
   - просмотр;
   - редактирование;
   - удаление;
   - статусы;
   - due date;
   - priority;
   - поиск;
   - project;
   - Inbox.
6. Projects CRUD.
7. Telegram sources:
   - chat;
   - group;
   - channel.
8. Email sources:
   - Gmail OAuth 2.0;
   - Yandex OAuth2 + IMAP;
   - Mail.ru OAuth2 + IMAP;
   - generic IMAP с app password при поддержке.
9. Выбор папок email.
10. Настройки источника:
    - text analysis;
    - voice analysis;
    - сохранение вложений.
11. AI processing входящих сообщений.
12. Speech-to-text для voice.
13. Inbox.
14. Proactive analysis новых сообщений.
15. `/analyze` (полноценно работает с P1, т.к. зависит от подключённых источников; в P0 команда доступна, но при отсутствии источников возвращает информационное сообщение — см. §54).
16. Google Calendar OAuth2.
17. Создание календарных событий.
18. `/today`.
19. `/schedule`.
20. Morning briefing.
21. Evening stats / `/stats`.
22. Inline confirmations.
23. Error handling и retry.
24. Хранение оригинального сообщения и вложений.
25. Logging/audit.
26. Security requirements.
27. Промежуточный UX-статус для долгих AI/STT операций с последующим обновлением bot message.
28. Short callback-token mechanism, исключающий зависимость callback_data от UUID.
29. Undo для поддерживаемых операций с TTL 15 секунд.

## Phase boundary

Для планирования разработки:

```text
MVP = P0 Core + P1 Integrations/Assistant
```

При этом P0 должен быть автономно собираемым и тестируемым без Email integrations.

То есть:

- Email schema support допускается в общей PostgreSQL schema;
- Email runtime adapters отключены в P0;
- Email Celery jobs не регистрируются/не запускаются в P0;
- Email OAuth secrets не требуются для P0;
- P0 release не зависит от доступности Gmail/Yandex/Mail.ru.

## Не входит в MVP

- MAX bot;
- анализ изображений;
- анализ содержимого документов;
- executor / сложные зависимости задач;
- полноценный bidirectional calendar sync;
- smart planning;
- полноценное автоматическое управление временем;
- расширенная аналитика;
- eTime;
- функции, явно обозначенные в исходных требованиях как future extensions.

---

# 3. UX и продуктовая модель

## Главный принцип

Пользователь может сформулировать намерение обычным текстом или голосом.

Общий pipeline:

```text
Input
  ↓
Persist input
  ↓
Immediate processing status (если операция может быть долгой)
  ↓
AI interpretation
  ↓
Action / Proposal / Clarification
  ↓
Confirmation when required
  ↓
Action
  ↓
Result
  ↓
Undo (15 sec where supported)
```

Для операций, которые могут занимать заметное время (STT, LLM, внешние API), бот сначала отправляет короткое промежуточное сообщение, например:

```text
🤔 Анализирую сообщение...
```

или:

```text
🎙 Обрабатываю голосовое...
```

После завершения фоновой задачи это bot message редактируется через `editMessageText`/`editMessageReplyMarkup`. Нельзя рассчитывать на редактирование пользовательского входящего сообщения.

## Главные разделы

- Today
- Tasks
- Projects
- Inbox
- Sources
- Calendar
- Settings

## Глобальные confirmation rules

Без подтверждения:

- просмотр;
- поиск;
- сводки;
- статистика.

С подтверждением:

- создание;
- редактирование;
- удаление;
- изменение статуса;
- создание календарного события.

Дополнительное явное подтверждение:

- массовое удаление;
- массовое изменение;
- отправка email;
- отмена календарного события.

---

# 4. Архитектура системы

## Компоненты

```text
                        Telegram
                           │
                           ▼
                    ┌─────────────┐
                    │  FastAPI    │
                    │ Web/API     │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         PostgreSQL      Redis       SeaweedFS (S3)
              │            │
              │            ▼
              │         Celery
              │            │
              │     ┌──────┼──────────────┐
              │     ▼      ▼              ▼
              │   AI jobs  Sync jobs   Scheduled jobs
              │     │
              │     ▼
              │  LLM / STT
              │
              └──────────────────────────┐
                                         ▼
                               External integrations
                               Gmail / IMAP / Calendar
```

## Backend layers

```text
app/
  api/
  bot/
  core/
  db/
  models/
  schemas/
  repositories/
  services/
  integrations/
  ai/
  workers/
  notifications/
  security/
  tests/
```

Правило: handlers/controllers не должны содержать бизнес-логику.

---

# 5. Рекомендуемая структура проекта

```text
taskmate/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── router.py
│   │   └── v1/
│   ├── bot/
│   │   ├── handlers/
│   │   ├── callbacks/
│   │   ├── keyboards/
│   │   └── middleware/
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   └── security.py
│   ├── db/
│   │   ├── session.py
│   │   └── migrations/
│   ├── models/
│   ├── schemas/
│   ├── repositories/
│   ├── services/
│   ├── integrations/
│   │   ├── telegram/
│   │   ├── gmail/
│   │   ├── imap/
│   │   └── google_calendar/
│   ├── ai/
│   │   ├── router.py
│   │   ├── prompts/
│   │   ├── schemas.py
│   │   └── providers/
│   ├── workers/
│   │   ├── celery_app.py
│   │   └── tasks/
│   └── notifications/
├── tests/
├── alembic.ini
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

---

# 6. Domain model

Основные сущности:

- User
- Source
- SourceProject
- Message
- Attachment
- Project
- Task
- TaskEvent / AuditEvent
- CalendarConnection
- CalendarEvent
- InboxItem
- WaitingFor
- Reminder *(v1.4 — недостающее хранилище для intent `reminder`, см. §12.1)*
- AIProcessingJob
- Notification
- UserSettings
- OAuthState *(v1.3 — связывает браузерный OAuth callback с Telegram-пользователем, см. §13.2)*

> **v1.2:** предыдущая версия списка называла отдельную сущность `OAuthCredential`, которой в физической схеме не существует. Учётные данные хранятся в двух разных местах: `source_credentials` (для Sources — Telegram/email) и напрямую в полях `encrypted_access_token`/`encrypted_refresh_token` таблицы `calendar_connections` (для Calendar). Единой универсальной OAuth-таблицы в MVP нет — это сознательное упрощение, а не пропуск.

---

# 7. PostgreSQL schema

## users

```sql
users
-----
id UUID PK
telegram_user_id BIGINT UNIQUE NOT NULL
telegram_username VARCHAR NULL
first_name VARCHAR NULL
last_name VARCHAR NULL
timezone VARCHAR NOT NULL DEFAULT 'UTC'
locale VARCHAR NOT NULL DEFAULT 'ru'
is_active BOOLEAN NOT NULL DEFAULT TRUE
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Индексы:

- unique(telegram_user_id)

---

## user_settings

```sql
user_settings
-------------
user_id UUID PK FK users.id
morning_briefing_enabled BOOLEAN NOT NULL DEFAULT TRUE
morning_briefing_time TIME NOT NULL DEFAULT '08:00'
evening_stats_enabled BOOLEAN NOT NULL DEFAULT TRUE
evening_stats_time TIME NOT NULL DEFAULT '20:00'
weather_enabled BOOLEAN NOT NULL DEFAULT FALSE
default_project_id UUID NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

---

## projects

```sql
projects
--------
id UUID PK
user_id UUID NOT NULL FK users.id
name VARCHAR(255) NOT NULL
description TEXT NULL
is_archived BOOLEAN NOT NULL DEFAULT FALSE
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Уникальный индекс среди активных (неархивных) проектов:

```sql
CREATE UNIQUE INDEX ux_projects_user_name_active
    ON projects (user_id, name)
    WHERE is_archived = FALSE;
```

Ограничение действует только для неархивных проектов — это позволяет архивировать проект и создать новый с тем же именем, но не даёт AI/пользователю запутаться при выборе среди двух одновременно активных проектов с одинаковым названием.

---

## sources

```sql
sources
-------
id UUID PK
user_id UUID NOT NULL FK users.id
type VARCHAR NOT NULL
name VARCHAR(255) NOT NULL
status VARCHAR NOT NULL
external_source_id VARCHAR NULL
analysis_text BOOLEAN NOT NULL DEFAULT TRUE
analysis_voice BOOLEAN NOT NULL DEFAULT TRUE
save_attachments BOOLEAN NOT NULL DEFAULT TRUE
connected_at TIMESTAMPTZ NULL
last_analyzed_at TIMESTAMPTZ NULL
last_synced_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

`connected_at` фиксируется в момент перехода source в статус `active` (успешное подключение). При создании source `last_analyzed_at` и `last_synced_at` инициализируются значением `connected_at`.

> **v1.2:** `analysis_voice` сохранён в схеме для единообразия таблицы, но имеет практический эффект только для источников типа `telegram_*`. Для email-источников (`gmail`, `yandex`, `mailru`, `imap`) поле присутствует, но не используется в MVP, так как voice-вложения в письмах не анализируются.

`external_source_id` хранит внешний идентификатор источника:

- Telegram chat/group/channel ID — в строковом виде, чтобы поддерживать в том числе отрицательные значения;
- внешний ID email-аккаунта/подключения, если его предоставляет провайдер.

`type`:

- telegram_chat
- telegram_group
- telegram_channel
- gmail
- yandex
- mailru
- imap

`status`:

- connecting
- active
- paused
- error
- disconnected

Уникальный индекс (частичный, т.к. `external_source_id` может быть `NULL` во время подключения):

```sql
CREATE UNIQUE INDEX ux_sources_user_type_external
    ON sources (user_id, type, external_source_id)
    WHERE external_source_id IS NOT NULL;
```

Это предотвращает повторное подключение одного и того же чата/канала/ящика одним и тем же пользователем.

Для email-папок используется отдельная таблица `source_folders`.

### Границы первичной синхронизации (v1.2)

При подключении источника **MVP не выполняет backfill исторических сообщений**. Proactive analysis и email-sync обрабатывают только сообщения, полученные после `connected_at`:

```text
process only WHERE received_at > source.connected_at
```

Это ограничение обязательно, так как без него подключение почтового ящика или канала с большой историей привело бы к неконтролируемому объёму AI-обработки, стоимости и мгновенному переполнению Inbox старыми элементами. Полноценный импорт истории (если потребуется) — отдельная P1+/пост-MVP фича, требующая отдельного UX (явный запрос пользователя, ограничение объёма, прогресс-бар).

### Отключение Source (v1.4)

В предыдущих версиях `DELETE /api/v1/sources/{id}` присутствовал в §25 как MVP-эндпоинт, а его точное поведение при этом было явно вынесено в §55 (п. 7 «Поведение удаления source») как решение, **требующее подтверждения до production** — то есть для самого MVP оно фактически не было определено. Это не техническая деталь: `messages.source_id` — обязательный (`NOT NULL`) FK на `sources.id` без `ON DELETE`, то есть физический `DELETE` строки `sources` у источника с уже полученными сообщениями завершится ошибкой ссылочной целостности. Это тот же класс проблемы, который был найден и исправлен для Calendar в v1.3 (см. §13.1) — только там `calendar_events.connection_id` играл роль `messages.source_id`.

Решение — тот же принцип, что и для Calendar: «удаление» источника не физическое, а перевод в терминальный статус (`sources.status` уже содержал значение `disconnected` в перечне статусов, см. выше, — им никто не пользовался до этой правки).

**Эндпоинт:**

```http
DELETE /api/v1/sources/{id}
```

Поведение:

1. проверяется ownership (`source.user_id == current_user.id`);
2. `sources.status` переводится в `disconnected`;
3. `source_credentials` для этого source (если есть) — `encrypted_access_token`/`encrypted_refresh_token`/`encrypted_username`/`encrypted_password` затираются (`NULL`), по той же причине, что и для Calendar (§13.1, п. 3): хранить более недействительные для пользователя секреты незачем;
4. уже полученные `messages` и созданные из них `tasks`/`inbox_items` **не удаляются** — это исторические записи, ссылочная целостность не нарушается, так как `sources` не удаляется физически;
5. Telegram-источник перестаёт быть целью для входящей обработки, email-источник перестаёт синхронизироваться — оба пайплайна (§41) обязаны проверять `sources.status = 'active'` перед постановкой сообщения в AI-очередь; `disconnected`/`paused`/`error` источники не порождают новые `messages`/AI-jobs;
6. операция необратима через Undo — аналогично удалению project (§51) и отключению Calendar (§13.1).

**Переподключение после отключения:** уникальный индекс `ux_sources_user_type_external` (см. выше) действует по `(user_id, type, external_source_id)` без условия на `status`, поэтому у отключённого source сохраняется его `external_source_id`. Если пользователь повторно подключает тот же самый внешний чат/канал/ящик, backend обязан **переиспользовать существующую строку** `sources` (перевести её обратно `disconnected → connecting → active` через тот же flow, что и первичное подключение, см. §13.2 для email/OAuth-источников), а не создавать новую — иначе вставка новой строки с тем же `(user_id, type, external_source_id)` будет отклонена уникальным индексом. Это то же решение, что уже принято для `calendar_connections` (см. §13.1, п. 5).

---

## source_projects

Many-to-many:

```sql
source_projects
---------------
source_id UUID FK sources.id
project_id UUID FK projects.id
PRIMARY KEY(source_id, project_id)
```

Если source связан с одним project — AI автоматически использует его.

Если с несколькими — пользователь выбирает project.

---

## source_folders

Для email-источников папки/labels должны храниться отдельно от самого source.

```sql
source_folders
--------------
id UUID PK
source_id UUID NOT NULL FK sources.id
external_folder_id VARCHAR NOT NULL
name VARCHAR(255) NOT NULL
is_selected BOOLEAN NOT NULL DEFAULT TRUE
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Уникальность:

```text
(source_id, external_folder_id)
```

Source без email-папок не использует эту таблицу.

---

## source_credentials

Секреты нельзя хранить в открытом виде.

```sql
source_credentials
------------------
id UUID PK
source_id UUID UNIQUE FK sources.id
provider VARCHAR NOT NULL
encrypted_access_token TEXT NULL
encrypted_refresh_token TEXT NULL
encrypted_username TEXT NULL
encrypted_password TEXT NULL
token_expires_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Шифрование выполняется application-level encryption.

---

## messages

```sql
messages
--------
id UUID PK
user_id UUID NOT NULL FK users.id
source_id UUID NOT NULL FK sources.id
external_message_id VARCHAR NOT NULL
external_thread_id VARCHAR NULL
sender_external_id VARCHAR NULL
sender_name VARCHAR NULL
sender_email VARCHAR NULL
subject TEXT NULL
text TEXT NULL
message_type VARCHAR NOT NULL
received_at TIMESTAMPTZ NOT NULL
raw_payload JSONB NULL
processing_status VARCHAR NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

`message_type`:

- text
- voice
- photo
- document
- email
- other

Типы `photo`/`document`/`other` сохраняются как записи `messages` + `attachments`, но их содержимое не передаётся в AI (см. §2 «Не входит в MVP» — анализ изображений/документов); в Inbox такие сообщения могут появляться только по метаданным (например, "получено фото без подписи").

`processing_status`:

- received
- queued
- processing
- processed
- failed
- ignored — сообщение сознательно не обрабатывалось (например, `analysis_text=false` для источника, либо AI-intent = `ignore`)

Уникальность:

```text
(source_id, external_message_id)
```

Это обязательный механизм idempotency.

### Определение `external_message_id` (v1.2)

- Telegram: `message_id` апдейта в строковом виде.
- Gmail/Yandex/Mail.ru (IMAP-протокол): заголовок `Message-ID` из RFC 5322. Именно `Message-ID`, а не IMAP UID — UID не гарантированно стабилен между сессиями (сбрасывается при изменении `UIDVALIDITY` папки), что привело бы к дублям или потере сообщений.
- Если `Message-ID` отсутствует или пуст (редкий случай): используется составной идентификатор `"{UIDVALIDITY}:{UID}"` для конкретной папки как запасной вариант.

---

# 8. Attachments

```sql
attachments
-----------
id UUID PK
message_id UUID NOT NULL FK messages.id
filename VARCHAR NOT NULL
mime_type VARCHAR NULL
size_bytes BIGINT NULL
storage_key VARCHAR NOT NULL
checksum VARCHAR NULL
created_at TIMESTAMPTZ NOT NULL
```

MVP хранит вложения, но не анализирует содержимое изображений/документов.

---

# 9. Tasks

```sql
tasks
-----
id UUID PK
user_id UUID NOT NULL FK users.id
project_id UUID NULL FK projects.id ON DELETE SET NULL
source_message_id UUID NULL FK messages.id ON DELETE SET NULL
title VARCHAR(500) NOT NULL
description TEXT NULL
status VARCHAR NOT NULL
priority VARCHAR NOT NULL DEFAULT 'normal'
due_at TIMESTAMPTZ NULL
completed_at TIMESTAMPTZ NULL
cancelled_at TIMESTAMPTZ NULL
deleted_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

### Удаление задачи — soft-delete (v1.2)

«Удаление» задачи из Tasks/Inbox/через AI **не является физическим `DELETE`**, а устанавливает `deleted_at = now()`. Причины:

1. позволяет тривиально и надёжно реализовать Undo (см. §29) — восстановление это `UPDATE tasks SET deleted_at = NULL`, без пересборки строки из JSON;
2. не ломает ссылочную целостность `task_events.task_id`, `inbox_items.task_id`, `calendar_events.task_id` (см. правила `ON DELETE` ниже).

Правила выборок: все стандартные запросы (Tasks list, Today, поиск, `/today`, `/schedule`) по умолчанию фильтруют `deleted_at IS NULL`. `DELETE /api/v1/tasks/{id}` в REST API выполняет именно soft-delete.

Физическое (hard) удаление данных выполняется только в рамках отдельного retention/purge-процесса (см. §55, открытый вопрос «Retention policy») и не связано с пользовательским Undo.

> **v1.3:** `source_message_id` получил явное `ON DELETE SET NULL` (по аналогии с `inbox_items.message_id`, см. §11) — при retention-purge исходного сообщения задача не должна становиться недоступной из-за оборванного обязательного FK; ссылка на источник просто обнуляется, сама задача не затрагивается.

Статусы:

- new
- in_progress
- completed
- cancelled

Priority:

- low
- normal
- high

Если `project_id IS NULL`, задача находится в Inbox / общем пространстве задач.

При удалении project используется:

```sql
FOREIGN KEY (project_id)
REFERENCES projects(id)
ON DELETE SET NULL
```

Это означает, что задачи не удаляются и автоматически остаются без project, то есть попадают в Inbox.

---

# 10. Task history

```sql
task_events
-----------
id UUID PK
task_id UUID NOT NULL FK tasks.id ON DELETE CASCADE
user_id UUID NOT NULL FK users.id
event_type VARCHAR NOT NULL
old_value JSONB NULL
new_value JSONB NULL
source VARCHAR NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

`source` (v1.3):

- `user_command` — прямая команда через Telegram-текст/кнопку без участия AI-интерпретации (например, нажатие `[Выполнено]`)
- `ai` — действие выполнено по AI-предложению после подтверждения пользователем
- `system` — фоновый job (`schedule_tick`, retention purge и т.п.)
- `undo` — операция отмены предыдущего действия

Используется для:

- audit;
- debugging;
- undo;
- отображения истории.

> **v1.2:** Пока задача жива (в т.ч. soft-deleted, `deleted_at IS NOT NULL`), `task_events` не удаляются. `ON DELETE CASCADE` срабатывает только при физическом hard-purge задачи в рамках retention-процесса (§55) — тогда вместе с задачей уходит и её история, что соответствует цели retention (право на удаление данных).

---

# 11. Inbox

```sql
inbox_items
-----------
id UUID PK
user_id UUID NOT NULL FK users.id
message_id UUID NULL FK messages.id ON DELETE SET NULL
task_id UUID NULL FK tasks.id ON DELETE SET NULL
item_type VARCHAR NOT NULL
status VARCHAR NOT NULL
title VARCHAR(500) NULL
summary TEXT NULL
priority VARCHAR NOT NULL DEFAULT 'normal'
snoozed_until TIMESTAMPTZ NULL
resolved_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

> **v1.3:** `message_id` получил явное `ON DELETE SET NULL` — по аналогии с остальными nullable FK, приведёнными к единому правилу в v1.2 (см. §10).

Типы:

- task_candidate
- reply_required
- waiting_for
- opportunity
- urgent
- question
- request
- approval
- meeting
- promise
- risk
- financial
- legal
- personal

Статусы:

- new
- proposed
- snoozed
- resolved
- ignored

### Откуда берётся item_type (v1.3)

В предыдущей версии не было объяснено, как 14 значений `item_type` соотносятся с 17 `intent` из AIResult (§20) — `urgent`, `financial`, `legal`, `risk`, `opportunity`, `approval`, `promise`, `personal` не совпадали ни с одним intent. Причина в том, что это **две разные задачи классификации**, выполняемые AI, и в AIResult для второй не хватало поля. Правило после исправления (см. подробности схемы в §20 «Inbox-классификация»):

| item_type | Источник значения |
|---|---|
| `task_candidate` | `intent = create_task` с `requires_confirmation = true`, пока пользователь не подтвердил |
| `reply_required` | `intent = reply_email` |
| `waiting_for` | `intent = create_waiting_for` (см. §20) |
| `meeting` | `intent = create_event`, когда событие предложено, но ещё не подтверждено |
| `urgent`, `opportunity`, `question`, `request`, `approval`, `promise`, `risk`, `financial`, `legal`, `personal` | напрямую из `AIResult.inbox_classification.item_type` — отдельного поля классификации содержания, не являющегося командой (см. §20) |

Один и тот же message может одновременно породить task/event-предложение (через `intent`) и быть помечен как `urgent`/`financial`/... (через `inbox_classification`) — это две независимые, не взаимоисключающие метки одного и того же входящего сообщения.

---

# 12. WaitingFor

```sql
waiting_for
-----------
id UUID PK
user_id UUID NOT NULL FK users.id
message_id UUID NULL FK messages.id ON DELETE SET NULL
title VARCHAR(500) NOT NULL
description TEXT NULL
expected_from VARCHAR NULL
due_at TIMESTAMPTZ NULL
status VARCHAR NOT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

> **v1.4:** `message_id` получил явное `ON DELETE SET NULL` — в v1.2/v1.3 это правило было явно установлено для `tasks.source_message_id` и `inbox_items.message_id` (см. §9, §11) как реакция на будущий retention-purge сообщений (§55), но пропущено для структурно идентичного поля здесь.

Статусы:

- active
- completed
- cancelled

---

# 12.1. Reminders (v1.4)

Intent `reminder` присутствует среди 17 (теперь 18) допустимых значений `AIResult.intent` с первой версии ТЗ (см. §20) — вместе со схемой `entities` (`title`, `due_at`, `related_task_id`). При этом ни в domain model (§6), ни в схеме БД, ни в REST API (§25) для него не было ни таблицы, ни эндпоинта: `notifications.type` уже содержит значение `task_reminder` (см. §16), но `notifications` не имеет поля для хранения *будущего* момента срабатывания — только `sent_at`, факт уже состоявшейся отправки. Как специфицировано до этой правки, intent `reminder` был неисполним: AI мог вернуть `intent = reminder`, но backend'у было некуда и как его сохранить.

```sql
reminders
---------
id UUID PK
user_id UUID NOT NULL FK users.id
task_id UUID NULL FK tasks.id ON DELETE SET NULL
related_message_id UUID NULL FK messages.id ON DELETE SET NULL
title VARCHAR(500) NOT NULL
due_at TIMESTAMPTZ NOT NULL
status VARCHAR NOT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Статусы:

- pending
- fired
- cancelled

`task_id` заполняется, только если reminder явно связан с задачей (`related_task_id` в `entities`, см. §20); reminder не обязан быть привязан к задаче — «напомни мне позвонить маме завтра в 18:00» валиден без `task_id`.

## Срабатывание

В отличие от `morning_briefing_time`/`evening_stats_time` (§28), `due_at` в `reminders` — это уже абсолютный момент времени в UTC (интерпретированный из локального времени пользователя в момент создания, см. §32), поэтому для срабатывания не требуется отдельный пересчёт по timezone на каждом тике — достаточно прямого сравнения `due_at <= now()`.

`schedule_tick` (§28), уже выполняющийся периодически, на каждом тике дополнительно:

1. выбирает `reminders WHERE status = 'pending' AND due_at <= now()`;
2. для каждой создаёт `notifications` с `type = 'task_reminder'`, `body`, сформированным из `title` (и, если есть, контекста задачи);
3. переводит `reminders.status` в `fired`.

Отдельный Celery job не заводится — механизм переиспользует уже существующий periodic tick, что не требует новой инфраструктуры планирования.

## REST API

См. §25 «Reminders».

---

# 13. Calendar connections

```sql
calendar_connections
--------------------
id UUID PK
user_id UUID NOT NULL FK users.id
provider VARCHAR NOT NULL
calendar_external_id VARCHAR NULL
status VARCHAR NOT NULL
encrypted_access_token TEXT
encrypted_refresh_token TEXT
token_expires_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

MVP provider:

- google

`status` (v1.3):

- `connecting` — создана черновая запись, ожидает завершения OAuth (см. §13.1);
- `active` — подключение работает, токены валидны;
- `error` — refresh token недействителен или обмен токенами завершился ошибкой, требуется переподключение;
- `disconnected` — пользователь явно отключил календарь (см. §13.1).

Правило OAuth token refresh:

1. перед запросом к Google Calendar проверить `token_expires_at`;
2. если access token истёк или истечёт в ближайшее время — получить новый через refresh token;
3. сохранить новый encrypted access token и новый `token_expires_at`;
4. повторить исходный API request;
5. если refresh token недействителен — перевести connection в `error` и уведомить пользователя о необходимости переподключения.

`encrypted_refresh_token` не используется как access token и не передаётся клиенту.

---

# 13.1. Отключение Calendar (v1.3)

В v1.2 у Sources был `DELETE /api/v1/sources/{id}`, а у Calendar аналогичного способа отключения не было — при этом `calendar_events.connection_id` является обязательным (`NOT NULL`) FK, поэтому строку `calendar_connections` в принципе нельзя физически удалить, не нарушив существующие события.

Решение — тот же принцип, что и у Sources: «удаление» подключения не физическое, а перевод в терминальный статус.

## Эндпоинт

```http
DELETE /api/v1/calendar/connections/{id}
```

Поведение:

1. проверяется ownership (`connection.user_id == current_user.id`);
2. `calendar_connections.status` переводится в `disconnected`;
3. `encrypted_access_token`/`encrypted_refresh_token` затираются (перезаписываются `NULL`) — хранить более недействительные для пользователя секреты незачем;
4. существующие `calendar_events`, ссылающиеся на это подключение, **не удаляются и не отменяются** — это исторические записи; при этом POST на создание/отмену нового события через отключённое подключение возвращает `CONFLICT` с понятным сообщением («Календарь отключён, переподключите его в /sources»);
5. операция необратима через Undo (аналогично удалению project, см. §51) — повторное подключение проходит заново через OAuth-flow (§13.2) и создаёт новую логическую привязку токенов (физически может быть та же строка, переведённая обратно в `connecting` → `active`, либо новая — на усмотрение реализации).

## Листинг

```http
GET /api/v1/calendar/connections
```

Возвращает подключения пользователя (обычно одно — MVP provider только `google`), включая `status`, без расшифрованных токенов.

---

# 13.2. OAuth flow (v1.3)

Это самое критичное исправление данной ревизии. В v1.2 для 4 провайдеров (Google Calendar, Gmail, Yandex, Mail.ru) были env-переменные `client_id`/`secret` (§38), логика token refresh (§13), `OAuthService` в списке сервисов (§27) — но не было ни одного эндпоинта инициации авторизации, ни callback-эндпоинта, и не было объяснено, как HTTP-callback (приходит в браузер, без Telegram-контекста) связывается с конкретным Telegram-пользователем.

## Проблема связывания контекста

`GET /oauth/{provider}/callback` — это обычный браузерный редирект. У него нет `chat_id`/Telegram `user_id`. Без отдельного связывающего токена невозможно узнать, какому пользователю принадлежит пришедший `code`.

## Таблица oauth_states

```sql
oauth_states
------------
id UUID PK
user_id UUID NOT NULL FK users.id
provider VARCHAR NOT NULL                        -- google | gmail | yandex | mailru
purpose VARCHAR NOT NULL                         -- calendar | source
source_id UUID NULL FK sources.id ON DELETE CASCADE
calendar_connection_id UUID NULL FK calendar_connections.id ON DELETE CASCADE
state_token VARCHAR(64) UNIQUE NOT NULL          -- криптографически случайный (>= 32 байта, base64url)
expires_at TIMESTAMPTZ NOT NULL
consumed_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
```

Ограничение: ровно одно из `source_id`/`calendar_connection_id` заполнено, в зависимости от `purpose` (`source` → `source_id`, `calendar` → `calendar_connection_id`).

Уникальность: `unique(state_token)`. `expires_at = created_at + OAUTH_STATE_TTL_SECONDS` (см. §38).

## Очистка oauth_states (v1.4)

`oauth_states` имеет ту же форму, что и `ui_actions` (§17): накопительная таблица с `expires_at`/`consumed_at` и без TTL на уровне БД. Для `ui_actions` это было явно распознано как проблема неограниченного роста и закрыто периодическим `cleanup_ui_actions` (v1.2), но аналогичный job для `oauth_states` заведён не был, хотя каждая попытка подключения источника/календаря (в том числе неуспешная или брошенная на середине) добавляет строку. Периодический background job (см. §28, `cleanup_oauth_states`) удаляет строки, у которых `expires_at < now()` или `consumed_at` установлен более `UI_ACTIONS_CLEANUP_RETENTION_HOURS` часов назад (переиспользуется существующая переменная — семантика идентична: сколько хранить уже потреблённые/просроченные одноразовые токены).

## Публичные эндпоинты (вне Telegram webhook)

```http
GET /api/v1/oauth/{provider}/authorize?state=<state_token>
GET /api/v1/oauth/{provider}/callback?code=...&state=<state_token>
```

`{provider}` ∈ `google` (Calendar), `gmail`, `yandex`, `mailru` (Sources).

Это единственные два HTTP-роута backend'а, рассчитанные на прямой заход из браузера пользователя, а не из Telegram webhook.

## Пошаговый flow

### 1. Инициация (внутри Telegram)

1. Пользователь в боте выбирает «Подключить Gmail» / «Подключить Google Calendar».
2. Backend создаёт черновую запись в статусе `connecting`:
   - для источника — строку `sources` (`status='connecting'`);
   - для календаря — строку `calendar_connections` (`status='connecting'`).
3. Backend создаёт `oauth_states` со свежим `state_token` и `expires_at = now() + OAUTH_STATE_TTL_SECONDS`.
4. Бот отправляет пользователю кнопку-ссылку:
   ```text
   {PUBLIC_BASE_URL}/api/v1/oauth/{provider}/authorize?state=<state_token>
   ```

### 2. GET /api/v1/oauth/{provider}/authorize

1. Находит `oauth_states` по `state_token`; если запись не найдена, `consumed_at IS NOT NULL` или `expires_at <= now()` — отдаёт HTML-страницу с ошибкой и предложением начать подключение заново в боте.
2. Строит authorization URL провайдера: `client_id` (из §38), `redirect_uri = {PUBLIC_BASE_URL}/api/v1/oauth/{provider}/callback`, нужные `scope`, и **тот же `state_token`** передаётся провайдеру как его собственный OAuth `state` — это одновременно и CSRF-защита, и механизм связывания callback с Telegram-пользователем.
3. `302 Redirect` на провайдера.

### 3. GET /api/v1/oauth/{provider}/callback

1. Провайдер редиректит сюда с `code` и `state`.
2. Backend находит `oauth_states` по `state = state_token`; если не найден/истёк/уже потреблён — страница с ошибкой, без побочных эффектов.
3. Атомарно помечает находку как потреблённую тем же паттерном, что и в §17 (защита от повторного использования одного `code`, например при повторном заходе по ссылке или ретрае браузера):
   ```sql
   UPDATE oauth_states
   SET consumed_at = now()
   WHERE id = :id
     AND consumed_at IS NULL
     AND expires_at > now()
   RETURNING *;
   ```
4. Обменивает `code` на `access_token`/`refresh_token` через token endpoint провайдера.
5. По `purpose`:
   - `source`: шифрует и сохраняет токены в `source_credentials` (по `source_id` из `oauth_states`), переводит `sources.status` в `active`, устанавливает `sources.connected_at = now()` (см. §7 — именно с этого момента начинается proactive analysis, backfill не выполняется);
   - `calendar`: шифрует и сохраняет токены в `calendar_connections` (по `calendar_connection_id`), переводит `calendar_connections.status` в `active`.
6. Backend отправляет пользователю сообщение в Telegram **напрямую через Bot API** (`user_id` → `users.telegram_user_id` уже известны из `oauth_states.user_id`, обращение к Telegram API не требует входящего webhook) — например, «✅ Gmail подключён».
7. Браузеру отдаётся статическая HTML-страница («Готово, вернитесь в Telegram») — без бизнес-логики на фронте.
8. Если обмен `code` на токены завершился ошибкой — соответствующая запись (`sources`/`calendar_connections`) переводится в `error`, пользователю в Telegram отправляется сообщение об ошибке с предложением подключить заново.

## Environment variables (дополнение к §38)

```text
PUBLIC_BASE_URL=
OAUTH_STATE_TTL_SECONDS=600
```

`PUBLIC_BASE_URL` — публичный HTTPS-адрес backend'а, используется для построения `redirect_uri` и ссылки, которую бот отправляет пользователю. Обязателен к заполнению в любом окружении, где включены Calendar/email источники (P1).

## Acceptance criteria (дополнение к §48/§49)

- Инициация OAuth создаёт ровно одну запись `oauth_states` с уникальным `state_token`, привязанным к конкретному `user_id` и к конкретной черновой `source`/`calendar_connections` записи.
- `GET /callback` без валидного и ещё не потреблённого `state` не создаёт и не изменяет `source_credentials`/`calendar_connections`.
- Один и тот же `code` не может быть успешно обменян на токены дважды (атомарная пометка `consumed_at` предотвращает повторное использование при двойном заходе по ссылке).
- После успешного `callback` пользователь получает подтверждение в Telegram, инициированное backend'ом (а не только через веб-страницу браузера).
- Ошибка на любом шаге flow переводит `sources.status`/`calendar_connections.status` в `error`, а не оставляет запись бессрочно в `connecting`.

---

# 14. Calendar events

```sql
calendar_events
---------------
id UUID PK
user_id UUID NOT NULL FK users.id
connection_id UUID NOT NULL FK calendar_connections.id ON DELETE RESTRICT
external_event_id VARCHAR NULL
task_id UUID NULL FK tasks.id ON DELETE SET NULL
title VARCHAR(500) NOT NULL
description TEXT NULL
start_at TIMESTAMPTZ NOT NULL
end_at TIMESTAMPTZ NOT NULL
status VARCHAR NOT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

`status` (v1.2):

- `pending` — создано локально, ещё не подтверждено внешним calendar API;
- `confirmed` — событие существует в Google Calendar (`external_event_id` заполнен);
- `cancelled` — событие отменено (см. cancel-эндпоинт в §25 и acceptance criteria в §48).

### Unique-индексы против дублей (v1.3)

Acceptance criteria (§48, п. 5) требует, что повторное создание события предотвращается, но в v1.2 для `calendar_events` не было ни одного unique-индекса (в отличие, например, от `messages`). Добавлены:

```sql
-- один и тот же внешний event не может быть привязан к подключению дважды
CREATE UNIQUE INDEX ux_calendar_events_connection_external
    ON calendar_events (connection_id, external_event_id)
    WHERE external_event_id IS NOT NULL;

-- у задачи может быть не более одного НЕотменённого календарного события
CREATE UNIQUE INDEX ux_calendar_events_task_active
    ON calendar_events (task_id)
    WHERE task_id IS NOT NULL AND status <> 'cancelled';
```

Второй индекс формализует правило из «Task → Calendar suggestion» ниже: пока у задачи есть активное (не `cancelled`) событие, кнопка `[Добавить в календарь]` не предлагается повторно. Если событие отменено, новое для той же задачи создать можно — партиальный индекс это не блокирует.

`connection_id` получил `ON DELETE RESTRICT`: физическое удаление строки `calendar_connections` не предусмотрено в MVP (см. §13.1 «Отключение Calendar»), поэтому этот FK — защита от случайного нарушения ссылочной целостности, а не рабочий сценарий.

### Task → Calendar suggestion (P1, v1.2)

Если у задачи заполнен `due_at` с конкретным временем (не просто датой) и с ней ещё не связан `calendar_event`, бот после создания/редактирования задачи может предложить кнопку `[Добавить в календарь]`. Подтверждение обязательно (см. §3). После подтверждения создаётся `calendar_events` с `task_id`, указывающим на задачу. Функция входит в P1 — Calendar (см. §54) и использует общий OAuth/refresh-flow, описанный в §13.

---

# 15. AI processing jobs

```sql
ai_processing_jobs
------------------
id UUID PK
user_id UUID NOT NULL FK users.id
message_id UUID NULL FK messages.id ON DELETE SET NULL
job_type VARCHAR NOT NULL
status VARCHAR NOT NULL
provider VARCHAR NULL
model VARCHAR NULL
input_hash VARCHAR NULL
result JSONB NULL
error_code VARCHAR NULL
attempts INTEGER NOT NULL DEFAULT 0
status_message_id BIGINT NULL
started_at TIMESTAMPTZ NULL
completed_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
```

`job_type` (v1.3):

- `interpret_message` — основная интерпретация входящего сообщения (`AIResult`, §20)
- `transcribe_voice` — STT-этап перед интерпретацией
- `classify_inbox` — заполнение `inbox_classification` в рамках proactive analysis (§28)
- `analyze_source` — пакетный запуск proactive analysis по источнику

> **v1.4:** `message_id` получил явное `ON DELETE SET NULL` — по той же причине и по той же логике, что и для `waiting_for.message_id` (см. §12): retention-purge исходного сообщения (§55) не должен ломать уже завершённую историю AI-job'ов.

---

# 16. Notifications

```sql
notifications
-------------
id UUID PK
user_id UUID NOT NULL FK users.id
type VARCHAR NOT NULL
title VARCHAR NULL
body TEXT NOT NULL
payload JSONB NULL
status VARCHAR NOT NULL
sent_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
```

`type` (v1.3):

- `morning_briefing`
- `evening_stats`
- `urgent_item` — создаётся при `inbox_classification.item_type = 'urgent'` (см. §20)
- `task_reminder`
- `source_error` — источник перешёл в `status = 'error'`
- `calendar_error` — calendar connection перешла в `status = 'error'`
- `system` — служебные уведомления, не привязанные к конкретной бизнес-сущности

`status` (v1.2):

- pending
- sent
- failed

---

# 17. Callback actions / short UI tokens

UUID остаются основными идентификаторами доменных сущностей. Для Telegram inline callbacks нельзя использовать длинные UUID непосредственно в `callback_data`.

Для MVP используется DB-backed short action token:

```sql
ui_actions
----------
id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
user_id UUID NOT NULL FK users.id
action VARCHAR(64) NOT NULL
entity_type VARCHAR(64) NULL
entity_id UUID NULL
payload JSONB NULL
expires_at TIMESTAMPTZ NULL
consumed_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
```

Telegram callback payload:

```text
a:<base36(id)>
```

Пример:

```text
a:k9
```

Цель — держать `callback_data` существенно меньше лимита Telegram и не передавать UUID/контекст напрямую.

Техническое ограничение: Telegram `callback_data` ограничено 64 байтами. Поэтому разработчик не должен строить callback payload из полного UUID доменной сущности.

Правила:

1. `id` — короткий numeric ID;
2. callback handler загружает `ui_actions` по ID;
3. проверяется `user_id`;
4. атомарно помечается `consumed_at` (см. ниже) — только после успешной атомарной пометки action считается захваченным и выполняется;
5. выполняется action;
6. если атомарная пометка не удалась — action уже выполнен/просрочен, обработка прекращается без побочных эффектов и без повторного `answerCallbackQuery` с ошибкой.

### Атомарность consumed_at (v1.2)

Двойной тап по кнопке или ретрай доставки callback от Telegram не должны приводить к двойному выполнению действия. Пометка `consumed_at` и проверка `expires_at` выполняются одним атомарным SQL-запросом, а не последовательностью SELECT + UPDATE:

```sql
UPDATE ui_actions
SET consumed_at = now()
WHERE id = :id
  AND consumed_at IS NULL
  AND (expires_at IS NULL OR expires_at > now())
RETURNING *;
```

Если запрос не вернул строку — значит, action уже был потреблён параллельным запросом либо истёк; обработчик просто отвечает пользователю (`answerCallbackQuery`) без выполнения действия повторно.

Redis используется для Celery и transient jobs, но callback state не зависит от process memory.

### Очистка ui_actions (v1.2)

`ui_actions` — накопительная таблица без TTL на уровне БД. Периодический background job (см. §28, `cleanup_ui_actions`) удаляет строки, у которых `expires_at < now()` или `consumed_at` установлен более `UI_ACTIONS_CLEANUP_RETENTION_HOURS` часов назад, чтобы избежать неограниченного роста таблицы.

---

## telegram_updates

Идемпотентность на уровне Telegram-апдейтов в целом (а не только `messages`) — например, для `callback_query`, `edited_message` и прочих типов, для которых нет своей уникальной таблицы:

```sql
telegram_updates
-----------------
update_id BIGINT PRIMARY KEY
update_type VARCHAR NOT NULL
received_at TIMESTAMPTZ NOT NULL
```

Webhook перед обработкой пытается вставить `update_id` (`INSERT ... ON CONFLICT DO NOTHING`); если строка уже существует — апдейт уже обрабатывался, повторная обработка не выполняется. Это отдельный, более общий механизм, чем уникальность `(source_id, external_message_id)` в `messages`, которая защищает от дублей на уровне бизнес-сообщений.

---

# 18. AI pipeline

## Общая схема

```text
Telegram / Email
      ↓
Receive
      ↓
Persist raw message
      ↓
Create AI job
      ↓
Queue
      ↓
Normalize input
      ↓
STT if voice
      ↓
LLM interpretation
      ↓
Validate structured output
      ↓
Confidence evaluation
      ↓
Action policy
      ↓
Create proposal / execute safe action
      ↓
Notify user
```

## AI provider abstraction

Backend не должен быть жёстко связан с одним LLM.

Интерфейс:

```python
class LLMProvider(Protocol):
    async def interpret(
        self,
        context: AIContext,
    ) -> AIResult:
        ...
```

STT:

```python
class STTProvider(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        mime_type: str,
    ) -> str:
        ...
```

> **v1.2:** Таймаут не является частью сигнатуры Protocol — он применяется на уровне `AIService`/`STTService`, оборачивающих вызов провайдера (`asyncio.wait_for` или аналог), с настраиваемым порогом через `AI_TIMEOUT_SECONDS` / `STT_TIMEOUT_SECONDS` (см. §38). Превышение таймаута переводит `ai_processing_jobs.status` в retryable-ошибку согласно retry policy (§30).

---

# 19. Audio / STT processing

Telegram voice message должен пройти нормализацию перед STT.

Pipeline:

```text
Telegram OGG/Opus
      ↓
Download
      ↓
Validate mime/size/duration
      ↓
FFmpeg conversion
      ↓
STT provider format
      ↓
Transcription
      ↓
LLM interpretation
```

Требования:

1. worker проверяет расширение/mime type;
2. worker ограничивает размер и длительность файла конфигурацией;
3. входной OGG/Opus при необходимости конвертируется FFmpeg;
4. временный файл удаляется после завершения job;
5. оригинальное вложение, если `save_attachments=true`, хранится отдельно в локальном SeaweedFS через S3-compatible API;
6. FFmpeg failure переводит AI job в retryable/non-retryable error в зависимости от причины;
7. путь к FFmpeg задаётся через configuration.

Минимальные environment variables:

```text
FFMPEG_PATH=/usr/bin/ffmpeg
MAX_VOICE_SIZE_BYTES=
MAX_VOICE_DURATION_SECONDS=
```

---

# 20. AIResult contract

LLM должен возвращать строго валидируемый JSON.

```json
{
  "intent": "create_task",
  "confidence": 0.94,
  "entities": {
    "title": "Отправить договор клиенту",
    "description": null,
    "due_at": "2026-09-26T12:00:00+03:00",
    "priority": "normal",
    "project_hint": null
  },
  "action": {
    "type": "create_task",
    "requires_confirmation": true
  },
  "reason": "Пользователь явно сформулировал действие"
}
```

Допустимые intents:

- general_query
- create_task
- edit_task
- delete_task
- search_task
- change_task_status
- create_event
- search_event
- project_action
- source_action
- analyze
- show_today
- show_schedule
- show_stats
- reminder
- reply_email
- create_waiting_for
- ignore

LLM не должен непосредственно изменять PostgreSQL.

AI только возвращает структурированное намерение.

## Entity schemas по intent (v1.2)

Пример в начале раздела описывает `entities` только для `create_task`. Ниже — обязательные поля `entities` для остальных intents. Все поля, кроме отмеченных как обязательные, могут быть `null`, если не извлекаются из сообщения.

| intent | entities (обязательные поля) | Примечание |
|---|---|---|
| `create_task` | `title`*, `description`, `due_at`, `priority`, `project_hint` | как в примере выше |
| `edit_task` | `target`* (см. ниже), `changed_fields` — объект с любым подмножеством `{title, description, due_at, priority, project_hint}` | `changed_fields` содержит только реально изменяемые поля; см. «Перенос между проектами» ниже |
| `delete_task` | `target`* | — |
| `change_task_status` | `target`*, `new_status`* | `new_status` — одно из значений `tasks.status` |
| `search_task` | `query`, `filters` — `{status, project_hint, due_from, due_to}` | все поля опциональны |
| `create_event` | `title`*, `start_at`*, `end_at`*, `description`, `source_task_id` | `source_task_id` — если событие создаётся из задачи |
| `search_event` | `date_from`, `date_to`, `query` | — |
| `project_action` | `action`* (`create`\|`rename`\|`archive`\|`delete`), `project_name`, `project_id`, `new_name` (для `rename`) | AI не имеет права сам выполнять `delete` — только предлагать (см. §22) |
| `source_action` | `action`* (`pause`\|`resume`\|`disconnect`\|`change_settings`), `source_id`*, `settings` | — |
| `analyze` | `source_id` (опционально — при отсутствии анализируются все активные источники пользователя) | — |
| `show_today` / `show_schedule` / `show_stats` | `{}` (пустой объект) | действие не требует подтверждения (§3) |
| `reminder` | `title`*, `due_at`*, `related_task_id` | создаёт запись `reminders` (v1.4, см. §12.1) — до этой правки intent существовал без хранилища |
| `reply_email` | `message_id`*, `draft_text`* | всегда `requires_confirmation: true`; см. «Ограничение: только email» ниже (v1.3) |
| `create_waiting_for` | `title`*, `expected_from`, `due_at`, `related_message_id` | v1.3 — см. «WaitingFor через AI» ниже |
| `general_query` | `{}` | ответ идёт текстом в `reason`/отдельном текстовом поле, действие не выполняется |
| `ignore` | `reason` | используется для логирования, почему сообщение не породило действие |

### Перенос между проектами через AI (v1.3)

`changed_fields.project_hint` закрывает пробел, из-за которого «перенос задачи между проектами/Inbox» числился в обязательном списке Undo-операций (§29), но не имел канала вызова через AI. Семантика — как у `changed_fields` в целом (значение имеет смысл только если ключ присутствует):

- ключ `project_hint` отсутствует в `changed_fields` → project задачи не меняется;
- ключ присутствует со строковым значением → backend резолвит его в конкретный `project_id` по тем же правилам, что и `create_task` (§42): один кандидат — назначается автоматически, несколько — пользователю показывается выбор, ни одного — предлагается создать project;
- ключ присутствует со значением `null` → однозначная команда «убрать из проекта» (перенести в Inbox), `project_id` задачи становится `NULL`.

Как и остальные `edit_task`, операция требует подтверждения (§3) и поддерживает Undo (§29): исходное значение `project_id` фиксируется в `task_events.old_value` до изменения.

### WaitingFor через AI (v1.3)

Таблица `waiting_for` (§12) и тип `waiting_for` в Inbox (§11) существовали с первой версии ТЗ, но не было intent, которым AI мог бы создать такую запись из свободного текста («Жду ответ от Игоря по договору до среды»). `create_waiting_for` закрывает этот канал создания. Изменение статуса (`completed`/`cancelled`) в MVP выполняется только через REST API/кнопки в Inbox (§25), а не через повторный свободный текст — это сознательное ограничение объёма, аналогичное тому, как `Notifications` не имеют своего AI-intent.

### Ограничение: reply_email только для email (v1.3)

`reply_email` — единственный intent для исходящего ответа, и это намеренное, а не случайное ограничение: Telegram Bot API технически не позволяет отправить сообщение в чужой чат/канал от имени пользователя так, как SMTP/Gmail API отправляет письмо от имени владельца OAuth-токена. Поэтому:

- кнопка «Ответить» в Inbox активна только для `inbox_items`, у которых связанное `messages.message_type = 'email'`;
- для `inbox_items`, связанных с Telegram-сообщениями, кнопки исходящей отправки нет вообще — пользователь отвечает вручную в самом Telegram;
- если такой запрос всё же приходит через API (`POST /api/v1/inbox/{id}/reply`, см. §25) для non-email item — возвращается `CONFLICT` с кодом `REPLY_NOT_SUPPORTED_FOR_SOURCE`.

### Inbox-классификация: inbox_classification (v1.3)

Помимо `intent` (командного намерения), AIResult может содержать независимое поле для случаев, когда сообщение не является явной командой, но заслуживает отображения в Inbox с определённой категорией — то есть закрывает пробел между 18 intents и 14 `inbox_items.item_type` (см. §11 «Откуда берётся item_type»):

```json
{
  "intent": "ignore",
  "confidence": 0.81,
  "entities": { "reason": "Информационное сообщение, не требующее действия" },
  "action": { "type": "ignore", "requires_confirmation": false },
  "reason": "...",
  "inbox_classification": {
    "item_type": "financial",
    "priority": "high"
  }
}
```

Правила:

- `inbox_classification` — необязательное поле, отдельное от `intent`/`action`; заполняется преимущественно в рамках proactive analysis (§28), но может присутствовать и при обработке команды (например, письмо одновременно порождает задачу через `intent = create_task` и помечено как `urgent` через `inbox_classification`);
- `inbox_classification.item_type` ∈ `{urgent, opportunity, question, request, approval, promise, risk, financial, legal, personal}` — то есть только тем значениям `item_type`, которые не выводятся напрямую из `intent` (см. таблицу в §11);
- если `inbox_classification.item_type = 'urgent'`, backend создаёт запись `notifications` с `type = 'urgent_item'` сразу при создании `inbox_items` (закрывает acceptance criteria §44 п. 19 «Urgent item создаёт notification»);
- при отсутствии `inbox_classification` в ответе AI Inbox-запись, если она создаётся, получает `item_type`, выведенный из `intent`/`action` по таблице в §11.

`target`* — способ адресации задачи для `edit_task`/`delete_task`/`change_task_status`, один из двух вариантов:

```json
{ "task_id": "<uuid>" }
```
```json
{ "search_hint": { "title_contains": "договор", "recency": "last_mentioned" } }
```

Если `target` — `search_hint` и найдено несколько кандидатов, backend (не AI) инициирует уточняющий выбор у пользователя (см. acceptance criteria §45 «Search» — «bot displays candidates; user can choose one; no silent arbitrary selection»).

---

# 21. Confidence policy

Предлагаемая реализация:

```text
>= 0.85
    high confidence

0.60–0.849
    medium confidence

< 0.60
    low confidence
```

Пороговые значения должны быть конфигурируемыми.

Важно: confidence не является единственным критерием. Для опасных действий confirmation обязателен независимо от confidence.

### Что именно меняется по уровням (v1.2)

Так как create/edit/delete/change_status и создание calendar event и без того всегда требуют подтверждения (§3), confidence влияет не на «нужно ли подтверждение», а на **форму предложения**:

- **high (>= 0.85):** proposal показывается сразу с уже заполненными извлечёнными полями и одной кнопкой подтверждения (`[Подтвердить] [Отменить]`);
- **medium (0.60–0.849):** proposal показывается, но с явным акцентом на возможность править поля перед подтверждением (кнопка `[Изменить] [Подтвердить] [Отменить]`); в `reason` AI обязан указать, что именно вызывает неуверенность;
- **low (< 0.60):** предложение действия **не создаётся** вообще — вместо `ui_actions`-предложения бот задаёт пользователю уточняющий вопрос (свободный текст или набор кнопок с вариантами интерпретации), и только после ответа пользователя запускается повторная интерпретация.

---

# 22. Action engine

После AI interpretation backend выполняет policy check:

```text
AIResult
  ↓
ActionPolicy
  ↓
Is action allowed?
  ↓
Does confirmation required?
  ↓
Create proposal OR execute
```

AI не имеет права:

- самостоятельно отправлять email;
- самостоятельно удалять массовые данные;
- самостоятельно удалять project;
- самостоятельно отменять calendar event.

---

# 23. Telegram architecture

Telegram webhook:

```text
POST /webhooks/telegram
```

Webhook:

1. валидирует Telegram secret;
2. извлекает `update_id`;
3. проверяет idempotency через `telegram_updates` (`INSERT ... ON CONFLICT DO NOTHING`, см. §17) — при конфликте апдейт уже обработан, webhook сразу возвращает `200 OK`;
4. определяет user;
5. сохраняет входящие данные;
6. при необходимости создаёт/отправляет промежуточное bot message;
7. ставит job в Celery;
8. немедленно возвращает `200 OK`.

Webhook **никогда не должен ждать** LLM, STT или внешние API.

## Intermediate processing status

Для text input:

```text
🤔 Анализирую сообщение...
```

Для voice:

```text
🎙 Обрабатываю голосовое...
```

После завершения Celery job:

- успешный результат → редактируется status message;
- ошибка → редактируется status message на понятное сообщение об ошибке;
- если нужно показать несколько действий — message получает новую inline keyboard.

В `ai_processing_jobs.status_message_id` сохраняется Telegram message ID промежуточного bot message.

Важно: `editMessageText` применяется к сообщению, отправленному ботом, а не к пользовательскому сообщению.

---

# 24. Callback architecture

В callback payload нельзя передавать полные UUID и сложный JSON.

Использовать таблицу `ui_actions`, описанную выше.

Формат:

```text
a:<base36(ui_actions.id)>
```

Примеры:

```text
a:1
a:2
a:k9
```

Callback handler:

1. парсит короткий token;
2. загружает `ui_actions`;
3. проверяет user ownership;
4. проверяет `expires_at`;
5. проверяет, не выполнено ли одноразовое действие;
6. выполняет action;
7. обновляет bot message / keyboard;
8. создаёт result/undo action при необходимости.

Таким образом UUID доменных сущностей не зависят от лимита `callback_data`.

---

Нельзя доверять ID из callback без проверки принадлежности пользователю.

---

# 25. REST API

API version:

```text
/api/v1
```

## Users

```http
GET /api/v1/me
PATCH /api/v1/me
```

## Tasks

```http
GET    /api/v1/tasks
POST   /api/v1/tasks
GET    /api/v1/tasks/{id}
PATCH  /api/v1/tasks/{id}
DELETE /api/v1/tasks/{id}

POST /api/v1/tasks/{id}/complete
POST /api/v1/tasks/{id}/cancel
POST /api/v1/tasks/{id}/undo
```

`DELETE /api/v1/tasks/{id}` выполняет soft-delete (`deleted_at = now()`, см. §9), а не физическое удаление строки.

Query parameters:

```text
status
project_id
priority
due_from
due_to
search
limit   (default 20, max 100)
offset  (default 0)
```

## Projects

```http
GET    /api/v1/projects
POST   /api/v1/projects
GET    /api/v1/projects/{id}
PATCH  /api/v1/projects/{id}
DELETE /api/v1/projects/{id}
```

## Sources

```http
GET    /api/v1/sources
POST   /api/v1/sources
GET    /api/v1/sources/{id}
PATCH  /api/v1/sources/{id}
DELETE /api/v1/sources/{id}
GET    /api/v1/sources/{id}/folders
PATCH  /api/v1/sources/{id}/folders
```

`DELETE /api/v1/sources/{id}` (v1.4) — выполняет soft-disconnect (`status = 'disconnected'`, затирание `source_credentials`), а не физическое удаление строки; подробное поведение см. §7 «Отключение Source». `POST`-эндпоинта для повторного подключения нет — переподключение того же source проходит через тот же flow, что и первичное подключение (Telegram: повторная привязка чата; email/Calendar: OAuth-flow, §13.2).

### Payload PATCH /api/v1/sources/{id}/folders (v1.3)

```json
{
  "folders": [
    { "external_folder_id": "INBOX", "is_selected": true },
    { "external_folder_id": "Promotions", "is_selected": false }
  ]
}
```

Семантика:

- частичное обновление: перечисленные `external_folder_id` обновляют `source_folders.is_selected`; папки source, не упомянутые в payload, сохраняют текущее значение;
- `external_folder_id`, отсутствующий в `source_folders` для данного source (то есть не возвращавшийся ранее в `GET .../folders`), — `VALIDATION_ERROR`;
- ответ — актуальный список папок в том же формате, что и `GET /api/v1/sources/{id}/folders`.

## Inbox

```http
GET  /api/v1/inbox
GET  /api/v1/inbox/{id}
POST /api/v1/inbox/{id}/resolve
POST /api/v1/inbox/{id}/ignore
POST /api/v1/inbox/{id}/snooze
POST /api/v1/inbox/{id}/reply
```

`POST /api/v1/inbox/{id}/reply` (v1.3) — доступен только когда связанное сообщение имеет `messages.message_type = 'email'` (см. §20 «Ограничение: reply_email только для email»); тело запроса — `{ "draft_text": "..." }`; отправка требует явного подтверждения (§3, «отправка email»). Для inbox item, связанного с Telegram-сообщением, эндпоинт возвращает `409 CONFLICT` (`REPLY_NOT_SUPPORTED_FOR_SOURCE`).

## Calendar

```http
GET  /api/v1/calendar/events
POST /api/v1/calendar/events
GET  /api/v1/calendar/events/{id}
PATCH /api/v1/calendar/events/{id}
POST /api/v1/calendar/events/{id}/cancel

GET    /api/v1/calendar/connections
DELETE /api/v1/calendar/connections/{id}
```

`POST .../cancel` требует дополнительного явного подтверждения (см. §3), переводит `status` события в `cancelled` (см. §14) и, если событие уже существует во внешнем календаре (`external_event_id` заполнен), отменяет его через Google Calendar API.

`DELETE /api/v1/calendar/connections/{id}` (v1.3) — отключение календаря, подробное поведение см. §13.1. Подключение нового calendar-соединения инициируется не через `POST` на этот ресурс, а через OAuth-flow (§13.2): `POST`-эндпоинта для прямого создания `calendar_connections` в MVP нет намеренно — токены не могут появиться иначе, чем через обмен `code` в OAuth callback.

## OAuth (v1.3)

```http
GET /api/v1/oauth/{provider}/authorize
GET /api/v1/oauth/{provider}/callback
```

Единственные публичные (не webhook, не API-авторизованные) HTTP-роуты backend'а. Подробности — §13.2.

## Waiting For (v1.3)

```http
GET    /api/v1/waiting-for
POST   /api/v1/waiting-for
GET    /api/v1/waiting-for/{id}
PATCH  /api/v1/waiting-for/{id}
POST   /api/v1/waiting-for/{id}/complete
POST   /api/v1/waiting-for/{id}/cancel
```

Ранее (v1.2) таблица `waiting_for` существовала в domain model (§6) и в схеме (§12), но не имела ни одного REST-эндпоинта. `POST /api/v1/waiting-for` используется как REST-путём создания (например, из будущего веб-интерфейса), так и внутренним вызовом `AIActionService` при обработке intent `create_waiting_for` (§20).

## Reminders (v1.4)

```http
GET    /api/v1/reminders
POST   /api/v1/reminders
GET    /api/v1/reminders/{id}
PATCH  /api/v1/reminders/{id}
POST   /api/v1/reminders/{id}/cancel
```

Ранее intent `reminder` существовал в AIResult (§20) без какой-либо таблицы и REST-эндпоинта. `POST /api/v1/reminders` используется как обычный REST-путь создания, так и внутренним вызовом `AIActionService`/`NotificationService` при обработке intent `reminder`. Срабатывание (перевод в `fired` и отправка `notifications`) выполняется фоново через `schedule_tick` (см. §12.1, §28), а не через REST — отдельного `POST .../fire` нет.

## Notifications (v1.3)

```http
GET /api/v1/notifications
GET /api/v1/notifications/{id}
```

Только чтение: основной канал доставки уведомлений — сообщения бота в Telegram (см. §16, `notifications.status` описывает именно статус доставки в Telegram, а не факт прочтения пользователем). REST-эндпоинты нужны для истории/аудита, а не для управления уведомлениями; отдельного "mark as read" в MVP нет, так как в схеме `notifications` нет поля прочтения — это сознательное ограничение объёма.

## Analysis

```http
POST /api/v1/analyze
GET  /api/v1/analyze/{job_id}
```

## Statistics

```http
GET /api/v1/today
GET /api/v1/stats
```

---

# 26. API response standard

Успешный response:

```json
{
  "data": {},
  "meta": {}
}
```

### Формат meta для списковых эндпоинтов (v1.3)

Для любого `GET`-эндпоинта, принимающего `limit`/`offset` (Tasks, Projects, Sources, Inbox, Calendar events, Waiting For, Notifications — см. §25), `data` — массив, а `meta` обязан содержать:

```json
{
  "data": [ /* ... */ ],
  "meta": {
    "pagination": {
      "limit": 20,
      "offset": 0,
      "total": 134
    }
  }
}
```

`total` — общее количество записей, удовлетворяющих фильтрам, без учёта `limit`/`offset`. Для не-списковых (single-object) ответов `meta` остаётся `{}`, если нет иной специфичной для эндпоинта метаинформации.

Ошибка:

```json
{
  "error": {
    "code": "TASK_NOT_FOUND",
    "message": "Task not found",
    "request_id": "..."
  }
}
```

Каждый request получает `request_id`.

---

# 27. Service layer

Минимальный набор сервисов:

```text
UserService
TaskService
ProjectService
SourceService
MessageService
InboxService
CalendarService
NotificationService
AIService
AIActionService
ProactiveAnalysisService
BriefingService
StatsService
AttachmentService
OAuthService
WaitingForService
```

> **v1.3:** `OAuthService` отвечает не только за token refresh (§13), но и за весь flow из §13.1/§13.2: создание `oauth_states`, построение authorize URL, обмен `code` на токены в callback-эндпоинте, запись результата в `source_credentials`/`calendar_connections`. `WaitingForService` — новый сервис для CRUD над `waiting_for` (см. §12, §20, §25).

> **v1.4:** CRUD над `reminders` (§12.1, §25) не требует отдельного сервиса — это часть `TaskService`/`AIActionService` для создания и `NotificationService` для срабатывания внутри `schedule_tick` (§28). Отдельный `ReminderService` сознательно не заводится, чтобы не плодить сервисы под таблицы из одного-двух методов.

---

# 28. Background jobs

Celery workers.

## Incoming message processing

```text
process_message(message_id)
```

Шаги:

1. lock message;
2. проверить duplicate;
3. получить content;
4. STT при необходимости;
5. вызвать LLM;
6. validate result;
7. execute/propose action;
8. сохранить AI job;
9. отправить notification.

---

## Proactive analysis

```text
analyze_source(source_id)
```

Выбирает сообщения:

```text
received_at > source.last_analyzed_at
```

После успешного завершения обновляет `last_analyzed_at`.

---

## Механизм планирования брифингов (v1.2)

`morning_briefing_time`/`evening_stats_time` индивидуальны для каждого пользователя и заданы в его локальной timezone (`users.timezone`). Статическое расписание Celery beat не покрывает произвольные пары «время + часовой пояс» на пользователя, поэтому используется periodic tick:

```text
schedule_tick()  — Celery beat, каждые SCHEDULER_TICK_INTERVAL_SECONDS (default 60)
```

`schedule_tick`:

1. вычисляет текущее локальное время для каждого активного пользователя (`user.timezone`);
2. выбирает пользователей, у которых текущее локальное время попадает в окно тика и совпадает с `morning_briefing_time` (при `morning_briefing_enabled = true`) или `evening_stats_time` (при `evening_stats_enabled = true`);
3. ставит в очередь `send_morning_briefing(user_id)` / `send_evening_stats(user_id)` для каждого найденного пользователя.

При изменении пользователем `timezone` или времени брифинга пересчёт происходит автоматически на следующем тике — отдельная логика реордеринга не требуется.

---

## Morning briefing

```text
send_morning_briefing(user_id)
```

Собирает:

- calendar;
- tasks today;
- overdue;
- high priority;
- optional weather.

---

## Evening stats

```text
send_evening_stats(user_id)
```

Собирает:

- completed;
- created;
- activity;
- tomorrow tasks.

---

## Email sync

```text
sync_email_source(source_id)
```

Требования:

- incremental sync, начиная от `last_synced_at` (изначально равен `connected_at`, см. §7 — backfill истории не выполняется);
- deduplication;
- retry;
- обновление `last_synced_at`.

---

## Telegram source processing

Если архитектура источника позволяет получать сообщения через webhook/update:

```text
process_telegram_source_message(...)
```

---

## Очистка ui_actions (v1.2)

```text
cleanup_ui_actions()  — periodic, каждый час
```

Удаляет строки `ui_actions`, где `expires_at < now()`, а также строки с `consumed_at`, установленным более `UI_ACTIONS_CLEANUP_RETENTION_HOURS` часов назад (см. §17, §38). Предотвращает неограниченный рост таблицы.

---

## Очистка oauth_states (v1.4)

```text
cleanup_oauth_states()  — periodic, каждый час
```

Удаляет строки `oauth_states`, где `expires_at < now()`, а также строки с `consumed_at`, установленным более `UI_ACTIONS_CLEANUP_RETENTION_HOURS` часов назад (см. §13.2). Тот же паттерн и та же причина, что и у `cleanup_ui_actions` выше.

---

## Срабатывание reminders (v1.4)

Дополнительный шаг внутри уже существующего `schedule_tick()` (см. «Механизм планирования брифингов» выше): на каждом тике также выбираются `reminders WHERE status = 'pending' AND due_at <= now()`, для каждой создаётся `notifications` (`type = 'task_reminder'`) и `reminders.status` переводится в `fired`. Подробности — §12.1. Отдельный periodic job не заводится.

---

# 29. Undo

Undo входит в MVP для действий, для которых обратная операция безопасна и однозначна.

TTL кнопки Undo:

```text
15 секунд
```

Механика:

1. действие выполнено;
2. создан `task_event` с `old_value` и `new_value`;
3. создаётся временный `ui_actions` с action=`undo`;
4. bot message показывает `[Отменить]`;
5. по нажатию выполняется обратная операция;
6. после успешного Undo callback помечается `consumed_at`;
7. после `expires_at` Undo больше недоступен.

Если операция необратима или её состояние нельзя корректно восстановить, Undo не показывается.

> **v1.2 — Undo удаления задачи:** так как удаление задачи реализовано как soft-delete (`tasks.deleted_at`, см. §9), обратная операция — это `UPDATE tasks SET deleted_at = NULL`, а не восстановление строки из `task_events.old_value`. Это гарантирует, что все связи (`inbox_items.task_id`, `calendar_events.task_id`) остаются нетронутыми на всё время, пока Undo потенциально доступен, — их не нужно отдельно "чинить" после восстановления.

Для MVP Undo обязательно для:

- создания задачи;
- удаления задачи;
- изменения статуса задачи;
- изменения основных полей задачи;
- перемещения задачи между проектами / Inbox.

> **v1.2:** формулировка «если состояние полностью известно» из предыдущей версии убрана как неоперациональная. Перемещение между проектами/Inbox — это изменение одного поля `project_id`, состояние которого всегда полностью известно (текущее значение фиксируется в `task_events.old_value` до изменения), поэтому Undo для этой операции поддерживается безусловно.

TTL измеряется от времени успешного завершения операции.

---

# 30. Retry policy

Для внешних API:

```text
attempt 1 → immediate
attempt 2 → +30 sec
attempt 3 → +2 min
attempt 4 → +10 min
```

После превышения лимита:

```text
FAILED
```

Пользователь получает уведомление для пользовательских операций.

Если у job существует `status_message_id`, финальный retryable/non-retryable result должен обновить это сообщение, а не создавать бесконечную серию новых сообщений.

Для фоновых задач ошибка логируется и job помечается failed.

---

# 31. Idempotency

Обязательна на уровнях:

1. Telegram `update_id`.
2. Source + external message ID.
3. AI processing job.
4. Calendar external event ID.
5. Callback action.
6. OAuth `state`/`code` (v1.3) — атомарная пометка `oauth_states.consumed_at` исключает повторный обмен одного `code` на токены (см. §13.2).

Один внешний message не должен приводить к двум одинаковым задачам.

---

# 32. Timezone

Каждый User имеет timezone.

Все даты в PostgreSQL:

```text
TIMESTAMPTZ
```

Интерпретация:

> завтра

происходит относительно timezone пользователя.

Morning/evening jobs также выполняются в timezone пользователя, через периодический `schedule_tick` (см. §28), а не через статическое per-user Celery beat расписание.

UTC используется для хранения абсолютного времени.

> **v1.2:** при смене пользователем `timezone` отдельного пересчёта/миграции не требуется — `schedule_tick` на каждом тике заново вычисляет локальное время из текущего значения `users.timezone`, поэтому новое расписание брифингов применяется автоматически со следующего тика.

---

# 33. Security

## Secrets

Нельзя хранить:

- access token;
- refresh token;
- IMAP password;
- API keys

в plaintext.

## Encryption

Использовать application-level encryption для OAuth credentials.

Encryption key:

- только environment/secret manager;
- не хранить в PostgreSQL.

## Ownership

Каждый объект должен проверять:

```text
object.user_id == current_user.id
```

## Telegram webhook

Использовать Telegram webhook secret token.

## Logs

Не логировать:

- OAuth tokens;
- passwords;
- полные email bodies;
- sensitive credentials.

---

# 34. File storage

Файлы хранить в локальном SeaweedFS через его S3-compatible API. SeaweedFS входит в Docker Compose MVP и использует отдельный persistent volume; облачный S3-аккаунт не требуется.

DB хранит только metadata:

```text
storage_key
filename
mime_type
size
checksum
```

Object key:

```text
users/{user_id}/messages/{message_id}/{attachment_id}.{ext}
```

`{ext}` выводится из `filename`/`mime_type` при сохранении — упрощает раздачу через signed URL с корректным именем файла на стороне клиента.

Доступ к объектам только через backend / signed URLs.

Backend работает с SeaweedFS исключительно через S3-протокол и path-style addressing. Внутренний endpoint Compose — `http://seaweedfs:8333`; рекомендуемый host mapping для локальной разработки — `http://localhost:9000`. Bucket `taskmate` создаётся при старте и не должен быть публичным.

---

# 35. Error model

Категории:

```text
VALIDATION_ERROR
AUTH_ERROR
NOT_FOUND
CONFLICT
EXTERNAL_SERVICE_ERROR
AI_ERROR
STT_ERROR
INTEGRATION_ERROR
RATE_LIMIT
INTERNAL_ERROR
```

Пользователь должен получать понятное сообщение.

Технические детали — только в logs.

---

# 36. Observability

Обязательно:

- structured logs;
- request_id;
- job_id;
- user_id;
- source_id;
- message_id;
- AI processing duration;
- external API duration;
- exception stack trace.

Метрики MVP:

- incoming messages;
- processed messages;
- AI failures;
- average AI latency;
- task creation rate;
- duplicate rate;
- external integration failures;
- background job failures.

---

# 37. Database migrations

Использовать Alembic.

Правила:

- никакого ручного изменения production schema;
- каждая schema change — migration;
- migration должна быть backward-compatible при возможности;
- destructive migration отдельно проверяется.

---

# 38. Configuration

`.env.example`:

```text
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8000
PUBLIC_BASE_URL=

DATABASE_URL=
REDIS_URL=

TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=

LLM_PROVIDER=
LLM_API_KEY=
LLM_MODEL=

STT_PROVIDER=
STT_API_KEY=

S3_ENDPOINT=http://seaweedfs:8333
S3_BUCKET=taskmate
S3_ACCESS_KEY=taskmate
S3_SECRET_KEY=taskmate-local-secret
S3_REGION=us-east-1

GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

GMAIL_CLIENT_ID=
GMAIL_CLIENT_SECRET=

YANDEX_CLIENT_ID=
YANDEX_CLIENT_SECRET=

MAILRU_CLIENT_ID=
MAILRU_CLIENT_SECRET=

OAUTH_STATE_TTL_SECONDS=600

ENCRYPTION_KEY=

FFMPEG_PATH=/usr/bin/ffmpeg
MAX_VOICE_SIZE_BYTES=
MAX_VOICE_DURATION_SECONDS=

AI_TIMEOUT_SECONDS=30
STT_TIMEOUT_SECONDS=30
SCHEDULER_TICK_INTERVAL_SECONDS=60
UI_ACTIONS_CLEANUP_RETENTION_HOURS=24
```

Secrets не должны попадать в git.

---

# 39. Docker

MVP services:

```text
api
worker
scheduler
postgres
redis
seaweedfs
```

`api` и `worker` images должны содержать FFmpeg.

Пример Docker requirement:

```dockerfile
RUN apt-get update \
    && apt-get install -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*
```

В MVP object storage — локальный SeaweedFS в одноконтейнерном режиме `weed mini`. S3 gateway слушает порт `8333` внутри Compose и публикуется на `localhost:9000`; данные хранятся в именованном Docker volume `seaweedfs_data`, смонтированном в `/data`. `api` ожидает запуск сервиса `seaweedfs` и при старте проверяет/создаёт bucket `taskmate`.

Development:

```text
docker compose up
```

---

# 40. API authentication

Поскольку основной UX — Telegram bot, backend должен уметь идентифицировать пользователя по Telegram context.

Для внутренних REST endpoints рекомендуется отдельная authentication mechanism.

Нельзя принимать `user_id` от клиента и считать его доверенным.

> **v1.3:** единственное осознанное исключение — публичные роуты `GET /api/v1/oauth/{provider}/authorize` и `GET /api/v1/oauth/{provider}/callback` (§13.2), которые обслуживают браузерный редирект и не могут требовать предварительной аутентификации. Их защита строится не на сессии пользователя, а на непредсказуемом и одноразовом `state_token` (`oauth_states`).

---

# 41. Source processing rules

## Telegram

Сообщение:

```text
Telegram update
↓
Identify source
↓
Persist message
↓
Check source settings
↓
Queue AI
```

## Email

```text
Sync
↓
Find new messages
↓
Persist
↓
Attachments
↓
Queue AI
```

Если `analysis_text = false`, текст не отправляется в AI.

Если `analysis_voice = false`, voice не отправляется в AI.

Если `save_attachments = false`, вложения не сохраняются.

## Проверка статуса source (v1.4)

Шаг «Check source settings» (Telegram) и «Sync» (Email) обязаны проверять `sources.status = 'active'` до постановки сообщения в AI-очередь/до запуска синхронизации. Источник в статусе `disconnected`, `paused` или `error` не порождает новые `messages`/AI-jobs — это необходимое следствие появления soft-disconnect для Source (см. §7 «Отключение Source»): иначе уже отключённый источник продолжил бы обрабатываться после `DELETE /api/v1/sources/{id}`.

---

# 42. Project assignment rules

## Один project

Автоматически assign.

## Несколько projects

AI определяет candidates.

Если ambiguity:

```text
Выберите проект:
```

## Нет project

Задача сохраняется без project и попадает в Inbox.

---

# 43. Duplicate task detection

Перед созданием задачи AIActionService должен проверить:

- source_message_id;
- существующие task;
- близкое semantic/title совпадение;
- активные задачи пользователя.

MVP должен как минимум гарантировать отсутствие повторной обработки одного external message.

Более сложное semantic deduplication может быть реализовано как P1.

---

# 44. Acceptance criteria — общий уровень

MVP считается готовым, если:

1. Пользователь может зарегистрироваться через Telegram.
2. `/menu` открывает главное меню.
3. `/today` показывает актуальные задачи и события.
4. Пользователь может создать задачу текстом.
5. Пользователь может создать задачу голосом.
6. Пользователь может изменить задачу.
7. Пользователь может удалить задачу с подтверждением.
8. Пользователь может изменить статус.
9. Пользователь может найти задачу.
10. Пользователь может создать project.
11. Пользователь может связать source и project.
12. Telegram source принимает новые сообщения.
13. Email source может синхронизировать новые письма.
14. AI может определить task intent.
15. AI result валидируется схемой.
16. Низкая уверенность не приводит к молчаливому изменению данных.
17. Inbox отображает unresolved items.
18. `/analyze` запускает proactive analysis.
19. Urgent item создаёт notification.
20. Google Calendar подключается.
21. Пользователь может создать calendar event.
22. Morning briefing отправляется по расписанию.
23. Evening stats отправляется по расписанию.
24. Ошибки внешних сервисов корректно обрабатываются.
25. Повторная доставка одного сообщения не создаёт duplicate task.
26. OAuth credentials хранятся в зашифрованном виде.
27. Пользователь не может получить данные другого пользователя.
28. Файлы сохраняются в локальном SeaweedFS через S3-compatible API и переживают перезапуск контейнера благодаря persistent volume.
29. Для долгих AI/STT операций пользователь получает intermediate processing status.
30. Callback data не содержит полных UUID и не превышает лимит Telegram.
31. Undo доступен 15 секунд для поддерживаемых операций.
32. Удаление project не удаляет его tasks и приводит к `project_id = NULL`.
33. Calendar OAuth автоматически обновляет access token через refresh token.
34. Voice pipeline использует FFmpeg перед STT, если провайдер не принимает исходный формат.
35. Email runtime отключён в P0 и не является зависимостью core.
36. Все migrations воспроизводимы.
37. Все критические flows покрыты automated tests.

**Добавлено в v1.2:**

38. Подключение источника не запускает обработку исторических сообщений, полученных до `connected_at` (§7).
39. Удаление задачи — soft-delete: задача пропадает из Tasks/Today/Inbox, но не удаляется физически, пока не наступит retention purge.
40. Пользователь может отменить calendar event через `POST /api/v1/calendar/events/{id}/cancel` с обязательным подтверждением.
41. Повторный/параллельный вызов одного и того же callback (`a:<id>`) не выполняет действие дважды.
42. Просроченные/потреблённые записи `ui_actions` периодически удаляются фоновым job'ом.

**Добавлено в v1.3:**

43. Пользователь может подключить Gmail/Yandex/Mail.ru/Google Calendar через полный OAuth-flow (§13.2): инициация в боте → браузерный authorize/callback → автоматическое подтверждение в Telegram.
44. Callback OAuth без валидного `state` или с уже потреблённым `state` не создаёт и не изменяет credentials.
45. Пользователь может отключить Calendar-подключение через `DELETE /api/v1/calendar/connections/{id}`; существующие calendar_events сохраняются, новые через отключённое подключение не создаются.
46. Свободный текст с намерением «жду ответа/подтверждения от кого-то» создаёт запись `waiting_for` через intent `create_waiting_for`.
47. Перенос задачи между проектами/Inbox доступен через `edit_task.changed_fields.project_hint` и поддерживает Undo.
48. Сообщение, классифицированное как `urgent` (через `inbox_classification`), создаёт `notifications`-запись с `type = 'urgent_item'`.
49. Повторное создание одного и того же calendar event (по `connection_id + external_event_id` или повторное активное событие для одной задачи) предотвращается уникальными индексами, а не только атомарностью callback.
50. `waiting_for` и `notifications` доступны через REST API (`/api/v1/waiting-for`, `/api/v1/notifications`).

**Добавлено в v1.4:**

51. Пользователь может отключить Source через `DELETE /api/v1/sources/{id}`; уже полученные messages/tasks/inbox_items сохраняются, source переводится в `disconnected`, новые сообщения через него не обрабатываются.
52. Повторное подключение ранее отключённого source (тот же внешний чат/канал/ящик) переиспользует существующую строку `sources`, а не создаёт дубликат (не нарушает `ux_sources_user_type_external`).
53. Свободный текст с намерением «напомни мне о X в момент Y» создаёт запись `reminders` через intent `reminder` и порождает `notifications` (`type = 'task_reminder'`) в момент наступления `due_at`.
54. Просроченные/потреблённые записи `oauth_states` периодически удаляются фоновым job'ом, аналогично `ui_actions`.
55. Retention-purge сообщения не приводит к ошибке ссылочной целостности для `waiting_for`/`ai_processing_jobs`, ссылающихся на него (`message_id` обнуляется).

---

# 45. Acceptance criteria — Tasks

### Create

Given user sends:

> Завтра отправить договор

Then:

- task candidate detected;
- title extracted;
- due date extracted;
- confirmation displayed;
- after confirmation task created.

### Edit

Given task exists.

When user says:

> Перенеси на пятницу

Then:

- correct task is found;
- new due date proposed;
- after confirmation task updated.

### Delete

Given task exists.

When delete requested:

- confirmation required;
- task is soft-deleted (`deleted_at` set) only after confirmation, and disappears from Tasks/Today/search/Inbox;
- Undo is available for 15 seconds and simply clears `deleted_at`, with no side effects on `inbox_items`/`calendar_events` links.

### Search

Given multiple matching tasks:

- bot displays candidates;
- user can choose one;
- no silent arbitrary selection.

### Change status (v1.3)

Given task exists.

When user says:

> Отметь как выполненную

Then:

- correct task is found (or, if several candidates match, disambiguation is shown per «Search» above);
- new status is proposed via `intent = change_task_status`;
- confirmation required (§3) regardless of confidence (§21);
- after confirmation, `tasks.status` is updated and `completed_at`/`cancelled_at` set accordingly for terminal statuses;
- `task_events` records `old_value`/`new_value` for status;
- Undo is available for 15 seconds and reverts `status` (and clears `completed_at`/`cancelled_at` if they were set by this transition).

---

# 46. Acceptance criteria — AI

AI должен:

1. возвращать JSON согласно schema;
2. не возвращать произвольные действия;
3. не иметь прямого доступа к DB;
4. иметь timeout;
5. иметь retry policy;
6. записывать результат в `ai_processing_jobs`;
7. сохранять исходное сообщение;
8. корректно обрабатывать invalid JSON;
9. переводить job в failed после исчерпания retry;
10. уведомлять пользователя, если сообщение не обработано.

### Confidence tiers (v1.3)

Given AI has interpreted a message with a given `confidence` (§21):

Then:

- `confidence >= 0.85` (high) → proposal is shown immediately with pre-filled extracted fields and only `[Подтвердить] [Отменить]`;
- `0.60 <= confidence < 0.85` (medium) → proposal is shown with an additional `[Изменить]` option, and `reason` explicitly states the source of uncertainty;
- `confidence < 0.60` (low) → no `ui_actions` proposal is created; the bot instead asks a clarifying question, and interpretation is re-run only after the user responds;
- in all three tiers, no data is mutated before the user's explicit confirmation.

---

# 47. Acceptance criteria — Telegram

1. Webhook отвечает быстро.
2. Duplicate update не обрабатывается дважды.
3. Callback проверяет ownership.
4. Inline buttons работают после перезапуска backend.
5. Ошибка callback не приводит к неконсистентному состоянию.
6. Пользователь всегда получает понятный результат.
7. Кнопка/эндпоинт «Ответить» недоступны для inbox item, связанного с Telegram-сообщением; попытка вызвать `POST /api/v1/inbox/{id}/reply` для такого item возвращает `REPLY_NOT_SUPPORTED_FOR_SOURCE` (v1.3).

---

# 48. Acceptance criteria — Calendar

1. OAuth работает.
2. Token refresh работает.
3. Calendar event создаётся только после confirmation.
4. External event ID сохраняется.
5. Duplicate event creation предотвращается.
6. Ошибка Calendar API обрабатывается.
7. `/schedule` показывает события.
8. Событие может быть отменено только после дополнительного явного подтверждения; статус переходит в `cancelled` (v1.2).
9. Пользователь может отключить Calendar-подключение (`DELETE /api/v1/calendar/connections/{id}`); попытка создать/отменить событие через отключённое подключение возвращает понятную ошибку, а не тихо падает (v1.3).
10. Дублирующее событие (тот же `connection_id + external_event_id`, либо второе активное событие для той же задачи) не создаётся благодаря unique-индексам §14 (v1.3).

---

# 49. Acceptance criteria — Email

1. OAuth credentials сохраняются безопасно.
2. Папки можно выбрать.
3. Новые письма синхронизируются.
4. Duplicate email не создаётся.
5. Attachments сохраняются согласно source settings.
6. Выключение text analysis предотвращает передачу текста в AI.
7. Ошибка авторизации переводит source в error state.

---

# 50. Acceptance criteria — Notifications

Каждое уведомление:

- принадлежит пользователю;
- имеет type;
- имеет payload;
- имеет статус доставки;
- не должно отправляться дважды при retry;
- доступно через `GET /api/v1/notifications` и `GET /api/v1/notifications/{id}` только своему владельцу (v1.3).

---

# 51. Additional acceptance criteria for corrected architecture

## Telegram callback_data

Given a button is generated for any domain entity with UUID:

Then:

- callback payload uses `ui_actions`;
- callback data contains no full UUID;
- callback payload is compact in the form `a:<base36(id)>`;
- callback execution verifies user ownership;
- expired or already consumed callbacks are rejected safely;
- concurrent taps or Telegram retries of the same callback execute the action at most once (atomic conditional `UPDATE ... WHERE consumed_at IS NULL`, see §17);
- callback handling does not depend on process memory;
- generated callback payload remains below Telegram's 64-byte limit.

## Long-running processing UX

Given a user sends a voice message or another input requiring STT, LLM or a slow external API:

Then:

1. webhook returns `200 OK` without waiting for AI;
2. bot sends an intermediate processing message;
3. Celery job processes the request;
4. `status_message_id` is stored with the AI job;
5. bot edits the intermediate message on completion;
6. on failure, bot edits the intermediate message to a clear error and offers retry where applicable.

## Undo

Given a supported mutation succeeds:

Then:

- result contains `[Отменить]`;
- the undo action expires after 15 seconds;
- undo restores the prior state recorded in `task_events`;
- a second execution of the same undo callback is rejected safely.

## Project deletion

Given a project with tasks is deleted:

Then:

- project is deleted (hard delete — this is intentional, see note below);
- tasks remain;
- `project_id` becomes `NULL`;
- tasks remain available in Tasks and Inbox.

> **v1.2:** Project deletion is a hard delete and is **not** covered by Undo, unlike task operations. Rationale: immediately after deletion, tasks that already had `project_id = NULL` for other reasons become indistinguishable from tasks that just lost their project — reconstructing "which NULL tasks belonged to the deleted project" is not reliably possible. This is a deliberate, documented trade-off, not an oversight.

## Source disconnect (v1.4)

Given a source with received messages is disconnected via `DELETE /api/v1/sources/{id}`:

Then:

- `sources.status` becomes `disconnected` (no physical row deletion — `messages.source_id` is a mandatory FK, so a hard delete is not possible while messages exist);
- `source_credentials` for this source, if any, has its encrypted fields cleared;
- existing `messages`, `tasks`, `inbox_items` linked to the source remain untouched;
- no new `messages`/AI jobs are created from this source afterwards (§41);
- reconnecting the same external chat/channel/mailbox reuses the existing `sources` row instead of creating a duplicate (unique index `ux_sources_user_type_external` would otherwise reject it).

## Calendar OAuth refresh

Given the Google Calendar access token has expired or is close to expiry:

Then:

- `OAuthService` uses the encrypted refresh token;
- obtains a new access token;
- encrypts and stores the new access token;
- updates `token_expires_at`;
- retries the original calendar request;
- if refresh fails, connection becomes `error` and user is asked to reconnect.

## Voice conversion

Given Telegram sends voice in OGG/Opus:

Then:

- worker validates file type, size and duration;
- FFmpeg converts the audio to the STT provider format;
- STT receives the converted file;
- temporary converted files are deleted;
- the original attachment is retained only when source settings allow it.

## Email P1 isolation

Given the system is running the P0 release:

Then:

- email synchronization workers are not scheduled;
- email provider adapters are not invoked;
- P0 does not require email OAuth secrets;
- email-related schema may exist but has no runtime dependency on email providers.

---

# 52. Testing strategy

## Unit tests

Покрыть:

- TaskService;
- ProjectService;
- InboxService;
- AIActionService;
- confirmation policy;
- date/time parsing;
- ownership;
- idempotency.

## Integration tests

- PostgreSQL;
- Redis;
- Telegram webhook;
- OAuth;
- Calendar;
- email sync.

## E2E

Минимальные сценарии:

1. `/start`.
2. Создание задачи.
3. Изменение задачи.
4. Удаление задачи.
5. Telegram message → AI → task.
6. Email → AI → Inbox.
7. Voice → STT → task.
8. Calendar event.
9. Morning briefing.
10. `/analyze`.
11. OAuth: инициация в боте → authorize → callback → подключённый source/calendar (v1.3).
12. Отключение Calendar-подключения и последующий отказ создать событие через него (v1.3).

---

# 53. Definition of Done

Функциональность считается готовой только если:

- реализована;
- покрыта тестами;
- имеет migration;
- имеет logging;
- имеет error handling;
- имеет acceptance criteria;
- проверен ownership;
- проверена idempotency;
- добавлена документация API;
- добавлены необходимые environment variables;
- отсутствуют secrets в repository;
- проходит CI pipeline (lint + pytest) без ошибок.

---

# 54. MVP implementation priorities

## P0 — Core

1. Telegram bot.
2. User.
3. Tasks.
4. Projects.
5. Inbox.
6. AI router.
7. PostgreSQL.
8. Redis/Celery.
9. confirmations.
10. error handling.
11. `/analyze` — заглушка: без подключённых источников возвращает информационное сообщение (см. §2, §44).

## P1 — Sources

1. Telegram sources.
2. Gmail.
3. IMAP providers.
4. attachments.
5. proactive analysis.
6. `/analyze` — полная активация (реальный запуск proactive analysis по подключённым источникам).

Email integrations are P1 in the implementation sequence. The database schema may contain email-related fields from the beginning, but the email collection/sync code is not activated in the P0 release.

P0 must not depend on:

- Gmail OAuth libraries;
- IMAP polling/sync workers;
- email provider callbacks;
- email-specific background jobs.

Email adapters remain isolated under:

```text
app/integrations/gmail/
app/integrations/imap/
```

and are enabled only in the P1 phase.

## P1 — Calendar

1. Google OAuth.
2. `/schedule`.
3. Create event.
4. Task → calendar suggestion.

## P1 — Assistant

1. Voice/STT.
2. Morning briefing.
3. Evening stats.
4. Reminders (v1.4, §12.1) — создание через intent `reminder` и срабатывание внутри `schedule_tick`.

---

# 55. Архитектурные решения, требующие подтверждения до production

Следующие решения не заданы однозначно исходными бизнес-требованиями и должны быть утверждены до production:

1. Конкретный LLM provider/model.
2. Конкретный STT provider.
3. ~~Конкретный S3-compatible storage~~ — решено локальной поправкой: SeaweedFS в Docker Compose; доступ приложения только через S3 API, см. §34 и §39.
4. Celery + Redis как окончательный queue stack.
5. Точные confidence thresholds.
6. Retention policy для сообщений и файлов.
7. ~~Поведение удаления source~~ — решено в v1.4: soft-disconnect по аналогии с Calendar, см. §7 «Отключение Source».
8. Политика semantic duplicate detection для разных источников.
9. Максимальный размер attachment.
10. Максимальная длина сообщения для AI.
11. Rate limits.
12. Точный OAuth flow для каждого email provider.
13. Production monitoring/alerting stack.

Уже принятые решения для MVP:

- callback state → DB-backed `ui_actions` с коротким token;
- callback payload → `a:<base36(id)>`;
- Undo TTL → 15 секунд;
- удаление project → `ON DELETE SET NULL`;
- Calendar OAuth → автоматический refresh access token;
- Telegram voice → FFmpeg normalization перед STT;
- долгие AI/STT операции → отдельное bot status message + последующий edit;
- Email → P1 runtime, изолированный от P0.
- Source disconnect → soft-disconnect (`status = 'disconnected'`), а не физическое удаление (v1.4).
- Object storage → локальный SeaweedFS с S3 API и persistent Docker volume.
- Reminders → отдельная таблица `reminders`, срабатывание внутри существующего `schedule_tick`, без нового Celery job (v1.4).

---

# 56. Рекомендуемый порядок реализации

```text
Phase 1
Infrastructure
    ↓
PostgreSQL
Redis
FastAPI
Docker
Alembic
    ↓
Phase 2
User + Telegram
    ↓
Phase 3
Tasks + Projects + Inbox
    ↓
Phase 4
AI pipeline
    ↓
Phase 5
Telegram Sources
    ↓
Phase 6
Email Sources
    ↓
Phase 7
Calendar
    ↓
Phase 8
Voice
    ↓
Phase 9
Proactive Analysis
    ↓
Phase 10
Morning / Evening jobs
    ↓
Phase 11
Security / Observability / hardening
    ↓
MVP Release
```

---

# 57. Итоговая архитектурная цель

TaskMate AI MVP должен быть построен не как Telegram-бот с набором handlers, а как backend-платформа:

```text
                 Telegram UX
                     │
                     ▼
                 FastAPI
                     │
          ┌──────────┼───────────┐
          ▼          ▼           ▼
       Domain      AI Layer    Integrations
       Services                 │
          │                     │
          ▼                     ▼
      PostgreSQL             External APIs
          │
          ▼
       Redis/Celery
          │
          ▼
      Async Workers
```

Ключевой принцип архитектуры:

**Telegram — это интерфейс, FastAPI — application layer, PostgreSQL — source of truth, Celery/Redis — asynchronous execution layer, AI — replaceable interpretation layer, integrations — отдельный adapter layer.**

Такое разделение позволит после MVP заменить Telegram на MAX, добавить новые AI-провайдеры, расширить календарную интеграцию и добавить новые источники без переписывания core domain.

---

# 58. Changelog v1.1 → v1.2

Список изменений по итогам второго технического ревью. Формат: **проблема → решение → где искать**.

## Критичные

1. **Удаление задачи ломало Undo и FK** (`task_events`, `inbox_items.task_id`, `calendar_events.task_id` не имели `ON DELETE`, а Undo не мог физически восстановить удалённую строку) → введён soft-delete (`tasks.deleted_at`), явные `ON DELETE SET NULL`/`CASCADE` для связанных FK → §9, §10, §11, §14, §29, §45, §51.
2. **Не было границы для первичного анализа/синхронизации при подключении источника** (риск разовой обработки всей истории чата/ящика) → добавлено поле `sources.connected_at`, proactive analysis и email sync стартуют только от него, backfill истории явно исключён из MVP → §7, §28, §41, §44 (п. 38).
3. **AIResult-схема `entities` была описана только для `create_task`** → добавлена таблица обязательных полей для всех 17 intents, включая формат адресации задачи (`target`) → §20.

## Важные

4. **Уникальность составных индексов не была объявлена явно** (`sources`, `projects`) → добавлены частичные `UNIQUE INDEX` с пояснением условий (`WHERE external_source_id IS NOT NULL`, `WHERE is_archived = FALSE`) → §7.
5. **IMAP UID нестабилен между сессиями** → `external_message_id` для email теперь явно определяется через `Message-ID` (RFC 5322) с fallback на `UIDVALIDITY:UID` → §7.
6. **Race condition при обработке callback** (двойной тап/ретрай Telegram мог выполнить действие дважды) → атомарный `UPDATE ... WHERE consumed_at IS NULL ... RETURNING` вместо раздельных SELECT+UPDATE → §17, §51 (п. 41).
7. **Не было общей идемпотентности Telegram `update_id`** (только на уровне `messages`) → добавлена таблица `telegram_updates` и шаг `INSERT ... ON CONFLICT DO NOTHING` в webhook → §17, §23.
8. **`ui_actions` не имела механизма очистки** → добавлен periodic job `cleanup_ui_actions` → §17, §28, §51 (п. 42).
9. **Не было cancel-эндпоинта для calendar event**, хотя UX требует подтверждения отмены → добавлен `POST /api/v1/calendar/events/{id}/cancel` и enum `calendar_events.status` (`pending`/`confirmed`/`cancelled`) → §14, §25, §48.
10. **Confidence tiers не были связаны с конкретным поведением** (всё и так требует подтверждения) → прописано, что confidence меняет форму proposal (high/medium) или блокирует его создание в пользу уточняющего вопроса (low) → §21.
11. **Индивидуальное расписание брифингов на пользователя не сочеталось со статическим Celery beat** → введён `schedule_tick` (periodic, пересчитывает локальное время каждого пользователя по его `timezone`) → §28, §32.
12. **Не были определены enum-значения** для `messages.message_type`, `calendar_events.status`, `notifications.status` → добавлены → §7, §14, §16.
13. **`OAuthCredential` в доменной модели не соответствовал физической схеме** → список сущностей приведён в соответствие со схемой (`source_credentials` + поля в `calendar_connections`) → §6.
14. **`/analyze` был в общем MVP scope без учёта зависимости от P1 sources** → явно размечен: заглушка в P0, полная активация в P1 → §2, §54.
15. **"Task → Calendar suggestion" был заглушкой без описания** → добавлен минимальный pipeline и условие срабатывания → §14.

## Незначительные

16. Уточнено, что `analysis_voice` не имеет эффекта для email-источников → §7.
17. Ключ объекта в S3 дополнен расширением файла; локальная реализация S3 зафиксирована как SeaweedFS → §34, §39.
18. Добавлены дефолты пагинации (`limit`/`offset`) для списковых эндпоинтов → §25.
19. Явно описан пересчёт расписания брифингов при смене пользователем timezone → §32.
20. В Definition of Done добавлено требование прохождения CI (lint + pytest) → §53.
21. Добавлено пояснение по таймаутам AI/STT-вызовов и соответствующие environment variables → §18, §38.

## Изменения схемы данных (сводно)

- `tasks`: + `deleted_at`.
- `sources`: + `connected_at`.
- `task_events.task_id`: + `ON DELETE CASCADE`.
- `inbox_items.task_id`, `calendar_events.task_id`: + `ON DELETE SET NULL`.
- Новая таблица `telegram_updates`.
- Новые unique-индексы: `sources(user_id, type, external_source_id)` (partial), `projects(user_id, name)` (partial, только активные).

## Новые/изменённые эндпоинты

- `POST /api/v1/calendar/events/{id}/cancel` (новый).
- `DELETE /api/v1/tasks/{id}` — уточнено, что это soft-delete.

## Новые background jobs

- `schedule_tick()` — периодический пересчёт расписания брифингов.
- `cleanup_ui_actions()` — очистка просроченных/потреблённых callback-токенов.

## Новые environment variables

```text
AI_TIMEOUT_SECONDS=30
STT_TIMEOUT_SECONDS=30
SCHEDULER_TICK_INTERVAL_SECONDS=60
UI_ACTIONS_CLEANUP_RETENTION_HOURS=24
```

## Что осталось открытым (не входит в эту правку)

Пункты из §55 «Архитектурные решения, требующие подтверждения до production» не изменились по существу — это по-прежнему решения бизнес-уровня (выбор LLM/STT провайдера, retention policy, лимиты, поведение удаления source и т.д.), которые не блокируют написание кода, но должны быть закрыты до production-релиза.

---

# 59. Changelog v1.2 → v1.3

Список изменений по итогам третьего технического ревью. Формат: **проблема → решение → где искать**.

## Критичные

1. **OAuth-flow не был технически специфицирован** (были только env-переменные client_id/secret, token refresh и упоминание `OAuthService`, но ни одного эндпоинта инициации/callback, и не было объяснено, как браузерный callback без Telegram-контекста связывается с `user_id`) → добавлена таблица `oauth_states` (одноразовый `state_token`, атомарная пометка `consumed_at`), публичные эндпоинты `GET /api/v1/oauth/{provider}/authorize` и `GET /api/v1/oauth/{provider}/callback`, пошаговый flow с проактивной отправкой подтверждения в Telegram → §6, §13.2, §25, §31, §38, §40.
2. **Не было способа отключить Calendar** (у Sources есть `DELETE /api/v1/sources/{id}`, у Calendar аналога не было; `calendar_events.connection_id` — обязательный FK без `ON DELETE`) → добавлен `DELETE /api/v1/calendar/connections/{id}` (перевод в статус `disconnected`, затирание токенов, существующие события сохраняются), `calendar_connections.status` получил полный enum, `connection_id` получил явный `ON DELETE RESTRICT` → §13, §13.1, §25, §48.

## Важные

3. **`waiting_for` не был доступен как AI intent** (таблица и Inbox-тип существовали, но среди 17 значений `AIResult.intent` канала создания не было) → добавлен intent `create_waiting_for` с описанной схемой `entities` → §20.
4. **Не было REST API для `waiting_for` и `notifications`** (обе таблицы есть в domain model и в схеме, но ни одного эндпоинта в §25) → добавлены `GET/POST/PATCH /api/v1/waiting-for...` и `GET /api/v1/notifications...` (последнее — read-only, так как канал доставки — Telegram) → §25, §27 (новый `WaitingForService`), §50.
5. **`edit_task.changed_fields` не включал смену проекта**, хотя перенос задачи между проектами/Inbox входит в обязательный список Undo-операций (§29) → в `changed_fields` добавлен `project_hint` с чёткой семантикой (отсутствие ключа = без изменений, `null` = перенос в Inbox, строка = резолвится как в `create_task`) → §20.
6. **Не было связи между 17 intents и 14 типами `inbox_items.item_type`** (`urgent`, `financial`, `legal`, `risk`, `opportunity`, `approval`, `promise`, `personal` не совпадали ни с одним intent; неясно было, откуда acceptance criteria §44 п. 19 берёт классификацию `urgent`) → добавлено поле `AIResult.inbox_classification` (независимое от `intent`, заполняется в т.ч. при proactive analysis), явная таблица соответствия intent → item_type, правило «urgent создаёт notification» → §11, §16, §20, §44.
7. **Не было unique-ограничения на `calendar_events`**, хотя acceptance criteria (§48 п. 5) требует предотвращения дублей → добавлены `ux_calendar_events_connection_external` (по `connection_id, external_event_id`) и `ux_calendar_events_task_active` (не более одного активного события на задачу) → §14, §48.
8. **`reply_email` — единственный intent для ответа, ограничение для Telegram нигде не проговорено** → явно задокументировано: кнопка/эндпоинт «Ответить» работают только для email-сообщений; добавлен `POST /api/v1/inbox/{id}/reply` с кодом ошибки `REPLY_NOT_SUPPORTED_FOR_SOURCE` для остальных источников → §20, §25, §47.
9. **Не было acceptance-сценария для `change_task_status`** и для поведения confidence-tiers (центральное изменение v1.2) → добавлены сценарии «Change status» и «Confidence tiers» → §45, §46.

## Незначительные

10. Добавлены enum-значения для `notifications.type`, `ai_processing_jobs.job_type`, `task_events.source` → §10, §15, §16.
11. `tasks.source_message_id` и `inbox_items.message_id` получили явный `ON DELETE SET NULL` → §9, §11.
12. Специфицирован формат `meta.pagination` (`limit`/`offset`/`total`) для списковых эндпоинтов → §26.
13. Специфицирован payload `PATCH /api/v1/sources/{id}/folders` → §25.

## Изменения схемы данных (сводно)

- Новая таблица `oauth_states`.
- `tasks.source_message_id`: + `ON DELETE SET NULL`.
- `inbox_items.message_id`: + `ON DELETE SET NULL`.
- `calendar_events.connection_id`: + `ON DELETE RESTRICT`.
- Новые unique-индексы: `calendar_events (connection_id, external_event_id)` (partial), `calendar_events (task_id)` (partial, только активные).
- `AIResult`: новое необязательное поле `inbox_classification`.
- `calendar_connections.status`: enum расширен до `connecting/active/error/disconnected`.

## Новые/изменённые эндпоинты

- `GET /api/v1/oauth/{provider}/authorize` (новый).
- `GET /api/v1/oauth/{provider}/callback` (новый).
- `GET /api/v1/calendar/connections` (новый).
- `DELETE /api/v1/calendar/connections/{id}` (новый).
- `GET/POST/PATCH /api/v1/waiting-for`, `.../complete`, `.../cancel` (новые).
- `GET /api/v1/notifications`, `GET /api/v1/notifications/{id}` (новые).
- `POST /api/v1/inbox/{id}/reply` (новый).

## Новые intents

- `create_waiting_for`.

## Новые environment variables

```text
PUBLIC_BASE_URL=
OAUTH_STATE_TTL_SECONDS=600
```

## Что осталось открытым (не входит в эту правку)

Список из §55 не изменился по существу. Дополнительно стоит отметить как открытый вопрос для production: точный набор OAuth `scope` для каждого провайдера (Gmail/Yandex/Mail.ru/Google Calendar) в данной ревизии не детализирован — это уже покрывается существующим пунктом §55 п. 12 «Точный OAuth flow для каждого email provider», который теперь сужается до «точный список scope», так как сам механизм связывания callback с пользователем специфицирован в §13.2.

---

# 60. Changelog v1.3 → v1.4

Список изменений по итогам четвёртого технического ревью. Формат: **проблема → решение → где искать**.

## Критичные

1. **Не было способа отключить Source без нарушения ссылочной целостности** (`messages.source_id` — обязательный `NOT NULL` FK без `ON DELETE`; `DELETE /api/v1/sources/{id}` уже был MVP-эндпоинтом в §25, но его поведение было явно вынесено в §55 п. 7 как решение, требующее подтверждения только «до production» — то есть не определено для самого MVP; тот же класс проблемы для Calendar уже был найден и исправлен в v1.3, см. §13.1) → введён soft-disconnect (`sources.status = 'disconnected'`, затирание `source_credentials`, существующие messages/tasks/inbox_items сохраняются), явное требование проверять `status = 'active'` перед постановкой в AI-очередь/синхронизацию, правило переиспользования существующей строки при переподключении → §7, §25, §41, §51, §55.
2. **Intent `reminder` существовал в AIResult-контракте (17→18 допустимых intents, схема `entities`) без единой строки хранения** (ни таблицы, ни REST-эндпоинта, ни сервиса — `notifications.type` предвосхищал `task_reminder`, но `notifications` не хранит будущий момент срабатывания) → добавлена таблица `reminders`, REST API `/api/v1/reminders`, срабатывание встроено в уже существующий `schedule_tick` без нового Celery job → §6, §12.1, §20, §25, §28, §54.

## Важные

3. **`waiting_for.message_id` и `ai_processing_jobs.message_id` не имели `ON DELETE SET NULL`**, хотя это же правило для структурно идентичных полей (`tasks.source_message_id`, `inbox_items.message_id`) было явно введено в v1.2/v1.3 как защита от будущего retention-purge сообщений → добавлено `ON DELETE SET NULL` для обоих полей → §12, §15.
4. **У `oauth_states` не было периодической очистки**, хотя это структурно та же накопительная таблица (`expires_at`/`consumed_at`), что и `ui_actions`, для которой аналогичная проблема была явно распознана и закрыта `cleanup_ui_actions` ещё в v1.2 → добавлен `cleanup_oauth_states()`, переиспользующий `UI_ACTIONS_CLEANUP_RETENTION_HOURS` → §13.2, §28.

## Незначительные

5. Уточнено, что переподключение ранее отключённого source обязано переиспользовать существующую строку `sources`, а не создавать новую (иначе конфликт с `ux_sources_user_type_external`) — аналогично уже принятому для `calendar_connections` в §13.1 → §7.
6. Пункт 7 списка открытых вопросов §55 («Поведение удаления source») помечен решённым со ссылкой на §7, без изменения нумерации остальных пунктов (на неё ссылается changelog v1.2→v1.3).
7. Добавлены `reminders` и Source disconnect в список «Уже принятые решения для MVP» (§55).

## Изменения схемы данных (сводно)

- Новая таблица `reminders`.
- `waiting_for.message_id`: + `ON DELETE SET NULL`.
- `ai_processing_jobs.message_id`: + `ON DELETE SET NULL`.
- `sources.status = 'disconnected'` получил закреплённую семантику (ранее значение существовало в enum, но не имело описанного сценария использования).

## Новые/изменённые эндпоинты

- `GET/POST/PATCH /api/v1/reminders`, `.../{id}/cancel` (новые).
- `DELETE /api/v1/sources/{id}` — уточнено, что это soft-disconnect, а не физическое удаление.

## Новые background jobs

- `cleanup_oauth_states()` — периодическая очистка просроченных/потреблённых `oauth_states`.
- Срабатывание `reminders` — встроено в существующий `schedule_tick()`, отдельного job нет.

## Новые environment variables

Нет — `cleanup_oauth_states()` переиспользует уже существующий `UI_ACTIONS_CLEANUP_RETENTION_HOURS`.

## Что осталось открытым (не входит в эту правку)

Список из §55 не изменился по существу, за исключением пункта 7, помеченного решённым. Не закрыт вопрос о том, нужен ли пользователю способ увидеть/отменить ещё не сработавший reminder из главного меню (а не только через REST) — это UX-вопрос, а не архитектурный пробел, и может быть решён на уровне бот-команд в рамках реализации P1 — Assistant без изменения схемы.
