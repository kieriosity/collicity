> **Superseded, reference only.** Archived 2026-10-05; replaced by the Collicity product specification. See `README.md` in this folder for status and reading order.

# Compliance Assistant Platform — Architecture & Design Plan

**Status:** v3.1 — pilot basis, specification corrections only; architecture frozen
**Companion:** `acceptance-cases-release-1-v3-1.md`
**Domain sequence:** internal communications → vulnerabilities → regulatory change

---

## 0. What changed from v3

Corrections, no architectural change:

- **Pending private evidence has its own visibility.** Attach proposals, their passages, proposed changes, rationale, and history entries are visible only to principals authorized for both the obligation and the new evidence; other viewers see no indication. Only those principals may decide the attachment
- **Current source ACL separated from capture-time snapshot.** Visibility derives from `source_object.current_acl`; `acl_snapshot` is audit evidence only. Revocation works without any content change or new capture
- **Precision and recall redefined** at the obligation level with adjudicated denominators; recall as a ratio of weighted detected and missed obligations with a stratified bootstrap interval; revision-linking accuracy added
- **Citation integrity and semantic support separated.** Integrity (exists, matches, not concealed) is a deterministic check; support is evaluated model behavior; hidden-in-original content is preserved as metadata and shown with a marker
- **Recovery defined over the complete monitored scope** plus reconciliation of every previously captured object; the coverage claim is narrowed to the configured monitored scope
- **Scope is in the dedup key everywhere**; `content_identity` handles the same message captured in more than one scope
- **Proposal states:** `pending / blocked / invalid / accepted / rejected / stale`; `base_revision` on every proposal; stale detection on accept
- **Suppression floor → mandatory human review**, with a dedicated stage outcome
- **Duplicate handling always preserves the candidate obligation**
- **Assignment status separated from lifecycle status**; reassignment applies only to active obligations
- **Authority predicates defined separately** for cancel, extend, and complete, per obligation type
- **Mail authentication separated from sender identity and identity confidence**

---

## 1. First-release thesis

Unchanged from v3. Buyer: compliance practitioner or team lead whose obligations arrive through email and chat. Release 1 obligation types: deadline-bearing requests, acknowledgment requests, evidence requests. Personal scope: an obligation is visible only to principals who can currently read every source it rests on.

## 2. Goals and non-goals

Unchanged from v3.

---

## 3. Domain model

### 3.1 Entities

**source_object** — one monitored object in one scope

| Field | Notes |
| --- | --- |
| tenant, connector, scope, source_id | scope = mailbox or folder or channel as the connector defines it |
| content_identity | provider-stable identity across scopes (Internet Message-ID; Gmail message id; chat message id) |
| current_acl | mutable; updated by membership events, reconciliation, and capture. The only ACL used for access decisions |
| deleted_at_source | set by reconciliation; captures are retained |

**source_version** — one immutable capture of a source_object

| Field | Notes |
| --- | --- |
| version | etag / change key / history-derived state / content hash |
| captured_at, snapshot_ref | immutable in object storage |
| acl_snapshot | who could read at capture; audit evidence only, never used for access decisions |
| conversation_id | thread grouping from the connector |
| deterministic_fields | dates, attachment metadata, explicit request phrasing hits |
| mail_auth | SPF, DKIM (signing domain), DMARC result and alignment. Authenticates domains and servers, not people |
| sender_identity | resolved principal (internal user via connector identity) or profile contact; `identity_confidence` high / medium / low (see §6.10) |
| content_flags | per region: hidden-in-original (display:none, color-matched, zero-size, zero-width characters, comments), from the original markup, kept separately from displayed text |
| stage_outcome | prefilter_drop / triage_discard / triage_uncertain / floor_surfaced / surfaced, with stage versions |

**obligation**

| Field | Notes |
| --- | --- |
| id, tenant, type | |
| title, summary | |
| status | proposed → accepted → in_progress → completed; terminal alternatives rejected, cancelled, merged |
| assignment_status | none / assigned / needs_reassignment. Meaningful only while status is active (proposed, accepted, in_progress) |
| owner, deadline | owner is retained as history on terminal obligations |
| approved_fields | fields a human has explicitly accepted |
| revision | monotonic per obligation; per-field revision map |
| visibility_set | computed per §4.1 |
| history | append-only; each entry carries its own visibility set |

**evidence_link** — many-to-many between obligation and source_version

| Field | Notes |
| --- | --- |
| link_type | creates / revises / cancels / completes / references |
| passages | (start, end, quote) into the snapshot, with the region's content_flags |
| extraction | method, model and prompt version, confidence |
| proposed_changes | structured |
| state | pending / accepted / rejected |

**proposal** — any change that requires review

