> **Superseded, reference only.** Archived 2026-10-05; replaced by the Collicity product specification. See `README.md` in this folder for status and reading order.

# Compliance Assistant Platform — Architecture & Design Plan

**Status:** Draft v2
**Domain sequence:** internal communications → vulnerabilities → regulatory change

---

## 0. What changed from v1

- The **compliance record** and **organization profile** are now the central objects; infrastructure is described in service of them
- First release is cut to **one domain (internal comms), one deployment shape, one connector family**, a focused desktop inbox, and authentication plus permissions from the start
- The **triage gate is treated as a correctness risk**: inputs retained, uncertain cases escalated, discards sampled and audited, recall measured alongside precision and cost
- **Within-organization permissions** added; RLS role requirements made explicit
- **Reliable ingestion** specified: checkpoints, stable IDs and versions, deduplication, reconciliation, freshness, idempotent activities, desktop resync
- **Deployment operations** budgeted separately from the "one codebase" claim; additional shapes deferred until a customer requirement justifies them
- **Sequencing fixed**: thin interfaces (LLM backend, connector, widget manifest, typed topics) are defined now; the widget board, additional model implementations, and desktop inference are deferred

---

## 1. First-release thesis

**Buyer:** a compliance practitioner or team lead whose obligations arrive through email and chat — audit requests, evidence deadlines, policy acknowledgments, vendor attestations, regulator correspondence.

**Value:** those obligations are surfaced from their own communications into a reviewed task list where each item carries its source, the supporting passage, why it was flagged, an owner, a deadline, and a completion record.

**Why this order:**
- It matches the persistent-assistant product shape (the desktop presence is central here, not a nice-to-have)
- It forces the hardest permission model to be solved first; vulnerabilities and regulatory change then inherit it
- Internal comms is the connector-heavy domain, so the connector interface is exercised early

**What it costs, and the mitigation:**
- No authoritative feed to reconcile against → recall is measured against a **human-labeled sample** of messages, not a ground-truth feed
- Highest permission risk → release 1 uses **personal scope**: a finding is visible only to principals who could already read its source (see §5)
- Fuzziest signal → release 1 targets a **narrow set of obligation types** (deadline-bearing requests, acknowledgment requests, evidence requests) rather than "anything compliance-related"

---

## 2. Goals and non-goals

**Goals**
- Turn a detected change in a connected source into a cited, reviewed, assigned, and tracked compliance record
- Make every record explainable: what changed, why it may apply to this organization, and what happened after review
- Keep humans responsible for action; the system proposes, people decide
- Keep LLM cost low without silently dropping relevant items
- Native, persistent desktop presence with a focused inbox and detail view

**Non-goals (release 1)**
- Vulnerability and regulatory feeds (releases 2 and 3)
- Org-wide finding sharing beyond source ACLs
- VPC and on-prem deployment shapes (boundaries defined, not implemented)
- Desktop-side model inference
- Widget board, connector SDK, widget SDK, any marketplace
- Autonomous remediation

---

## 3. Core domain model

### 3.1 Compliance record

The central object. One record per detected obligation or change. Fields:

| Group | Fields |
|---|---|
| Source | connector, source object ID (stable), source version, captured-at timestamp, immutable snapshot reference |
| Evidence | supporting passages (quoted, with offsets into the snapshot), extracted structured fields, extraction method (deterministic or model, with model/prompt version) |
| Change | what changed relative to the previous version of the same source object, if any |
| Applicability | which organization-profile elements matched (roles, policies, calendars, later: inventory, jurisdictions), model rationale, confidence band |
| Review | reviewer, decision (accept / reject / modify / defer), reviewer rationale, timestamp |
| Ownership | owner, deadline, status, assignment history |
| Completion | completion evidence (link, attachment reference, or note), completed-by, completed-at |
| Visibility | visibility policy (see §5), derived from source ACL at capture time, re-evaluated on source ACL change |
| History | append-only log of every state change above; reviewer overrides and source re-captures never overwrite prior state |

Rules:
- Source snapshots are immutable; a re-captured source produces a new version linked to the same record, not an edit
- Model outputs are stored with the model and prompt version that produced them so a later model change can be compared against prior decisions
- A rejected record is retained (subject to retention policy) so gate and model performance can be audited

### 3.2 Organization profile

Maintained data that applicability decisions are made against. It replaces the undefined "org context" from v1.

**Release 1 (internal comms):**
- People and roles (who is an auditor, regulator contact, vendor, executive, control owner)
- Teams and reporting lines relevant to assignment
- Policies and controls (name, owner, review cadence)
- Compliance calendar (audit windows, filing dates, recurring attestations)
- Obligation types the organization cares about, with examples

**Release 2 (vulnerabilities):** software and asset inventory (CPE-mappable product/version list), criticality tiers, patch SLAs

**Release 3 (regulatory):** jurisdictions, business activities, regulated products/services, applicable frameworks

