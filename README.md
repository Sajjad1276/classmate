# ClassMate

ClassMate is a multi-school Telegram classroom memory and coordination bot. It organizes daily reports, homework, exams, timetables and reminders with trusted human approval. The MVP intentionally does not use AI.

## Stack

- Python 3.11+
- aiogram 3.x
- PostgreSQL
- SQLAlchemy 2 async + asyncpg
- Alembic migrations
- Docker Compose for local development

## Quick start

```bash
cp .env.example .env
# Set BOT_TOKEN and add your Telegram numeric ID to BOOTSTRAP_ADMIN_IDS
docker compose up --build
```

The local Compose stack starts PostgreSQL, applies Alembic migrations, then runs the bot and reminder worker as separate processes.

## First-time setup

1. Add your Telegram numeric ID to BOOTSTRAP_ADMIN_IDS in .env.
2. Start the bot and send /start in a private chat.
3. Create a school: /school_new نام مدرسه | ۱۴۰۵-۱۴۰۶ | Asia/Tehran.
4. Create a class: /class_new شناسه_مدرسه | دهم | ۱۰۴ | دهم ۱۰۴.
5. Optionally assign staff: /school_admin_add school_id | telegram_id and /class_manager_add class_id | telegram_id.
6. Generate connection and student invitation codes: /class_code شناسه_کلاس.
7. Add the bot to the target Telegram group and run /connect کد_اتصال as a group administrator.
8. Students start the bot privately and run /join کد_عضویت.
9. Add subjects in the group: /subject_add ریاضی.
10. Use /new_homework, /new_exam and /new_report to submit records.
11. A trusted approver reviews them using the inline approval buttons.

## Useful commands

- /homework — approved homework for the current group.
- /exams — approved exams for the current group.
- /today — approved daily report for today.
- /week — show the configured timetable.
- /timetable_set شنبه | ۱ | ریاضی | 08:00-08:45 — set a timetable slot (class manager).
- /year_new school_id | ۱۴۰۶-۱۴۰۷ — create and activate a school year.
- /school_admin_add school_id | telegram_id — appoint a school admin (platform admin).
- /class_manager_add class_id | telegram_id — appoint a class manager (school admin).
- /reminders_off class_id [homework|exams|all] — disable private reminders; /reminders_on re-enables them.
- /pending — pending submissions for trusted approvers.
- /ask تکلیف نگارش — deterministic keyword-based search.
- /ask برای جلسه بعد نگارش چی داریم؟ — resolve the next scheduled lesson from the class timetable and list approved homework due for that lesson.
- /approver_add class_id | telegram_id — grant trusted approver access.

For private-chat queries that need a class ID, pass it after the command, for example /homework 12.

## Security

- Never commit .env, bot tokens, database passwords or Railway credentials.
- Telegram Privacy Mode is compatible; the bot does not read all group messages.
- Class and school roles are checked in the application.
- Approved records are separated from pending submissions.
- Audit and approval events are persisted.

## Project docs

- Product specification: docs/PRODUCT.md
- Architecture: docs/ARCHITECTURE.md
- Railway deployment preparation: docs/RAILWAY.md

## Current MVP limitations

Timetable entry management is available through /timetable_set. The next-session resolver uses the next future weekday occurrence in the timetable (08:00 when a period has no start time); holidays and one-off timetable changes are not automatically inferred. Record editing/revision UI, a web dashboard, attendance/grades, advanced analytics and richer notification schedules are follow-up tasks. FSM form state is in-memory, while submitted records and reminder jobs are durable in PostgreSQL.
