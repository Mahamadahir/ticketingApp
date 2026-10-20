FROM python:3.15-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini docker-entrypoint.sh ./
COPY migrations ./migrations
COPY app ./app

# Run as an unprivileged user: a flaw in the app or a dependency should not
# hand an attacker root inside the container.
RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

CMD ["./docker-entrypoint.sh"]
