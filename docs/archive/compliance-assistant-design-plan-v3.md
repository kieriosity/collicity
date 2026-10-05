> **Superseded, reference only.** Archived 2026-10-05; replaced by the Collicity product specification. See `README.md` in this folder for status and reading order.

# Compliance Assistant Platform — Architecture & Design Plan

**Status:** v3 — pilot basis
**Companion:** `acceptance-cases-release-1.md`
**Domain sequence:** internal communications → vulnerabilities → regulatory change

---

## 0. What changed from v2

- **Obligation is a first-class entity**, separate from source objects. Evidence links are typed (creates / revises / cancels / completes / references). Source-driven changes to human-approved fields become proposals that go back through review
- **Assignment is constrained to eligible viewers.** Combined evidence uses an intersection rule, attaching evidence is a reviewed proposal, discard audit respects visibility, and revocation semantics cover the desktop cache and pending notifications
- **Measurement covers the whole pipeline** from raw ingest, including deterministic drops, with authorized conversation context. Reviewer dispositions are separated from detection precision. Recall is estimated with a designed sample and stated uncertainty. NVD ground-truth claim corrected
- **Untrusted content boundary** added: models propose, application code validates and transitions; citations are verified against snapshots; suppression floor; safe rendering; adversarial evaluation cases
- **Ingestion semantics finished:** capture-before-checkpoint ordering, transactional outbox, expired-cursor and full-resync handling, visible sync gaps. The coverage claim is restated without "never"
- **Technology choices frozen** (§12)

---

## 1. First-release thesis

**Buyer:** a compliance practitioner or team lead whose obligations arrive through email and chat.

**Value:** obligations are surfaced from their own communications into a reviewed task list, where each carries its source, supporting passages, why it was flagged, an owner, a deadline, and a completion record — and where later messages that change or cancel an obligation are tracked against it rather than lost.

**Release 1 obligation types:** deadline-bearing requests, acknowledgment requests, evidence requests. Everything else is out of scope for detection.

**Scope rule:** personal scope. An obligation is visible only to principals who could already read every source it rests on.

---

## 2. Goals and non-goals

**Goals**
- A detected change in a connected source becomes a cited, reviewed, assigned, tracked obligation
- Every obligation is explainable: what created it, what changed it, why it may apply, who decided what
- Humans own decisions; the system proposes
- Low LLM cost without silent loss of relevant items, and measured evidence of that
- Native, persistent desktop presence with one inbox and one detail view

**Non-goals (release 1)**
- Vulnerability and regulatory feeds; org-wide sharing beyond source ACLs; VPC/on-prem shapes; desktop inference; widget board; any SDK or marketplace; autonomous remediation

---

## 3. Domain model

### 3.1 Entities

**source_version** — one immutable capture of one source object
| Field | Notes |
|---|---|
| source_object (tenant, connector, scope, source_id) | source_id is the connector's stable identifier |
| version | etag, history ID, modified timestamp, or content hash |
| captured_at, snapshot_ref | snapshot is immutable in object storage |
| acl_snapshot | principals who could read the source at capture |
| conversation_id | thread grouping from the connector |
| deterministic_fields | dates, authenticated sender, resolved directory identity, attachment metadata |
| stage_outcome | prefilter_drop / triage_discard / triage_uncertain / surfaced, with stage versions |

**obligation** — the unit of work
| Field | Notes |
|---|---|
| id, tenant, type | type from the release 1 set |
| title, summary | model-proposed, reviewer-editable |
| status | proposed → accepted → in_progress → completed; also rejected, cancelled, needs_reassignment |
| owner, deadline | |
| approved_fields | set of fields a human has explicitly accepted |
| visibility_set | computed, see §4.1 |
| history | append-only log of every change with actor and cause |

**evidence_link** — many-to-many between obligation and source_version
| Field | Notes |
|---|---|
| link_type | creates / revises / cancels / completes / references |
| passages | (start, end, quote) offsets into the snapshot |
| extraction | method (deterministic / model), model and prompt version, confidence |
| proposed_changes | structured field changes implied by this evidence |
| state | pending / accepted / rejected |

**proposal** — a pending change to an obligation that requires review
| Field | Notes |
|---|---|
| obligation_id, evidence_link_id | |
| kind | field_change / cancel / complete / attach_evidence / merge_into / assign |
| field, current_value, proposed_value | for field_change |
| consequence | e.g., "owner X loses access if accepted" |
| state, decided_by, decided_at, rationale | |

