# syntax=docker/dockerfile:1
# Hardened, multi-stage Rust container for the Bluestaq App Store container template.
# Shipped as a scaffold template by the deploy-recipes skill. Copy to your repository root
# as `Dockerfile` and adjust only the marked lines. Replace every <pinned-digest> with the
# real image digest at build time. The cc (glibc) distroless nonroot runtime ships no shell,
# no package manager, and a numeric non-root user.

# ---- build: cache dependencies, then build the release binary locked to the lockfile ----
FROM rust:1.83-slim@sha256:<pinned-digest> AS build
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
# Prime the dependency cache with a stub, then build the real sources.
RUN mkdir src && echo "fn main(){}" > src/main.rs && cargo build --release && rm -rf src
COPY . .
RUN cargo build --release --locked

# ---- ship: the binary on distroless cc nonroot (glibc). Adjust `server` to your bin name. ----
FROM gcr.io/distroless/cc-debian13:nonroot@sha256:<pinned-digest>
COPY --from=build /app/target/release/server /usr/local/bin/server
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/server"]
