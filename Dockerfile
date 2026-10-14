# Interview Assistant for a shared test deployment (SPEC-deploy; README "Deploy").
# ffmpeg is needed for Audio file mode and is not in Render's native Python runtime, hence Docker.
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps out .env, sessions/, recordings and brand source files
COPY . .
RUN useradd --create-home app && mkdir -p sessions audit_logs && chown -R app:app sessions audit_logs packs
USER app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
# One process only: live interviews are held in memory. Shell form so $PORT (set by Render) expands; exec so
# uvicorn gets the stop signal and pushes the last changes to Supabase before the container stops.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips "*"
