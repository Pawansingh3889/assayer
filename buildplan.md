# Build Plan — One Interface Between the User and All the Data

**Core idea.** A single pane of glass where a user sees their entire data estate and asks
questions of it in plain language, where every answer is governed at the boundary and every
access is recorded in an audit trail a third party can verify.

**Strategy.** Adopt the free layers that already exist and are better than anything you'd
build solo. Reuse your own tools where they genuinely fill a gap. Build only the parts that
do not exist — which, after the landscape survey, is a much shorter list than it looked.

Assumed horizon: ~10 weeks part-time, solo, targeting a first client conversation at the end.
Adjust the phase lengths, not the order.

---

## 1. The stack, by decision type

### ADOPT — free, mature, don't rebuild

| Layer | Tool | Licence | Why |
|---|---|---|---|
| Governed query | **WrenAI** | Apache-2.0 (`core/`, `sdk/`, `skills/`) | MDL semantic layer, row + column access control enforced before SQL hits the DB, SQL Server connector, Ollama support. 16.7k stars |
| Policy + audit spine | **microsoft/agent-governance-toolkit** | MIT | Merkle audit chain, inclusion proofs, `agt verify` CLI, evidence mapped to EU AI Act / ISO 42001 / SOC 2 |
| Local inference | **Ollama** or **LocalAI** | MIT | No cloud keys, no GPU requirement |
| Catalogue backbone | **OpenMetadata** *(optional, phase 3)* | Apache-2.0 | 120+ connectors, column-level lineage, no SQL execution path — zero blast radius |
| Anchoring reference | **halo-record** | Apache-2.0 | Its witness/omission-detection protocol is the design you want. Read it before writing yours |

### REUSE — your existing work, repositioned

| Repo | New role | Honest assessment |
|---|---|---|
| **schema-scout** | Catalogue + **agentic-readiness scoring** | Keep and invest. Readiness scoring exists nowhere else in the survey — this is a real differentiator and a billable pre-sales artefact |
| **sql-sop** | SOP/runbook layer + public credibility | Keep publishing. 500+ monthly downloads is your only third-party validation |
| **FloorMind** | The BRC assembly and reference deployment | Becomes the domain product, not the platform |
| **sql-steward** | **Policy + audit boundary only** — retire its query/semantic side | Painful but correct. Maintaining a semantic layer against WrenAI's is a losing battle; the governed-API-plus-ledger part is what was actually valuable |
| **OpsMind** | Operator surface: fleet health, scan scheduling, metric promotion workflow | Keep it operator-facing. Do not let it become the client-facing brand |

### BUILD — genuinely does not exist

1. **Witnessed anchoring for agent data-access decisions.** External witness, published root
   hashes, omission detection. Microsoft's chain is local; a chain you compute yourself proves
   nothing to a regulator because you can recompute it. This is the whole moat.
2. **The unified interface.** Catalogue + Ask + Boards + Ledger over heterogeneous sources.
   Thin — it proxies WrenAI, it does not reimplement it.
3. **BRC/HACCP domain pack.** Metric definitions, compliance question templates, evidence
   mappings. Nothing in the survey has a food-safety layer. Least copyable asset you own.

---

## 2. Architecture

```
        USER
          │
┌─────────▼──────────────────────────────────────┐
│  INTERFACE  (build — thin)                     │
│  Catalogue · Ask · Boards · Ledger             │
│  no DB driver, no connection string            │
└─────────┬──────────────────────────────────────┘
          │
┌─────────▼──────────────────────────────────────┐
│  GOVERNANCE SPINE  (adopt + extend)            │
│  AGT policy engine  ·  Merkle audit chain      │
│  + WITNESSED ANCHORING  ◄── the build          │
└─────────┬──────────────────────────────────────┘
          │
┌─────────▼──────────────────────────────────────┐
│  GOVERNED QUERY  (adopt)                       │
│  WrenAI — MDL semantic layer, RLAC/CLAC        │
└─────────┬──────────────────────────────────────┘
          │
   ┌──────▼──────┐   ┌──────────────┐   ┌─────────────┐
   │ SQL Server  │   │ schema-scout │   │  Ollama     │
   │ (read-only) │   │  catalogue   │   │  local LLM  │
   └─────────────┘   └──────────────┘   └─────────────┘
                            ▲
                   ┌────────┴────────┐
                   │  BRC/HACCP pack │  (build)
                   └─────────────────┘
```

The one invariant: **every path from user to data passes through the governance spine, and
every transit leaves a witnessed ledger entry.** If any feature needs an exception to that,
the feature is wrong.

---

## 3. Phases

Each phase has a kill criterion. If it trips, stop and re-plan rather than pushing through —
the whole point of adopting free layers is that you find out early when they don't fit.

### Phase 0 — Prove the seam (week 1)

Stand up WrenAI + SQL Server + Ollama locally. Point schema-scout at the database. Ask three
real BRC questions through WrenAI's MDL and try, deliberately, to make it leak a PII column.

**Deliverable:** a one-page memo — does WrenAI's row/column access control actually hold?
**Kill criterion:** if RLAC/CLAC can be talked around by a determined prompt, WrenAI is not
your access layer and the whole plan changes. Find this out in week one, not week six.

### Phase 1 — Governance spine (weeks 2–3)

