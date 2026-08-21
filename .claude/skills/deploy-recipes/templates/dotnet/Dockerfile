# syntax=docker/dockerfile:1
# Hardened, multi-stage .NET (ASP.NET Core) container for the Bluestaq App Store container
# template. Shipped as a scaffold template by the deploy-recipes skill. Copy to your
# repository root as `Dockerfile` and adjust only the marked lines (YourApp.dll to your
# assembly). Replace every <pinned-digest> with the real image digest at build time. The
# jammy-chiselled runtime already defaults to port 8080 and ships the numeric non-root `app`
# user, so the contract holds without ENV PORT.

# ---- build: restore then publish Release ----
FROM mcr.microsoft.com/dotnet/sdk:8.0@sha256:<pinned-digest> AS build
WORKDIR /src
COPY *.csproj ./
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app --no-restore

# ---- ship: the published app on the chiselled aspnet nonroot base ----
FROM mcr.microsoft.com/dotnet/aspnet:8.0-jammy-chiseled@sha256:<pinned-digest>
WORKDIR /app
COPY --from=build /app .
EXPOSE 8080
ENTRYPOINT ["dotnet","YourApp.dll"]