**review_decision** — every reviewer action
| Field | Notes |
|---|---|
| target | obligation / evidence_link / proposal |
| decision | accept / reject / modify |
| disposition (on reject) | not_relevant / duplicate_of:id / already_completed / wrong_boundary_split / wrong_boundary_merge / out_of_scope_type |
| corrected_fields (on modify) | which fields and from what |

**organization_profile** — as v2: people and roles (including who may cancel or extend requests), teams, policies and controls, compliance calendar, obligation types with examples. Versioned; applicability rationale references the version used.

### 3.2 Rules

- One source version may create many obligations; one obligation may rest on many source versions
- Fields not yet human-approved may be updated directly by new accepted evidence; the obligation remains pending review as a whole
- Fields in `approved_fields` change only through an accepted proposal
- `cancels` and `completes` links always generate proposals; they are never auto-applied
- Duplicate detection operates at the obligation level: a restated request produces a `references` link and, if confidence is high, a `merge_into` proposal; the reviewer confirms
- Conversation context: triage and synthesis receive prior messages in the same conversation, bounded (proposed: 10 messages or 30 days), restricted to sources within the acting visibility set
- Worked example: "Send A and B by Friday" → two obligations, each with a `creates` link to the same source version. "A can wait until Tuesday" → a `revises` link on obligation A carrying `deadline: Fri → Tue`. If A's deadline is approved, this becomes a `field_change` proposal; if not, the deadline updates directly and A stays pending

### 3.3 Retention

- Snapshots, deterministic fields, stage outcomes, prompts, and model responses are retained under a tenant-configurable policy with floors: 90 days for items not surfaced, obligation lifetime plus 1 year for surfaced items
- Retention applies to dropped and discarded items; without it neither the audit nor the recall estimate is possible

---

## 4. Visibility, assignment, audit, revocation

### 4.1 Visibility set
- The visibility set of an obligation is the **intersection** of `acl_snapshot` across all evidence links in state `accepted`. Pending links do not shrink visibility until accepted
- Recomputed whenever a link changes state or a source ACL changes

### 4.2 Assignment under personal scope
- Eligible owners = visibility set ∩ active users
- Synthesis proposes an owner from the organization profile; the application validates eligibility. An ineligible proposal is stored with the reason ("no access to source(s) …") and the reviewer is shown eligible alternatives. Release 1 has no in-product sharing; granting access happens outside the system and takes effect on the next capture of the source
- Attaching evidence that would remove the current owner from the visibility set is an `attach_evidence` proposal showing that consequence. Accepting it moves the obligation to `needs_reassignment` and notifies reviewers within the new visibility set

### 4.3 Discard audit under personal scope
- An auditor sees only dropped or discarded items whose sources they could read
- Consequence: private-mailbox items are audited by the mailbox owner (a bounded weekly self-audit task, proposed 10 items) or by a delegate with mailbox access; shared mailboxes and channels by designated auditors
- Recall estimates (§5.3) are stratified by audit coverage and state where coverage is limited

### 4.4 Revocation
- ACL changes are detected on capture, on reconciliation, or via connector membership events where available
- On revocation: recompute visibility → emit `access.revoked` to affected sessions → client purges cached content for those obligations immediately, and on every full resync; client cache has a bounded TTL (24h) and is encrypted with a key held in the OS keychain → the notification dispatcher checks visibility at send time, not enqueue time, and drops queued items for revoked principals → if the owner is revoked, the obligation moves to `needs_reassignment`
- Every revocation effect is written to the audit log

---

## 5. Detection pipeline and measurement

### 5.1 Stages
```
ingest → normalize → deterministic pre-filter → model triage → synthesis → review
```
Each stage records its outcome and version on the source_version. The pre-filter is a loss point like any other and is measured like any other.

### 5.2 Gate behavior
- Deterministic extraction precedes and constrains the model: dates, authenticated sender (from DKIM/SPF results and directory resolution, never display name), sender role from the profile, explicit request phrasing, attachment metadata
- Triage outcomes: relevant → synthesis; uncertain → stronger model; not relevant → discard (retained, sampled)
- **Suppression floor:** a message with a resolved date and a sender in a role tagged auditor, regulator, or executive cannot be discarded by the model alone; the minimum outcome is uncertain

