# Operations runbook

## First school setup

1. Add the owner's numeric Telegram user ID to `BOOTSTRAP_ADMIN_IDS` in Railway Variables on both `classmate` and `classmate-worker`.
2. In a private chat, send `/start` to the bot.
3. Create the school and academic year with `/school_new`.
4. Create the class with `/class_new`.
5. Add school admins, class managers and trusted approvers with the role-management commands documented in the root README.

The bootstrap list is only for the first platform-admin setup. Keep it limited to trusted owners.

## Group onboarding

1. Generate class connection/student invitation codes using `/class_code`.
2. Add the bot to the class Telegram group and promote it only as much as needed to receive commands and send messages.
3. As a group administrator, run `/connect <connection-code>`.
4. Students start the bot privately and run `/join <student-code>`.
5. Configure subjects and timetable slots.

## Routine checks

- Confirm both application deployments are running and PostgreSQL is healthy.
- Review Railway deploy logs for database connection, Telegram polling and worker retry errors.
- Use `/pending` for approval queues, `/homework`, `/exams`, `/today` and `/week` for class queries.
- Keep only one active bot polling process for a given token.

## Recovery

- If Telegram reports a conflict during polling, check for another process or old deployment using the same bot token.
- If the worker reports that the schema is behind, inspect the bot service's pre-deploy migration logs first.
- If the database is unreachable, verify the private variable reference `${{Postgres.DATABASE_URL}}` and that both services are in the same Railway environment.
- If a token is exposed, revoke/rotate it with BotFather and update both Railway application services.

## Data safety

PostgreSQL is the source of truth. Keep the Railway persistent volume attached and arrange periodic database backups before storing real student/class information. The MVP does not yet provide an in-app backup/restore workflow.
