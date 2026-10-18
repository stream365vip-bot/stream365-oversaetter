FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg/ffprobe: trækker indlejrede engelske tekstspor ud af videoen (TRANSLATE_MODE=direct).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY oversaetter ./oversaetter

USER 1000:1000
CMD ["python", "-m", "oversaetter"]