Put AGT between the interface and WrenAI. Every query gets a policy decision and a ledger
entry before execution. Wire sql-steward's existing ledger format into AGT's chain, or migrate
to AGT's outright — do not run two.

**Deliverable:** every query answered or refused, with a chain entry, verifiable via `agt verify`.
**Kill criterion:** if AGT's policy model can't express your compile-time PII blocking, keep
sql-steward's boundary and take only AGT's audit module.

### Phase 2 — Witnessed anchoring (weeks 4–5) — *the differentiator*

Build the part that doesn't exist. Periodically publish a signed root hash to an external
witness, and implement omission detection so a deleted entry is as detectable as an edited one.
Read halo-record's protocol first; consider depending on it rather than reimplementing.

**Deliverable:** a `verify` command a third party can run, offline, against a published root,
that detects both tampering *and* omission.
**Kill criterion:** none — if this doesn't work, there is no product. Give it the time it needs.

### Phase 3 — The interface (weeks 6–8)

Four screens, built thin. Catalogue (schema-scout data, governance badges, readiness scores),
Ask (proxied to WrenAI, provenance block on every answer, refusals rendered as first-class
results), Boards (pinned metric tiles, no ad-hoc SQL tile type), Ledger (browsable, with a
live **Verify chain** button).

Study Querybook's UX before designing. Build order: Catalogue → Ledger → Boards → Ask. Ask is
the most fun and the least urgent; WrenAI already does the hard part.

**Kill criterion:** if you're writing query logic in the interface, stop — it belongs in WrenAI.

### Phase 4 — Domain pack and demo (weeks 9–10)

BRC/HACCP metric definitions, compliance question templates, evidence export mapped to audit
requirements. Rebuild the FloorMind evidence report against the new stack. Rehearse the demo.

**Deliverable:** a six-minute demo ending on live chain verification, and an evidence PDF a
compliance officer would actually file.

---

## 4. Traps found while checking (act on these)

**WrenAI licensing — verified, mostly fine, two conditions.**
The LICENSE states plainly: *"WrenAI is multi-licensed. The license that applies depends on the
path within this repository."* Apache-2.0 covers `core/`, `sdk/`, `skills/`, `examples/` and root
files; `docs/` is CC-BY-4.0; **AGPL-3.0 is reserved for future modules.** So:

1. **Pin a version and re-read the LICENSE on every upgrade.** An AGPL module landing in a path
   you depend on would be a serious problem for a commercial consulting product, and it is
   explicitly signposted as coming.
2. **Trademarks are excluded** — "Wren", "WrenAI" and the logos remain Canner's property. Your
   product cannot carry Wren branding, and you should say "built on WrenAI" rather than
   anything that implies endorsement.

**Footprint versus the 11GB laptop.**
WrenAI plus SQL Server plus Ollama plus your interface plus AGT is a lot of containers. The
"runs on an 11GB laptop with no cloud keys" line is the strongest sentence in your pitch and
this plan puts it at risk. Two mitigations: WrenAI now offers a CLI/pip installation path
alongside the container stack — prefer it — and defer Keycloak until a client actually asks for
SSO. Measure the footprint at the end of Phase 0 and treat the number as a requirement, not an
outcome.

**Vendor every front-end asset locally from commit one.** A CDN-loaded font or chart library
silently breaks the air-gap claim, and a buyer's security team checks the network tab.
Retrofitting this is miserable.

**Air-gap acceptance testing must cover first boot.** Most competitors fail here — RAGFlow,
Onyx and Open WebUI all fetch model weights on first run, n8n phones home at licence
activation. Make sure you don't, then make it a demo talking point.

---

## 5. Open source versus commercial

Mirror what already worked for you with sql-sop.

**Open (Apache-2.0):** the witnessed-anchoring library, and schema-scout's readiness scoring.
These build credibility, invite scrutiny that makes the crypto trustworthy, and are the natural
top of a consulting funnel. A tamper-evidence library nobody can audit is a contradiction.

**Commercial:** the BRC/HACCP domain pack, the interface, deployment and audit-preparation
services. Domain depth and delivery are what clients pay for; the primitives are what get you
in the room.

---

## 6. Week one checklist

- [ ] `git clone` WrenAI, read `LICENSE` in full, record the commit SHA you're pinning
- [ ] WrenAI + SQL Server + Ollama running locally; note peak RAM
- [ ] schema-scout pointed at the SQL Server, catalogue exported
- [ ] Three BRC questions answered through the MDL
- [ ] One deliberate attempt to breach RLAC/CLAC — write down what happened
- [ ] Read halo-record's witness protocol end to end
- [ ] Skim AGT's audit module — does its policy model fit compile-time PII blocking?

---

## 7. Decisions still needed from you

1. **Does sql-steward's query side really get retired?** It is the right call architecturally
   and the hardest one emotionally. Decide before Phase 1, not during.
2. **Product name.** Cannot reference Wren. Needed before the first public commit.
3. **Is OpsMind in scope for v1 at all?** The plan works without it. Operator tooling is easy
   to defer and easy to let sprawl.
4. **Which client conversation is this aimed at?** The BRC pack should be built for a named
   prospect's actual audit requirements, not a generic reading of the standard. Generic domain
   packs are just documentation.
