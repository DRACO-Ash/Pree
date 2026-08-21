# syntax=docker/dockerfile:1
# Hardened, multi-stage Java (Spring Boot) container for the Bluestaq App Store container
# template. Shipped as a scaffold template by the deploy-recipes skill. Copy to your
# repository root as `Dockerfile` and adjust only the marked lines. Replace every
# <pinned-digest> with the real image digest at build time. The distroless nonroot runtime
# ships no shell, no package manager, and a numeric non-root user by construction.

# ---- build: go offline first so the layer caches, then package ----
FROM maven:3.9-eclipse-temurin-21@sha256:<pinned-digest> AS build
WORKDIR /app
COPY pom.xml .
RUN mvn -B -e dependency:go-offline
COPY src ./src
RUN mvn -B -DskipTests clean package

# ---- ship: the jar on a distroless java nonroot base ----
FROM gcr.io/distroless/java21-debian13:nonroot@sha256:<pinned-digest>
WORKDIR /app
USER 65532:65532
COPY --from=build --chown=65532:65532 /app/target/*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java","-jar","app.jar"]