The profile is versioned. Applicability rationale references the profile version used.

### 3.3 Evidence retention

- Source snapshots and gate inputs are retained under a **defined retention policy** (tenant-configurable, with a floor long enough to audit gate decisions — proposed 90 days for discards, record lifetime for accepted items)
- Retention applies equally to discarded items; without this the gate cannot be audited

---

## 4. Detection pipeline and the triage gate

```
ingest → normalize → deterministic pre-filter → model triage → synthesis → human review → assignment → completion
```

**Deterministic before model.** Anything extractable without a model is extracted without one: dates and deadlines, sender identity resolved against the org profile, thread and message IDs, attachment metadata, explicit request phrasing patterns. In release 2 this is decisive: CVE ID, CVSS score and vector, and affected CPEs come from the NVD API as structured fields and never pass through a model.

**Model triage has three outcomes, not two.**
- Relevant → synthesis
- Not relevant → discard (retained, sampled)
- Uncertain → escalate to the stronger model before deciding

Discarding is the consequential decision, because a wrong discard never reaches the stronger model or a human. The gate is therefore designed for recall first and measured accordingly.

**Gate requirements:**
- All gate inputs retained per §3.3
- Uncertain cases escalate rather than discard
- A fixed percentage of discards (proposed 5%, higher during pilot) is routed to human audit
- A **labeled evaluation set** is maintained per domain and re-run on every model or prompt change
- Metrics tracked per tenant and globally: relevant items missed (from audit and labeled set), alert precision (accepted / surfaced), cost per accepted record, median review time

**Synthesis** (stronger model) produces the applicability rationale, the proposed owner and deadline, and the checklist or task text. Prompt caching is used for the org profile; batch mode is used where latency is not user-facing.

**Desktop inference is deferred.** One inference stack on the backend for release 1. The LLM interface (§7.3) allows a local or desktop implementation later if benchmarks show benefit.

---

## 5. Security and authorization

### 5.1 Tenant isolation
- Postgres row-level security on every tenant-scoped table
- The application connects as a **non-owner role without `BYPASSRLS`**; `FORCE ROW LEVEL SECURITY` is set on tenant tables so the owning role is also subject to policy
- Tenant ID is set per connection/transaction from the authenticated principal, never from request input
- Background workers run under the same constraints; a Temporal activity carries the tenant and acting principal in its input and opens its database session accordingly

### 5.2 Within-organization visibility
Tenant isolation is necessary but not sufficient. A record derived from a private mailbox or channel must not surface to someone who could not read the source.

- Every record carries a **visibility policy** derived from the source ACL at capture time (mailbox owner; channel members; shared-mailbox members)
- Release 1 default is **personal scope**: visible to source-ACL principals only. Explicit sharing to a team or role is a later feature, and when added it is an audited act by a principal who can already see the record
- Visibility is enforced at every read path, not only the API:
  - REST/GraphQL API queries
  - Realtime subscriptions (the subscription filter applies the same policy as the API)
  - Desktop notifications (the notification service resolves recipients through the policy; summaries and titles are as sensitive as bodies)
  - Evidence and snapshot storage (object keys are tenant- and policy-scoped; presigned access is policy-checked)
  - LLM prompt assembly (a synthesis prompt never includes content from a source the requesting principal cannot see; batch jobs are run per visibility set, not per tenant)
  - Background jobs and exports
- Source ACL changes (a user leaves a channel) trigger re-evaluation of visibility on affected records

### 5.3 Authentication
- OIDC for user login from release 1 (SAML added when a customer requires it; use an identity provider integration rather than a hand-built SAML stack)
- OAuth2 per connector for source access, with token storage encrypted at rest and scoped to the connecting principal

---

## 6. Reliable ingestion

"Never miss a change" is a set of concrete requirements:

- **Checkpoints:** each connector persists a cursor (delta token, history ID, last-modified watermark) per source scope; a run resumes from the checkpoint, never from "now"
- **Stable source IDs and versions:** every captured object has a source-stable identifier and a version (etag, modified timestamp, or content hash); the record model keys on both
- **Deduplication:** ingestion is keyed on (tenant, connector, source ID, version); a re-delivered object is a no-op
- **Idempotent activities:** Temporal activities may execute more than once; every activity that writes is idempotent by construction (upsert on the dedup key, idempotency keys on outbound side effects such as notifications and reminders). Retries must not create duplicate records or reminders
- **Reconciliation:** a scheduled full-scan per source scope compares source state against captured state and repairs gaps; cadence is per connector (daily for mailboxes, weekly for archives)
- **Freshness:** each source scope exposes last-successful-sync, last-attempt, and error state; the desktop shows freshness so a silent connector failure is visible to the user
- **Desktop recovery:** the realtime channel carries per-tenant sequence numbers; on reconnect the client sends its last sequence and receives the delta, or a full resync if the gap exceeds the retained window

---

## 7. System architecture

