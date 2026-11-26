# MVP-05D.1 — Episode evidence continuity

Candidate implementation, 26 November 2026. Architecture v2.5.1 §§5, 19.1–19.3; roadmap 05D, GAP-07/09/12. No architecture amendment or acceptance promotion.

## Audit and scope

P2-02 already commits bounded summaries, separate MODEL_INTERPRETED episodes, source-turn IDs, evidence quotes and processing watermarks in a fenced transaction. Memory provides topical/date-filtered retrieval, exact-ID entity links and historical revision fences (05B/C). Reuse these; no new schema, inference, worker, scheduler or embedding dependency.

- Admission formerly matched citations case-insensitively against full original input, including omitted text. Require exact original and contiguous model-visible user evidence before any summary or episode writes. This validates citations, not the interpretation's semantic truth.
- Cue-based episodic context retains original source-turn ID/report date. Add complete short original quotations when space permits, separately labeled from interpretation. Do not truncate a quote into apparently verbatim evidence; longer evidence remains available by ID.
- Direct episode-source lookup formerly checked scope but not current source eligibility. Apply the same predicate as episodic search. A cached hit ID cannot bypass correction, retraction, dispute, sensitivity or expiry. Return episode ID, summary ID, model ID, epistemic status, stored evidence quote and exact-source-match flag with existing turn fields. Administrative archive export is outside this recall API.

Report timestamps are not necessarily real-world experience dates. Existing episodes retain their stored provenance; only stored quotes that match the original exactly are labeled as verbatim in recall. This change does not retroactively certify model visibility for legacy citations or delete records.

## Verification

New regression cases cover exact visible citations, case alteration, omitted middle and synthetic omission markers; atomic summary rollback for omitted citations; provenance and quote retrieval; and five existing source-eligibility cases extended to test direct ID dereference. CI pending. No measured model quality, latency or behavioral acceptance claimed.

## Follow-ups (proposed order)

1. Complete episodic representation: assess independent admission, multi-source provenance and event/report-time distinction; avoid storing routine turns by default.
2. Meta-episodic patterns from distinct eligible episodes, with correction invalidation and model-interpreted status.
3. Extend existing PERSONAL_MEANING records with versioned user definitions and separate inferred associations.
4. Document knowledge and procedure references; Procedure Manager remains authoritative for execution.
5. Knowledge-gap and retrieval feedback review (GAP-04/09/12).

Full derived lineage, semantic claim verification, sensitive-source governance and real-model behavior remain open. No new universal graph or memory service.
