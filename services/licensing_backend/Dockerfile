FROM python:3.12-slim
ARG CREATOR_RELEASE_VERSION=development
ARG CREATOR_RELEASE_COMMIT=unknown
LABEL org.opencontainers.image.version=$CREATOR_RELEASE_VERSION \
      org.opencontainers.image.revision=$CREATOR_RELEASE_COMMIT
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
