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

EXPOSE 8001 443

# .env is not baked into the image — pass real environment variables
# at `docker run` / compose time (see README). If SSL_CERTFILE/SSL_KEYFILE
# are set and both files are mounted into the container, run_server.py
# binds HTTPS on 443 instead of plain HTTP on 8001.
CMD ["python", "scripts/run_server.py"]
