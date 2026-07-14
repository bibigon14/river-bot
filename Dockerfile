FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PYTHONUNBUFFERED=1
# Overridden to /data in Kubernetes (mounted PVC); defaults to /app locally.
ENV DATA_DIR=/app

CMD ["python", "bot.py"]
