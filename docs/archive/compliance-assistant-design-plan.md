> **Superseded, reference only.** Archived 2026-10-05; replaced by the Collicity product specification. See `README.md` in this folder for status and reading order.

# Compliance Assistant Platform — Architecture & Design Plan

**Status:** Draft v1
**Purpose:** A multi-tenant SaaS platform that monitors external sources (email, Slack, Teams, file systems, regulatory feeds like NIST NVD, Federal Register, Congress.gov) and uses tiered LLM reasoning to generate compliance checklists, reminders, and alerts — surfaced through a persistent, dockable desktop assistant.

---

## 1. Goals and non-goals

**Goals**
- Continuously monitor connected sources for compliance-relevant changes (new CVEs, executive orders, laws/regulations, internal comms) and turn them into actionable checklists/reminders
- Serve multiple organizations (multi-tenant SaaS) with a subset of customers requiring single-tenant/VPC or fully on-prem deployment
- Keep LLM cost low via a tiered local-model-first, frontier-model-when-needed routing strategy
- Provide a native, persistent, dockable desktop presence — not a browser tab — as the primary touchpoint
- Design connector and widget interfaces cleanly now so they can be externalized to 3rd parties later, without committing to that complexity yet

**Non-goals (for v1)**
- Public 3rd-party connector or widget marketplace (interfaces should support it later; don't build sandboxing/review process yet)
- Mobile clients
- Full agentic autonomous action-taking (v1 generates checklists/reminders for humans to act on, not auto-remediation)

---

## 2. System architecture overview

Three tiers, cleanly separated so deployment topology (pure cloud vs. VPC vs. on-prem) is a configuration choice, not a code fork:

1. **Desktop client** — thin native shell (Tauri): dockable widget/tray UI, hotkey summon, a small local model for instant triage, persistent realtime connection to the backend
2. **Backend platform (multi-tenant)** — API + durable orchestration (Temporal), connector execution, multi-tenant data store (Postgres + row-level security)
3. **External systems** — connectors (email, Slack, Teams, file systems, regulatory feeds) and the LLM backend layer (cloud API, customer's own cloud LLM, or fully local model), which is itself deployment-dependent

A widget summary of this layering was shared earlier in this conversation as a diagram — see prior message.

---

## 3. Desktop client

- **Framework:** Tauri (native window primitives, small footprint, multi-window support, sidecar process support for bundling a local Python/inference service if needed)
- **Presence:** system tray / menu bar icon (Windows/macOS solid support; Linux tray support is fragmented — degrade gracefully to a pinned window where tray isn't available)
- **Interaction model:** global hotkey to summon/dismiss a compact dock panel; expandable to a fuller detail view
- **Realtime:** persistent WebSocket (or SSE) connection to the backend; state is pushed on change, not polled
- **Local inference:** small quantized model (7–8B class, e.g. Llama 3.1 8B / Qwen2.5 7B, via Ollama) for instant local triage/classification; hardware is probed at first run (GPU presence, core count) to pick a default model tier, with GPU acceleration used automatically when available (CUDA/Metal/ROCm) and CPU as the guaranteed fallback

### 3.1 Widget system — foundational hooks (build now, defer complexity)

Preparing for a future multi-widget, eventually 3rd-party-extensible board without building the hard parts yet:

**Build now:**
- Generic `Widget` component contract (mount / receive-data / resize / unmount lifecycle) — even first-party widgets implement this, not bespoke screens
- Realtime events structured as **typed topics** (e.g. `compliance.alerts`, `tasks.checklist`, `reminders.upcoming`, `connector.status`) rather than one monolithic state blob, so widgets subscribe only to what they need
- A small **widget manifest** (id, display name, size constraints following small/medium/large presets, subscribed topics) — used internally today, same shape a 3rd party would fill in later
- A generic layout/board container supporting add/remove/reorder/resize, with per-user layout persisted, even while only first-party widgets exist

**Defer:**
- Sandboxed/isolated widget execution (iframe + postMessage, or separate process) — unnecessary until running untrusted code
- Public marketplace, versioned public widget API, review process
- Permission/consent UI for widget data access

---

## 4. Backend platform

- **API:** Python (FastAPI) — best fit for compliance document parsing (PDF executive orders, XML bill text, NVD JSON) and local-model tooling
- **Orchestration:** Temporal (self-hostable) for durable, retryable scheduled polling and long-running connector jobs — matters both for reliability ("never miss a change") and because it's self-hostable for on-prem deployments
- **Data store:** Postgres with row-level security for tenant isolation — same schema serves multi-tenant cloud and single-tenant on-prem (on-prem is just "one tenant row")
- **Queue/eventing:** Redis or SQS for event fan-out to the realtime layer

### 4.1 Connector architecture

- **Internal connector interface only for v1** (no 3rd-party SDK yet): a defined internal contract — `authenticate()`, `poll()` / `on_webhook()`, `normalize_event()` — with connector code structurally isolated from core business logic so it's externalizable later without a rewrite
- **Auth is two distinct concerns:**
  - SAML/OIDC — how a tenant's employees log into the platform (enterprise SSO; use existing middleware/provider rather than hand-rolling)
  - OAuth2 — how the platform connects to each external data source (per-connector app registration, scopes, token refresh)
- **First-party connectors to prioritize:** email (Graph API / Gmail API), Slack, Teams, file system watch, and regulatory feeds (NVD JSON feed, Federal Register API, Congress.gov API)

### 4.2 LLM orchestration and cost tiering

| Tier | Where it runs | What it does |
|---|---|---|
| Local triage | Small model on backend worker (and separately, on desktop client for instant UI feedback) | Classify "is this change substantive," extract structured fields (CVE ID/CVSS, bill number/agency/effective date), route to review or discard |
| Frontier synthesis | Hosted API (Claude/GPT) — called only after Tier 1 filters noise | Generate checklist language, assess applicability to org context, summarize long documents, draft reminders |
| On-prem/air-gapped | Local model only (larger, e.g. 32–70B class if GPU available) | Handles both triage and synthesis when no external API calls are permitted |

- **Cost levers:** prompt caching (repeated org/policy context), batch API (~50% discount for non-real-time synthesis), and the triage gate itself (most feed volume is noise/duplicates and should never reach the frontier tier)
- **Pluggable LLM backend interface** (`llm.classify()`, `llm.synthesize()`) abstracting: hosted API, customer's own cloud LLM (Azure OpenAI/Bedrock in their VPC), or fully local model — required because on-prem customers often cannot send data to external APIs at all

---

## 5. Multi-tenancy and deployment models

One codebase, three deployment shapes:

1. **Multi-tenant cloud** — standard SaaS, tenant-scoped via Postgres RLS, hosted LLM APIs
2. **Single-tenant/VPC** — dedicated deployment in customer's cloud boundary; may use customer's own LLM deployment for data residency
3. **On-prem/air-gapped** — full stack (API, workers, Postgres, Redis, local LLM via Ollama) deployed via Docker Compose in customer's datacenter; license-key gated; no external API calls

Packaging: Docker containers for all services; Docker Compose for on-prem/single-org, Kubernetes/Helm for multi-tenant cloud. Feature/deployment differences are config-driven (env vars, feature flags), not separate code branches.

Per-tenant LLM usage/cost must be metered regardless of deployment shape, for plan enforcement and (for on-prem) capacity planning.

---

## 6. Phased roadmap (suggested)

**Phase 1 — Core platform, single deployment shape**
- Backend API + Temporal + Postgres/RLS scaffolding
- 2–3 first-party connectors (email, one regulatory feed, Slack)
- Tiered LLM routing (local triage + hosted frontier)
- Desktop client: tray icon, dock panel, hotkey, WebSocket realtime — single built-in widget (e.g. "recent alerts")

**Phase 2 — Multi-tenancy and deployment flexibility**
- SAML/OIDC SSO
- Docker Compose on-prem packaging + license gating
- Pluggable LLM backend interface (customer-cloud and local-only modes)
- Widget board with add/remove/resize (still first-party widgets only)

**Phase 3 — Extensibility**
- Externalize the connector interface into a documented SDK
- Externalize the widget interface; add sandboxing and a review/publish flow

---

## 7. Open decisions

- SSO/identity provider integration approach (e.g. build vs. a provider like WorkOS)
- Exact on-prem licensing/telemetry mechanics for air-gapped customers
- Which regulatory feeds and communication connectors ship in the very first release
- Default idle-state content for the desktop widget, and native-OS-notification vs. in-widget-only alerting for urgent items (raised, not yet decided)
