FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt yt-dlp streamlink
RUN python -m playwright install --with-deps chromium
COPY supervisor supervisor
COPY scripts scripts
COPY benchmarks benchmarks
CMD ["uvicorn", "supervisor.api:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log"]
