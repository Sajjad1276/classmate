# Architecture

## Runtime topology

- classmate.bot.main: aiogram polling process.
- classmate.worker.main: independent reminder delivery process.
- PostgreSQL: source of truth for users, tenants, records, approvals, audit and reminder jobs.

Both processes use the same image and database. They can be deployed as separate Railway services with different start commands.

## Application structure

- bot/: Telegram routers, callback keyboards and FSM forms.
- domain/: pure date and normalization rules.
- services/: use-case and authorization logic.
- db/: SQLAlchemy models and async session factory.
- worker/: database-backed reminder leasing and Telegram delivery.

Routers should orchestrate input/output only. Business rules belong in services; database access belongs behind session/repository boundaries.

## Tenant isolation

A school owns academic years. A class belongs to one academic year. All class-scoped access must resolve through this hierarchy. Never trust a user-supplied class ID without checking membership or school/class role. Telegram chat IDs are unique bindings to one class.

The MVP applies tenant boundaries through scoped SQL queries and permission checks. Before exposing a public API or dashboard, add PostgreSQL Row-Level Security and dedicated isolation tests.

## Database and migrations

PostgreSQL is the production database. Alembic owns schema changes. Do not use Base.metadata.create_all() at application startup.

The initial migration bootstraps metadata; future migrations should be generated and reviewed as explicit Alembic operations rather than relying on runtime schema creation.

## Telegram update processing

The bot uses long polling and Telegram Privacy Mode remains enabled. During group binding, the bot must be a Telegram administrator so it can reliably verify that the command issuer is a group administrator; it does not require message deletion or member-ban privileges. Group commands, mentions/replies to the bot and inline callbacks are supported; the bot does not ingest arbitrary private group conversations.

FSM state currently uses in-memory storage. A bot restart can interrupt an unfinished form, but never destroys submitted records or reminders. Redis-backed FSM storage can be added if persistent wizard continuity is required.

## Invitation codes\n\nGroup-link codes are stored as SHA-256 hashes and expire after 30 minutes; a successful group connection consumes the code. Student join codes are reusable for seven days. Expired codes are rejected at query time.\n\n## Next-lesson resolver\n\nThe resolver reads a class's timetable entries for a subject, evaluates the next future weekday/time in the school's timezone and creates/reuses a LessonSession row. Homework may reference that session through due_session_id and also stores the local due date for reminders. The deterministic /ask flow recognizes next-session phrasing and returns approved work linked to that session or due on the same local date. This does not infer holidays or exceptional schedule changes.\n\n## Reminder worker

Workers claim due reminder rows via PostgreSQL FOR UPDATE SKIP LOCKED, set a processing lease, send, then mark success or schedule exponential retry. Expired leases are recovered after worker crashes. The queue is durable and independent of the bot process.

## Railway topology

Create one Railway project with:
1. Managed PostgreSQL service.
2. Bot service from this repository, start command: python -m classmate.bot.main.
3. Reminder worker service from this repository, start command: python -m classmate.worker.main.

Set BOT_TOKEN on both application services. Set DATABASE_URL on both to the PostgreSQL service reference variable. Run alembic upgrade head once as a controlled migration step before starting the bot/worker on a new environment. Do not make both services run migrations concurrently.

No Railway resources are created and no deployment is performed by this repository change.
