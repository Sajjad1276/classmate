# Operations runbook

## First platform-admin setup

1. Add the owner's numeric Telegram user ID to `BOOTSTRAP_ADMIN_IDS` on the `classmate` bot service.
2. Open a private chat with the configured bot and press **Start**.
3. Confirm that the **پنل مدیر کل** button appears. The application sets the platform-admin role when the Telegram ID matches the configured list.
4. Use the platform panel to create a school and its first academic year.
5. Use **مدیریت مدرسه** to create classes and appoint school/class managers.
6. Use a class dashboard's **مدیریت این کلاس** panel to add trusted approvers, subjects and timetable slots.

The bootstrap list is only for the first platform-admin setup. Keep it limited to trusted owners. Do not send the bot token or admin credentials in chat. The group-add link expires after 24 hours and can only be consumed by a platform admin or an admin of that school. Telegram's group picker enforces the user's ability to add the bot to the selected group.

## Student onboarding

1. The class manager creates codes from **مدیریت این کلاس → ساخت کد اتصال و عضویت**.
2. Give the 7-day student invitation code only to intended members.
3. Students open the bot privately, press Start, choose **عضویت در کلاس**, and enter the code.
4. Students use **ثبت اطلاعات** to submit homework, exams and daily reports.
5. Trusted approvers use **صف بررسی** to approve/reject submissions.

## Group onboarding and supported commands

A central school group is linked using the one-time Telegram startgroup link from the school panel. A class group is linked by a Telegram group administrator running /connect <code> inside that group; the code expires after 30 minutes and is consumed on success. The private UI is button-driven. Group support currently uses addressed commands rather than a dedicated inline dashboard.

In linked groups, supported commands include /homework, /exams, /today, /week, /ask <question>, /subject_add, /timetable_set, /pending and /cancel_record (authorization is checked server-side). The inline group dashboard, broadcast tools and automatic ingestion of arbitrary messages are not implemented. Keep Telegram Privacy Mode enabled.

## Routine checks

- Confirm the bot and worker deployments are running and PostgreSQL is healthy.
- Review Railway deploy logs for database connection, Telegram polling and worker retry errors.
- Keep only one active long-polling process for the production bot token.
- Confirm the deployed bot identity matches the intended ClassMate username before inviting real students.

## Recovery

- If Telegram reports a conflict during polling, check for another process or old deployment using the same bot token.
- If the worker reports that the schema is behind, inspect the bot service's pre-deploy migration logs first.
- If the database is unreachable, verify the private variable reference `${{Postgres.DATABASE_URL}}` and that both services are in the same Railway environment.
- If a token is exposed, revoke/rotate it with BotFather and update both Railway application services.

## Data safety

PostgreSQL is the source of truth. Keep the Railway persistent volume attached and arrange periodic database backups before storing real student/class information. The MVP does not yet provide an in-app backup/restore workflow.


## Persistent conversation state and update idempotency

The production bot stores aiogram FSM state/data in PostgreSQL (`fsm_state_records`), not process memory. A bot restart or Railway redeploy therefore does not erase an in-progress form. Telegram updates are claimed in `processed_updates` with a short processing lease; already processed updates are ignored, while failed or expired leases may be retried. Keep the production PostgreSQL volume and database backups intact.

## Class data removal

Class managers, school administrators and platform administrators can soft-delete a class subject with `/subject_remove <class_id> <subject_id>` and a timetable slot with `/timetable_remove <class_id> <entry_id>` in the bot's private chat. The commands retain the underlying rows and write an audit event. Re-adding a removed subject or timetable slot restores the existing soft-deleted link/slot rather than creating a duplicate. A trusted approver is not a class manager and cannot archive class data. A school administrator or platform administrator can archive a shared subject across the whole school with `/subject_archive <subject_id>` and restore it with `/subject_restore <subject_id>`; restoring returns its previous class links and timetable slots. Class managers cannot bypass a school-wide archive by re-adding the subject.

## Approved-record cancellation

Users who can approve class records (class manager, trusted approver, school administrator or platform administrator) can cancel an approved record using `/cancel_record <class_id> homework|exam|report <record_id>` in private chat, or `/cancel_record homework|exam|report <record_id>` in its connected group. Cancellation changes the record status, records an audit/approval event and cancels queued, retrying, processing and uncertain reminders. The worker re-checks the source record immediately before delivery; a Telegram request that has already started or been accepted cannot be recalled.

## Reminder delivery reconciliation

The worker marks a reminder `uncertain` rather than automatically retrying when a network/server failure or an expired worker lease makes Telegram's delivery result ambiguous. This avoids an automatic duplicate send, but it cannot guarantee exactly-once delivery because Telegram's send API does not provide an idempotency key for these messages.

A platform administrator can list ambiguous jobs with `/reminder_uncertain`, then use `/reminder_resolve <job_id> sent` to mark a job as already delivered, or `/reminder_resolve <job_id> retry` to requeue it. The latter can produce a duplicate if Telegram had accepted the original send. The retry counter resets on this explicit manual requeue.

## Migration rollback policy

Migration `0001_initial` no longer drops the complete schema on downgrade. Migrations `0003_reliability_state` and `0004_daily_report_cancelled_at` refuse automatic downgrade because doing so would discard live conversation state, update history or cancellation history. Migration `0005_daily_report_multi_submit` refuses downgrade while duplicate class/date reports exist; once the data is consolidated, it can restore the old unique constraint. For recovery, take/restore a verified database backup or write a reviewed forward migration; do not issue `alembic downgrade base` against production.


## Telegram polling singleton

The bot holds a PostgreSQL session-level advisory lock for the lifetime of long polling. During Railway rolling deploys, a new container waits for the old poller to release its lock instead of issuing a competing getUpdates call. Keep the lock-owning PostgreSQL connection stable; if it is lost, PostgreSQL releases the lock.

## Daily reports

More than one daily report is allowed for the same class/date. Reports are individually reviewed and audited; do not assume one report row per day when writing exports or summaries.