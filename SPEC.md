# Assayer — Functional Specification

Version 0.1 (draft) · 28 July 2026

Audience: implementers, and the compliance reviewer who has to accept the output as evidence.

---

## 1. The invariant

> Every path from a user to data passes through the governance spine, and every transit leaves
> a witnessed ledger entry.

There are no exceptions, no admin bypass, no debug mode. Any proposed feature requiring an
exception is rejected rather than accommodated. This single sentence is what makes the audit
trail meaningful; the moment one path avoids it, the evidence value of every other entry drops
to zero.

---

## 2. Scope

### In

- Read-only, natural-language question answering over a governed metric layer
- Whole-estate catalogue with per-column governance classification
- Saved metric boards, exportable as compliance evidence
- Hash-chained, externally witnessed audit ledger with offline verification
- Compliance domain packs (BRC/HACCP first)

### Out (v1)

Writes and DDL of any kind · ad-hoc or user-authored SQL · user-defined metrics from the UI ·
scheduling and alerting · multi-tenancy · mobile layouts · row-level security beyond what the
semantic layer already enforces.

`generate ad-hoc SQL` is out permanently, not just for v1.

---

## 3. Surfaces

### 3.1 Catalogue

Displays every table and column discovered by schema-scout, whether or not it is queryable.

Each column carries exactly one classification:

| Badge | Meaning | Queryable |
|---|---|---|
| **Approved** | Reachable through at least one published metric | yes, via metrics |
| **Candidate** | Catalogued, clean, not yet promoted to the semantic layer | no |
| **Blocked** | PII-classified; refused at compile time | never |

Each table shows its agentic-readiness score and last-scan timestamp. Drift between the
catalogue and live schema must be surfaced prominently, not silently reconciled.

**Requirement C-1.** Promotion from Candidate to Approved is an auditable event and writes a
ledger entry naming the approver.

**Requirement C-2.** The catalogue never exposes sample data — names, types and lineage only.

### 3.2 Ask

Natural language in; supervisor → analyst; governed metrics out.

Every response — answered or refused — renders a provenance block containing:

- metrics used, linked to their catalogue entries
- agent hops with timings
- the compiled query, read-only and expandable
- the ledger entry ID

**Requirement A-1.** Refusals are first-class results, not errors. A refusal states the reason
and confirms that no query executed.

**Requirement A-2.** There is no affordance anywhere in the interface to run a query the
metric layer did not produce.

**Requirement A-3.** Computed results are never cached. Catalogue and metric metadata may be.

### 3.3 Boards

Pinned metric tiles. Every tile binds to a named metric; there is no tile type backed by raw
SQL, therefore no path by which a saved board becomes an ungoverned query.

**Requirement B-1.** Board exports carry the same provenance block, rendered for print.

**Requirement B-2.** If a metric definition changes after a board was built, the board must
declare it. Silent redefinition makes exported evidence misleading.

### 3.4 Ledger

Browsable and filterable by actor, date, metric and outcome. Carries a **Verify chain** control
that performs the full verification in front of the user and reports the result plainly.

---

## 4. Ledger format

### 4.1 Entry

Canonicalised per RFC 8785 (JSON Canonicalisation Scheme), hashed with SHA-256.

```json
{
  "seq": 4471,
  "epoch": 37,
  "ts": "2026-07-28T09:12:44.118Z",
  "actor": "sub:8f2c...",
  "question_hash": "sha256:9a1f...",
  "policy": { "decision": "refuse", "rule": "pii.compile_time_block",
              "detail": "column employee.ni_number classified PII" },
  "metrics": [],
  "query_hash": null,
  "rows_returned": null,
  "outcome": "refused",
  "prev_hash": "sha256:4c7e...",
  "entry_hash": "sha256:b03d..."
}
```

**Requirement L-1. The ledger stores hashes, never payloads.** No question text, no result
rows, no column values. This is not only a PII control — it resolves the conflict in §7.

**Requirement L-2.** `seq` is strictly monotonic with no gaps. Gap detection is how omission is
caught; a chain alone cannot detect a cleanly removed tail.

**Requirement L-3.** Every entry commits to its predecessor via `prev_hash`.

