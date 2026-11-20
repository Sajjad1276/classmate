# Railway production deployment

ClassMate uses three long-running services in one Railway project. The bot and worker share PostgreSQL privately; they must never use a public database URL.

## Project topology

| Service | Source | Runtime command | Purpose |
|---|---|---|---|
| Postgres | Railway PostgreSQL template | Template-managed | Persistent application database |
| classmate | `Sajjad1276/classmate`, branch `feat/classmate-mvp` until PR #1 is merged | `python -m classmate.bot.main` | Telegram long-polling bot |
| classmate-worker | Same repo and branch | `python -m classmate.worker.main` | Durable reminder delivery worker |

Both application services use the repository Dockerfile builder. Do not configure a public HTTP domain: the bot uses Telegram long polling and the worker has no inbound HTTP endpoint.

## Required application variables

Set these on **both** application services:

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `DEFAULT_TIMEZONE` | `Asia/Tehran` |
| `LOG_LEVEL` | `INFO` |
| `REMINDER_POLL_SECONDS` | `5` |
| `BOT_TOKEN` | Telegram token from BotFather; enter privately in Railway |
| `BOOTSTRAP_ADMIN_IDS` | On the `classmate` bot only: comma-separated numeric Telegram user IDs that may create the first school |

Never commit real tokens, passwords or connection strings. Do not paste them into source code, issues or chat messages.

## Migration ordering

Only the `classmate` bot service runs `alembic upgrade head (current schema head: 0005_daily_report_multi_submit)` as its pre-deploy command. The worker entry point calls the schema-readiness check and waits until the database's `alembic_version` matches the repository's Alembic head before starting its polling loop. This avoids two services racing to apply the same migration.

The migration runs on every bot deployment; `upgrade head` is safe to repeat for an already-current database. Keep schema changes in new Alembic revisions. Do not edit an already-applied migration.

## Initial deployment checklist

1. Confirm the PostgreSQL service reports a healthy/successful deployment and has its persistent volume mounted at `/var/lib/postgresql/data`.
2. Attach the application services to this repository and the intended branch.
3. Set the non-secret values above on both services.
4. Add `BOT_TOKEN` privately on both application services. Add `BOOTSTRAP_ADMIN_IDS` on the `classmate` bot service only.
5. Deploy the bot. Confirm its pre-deploy migration succeeds and its runtime stays healthy.
6. Deploy the worker; it should pass the schema-head wait and then remain running.
7. Open a private chat with the bot and send `/start`. Create a school and class using the commands in the root README.

## Local development

1. Copy `.env.example` to `.env`.
2. Set `BOT_TOKEN` and add your numeric Telegram user ID to `BOOTSTRAP_ADMIN_IDS`.
3. Run `docker compose up --build`. Compose starts PostgreSQL, runs migrations once, then starts bot and worker.

## Operational cautions

- A Telegram token is a credential. Rotate it with BotFather if it is exposed.
- Only trusted approvers can approve submitted homework, exams and reports; submitters cannot approve their own records.
- Reminder delivery is at-least-once, not exactly-once. A retry around a process/database interruption can cause a duplicate Telegram message.
- FSM wizard state and processed-update leases are persisted in PostgreSQL. The bot also holds a session-level PostgreSQL advisory lock while long polling, so overlapping Railway deployments serialize instead of competing for the same Telegram token.
- Reminder outcomes that are ambiguous after network/worker interruption are marked uncertain for manual reconciliation; automatic retries are bounded and avoid blindly duplicating sends.
- Keep the Postgres volume attached. Do not delete it unless intentionally destroying all ClassMate data.
- The polling advisory lock serializes bot replicas, but a single bot replica remains the recommended production topology.
