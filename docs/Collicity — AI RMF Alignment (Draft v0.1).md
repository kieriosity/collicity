# Collicity — AI RMF Alignment (Draft v0.1)

Oct 11, 2026 · @Philip Maynard · Companion to *Collicity — Product Specification (Draft v0.19)*

## 1. Purpose and status

This document maps the NIST AI Risk Management Framework (AI RMF 1.0, NIST AI 100-1) and its Generative AI Profile (NIST AI 600-1) to the Collicity product specification. It shows where the specification already meets each part of the framework, which gaps the review found, and which items belong to the company rather than the product.

- **Aligned, not compliant.** The AI RMF is voluntary and has no certification, so Collicity describes itself as aligned with it. ISO/IEC 42001, which can be certified, is on the compliance roadmap if customers call for it (spec SEC-05), and this mapping is a step toward it.
- **Provider, not deployer.** Collicity provides an AI system that tenants deploy. Some outcomes, such as a tenant's own AI policy, belong to the tenant; Collicity supports them with notice language, audit records and controls.
- **A moving target.** NIST says AI RMF 1.0 is being revised, and its AI overlays for SP 800-53 (the COSAiS project) aren't published yet. This mapping will be redone when either appears (§7).
- **Category names** below are short paraphrases of the framework's wording. Requirement IDs refer to the specification.

## 2. Trustworthiness characteristics

| Characteristic | How Collicity addresses it |
| --- | --- |
| Valid and reliable | Verified-success labels and lower-bound routing (INV-06, RTR-02); deterministic checks that gate delivery (SUP-01); an independent evaluation set (SUP-06); status computed only from recorded state (RUN-08) |
| Safe | Five product invariants and 26 master rules enforced outside the model (spec §1); an approval or narrow mandate for every side effect (ASG-11, ASG-24); hard lines that are never mandated; the kill switch (ASG-13) |
| Secure and resilient | One connector gateway holding every credential (spec §16); prompt-injection defenses (CON-10, SEC-11 to SEC-13, SEC-18); durability targets (spec §17) |
| Accountable and transparent | A tamper-evident audit log (ADM-07); on-behalf-of attribution and agent identity (CON-07, MR-04); AI disclosure on everything Collicity sends (SEC-14) |
| Explainable and interpretable | Router decision records (RTR-04); citations on narrative and answers (TPL-07, QA-02); approval screens that show the exact action (ASG-11) |
| Privacy-enhanced | Data minimization and deletion (SEC-04); visibility that follows the sources (SEC-08); approved models, endpoints and regions only (MR-07); notice for people affected who aren't users (SEC-19) |
| Fair, with harmful bias managed | Evaluation stratified by sender category, so miss and error rates can be compared across senders (SUP-06). The beta's automatic decisions about senders are limited to triage, identity confidence and throttling (ASG-15, ASG-17); the evaluation set samples what triage drops (SUP-06), and every change Collicity makes is approved by a person |

## 3. Core functions

