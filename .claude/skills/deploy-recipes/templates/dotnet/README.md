# .NET (ASP.NET Core) container scaffold templates (App Store, quality-gated)

Copy-ready files for an ASP.NET Core server app targeting the Bluestaq App Store container
template. They materialise the `deploy-recipes` .NET recipe as real files you copy, so the App
Store contract holds from the first commit instead of being prose you must remember.

## The files

- **`Dockerfile`** - hardened, multi-stage image: `dotnet publish` then the app on the
  `aspnet:8.0-jammy-chiseled` base, which already defaults to port 8080 and ships the numeric
  non-root `app` user. Copy to the repository root, replace every `<pinned-digest>`, and change
  `YourApp.dll` to your assembly.
- **`sonar-project.properties`** - Code Quality gate scoping. Coverage is OpenCover, read via
  `sonar.cs.opencover.reportsPaths` (Sonar's C# analyser does not consume Cobertura). Commit at
  the repository root.

## How to use

1. Copy the `Dockerfile` and `sonar-project.properties` to the repository root. Replace every
   `<pinned-digest>`, `CHANGE_ME_project_key`, and `YourApp.dll`.
2. Honour the runtime contract. The chiselled base defaults to 8080, so the contract holds
   without `ENV PORT`. To also honour a platform-injected `PORT`:
   `builder.WebHost.UseUrls($"http://0.0.0.0:{Environment.GetEnvironmentVariable("PORT") ?? "8080"}")`.
   Map `/` and `/health` to 200 unauthenticated. Do not override the chiselled
   `ASPNETCORE_HTTP_PORTS=8080` with `ENV PORT`.
3. Emit coverage as OpenCover, at 80% or more:
   `dotnet test --collect:"XPlat Code Coverage" -p:CoverletOutputFormat=opencover`. The default
   Coverlet Cobertura output is ignored by Sonar's C# analyser.
4. Scan dependencies: `dotnet list package --vulnerable --include-transitive`. Address every
   High or Critical.
5. For the upload zip and the pre-upload pipeline simulation, reuse the shared, stack-agnostic
   `scripts/package-appstore.sh` and `scripts/simulate-pipeline.sh` from `templates/node/`,
   changing only the test command to the coverage command above. Run the simulation green
   before you upload.

## Pitfalls

- The default Coverlet Cobertura output is ignored by Sonar's C# analyser; use OpenCover.
- `app.UseHttpsRedirection()` 307-redirects `/`, breaking the unauthenticated-200 health
  contract; remove it for the in-cluster HTTP listener.
- Do not override the chiselled `ASPNETCORE_HTTP_PORTS=8080` with `ENV PORT`.
