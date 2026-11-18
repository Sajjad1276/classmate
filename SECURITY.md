# Security policy

## Reporting a vulnerability

Do not publish bot tokens, database connection strings, student data, or exploit details in public issues. Report security concerns privately to the repository owner through GitHub's private vulnerability reporting feature when enabled.

## Credential handling

- Keep `.env` and production secrets out of Git.
- Store Telegram credentials and database secrets only in Railway's sealed Variables.
- Use the minimum required Telegram group permissions.
- Rotate any credential that may have been exposed.

## Student information

ClassMate may store school, class, membership, homework, exam and report information. Use it only with appropriate school/class authorization and do not upload real student data to test environments.
