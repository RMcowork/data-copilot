// Railway Infrastructure as Code: the one description of the Railway project
// (architecture.md, "Hosting"). Railway does not read this file at deploy time:
// `railway config plan` shows the drift between it and the project, and
// `railway config apply` applies it. Variables are unmanaged on purpose, so
// secrets live only in the Variables tab (architecture.md, "Secrets"). The npm
// package next to this file exists only so the CLI can evaluate it.
import { defineRailway, github, postgres, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const dataCopilot = github("RMcowork/data-copilot");

  const Postgres = postgres("Postgres", { region: "iad" });
  Postgres.networking = { privateNetworkEndpoint: "postgres" };
  const postgresVolume = volume("postgres-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "iad", sizeMB: 500 });

  // The app: UI and API in one service, built by Railpack from
  // requirements.txt and .python-version (no Dockerfile).
  const web = service("web", {
    source: dataCopilot,
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    deploy: {
      startCommand: "uvicorn app.main:app --host 0.0.0.0 --port $PORT",
      healthcheckPath: "/health",
      healthcheckTimeout: 120,
      // ON_FAILURE is Railway's default; declaring it makes every plan report
      // a false "null -> ON_FAILURE" drift, so only the retry count is set here.
      restartPolicyMaxRetries: 5,
    },
    replicas: { "iad": 1 },
  });

  // The morning digest: same repo and build, runs on a schedule and exits.
  // Railway cron is UTC: 04:30 UTC is 07:30 Israel in summer, 06:30 in winter.
  const digest = service("digest", {
    source: dataCopilot,
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    deploy: {
      startCommand: "python -m app.digest",
      cronSchedule: "30 4 * * *",
      restartPolicyType: "NEVER",
    },
    replicas: { "iad": 1 },
  });

  return project("data-copilot", {
    variables: { managed: false },
    resources: [web, digest, Postgres, postgresVolume],
  });
});
