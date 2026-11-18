# Railway deployment preparation (not deployed)

The repository is prepared for Railway's Dockerfile builder. No token or database credential is committed.

## Services to create later

### PostgreSQL
Create a managed PostgreSQL service in the same Railway project.

### ClassMate Bot
- Source: Sajjad1276/classmate
- Branch: main after the feature branch is reviewed/merged
- Builder: Dockerfile
- Start command: python -m classmate.bot.main
- Variables:
  - BOT_TOKEN: set privately in Railway Variables
  - DATABASE_URL: reference the PostgreSQL service's DATABASE_URL
  - BOOTSTRAP_ADMIN_IDS: comma-separated Telegram numeric IDs of initial platform admins
  - DEFAULT_TIMEZONE: Asia/Tehran
  - LOG_LEVEL: INFO

### ClassMate Worker
- Same repository and branch
- Same Dockerfile
- Start command: python -m classmate.worker.main
- Same environment variables as Bot

## Migration procedure

Run alembic upgrade head once after PostgreSQL is provisioned and before starting both application services. Use a controlled one-off deployment/job or a single service's pre-deploy command. Avoid running the same migration concurrently from both bot and worker processes.

## Local validation

1. Copy .env.example to .env.
2. Set BOT_TOKEN.
3. Run docker compose up --build.
4. On first boot, migrations run before bot and worker.
5. Send /start to the bot. Add your numeric Telegram ID to BOOTSTRAP_ADMIN_IDS before starting if you need to create the first school.

The Docker Compose migration dependency is intended for local development. Railway service startup commands are configured separately.
