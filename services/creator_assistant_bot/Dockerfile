FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
RUN useradd --create-home app && chown -R app:app /app
USER app
CMD ["python", "-m", "bot.main"]
