FROM python:3.12-slim

# Faster, cleaner container behavior: no .pyc files, unbuffered logs so
# `docker logs` shows output immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application (see .dockerignore — .env, *.db, .venv, .git are excluded).
COPY . .

# Run as a non-root user. Pre-create /data and hand both it and /app to the
# user so a fresh named volume mounted at /data is writable for the SQLite db.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser /app /data
USER appuser

CMD ["python", "bot.py"]