### 5.3 Measurement
- **Population:** all raw ingested messages per tenant per period, tagged with final stage outcome
- **Operational audit:** 5% of triage discards routed per §4.3; purpose is drift detection, not recall estimation
- **Recall estimation:** a stratified random sample by stage outcome (prefilter_drop, triage_discard, triage_uncertain, surfaced), sized per stratum for a target interval (proposed ±5 points at 95%), labeled with authorized conversation context. Missed-relevant rate per stratum with a Wilson interval, combined by stratum weight. Reported monthly during pilot with the interval, never as a point alone
- **Precision:** surfaced obligations rejected with disposition `not_relevant` divided by surfaced. Other dispositions are separate metrics: duplicate rate, already-completed rate, boundary error rate, field-correction rate
- **Labeled evaluation set:** multi-obligation messages, revisions, cancellations, restatements across threads, benign look-alikes, and the adversarial cases in §6. Re-run on every model, prompt, or pre-filter change with regression gates on recall and precision

### 5.4 Correction for release 2
NVD is ground truth for **ingestion coverage** (every published or modified CVE is captured). It is not ground truth for **applicability**. Inventory-to-vulnerability matching is evaluated against a labeled sample; CPE matches are graded exact / probable / possible, and only exact matches auto-propose.

---

## 6. Untrusted content boundary

All connector content — bodies, headers, attachments, extracted text — is untrusted input. Delimiting content in prompts is hygiene, not a control. The controls:

1. **Authorization and state transitions exist only in application code.** Models return structured proposals. There is no model-callable function that mutates state
2. **Proposal validation** before any proposal is stored: schema-enforced output, unknown fields dropped; owner must resolve to a directory principal in the eligible set; deadline must parse, be no earlier than the source date, and be within 12 months unless a cited passage contains the date; every proposed change must cite at least one passage whose offsets exist in the snapshot and whose quote matches the snapshot text exactly. Any failure rejects the proposal with a logged reason
3. **Cancellation and completion** are always proposals, and are flagged when the authenticated sender is neither the original requester nor a role the profile authorizes to cancel or extend
4. **Suppression floor** per §5.2, plus the sampled discard audit
5. **Rendering:** the desktop shows source content as sanitized text or allow-listed HTML; no scripts, no auto-loaded remote images, links display their true target, attachments open only through explicit user action, no URL is fetched on the user's behalf
6. **Attachment extraction** runs in sandboxed workers with type and size limits; its output is untrusted
7. **Quotes shown to reviewers come from the snapshot via offsets**, never from model output text; model rationale is labeled as model-generated
8. **Prompts and responses are retained** (§3.3) for forensics
9. **Adversarial evaluation cases:** embedded instructions to change deadline or owner, to cancel, or to mark as not relevant; hidden text (color-matched, HTML comments, zero-width); instructions inside attachments; display-name spoofing against the authenticated sender

---

## 7. Ingestion semantics

- **Ordering:** fetch batch → write snapshot bytes to object storage → in one Postgres transaction upsert source_version, deterministic fields, and outbox events → commit → advance checkpoint. A failure after commit and before checkpoint advance causes re-delivery, which the dedup key (tenant, connector, source_id, version) turns into a no-op. Orphaned snapshots are garbage-collected
- **Transactional outbox:** every record change writes an outbox row in the same transaction. A relay publishes to the realtime bus, assigns per-tenant monotonic sequence numbers, and marks rows published. Delivery is at-least-once; consumers are idempotent by event id; the desktop resyncs by sequence
- **Idempotent activities:** Temporal activities may run more than once. Every writing activity upserts on the dedup key; outbound side effects (notifications, reminders) carry idempotency keys
- **Cursor expiry and reset:** Gmail returns an invalid history ID; Graph returns 410 Gone with a resync location. The scope enters `resync_required`, runs a bounded full scan over [last good checkpoint − safety margin, now], reconciles against captured state, and establishes a new cursor. The scope records a `sync_gap` (start, end, recovered count, unrecoverable flag). Obligations created from rescanned items carry `recovered_from_rescan`. Deleted messages and intermediate versions inside the gap may be unrecoverable; the gap is shown in freshness
- **Partial failures:** per-item quarantine with retry and backoff; a batch never fails as a unit; poisoned items appear in connector status
- **Authorization failures:** token revoked → scope enters `reauth_required`, checkpoint preserved, user notified through freshness
- **Rate limits:** backoff with jitter honoring Retry-After; long scans heartbeat
- **Coverage claim:** every change the source still exposes at reconciliation time is captured, and every gap is recorded and visible. Not "never miss"

