FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini docker-entrypoint.sh ./
COPY migrations ./migrations
COPY app ./app

CMD ["./docker-entrypoint.sh"]