| Field | Notes |
| --- | --- |
| obligation_id, evidence_link_id | |
| kind | field_change / cancel / complete / attach_evidence / merge_into / assign |
| field, current_value, proposed_value, consequence | |
| base_revision | obligation (and field) revision at proposal creation |
| visibility_set | obligation visibility ∩ new evidence current_acl for attach_evidence; obligation visibility otherwise |
| state | pending / blocked (reason; stored, shown, not acceptable until the condition changes) / invalid (reason; stored for forensics, not actionable) / accepted / rejected / stale |
| flags | e.g., sender_not_authorized, not_owner, identity_low_confidence |
| decided_by, decided_at, rationale | |

**review_decision** — unchanged from v3 (decision accept / reject / modify; dispositions not_relevant / duplicate_of / already_completed / wrong_boundary_split / wrong_boundary_merge / out_of_scope_type; corrected_fields on modify).

**organization_profile** — v3 plus:

- `authority` per obligation type: `cancel_roles`, `extend_roles`, `complete_roles`. Defaults: cancel and extend = original requester or listed roles; complete = current owner. Any other actor is flagged, never blocked from proposing
- external contacts with their domains, used by identity resolution

### 3.2 Rules

- One source version may create many obligations; one obligation may rest on many source versions
- Unapproved fields update directly from accepted evidence; approved fields change only through an accepted proposal
- `cancels` and `completes` links always produce proposals; never auto-applied
- **Duplicates:** the candidate obligation is always created. High match confidence attaches a `merge_into` proposal; accepting sets the candidate to `merged` and adds a `references` link on the original; rejecting leaves the candidate as a normal proposed obligation. A reviewer rejecting a low-confidence candidate with `duplicate_of` produces the same merged effect
- **Stale proposals:** on accept, if the obligation's current field revision differs from `base_revision`, the proposal becomes `stale`; it is re-presented with the current value and requires a renewed decision. A stale proposal never overwrites a newer decision
- **Authority:** cancel, extend, and complete are checked against the profile's authority predicates using `sender_identity` at ≥ medium confidence. Failures flag; they do not block the proposal
- **Cross-scope identity:** a second capture of the same `content_identity` in another scope produces a `references` link on obligations already created from the first, not new obligations
- Conversation context for triage and synthesis: bounded (10 messages or 30 days), restricted to sources within the acting visibility set
- Worked example unchanged from v3: two obligations from "Send A and B by Friday"; a `revises` link with a deadline proposal when "A can wait until Tuesday" arrives

### 3.3 Retention

Unchanged from v3: floors of 90 days for items not surfaced and obligation lifetime plus one year for surfaced items; applies to dropped and discarded items, prompts, and responses.

---

## 4. Visibility, assignment, audit, revocation

### 4.1 Visibility set

- Intersection of `current_acl` across the `source_object`s of all evidence links in state `accepted`
- Recomputed on any `current_acl` change (membership event, reconciliation, capture) and on any link state change
- `acl_snapshot` is never consulted for access

### 4.2 Pending evidence

- An `attach_evidence` proposal, its evidence link, passages, proposed changes, rationale, and every history entry produced for it carry `proposal.visibility_set` = obligation visibility ∩ new evidence `current_acl`
- Principals outside that set see the obligation exactly as before: no proposal, no passage, no changed field, no history entry, no count. Existence is not disclosed
- Only principals in that set may decide the proposal
- Pending links do not shrink the obligation's visibility; accepted links do

### 4.3 Assignment under personal scope

- Eligible owners = visibility set ∩ active users
- An owner proposal outside the eligible set is stored in state `blocked` with reason "no access to source(s) …"; it cannot be accepted; the reviewer sees the reason and the eligible list. A blocked proposal is re-evaluated when visibility changes and may become `pending`
- Direct assignment through the API to an ineligible principal is rejected with the same reason
- An accepted attach proposal that removes the current owner from the visibility set sets `assignment_status = needs_reassignment` and notifies reviewers in the new visibility set; lifecycle `status` is unchanged

### 4.4 Discard audit under personal scope

Unchanged from v3: auditors see only items whose sources they can currently read; private-mailbox items are self-audited by the owner (bounded weekly task) or by a delegate; recall reporting annotates coverage per stratum.

### 4.5 Revocation

- Triggers: a membership or permission event from the connector (no content change, no new source_version); a reconciliation that observes a changed ACL; a capture whose ACL differs
- Effects: recompute visibility → emit `access.revoked` to affected sessions → clients purge the obligation and all related content from cache immediately and on every full resync → the notification dispatcher checks visibility at send time and drops queued items for revoked principals → if the revoked principal is the owner of an **active** obligation, `assignment_status = needs_reassignment` and reviewers in the new visibility set are notified
- Completed, cancelled, merged, and rejected obligations keep their status and historical owner; only visibility changes
- Every effect is audit-logged