### 7.1 Desktop client
- Tauri shell: tray icon, hotkey summon, dockable panel
- Release 1 UI is **one inbox and one detail view**: the inbox lists records needing the user's review or action with freshness indicators; the detail view shows source passage, applicability rationale, review controls, owner, deadline, completion evidence, and history
- Realtime via WebSocket with the resync protocol in §6
- Native OS notifications for items with a deadline inside a configurable window; everything else stays in the inbox
- No local inference in release 1

### 7.2 Backend
- Python / FastAPI API
- Temporal for connector polling, reconciliation, gate/synthesis workflows, reminders
- Postgres (RLS per §5.1); object storage for immutable snapshots and completion evidence
- Redis for realtime fan-out and sequence tracking

### 7.3 LLM interface (defined now, one implementation now)
- `classify(input, schema) → {label, confidence, rationale}`
- `extract(input, schema) → structured fields` (used only where deterministic extraction is impossible)
- `synthesize(context, task) → text + structured proposals`
- Every call records model identity, prompt version, token counts, and cost against the tenant and record
- Release 1 implements one backend (hosted API with a small-model triage tier and a stronger synthesis tier). Customer-cloud and local-only implementations are later work behind the same interface

### 7.4 Connector interface (defined now, first-party only)
- `authenticate(principal)`, `checkpoint()`, `poll(cursor)` / `on_webhook(payload)`, `normalize(object) → (source_id, version, acl, content)`, `reconcile(scope)`
- Connector code isolated from core logic; no shared mutable state
- Release 1 ships **one connector family**: Microsoft 365 via Graph (mail first; Teams reuses the same app registration in release 1.x) or Google Workspace (Gmail first; Chat later). Choose by target customer base (§11)

### 7.5 Widget hooks (thin interfaces only)
- Typed realtime topics from day one: `record.created`, `record.updated`, `record.assigned`, `reminder.due`, `connector.freshness`
- Widget manifest schema (id, name, size preset, subscribed topics) defined and versioned; the release 1 inbox is the only implementation
- Board container, additional widgets, sandboxing, and any public API are deferred

---

## 8. Deployment

**Release 1: multi-tenant cloud only.**

Deployment boundaries are defined now so later shapes don't force a redesign:
- All services containerized; configuration via environment and feature flags, never code branches
- No service assumes it can reach an arbitrary external endpoint; outbound calls go through a named egress layer so an air-gapped build can substitute a **controlled feed-import mechanism** with visible feed currency
- Secrets, identity, backups, upgrades, and support are documented as an **operations budget per deployment shape**, separate from the codebase claim
- VPC and on-prem shapes are implemented when a concrete customer requirement justifies them

---

## 9. Sequenced roadmap

**Release 1 — Internal communications**
- Domain model (§3), gate with measurement (§4), permissions (§5), reliable ingestion (§6)
- One connector family (mail), personal scope, OIDC login
- Desktop inbox and detail view, tray, hotkey, realtime with resync, native notifications for near-deadline items
- Labeled evaluation set for obligation detection; pilot metrics live from day one

**Release 1.x — Chat**
- Teams or Chat connector on the same app registration; channel ACLs into visibility policy

**Release 2 — Vulnerabilities**
- NVD connector with deterministic extraction of CVE, CVSS, CPE
- Org profile gains software inventory and patch SLAs; applicability is CPE-to-inventory matching first, model rationale second
- Gate recall measured against the feed itself (ground truth exists here)

**Release 3 — Regulatory change**
- Federal Register connector first (structured API, daily, agency and document-type metadata); Congress.gov second
- Org profile gains jurisdictions, business activities, frameworks
- Passage-level evidence with document versioning; this is where the evidence model is most stressed

**Platform work, interleaved as customers require**
- Team and role sharing beyond personal scope (audited)
- SAML
- VPC shape, then on-prem with controlled import
- Widget board; externalized connector interface; externalized widget interface with sandboxing

---

## 10. Pilot success metrics

Judge release 1 on these, per tenant and in aggregate:
- Relevant items missed (from discard audit and the labeled set)
- Records accepted on review / records surfaced (precision)
- Median review time per record, and time from source change to reviewed record
- Cost per accepted record
- Records with completion evidence / records accepted (are tasks actually closed)
- Connector freshness SLO adherence (time since last successful sync under target)

These results decide the next platform investment, not the roadmap above.

---

## 11. Open decisions

- **Connector family for release 1:** Microsoft 365 (Graph) or Google Workspace, decided by the target customer base
- **Obligation types in scope for release 1:** confirm the initial narrow set (deadline-bearing requests, acknowledgment requests, evidence requests) and the labeled-set sourcing plan
- **Retention floors:** confirm 90 days for discards and record-lifetime for accepted items, and whether tenants may shorten them
- **Discard audit rate during pilot** and who performs the audit
- **Identity provider integration** choice for OIDC now and SAML later
- **Desktop idle state:** count of items awaiting review vs. single most urgent item
