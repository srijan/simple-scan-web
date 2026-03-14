FROM python:3.12-slim

# Install SANE, airscan backend, and image tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    sane-utils \
    libsane-common \
    sane-airscan \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Temp dir and consume dir (override consume via env + volume mount)
RUN mkdir -p /tmp/simple-scan-web /consume

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
