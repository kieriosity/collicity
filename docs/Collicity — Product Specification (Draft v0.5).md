# Collicity — Product Specification (Draft v0.5)

Oct 5, 2026 · @Philip Maynard

## 1. Overview

Collicity is a cross-platform AI automation workspace: people describe work in plain language, and Collicity turns it into named, budgeted, scheduled **assignments** made of **tasks** that act across their apps — always with the user's own identity and never beyond the user's own permissions.

It ships as two editions on one codebase:

- **Home** — personal and household automation (meal planning, shopping, smart-home routines), with consumer sign-in.
- **Enterprise** — team and departmental automation (e.g., a security posture report built from Qualys, Splunk and Intune), with central administration, SSO, model governance and security monitoring.

**Problem.** AI assistants answer questions but rarely finish multi-step work across systems. Tools that do automate (iPaaS, RPA, agent frameworks) usually run as over-privileged service accounts, reveal cost only on the invoice, and give no signal when the model is wrong. Collicity's bet is that delegated identity, hard budgets and a supervising quality loop are what make people trust an agent with real work.

**Differentiators** (each has its own section):

1. Zero-trust connectors that act strictly on behalf of the user (§6).
2. Cost-first design: estimates before running, hard budget caps, cheapest-capable-model routing (§8, §9).
3. A supervisor that catches bad output and learns which models work for which tasks (§9).
4. Templates that keep recurring outputs consistent (§10).
5. Time, event and location triggers across desktop, web and mobile (§11).

**Conventions.** MUST = required for the edition's launch; SHOULD = expected, may slip a release; MAY = optional. Priorities: P0 = essential, P1 = fast follow, P2 = design for it now, build later. When each requirement ships is set by the requirement matrix in §18, which overrides a P-level where they differ. Requirement IDs (e.g., CON-03) are stable for traceability. Numeric targets are proposals to validate.

### Product invariants

Five rules hold in every edition, client and connector. Each is enforced outside the model, has its own automated test suite and a zero-target guardrail in §19; weakening one requires a security review.

1. **Permission ceiling.** Effective permission = the user's own access ∩ scopes the user consented to ∩ capabilities the task declared (action, resources, fields, recipients, parameters) ∩ admin policy, enforced at the connector gateway, never by the model (CON-02).
2. **No action without authorization.** Every connector action traces to an authorization: the owner's acceptance of the assignment version for its declared reads; for an access check, the person's own start of the precheck, which authorizes only reads of their own access (ASG-21); and an approval or a still-valid pre-authorization for every side effect (ASG-11, PAY-02, PAY-03). An approval or pre-authorization also covers the declared compensation of the action it authorizes, even after it has been used, has expired or has been revoked, so compensations are authorized too (RUN-06).
3. **No credential reaches a model.** Tokens, keys and secrets never enter prompts, model outputs, logs or third parties (CON-04, SEC-03).
4. **No billable operation without reserved budget.** No model call, paid API call, compute or fee starts until its worst-case cost is reserved against every applicable budget (BUD-03, BUD-10).
5. **Untrusted content cannot expand authority.** Data from connectors, documents, emails or web pages, and anything a model derives from it, can never add permissions, connectors, recipients, budget or approvals, or become standing instructions (CON-10, SEC-12).

## 2. Goals and non-goals

v1 succeeds if people trust Collicity to finish recurring work end to end, with zero budget overruns and zero permission violations. Targets are measured 6 months after launch.

| # | Goal | Measure | Proposed target |
| --- | --- | --- | --- |
| G1 | People finish real multi-step work, not just chat | Active users with ≥1 assignment that completed ≥3 successful runs | 40% |
| G2 | No spend surprises | Runs that exceed their authorized budget | 0 (hard invariant) |
| G3 | Cheaper than "always use the best model" | Net cost per verified-successful task (worker + supervision + retries) vs. a frontier-model-only baseline | ≥40% lower |
| G4 | Trustworthy output | Escaped defects per 100 delivered runs, estimated from the independent evaluation set (SUP-06) | <2 |
| G5 | Every agent action is attributable | Connector calls traceable to a named user in Collicity's audit log (and in the target's log where it supports delegation) | 100% |

**Non-goals for v1**

- **Training our own foundation models** — Collicity routes across providers; its value is orchestration, trust and cost control.
- **A general iPaaS or low-code app builder** — assignments are AI-driven tasks, not arbitrary integration plumbing.
- **Acting with more access than the user** — no shared "super" service accounts for convenience (narrow, governed exception in CON-05).
- **Financial activity beyond purchases** — no investing, transfers between accounts or bill pay.
- **Offline execution** — clients can view outputs and queue approvals offline; runs need the cloud runtime.
- **On-device model inference as the main path** — local models are a P2 option for desktop.

## 3. Personas and user stories

Seven personas share one product; the home user and the enterprise analyst drive the core loop, the rest govern or extend it.

| Persona | Edition | Primary job |
| --- | --- | --- |
| Home user | Home | Offload recurring household planning and errands |
| Household member | Home | Give preferences; receive shared reports and assignments (§7.7); approve lists if several approvers are allowed (§21) |
| Knowledge worker / analyst | Enterprise | Automate recurring reports and request triage across enterprise tools |
| Report consumer (manager) | Enterprise | Read and question outputs without building them |
| Enterprise admin | Enterprise | Govern users, budgets, models and connectors |
| Security and compliance officer | Enterprise | Monitor agent activity, investigate, prove compliance |
| Connector developer | Both | Build and publish custom connectors |

**Stories, highest priority first**

Home user

1. As a home user, I want to say "plan our meals for the week" and get a ready-to-run assignment with its estimated cost, so I don't have to design the workflow.
2. As a home user, I want to approve the meal plan and edit the shopping list before anything is bought, so nothing arrives that I didn't want.
3. As a home user, I want purchases to happen automatically only within a limit I set, so I stay in control of my money.
4. As a home user, I want dinner ordered and my home readied as I get close to home, so both are ready when I arrive.

Enterprise analyst and consumer

1. As a security analyst, I want an assignment that pulls from Qualys, Splunk and Intune every Monday and refreshes my posture report in the same format, so management gets a consistent weekly view.
2. As an operations employee, I want requests from specific senders turned into a work queue with suggested actions, so nothing falls through the cracks.
3. As a manager, I want to ask "why did critical vulnerabilities rise this week?" against the report, so I get the explanation without asking the analyst.

Admin and security

1. As an enterprise admin, I want budgets per department and per user, so AI spend stays within plan.
2. As an enterprise admin, I want to allow or block specific models and providers, so data only reaches approved vendors.
3. As an enterprise admin, I want model performance and cost by task type, so I can see where money is well spent.
4. As a security officer, I want every agent action logged with the human it acted for, the model used and the approval trail, so I can investigate incidents.
5. As a security officer, I want to suspend a user, which revokes their connector tokens and stops their agents in one action (CON-06), so I can contain an incident.

Connector developer

1. As a connector developer, I want to wrap an internal API as a connector with declared scopes and actions, so admins can approve exactly what it can do.

Edge cases

1. As a user whose run hits its budget mid-way, I want it paused at a safe point with a one-tap "approve more", so work isn't lost and spend isn't exceeded.
2. As a user whose access to a system was removed, I want assignments that depend on it to stop and tell me why, rather than fail silently or retry.

## 4. Core concepts

Everything in Collicity is an assignment made of tasks, started by a trigger, paid for from a budget and executed through connectors.

| Concept | Definition | Key attributes |
| --- | --- | --- |
| Task | Smallest unit of work an agent performs | Name; description (user- or AI-written); input/output schema; capability grants (CON-02); model or "Auto"; success criteria; approval gate; timeout; retry policy |
| Assignment | Named, versioned workflow of tasks: sequence, branches, parallel steps, for-each loops | Owner; task graph; triggers; budgets and alert thresholds; template bindings; sharing |
| Run | One execution of an assignment, pinned to the version it started on | Status; step log; estimated vs. actual cost; models used; supervisor verdicts; outputs |
| Trigger | What starts a run | Manual, schedule, event (email, webhook, connector event), proximity (geofence), chained (another assignment finished) |
| Human step | A task that waits for a person | Input form, approval, editable list, choice; timeout and escalation |
| Approval gate | Required confirmation before a risky action | Default on for payments, external sends, deletes, permission or settings changes |
| Work queue | Items an assignment creates for people to act on | Source; parsed request; suggested actions; assignee; due date; status |
| Connector | Governed integration with an external system | Type (MCP, API, knowledge source, device-local, browser); auth method; declared scopes and actions; data classification |
| Template | Versioned output definition with data bindings | Sections, tables, charts, narrative blocks, style, export formats |
| Budget | Spending authority on an assignment, or on a person's workspace or chat (BUD-11), and on user/org in Enterprise | AI budget and a separate purchase budget; period; alert thresholds; hard stop |
| Model performance inventory | The supervisor's record of how each model performs per task type | Verified-success rate (INV-06), evidence by class, cost per verified success, latency, sample size |

**Object rules**

- Assignments and templates are versioned; editing never changes a run already in flight. An assignment version pins the versions of the library tasks and templates it uses.
- Every object has an owner, an access list (private, shared with people or groups, org-wide) and a data classification label.
- Tasks can live in a reusable task library and be shared across assignments (P1).
- Sharing an assignment shares its definition, never its runs, outputs or the owner's access (§7.7). Until GA, assignments are private; other people take part only as delegate approvers or custodians (§18.1) or through published snapshots (SEC-10).

## 5. Platforms and clients

All six platforms ship, but they don't do the same jobs: execution lives in the cloud, clients author, approve, chat and monitor, and mobile adds location triggers and on-device home control.

| Capability | Web | Windows / macOS / Linux | iOS / Android |
| --- | --- | --- | --- |
| Build and edit assignments, tasks, templates | Full | Full | Edit and AI builder; full visual editing P1 |
| Chat and ask-anything Q&A | Yes | Yes | Yes, plus voice (P1) |
| Approvals and human steps | Yes | Yes, with OS notifications | Yes, actionable push; biometric confirm for payments |
| Run monitoring and budgets | Yes | Yes | Yes |
| Proximity (geofence) triggers | — | — | Yes, background location |
| Local connectors | — | Yes: local files, desktop apps, local MCP servers | Apple HomeKit, Shortcuts / App Intents, Android intents |
| Enterprise admin console | Yes | Yes | Read-only dashboards and kill switch (P1) |
| Offline | — | View cached outputs, queue approvals | View cached outputs, queue approvals |

**Requirements**

- **PLT-01 (P0)** One shared design system; the core loop (create → run → approve → review) works on all six platforms.
- **PLT-02 (P0)** The desktop app hosts local connectors in a sandboxed process with per-connector permissions. Local connectors run only while the device is on and signed in; the scheduler shows when a run depends on one.
- **PLT-03 (P0)** Mobile supports actionable push approvals; payment approvals require device biometrics (Face ID / Touch ID / Android BiometricPrompt).
- **PLT-04 (P0)** Geofences are evaluated on the device; only the "trigger fired" event reaches the cloud, never a location trail.
- **PLT-05 (P1)** Enterprise device management: managed app configuration and app protection policies (Intune, Jamf), managed distribution.
- **PLT-06 (P0)** WCAG 2.2 AA on every client.

**Stack (decided 2026-10-05; detail in *Collicity — Technical Design*).** Web and desktop share one React + TypeScript codebase, a Vite single-page app built on React Aria Components for accessibility (PLT-06). Desktop runs it in a Tauri 2 shell; a one-week spike on WebKitGTK and screen readers confirms the shell at the start of Phase 2, with Electron as the fallback. The local-connector sandbox (PLT-02) is a Rust sidecar under either shell. Mobile uses React Native with Expo, chosen for its mature background geofencing, actionable push and biometrics; custom native modules add HomeKit, App Intents and approvals signed with a device key. The backend building blocks are in §16.

## 6. Connectors and zero-trust delegation

A connector can only do what the signed-in user could do themselves, using credentials issued to that user, scoped to the task and released only for the step that needs them — and every call is labeled as Collicity acting on that user's behalf.

### 6.1 Connector types

