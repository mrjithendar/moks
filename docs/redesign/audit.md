# Portal redesign: Phase 0 audit

State of the portal at `a604c65` (2026-11-06). Every screenshot in `before/` is **fixture** data: the real API, worker
and built portal at `/moks`, with Kubernetes, Prometheus and Loki answering from `backend/tests/portal_fixtures.py`.
None of it is mk4. The kindnet resources and the five nodes copy mk4's, read with `kubectl --context mk4` on
2026-11-06.

## 1. Routes and navigation

All routes are in `frontend/src/App.tsx`. `Need` shows "You don't have access" when the person lacks every permission
listed; the backend enforces them again on every call. Deep-link parameters come from `useUrlFilters` (the query
string), except where noted.

| Path | Component | Permission (any of) | Deep-link params |
|---|---|---|---|
| `/` | `Overview` | none | none |
| `/investigate` | `NewInvestigation` | none in the route; the nav needs `investigation.run` | `q`, `env`, `ns`, `incident` (prefill) |
| `/investigations` | `Workspace` (inbox) | none in the route; the nav needs `investigation.read` | `environment`, `state`, `unread`, `deleted`, `sort` |
| `/investigations/:id` | `InvestigationView` (full report) | none in the route | `:id` |
| `/conversations/:id` | `Workspace` (inbox + reading pane) | none in the route | `:id` plus the inbox params |
| `/explorer` | `Explorer` | `explorer.read` | `tab`, `env`, `ns`, `q`, `only` |
| `/metrics` | `Metrics` | `explorer.read` | tab and query state in component state; the env picker is not in the URL |
| `/logs` | `Logs` | `explorer.read` | `view`; search fields, and LogQL `env`/`q` |
| `/detections` | `Detections` | none | none |
| `/incidents`, `/incidents/:id` | `Incidents`, `IncidentView` | none | `status`, `severity`, `environment`, `q` |
| `/actions`, `/actions/:id` | `Actions`, `PlanView` | `action.request`/`action.approve`; `investigation.read`/`action.approve` | `status` |
| `/diagnostics`, `/diagnostics/:id` | `Diagnostics`, `DiagnosticView` | `action.request` | none |
| `/terminal`, `/terminal/:id` | `Terminals`, `TerminalView` | `terminal.use`/`terminal.revoke` | none |
| `/backups` | `Backups` | `backup.read` | none |
| `/knowledge` | `Knowledge` (runbooks) | none | none |
| `/knowledge/explorer`, `/retrieval`, `/review` | Knowledge pages | `knowledge.read`; `knowledge.test`; `knowledge.review`/`skills.review`/`investigation.read` | own params |
| `/skills` | `Skills` | none | none |
| `/finops` | `FinOps` (includes rightsizing) | `finops.read` | **none**: environment, namespace and days are component state |
| `/evals` | `Evals` | `admin.policies` | none |
| `/ml`, `/admin/ml` | `MLInsights`, `MLAdmin` | `ml.read`; `ml.admin`/`learning.review`/`learning.export` | none |
| `/audit` | `Audit` | `audit.read` | own filters |
| `/admin/environments`, `/connectors`, `/llm`, `/users`, `/backup-storage` | Admin pages | `admin.policies`, `admin.connectors`, `admin.llm`, `admin.users`, `admin.connectors` | none |
| `/login` | `Login` | none | `next` |
| `*` | "Page not found" | none | none |

**Navigation** (`frontend/src/nav.ts`): one `NAV` list feeds the sidebar and the mobile drawer. Each item has a
`show(can)` built from the same permissions as its route. Groups at baseline: **Monitor** (Overview, Metrics, Logs,
Detection rules, ML insights), **Investigate** (New investigation, Conversations, Incidents), **Operate** (Approvals &
actions, Diagnostics, Terminal, Kubernetes explorer, Backups), **Knowledge** (Knowledge Explorer, Retrieval test,
Knowledge review, Runbooks & knowledge, SRE skills), **Cost** (FinOps) and **Admin** (Audit log, Environments,
Connectors, Backup storage, LLM providers, Users & roles, Evaluation, Machine learning). Six groups, where the brief
asks for five; the Explorer sits under Operate although it is read-only monitoring.

## 2. Building blocks

