FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends i2c-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY led_api.py led_dashboard.html ./

ENV DATA_DIR=/app/data
VOLUME ["/app/data"]

EXPOSE 50001

CMD ["python3", "led_api.py"]