| Category | How Collicity addresses it | Gaps and notes |
| --- | --- | --- |
| **GOVERN 1:** policies and practices for AI risk are in place and followed | Master rules, invariants, release tests and detection rules (spec §1, §19); a security review for every rule change | Risk tolerance recorded (SEC-21, at GA); a company AI policy is still to write (§6 below) |
| **GOVERN 2:** accountability; people are empowered, responsible and trained | Roles (ADM-01, §18.1); the product owner signs off rule changes; custodians (RUN-06); approver authority (ASG-25) | Training is a company action (§6 below) |
| **GOVERN 3:** workforce diversity, equity, inclusion and accessibility inform AI risk work | Accessibility: WCAG 2.2 AA on every client (PLT-06) | Company action (§6 below) |
| **GOVERN 4:** a culture that considers and communicates AI risk | A threat model per feature (SEC-02); decisions and open questions recorded (spec §21) | Company action (§6 below) |
| **GOVERN 5:** engagement with relevant AI actors | Design partners (spec §18.1); outside feedback (SEC-22); bot-verification programs (CON-20) | Closed in v0.19 (SEC-22) |
| **GOVERN 6:** third-party software, data and supply-chain risk | Model and supplier risk (PRV-06); signed connector manifests, pinned tool descriptions and admin approval (CON-12, CON-13, CON-15); provider data terms (PRV-05) | Closed in v0.19 (PRV-06) |
| **MAP 1:** context is established and understood | Personas, goals and non-goals (spec §2, §3); the beta contract (spec §18.1) | — |
| **MAP 2:** the AI system is categorized | Task types and criticality (INV-02, RTR-02); impact tiers (RUN-04); data classes (spec §15) | — |
| **MAP 3:** capabilities, goals, benefits and costs are understood against benchmarks | Cost against a frontier-only baseline (G3); time saved against a week-0 baseline (G6) | — |
| **MAP 4:** risks are mapped for every component, third parties included | The threat table (spec §15); per-connector manifests (CON-12); supplier assessments (PRV-06) | — |
| **MAP 5:** impacts on individuals, groups, communities and society are characterized | People affected who aren't users (SEC-19); evaluation by sender category (SUP-06) | Closed in v0.19 for people who aren't users (SEC-19); impacts beyond customers and their correspondents aren't assessed |
| **MEASURE 1:** methods and metrics are chosen and applied | Evidence classes (spec §9.2); metrics and guardrails (spec §19); master-rule checks | — |
| **MEASURE 2:** the system is evaluated for trustworthiness | The evaluation set (SUP-06); the red-team suite (SEC-02); invariant suites | — |
| **MEASURE 3:** identified risks are tracked over time | The model inventory (INV-01 to INV-07); guardrail dashboards and detection rules (spec §19); outside reports (SEC-22) | — |
| **MEASURE 4:** feedback on whether measurement works is gathered | Supervisor precision and recall against the evaluation set (spec §19); approval quality (ASG-27); outside feedback (SEC-22) | Closed in v0.19 (ASG-27) |
| **MANAGE 1:** risks are prioritized and treated | Tiers and hard lines (spec §1); approvals and mandates (ASG-11, ASG-24); recorded risk tolerance (SEC-21, at GA) | Specified in v0.19 (SEC-21, P1, built at GA) |
| **MANAGE 2:** benefits are maximized and harms minimized, including the ability to disengage or deactivate | The kill switch (ASG-13); mandate suspension (ASG-24); alpha features that switch off without a release (spec §1) | — |
| **MANAGE 3:** third-party risks are managed | Supplier risk (PRV-06); admin-approved custom connectors (CON-15); per-version model records (INV-03) | — |
| **MANAGE 4:** response, recovery and communication are documented and monitored | AI incidents with a 72-hour notice to tenants (SEC-20); recovery and compensation (RUN-06); the injection runbook (SEC-02); retirement (SEC-21) | Closed in v0.19 (SEC-20) |

## 4. Generative AI Profile risks

| Risk (NIST AI 600-1) | Relevance to Collicity | Controls |
| --- | --- | --- |
| CBRN information or capabilities | Low: chat and Q&A could be misused | Provider safeguards; an acceptable-use policy (§6 below) |
| Confabulation | High: narrative, summaries and proposals | Deterministic and grounded checks (SUP-01, SUP-02); quotes rendered from stored source snapshots (SUP-01); cited answers (QA-02); honest status (RUN-08) |
| Dangerous, violent or hateful content | Low | Provider safeguards; an acceptable-use policy (§6 below) |
| Data privacy | High: mail and documents about third parties | SEC-04, SEC-08, MR-07, data classes (spec §15); notice for people affected (SEC-19) |
| Environmental impacts | Low | Cheapest-capable routing reduces compute (RTR-02); not measured (§6 below) |
| Harmful bias and homogenization | Medium: triage could treat senders unevenly | Evaluation by sender category (SUP-06); identity confidence rules (ASG-15) |
| Human-AI configuration | High: people approve what the agent proposes | Approval screens with the exact action (ASG-11); batch limits; approval quality (ASG-27); AI disclosure (SEC-14) |
| Information integrity | Medium | Provenance and lineage (SEC-12); AI disclosure (SEC-14); signed agent requests (CON-17, alpha) |
| Information security | High | Master rules, the gateway and the injection defenses (spec §1, §6, §15) |
| Intellectual property | Low | Provider data terms (PRV-05); no training on customer data (SEC-04) |
| Obscene, degrading or abusive content | Low | Provider safeguards; an acceptable-use policy (§6 below) |
| Value chain and component integration | High: model providers, connectors, MCP servers | Supplier risk (PRV-06); pinned tool descriptions (CON-13); signed manifests (CON-12); admin-approved connectors (CON-15) |

