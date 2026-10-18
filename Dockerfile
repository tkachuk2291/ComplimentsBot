FROM python:3.13.16-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_PATH=/app/data/state.json

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-deps -r requirements.lock && pip check

RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown bot:bot /app/data
COPY --chown=bot:bot *.py ./

USER bot
VOLUME ["/app/data"]

CMD ["python", "main.py"]
