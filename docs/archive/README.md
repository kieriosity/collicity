# Archive

Superseded documents, kept for reference only. The current specification is [Collicity — Product Specification (Draft v0.16)](<../Collicity — Product Specification (Draft v0.16).md>), and the current technical design, which still follows spec v0.5, is [Collicity — Technical Design (Draft v0.2)](<../Collicity — Technical Design (Draft v0.2).md>); their revision histories list what changed between drafts.

## Earlier Collicity drafts

| Files | Version |
| --- | --- |
| `Collicity — Product Specification (Draft v0.1)` (.md, .docx) | v0.1 |
| `Collicity — Product Specification (Draft v0.2)` (.md, .docx) | v0.2; its heading still reads v0.1 |
| `Collicity — Product Specification (Draft v0.3)` (.md, .docx) | v0.3; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.4)` (.md, .docx) | v0.4; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.5)` (.md, .docx) | v0.5; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.6)` (.md, .docx) | v0.6, Web Bot Auth as an alpha; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.7)` (.md, .docx) | v0.7, the master rules in their original wording; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.8)` (.md, .docx) | v0.8, mandates and the first rule amendments; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.9)` (.md, .docx) | v0.9, pending-review markers and master-rule checks; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.10)` (.md, .docx) | v0.10, the self-reviewed amendments with conditions; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.11)` (.md, .docx) | v0.11, conflicts from v0.10 resolved; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.12)` (.md, .docx) | v0.12, the re-confirmation date and amendment count; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.13)` (.md, .docx) | v0.13, the efficiency requirement, batch approval and mandate suggestions; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.14)` (.md, .docx) | v0.14, batch limits and external-sender exclusions; its diagrams link to the shared images in `../images/` |
| `Collicity — Product Specification (Draft v0.15)` (.md, .docx) | v0.15, the batch cap, mandate exclusions and unlock tiering; its diagrams link to the shared images in `../images/` |
| `Collicity — Technical Design (Draft v0.1)` (.md, .docx) | Technical design v0.1, before spec v0.5 adopted its proposed changes |

## Compliance Assistant design plans

Superseded on 2026-10-05 by the Collicity specification. Collicity is the broader platform: autonomous worker agents for day-to-day tasks. The compliance assistant is one customer vertical of it.

### What was carried into the Collicity spec

- Mail authentication is not sender identity; identity confidence levels (ASG-15)
- Sharing beyond source access is an explicit, logged act, now a published snapshot (QA-03, SEC-10)
- Visibility follows the sources and is enforced at every exit, including prompt assembly and client caches (SEC-08)
- Discarded and evaluation-set items are retained 90 days so missed items can be measured (§15 data classes)
- Citation integrity (deterministic) is separate from semantic support (probabilistic); quotes come from snapshots (§9.2, SUP-01)
- Hidden-in-original content is flagged and cannot carry a change on its own (CON-10)
- Approval requests record their base version and go stale (RUN-04)
- Database tenant-isolation details (SEC-09)
- The v3 technology stack as Collicity's default (§5, §16), until v0.5 replaced it with the stack decided in the technical design
- The release 1 acceptance cases, adopted into the beta gate (§18.1)

### Still worth reusing

For any email- or chat-triggered Collicity workflow, starting with the beta (email request triage → approved actions), whose intake follows this design: ingestion semantics (dedup keys with scope, capture-before-checkpoint, cursor-expiry recovery, sync gaps), the stratified recall and adjudicated precision method, authority predicates, the suppression floor, and the acceptance cases.

### Reading order

Each version lists only its changes from the one before, so the latest state is spread across files.

| File | Version | Status |
| --- | --- | --- |
| `compliance-assistant-design-plan.md` | v1 | Superseded by v2 |
| `compliance-assistant-design-plan-v2.md` | v2 | Partly current: v3 cites its deployment boundaries (§8) and platform-work list (§9). Its "enforce visibility at every read path" list (§5.2) was partly dropped in v3 |
| `compliance-assistant-design-plan-v3.md` | v3 | Partly current: every v3.1 section marked "unchanged from v3", including frozen technology choices (§12) |
| `compliance-assistant-design-plan-v3-1.md` | v3.1 | Latest |
| `acceptance-cases-release-1.md` | v3 | Partly current: AC-10 and AC-14 |
| `acceptance-cases-release-1-v3-1.md` | v3.1 | Latest |
