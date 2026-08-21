# syntax=docker/dockerfile:1
# Hardened, multi-stage Go container for the Bluestaq App Store container template.
# Shipped as a scaffold template by the deploy-recipes skill. Copy to your repository root
# as `Dockerfile` and adjust only the marked lines. Replace every <pinned-digest> with the
# real image digest at build time. A static CGO-free binary on a distroless base ships no
# shell, no package manager, and a numeric non-root user by construction.

# ---- build: fetch modules, then build a static, stripped binary ----
FROM golang:1.23@sha256:<pinned-digest> AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
# Adjust ./cmd/server to your main package path.
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /app ./cmd/server

# ---- ship: one static binary on distroless nonroot ----
FROM gcr.io/distroless/static-debian13:nonroot@sha256:<pinned-digest>
COPY --from=build /app /app
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/app"]
