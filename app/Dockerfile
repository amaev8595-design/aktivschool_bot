FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/main.py app/db.py app/classes.py app/schema.sql ./
COPY app/data ./data

CMD ["python", "main.py"]

