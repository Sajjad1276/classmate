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
3. Open the platform panel, press **ایجاد مدرسه** and enter only the school name and academic-year name in two short steps.
4. Press **افزودن ربات به گروه مدرسه**, choose the school's Telegram group and add ClassMate. The bot links the group automatically; no group ID or typed /connect command is needed.
5. Open **مدیریت مدرسه** to create classes and appoint school/class managers.
6. Open **مدیریت کلاس** for a class to create student invitation codes, add subjects and configure timetable slots.
7. Students press **عضویت در کلاس** in the private bot and enter the invitation code.
8. Use **ثبت اطلاعات** to submit homework, exams and daily reports.
9. Trusted approvers open **صف بررسی** and approve or reject submissions.

Private-chat actions are started from Telegram keyboard buttons and inline panels; users do not need to type slash commands. See [the button and panel guide](docs/COMMANDS.md).

## Private bot navigation

- **کلاس‌های من** — list classes the current account can access and open a class dashboard.
- **عضویت در کلاس** — enter an invitation code.
- **ثبت اطلاعات** — start a guided form for homework, exams or a daily report.
- **تکالیف / امتحانات / برنامه هفتگی / گزارش امروز** — display approved class information.
- **جست‌وجو** — deterministic keyword search and next-lesson lookup against approved data.
- **یادآوری** — enable or disable private homework and exam reminders; opt-out cancels only the current user's queued private jobs for that category, and opt-in rebuilds upcoming approved reminders without duplicating existing jobs.
- **صف بررسی** — available only to approvers for classes where they have permission.
- **مدیریت کلاس / مدیریت مدرسه / پنل مدیر کل** — displayed only for users with the corresponding application role.

Opening an inner panel hides the persistent Reply Keyboard. One Back button closes the current panel and returns to its parent (or restores the main keyboard at the root). School creation asks only for school name and academic year; it then provides a one-time group-add link. Hiding a button is only a UI convenience; each operation independently verifies authorization.

## Telegram group support

- A school can be linked to one central group through Telegram's one-time startgroup link.
- A class can be linked to its own group with /connect <code>; the code is one-time and expires after 30 minutes. The command checks that its issuer is a Telegram group administrator and that the bot itself is an administrator in the target group. A Telegram chat cannot be bound as both a school group and a class group.
- Linked groups support command-based class workflows, including approved homework/exam/report queries, weekly timetable, next-lesson search, submissions and review/cancellation by authorized roles.
- A dedicated inline group dashboard and broadcast/announcement tools are not yet implemented.

Keep Telegram Privacy Mode enabled. ClassMate does not ingest arbitrary group conversations; it handles addressed commands and supported bot interactions only.

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

The current /today-equivalent view shows approved daily reports; it is not yet a complete dashboard combining periods, due homework and exams. Multiple reports may be submitted for one class/date. The next-session resolver uses the next future timetable occurrence (08:00 when a period has no start time); holidays and one-off timetable changes are not inferred. Record revision UI, a web dashboard, attendance/grades and advanced analytics remain follow-up work. FSM state, processed-update leases, records, audit history and reminder jobs are stored in PostgreSQL.