- **Shell** (`shell.tsx`): a sidebar that collapses (state kept in `localStorage["moks.sidebar"]`) and a mobile
  drawer below 900px (a `dialog` with `useModal`: focus trap, Esc, backdrop). The header holds breadcrumbs, a
  `scope-chip` that only some pages set (through `useHeaderScope`), the connection dot and the profile menu.
  `useViewportHeight` writes `--vvh` and `--vvt` from `visualViewport`.
- **Components** (`components.tsx`): `PageHead`, `Badge`, `StatusBadge`, `ErrorBox`, `DataTable` (with row menu and
  empty state), `Loading`, `Time`/`ago`, `useLoad`, `useInterval`, `useRequest`, `useScopes` + `EnvNsFilter` +
  `ScopePicker`, `PartialErrors`, `Stat`, `LineChart`, `LogLines`, `SourceRef`.
- **UI** (`ui.tsx`): `useModal`, `Dialog`, `Drawer`, `ActionMenu`, `Truncate`, `Identifier`, `Skeleton`, `Empty`,
  `FeedbackProvider` (toast and confirm), `CopyButton`, `useIsMobile`, `Icon`.
- **Filters** (`filters.tsx`): `useUrlFilters` (typed query-string state), `FilterPanel` (chips with Clear all,
  collapsible, a drawer on phones) and `MultiSelect`.
- **Theme** (`styles.css`): dark and light tokens for surfaces, text, accent and status (`ok`/`warn`/`bad`/`info`,
  each with `-bg` and `-line`). There is a spacing scale (`--sp-1`…`--sp-6`), a type scale (`--fs-xs`…`--fs-xl`),
  control heights (`--ctl` 36px, `--ctl-s` 28px) and a `:focus-visible` ring. There is no table-density token, and
  breakpoints are written by hand at 420, 700, 820, 900, 1000, 1100 and 1180px.
- **API client** (`api.ts`): `api()` sends cookies, CSRF and a typed `ApiError` (status, code, request id, field
  errors), and redirects to `/login` on a 401. `qs()` builds query strings; `apiForm()` uploads. Server-sent events
  for investigations are in `investigationStream.ts`. Drafts and typed searches live in `sessionStorage` under
  `moks.` and are cleared on sign-out.
- **Tests at baseline**: Vitest 5 + jsdom + Testing Library (`npm test`), 25 test files next to the code, with
  mocked `fetch` in each test. There was **no browser test tool**. Earlier Chrome checks (see `docs/TESTING.md`)
  were throwaway CDP scripts, never committed.

## 3. Screenshots

`before/<screen>-<width>.png` for widths 390, 768 and 1440 and these screens: `overview`, `investigations-list`,
`investigation-detail`, `kubernetes-explorer`, `incidents`, `metrics`, `logs` and `rightsizing` (FinOps, scrolled
to Rightsizing; the only screenshot that is not full-page). The capture script is `frontend/e2e/capture.spec.ts`.

## 4. Top 10 usability problems

1. **The Explorer shows part of a namespace as all of it.** `kubernetes-explorer-*.png`: "163 of 163 pods" where
   `shop` has 220. The executor cuts tool results at 48 KB, and `/multi` drops the `truncated` flag, so neither the
   count nor any banner tells an engineer that 57 pods are missing. Mid-outage, the missing pod may be the broken one.
2. **The rightsizing values are wrong, not just badly shown.** In `rightsizing-*.png`, kindnet-cni (mk4's
   DaemonSet, 50Mi request and limit, using about 47Mi) appears as "mem 64Mi→64Mi". The engine formats the
   *current* request by rounding up to 16Mi, so 50Mi reads as 64Mi. The memory limit, the field at OOM risk, is not
   shown. One action label covers both resources, so an OOM-risk "increase" sits next to "cpu 100m→10m". With mk4's
   price the row says "increase"; with the fixture's price the same row says "reduce" beside an OOM-risk finding.
   See §6.
3. **A crashlooping pod shows a green "Running" badge.** `kubernetes-explorer-390.png`: the status badge shows
   the pod phase, so a pod in CrashLoopBackOff with 14 restarts looks healthy at a glance. The reason is in small grey
   text underneath.
4. **Rightsizing is unusable on a phone.** `rightsizing-390.png`: the table is clipped at the Action column. The
   current → recommended values, the saving and the Propose button are off-screen to the right. Workload names break
   at hyphens ("Deployment/checkout-↵6d1f1c").
5. **FinOps scope isn't in the URL.** Environment, namespace and days are component state, so a link to "rightsizing
   for kube-system" is impossible, and the Back button loses the choice. Metrics has the same problem with its
   environment picker. Elsewhere scope lives in the URL under three different names: `env`/`ns`, `environment`, and
   none.
