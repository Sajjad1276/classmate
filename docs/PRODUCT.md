# ClassMate product specification

## Product intent

ClassMate is a structured, auditable school-memory system delivered through Telegram. It stores class reports, homework, exams, timetables and reminders with trusted human approval. The MVP does not use AI/LLM interpretation: queries are deterministic and unapproved submissions are never presented as confirmed facts.

## Organizational hierarchy

School → Academic Year → Class → optional class Telegram group. A school may also have one central Telegram group. Roles are scoped to school/class membership, not inferred from Telegram group administrator status.

## Roles

- Platform admin: bootstraps and manages schools.
- School admin: manages academic years, classes and school-level membership.
- Class manager: manages a class, group binding, subjects, timetable and trusted approvers.
- Trusted approver: reviews submissions for assigned classes; cannot manage class data.
- Student: joins classes, submits records and reads approved class information.

The submitter cannot approve their own record. A role label in the UI never replaces server-side authorization.

## MVP modules

1. School/class creation, invitations and group binding.
2. School-level central group and optional class-specific groups.
3. School subject catalogue mapped to classes; soft-delete and privileged archive/restore.
4. Multiple daily reports per class/date, homework and exams.
5. PENDING → APPROVED/REJECTED workflow, with CANCELLED for post-approval cancellation.
6. Approved-only list/query paths, approval events and audit history.
7. Timetable entries with weekday, time and optional validity range; deterministic next-session resolution.
8. Durable group/private reminder jobs, per-class private reminder preferences and operator reconciliation of uncertain sends.
9. PostgreSQL-backed FSM state, Telegram update idempotency and a PostgreSQL polling singleton lock for rolling deployments.

## Record lifecycle

Implemented states are PENDING, APPROVED, REJECTED and CANCELLED. A record is created as pending; approval/rejection is audited. Only approved records appear in ordinary class queries. An authorized class/school approver may cancel an approved record; cancellation retains the record, timestamp, audit/approval event and cancels its associated reminders. A user-facing revision-request workflow and expiry lifecycle are not implemented yet and must not be represented as live states.

A class may have multiple daily reports for the same date. Reports are individually reviewed; read paths must return all qualifying approved reports rather than assuming a unique class/date row.

## Next-lesson semantics

The resolver reads the class timetable for a subject, respects weekday/time and optional validity boundaries, and selects the next future occurrence in the school's timezone. If a period has no start time, 08:00 is used as a fallback. Homework may be attached to that lesson or due on its local date. Holiday calendars and exceptional one-off timetable changes are not inferred; a manager must update the timetable.

## Reminder semantics

- Exams: default reminders 24 hours and 1 hour before.
- Homework: default reminder at 18:00 on the day before its due date.
- A class group receives group reminders when it is connected.
- Private reminders are queued for eligible members who started the bot and have that reminder category enabled.
- Database deduplication avoids duplicate job creation. Workers use leases and bounded retries. Network/server failures or expired leases with an ambiguous delivery outcome are marked uncertain for manual reconciliation, not automatically retried.
- Delivery is not exactly-once: a Telegram request may be accepted before a process loses the response. Manual retry can produce a duplicate. If the source record is cancelled before the send begins, the job is cancelled and the worker re-checks source status before sending; an in-flight Telegram request cannot be recalled.

## Group experience and limitations

Group binding and the inline class-group dashboard are implemented. The dashboard exposes approved homework/exams, the currently valid timetable, a bounded summary for today and submission entry points; approver-only review is authorized again at callback execution. Broadcast/announcement tooling and arbitrary-message ingestion are not implemented. Keep Telegram Privacy Mode enabled.

A web dashboard, revision/expiry workflows, attendance, grades, payments, SIS integrations, holiday calendars and advanced analytics remain out of scope for the current MVP.