---

## 5. Detection pipeline and measurement

### 5.1 Stages

```
ingest → normalize → deterministic pre-filter → floor check → model triage → synthesis → review
```

Each stage records outcome and version on the source_version. The pre-filter is measured like every other loss point.

### 5.2 Gate behavior

- Deterministic extraction precedes and constrains the model; sender role comes from `sender_identity` and the profile, never from display name or mail authentication alone
- Triage outcomes: relevant → synthesis; uncertain → stronger model; not relevant → discard (retained, sampled)
- **Suppression floor:** a message whose `sender_identity` (≥ medium confidence) is in a role tagged auditor, regulator, or executive **and** that carries a resolved date is assigned stage outcome `floor_surfaced` and goes to mandatory human review regardless of any model's opinion. Model outputs are attached as advisory and the item sits in a low-priority review queue. A role match at low identity confidence does not trigger the floor; the item is flagged `identity_low_confidence` and routed to `triage_uncertain`

### 5.3 Measurement

**Units.** The sampling unit is the message (source_version). The counting unit is the obligation. Labelers, working with authorized conversation context, enumerate every relevant obligation in a sampled message. System extractions from that message are matched one-to-one to labeled obligations by the labeler. A labeled obligation with no match is a miss; extracting one of two requests registers one detection and one miss.

**Precision** (surfaced obligations only)

- Adjudicated = surfaced obligations with a reviewer decision; pending are excluded and their count is reported
- Relevant = accepted, or rejected with `duplicate_of`, `already_completed`, `wrong_boundary_split`, `wrong_boundary_merge`, or modified
- Non-relevant = rejected with `not_relevant` or `out_of_scope_type`
- Precision = relevant / adjudicated. The disposition rates (duplicate, already completed, boundary, field correction, out of scope) are reported separately

**Recall** (all ingested messages)

- Strata by stage outcome: `prefilter_drop`, `triage_discard`, `triage_uncertain`, `floor_surfaced`, `surfaced`. Stratified random sample; weight w_h = N_h / n_h
- Detected_w = Σ_h w_h × (matched labeled obligations in sampled messages of h); Missed_w = Σ_h w_h × (unmatched labeled obligations)
- Recall = Detected_w / (Detected_w + Missed_w)
- Interval: stratified bootstrap — resample messages with replacement within each stratum, recompute recall, 2,000 replicates, percentile 2.5 and 97.5. Per-stratum Wilson intervals are reported for the miss proportions as diagnostics only and are never combined into the recall interval
- Sample sizing: minimum 100 messages per stratum (or all, if fewer) in the first cycle; subsequent cycles sized from the observed bootstrap variance to a target half-width of 5 points

**Revision linking.** Among labeled revising, cancelling, and completing messages in the sample, the fraction the system linked to the correct existing obligation. Reported with a bootstrap interval on the same sample.

**Operational audit.** 5% of `triage_discard` routed per §4.4 for drift detection. Not used for the recall estimate.

**Labeled evaluation set.** Multi-obligation messages, revisions, cancellations, cross-thread restatements, benign look-alikes, valid quotes with misleading meaning, and the adversarial cases in §6.9. Re-run on every model, prompt, or pre-filter change with regression gates on recall, precision, and revision linking.

### 5.4 Release 2 note

Unchanged from v3: NVD is ground truth for ingestion coverage, not applicability; matches graded exact / probable / possible.

---

## 6. Untrusted content boundary

All connector content is untrusted input. The controls are permissions and approval requirements enforced in application code; everything the model does is proposal.

