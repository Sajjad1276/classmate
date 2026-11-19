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

## Invitation codes

Group-link codes are stored as SHA-256 hashes and expire after 30 minutes; a successful group connection consumes the code. Student join codes are reusable for seven days. Expired codes are rejected at query time.

## Next-lesson resolver

The resolver reads a class's timetable entries for a subject, evaluates the next future weekday/time in the school's timezone and creates/reuses a LessonSession row. The search begins no earlier than valid_from, checks a full weekly cycle, and stops at valid_until. Homework may reference that session through due_session_id and also stores the local due date for reminders. The deterministic /ask flow recognizes next-session phrasing and returns approved work linked to that session or due on the same local date. This does not infer holidays or exceptional schedule changes.

## Daily summaries and reporting

A class may have multiple DailyReport submissions on the same local date. The unique class/date constraint was replaced by a non-unique lookup index; migration 0003 preserves existing report rows. The /today command and class-group dashboard use the school's timezone and a half-open local-day interval [midnight, next midnight) for scheduled exams, which avoids assuming every local day is exactly 24 hours. Homework is filtered by its local due date.\n\n## Reminder worker

Workers claim due reminder rows via PostgreSQL FOR UPDATE SKIP LOCKED, set a processing lease, send, then mark success or schedule exponential retry. Recovery also claims expired leases with SKIP LOCKED. Every claim receives a lease timestamp token; a worker may finish only while the persisted row is still processing under that exact token, preventing a delayed worker from overwriting a newer claim. The queue is durable and independent of the bot process.

Delivery is at-least-once, not exactly-once: if Telegram accepts a message and the worker crashes before persisting success, the recovered job may be sent again. Dedupe keys prevent duplicate scheduling for the same record/target/time, but cannot make Telegram delivery transactional with PostgreSQL.

## Telegram group interface and review

A school may have one central group binding; each class may have its own group binding. A connected class group exposes an inline menu for approved homework, future exams, currently valid timetable slots, a local-day summary, new submissions and the review queue. The central school group is not treated as a class record feed. Group binding requires the invoking user to be a Telegram administrator/creator and the bot to have sufficient administrator visibility for membership checks.

Review actions re-check class-level permissions and lock the record row. Only PENDING records can be approved; approval is rejected when the reviewer is the submitter. Approval/rejection is audited. On success, the submitter receives a private outcome message only if the user has previously started a private chat. Telegram notification failures are logged and do not roll back the committed review decision.

## Railway topology

Create one Railway project with:
1. Managed PostgreSQL service.
2. Bot service from this repository, start command: python -m classmate.bot.main.
3. Reminder worker service from this repository, start command: python -m classmate.worker.main.

Set BOT_TOKEN on both application services. Set DATABASE_URL on both to the PostgreSQL service reference variable. Run alembic upgrade head once as a controlled migration step before starting the bot/worker on a new environment. Do not make both services run migrations concurrently.

No Railway resources are created and no deployment is performed by this repository change.