---

## 8. Desktop client

- Tauri shell: tray icon, hotkey summon, dockable panel
- One inbox (items awaiting the user's review or action, with connector freshness and any sync gaps) and one detail view (passages from snapshot, applicability rationale, review controls, proposals awaiting decision, owner, deadline, completion evidence, history)
- Realtime over WebSocket with sequence-based resync; purge-on-revocation; bounded encrypted cache
- Native OS notifications only for items with a deadline inside a configurable window; recipients resolved at send time
- Rendering per §6.5; no local inference

---

## 9. Backend

- FastAPI API; Temporal workflows for connector polling, reconciliation, gate and synthesis, reminders; Postgres with RLS (§10); S3-compatible object storage for snapshots and completion evidence; Redis for realtime fan-out
- LLM interface as v2 (`classify`, `extract`, `synthesize`), one hosted implementation with a small triage tier and a stronger synthesis tier; every call logs model, prompt version, tokens, cost against tenant and obligation
- Connector interface as v2 (`authenticate`, `checkpoint`, `poll`/`on_webhook`, `normalize` → (source_id, version, acl, content), `reconcile`, plus `on_cursor_invalid`)
- Typed realtime topics and widget manifest schema defined; inbox is the only implementation

---

## 10. Tenant isolation

- RLS on every tenant-scoped table; application role is a non-owner without `BYPASSRLS`; `FORCE ROW LEVEL SECURITY` on tenant tables; tenant and acting principal set per transaction from the authenticated context; workers open sessions the same way

---

## 11. Deployment and roadmap

- Release 1 is multi-tenant cloud only. Boundaries as v2: containerized, config-driven, named egress layer, operations budgeted per shape, VPC and on-prem built when a customer requirement pays for them
- Release 1.x: chat connector on the same app registration. Release 2: NVD with deterministic extraction and graded inventory matching. Release 3: Federal Register then Congress.gov with passage-level document versioning. Platform work interleaved as v2

---

## 12. Frozen technology choices

| Layer | Choice | Note |
|---|---|---|
| Desktop | Tauri 2, TypeScript, React | Tray, global shortcut, multi-window via Tauri plugins |
| Backend | Python 3.12, FastAPI, Pydantic v2 | Pydantic models double as LLM output schemas |
| Workflow | Temporal (pinned server version), hosted for the pilot, self-host path retained | Self-host parity needed for later shapes |
| Database | PostgreSQL 16 with RLS | See §10 |
| Cache / bus | Redis 7 | Realtime fan-out only; Postgres outbox is the source of truth |
| Object storage | S3-compatible | Immutable snapshots, completion evidence |
| Identity | OIDC via a hosted identity-provider integration service | SAML later through the same service |
| Connector family | Microsoft 365 via Graph (mail; Teams in 1.x) | Override to Google Workspace before sprint 1 if the customer base is Google; only the connector module and AC parameters change |
| LLM | One hosted provider offering prompt caching, batch mode, and structured output; small tier for triage, large tier for synthesis | Provider-neutral interface; freeze the provider at sprint 1 |
| Observability | OpenTelemetry traces and metrics; structured logs with tenant and obligation ids | Required for cost-per-obligation and stage metrics |
| Infrastructure | Kubernetes on one cloud provider, Terraform | Single shape for release 1 |

---

## 13. Pilot metrics

- Recall estimate with interval, per stratum and combined (§5.3)
- Precision (`not_relevant` rejections only) and the separate disposition rates
- Median review time per obligation; time from source change to reviewed obligation
- Cost per accepted obligation
- Completion evidence rate among accepted obligations
- Freshness SLO adherence and count/duration of sync gaps
- Proposal validation rejection rate (a rising rate signals injection attempts or model drift)

---

## 14. Open decisions

- Confirm the Microsoft 365 default or override to Google Workspace before sprint 1
- Confirm the three release 1 obligation types and who produces the initial labeled set
- Confirm retention floors and whether tenants may shorten them
- Confirm the discard audit rate and the self-audit cadence for private mailboxes
- Choose the identity-provider integration service and the LLM provider at sprint 1
- Desktop idle state: count awaiting review vs. single most urgent item
