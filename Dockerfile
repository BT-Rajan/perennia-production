# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /srv

RUN groupadd -r perennia && useradd -r -g perennia perennia

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY public ./public
COPY scripts ./scripts

# data/ is created at runtime and should normally be a mounted volume
# so it survives container restarts/rebuilds.
RUN mkdir -p /srv/data && chown -R perennia:perennia /srv

USER perennia

EXPOSE 8000

# .env is not baked into the image — pass real environment variables
# at `docker run` / compose time (see README).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