1. **State transitions and authorization live only in application code.** No model-callable function mutates state
2. **Citation integrity** (deterministic, before a proposal is stored): output matches schema, unknown fields dropped; every proposed change cites at least one passage whose offsets exist in the snapshot, whose quote matches the snapshot text exactly, and whose region is not flagged hidden-in-original. Failure stores the proposal as `invalid` with the reason. Integrity establishes that the text exists and was not concealed; it does not establish that the text supports the change
3. **Semantic support is evaluated model behavior, not a control.** The labeled set includes exact quotes whose surrounding sentence negates or contradicts the proposed change. The measured rate gates model and prompt releases. What prevents harm is that approved fields change only by human acceptance and that cancel, complete, and extend are always proposals
4. **Owner and deadline validation:** owner must resolve to a principal in the eligible set, else `blocked`; deadline must parse, be no earlier than the source date, and fall within 12 months unless a cited passage contains the date, else `invalid`
5. **Authority flags:** cancel and extend proposals are flagged `sender_not_authorized` when `sender_identity` is neither the original requester nor a role in the type's `cancel_roles` / `extend_roles`; complete proposals are flagged `not_owner` when the sender is not the current owner. Flags never block
6. **Suppression floor** per §5.2 plus the sampled audit
7. **Rendering:** sanitized text or allow-listed HTML; hidden-in-original regions are displayed with a "hidden in original" marker; original rendering metadata is stored with the snapshot and never overwritten by the displayed form; no scripts, no auto-loaded remote images, links show true targets, attachments open only on explicit user action, no URL is fetched on the user's behalf
8. **Attachment extraction** in sandboxed workers with type and size limits; output is untrusted and carries its own content_flags
9. **Adversarial evaluation cases:** embedded instructions to change deadline or owner, to cancel, to complete, or to mark as not relevant; hidden text of each kind in `content_flags`; instructions in attachments; display-name spoofing; exact quotes with misleading meaning
10. **Identity:** `mail_auth` records SPF, DKIM signing domain, DMARC result and alignment; it authenticates domains and infrastructure. `sender_identity` is resolved separately: high = internal user identified by the connector; medium = external address matching a profile contact with DMARC-aligned domain; low = anything else, including display-name matches. Role checks, the floor, and authority predicates require ≥ medium; low confidence flags and never satisfies a predicate
11. **Quotes shown to reviewers come from the snapshot via offsets**, never from model output text
12. **Prompts and responses retained** per §3.3

---

## 7. Ingestion semantics

- **Dedup key:** (tenant, connector, scope, source_id, version). Same everywhere
- **Ordering:** fetch batch → write snapshot bytes → in one Postgres transaction upsert source_object (including `current_acl`), source_version, deterministic fields, outbox events → commit → advance checkpoint. Re-delivery after a failure between commit and checkpoint advance is a no-op under the dedup key
- **Cross-scope captures:** the same message in two scopes (Graph delta is per folder) yields two source_versions with one `content_identity`; obligation creation dedups on `content_identity` per §3.2
- **Transactional outbox, idempotent activities, partial failures, authorization failures, rate limits:** unchanged from v3
- **Monitored scope** is configured per scope: the folder or channel set and a monitored window (default: objects received within the last 90 days; Graph date filtering is on received time, per folder). Objects outside the window are outside the coverage claim by definition
- **Cursor expiry and reset** (Gmail invalid history ID; Graph 410 Gone with resync location): the scope enters `resync_required` and performs two steps before returning to `healthy`:
  1. **Forward scan** over the complete monitored scope — every folder or channel in the scope, over the full monitored window — capturing anything not already present under the dedup key
  2. **Reconciliation of captured objects** — every previously captured `source_object` in the scope is fetched directly (batched): a changed version is captured as a new source_version; an absent object is marked `deleted_at_source`; `current_acl` is refreshed
     A `sync_gap` records start, end, recovered count, and `unrecoverable = true` when intermediate versions between the last capture and now cannot be reconstructed. Obligations created from step 1 carry `recovered_from_rescan`. The gap is shown in freshness
- **Scheduled reconciliation** runs step 2 on its own cadence (daily for mailboxes, weekly for archives) independent of cursor resets; it is also how ACL changes without connector events are observed
- **Coverage claim:** within the configured monitored scope, every change the source exposes at reconciliation time is captured, and every gap is recorded and visible

---

## 8. Desktop client

Unchanged from v3, plus: hidden-in-original markers in the detail view; proposals and history rendered only within the viewer's visibility; stale proposals re-presented with current and proposed values side by side.

## 9. Backend

Unchanged from v3, plus: connector interface gains `on_acl_event` (membership and permission changes without content) and `fetch_objects(ids)` for reconciliation.

## 10. Tenant isolation

Unchanged from v3.

## 11. Deployment and roadmap

Unchanged from v3.

## 12. Frozen technology choices

Unchanged from v3.

---

## 13. Pilot metrics

- Recall with stratified bootstrap interval; per-stratum miss proportions as diagnostics
- Precision on adjudicated surfaced obligations; pending count; disposition rates reported separately
- Revision-linking accuracy with interval
- Median review time; time from source change to reviewed obligation
- Cost per accepted obligation
- Completion evidence rate among accepted obligations
- Freshness SLO adherence; count and duration of sync gaps; reconciliation lag
- Proposal state rates: blocked, invalid, stale — rising invalid signals injection attempts or model drift; rising stale signals review contention

---

## 14. Open decisions

- Confirm the Microsoft 365 default or override to Google Workspace before sprint 1
- Confirm the three release 1 obligation types, the authority defaults per type, and who produces the labeled set
- Confirm the default monitored window (90 days) and whether tenants may widen it
- Confirm identity-confidence thresholds for external contacts
- Confirm retention floors, audit rate, and self-audit cadence
- Choose the identity-provider integration service and the LLM provider at sprint 1
- Desktop idle state
