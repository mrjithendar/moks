# Portal redesign: milestone 1

2026-11-06. Every result here is **fixture**: the real API, worker and built portal at `/moks` in Chrome, with the
Kubernetes, Prometheus and Loki connectors answering from `backend/tests/portal_fixtures.py`. Nothing ran against
mk4. The kindnet values copy mk4's DaemonSet as read with `kubectl` on 2026-11-06. See [audit.md](audit.md) for
the setup and the problems this answers.

## 1. Screenshots

`before/<screen>-<width>.png` and `after/<screen>-<width>.png`, for widths 390, 768 and 1440. Both sets ran the same
fixture, captured by `frontend/e2e/capture.spec.ts`. The before set ran the backend at `a604c65`.

| Screen | What changed |
|---|---|
| `investigation-detail` | Question, Answer, Evidence, then Conversation history. Claims are labelled, and citations name what they point at |
| `investigations-list` | Status on every row, scope, relative time (absolute on hover and focus), and a two-line preview |
| `kubernetes-explorer` | A waiting reason beats the phase (CrashLoopBackOff, not Running); a truncated list says so; names stay on one line |
| `rightsizing` | One card per container: four fields, only the changed ones marked, each with its reason; CPU and memory separate |
| all | Five nav groups, a scope pill in the header, a "Menu" button below 768px |
| `overview`, `incidents`, `metrics`, `logs` | Only the shell changed (milestone 2) |

## 2. Changes by concern

Nothing is committed (the owner commits). The two patches are made so the rightsizing fix can ship first, on its own.

| Concern | Files |
|---|---|
| **Rightsizing fix** (patch 1, ships alone) | `backend/moks/connectors/finops.py`, `backend/moks/tools/phase2.py`, `backend/moks/api/finops.py` (adds `rightsizing_pricing`), `backend/tests/test_rightsizing_changes.py`, `frontend/src/rightsizing.ts`, `frontend/src/rightsizing.test.ts`, `frontend/src/fixtures/rightsizing.json`, `frontend/src/pages/Rightsizing.tsx`, `frontend/src/pages/Insights.tsx` (rightsizing list, CSV, URL scope), the rightsizing block of `styles.css` |
| Shell | `nav.ts`, `shell.tsx`, `scope.tsx`, `ui.tsx` (exports `Floating`), `styles.css` (breakpoints, density, focus, scope), `shell.test.tsx` |
| Inbox | `pages/Conversations.tsx`, `pages/Investigate.tsx` (claim labels, citations, exact source), `evidence.tsx`, `pages/Conversations.test.tsx` |
| Tables and states | `table.tsx`, `states.tsx`, `pages/Live.tsx`, `components.tsx` (`Multi.truncated`), `backend/moks/api/multi.py` (adds `truncated`), `backend/tests/test_multi_truncation.py` |
| Browser tests | `playwright.config.ts`, `e2e/*`, `package.json` (`@playwright/test` 1.65.0, `test:e2e`), `vite.config.ts` (vitest skips `e2e/`), `backend/tests/portal_fixtures.py`, `.gitignore` |
| Docs | `docs/redesign/*` |

## 3. Tests (all fixture)

| Command | Result |
|---|---|
| `npm run typecheck` | clean |
| `npm test` (vitest) | 154 passed: 149 before this work, plus 5 rightsizing tests |
| `npm run build` | built |
| `npx playwright test` (needs `npm run build`, `MOKS_PYTHON`, PostgreSQL) | 12 passed, 1 skipped (the screenshot capture, which runs only with `MOKS_CAPTURE`) |
| `pytest tests/test_rightsizing_changes.py tests/test_multi_truncation.py` | 6 passed. At `a604c65` the 4 engine and tool tests failed (§4); the API test and the truncation test need this change's fixtures and field |
| Rightsizing patch alone, on the base commit | typecheck clean, vitest 154 passed, 28 backend tests (rightsizing, phase-2 connectors, skills) passed |

Browser tests in `e2e/milestone1.spec.ts`:

- a deep link to a conversation;
- opening an evidence citation, down to its exact pod log lines and on to the explorer;
- a follow-up draft surviving navigation and a reload, then sending;
- Back restoring the inbox's filters, selection and scroll position;
- the mobile Menu's focus trap, Escape, backdrop, navigation and focus return;
- the explorer's filters and scope restored from the URL, 200+ rows, and one-line 63+ character names;
- a viewer seeing no Admin, getting the denied state on a direct URL, and a 403 from the API;
- the kindnet-cni proposal review, and a CSV that matches it.

