FROM python:3.12-slim
ARG CREATOR_RELEASE_VERSION=development
ARG CREATOR_RELEASE_COMMIT=unknown
LABEL org.opencontainers.image.version=$CREATOR_RELEASE_VERSION \
      org.opencontainers.image.revision=$CREATOR_RELEASE_COMMIT
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
RUN useradd --create-home app && chown -R app:app /app
USER app
CMD ["python", "-m", "bot.main"]
