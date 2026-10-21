# Pinned by digest (the multi-arch index, so it works on amd64 and arm64). Dependabot's
# docker ecosystem proposes new digests for this tag; see docs/build-plan.md.
FROM python:3.14-slim@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a

WORKDIR /srv

# The lock (hashes for every package, transitives included), not requirements.txt.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY alembic.ini docker-entrypoint.sh ./
COPY migrations ./migrations
COPY app ./app

# Run as an unprivileged user: a flaw in the app or a dependency should not
# hand an attacker root inside the container.
RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

CMD ["./docker-entrypoint.sh"]
