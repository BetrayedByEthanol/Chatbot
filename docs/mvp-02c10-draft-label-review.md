# MVP-02C.10 — Second-pass review of the 94 draft memory-conflict labels

**Status: PROVISIONAL ASSISTANT REVIEW, not independent adjudication.**
The same assistant that helped author the diagnostic has seen model predictions
and scores before this second pass. This review is therefore **not blinded**
and cannot count as the independent human annotation required by
`mvp-02c10-memory-contradiction-evaluation.md`.

**Evidence basis:** exact 94 source pairs from
`scenarios/mvp02c10_memory_conflict_diagnostic.json` at suite SHA256
`2d28d7761920d743addd11e75d2af86b9f229756810bc0fffe6df6e947dc241b`.
The full one-row-per-ID audit is stored in
`scenarios/mvp02c10_label_review_proposal.json`. The previous corpus
remains frozen. Suggested rewrites are *not* silently substituted for the
tested examples or retroactively counted as passing cases.

## Review rubric

The two independent annotation axes must remain separate:

- **Textual directional NLI** (`stored_text` premise, `candidate_text`
  hypothesis): **contradiction** if the assertions cannot coexist under
  aligned identity and temporal scope; **entailment** if the premise
  establishes the hypothesis; **neutral** otherwise. Do not assume an
  unstated date, identity, completed intention or truth of a quotation.
- **Memory-review applicability**: whether the pair is eligible for a
  *potential* same-subject, same-predicate, same-time **review**, based on
  authenticated assertions and source metadata. **Eligibility does not mean
  that the prior Memory record was false or should be replaced.** Explicit
  user corrections must be handled through Memory-owned correction authority
  independently of an arbitrary NLI softmax threshold.

The source dataset intentionally marks same-scope *entailments* as
`review_applicable=true`: that means *comparable for review*, not a positive
conflict. Its `expected_review` outcome is the conjunction of applicability
and contradiction. Thus these are not 1:1 labels.

## Provisional audit

| Second-pass verdict | Cases | Meaning |
|---|---:|---|
| **ACCEPT** | **86** | Current NLI and applicability annotations are defensible under the stated rubric. Not independently verified. |
| **REWORD** | **5** | Gold cannot reliably be adjudicated without narrowing ambiguous temporal/presuppositional/ownership wording. |
| **POLICY_REVIEW** | **3** | Textual NLI neutral is defensible; eligibility depends on how the system handles tentative source claims and active dialogue state. |
| **Total** | **94** | Every original case ID is represented in the machine-readable audit. |

There is **no categorical high-confidence label flip** proposed without
a changed premise or hypothesis. Five test items are underspecified or
pragmatically confounded; seven other benchmark false positives should not
be changed simply because the classifier scored them incorrectly.
The original labels are still **DRAFT_UNREVIEWED**.

### Five suggested wording repairs

| Case | Problem with original draft | Suggested replacement (premise → hypothesis) | Proposed draft NLI |
|---|---|---|---|
| **MC035** | “during the winter” lacks a year and might overlap “now.” | “I did not cycle during winter 2024.” → “I cycle every morning in autumn 2026.” | Neutral |
| **MC039** | “plan to **begin** running next month” pragmatically implies not already running, unlike “I run each morning.” | “I plan to join a running club next month.” → “I run alone each morning.” | Neutral |
| **MC041** | “considering **learning** Italian” clashes pragmatically with current fluency, even though advanced learning is possible. | “I am considering taking an advanced Italian course next year.” → “I speak Italian fluently.” | Neutral |
| **MC048** | “would **learn** guitar” suggests no current skill, unlike “I play guitar daily.” | “If I had more time, I would take guitar lessons.” → “I play guitar daily.” | Neutral |
| **MC089** | German `Ich habe ein Fahrrad` (I have a bicycle) does not *strictly* entail legal ownership; English hypothesis denies **owning** one. | “Ich besitze ein Fahrrad.” → “I do not own a bicycle.” | Contradiction |

**Do not overwrite the old five pairs in place.** If adjudicators approve
the changes, create a versioned v2 corpus with immutable v1 provenance and
rerun **both** FP32 and INT8 on the new corpus. Scores from v1 and v2 must
never be silently pooled or attributed to identical suite SHA256 values.

### Three eligibility-policy questions

| Case | Proposed status | Issue |
|---|---|---|
| **MC071** | Retain draft NLI=neutral; eligibility undecided | “I might enjoy gardening” is a *tentative preference*, not an admitted current enjoyment assertion. “I do not enjoy gardening” may nonetheless resolve uncertainty in conversation. |
| **MC072** | Retain draft NLI=neutral; eligibility undecided | “I think my appointment is on Friday” versus “My appointment is on Thursday” can be a correction of a tentative **working claim**, even if no authoritative Memory record exists. |
| **MC074** | Retain draft NLI=neutral; eligibility undecided | “I may still own an old bicycle” versus “I do not own a bicycle” can settle uncertainty without contradicting an admitted assertion. |

**Suggested contract distinction:** authorization to create a
`memory_records` conflict should be limited to verified admissible source
records; a later assertion may separately resolve a **tentative conversational
hypothesis** without being classified as an authoritative memory conflict.
This is a *proposal for domain-owner review*, not an implemented policy or
permission grant. Keep `review_applicable=false` in the original v1 until
the contract is explicitly decided.

## Review observations that are not annotation errors

- **MC024:** running yesterday vs reading yesterday are independent
  activities. Neutral remains correct despite FP32's high false-alert score.
- **MC039, MC041, MC048:** model contradiction scores >0.95 do not justify
  calling intentions and hypotheticals verified facts. The *wording* still
  merits repair before independent adjudication.
- **MC065:** “My dog is named Luna” and “My cat is named Milo” describe
  different animals. Neutral is correct despite a ~0.98 contradiction score.
- **MC077:** possible swimming tomorrow and office tomorrow can both happen.
  Neutral remains correct even though FP32 crossed 0.90.
- **MC079–MC086:** explicit corrections are contradiction examples for the
  language task, but they should use the existing correction-intent/Memory
  approval path, **not** depend on NLI reaching 0.90.
- **MC093:** running tomorrow as an intention and running yesterday as an
  experience are distinct; INT8's >0.90 score remains a false alert under
  the draft label.

These observations are diagnostic and were known before this review; they
must not be used to declare the dataset independently validated or the
threshold calibrated.

## Required independent adjudication

1. Give **at least two independent reviewers** the original v1 source pairs
   **without model predictions**. Ask each to label directional NLI,
   same-person/entity scope, temporal compatibility, assertion/modality,
   and whether this is (a) an authoritative Memory conflict candidate,
   (b) a tentative conversational update, or (c) neither.
2. For a case whose answer depends on unstated context, prefer
   **AMBIGUOUS/ABSTAIN** to forced three-way NLI. Record reasons. Adjudicate
   disagreements explicitly and retain original and revised annotations.
3. Apply accepted rewrites only to a **new dataset version** with new hash
   and revised labels. Keep the initial v1 benchmark for comparison.
4. Collect a **separate** representative, privacy-safe, never-before-seen
   holdout for model selection/calibration. Do not retrospectively drop the
   five flagged examples and recompute headline accuracy as if unbiased.

**Decision register:** Dataset v1 frozen; 86 provisional accepts, 5 rewrite
proposals, 3 policy-dependent cases; no approval of model routing,
contradiction threshold, Memory admission, or automated correction.
