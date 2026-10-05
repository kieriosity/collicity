# Archive: Compliance Assistant design plans

**Status:** superseded on 2026-10-05 by the Collicity product specification:
https://claude.ai/code/artifact/8762acb1-2d81-41e4-b799-1f2706510c18

Collicity is the broader platform: autonomous worker agents for day-to-day tasks. The compliance assistant is one customer vertical of it. These files are kept as reference, not as live specs.

## What was carried into the Collicity spec

- Mail authentication is not sender identity; identity confidence levels (ASG-15)
- Sharing beyond source access is an explicit, logged publish (QA-03)
- Visibility follows the sources and is enforced at every exit, including prompt assembly and client caches (SEC-08)
- Discarded and evaluation-set items are retained 90 days so missed items can be measured (§15 data classes)
- Citation integrity (deterministic) is separate from semantic support (probabilistic); quotes come from snapshots (§9.2, SUP-01)
- Hidden-in-original content is flagged and cannot carry a change on its own (CON-10)
- Approval requests record their base version and go stale (RUN-04)
- Database tenant-isolation details (SEC-09)
- The v3 technology stack as Collicity's default (§5, §16)

## Still worth reusing

For any email- or chat-triggered Collicity workflow, starting with the beta (email request triage → approved actions), whose intake follows this design: ingestion semantics (dedup keys with scope, capture-before-checkpoint, cursor-expiry recovery, sync gaps), the stratified recall and adjudicated precision method, authority predicates, the suppression floor, and the acceptance cases.

## Reading order

Each version lists only its changes from the one before, so the latest state is spread across files.

| File | Version | Status |
| --- | --- | --- |
| `compliance-assistant-design-plan.md` | v1 | Superseded by v2 |
| `compliance-assistant-design-plan-v2.md` | v2 | Partly current: v3 cites its deployment boundaries (§8) and platform-work list (§9). Its "enforce visibility at every read path" list (§5.2) was partly dropped in v3 |
| `compliance-assistant-design-plan-v3.md` | v3 | Partly current: every v3.1 section marked "unchanged from v3", including frozen technology choices (§12) |
| `compliance-assistant-design-plan-v3-1.md` | v3.1 | Latest |
| `acceptance-cases-release-1.md` | v3 | Partly current: AC-10 and AC-14 |
| `acceptance-cases-release-1-v3-1.md` | v3.1 | Latest |