| Type | Examples | Notes |
| --- | --- | --- |
| Remote MCP server | Vendor-hosted MCP servers (Atlassian, GitHub) | OAuth 2.1 per the MCP authorization spec |
| API connector (REST / GraphQL) | Qualys, Microsoft Graph (Intune, Outlook), Splunk REST | In the beta, first-party ones run in-process in the connector gateway with MCP-shaped tool schemas; custom ones are built with the connector SDK and use MCP transport (CON-12) |
| Knowledge (RAG) source | SharePoint, Google Drive, Confluence | Permission-aware retrieval (CON-09) |
| Device-local | Local files and apps, local MCP servers, Apple HomeKit | Runs only on the user's device |
| Event source | Mailbox, webhooks, connector change feeds | Feeds triggers (§11) |
| Browser automation (P2, gated) | Sites with no API | Highest risk; only if approved (§21) |

**Proposed launch catalog.** Enterprise: Microsoft 365 (Outlook, SharePoint, Teams, Excel), Google Workspace, Slack, Jira / Confluence, ServiceNow, Splunk, Qualys, Microsoft Intune, CrowdStrike, Salesforce. Home: Gmail / Outlook.com, Google and Apple calendars, Google Home, SmartThings, Home Assistant, Apple HomeKit (on-device), a grocery partner, a food-delivery partner, maps.

### 6.2 Zero-trust requirements

![On-behalf-of call flow · one check, one token, one audit record](images/on-behalf-of-call-flow.png)

The policy check happens before the token broker releases any credential for the call, so a task that drifts outside its ceiling never holds a token that could reach the target.

- **CON-01 (P0) Delegated identity only.** Connectors call target systems with tokens issued to the user — OAuth 2.1 authorization code with PKCE, or derived from the user's session via OAuth token exchange (RFC 8693) or the IdP's on-behalf-of flow (e.g., Entra ID OBO). Never with a shared Collicity identity.
- **CON-02 (P0) Permission ceiling.** Effective permission for any call = the user's own access in the target ∩ scopes the user consented to ∩ capabilities the task declared ∩ admin policy. A capability is more than an action name: it is a connector, action, resource selector, field allowlist, recipient rule, parameter limits and volume limit (table below), so read access to one folder can never grow into a whole mailbox without a visible change. A deterministic policy evaluator at the connector gateway checks each request before the call and filters each response to the granted resources and fields after it; the model is never the enforcement point. In the beta this is a typed grant evaluator; Cedar arrives at GA with policies as code (ADM-08), custom connectors (CON-15) and device-local and on-prem relay actions (§16). Connector manifests declare which parameters identify resources and recipients; an action whose resource can't be determined before the call needs approval every time. Widening any element is a new assignment version that needs re-acceptance (RUN-03). A write's grant also covers its declared compensation, which the gateway allows only to reverse an effect the run applied and doesn't count against the volume limit (RUN-06). A capability's grant also covers its declared access check (CON-12), which reads only the requesting person's own access to the capability's resources and runs only for a precheck (ASG-21). A resource selector is fixed, naming one resource such as table Stock in Inventory.xlsx on a team site, or personal, relative to the person the assignment runs for, such as the Requests folder in their own mailbox (ASG-20).
- **CON-03 (P0) No escalation attempts.** The gateway blocks any call outside the task's capability grants (CON-02) and never requests scopes beyond the connector manifest. A denied call (401/403) ends the step; it is never retried with other credentials, scopes or methods. Repeated out-of-policy attempts auto-suspend the task and raise a security event.
- **CON-04 (P0) Step-scoped grants and upstream tokens.** Collicity's own execution grant is short-lived: the gateway issues it for one step, and it expires when the step ends. Upstream access-token lifetime is set by the target's identity provider and is connector-specific: Microsoft Entra ID defaults to 60–90 minutes, and up to 28 hours for CAE sessions it can revoke in near real time. Upstream tokens therefore stay inside the token broker, are used only for granted calls, and are cut off early through revocation signals such as Continuous Access Evaluation where supported; each connector documents its token lifetime. They are audience-restricted (RFC 8707 resource indicators) and sender-constrained via DPoP (RFC 9449) or mTLS (RFC 8705) where the target supports it. Tokens never reach a model, a log or a third party. Refresh tokens are sealed in an HSM-backed vault with per-tenant keys, rotated, and readable only by the token broker in the connector gateway.
- **CON-05 (P0) Systems without OAuth.** Where a target only takes API keys or basic auth, the user stores their own personal credential in the vault. Admin-approved service accounts are allowed only under an explicit enterprise policy with per-call user attribution, a visible "not user-delegated" badge, and a live entitlement check: before every call, the gateway verifies the named user's current entitlement to the specific resource, through the target's permission API or group membership read from the system of record no more than 15 minutes earlier. If that can't be verified, the call is denied. A documented mapping alone never satisfies the ceiling, and targets with no way to check entitlements can't use the exception. *(Decided 2026-10-05.)*
- **CON-06 (P0) Continuous verification.** Each call re-checks that the user is still active (IdP / SCIM), session risk signals where the IdP offers them (e.g., Entra Continuous Access Evaluation), device posture for device-bound actions, and that the connector is still approved. Deprovisioning revokes all tokens and pauses runs within 60 seconds of Collicity receiving the deprovisioning signal: a SCIM event or an admin suspending the user (ADM-06). The bound is measured from receipt because the signal can arrive long after the IdP disables the user: Microsoft Entra ID provisions through SCIM about every 40 minutes. Targets that support Continuous Access Evaluation (CAE) also reject a disabled user's tokens on their own, although CAE critical events can take up to 15 minutes to reach them; a CAE challenge by itself moves the run to Paused: re-authorization (RUN-04), because it doesn't say why access changed. A tenant that needs a tighter bound can allow admin-consented polling of its directory for disabled accounts (for Entra ID, Graph's user delta query) at least every 30 seconds; each disabled account the poll finds is a deprovisioning signal. The poll runs under an application permission the tenant admin grants, reads only account status and acts on no one's data, so CON-01 still holds. Lifting a suspension restores sign-in only: the user re-consents to connectors, runs that ended stay ended, and repair items stay with the custodian.
- **CON-07 (P0) On-behalf-of attribution.** Every outbound call carries (a) the user's identity in the token, with Collicity as the actor (the RFC 8693 `act` claim) where supported; (b) a `User-Agent` of the form `Collicity/<version> (agent; run=<run-id>)` plus the target's own audit or on-behalf-of field where one exists; and (c) a matching Collicity audit record: user, connector, action, resource, run, task, model, approval, approver and result. Each connector's docs state what the target's own log will show.
- **CON-08 (P0) Consent and visibility.** Per connector, users see granted scopes, last use and which assignments depend on it, with one-click revoke. Enterprise admins see the same across the tenant.
- **CON-09 (P0) Permission-aware retrieval.** RAG returns only content the requesting user can access at query time. Prefer federated search with the user's token; where an index is unavoidable, store source permissions with each chunk and re-verify against the source before content enters a prompt.
- **CON-10 (P0) Connector data is untrusted.** Content returned by a connector cannot change a task's permissions, add connectors, satisfy approvals or issue instructions the runtime obeys (prompt-injection defense, §15). Connectors record regions hidden in the original (display:none, text matching its background, zero-size or zero-width text, comments); these regions are left out of what the model sees, reviewers still see them with a "hidden in original" marker, and any proposed change that cites only hidden text is invalid. Hidden content includes attachments: hidden sheets, rows, columns and cells in spreadsheets, hidden text and comments in documents, text matching its background in PDFs, and document metadata. Before a model sees untrusted text, it is normalized (Unicode NFKC). In what the model sees, Unicode tag characters and bidirectional overrides are flagged and removed, and mixed-script look-alikes are flagged and mapped to their Unicode skeleton (UTS #39); the original stays in the snapshot. Encoded payloads (Base64, hex, URL encoding) are decoded for inspection and flagged. Untrusted content is also delimited and labeled in prompts, as basic hygiene only; labels are not a control.
- **CON-11 (P1) Data classification.** Connectors carry sensitivity labels (e.g., Microsoft Purview) into Collicity; routing and sharing honor them, e.g., "Confidential" content only to admin-approved models.

**Capability grant elements (CON-02),** with examples from the beta contract (§18.1):

| Element | Example | Enforced |
| --- | --- | --- |
| Connector and action | Microsoft Graph · update an Excel table row | Before the call |
| Resource selector | Workbook Inventory.xlsx, table Stock only; mailbox folder Requests only | Before the call; responses filtered after |
| Field allowlist | Read Item and Qty; write Qty only | Before the call for writes; responses filtered for reads |
| Recipient rule | Draft replies only to the original sender | Before the call |
| Parameter limits | Qty change of at most 50 units; no deletes | Before the call |
| Volume limit | At most 20 rows changed per run | Running count at the gateway |

### 6.3 Custom connectors

- **CON-12 (P0)** Connector SDK (TypeScript and Python) that produces an MCP server with a signed manifest: auth method, token lifetime (CON-04), scopes, actions (each classed read / write / destructive), the parameters that identify resources and recipients (CON-02), each write's duplicate-prevention and recovery class (RUN-06, RUN-07), event replay window (RUN-02), cost model (BUD-03), data classes, rate limits and, from GA, how to check a person's own access to a resource and whether each resource parameter is fixed or personal (ASG-20, ASG-21). First-party connectors ship the same manifest.
- **CON-13 (P0)** Three ways in: register a remote MCP server URL, generate from an OpenAPI spec, or describe the API to the AI builder, which scaffolds a connector for human review.
- **CON-14 (P0)** Custom connector code runs in an isolated sandbox (WASM or microVM) with network egress limited to declared hosts.
- **CON-15 (P0, Enterprise)** Admin approval before a custom connector is usable by others; versions are pinned, and any scope change requires re-approval.
- **CON-16 (P2)** Public connector marketplace with publisher verification and security review.

## 7. Tasks, assignments and the assignment builder

Users reach a runnable assignment two ways — describe it to the AI builder or build it by hand — and both produce the same editable, versioned object with a cost estimate before anything runs.

### 7.1 Example assignments

| Example | Trigger | Tasks (H = human step) | Gate |
| --- | --- | --- | --- |
| Email request triage | New mail from listed senders, identity confidence medium or high (ASG-15) | Parse request → find target document or inventory record → draft change → add to work queue with suggestions → H: accept suggestion → apply change | Approval before any write, unless pre-authorized |
| Security posture report | Weekly, Monday 06:00 | Pull Qualys vulnerabilities → pull Splunk alerts → pull Intune compliance → compute metrics → write narratives → fill template → H: analyst review → publish to SharePoint | Review before publish |
| Weekly meal plan | Sunday 10:00 | H: preferences → draft plan → H: approve plan → build shopping list → H: edit list → price at local store → order and pay → create the delivery-night proximity trigger (§11) | Payment approval or pre-authorized cap (§14) |

### 7.2 Building

- **ASG-01 (P0)** Create, name, describe, duplicate, version and archive tasks and assignments. Assignments are shared under §7.7 from GA; tasks are shared only through the task library (ASG-07).
- **ASG-02 (P0)** A task description can be typed or AI-written ("Write this for me", "Improve"). The description is what the agent executes, so run logs show it verbatim.
- **ASG-03 (P0) AI assignment builder.** Conversational; asks clarifying questions; proposes tasks, human steps, triggers, required connectors (flagging any not yet authorized), a recommended model per task with its reason, and a cost estimate per run and per month. Nothing runs until the user accepts.
- **ASG-04 (P0) Manual builder.** Pick or create tasks; arrange them as sequence, branch, parallel or for-each; choose a model per task or "Auto".
- **ASG-05 (P0) Dry run.** Executes with read-only access and no external side effects, showing what would happen and its real token cost.
- **ASG-06 (P0)** Each task carries success criteria (e.g., "one row per device", "every number traces to source data") that the supervisor checks (§9).
- **ASG-07 (P1)** Task library: reusable tasks shared across assignments and, in Enterprise, across teams.
- **ASG-08 (P1)** Assignment blueprints: shareable starting points (e.g., "Weekly security posture report"), distinct from output templates (§10).

### 7.3 Running

- **ASG-09 (P0) Durable execution.** Runs survive restarts, can wait days on a human step and resume from the last completed step. Every write carries an idempotency key derived from run, step and action, but a key prevents duplicates only if the target enforces it, so the duplicate guarantee is declared per connector action (RUN-07), never platform-wide.
- **ASG-10 (P0) Human steps.** Input forms, approvals, editable lists and choices, each with a timeout and a fallback: remind, escalate or cancel.
- **ASG-11 (P0) Approval gates** default on for payments, sends to external recipients, deletions and permission or settings changes. A user may pre-authorize a narrow class of action within explicit limits (e.g., purchases under a cap at one merchant); the pre-authorization is itself the logged approval. Enterprise admins can make gates mandatory.
- **ASG-12 (P0) Run timeline.** Each step's inputs, outputs, connector calls, model, tokens, cost and supervisor verdict, exportable.
- **ASG-13 (P0) Kill switch.** Users can pause or cancel any run; enterprise admins can pause or cancel a user's runs, the runs of every instance of one shared assignment (§7.7), or all runs. A pause is resumable (§7.6). Suspending the user (ADM-06) is different: it is a deprovisioning signal (CON-06).

### 7.4 Work queues

- **ASG-14 (P0)** An assignment can create work-queue items: source, parsed request, match confidence, one-click suggested actions (e.g., "update inventory item 42 from 10 to 8"), assignee, due date.
- **ASG-15 (P0) Sender identity.** Mail authentication (SPF, DKIM, DMARC) proves which domain and servers sent a message, not which person, so sender identity is resolved separately with a confidence level. High: an internal user identified by the connector's directory. Medium: an external address matching a known contact whose domain passes aligned DMARC. Low: anything else, including display-name matches. Sender-based triggers, role and authority checks, and auto-actions require medium or high; low-confidence mail goes to a review lane and is never auto-actioned.
- **ASG-16 (P1)** Items auto-execute only when confidence is above a threshold and the action is on the user's pre-authorized list; otherwise they wait for a click.

- **ASG-17 (P0) Sender throttling.** Event triggers are rate-limited per sender (default 20 queue items per sender per day; the excess goes to a review lane), and bursts of near-duplicate requests from one sender are flagged. Auto-execution (ASG-16) switches off for a sender after any rejected, flagged or invalid item until the queue owner turns it back on, so an attacker can't send many variants until one gets through.

### 7.5 Runtime semantics

Seven behaviors need explicit rules before build. Each has a proposed default that owners can override per assignment; the defaults are still to be confirmed (§21).

- **RUN-01 (P0) Overlapping runs.** Each assignment has a concurrency policy: skip, queue (bounded), replace (cancel the older run at its next safe point) or run concurrently (capped). The cap and the queue bound count only Queued and Running runs (§7.6), so a run waiting days for an approval never blocks new runs. A run that has executed a step continues without waiting for a slot when it leaves Waiting or Paused: the cap limits how many runs start, not how many resume. A run that paused before its first step, such as one in Paused: re-acceptance, returns to Queued when the pause clears, in the order its trigger fired; the concurrency policy then applies to it as to a new trigger, so skip and replace leave one run instead of a burst. The queue bound doesn't refuse it, because its trigger was already accepted and deduplicated (RUN-02). Defaults: a scheduled trigger skips and notifies when the previous run is still active; per-item event triggers (one email, one webhook) run concurrently up to 5, then queue in order.
- **RUN-02 (P0) Trigger deduplication.** Every trigger event gets a dedup key: the provider's event or delivery ID, the mail provider's immutable message ID, or a hash of source and payload. Mail is never keyed by its Message-ID, which the sender chooses: a forged Message-ID can suppress a real message. Each key is scoped to its assignment, or to one instance of a shared assignment (ASG-20), so two that watch the same source each start once per event. Each key is kept at least as long as the source can redeliver or replay events (declared per connector; default 30 days) and, for mail, at least as long as the monitored window (90 days in the beta, §18.1). Events older than the dedup window are rejected, not run, unless an admin replays them deliberately. This makes run starts once per event; side effects happen once only as far as each action's duplicate prevention allows (RUN-07).
- **RUN-03 (P0) New versions and future runs.** In-flight runs stay on their version. Future runs use the latest published version unless the owner pins one; a recipient's instance of a shared assignment runs only the version its recipient accepted (ASG-23). If the new version adds connectors, scopes or side-effecting actions, widens any capability (CON-02) or raises the budget, future runs pause until the owner re-accepts it (invariant 2); schedule changes re-run the cost forecast (SCH-02).
- **RUN-04 (P0) Credentials after waits.** Runs never hold credentials across a wait. Each step obtains a fresh delegated token from the user's current grant at execution time; if the grant was revoked, expired or needs step-up authentication, the run moves to Paused: re-authorization (§7.6) and notifies the owner. Approvals bind to the exact action and parameters they showed, and expire after 7 days by default. Each approval request also records the version of the item it was based on; if the item changed before the decision, the request goes stale and is re-presented with current and proposed values side by side, so it never overwrites a newer decision. Request states: pending, blocked (e.g., the proposed assignee can't see the sources), invalid (failed a deterministic check; kept for forensics), accepted, rejected, stale, expired (the expiry passed before the request was decided or its action ran). An expired request never authorizes its action: its step asks again with a new expiry, re-presented as for a stale request, at once if the run is waiting for the decision or on resume if the run was paused (§7.6). The step's ASG-10 timeout still applies.
- **RUN-05 (P0) Revocation mid-run.** On revocation the gateway stops new calls at once. A call already sent may complete, since remote calls can't be reliably aborted. Its result is logged as "completed after revocation", quarantined (never passed to later steps, models or other users) and discarded; the run moves to Paused: re-authorization (§7.6), or is cancelled if the user was deprovisioned; any side effect that landed is reported as in RUN-06. If the user was deprovisioned after effects were applied, no delegated token remains to compensate with, so the run ends Completed with partial side effects instead.
- **RUN-06 (P0) Failure, compensation and partial side effects.** Every write action declares a recovery class: retryable (idempotent; retried with backoff within budget), compensable (a declared compensating action, e.g., cancel the order, restore the prior field value) or irreversible (e.g., a sent email; needs approval when consequential). The builder warns when an irreversible step comes before steps that can fail and suggests moving it last. Compensations are actions too, so they pass the same gateway and policy, under the approval or pre-authorization of the action they reverse (invariant 2): an approval binds the declared compensation and its exact restore values, and a pre-authorization covers the declared compensation, with restore values recorded when the action runs. Every run ends in one named terminal state: Succeeded, Failed (no side effects), Rolled back, Completed with partial side effects, or Cancelled (no side effects applied). Budget exhaustion, revocation and user pauses are resumable pauses, not outcomes (§7.6). The partial state lists each applied effect and opens a repair item in the owner's work queue; if the owner was deprovisioned, this item and the run's other open repair items (RUN-07) go to a custodian the tenant designates. A custodian's repair item rests only on the applied effects and their target resources, not on the run's sources, so a custodian who can read those targets can see it (SEC-08). For an outcome-unknown write (RUN-07), the custodian's item shows only the action, its target, the account it ran as, when it was attempted and any correlation ID, never values drawn from the run's sources; the custodian checks the target and treats what they find there as an applied effect. A repair item that no designated custodian can see, or that arrives while none is designated, is flagged to tenant admins with identifiers only until they designate a custodian who can see it or close it with a recorded reason (ADM-07).

