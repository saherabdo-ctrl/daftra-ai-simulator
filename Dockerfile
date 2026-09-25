# ============================================================
#  Daftra AI Simulator — production image
#
#  One image, two processes (see docker-compose.yml):
#    web   → python server.py          (FastAPI on :8000)
#    agent → python agent.py start     (LiveKit voice agent, outbound only)
# ============================================================

# ---- build: compile wheels with the toolchain, keep it out of the runtime ----
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# ---- runtime ----
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RECORDINGS_PATH=/recordings \
    PORT=8000

# ffmpeg/ffprobe: the agent mixes and probes call recordings.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version >/dev/null && ffprobe -version >/dev/null

COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/* && rm -rf /wheels

RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --chown=app:app . .

# Runtime state lives in ONE volume at /app/var; the paths the code writes to
# under data/ become symlinks into it. Mounting a volume over all of data/
# would freeze data/daftra_knowledge.md at whatever the first deploy shipped
# (Docker seeds a named volume only once), so only the writable paths move:
#   data/results/ data/scenarios/ data/calls/   → /app/var/…
#   data/users.json                              → /app/var/users.json
#   data/dialect_corpus.json (learned from calls) → /app/var/…, seeded from
#                                                  the repo copy on first start
RUN mkdir -p /app/seed /app/var /recordings \
    && mv data/dialect_corpus.json /app/seed/dialect_corpus.json \
    && rm -rf data/results data/scenarios data/calls data/users.json \
    && ln -s /app/var/results data/results \
    && ln -s /app/var/scenarios data/scenarios \
    && ln -s /app/var/calls data/calls \
    && ln -s /app/var/users.json data/users.json \
    && ln -s /app/var/dialect_corpus.json data/dialect_corpus.json \
    && chmod +x docker/entrypoint.sh \
    && chown -R app:app /app/seed /app/var /recordings \
    && chown app:app /app
# (WORKDIR creates /app itself as root; COPY --chown only covers its contents.)

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["python", "server.py"]
