FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY scripts/requirements.txt .
RUN pip install -r requirements.txt
COPY scripts/ .

# Files are written with the uid/gid the container runs as: match your music library's owner
USER 1000:1000
CMD ["python", "bot.py"]
