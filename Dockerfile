FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --upgrade pip && pip install .

# Railway services use separate start commands:
# Bot:    python -m classmate.bot.main
# Worker: python -m classmate.worker.main
CMD ["python", "-m", "classmate.bot.main"]