6. **Scope isn't shown consistently.** The header chip appears only on pages that call `useHeaderScope` (a
   conversation shows `mk4/shop`, the Explorer and FinOps show nothing; compare `investigation-detail-1440.png` with
   `kubernetes-explorer-1440.png`). "All authorized environments" is a label in some pickers and missing elsewhere.
7. **A report doesn't say which claims are observed and which are inferred.** `investigation-detail-*.png`: the
   summary, "Observed facts", hypotheses and "Missing evidence" are separate sections, but an individual statement
   has no Observed / Hypothesis / Documented guidance / Missing evidence label. Facts demoted to hypotheses are only
   explained in small grey text. Citations are opaque chips ("E2") that open a side panel. They don't show what
   they point to (a log query, a metric and its time range, an event), so the reader can't judge a citation before
   opening it.
8. **The inbox doesn't say how things stand.** `investigations-list-1440.png`: rows show subject, preview, scope
   and an unread dot. Completed investigations show no status, and the timestamp is wall-clock time only, with the
   absolute time in a `title` that keyboard users can't reach. "8 problem(s) detected from 16 successful evidence
   queries" previews read the same for every row. At 768px the list and reading pane are separate screens already
   (`investigation-detail-768.png`), but they split at 900px, not the 1024px the brief asks for.
9. **The navigation groups don't match the work.** Six groups, a separate one-item "Cost" group, and the Kubernetes
   explorer under Operate though it is read-only. At 1440px the sidebar is taller than the viewport and Admin falls
   below the fold (`overview-1440.png`).
10. **States are ad hoc.** Every page renders its own loading, empty and error states: `Empty` with different
    wording, `ErrorBox`, `PartialErrors`, raw alert boxes ("staging: no Prometheus connector" in `metrics-1440.png`).
    None of them distinguish *stale*, *partial*, *unavailable* or *permission denied*, or say what to do next.

## 5. Running it under /moks

- **Browser tests and screenshots:** `npm run build`, then `npx playwright test`. Playwright starts
  `e2e/fixture_server.py`: a fresh PostgreSQL database (`MOKS_E2E_PG`), the real FastAPI app and embedded worker in
  dev auth mode, and the built bundle served by the backend at `http://127.0.0.1:8099/moks/` with the fixture
  connectors swapped into the connector registry. Two users exist: `sre@moks.local` (admin) and `viewer@moks.local`
  (viewer of `mk4/shop` only). Investigations are created through the API and run in rules mode (no LLM provider).
  `MOKS_E2E_URL` points the tests at a server that is already running.
- **Live:** `dev/run-local.sh` against mk4 through port-forwards still works for manual checks. It was not used
  here: a live cluster cannot give 200 pods with 63-character names or a fixed scenario to assert on.
- **Before vs after:** the before set ran the backend from `git archive a604c65` with only the fixture files
  added, so the rightsizing before-screenshots show the old engine.

## 6. Rightsizing trace at baseline (kindnet-cni)

| Layer | What it held | Where |
|---|---|---|
| Kubernetes | requests cpu 100m, memory 50Mi; limits cpu 100m, memory 50Mi; 5 replicas | mk4, and fixture `KINDNET` |
| Engine `rightsize()` | `current.mem_request` = `fmt_mem(50Mi)` = **"64Mi"**; `current.mem_limit` **"64Mi"**; no `cpu_limit`; one `action` for both resources | `backend/moks/connectors/finops.py` |
| API `/finops/overview` and `/finops/{env}/k8s/rightsizing` | the engine's dict unchanged (the first wrong value is already here) | `backend/moks/api/finops.py`, `tools/phase2.py` |
| UI table | `cpu {current}→{recommended}`, `mem {current}→{recommended}` for requests only, plus one action badge | `frontend/src/pages/Insights.tsx` |
| CSV | the same four request values and one action; no limits | `Insights.tsx` `exportRecs` |
| Proposal payload | requests for **both** CPU and memory, plus `limits.memory` = `recommended.mem_limit`, which the table never showed; Propose is offered only for `Deployment/`, so it is hidden for this DaemonSet without saying why | `Insights.tsx` `PlanForm initial` |

The values first go wrong in the engine (the rounded "current" value and the merged action). Every later layer
copies them, and the proposal adds a limit nobody was shown.
