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

## Group onboarding status

Private-chat navigation is button-driven. Group activities and the group-specific button panel are the next implementation phase; this runbook does not treat old slash-command group handlers as a completed group interface. Keep Telegram Privacy Mode enabled. Do not rely on arbitrary group-message reading.

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