### 4.2 Epoch and witnessing

An epoch closes after `ASSAYER_EPOCH_ENTRIES` entries or `ASSAYER_EPOCH_SECONDS`, whichever
comes first. On close:

1. Compute the Merkle root over the epoch's entry hashes
2. Commit to the previous epoch's root, forming a chain of epochs
3. Sign the root with the deployment's Ed25519 key
4. Publish `{epoch, root, prev_root, seq_range, ts, signature}` to the witness
5. Store the witness's countersigned receipt locally

**Requirement W-1.** The witness must be outside the operator's unilateral control. Acceptable:
a customer-controlled WORM bucket, a Rekor instance, a counterparty or auditor endpoint. A
witness the operator can rewrite is not a witness.

**Requirement W-2.** Failure to publish is itself a ledger entry. A silent witnessing outage
is indistinguishable from suppression, so it must be visible.

**Requirement W-3.** Multiple witnesses are supported. Divergence between witnesses is a
verification failure, not a warning.

### 4.3 Verification

`assayer verify` runs offline against a ledger directory and a receipts directory, using
standard primitives only. It must be re-implementable from this spec in an afternoon — an
auditor who has to trust our binary has not verified anything.

Checks, all of which must pass:

1. **Chain integrity** — every `entry_hash` recomputes; every `prev_hash` matches
2. **Sequence completeness** — `seq` contiguous across the whole range
3. **Epoch continuity** — each root commits to its predecessor; no epoch missing
4. **Inclusion** — sampled entries prove into their epoch root
5. **Witness signatures** — every published root has a valid countersigned receipt

---

## 5. Evidence export

Produces a document a compliance officer can file without further processing:

- period covered, and the verification result at time of export
- the witnessed roots covering the period, with their receipts
- questions asked and refused, by actor and by outcome — as counts and hashes, never content
- metric definitions in force during the period, with change history
- mapping to the active domain pack's clauses

**Requirement E-1.** An export whose verification does not pass must say so on its first page.
Exports must not be silently suppressible on failure.

---

## 6. Acceptance criteria

Testable, and the basis of the demo.

| # | Criterion |
|---|---|
| AC-1 | A question touching a Blocked column is refused with no query executed, and produces a ledger entry |
| AC-2 | `assayer verify` passes on a clean ledger with the network disabled |
| AC-3 | Editing one byte of one ledger entry causes verification to fail, naming the entry |
| AC-4 | **Deleting an entry entirely causes verification to fail** via sequence gap — this is the criterion that distinguishes Assayer from every local hash chain |
| AC-5 | Deleting an entire epoch, including its local receipt, still fails against the witness's copy |
| AC-6 | No process in the deployment holds a database driver except WrenAI |
| AC-7 | A cold start on an air-gapped host completes with no outbound network attempt |
| AC-8 | Peak RSS of the full stack stays within the target demo footprint |

AC-4, AC-5 and AC-7 are the ones competitors fail. Automate them first.

---

## 7. Known tension: immutability versus erasure

An append-only ledger and a GDPR erasure request are in direct conflict. A compliance buyer
will raise this, and "we hash everything" must be the prepared answer rather than an
improvisation.

**Resolution.** The ledger holds only salted hashes and metadata, which the ICO's guidance
treats as far weaker personal data than the plaintext — and critically, plaintext lives in a
separate, erasable side store keyed by entry ID. Erasure deletes the side-store record; the
ledger entry, its hash and the chain remain intact, so the audit trail survives an erasure
request without being falsified. The salt is per-deployment and rotatable.

**Open question.** Whether erasure of the side store should itself be a ledger entry. It should
— an unrecorded deletion is exactly what the system exists to prevent — but the entry must not
identify the data subject.

---

## 8. Open questions

1. Witness selection for the first deployment: customer WORM bucket, or a shared auditor
   endpoint? The second is a stronger claim and a harder sell.
2. Does the metric registry need versioning in v1? §3.4 B-2 implies yes.
3. Where does the readiness score live — schema-scout output only, or promoted into the ledger
   as evidence of assessment at a point in time?
4. Do we support more than one domain pack loaded at once? Sites certified to multiple
   standards will ask.
