# MOKS

AI-assisted SRE investigation portal for Production Operations, served at
**https://jithendar.com/moks**.

Ask "Why is checkout returning 502 errors?" or "Show pods restarting in the last 30 minutes."
MOKS collects live evidence from Kubernetes, Prometheus and Loki through typed, read-only,
policy-checked tools and streams its progress. The answer keeps **observed facts** (each citing
its evidence), **suspected causes with confidence**, **missing evidence**, **next steps** and
**proposed remediation** separate. It never claims a root cause or a recovery it has not verified.

> Not to be confused with the RoboShop shopping widget also called "Moks"
> (`roboshop-ai/services/moks`, served at `/roboshop/moks/*`).

## Status

Phase 1, the read-only vertical slice, is implemented and tested, including live read-only
checks against mk4. It is **not deployed yet**: deploying needs a commit, a CI image build and
the steps in the [operator guide](docs/OPERATOR_GUIDE.md).

| | |
|---|---|
| Backend | Python 3.12 · FastAPI · SQLAlchemy/asyncpg · Alembic · kubernetes_asyncio · httpx |
| Frontend | React 19 · TypeScript · Vite · React Router (basename `/moks`) |
| Data | PostgreSQL (users, RBAC, sessions, environments, connectors, LLM configs, investigations, evidence, hash-chained audit) |
| LLM | Provider-neutral: OpenAI(-compatible), Gemini, Amazon Bedrock, Ollama; rule-based fallback |
| Deploy | One image, Helm chart, Argo CD (`moks-portal`, project `moks`), Traefik Prefix `/moks` without stripping |
| Observability | Prometheus metrics on `:9464`, JSON logs for Alloy/Loki, OTel traces, Grafana dashboard |

## Layout

```text
backend/          FastAPI app, worker, connectors, tools, orchestrator, migrations, tests
frontend/         React portal
deploy/helm/moks  Helm chart (values-minikube.yaml for mk4, values-eks.yaml optional)
deploy/argocd     AppProject + Application
deploy/secrets    Secret templates (no real values)
deploy/terraform  AWS: ECR repo, GitHub OIDC CI role, MOKS runtime IAM user (Bedrock)
deploy/rbac, iam  Kubernetes RBAC and AWS IAM examples
deploy/grafana    Dashboard JSON and Prometheus scrape job
lab/              Four reproducible incidents with expected evidence and fixes
dev/              Local run script and seed
scripts/          create-secrets, set-image, backup
docs/             Architecture, security/RBAC, connectors, operator guide, testing, limitations
```

## Documents

- [Architecture and design](docs/ARCHITECTURE.md): components, `/moks` path handling, investigation loop, data model, Phase 2–4 design
- [Security, RBAC and policy](docs/SECURITY.md)
- [Connectors and tool contracts](docs/CONNECTORS.md)
- [Operator guide](docs/OPERATOR_GUIDE.md): commands in implementation order, local development, backup/restore
- [Demo incident lab](lab/README.md)
- [Testing and results](docs/TESTING.md)
- [Status and limitations](docs/LIMITATIONS.md)

## Quick start (local)

```sh
cd backend && pip install -r requirements.txt && pip install --no-deps -e . && cd ..
cd frontend && npm ci && npm run build && cd ..
MOKS_DATABASE_URL=postgresql+asyncpg://moks@localhost:5432/moks dev/run-local.sh
open http://localhost:8080/moks/      # dev sign-in: sre@moks.local
```
