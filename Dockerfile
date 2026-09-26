FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
RUN useradd --create-home --uid 10001 botuser && mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser
ENV DB_PATH=/app/data/oleg_bot.sqlite3
CMD ["python", "bot.py"]