## 5. Gaps closed in spec v0.19

| Gap | Framework reference | Spec change |
| --- | --- | --- |
| Approvers can rubber-stamp, especially in batches | AI 600-1 human-AI configuration; MEASURE 4 | ASG-27 approval quality; a diagnostic metric and a threat row |
| No assessment of model providers and subprocessors, or plan for deprecations and provider incidents | GOVERN 6; MANAGE 3; AI 600-1 value chain | PRV-06 model and supplier risk; a threat row |
| People whose mail and documents Collicity processes get no notice | MAP 5; AI 600-1 data privacy | SEC-19 people affected who aren't users |
| No response or customer notice for AI incidents beyond prompt injection | MANAGE 4 | SEC-20 AI incidents; a guardrail metric |
| Accepted residual risk and retirement weren't recorded | MANAGE 1; GOVERN 1 | SEC-21 risk tolerance and retirement (P1, at GA) |
| No channel for outside parties to report problems | GOVERN 5; MEASURE 3 | SEC-22 outside feedback |
| Miss and error rates weren't compared across senders | AI 600-1 harmful bias | SUP-06 stratified by sender category |

## 6. Company actions outside the product

These are outcomes the framework expects of the organization. None is a product requirement.

- **AI policy (GOVERN 1):** a short company policy on how Collicity develops and runs AI, pointing to the master rules.
- **Roles and training (GOVERN 2):** named owners for AI risk, security review and incidents, and training as the team grows from one person to three.
- **Diversity and accessibility practices (GOVERN 3):** hiring and review practices as the team grows.
- **Risk culture (GOVERN 4):** regular review of incidents, evaluation results and the risk-tolerance record (spec SEC-21).
- **Acceptable-use policy:** terms that forbid using chat and Q&A for harmful content, backing the providers' safeguards.
- **Environmental reporting (optional):** token and compute use per tenant, if customers ask for it.

## 7. Maintenance

- Redo this mapping when NIST publishes the revised AI RMF or the SP 800-53 AI overlays.
- Review it with each new version of the specification, and before an ISO/IEC 42001 readiness assessment (spec SEC-05).

## Revision history

| Version | Date | Changes |
| --- | --- | --- |
| v0.1 | 2026-10-11 | First draft: trustworthiness characteristics, core functions and Generative AI Profile risks mapped to spec v0.19; the gaps v0.19 closed; company actions; maintenance |

## Sources

- [NIST AIRC: AI Risk Management Framework](https://airc.nist.gov/airmf-resources/airmf/): AI RMF 1.0, its four functions, and the note that a revised version is in progress.
- [NIST AI 100-1: Artificial Intelligence Risk Management Framework (AI RMF 1.0)](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf): the categories and trustworthiness characteristics paraphrased in §2 and §3.
- [NIST AI 600-1: Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf): the twelve risks in §4.
- [NIST: AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework): NIST's related profiles and resources.
- [NIST CSRC: control overlays for securing AI systems](https://csrc.nist.gov/News/2025/webinar-nist-security-control-overlays-for-ai-syst): the SP 800-53 AI overlays still in development.
