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
# Set BOT_TOKEN and add the platform owner's numeric Telegram ID to BOOTSTRAP_ADMIN_IDS
docker compose up --build
```

The local Compose stack starts PostgreSQL, applies Alembic migrations, then runs the bot and reminder worker as separate processes.

## First-time setup

1. Put the platform owner's numeric Telegram ID in `BOOTSTRAP_ADMIN_IDS`.
2. Open a private chat with the bot and press **Start**. If the ID is configured correctly, the role-specific **پنل مدیر کل** button appears.
3. Open the platform panel and create the school and first academic year.
4. Open **مدیریت مدرسه** to create classes and appoint school/class managers.
5. Open **مدیریت کلاس** for a class to create group/student invitation codes, add subjects and configure timetable slots.
6. Students press **عضویت در کلاس** in the private bot and enter the invitation code.
7. Use **ثبت اطلاعات** to submit homework, exams and daily reports.
8. Trusted approvers open **صف بررسی** and approve or reject submissions.

Private-chat actions are started from Telegram keyboard buttons and inline panels; users do not need to type slash commands. See [the button and panel guide](docs/COMMANDS.md).

## Private bot navigation

- **کلاس‌های من** — list classes the current account can access and open a class dashboard.
- **عضویت در کلاس** — enter an invitation code.
- **ثبت اطلاعات** — start a guided form for homework, exams or a daily report.
- **تکالیف / امتحانات / برنامه هفتگی / گزارش امروز** — display approved class information.
- **جست‌وجو** — deterministic keyword search and next-lesson lookup against approved data.
- **یادآوری** — enable or disable private homework and exam reminders.
- **صف بررسی** — available only to approvers for classes where they have permission.
- **مدیریت کلاس / مدیریت مدرسه / پنل مدیر کل** — displayed only for users with the corresponding application role.

Each inline panel edits the same panel message. Back buttons return to the parent panel, and wizard sessions have a return-to-menu control. Hiding a button is only a UI convenience; each operation independently verifies authorization.

## Group interface status

The private interface is button-driven. Group workflows are a separate follow-up phase and are not considered complete in this release. Keep Telegram Privacy Mode enabled; the bot does not read arbitrary group messages.

## Security

- Never commit `.env`, bot tokens, database passwords or Railway credentials.
- Class and school roles are checked in the application.
- Approved records are separated from pending submissions.
- Audit and approval events are persisted.
- Invitation codes are stored as hashes and expire.

## Project docs

- Button and panel guide: docs/COMMANDS.md
- Product specification: docs/PRODUCT.md
- Architecture: docs/ARCHITECTURE.md
- Railway deployment preparation: docs/RAILWAY.md
- Operations runbook: docs/OPERATIONS.md

## Current MVP limitations

The current `/today`-equivalent panel shows the approved daily report; it is not yet a complete daily dashboard combining periods, due homework and exams. The next-session resolver uses the next future weekday occurrence in the timetable (08:00 when a period has no start time); holidays and one-off timetable changes are not automatically inferred. Record revision UI, a web dashboard, attendance/grades, advanced analytics and richer notification schedules are follow-up tasks. FSM form state is in-memory, while submitted records and reminder jobs are durable in PostgreSQL.
