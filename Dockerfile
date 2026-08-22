# syntax=docker/dockerfile:1
# Pree container image for the Bluestaq App Store python template.
#
# Three stages, in this order for a reason:
#   build : resolves the hash-locked requirements into an isolated venv, so pip never reaches
#           the shipped filesystem. The image-policy scan judges what ships, not what ran.
#   prep  : assembles the runtime filesystem, creates the numeric user, and sweeps every
#           setuid and setgid bit LAST. Nothing may follow the sweep, because a later
#           instruction can re-introduce the class the sweep just cleared.
#   ship  : FROM scratch with a single COPY of the prepared filesystem. The scan reads layer
#           history, so one clean layer is the only construction with no history to flag.
#
# The base digest is pinned to the resolved python:3.12-slim multi-architecture index.

ARG BASE_DIGEST=sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

# ---- build: install from the hash-locked requirements into an isolated venv ----
FROM python:3.12-slim@${BASE_DIGEST} AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt ./
RUN pip install --require-hashes --no-deps -r requirements.txt

# ---- prep: assemble the runtime filesystem, then sweep suid and sgid bits last ----
FROM python:3.12-slim@${BASE_DIGEST} AS prep
# Tolerated step, on its own RUN. Base-package upgrades are best-effort against a mirror that
# may lag; chaining a mandatory step behind them would let a tolerated miss swallow it.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY src ./src
# The package manager must not reach the shipped filesystem: it carries CVEs the scan stops on.
RUN rm -rf /opt/venv/lib/python3.12/site-packages/pip* \
           /opt/venv/bin/pip* \
           /usr/local/lib/python3.12/site-packages/pip* \
           /usr/local/lib/python3.12/site-packages/setuptools* \
           /usr/local/lib/python3.12/ensurepip \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
 && rm -rf /var/lib/apt /var/cache/apt /etc/apt /usr/bin/apt /usr/bin/apt-* \
           /usr/bin/dpkg /usr/bin/dpkg-* /usr/sbin/dpkg-* /var/lib/dpkg/info \
 && useradd --uid 10001 --user-group --system --no-create-home \
            --shell /usr/sbin/nologin appuser \
 && chown -R 10001:10001 /app
# LAST mutation in this stage. Files and directories alike; the policy tests the /6000 mask
# only, so the sticky bit is left alone. Nothing may be added below this line.
RUN find / -xdev -perm /6000 \( -type f -o -type d \) -exec chmod a-s {} +

# ---- ship: one flattened layer, no history for the policy scan to read ----
FROM scratch
COPY --from=prep / /
# Metadata is re-declared because a scratch stage inherits none of it. PATH is explicit so the
# venv binaries resolve without a shell profile.
ENV PATH="/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 10001:10001
EXPOSE 8080
# The storage proof, not a liveness path: it performs a real write and races its own hard
# timeout, so the check either confirms the mount or names the errno.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["/opt/venv/bin/python", "-c", "import os,sys,urllib.request;p=os.environ.get('PORT','8080');sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz/storage',timeout=3).status==200 else 1)"]
# No ENV PORT and no ENV PREE_DATA_DIR anywhere in this file. The platform injects both and an
# image-level default would beat the code fallback chain. exec so SIGTERM reaches gunicorn.
#
# --forwarded-allow-ips is pinned to 255.255.255.255, the limited broadcast address, which can
# never be the source of a TCP connection. gunicorn validates the value as an IP or network, so
# a non-IP sentinel refuses to start; an empty value also works but reads as "unset". uvicorn
# installs ProxyHeadersMiddleware unconditionally and gunicorn's default trust list is
# os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1,::1"), so as shipped the middleware rewrote
# scope["client"] from a caller-supplied X-Forwarded-For before the app ran. Both rate-limit
# tiers key on that value: measured against the running server, 400 requests with a rotating
# header were all admitted where 240 admitted and 160 refused with a fixed one, and 60 of 60
# writes to /v1/assess were accepted where 20 should have been. A sidecar ingress forwarding
# over loopback is trusted by that default, and FORWARDED_ALLOW_IPS=* is a common platform
# value, so the environment alone could turn the limiter off. An explicit flag beats the
# environment default, so the trust list cannot be widened from outside the image.
#
# If operations later needs real client addresses, the correct change is to set this to the
# ingress address specifically. Do not set it to "*": that trusts every peer and hands the
# limiter's key space to the caller.
CMD ["sh","-c","exec gunicorn 'pree.main:build()' --pythonpath /app/src -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8080} --workers 2 --timeout 60 --forwarded-allow-ips=255.255.255.255 --access-logfile - --error-logfile -"]
