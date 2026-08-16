# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: builder – install Python deps with uv into a virtualenv
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Copy only dependency definition first (better layer caching)
COPY requirements.txt .

# Create venv and install dependencies (no project source needed)
RUN uv venv /app/.venv && \
    . /app/.venv/bin/activate && \
    uv pip install --no-cache -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime – minimal image with ffmpeg + app
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Install only what is required at runtime
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Copy the virtualenv from builder
COPY --from=builder /app/.venv /app/.venv

# Make sure we use the venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    TMP_DIR=/app/tmp

# Application code (only the bot itself)
COPY bot.py .

# Directories for persistent data and temporary files
RUN mkdir -p /app/data /app/tmp && \
    chmod 777 /app/data /app/tmp

# Do NOT copy .env, data/, secrets, etc. – they must come from runtime env/volumes

CMD ["python", "bot.py"]
