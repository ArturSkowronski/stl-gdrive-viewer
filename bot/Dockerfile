FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py drive_writer.py ./

# Shared with the TBA container so the bot can read downloaded files
# directly off the filesystem instead of re-downloading from HTTPS.
RUN mkdir -p /var/lib/telegram-bot-api /tmp/stl-bot
ENV BOT_WORK_DIR=/tmp/stl-bot

CMD ["python", "-u", "worker.py"]
