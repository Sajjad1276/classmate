# ClassMate product specification

## Product intent

ClassMate is a structured, auditable school-memory system delivered through Telegram. It stores classroom reports, homework, exams, schedules and reminders. It does not use AI in the MVP: queries are deterministic, and unverified submissions are never presented as confirmed facts.

## Organizational hierarchy

School -> Academic Year -> Class -> Telegram Group.

Users may belong to many classes and schools. Roles are scoped to the class or school membership, not inferred from Telegram group administrator status.

## Roles

- Platform admin: bootstraps and manages schools.
- School admin: manages academic years, classes and school-level membership.
- Class manager: manages a class, its group binding, subjects and trusted approvers.
- Trusted approver: reviews submissions for one class.
- Student: submits and reads class information.

The submitter cannot approve their own record.

## MVP modules

1. School and class onboarding.
2. Group binding with a one-time code that expires after 30 minutes; private membership with a reusable invitation code that expires after 7 days.
3. Subject catalogue per school, mapped to classes.
4. Daily reports, homework and exams.
5. Pending -> approved/rejected workflow with actor and timestamps.
6. Approved-only list/query endpoints in Telegram.
7. Timetable data model, manager-controlled slot editing and weekly display.
8. Durable group/private reminder jobs.
9. Audit log and tenant-scoped access checks.\n10. School-admin and class-manager assignment flows.\n11. Per-class private reminder preferences.

## Record lifecycle

States: PENDING, APPROVED, REJECTED, REVISION_REQUESTED, CANCELLED and EXPIRED.

Only approved records appear in the normal class lists and deterministic answers. Updates must become new auditable events; destructive silent overwrites are not supported in the MVP.

## Reminder semantics

- Exams: default reminders 24 hours and 1 hour before.
- Homework: default reminder at 18:00 on the day before its due date.
- Group target is used when the class has a connected Telegram group.
- Private reminders are queued only for eligible class members who previously started the bot.
- Telegram delivery is at-least-once: the database prevents duplicate job creation and workers lease rows, but an external send can succeed immediately before a process crash. That failure window cannot be made exactly-once with Telegram's sendMessage API.

## Deliberate MVP exclusions

AI/LLM interpretation, web dashboard, grades, attendance, payments, school SIS integrations and advanced analytics.
