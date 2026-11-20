# Architecture

## Runtime topology

- classmate.bot.main: aiogram polling process.
- classmate.worker.main: independent reminder delivery process.
- PostgreSQL: source of truth for users, tenants, records, approvals, audit and reminder jobs.

Both processes use the same image and database. They can be deployed as separate Railway services with different start commands.

## Application structure

- bot/: Telegram routers split by group, private, management, approval and wizard responsibilities; context.py owns shared identity/class helpers; formatters.py owns Persian rendering.
- domain/: typed workflow/role enums, date rules and normalization.
- services/: record lifecycle, audit, authorization, archive/restore and notification scheduling.
- db/: SQLAlchemy models, PostgreSQL FSM storage and polling singleton lock.
- worker/: independent reminder claiming, delivery, retry and manual reconciliation.

Routers should orchestrate input/output only. Business rules belong in services; database access belongs behind session/repository boundaries.

## Tenant isolation

A school owns academic years. A class belongs to one academic year. All class-scoped access must resolve through this hierarchy. Never trust a user-supplied class ID without checking membership or school/class role. Telegram chat IDs are unique bindings to one class.

The MVP applies tenant boundaries through scoped SQL queries and permission checks. Before exposing a public API or dashboard, add PostgreSQL Row-Level Security and dedicated isolation tests.

## Database and migrations

PostgreSQL is the production database. Alembic owns schema changes. Do not use Base.metadata.create_all() at application startup.

The initial migration bootstraps metadata; future migrations should be generated and reviewed as explicit Alembic operations rather than relying on runtime schema creation.

## Telegram update processing

The bot uses long polling and Telegram Privacy Mode remains enabled. For class-group binding, both the command issuer and the bot must be Telegram administrators. A shared PostgreSQL transaction-scoped advisory lock serializes class-group and school-group binding mutations, and each path checks the other tenant table before binding. Arbitrary message deletion or member-ban privileges are not required. Group commands, mentions/replies to the bot and inline callbacks are supported; the bot does not ingest arbitrary private group conversations.

FSM state/data is persisted in PostgreSQL (fsm_state_records) and survives process restarts. A DB-backed update middleware claims Telegram update IDs using an expiring lease in processed_updates, preventing duplicate handling after redelivery. A PostgreSQL session-level advisory lock serializes long polling during rolling deployments; only the lock-owning connection may poll the production bot token.

## Invitation codes\n\nGroup-link codes are stored as SHA-256 hashes and expire after 30 minutes; a successful group connection consumes the code. Student join codes are reusable for seven days. Expired codes are rejected at query time.\n\n## Next-lesson resolver\n\nThe resolver reads a class's timetable entries for a subject, evaluates the next future weekday/time in the school's timezone and creates/reuses a LessonSession row. Homework may reference that session through due_session_id and also stores the local due date for reminders. The deterministic /ask flow recognizes next-session phrasing and returns approved work linked to that session or due on the same local date. This does not infer holidays or exceptional schedule changes.\n\n## Reminder worker

Workers claim due reminder rows via PostgreSQL FOR UPDATE SKIP LOCKED, set a processing lease, re-check the source record immediately before sending, then mark success or schedule a bounded retry. Expired leases and network/server outcomes with uncertain Telegram delivery are marked uncertain for operator reconciliation rather than automatically duplicated. Cancelling a source record cancels queued, retrying, processing and uncertain reminders; a send already accepted by Telegram cannot be recalled.

## Railway topology

Create one Railway project with:
1. Managed PostgreSQL service.
2. Bot service from this repository, start command: python -m classmate.bot.main.
3. Reminder worker service from this repository, start command: python -m classmate.worker.main.

Set BOT_TOKEN on both application services. Set DATABASE_URL on both to the PostgreSQL service reference variable. Run alembic upgrade head once as a controlled migration step before starting the bot/worker on a new environment. Do not make both services run migrations concurrently.


## Record and timetable invariants

A class/date can contain multiple daily reports; the lookup index on (class_id, report_date) is non-unique. Approved-only queries exclude cancelled/rejected submissions. Subjects, class-subject links and timetable slots use soft deletion; school-wide archive/restore is timestamp-correlated so a global restore does not undo an earlier class-only removal. A report cancellation timestamp and audit/approval event preserve the cancellation history.

## Migration history

The current schema is at revision 0005_daily_report_multi_submit. Migrations are forward-applied by the bot service before polling; the worker waits for the schema head and does not independently migrate. Migrations that contain persistent runtime state intentionally refuse destructive downgrade. Migration 0005 can be downgraded only when no duplicate class/date reports exist.