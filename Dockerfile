FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    sane-utils \
    libsane-common \
    sane-airscan \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/simple-scan-web /consume

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

RUN useradd -r -s /usr/sbin/nologin app && chown -R app:app /app /consume /tmp/simple-scan-web
USER app

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