Three existing assertions now describe the new design. None was removed. `shell.test.tsx` names the button "Menu"
(was "Open navigation") and the item "Investigations" (was "Conversations"), and the role-filtered groups come out
as Investigate, Monitor, Knowledge. `Conversations.test.tsx` expects the Back link to carry `?env=mk4`, after the
test opens an old `?environment=mk4` link: it now also checks that old links are converted.

## 4. Rightsizing root cause

The values first went wrong in the **engine**, `finops.rightsize()`:

1. The *current* request went through `fmt_mem`, which rounds up to 16Mi steps for patches, so 50Mi was reported
   as "64Mi". The recommendation was also 64Mi, which gave "64Mi → 64Mi".
2. One `action` covered both resources. With mk4's price the CPU saving fell under the threshold, so the row read
   "increase" (the OOM finding) while it recommended CPU 100m → 10m. With another price the same row read "reduce"
   beside an OOM risk.
3. The memory *limit*, the field the OOM risk is about, was raised to 96Mi only in the proposal payload. The table
   and the CSV never showed it. The CPU limit was not in the output at all.

The API passed this on unchanged, and the UI, the CSV and the proposal each read different parts of it. The fix is
in the engine. Each container still has one row (the API change is additive), and gains `recommendations`: one entry
per resource with its own `action`, the `changes` (field, current as set, recommended, direction, reason), the
`capacity` freed or reserved, and the exact `patch`. `current` and `recommended` now carry all four values, and
unchanged fields repeat the current one. The tool also reports `pricing` (fixed or per unit). The table, CSV and
proposal all read the same `recommendations` (`frontend/src/rightsizing.ts`), and each output has a test:

| Output | Test |
|---|---|
| Engine | `test_kindnet_memory_and_cpu_are_separate_recommendations_with_true_values` and two others |
| API | `test_the_api_returns_the_engine_values_for_kindnet` (`/finops/overview`, executor, fixture cluster) |
| UI | `e2e` rightsizing test: four fields, only the changed ones marked, separate CPU and memory, no money under a fixed price |
| CSV | `rightsizing.test.ts` on the engine's own output, and the e2e test on the downloaded file |
| Proposal | `rightsizing.test.ts` (`resizePlan`, `diffLines`), and the e2e preview: exact diff, impact, approval state, Propose disabled with the reason |

kindnet is a DaemonSet, and `patch_resources` accepts Deployments only, so Propose is disabled and says so. For
Deployments, Propose opens the existing `PlanForm`, and the plan goes through the usual approval flow.

## 5. Accessibility (fixture, Chrome, `e2e/a11y.spec.ts`)

| Check | Result |
|---|---|
| Text contrast ≥ 4.5:1 in the changed components, dark and light themes | passed, computed from the rendered colours |
| Keyboard-only Tab pass through inbox, conversation, explorer and rightsizing, with a visible focus ring at every stop | passed. The ring is now 3px |
| Touch targets ≥ 44px at 390px (header, and buttons, summaries and selects in the changed components) | passed after raising citation chips to 44px on touch screens |
| No page-level horizontal overflow at 390px on the changed screens | passed after fixing the phone header (it pushed the avatar to 447px) |
| 3:1 for UI parts | not measured by the test. Checked by hand for the focus ring (`#5aa9f8` on `#0e1116`, `#0b5fcc` on `#f4f5f7`) |

## 6. Known issues and decisions to review

- **Scope** lives in the URL as `env` and `ns`, and is shared by Investigations, the Kubernetes explorer and FinOps.
  Old inbox links with `?environment=` are converted. Metrics, Logs and Incidents keep their own pickers until
  milestone 2, so the header pill appears only where it is true.
- **Truncation** is reported, not removed. The executor's 48 KB result bound still cuts large namespaces (`shop`
  shows 163 of 220). Paging is a backend change, not done here.
- **Retry:** no endpoint re-runs an investigation, so none is offered. Stop uses the existing cancel endpoint.
- **Phone breakpoint:** the shell switches to the Menu drawer below 768px. Page padding still changes at 900px, as
  before, so milestone-2 pages look the same.
- **New shared components** (`ResourceTable`, `StateView`) are used only by the explorer's pods tab, and the explorer's
  states on every tab. `DataTable` stays for unmigrated screens.
- **Fixture launcher:** `e2e/fixture_server.py` drops `moks_e2e_*` databases left behind by killed runs, and only
  those with nothing connected.
- **Not run:** mk4 or any live cluster, Safari or Firefox, a real phone keyboard (`visualViewport` was not exercised;
  the composer stays above the fold at 390×844), and a screen reader.