- **RUN-07 (P0) Duplicate prevention and uncertain outcomes.** Each write action declares how duplicates are prevented: (a) the target enforces an idempotency key; (b) conditional or natural-key writes (If-Match on an ETag, create-if-absent); (c) a correlation ID stamped on the object plus read-before-write; or (d) none. When an outcome is unknown (timeout, lost acknowledgement, crash after sending), (a) and (b) retry safely, (c) reads the target to find out, and (d) is never retried automatically: the step pauses as "outcome unknown" and opens a repair item showing what may have happened. Temporal retries activities but leaves idempotency keys to be enforced by the service being called, so this declaration is required for every connector action. Class (d) actions need approval each time unless they are compensable.

![Failure handling (RUN-06) · 3 questions, 3 terminal outcomes](images/failure-handling.png)

An irreversible effect, such as a sent email, makes the last answer "no", which is why the builder pushes irreversible steps to the end of an assignment.

### 7.6 Run states

Waiting is part of normal work, a pause needs someone to act, and only terminal states are outcomes. A paused run keeps everything it has done and holds no budget it hasn't spent.

| State | Kind | Entered when | Leaves when | Budget reservation | Open approvals |
| --- | --- | --- | --- | --- | --- |
| Queued | Active | A trigger is accepted after dedup (RUN-02) | A concurrency slot frees (RUN-01) | None | — |
| Running | Active | A step executes | The step completes, waits, pauses or fails | Held for the current operation only | — |
| Waiting: person | Waiting | Approval, input or review is requested (ASG-10) | The person responds; a timeout reminds, escalates or cancels | Released | Valid until expiry or a base-version change (RUN-04) |
| Waiting: timer or device | Waiting | A scheduled step, or the device is offline (SCH-07) | The timer fires or the device returns | Released | Unchanged |
| Paused: budget | Paused | A reservation would exceed a limit (BUD-03) | An increase is approved at the level that blocked it (BUD-04) | Released; reserved again on resume | Kept; re-checked on resume |
| Paused: re-authorization | Paused | A grant is revoked, expired or needs step-up (RUN-04, RUN-05) | The user re-consents | Released | Kept; re-checked on resume |
| Paused: re-acceptance | Paused | A trigger fires on a version awaiting acceptance (RUN-03) | The owner accepts the version; the run returns to Queued (RUN-01) | Released | — |
| Paused: outcome unknown | Paused | A class (d) write's result is uncertain (RUN-07) | A person resolves the repair item | Released | Kept |
| Paused: by user or admin | Paused | Pause or kill switch (ASG-13) | Resume | Released | Kept; re-checked on resume |
| Succeeded, Failed, Rolled back, Completed with partial side effects | Terminal | See RUN-06 | — | Settled | Withdrawn |
| Cancelled | Terminal | A user or admin cancels; the concurrency policy skips or replaces the run (RUN-01); the user is deprovisioned, including by an admin suspension (runs pause within 60 s per CON-06, then cancel; lifting the suspension doesn't restore them); or a pause outlasts 14 days (default). Only when no side effects were applied | — | Settled | Withdrawn |

- On resume, completed steps never re-run; the next step gets a fresh token (RUN-04) and a new reservation.
- "Released" covers reservations whose operation never started. A reservation whose operation may have started, such as a call in flight at revocation (RUN-05) or a write whose outcome is unknown (RUN-07), settles or is charged in full instead (BUD-03).
- Effects already applied stay applied and stay listed on the run timeline.
- Cancelling a run that has applied effects goes through RUN-06 instead, ending Rolled back or Completed with partial side effects; if the user was deprovisioned, it can only end Completed with partial side effects (RUN-05).

### 7.7 Sharing

An assignment's owner shares either its report or the assignment itself; neither lends anyone the owner's access. In the beta, a report is shared as one-off published snapshots (SEC-10), and other people take part only as delegate approvers or custodians (§18.1). Assignment sharing and recurring report sharing arrive at GA.

- **ASG-18 (P0) Two ways to share.** The owner chooses each time: share the report, so recipients see results without access to the apps behind them (ASG-19), or share the assignment, so each recipient runs it with their own access (ASG-20). Only the owner shares, and only a version they have accepted. A share names people, groups or the whole tenant. Assignments are shared only inside the tenant; a recipient outside it gets the report, within SEC-10's label rules. Admins choose who may share assignments (ADM-01). Every share, offer, acceptance, decline, withdrawal, precheck and access request is audited (ADM-07).
- **ASG-19 (P0) Share the report.** Sharing a report publishes its editions as snapshots (SEC-10), which recipients can read without access to the sources behind them. Recipients inside the tenant can question them, charged to their own workspace budget (BUD-11); recipients outside it can only read them. Sharing can be recurring. The owner confirms one disclosure screen that lists the recipients and the source scopes the version declares (the sites, folders, lists and mailboxes its selectors resolve to). Each new edition then publishes without a new screen while it comes from the same version, its sources lie within those scopes, and its most restrictive label is no higher than that of the edition the owner confirmed. An edition is held for the owner to confirm again when any of these changes, when its label needs a second approver, or when the supervisor flagged it or injection screening found a positive (SUP-03, SEC-13). No edition publishes while the owner can't read every source or lacks the Publisher permission (SEC-10). A group recipient means its members at each publication, each of whom must meet SEC-10's recipient rules. The owner or an admin can withdraw the share, which stops future editions; an admin withdrawing one of its snapshots withdraws the share too.
- **ASG-20 (P0) Share the assignment.** Each recipient who accepts gets an instance they own, run with their own delegated access, connections, triggers, approvals and budget; the owner's access is never used for a recipient's run (invariant 1).
  - **What travels:** the definition, including the versions of the library tasks and templates it pins (ASG-07, TPL-06), which recipients use read-only inside their instance. Saved chat context and run data never travel. Standing instructions that rest on sources (SEC-12) can be shared only with people who can read those sources, or after the owner confirms a disclosure screen that lists them (SEC-10).
  - **Before accepting,** a recipient sees the version's tasks, triggers, capabilities, template, budget and cost estimate (BUD-02), and the provenance of each standing instruction. A resource or source they can't read appears only by its connector and type, never by name or path, and nothing previews the owner's runs (SEC-08). The share screen shows the owner what recipients will see.
  - **Resources and triggers:** fixed selectors keep the resources the owner resolved; personal selectors resolve in the recipient's own account when they accept, and the recipient may pick another resource of the same type (CON-02). Mail triggers watch the recipient's resolved folders, and webhook triggers get their own endpoint and secret.
  - **The instance:** the recipient sets its triggers, schedule, budget (starting from the version's, under their user budget, BUD-06), connections and personal resources; everything else comes from the version they accepted. Side effects need the recipient's approval or a pre-authorization the recipient made for that instance (ASG-11, ASG-16); pre-authorizations never carry over from the owner or from the recipient's other assignments. A recipient's runs and outputs follow SEC-08, so the owner sees them only if they can read the sources.
  - **Changes:** a recipient can share their instance's report (ASG-19) but not the assignment. To change the definition, they duplicate it (ASG-01) into an assignment they own and accept; the duplicate keeps each instruction's provenance (SEC-12), starts private and receives no later versions. Duplicating never copies shares.
- **ASG-21 (P0) Access precheck.** The precheck runs in two stages.
  - **When the assignment is shared,** Collicity checks its own records for each recipient: whether their role lets them own an assignment (ADM-01); whether admin policy allows each connector and action for them; whether each task has an eligible model for them, its pinned model or, on Auto, its task type's curated default (RTR-01, RTR-02); and whether their user budget has headroom for the instance's budget (BUD-04, BUD-06).
  - **When the recipient opens the share,** they connect any connector they haven't connected; consent is theirs to give (CON-08). They then start the target checks, which run under their own token through each connector's declared access check (CON-12) and read only their own access to the declared resources. Starting them authorizes those reads and nothing else (invariant 2).
  - **Results:** each requirement shows as present, missing or unverifiable. The owner sees a recipient's results once the recipient has run the check, and only for resources the owner can read. A recipient's share activates only when nothing is missing for them. Unverifiable writes are shown to the recipient before they accept. Unverifiable reads are confirmed after acceptance by a dry run the recipient starts (ASG-05), charged to their workspace budget (BUD-11), and the instance's triggers start only when it passes.
  - A denied access check is a result, never an out-of-policy call (CON-03, ADM-06). The precheck predicts and the gateway enforces: every call is still checked (CON-02, CON-03).
- **ASG-22 (P0) Access requests.** For anything missing, the owner or the recipient can start a request, which goes to whoever can grant it:
  - admin policy, models and roles: the tenant admin (ADM-01, ADM-03, ADM-05);
  - budget headroom: the recipient's budget owner (BUD-04);
  - consent: the recipient, or the tenant admin where the target requires admin consent (CON-08);
  - access in a target system: a person the requester names as able to grant it, or the tenant's IT queue; or the target's own access-request process, which the recipient starts under their own identity, because such processes request access for whoever calls them.

  Access to a target is granted in that system; Collicity never changes a target's permissions. An IT-queue ticket is a write under the requester's own identity, within a grant the tenant admin sets for access requests (one ticketing connector, one queue, allowlisted fields, a per-person daily limit), and the requester approves the exact ticket (ASG-11). A request names the recipient, the resource, the operation and the assignment that needs it. It expires after 7 days by default, carries over to a new version that still needs the same access, and goes stale only if the new version doesn't. Collicity withdraws a request when the share ends for its recipient or either person is deprovisioned (CON-06), tells the people it went to, and asks the requester to approve closing any ticket it opened. When a share ends, Collicity tells whoever granted access through one of its requests that the assignment no longer needs it. When a grant lands, the precheck runs again.
- **ASG-23 (P0) Offers, versions and the end of a share.**
  - **Offers:** an assignment shared with a group or the tenant is offered to each member, and an instance exists once a member accepts. A person who joins the group sees the offer; a person who leaves it is treated as if the share were withdrawn for them.
  - **Versions:** an instance runs only versions its recipient accepted. Each new version is offered with its changes and their provenance, measured against the version the recipient last accepted, never against the version published just before it. The instance keeps its accepted version until the recipient accepts the new one, and a version that widens any capability beyond the accepted version also needs the precheck to pass again (ASG-21). RUN-03's re-acceptance pause applies to the owner's own runs, never to instances.
  - **Ending:** the owner or an admin can withdraw a share, and a recipient can leave one. Either way the instance starts no new runs; runs already started finish their current step and end through RUN-06. Archiving or deleting the assignment ends its shares, with notice so recipients can first duplicate their accepted version; a version is kept while any instance's run is pinned to it. If the owner is deprovisioned, instances keep running their accepted versions and receive no new ones.

## 8. Budgets, cost estimates and alerts

Every assignment has a hard AI budget that cannot be exceeded without the owner's explicit authorization, and so does each person's workspace, which pays for work charged neither to an assignment nor to chat (BUD-11). The guarantee comes from reserving each billable operation's worst-case cost before it starts, not from after-the-fact accounting.

**What counts as AI cost:** model tokens (input, output, cached) at the provider's price — including router and supervisor calls; paid API calls; sandbox and browser compute; and any platform fee. Purchases are not AI cost; they draw from a separate purchase budget (§14).

- **BUD-01 (P0)** Each assignment has an AI budget per run and per period (day, week or month). *(Confirm the budget period.)*
- **BUD-02 (P0) Estimate before running.** The builder shows a per-run estimate as a typical and worst-case range, and a monthly projection = per-run estimate × scheduled runs (§11) + an event-trigger forecast from history.
- **BUD-03 (P0) Hard stop by reservation, for every billable operation.** Before any operation that costs money — model calls (worker, router, supervisor, judge, embeddings), paid API calls, sandbox and browser compute, platform fees — the runtime reserves its worst-case cost: tokens up to the `max_tokens` cap at current price; compute at its maximum allowed duration × rate, enforced by a hard timeout; fixed fees at their price. An operation with no declared cost model can't run. If any reservation would exceed what remains, the run pauses at that step boundary (§7.6). Reservations settle to actual cost afterwards. At expiry, an unsettled reservation is released only if its operation never started; one whose operation may have started is charged in full, then reconciled against provider usage where the provider reports it.
- **BUD-04 (P0) Authorization to continue.** A paused run shows what was spent, what remains, which limit blocked it and the estimate to finish. Only whoever controls the blocking limit can raise it: an assignment owner can raise the assignment cap within the headroom of their own user budget; user, group and organization limits need their budget owner (a group budget owner or an org admin), to whom the request is routed. Options: a one-time increase, a permanent raise, a cheaper model, or cancel. Every authorization is logged; nothing auto-increases.
- **BUD-05 (P0) Alerts.** User-set thresholds; recommended default 50%, 80% and 95% (notify) and 100% (pause). Channels: in-app, push, email; Enterprise adds Slack, Teams and webhooks.
- **BUD-06 (P0, Enterprise) Budget hierarchy.** Organization → department or group → user → assignment, workspace or chat (BUD-11). The most restrictive remaining limit wins.
- **BUD-07 (P0) Bring-your-own keys.** Cost is still tracked from the price catalog (list price, or tenant-negotiated rates an admin enters) and labeled "estimated" until reconciled with the provider's usage API where one exists (P1).
- **BUD-08 (P0) Price catalog.** Versioned prices for every billable operation (models, paid APIs, compute, fees), updated without an app release; each run records the price version it used.
- **BUD-09 (P1) Cost anomalies.** Flag runs costing more than 3× their trailing median.
- **BUD-10 (P0) Atomic reservations across levels.** One reservation is a single transaction against every applicable ledger — assignment, workspace or chat (BUD-11), user, group, organization — and succeeds only if all of them have headroom; otherwise nothing is reserved. Concurrent runs therefore can't jointly exceed any limit.
- **BUD-11 (P0) Workspace budget.** Billable operations charged neither to an assignment nor to chat reserve against the acting person's workspace budget: Q&A (QA-01–03), AI-written task descriptions (ASG-02) and dry runs (ASG-05). From GA, chat reserves against the person's own chat budget instead (QA-04). Each has a period like any budget (BUD-01) and sits under the person's user budget (BUD-06) or, in Home, under the account cap; the person can raise either within the headroom of the budget above it, as an assignment owner can (BUD-04). If a reservation would exceed what remains, the operation doesn't start, and the person sees which limit blocked it.

## 9. Auto-router, supervisor and model performance inventory

The router picks the cheapest model predicted to clear the task's quality bar, with a fully supervised curated default as the fallback; the supervisor checks the work, steps in when it is wrong, and feeds the outcome back so the next pick is better.

### 9.1 Auto-router

- **RTR-01 (P0) Eligible models** = allowed by admin policy ∩ offered by the user's configured providers ∩ capable of what the task needs (tool use, vision, context length, structured output) ∩ permitted for the data's classification and residency.
- **RTR-02 (P0) Selection rule.** Among eligible models, pick the lowest expected cost per verified success. Every model is judged by the conservative lower bound of its verified-success rate for the task type (INV-06), not its point estimate, and only models whose lower bound clears the task's quality bar (set by criticality: low, standard, high) qualify:

```latex
m^{*} = \arg\min_{m \in E} \frac{\hat{c}(m)}{p_{\mathrm{LB}}(m, t)} \quad \text{subject to} \quad p_{\mathrm{LB}}(m, t) \ge q_t
```

- Notation: ĉ(m) is the estimated cost of one attempt and q\_t the quality bar; p\_LB(m, t) is the 5th percentile of the Beta posterior over the probability that one attempt by model m on task type t ends in verified success, with the prior set by RTR-07. Example, before any prior: 29 verified successes in 30 attempts (97%) has a lower bound near 86%, while 96% over 50,000 runs has one near 95.9%, so the well-evidenced model wins. Dividing by p\_LB prices in the retries a weaker model will need.
- Fallback: while no eligible model's lower bound clears the quality bar, the task runs on its task type's curated default model under full supervision (every check runs on every attempt). This is the normal early state: even a prior with a 90% mean at the RTR-07 strength cap of 10 pseudo-attempts has a lower bound near 72%, so clearing a 90% bar takes 20 consecutive verified successes. Collicity curates each task type's default. If it isn't eligible for the task (RTR-01), the fallback is the eligible model with the highest lower bound, ties going to the lower estimated cost, under the same full supervision, and the decision record says why (RTR-04). If no model is eligible, the step fails before any model call and the owner sees which condition excluded each model; an admin can force an eligible model (RTR-05).
- **RTR-03 (P0) Cascade.** Optionally start on a cheaper model and escalate when the supervisor's check fails.
- **RTR-04 (P0) Explainable choices.** Each decision records candidates, scores and a plain-language reason ("Chose model A: lower bound 94% on extraction at $0.004 per attempt; model B: 95% at $0.03").
- **RTR-05 (P0) Overrides.** Users can pin a model per task; admins can force or forbid models per group or task type.
- **RTR-06 (P1) Exploration.** A capped share of runs (default ≤5%; off for high-criticality tasks and when an admin disables it) tries other eligible models so new or under-sampled models can earn evidence; lower-bound routing would otherwise starve them. Thompson sampling is one option.
- **RTR-07 (P0) Prior policy.** One prior policy for every model and task type: a Beta prior whose mean comes from, in order, the previous version of the same model (INV-03), benchmarks mapped to the task type, or 50% if neither exists. Its strength is capped at 10 pseudo-attempts, so real evidence dominates after a few dozen runs. Shadow evaluations count as real evidence only once adjudicated.

### 9.2 Task supervisor

The supervisor records four kinds of evidence separately and combines them only to decide each attempt's verified-success label (INV-06), so "96% on extraction" always says which evidence it rests on.

| Evidence class | What it establishes | Examples | Judged by | Signal arrives |
| --- | --- | --- | --- | --- |
| Deterministic | Verifiable facts about the output | Schema validates; one row per device; totals add up; referenced IDs exist; each cited quote exists in the source, matches exactly and isn't hidden | Code | Immediately |
| Grounded | The output faithfully reflects source material (probabilistic) | Each cited passage actually supports its claim (an exact quote can still be negated by its own sentence); narrative matches retrieved data | Cross-family judge model + citation checks | Immediately |
| Subjective | Quality as a person would judge it | Writing quality, usefulness, quality of recommendations | Ratings; judge model with a rubric; evaluation set | Minutes to days |
| Outcome | The work held up in the real world | Accepted without edits; ticket correctly routed; no later correction | User and system events | Hours to weeks |

- **SUP-01 (P0) Evidence, not verdicts.** Each check writes an evidence record of its class (check ID, result, judge model and version where one was used, hashed inputs) instead of a single pass / fail. Deterministic checks gate delivery: an output that fails one is never delivered as successful. Quotes shown to reviewers are rendered from the stored source snapshot by position, never from the model's text.
- **SUP-02 (P0)** Deterministic checks run first; a model from a different family than the worker judges grounded and subjective quality where possible. Check depth scales with task criticality; low-criticality tasks are sampled to control cost. Under the RTR-02 fallback, every check runs on every attempt, whatever the task's criticality.
- **SUP-03 (P0) Interventions, in escalating order:** annotate (flag to the user) → recommend (model, prompt or task change) → retry with feedback → take over (re-run the step on a stronger model) → pause and ask a human (§7.6). All interventions spend from the budget of the work they check (the assignment budget, or the workspace budget for a dry run, BUD-11) and appear on the run timeline.
- **SUP-04 (P0) Outcome signals.** Acceptance without edits, edit size, ratings, downstream corrections (a reopened ticket, a re-issued report) and reversals attach to the run as outcome evidence, even when they arrive days later.
- **SUP-05 (P1) Improvement suggestions,** e.g., "Task 3 fails 20% of the time on model X; model Y would add $0.40 per month and fix most failures."
- **SUP-06 (P0) Independent evaluation set.** A random sample of runs, stratified by task type and criticality, is adjudicated by reviewers who don't see the supervisor's verdict. It is the ground truth for escaped defects, supervisor precision and recall, and unnecessary interventions (§19). For triage workflows the sample also draws from inputs that never became queue items, stratified by pipeline stage including pre-filter drops (archived AC-15). Reviewers must be entitled to the data: tenant-designated reviewers in Enterprise, opt-in only in Home.

### 9.3 Model performance inventory

- **INV-01 (P0)** Stores the raw evidence of every run (all SUP-01 records plus outcome events); scores are derived from it, never stored in its place. Per model version × task type × tenant it reports pass rate per evidence class, intervention rate, user rating, average cost, cost per verified success, p50 / p95 latency, sample size, and the verified-success posterior (INV-06) with its lower bound.
- **INV-02 (P0)** Task types from a fixed taxonomy (extract, summarize, classify, draft, analyze data, plan, tool-heavy action, code, vision) plus custom tags. Each task type has a scoring policy: which evidence classes count, their weights, and hard requirements (e.g., all deterministic checks pass). Changing a policy rescores history without new runs.
- **INV-03 (P0)** A provider model update starts a new record, seeded with the previous version's posterior mean as its prior, at the RTR-07 strength cap; the previous version's raw evidence is not copied.
- **INV-04 (P0, Enterprise)** Admin dashboard: the inventory, the cost-vs-quality frontier per task type, and models trending worse.
- **INV-05 (P1)** Opt-in, anonymized cross-tenant benchmarks to improve priors; otherwise tenant data never leaves the tenant.

- **INV-06 (P0) Success label.** The router learns from one binary label per attempt. Verified success means every hard requirement in the task type's scoring policy passes, the weighted score of the remaining evidence meets the policy's threshold, and no negative outcome evidence (a correction or reversal) arrives within the outcome window (default 7 days). Weighted scores only decide the label; they are never treated as probabilities. Labels stay provisional until the window closes and enter the posterior only then; an independent adjudication (SUP-06) overrides the automatic label.
- **INV-07 (P0) Attribution.** Every attempt is one observation for the model that made it. A failed first attempt counts as a failure for that model even if a retry or another model's takeover then succeeds; the takeover's result counts for the model that took over. Retries with supervisor feedback count as attempts of the same model. Cost per verified success includes the cost of failed attempts, charged to the model that made them.

## 10. Templates

A template fixes the shape of a recurring output — sections, tables, charts, narrative slots and styling — so each run refreshes the content without changing the format.

**Example: security posture template.** Executive summary (narrative) · KPI tiles (critical vulnerabilities, mean time to remediate, device compliance %) · 12-week trend chart · top-10 risks table · one section per source tool · recommendations (narrative).

- **TPL-01 (P0)** A template is a versioned set of blocks: heading, text, narrative (AI-written, with its prompt, length and tone), table, chart (type, series, axes), KPI tile, image, page break.
- **TPL-02 (P0) Data bindings.** Each block binds to a task output or query. Tables and charts render deterministically from bound data; the model writes prose but never supplies numbers it wasn't given (same grounding rule as SUP-01).
- **TPL-03 (P0) Editions.** Each run produces a new edition. Editions are kept with diffs ("critical vulnerabilities 142 → 118") and can be compared side by side.
- **TPL-04 (P0) Editor.** Visual layout plus "describe the report" AI drafting, previewed with the last run's data.
- **TPL-05 (P0) Export and publish.** PDF, DOCX, PPTX, XLSX, Markdown and a live web view; publish to SharePoint, Google Drive, email, Slack or Teams through connectors.
- **TPL-06 (P0) Versioning.** An assignment pins a template version and offers to upgrade when a new one is published.
- **TPL-07 (P0) Citations.** Every narrative sentence links to the task output or data rows it came from.
- **TPL-08 (P1)** Brand kits (fonts, colors, logo) per organization or household.
- **TPL-09 (P1)** Share templates within a team or organization; a public gallery is P2.

## 11. Scheduling and triggers

Assignments and individual tasks can start on a schedule, an event or a person's location, and every schedule feeds the cost forecast before it is saved.

**Proximity example.** The approved meal plan includes delivery on Thursday. The meal-plan run creates a one-night proximity trigger: on Thursday between 17:00 and 20:00, when the phone enters the "Home" zone, Collicity orders the meal (cloud connector, subject to §14), sets the thermostat and turns on the lights (HomeKit actions run on the phone; other smart-home platforms run from the cloud). The trigger then expires.

- **SCH-01 (P0) Time schedules.** One-off and recurring (cron-equivalent behind a friendly editor), business-day and holiday aware per region, correct across time zones and DST, with blackout windows.
- **SCH-02 (P0) Cost-aware scheduling.** Changing a schedule updates the projected period cost (BUD-02) and warns before saving if the projection exceeds the budget.
- **SCH-03 (P0) Event triggers.** Matching email, webhooks, connector change events (a new Jira ticket, an Intune device falling out of compliance), another assignment finishing.
- **SCH-04 (P0) Proximity triggers.** Enter, exit or dwell at a named place, with conditions (time window, day, "only if task X was approved"), a cooldown (default once per day per trigger) and an expiry. ETA-based triggers ("20 minutes from home") are P1, since they need continuous location.
- **SCH-05 (P0) Location privacy.** Geofences are evaluated on the device; the server receives only the trigger event and place ID; raw location is never stored. The app explains why it needs location before asking, and works within OS limits (iOS monitors up to 20 regions per app, Android up to 100 geofences) by registering only active triggers.
- **SCH-06 (P0) Missed triggers.** If the device was offline or the trigger fires outside its window, the owner's chosen policy applies: skip, run late, or ask.
- **SCH-07 (P0) Device-dependent tasks.** Tasks that need a desktop or phone wait or skip when that device is offline, per the owner's choice, and the schedule preview shows that dependency.
- **SCH-08 (P1) Calendar triggers,** e.g., "one hour before any meeting with external attendees".

## 12. Ask-anything and chat

Users can question any template, assignment, task, run or output in place, and use a general chat with the model of their choice — both bound by the same permissions as everything else.

- **QA-01 (P0) Ask panel on every object.** Answers questions about definitions ("What does this assignment do?"), history ("Why did Tuesday's run fail?", "What did this cost last month?") and content ("Why did critical vulnerabilities drop?").
- **QA-02 (P0)** Answers cite their sources: run steps, data rows, documents.
- **QA-03 (P0) Viewer-scoped answers.** Answers draw only on what the asking person may see. By default an output, run or work-queue item is visible only to people who can currently read every source it rests on (SEC-08). Sharing beyond that creates a published snapshot (SEC-10); its viewers can question the snapshot's own content, never its underlying sources or run data.
- **QA-04 (P0) Chat.** Choose any allowed model or "Auto"; history, file attachments, and connectors under the same delegation rules. Each message shows its cost, which counts against the person's chat budget (BUD-11).
- **QA-05 (P0)** "Turn this into an assignment" hands a chat to the builder with its context.
- **QA-06 (P1)** Compare mode: one prompt to two or three models side by side.
- **QA-07 (P1)** Voice input on mobile.

## 13. Editions, identity and model providers

Home optimizes for zero-setup sign-in and personal use; Enterprise adds SSO, central governance and security monitoring on the same product.

| Area | Home | Enterprise |
| --- | --- | --- |
| Sign-in | OIDC / OAuth with Google, Apple, Microsoft personal, Facebook; passkeys; email link | SAML 2.0 or OIDC SSO with Okta, Entra ID, Ping, Google Workspace or any standards-based IdP; SCIM 2.0 provisioning |
| MFA | Passkeys or the provider's MFA; step-up for payments | Inherited from the IdP and its conditional access; step-up for risky actions |
| Administration | Account settings | Central admin console (below) |
| Budgets | Monthly account cap + per assignment, workspace and chat | Organization → group → user → assignment, workspace or chat |
| Models | Default provider or own keys | Admin allow / deny lists; tenant-owned provider accounts |
| Connectors | Consumer catalog | Admin-approved catalog + custom connectors |
| Data | Personal; export or delete anytime | Tenant isolation, retention policies, US / EU residency, customer-managed keys (P1) |
| Audit | Personal activity log | Immutable audit log with SIEM export |

iOS note: App Store guideline 4.8 requires Sign in with Apple (or an equivalent privacy-focused option) when other social logins are offered.

### 13.1 Enterprise admin console

- **ADM-01 (P0) Users and groups.** SCIM sync; roles: Owner, Admin, Security Auditor, Builder, Member, Viewer; license assignment; a custodian for repair items whose owner was deprovisioned, and the flagged repair items no custodian can see (RUN-06).
- **ADM-02 (P0) Budgets.** Organization, group, user, workspace and chat budgets; alert thresholds; routing for over-budget requests.
- **ADM-03 (P0) Model governance.** Allow or deny models and providers globally or per group; rules for which data labels may reach which providers; default router quality bars.
- **ADM-04 (P0) Model performance.** Inventory dashboards (INV-04).
- **ADM-05 (P0) Connector governance.** Enable pre-built connectors, approve custom ones, narrow scopes, require approvals for classes of action.
- **ADM-06 (P0) Security monitoring.** Live activity feed, policy denials, anomaly alerts (unusual volume, new destinations, repeated denied calls), user suspension (a deprovisioning signal, CON-06), connector suspension, and pausing a user's runs or all runs (ASG-13); near-real-time export to Splunk, Microsoft Sentinel, syslog or webhook.
- **ADM-07 (P0) Audit log.** Append-only and tamper-evident (hash-chained), searchable; holds identifiers and hashes, never payloads or prompt text (SEC-07); retention configurable, default 1 year, up to 7. Encrypted personal fields (SEC-07) are searched through blind indexes (keyed hashes), so search works without decrypting them.
- **ADM-08 (P1)** Policies exportable and importable as code, with staged rollout.

### 13.2 Model providers

- **PRV-01 (P0) Default provider.** Collicity-supplied models billed through Collicity credits; no setup.
- **PRV-02 (P0) Bring your own.** Anthropic API, OpenAI API, Amazon Bedrock, Microsoft Foundry, Google Vertex AI. OpenAI-compatible endpoints are P1; local models (e.g., Ollama on desktop) are P2.
- **PRV-03 (P0)** Keys live in the vault and are never displayed after entry. Enterprises can bind providers to their own cloud accounts (Bedrock, Foundry, Vertex) so prompts stay inside their cloud boundary.
- **PRV-04 (P0)** Mixed mode with a fallback order per provider; the router only considers providers the user or tenant has configured.
- **PRV-05 (P0)** Each model shows its provider's data terms (retention, training use, region) so admins can require zero-data-retention options.

## 14. Payments and real-world actions

Spending real money is a separate, stricter path than spending AI budget: its own purchase budget, a merchant allowlist, and an approval that defaults to "ask every time".

- **PAY-01 (P0) Purchase budget,** separate from the AI budget: per-assignment and per-account caps, a per-transaction maximum and a per-period maximum.
- **PAY-02 (P0) Default: approve each purchase** on the phone with biometrics, showing merchant, items, total including fees, tip and tax, and delivery time.
- **PAY-03 (P0) Optional pre-authorization.** The user can allow auto-purchase for one assignment, named merchants and an amount ceiling. Collicity still notifies after each purchase; the pre-authorization expires (default 90 days) and can be revoked anytime.
- **PAY-04 (P0) No raw card data.** Collicity uses the user's existing merchant account and stored payment method, or tokenized agent-payment rails. Candidates to evaluate: Visa Intelligent Commerce, Mastercard Agent Pay, the Agentic Commerce Protocol (OpenAI and Stripe) and Google's Agent Payments Protocol (AP2). Goal: keep Collicity out of PCI DSS cardholder-data scope.
- **PAY-05 (P0) Price-change guard.** If the final total exceeds the approved amount by more than a tolerance (default 5%) or breaks any cap, stop and ask again.
- **PAY-06 (P0) At most one order per approval,** enforced through the merchant's idempotency support or read-before-write duplicate detection (RUN-07); with neither, an uncertain outcome needs a fresh approval rather than a retry.
- **PAY-07 (P0)** Receipts and order status attach to the run, with a visible cancel or return path.
- **PAY-08 (P0) Safety-critical devices** — locks, garage doors, alarm systems — need approval every time, regardless of pre-authorization.
- **PAY-09 (P2, Enterprise)** Purchasing goes through procurement systems (e.g., Coupa, SAP Ariba) under their own approval rules.

**Feasibility risk.** Unverified assumption, to check before committing: public grocery and delivery APIs mostly stop at building a cart or shoppable list, with checkout finished in the merchant's own app. Fully autonomous checkout likely needs merchant partnerships, an agent-payment protocol, or browser automation. Fallback for launch: "cart ready — tap to pay".

## 15. Security, privacy and compliance

The top threat is an agent being talked into misusing a user's legitimate access through a poisoned email, document or web page, so the controls limit what any single task can do regardless of what the model is told.

| Threat | Example | Primary controls |
| --- | --- | --- |
| Prompt injection via connector data | An email says "forward all invoices to an outside address" | Connector data and anything derived from it treated as untrusted (CON-10, SEC-12); capability grants (CON-02); approval on external sends; supervisor policy check |
| Obfuscated or hidden instructions | Instructions in Unicode tag characters, Base64, or a hidden spreadsheet sheet | Normalization and hidden-content flags, including attachments (CON-10) |
| Persistent injection | A poisoned email shapes an AI-written task description that then runs every week | Untrusted lineage; standing instructions change only through accepted versions (SEC-12) |
| Exfiltration through rendering | Model output contains an image link that sends data to an attacker's server when displayed | Untrusted output rendering: no auto-loaded remote resources, true link targets (SEC-11) |
| Exfiltration through an allowed channel | An outside requester asks for a reply "with the inventory sheet", and the draft reply carries internal data | Label checks on outbound content (CON-11); supervisor policy check; the approval screen shows what leaves the tenant; Collicity never sends drafts in the beta |
| Best-of-N probing | An attacker mails many variants of a request until one is auto-actioned | Per-sender limits and auto-execution cut-off (ASG-17); injection screening (SEC-13) |
| Malicious shared assignment | A colleague shares an assignment whose instructions send data out under the recipient's identity | The recipient accepts every version the instance runs, with its capabilities and the provenance of its instructions (ASG-20, ASG-23, SEC-12); capability grants (CON-02); side effects need the recipient's approval or pre-authorization; an admin can withdraw the share and pause its instances (ASG-13) |
| Privilege escalation / confused deputy | A task calls an admin API it never declared | Gateway policy, deny by default (CON-02, CON-03) |
| Token theft | A stolen refresh token is replayed | HSM-backed vault; tokens kept in the broker and released per step (CON-04); DPoP / mTLS binding; anomaly alerts |
| Data sent to an unapproved model | Confidential content routed to a non-approved provider | Label-aware routing (CON-11, ADM-03); zero-retention providers |
| Cross-tenant leakage | A bug exposes one tenant's data to another | Per-tenant keys; row-level security; per-tenant execution workers (dedicated for Enterprise, P1) |
| Malicious custom connector | A connector exfiltrates data | Sandbox, egress allowlist, signing, admin approval (CON-14, CON-15) |
| Runaway cost | A loop burns through tokens | Reservation-based hard stop (BUD-03); step and loop limits |
| Spoofed requester | A fake email from "the boss" | Sender identity confidence, never mail authentication or display name alone (ASG-15) |
| Unsafe physical action | A spoofed location unlocks a door | Approval for safety-critical devices (PAY-08); mock-location detection |

- **SEC-01 (P0) Encryption.** TLS 1.3 in transit; AES-256 at rest; per-tenant keys in KMS / HSM; customer-managed keys for Enterprise (P1).
- **SEC-02 (P0) Secure development.** Threat model per feature; SAST, DAST and dependency scanning; SBOMs; signed builds for desktop and mobile; AI red-team exercise before the beta (gate 0) and again before GA, using a maintained injection suite: direct, indirect, encoded, attachment, multi-turn and best-of-N cases, run against instrumented tool stubs and data seeded with honeytokens, and re-run on every model, prompt or connector change; third-party penetration test before GA; bug bounty after GA.
- **SEC-03 (P0) Logging hygiene.** Secrets and tokens are redacted from logs and prompts; Enterprise chooses prompt and response retention.
- **SEC-04 (P0) Privacy.** Data minimization; user export and deletion (GDPR, CCPA) under the per-class rules below; no training on customer data by Collicity; data processing agreements with every model subprocessor.
- **SEC-05 Compliance roadmap.** SOC 2 Type I at Enterprise GA and Type II within 12 months; ISO/IEC 27001; GDPR from day one. Later, if the market calls for them: HIPAA BAA, ISO/IEC 42001 (AI management), FedRAMP. Track EU AI Act transparency duties for AI systems that interact with people.
- **SEC-06 (P0) Age.** Home accounts are 18+ at launch, which simplifies payments and consent. *(Assumption to confirm.)*

**Data classes and retention.** Deletion rights and a tamper-evident audit log are reconciled by separating five data classes, each with its own retention and deletion rule.

| Data class | Examples | Default retention | Home user asks to delete | Enterprise |
| --- | --- | --- | --- | --- |
| Operational content | Assignments, templates, chat history, outputs and editions | Until the owner deletes it | Deleted within 30 days | Tenant policy; legal hold overrides deletion |
| Prompts and model outputs | Run transcripts, judge prompts | 30 days | Deleted within 30 days | Tenant-set, 0–365 days |
| Connector payloads | Raw data fetched from target systems | Run duration + 7-day debug window; never in the audit log. Exception: items a triage step discarded or didn't surface, and runs drawn into the evaluation set, are kept 90 days so missed items can be measured (SUP-06) | Deleted | Same; a tenant may shorten it, which disables missed-item measurement for that tenant |
| Derived metadata | Cost, token counts, model choice, supervisor evidence and scores | Kept for routing and billing | Unlinked from the person (pseudonymized) | Stays in the tenant; pseudonymized when a user is removed |
| Audit records | Who, on whose behalf, which action and resource, approval, result | 1 year (Enterprise up to 7) | Personal fields crypto-shredded after the legal minimum; record and chain remain | Tenant is the controller and handles employee requests under its own policy |

- **SEC-07 (P0) Audit records hold identifiers and hashes, never payloads or prompt text.** Personal fields are encrypted with per-person keys and the hash chain covers the encrypted fields, so destroying a person's key erases their personal data while the chain still verifies. Destroying the key also deletes the person's blind-index entries (ADM-07), which sit outside the chain. Crypto-shredding is complete once backups taken before the key was destroyed have expired.

- **SEC-08 (P0) Visibility follows the sources.** An output, run, work-queue item or answer is visible only to people who can currently read every source it rests on, computed from each source's current access list (refreshed by connector events and reconciliation), never from access at capture time. The only exception is a published snapshot (SEC-10). It is enforced at every exit: API, live updates, notifications (titles are as sensitive as bodies), file and download links, exports, background jobs, model prompt assembly (no content from a source the acting person can't read; batch jobs and prompt caches are grouped by visibility, never shared across a tenant) and client caches (encrypted with a key in the OS keychain, expiring, purged on revocation; notifications checked at send time, not queue time).
- **SEC-09 (P0) Tenant isolation in the database.** Row-level security on every tenant table, with FORCE ROW LEVEL SECURITY; the application's database role is not the table owner and lacks BYPASSRLS; tenant and acting user are set per transaction from the signed-in identity, never from request input; background workers carry both and open their sessions the same way.

- **SEC-10 (P0) Published snapshots.** Publishing turns an output edition into a frozen, labeled copy that named recipients can see without access to its sources.
  - **Who may publish:** someone who can currently read every source, holds the Publisher permission for that destination (admins grant it per group), and confirms a disclosure screen listing the recipients and every source behind the content. A recurring report share is confirmed once for the editions within its bounds (ASG-19); the read and Publisher conditions hold for every edition.
  - **Labels:** the snapshot carries the most restrictive sensitivity label among its sources. Admins choose which labels can never be published and which need a second approver; recipients outside the tenant are allowed only for labels that permit external sharing.
  - **Revocation:** the publisher or an admin can withdraw a snapshot at any time; recipients lose it in Collicity at once, and client caches are purged. If the publisher loses access to a source, or a source's label rises to a non-publishable level, the snapshot is flagged and, by default, withdrawn pending review. Copies already exported (PDF, email, a file in SharePoint) are beyond Collicity's reach, and the disclosure screen says so.
  - **Q&A:** questions about a snapshot are answered only from its own content (text, tables, chart data) plus what the asker can read themselves, never from its sources, run logs or the publisher's access.
  - Every publish, view, flag and withdrawal is audit-logged.

- **SEC-11 (P0) Untrusted output rendering.** Model output is untrusted wherever it is shown or exported: chat, Q&A answers, queue items, run timelines, templates and exports. It renders as sanitized Markdown or allow-listed HTML; remote images and other remote resources never load automatically; links show their true destination and open only on a click, with a warning for domains outside the tenant's link allowlist; scripts, forms and embedded frames are stripped; and no URL in model output is fetched on anyone's behalf. The link allowlist is a tenant setting that admins manage. Exports (TPL-05) apply the same rules before publishing.
- **SEC-12 (P0) Untrusted lineage.** Every piece of content records its provenance: its sources, and whether any untrusted input contributed to it. Model output derived from untrusted content stays untrusted through later steps, runs and exports, so a summary of a poisoned email is handled like the email itself. Untrusted-derived content never becomes standing instructions on its own: task descriptions, template prompts, assignment versions, builder output, saved chat context and supervisor suggestions change only through a version a person accepts, shown with its provenance. System prompts hold no secrets and are assumed to be extractable.
- **SEC-13 (P1) Injection screening as a signal.** A classifier from a different model family than the worker screens untrusted inputs, planned actions and outputs for injection patterns, and the supervisor compares each run's actual tool calls with the task's plan. A positive result raises risk: auto-execution and pre-authorization are suspended for that item, and it goes to human review. A negative result never grants anything, since a guardrail model can itself be manipulated.

## 16. Reference architecture

![Reference architecture · three cloud planes; calls to models and target systems pass the connector gateway](images/reference-architecture-v0.5.png)

Clients never call target systems directly: the execution plane plans each action, the connector gateway checks and credentials it, and the audit log records it. Model calls also go through the connector gateway: the router picks the model, and the gateway checks the budget reservation (BUD-03) and makes the call. Device-local actions (desktop files, HomeKit) are sent to the user's own device under the same policy check, and on-prem systems are reached through an outbound-only relay the customer installs.

**Building blocks** (decided 2026-10-05; client stack in §5; detail and reasons in *Collicity — Technical Design*):

- **Infrastructure:** AWS as the single cloud; ECS on Fargate, provisioned with Terraform; OpenTelemetry to CloudWatch and X-Ray.
- **Backend:** Python 3.13 (3.14 during GA) with FastAPI and Pydantic v2, whose models double as LLM output schemas.
- **Orchestration:** Temporal Cloud runs short run segments between waits; Postgres is the system of record for run state.
- **Data:** Aurora PostgreSQL with row-level security (SEC-09); a transactional outbox in Postgres feeding live updates over Server-Sent Events, with Valkey added at GA if load needs it; S3 for snapshots and outputs, with Object Lock for audit anchors; pgvector at GA where an index is unavoidable (CON-09).
- **Security:** a typed grant evaluator in the beta and Cedar from GA (CON-02); KMS envelope encryption for secrets (CON-04); WorkOS for SSO and SCIM; Firecracker microVMs as sandboxes: a Lambda with no internet access for content extraction in the beta (CON-10) and Fargate tasks for custom connectors at GA (CON-14).
- **Models:** Collicity's default provider (PRV-01) is Claude through Claude Platform on AWS; a second model family on Bedrock judges across families (SUP-02).

## 17. Non-functional requirements

Proposed launch targets; the scale row is a placeholder until the go-to-market plan sets it.

| Area | Target |
| --- | --- |
| Availability | 99.9% monthly for the control and execution planes (Enterprise SLA) |
| Schedule accuracy | Scheduled runs start within 60 s of their time (p99) |
| Proximity trigger latency | Trigger to first action under 30 s (p95) while the device is online |
| UI responsiveness | Interactions under 200 ms (p95), excluding model calls; first chat token under 2 s (p95) on the default provider |
| Revocation | Deprovisioning signal received by Collicity (CON-06) → tokens revoked and runs paused within 60 s |
| Durability | No lost runs; RPO ≤ 5 min, RTO ≤ 1 h; audit log RPO 0 when an Availability Zone is lost; cross-region disaster recovery targets for the audit log are set at GA |
| Scale (design point, year 1) | 100k Home users, 500 Enterprise tenants, 10k concurrent runs |
| Data residency | US and EU regions at Enterprise GA |
| Observability | OpenTelemetry traces from client to run to model to connector, using the GenAI semantic conventions |
| Accessibility | WCAG 2.2 AA on all clients |
| Localization | English at launch; internationalization-ready from day one |
| Supportability | Per-run support bundle with redacted logs |

## 18. Phasing and MVP scope

![Phasing · 5 phases, 3 gates](images/phasing.png)

Enterprise GA needs every requirement in the matrix's Beta and GA columns, not every P0; the matrix is authoritative for when each requirement ships. Phase 3 can overlap Phase 2 if capacity allows, and durations depend on team size (§21).

**Requirements by phase** (cumulative: each column also needs everything to its left)

| Area | Beta (Phases 0–1) | Enterprise GA (Phase 2) | Home (Phase 3) | Later |
| --- | --- | --- | --- | --- |
| Platforms (§5) | PLT-01 web only; PLT-06 | PLT-01 desktop and mobile; PLT-02; PLT-03 approvals; PLT-05 | PLT-03 payment biometrics; PLT-04 | — |
| Connectors (§6) | CON-01–04, 06–10; CON-05 only if a partner needs it; Graph plus at most 2 partner connectors, each with a full CON-12 manifest | CON-11–15; enterprise launch catalog | Consumer catalog | CON-16 |
| Tasks and runtime (§7) | ASG-01, 02, 04–06, 09–15, 17, with every action approved (no pre-authorization before gate 1); RUN-01–07 | ASG-03, 07, 08, 16, 18–23 | — | — |
| Budgets (§8) | BUD-01–05, 08, 10, 11; BUD-06 without groups | BUD-06 groups; BUD-07, 09 | Account cap | — |
| Router, supervisor, inventory (§9) | RTR-01–05, 07; SUP-01–04, 06; INV-01–03, 06, 07 | RTR-06; SUP-05; INV-04 | — | INV-05 |
| Templates (§10) | — | TPL-01–07 | — | TPL-08, 09 |
| Scheduling and triggers (§11) | SCH-03 mail events | SCH-01, 02, 07, 08 | SCH-04–06 | — |
| Q&A and chat (§12) | QA-01–03 | QA-04–06 | QA-07 | — |
| Admin and providers (§13) | ADM-01–03, 05, 07; ADM-06 without SIEM export; PRV-01, 03, 05; PRV-02 only for a partner's own cloud | ADM-04, 08; ADM-06 SIEM export; PRV-02, 04 | Consumer sign-in | Local models |
| Payments (§14) | — | — | PAY-01–08 | PAY-09 |
| Security and privacy (§15) | SEC-01 without customer-managed keys; SEC-02–04, 07–12 | SEC-01 customer-managed keys; SEC-05, 13 | SEC-06 | — |
| Non-functional (§17) | Revocation, durability, observability, accessibility | All rows | Home scale | — |

**Exit gates** (every criterion must hold; targets are proposals until the beta starts)

| Gate | Exit criteria | Evidence |
| --- | --- | --- |
| Gate 0: foundations → beta | Red-team suite blocks 100% of out-of-policy calls across every capability element (CON-02) and every injection case (SEC-02), with no honeytoken leaving the test sandbox; fault injection shows 0 duplicate writes for class (a)–(c) actions and 0 unreserved billable operations; every invariant test suite passes | CI, red-team report |
| Gate 1: beta → GA build | At least 3 design partners from at least 2 functions active weekly for 8 consecutive weeks; 0 guardrail violations (§19); missed requests ≤10% with an upper 95% bound ≤15%; triage precision ≥80%; escaped defects <2% of adjudicated delivered actions; every §18.1 acceptance case passes; 80% of runs within ±25% of their cost estimate; every Beta-column requirement passes its acceptance tests | Evaluation set, guardrail dashboards, CI |
| GA launch | Every GA-column requirement passes its acceptance tests; SOC 2 Type I report issued; third-party penetration test with no open critical or high findings; US and EU regions live | CI, audit and pentest reports |
| Gate 2: GA → Home | 99.9% availability and 0 guardrail violations over the 60 days after GA; a restore drill meets the §17 RPO and RTO | SLO dashboards, drill report |
| Gate 3: Home purchases on | A signed merchant partner; app-store approval; PAY acceptance cases pass; 0 unauthorized purchases in a 4-week closed beta | Contract, store review, payment records |

### 18.1 Beta contract: email request triage

The beta proves one workflow end to end: an email request becomes a work-queue item, and an approved action carries it out, with every invariant holding. Its intake reuses the archived compliance plan v3.1 design, with one change: copies of a message captured in more than one scope are matched by a hash of the normalized message, including its sender and sent time, not by the Internet Message-ID that v3.1 uses as `content_identity`, since the sender controls that ID (RUN-02).

**Design partners** (names to add, §21)

| Partner | Function | Status |
| --- | --- | --- |
| Compliance customer | Compliance | Identified; name to add |
| To recruit | IT service desk or operations | Needed for gate 1 |
| To recruit (optional) | Another function | Strengthens gate 1 |

**Supported inputs**

- Microsoft 365 mail through Graph: the user's own mailbox and shared mailboxes they can read; selected folders; a 90-day monitored window.
- Message bodies, sanitized, with hidden regions flagged (CON-10); attachments as metadata, plus text extracted in a sandbox from PDF, DOCX and XLSX files up to 10 MB.
- English only. Not in the beta: Teams or other chat, Gmail, calendar invites, image-only content.

**Approved action types** (every action needs approval during the beta; pre-authorization starts after gate 1)

| Action | Capability limits | Duplicate prevention (RUN-07) | Recovery (RUN-06) |
| --- | --- | --- | --- |
| Update a row in a named Excel table | Named workbook and table; allowlisted columns; numeric change limits | (b) conditional write where Graph supports it, else (c) | Compensable: restore prior values |
| Create or update an item in a named SharePoint list | Named list; allowlisted fields | (c) for creates; (b) or (c) for updates | Compensable: delete the item or restore values |
| Create or update a ticket in one partner ticketing system | One project or queue; allowlisted fields | (a) if the system enforces idempotency keys, else (c) | Compensable: close as created in error, or restore fields |
| Draft a reply in the user's mailbox | Original sender only; Collicity never sends | (c) correlation ID in a message header | Compensable: delete the draft |

Out of the beta: sending email, deleting records, and changing permissions or settings.

**Roles**

| Role | Who | Can |
| --- | --- | --- |
| Queue owner | Owner of the monitored mailbox or the assignment | Accept or reject items, approve actions, name delegates |
| Delegate approver | Named by the queue owner; must pass SEC-08 visibility for the item | Accept items and approve actions on that queue |
| Custodian | Designated by the tenant admin; must pass SEC-08 visibility for the item | Receive and resolve repair items from runs whose queue owner was deprovisioned after effects were applied (RUN-05, RUN-06, RUN-07) |
| Requester | The email's sender | Nothing in Collicity; follow-up mail becomes change, cancel or complete proposals checked against authority rules |
| Tenant admin | Customer administrator | Configure connectors, capabilities and budgets; designate the custodian and close flagged repair items (RUN-06); suspend users (CON-06) and pause runs (ASG-13); never approve another person's actions |

An action a delegate approves runs with the queue owner's delegated token, so the queue owner's permission ceiling applies (CON-01, CON-02). Its audit record names the queue owner as the user and the delegate as the approver (CON-07).

**Queue item lifecycle.** Main path: Proposed → Accepted → Action proposed → Approved → Executing → Done, with completion evidence. An item whose approval expires before it executes returns to Action proposed (RUN-04). Other states: Rejected (disposition: not relevant, duplicate of, already completed, wrong split, wrong merge, out of scope), Merged, and Cancelled (only through an accepted cancel proposal). Each item also has two status fields, kept separate from its lifecycle state as the archived compliance plan v3.1 keeps its assignment status: an assignment status (none, assigned or needs reassignment) and a repair status (none or needs repair). An item needs reassignment when its assignee can no longer see it (SEC-08), unless it is already Done, Rejected, Merged or Cancelled. It needs repair while a repair item for its action is open (RUN-06, RUN-07). Neither field changes the lifecycle state. One email can create several items. Follow-up mail updates unapproved fields directly; approved fields, cancellation and completion change only through accepted proposals.

**Acceptance cases**

- Adopted from the archive, reading obligation as queue item and proposal as approval request: all 21 cases in `acceptance-cases-release-1-v3-1.md`, with AC-10 and AC-14 as written in `acceptance-cases-release-1.md`. AC-15 (missed requests) and AC-16 (adjudicated precision) define two gate 1 metrics.
- New cases to write for the action half: capability violation blocked (CON-02); lost acknowledgement for each action type (RUN-07); compensation and partial side effects (RUN-06); budget pause and resume (§7.6); stale and expired approvals (RUN-04); revocation mid-run (RUN-05); snapshot withdrawal (SEC-10); exfiltration through rendering blocked (SEC-11); untrusted lineage across steps and no writes to standing instructions (SEC-12); obfuscated and attachment-hidden instructions flagged (CON-10); per-sender throttling (ASG-17).

**Exit:** gate 1 in the exit-gates table above.

## 19. Success metrics

Leading indicators show within weeks whether the core loop works. Quality metrics come from the independent evaluation set (SUP-06), so defects that neither the supervisor nor the user noticed still count, and so do requests that never became work. Each product invariant has a guardrail that must hold at zero from day one.

| Metric | Type | Proposed target | Measured by |
| --- | --- | --- | --- |
| New users who run an assignment within 7 days | Leading | 35% | Product analytics |
| AI-built assignments accepted with ≤2 edits | Leading | 60% | Builder telemetry |
| Runs whose actual cost lands within ±25% of the estimate | Leading | 80% | Budget ledger |
| Median time from approval request to decision | Leading | <10 min (Home), <1 h (Enterprise) | Approval events |
| Missed requests: relevant requests in sampled inbound mail that never became queue items | Quality | ≤10%, upper 95% bound ≤15% | Sample stratified by pipeline stage, including pre-filter drops; bootstrap interval (archived AC-15) |
| Triage precision: adjudicated queue items that were real requests | Quality | ≥80% | Reviewer decisions, pending items excluded (archived AC-16) |
| Escaped defect rate: defective delivered runs ÷ adjudicated delivered runs | Quality | <2% | Evaluation set; paused, cancelled and withheld runs excluded |
| Supervisor precision: flags that were real defects | Quality | ≥80% | Evaluation set |
| Supervisor recall: real defects the supervisor flagged | Quality | ≥90% | Evaluation set |
| Unnecessary interventions: retries or take-overs on correct output | Quality | ≤10% of interventions | Evaluation set |
| Supervision cost share | Quality | ≤15% of run cost | Budget ledger |
| Net cost per verified-successful task | Quality | ≥40% below frontier-only baseline (G3) | Budget ledger + evaluation set |
| Assignments still running 8 weeks after creation | Lagging | 50% | Product analytics |
| Enterprise seats active weekly | Lagging | 60% of licensed | Admin console |
| Connector calls beyond the user's permission ceiling (invariant 1) | Guardrail | 0 | Gateway + audit log |
| Side effects, including purchases, without valid authorization (invariant 2) | Guardrail | 0 | Audit log + payment records |
| Credentials found in prompts, outputs or logs (invariant 3) | Guardrail | 0 | Secret scanning of stored prompts and logs |
| Billable operations started without a reservation, or budget overruns without authorization (invariant 4) | Guardrail | 0 | Budget ledger reconciliation |
| Injection test cases that expanded authority or leaked a honeytoken (invariant 5) | Guardrail | 0 | Release-gating red-team suite |

## 20. Key risks

Scope is the largest risk: six platforms, two editions and real-money purchases at once would delay learning by many months, so phasing (§18) is the main mitigation.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Scope: six platforms × two editions × payments | Slow launch, thin quality | Phase by edition and capability; one web and desktop codebase and one design system (PLT-01); execution centralized in the cloud |
| Prompt injection causes a harmful action | Security incident, loss of trust | §15 controls; approvals default on; red-team before the beta and before GA |
| Merchants don't allow agent checkout | Meal-plan purchase step blocked | Partner early; launch with "cart ready — tap to pay" |
| Key target systems lack delegated auth | Weakens the "user's own permissions" promise | Personal credentials or live entitlement checks (CON-05); publish a per-connector attribution matrix |
| Sparse routing data early on | Poor model choices, higher cost | Benchmark priors; conservative quality bars; capped exploration |
| Model and price churn | Wrong estimates, stale inventory | Versioned price catalog; per-version inventory records |
| OS limits on background location | Late or missed proximity actions | Cooldowns, missed-trigger policy, visible trigger health |
| Supervisor overhead | Erodes cost savings | Deterministic checks first; sample by criticality |
| App store review of payments and location | Launch delay | Early review consultation; clear in-app disclosures |
| Liability for wrong purchases or actions | Refunds, disputes | Approvals default on; caps; clear terms of service |

## 21. Decisions and open questions

Scoping decisions through the beta contract are made, and so are the cloud, the stack and a team of 1–3 engineers. The first remaining blocker is naming the beta design partners; the beta's AWS region and the target date follow.

**Decided 2026-10-05**

- **Launch edition:** Enterprise first; Home follows, starting with its non-payment features.
- **Beta workflow:** email request triage with approved actions; the contract is in §18.1.
- **Deployment:** multi-tenant SaaS plus an outbound-only on-prem relay; a dedicated single-tenant cloud comes later.
- **Purchasing:** approve each purchase by default; opt-in, capped pre-authorization per assignment and merchant (PAY-02, PAY-03).
- **Systems without OAuth:** personal credentials in the vault; admin-governed service accounts as a badged exception with live entitlement checks (CON-05).
- **Cloud and team:** AWS is the single cloud; 1–3 engineers build the product through the beta, and *Collicity — Technical Design* gives a timeline for each team size.
- **Stack:** one React + TypeScript codebase for web and desktop, with Tauri 2 as the desktop shell unless the spike at the start of Phase 2 rules it out; React Native with Expo for mobile; a Python backend on AWS managed services (§5, §16). The technical design gives the detail and reasons.
- **Sharing:** the owner chooses between sharing the report and sharing the assignment. A shared assignment runs as each recipient's own instance, with their own access, after an access precheck; missing access is requested from whoever can grant it, never granted by Collicity (§7.7). In the beta, reports are shared as one-off snapshots (SEC-10); assignment sharing and recurring report sharing arrive at GA.
- **Prior work:** the compliance assistant plans v1–v3.1 and their acceptance cases are archived under `docs/archive/` in the collicity repository. Compliance is one customer vertical of Collicity; their sync semantics, measurement method and acceptance cases are reusable for any email- or chat-triggered workflow, starting with the beta.

**Open**

| Question | Owner | Blocking? | Default if unanswered |
| --- | --- | --- | --- |
| Who are the named beta design partners (§18.1)? | Product owner | Yes, first: they fix the mailboxes, action types, ticketing connector and beta AWS region | The compliance customer plus one IT service desk or operations team |
| Which AWS region hosts the beta (where the design partners need their data)? | Product owner with the design partners | Yes, before Phase 0 infrastructure | — |
| Approval gates (ASG-11) and invariant 2 disagree: does every side effect need an approval or a pre-authorization, or only payments, external sends, deletions and permission or settings changes? The beta approves every action either way | Product, security | Before pre-authorization starts after gate 1 | Invariant 2 governs: every side effect needs an approval or a still-valid pre-authorization, and a gate is never switched off |
| Should SEC-01's per-tenant keys also cover database rows? | Product, security | Before the beta's data model is fixed | Per-tenant keys cover secrets, audit personal data and stored content in S3; database rows use the cluster key, isolated by row-level security (SEC-09) |
| Target date for the Enterprise beta? | Product owner | Yes, for phasing | — |
| Confirm runtime defaults RUN-01 to RUN-07 and the run states | Engineering, product | Before orchestrator build | As proposed in §7.5 and §7.6 |
| Scoring policy per task type; evaluation-set sample size and reviewers | Product, data | Before beta | Deterministic checks gate; each design partner names reviewers |
| AI budget period: per run + per month, or lifetime per assignment? | Product | Before budget build | Per run + per month |
| Pricing: seats plus usage credits? Margin on default-provider tokens? Discount for own keys? | Business | Before beta | Seats + credits at cost-plus |
| Which compliance frameworks do the first target customers require (FedRAMP, HIPAA)? | Sales, legal | Before Enterprise GA | SOC 2 + ISO 27001 |
| Which grocery and delivery partners to approach for Home? | Business development | Before Home | — |
| Several approvers in Home (household members approving one assignment's actions)? | Product | No | One approver per action at launch |
| Allow browser-automation connectors for sites with no API? | Product, security | No (P2) | Not in v1 |
| Support local models on desktop? | Product | No (P2) | Not in v1 |
| Public marketplace for connectors, templates and blueprints, with revenue share? | Business | No (P2) | Organization-internal sharing only |
| Offer opt-in cross-tenant model benchmarks (INV-05)? | Product, legal | No | Opt-in only |
| Home minimum age? | Legal | Before Home launch | 18+ |
| A second review of sharing (§7.7) left these open. How do access checks, which run outside any run, reach the gateway, and which budget pays for them? What grant bounds a request filed through a target's own access-request process? What happens when the post-acceptance dry run can't read something? Personal selectors resolve at acceptance, after the precheck: how are they checked? Should the precheck test the Auto model against RTR-02's fallback rather than the curated default alone? What do SEC-10's label rules mean for disclosing standing instructions? Can recipients outside the tenant question beta snapshots (QA-03), given they have no budget to charge? | Product owner with engineering | The QA-03 question before the beta; the rest before the GA build | ASG-18 to ASG-23 as written; outside recipients only read snapshots |

## Revision history

| Version | Date | Changes |
| --- | --- | --- |
| v0.5 | 2026-10-05 | Adopted the 18 changes proposed in the technical design: the deprovisioning bound runs from receipt of the signal, with optional directory polling, and an admin suspension deprovisions while a pause stays resumable (CON-06, ASG-13, ADM-06, §3, §7.6, §17 Revocation row); refresh tokens are readable only by the token broker (CON-04); a typed grant evaluator in the beta and Cedar from GA (CON-02); in-process first-party connectors in the beta (§6.1); hidden regions and look-alikes in the model view (CON-10); approvals and pre-authorizations cover declared compensations (invariant 2, RUN-06, CON-02); concurrency counts only Queued and Running runs, and runs paused before their first step are re-admitted (RUN-01, §7.6); mail is deduplicated by immutable message ID and matched across scopes by content hash (RUN-02, §18.1); an expired approval state and what follows it (RUN-04, §18.1); custodians for repair items of deprovisioned owners, with an admin flag for items no custodian can see (RUN-05, RUN-06, ADM-01, §7.6, §18.1); reservations that may have started are charged in full (BUD-03, §7.6); a workspace budget, with the GA chat budget beside it (BUD-11, BUD-06, BUD-10, ADM-02, QA-04, §4, §13); a curated-default fallback for the router, and what happens when the default isn't eligible (RTR-02, SUP-02); status fields for reassignment and repair (§18.1); delegate approvals record both people (CON-07, §18.1); blind indexes and crypto-shredding (ADM-07, SEC-07); audit RPO 0 now covers the loss of an Availability Zone, with cross-region targets set at GA (§17 Durability row); a tenant link allowlist (SEC-11); the decided cloud, stack and team (§5, §16, §20, §21) and a redrawn reference architecture. Also records the product owner's decision on sharing: share the report or the assignment, with an access precheck and access requests (§7.7, ASG-18 to ASG-23), and its knock-ons (invariant 2, §3, §4, ASG-01, ASG-13, CON-02, CON-12, RUN-02, RUN-03, SEC-10, §15). Two new open questions: approval gates against invariant 2, and per-tenant keys for database rows (§21) |
| v0.4 | 2026-10-05 | Prompt-injection hardening from the OWASP cheat sheet: untrusted output rendering (SEC-11); untrusted lineage and protected standing instructions (SEC-12); injection screening as a signal (SEC-13); normalization and hidden content in attachments (CON-10); sender throttling (ASG-17); a defined injection test suite (SEC-02, gate 0); five new threat rows (§15) |
| v0.3 | 2026-10-05 | Second review: capability grants and live entitlement checks (CON-02, CON-05); step-scoped grants vs. upstream token lifetime (CON-04); duplicate prevention and uncertain outcomes (RUN-07); run states (§7.6); reservations for every billable operation, atomic across levels, with increase authority (BUD-03, BUD-04, BUD-10); one prior policy, success label and attribution (RTR-07, INV-06, INV-07); published snapshots (SEC-10); requirement matrix, exit gates and beta contract (§18); missed-request metrics (§19); a consistency pass across sections |
| v0.2 | 2026-10-05 | First review: product invariants, runtime semantics, evidence classes and lower-bound routing, data classes, evaluation set; fixes ported from the archived compliance plans; stack defaults; beta workflow default |
| v0.1 | 2026-10-05 | First draft |

## Sources

- [Temporal: Activity Definition, idempotency](https://docs.temporal.io/activity-definition#idempotency): idempotency keys are enforced by the service being called, not by Temporal (RUN-07).
- [Microsoft Entra: Configurable token lifetimes](https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes): access tokens default to 60–90 minutes, and up to 28 hours for CAE-capable sessions (CON-04).
- [OWASP LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html): attack types and defenses behind CON-10, ASG-17, SEC-02 and SEC-11 to SEC-13.
- *Collicity — Technical Design (Draft v0.2)*, in `docs/`: the stack, building blocks and team-size timelines behind §5, §16 and §21, and the reasoning behind this version's changes.
- [Microsoft Entra: develop a SCIM endpoint](https://learn.microsoft.com/en-us/entra/identity/app-provisioning/use-scim-to-provision-users-and-groups): provisioning cycles run about every 40 minutes (CON-06).
- [Microsoft Entra: continuous access evaluation](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-continuous-access-evaluation): critical events, including a disabled user, can take up to 15 minutes to propagate (CON-06).
- [Unicode Technical Standard #39](https://www.unicode.org/reports/tr39/): skeletons for mixed-script look-alikes (CON-10).
