# syntax=docker/dockerfile:1
# Hardened, flattened Node container for the Bluestaq App Store container template.
# Shipped as a scaffold template by the deploy-recipes skill. Copy to your repository
# root as `Dockerfile` and adjust only the marked lines. The construction is load-bearing:
# it is the fix for the image-policy-scan failures catalogued in appstore-gate-compliance
# (base-image suid bits, the npm toolchain's CVEs, and layer history a later chmod cannot
# clear). Do not "simplify" it back to a conventional two-stage image; that fails the scan.

# ---- build: install from the lockfile; npm lives here only ----
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY . .

# ---- prep: assemble one clean filesystem; the suid sweep is the LAST mutation ----
FROM node:22-alpine AS prep
# Patch base packages, best-effort, in its own layer so it can never mask the fail-closed steps.
RUN apk upgrade --no-cache || true
WORKDIR /app
# The application and its production dependencies from the build stage.
COPY --from=build /app /app
# A non-root numeric user. busybox adduser sets the setgid bit on the home it creates,
# so the suid sweep below must run AFTER this, or it re-introduces the class the scan flags.
RUN addgroup -g 10001 app \
 && adduser -D -u 10001 -G app app \
 && chown -R 10001:10001 /app
# Remove the package-manager toolchain the runtime never uses (the build stage kept it).
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx \
           /usr/local/bin/corepack /opt/yarn* 2>/dev/null || true
# THE LAST MUTATION: strip every suid/sgid bit from files AND directories, fail closed.
# Nothing may follow this line, or a later instruction re-introduces the class.
RUN find / -xdev -perm /6000 \( -type f -o -type d \) -exec chmod a-s {} +

# ---- ship: one clean layer, so the scanner reads no history ----
FROM scratch
COPY --from=prep / /
# Image config does not travel through COPY, so re-declare all of it, including PATH.
# Never add `ENV PORT=` or `ENV DATA_DIR=`: the platform injects those and the code defaults them.
ENV NODE_ENV=production PATH=/usr/local/bin:/usr/bin:/bin
WORKDIR /app
USER 10001:10001
EXPOSE 8080
# The probe races a hard 2s timeout, strictly shorter than the platform's probe. Your
# /healthz must prove storage with a real WRITE and return the errno in its 503 body.
HEALTHCHECK --interval=15s --timeout=2s --start-period=5s --retries=3 \
  CMD ["node","-e","fetch('http://127.0.0.1:'+(process.env.PORT||8080)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node","server.js"]
