# syntax=docker/dockerfile:1
# Hardened, multi-stage Python container for the Bluestaq App Store container template.
# Shipped as a scaffold template by the deploy-recipes skill. Copy to your repository root
# as `Dockerfile` and adjust only the marked lines. Replace every <pinned-digest> with the
# real image digest at build time (the quality gate expects a pinned base). The venv is built
# in the first stage so the package manager never reaches the final image.

# ---- build: install from the hash-locked requirements into an isolated venv ----
FROM python:3.12-slim@sha256:<pinned-digest> AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --require-hashes -r requirements.txt

# ---- ship: copy the venv and the app; strip suid bits; run as a numeric non-root user ----
FROM python:3.12-slim@sha256:<pinned-digest>
# Strip every suid/sgid bit (the image-policy scan stops on suid_or_guid_set), then add a
# numeric non-root user. Best-effort strip, then the fail-closed user creation.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true && \
    useradd -u 10001 -r -s /usr/sbin/nologin appuser
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chown=10001:10001 . .
USER 10001:10001
EXPOSE 8080
# Bind 0.0.0.0 and read PORT (default 8080). `exec` so SIGTERM reaches gunicorn and shutdown
# does not hang. For Flask (WSGI) drop the uvicorn worker class.
CMD ["sh","-c","exec gunicorn app:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8080}"]
