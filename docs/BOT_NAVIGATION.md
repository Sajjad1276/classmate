# ClassMate bot navigation

This document is the reference map for the private and class-group button experience.

## 1. Private-chat home

The persistent reply keyboard contains only top-level destinations:

- **🏫 Classes**: my classes and joining a class.
- **📚 Academic**: homework, exams, weekly timetable, today's reports, and search.
- **➕ Create**: daily report, homework, or exam submission.
- **🔔 Reminders**: per-class homework and exam reminder preferences.
- **🕓 Review queue**: shown only to users who can approve at least one class.
- **⚙️ Management**: shown only to class managers, school administrators, and platform administrators.
- **ℹ️ Help**: usage guidance.

The management section is role-aware. It exposes class management to class managers and school/platform administrators, school management to school administrators, and the platform panel only to platform administrators. Every callback repeats the relevant server-side permission check.

## 2. Section hierarchy

### Classes

🏫 Classes → My classes → class panel. The classes section also contains Join class. Joining opens a code-entry state. During input, the persistent keyboard is replaced by a single **⬅️ بازگشت** reply button. Invalid or expired codes are reported in a new message; the original code prompt stays visible so the user can retry. A successful join restores the role-aware home keyboard.

### Academic

📚 Academic → homework / exams / weekly timetable / today / search → class selector when the user has more than one eligible class → class-scoped result.

A class panel provides its own academic shortcuts, reminders, review queue (when authorized), approved-record cancellation (when authorized), and class management (when authorized). Back navigation follows the hierarchy: result → class panel → class list → Classes section.

### Create

➕ Create → daily report / homework / exam → class (when needed) → subject (for homework/exam) → guided fields → pending submission.

Private record wizards use the same single-button reply keyboard for back navigation. Invalid field values produce a separate error message; they do not replace the current field prompt. Returning clears the unsubmitted wizard state. Submission confirmation states that the record is pending approval.

### Reminders

🔔 Reminders → eligible class selector → per-class homework and exam toggles. The toggle callback updates the preference and scheduled jobs, then re-renders the same class settings page.

### Review

🕓 Review queue → only classes where the user has approval permission → pending records → approve/reject. The callback rechecks permission and record status on the server; a visible button is never treated as authorization.

### Management

⚙️ Management →
- Class management: class selector → class management panel → invitation/group codes, trusted approvers, subjects, and timetable configuration.
- School management: school selector → school panel → central group connection, academic years, classes, class managers, and class list.
- Platform administration: create schools, assign school administrators, and open school/class administration.

Input forms use the single reply-keyboard back control. Returning from a form restores its parent panel instead of silently losing the management context.

## 3. Navigation and input rules

1. The private home is a persistent ReplyKeyboard; section and detail pages use InlineKeyboard buttons.
2. Inline back buttons belong to navigation pages. During a private input state, back is a reply-keyboard button, not a glass/inline button.
3. One active input state owns one prompt message. Invalid input creates a separate error message and preserves that prompt.
4. A valid multi-step form may advance the text of its prompt, but it does not replace the back control.
5. Pressing back clears the active form/wizard and restores the correct parent page plus the role-aware home keyboard.
6. Successful class joining returns directly to the home keyboard. Other completed management forms retain a route back to their parent panel.
7. Destructive operations require a confirmation step. Permissions are checked again when each callback is executed.
8. Group chats do not receive a private reply keyboard. Their class menu remains inline and authorization stays class-scoped.

## 4. Current interaction vocabulary

- Home section: persistent reply-keyboard button.
- Menu / selector / detail: inline buttons with a parent back action.
- Input state: one reply-keyboard ⬅️ بازگشت control in private chat.
- Validation error: a new message; the form prompt remains available.
- Success: a result message or updated panel, with the next valid navigation path.
