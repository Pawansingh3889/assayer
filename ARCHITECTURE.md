# Assayer — Architecture

Version 0.1 (draft) · 28 July 2026

Audience: whoever maintains this after the first client deployment, and the security reviewer
who has thirty minutes.

---

## 1. Context and goals

Assayer is deployed on a customer's own hardware, usually alongside a SQL Server estate holding
production and quality data. It is used by two groups: quality and compliance staff asking
questions, and auditors checking that the answers were governed.

**Goals**

1. A user can see the whole estate and question a governed subset of it
2. An auditor can verify the access record without trusting the operator or us
3. The whole system runs air-gapped, on modest hardware, with no cloud account

**Non-goals**

Self-serve analytics · write access · being the customer's only access control · scale beyond
a single site in v1

**Constraints that shaped everything below**

- No GPU. Inference is CPU-bound or a small quantised model, so the design must tolerate
  10–60 second answers and cannot depend on a chatty multi-call agent loop
- Air-gapped, including first boot. Most competitors fetch model weights on first run; we cannot
- Solo maintainer. Every component we build is a component we maintain forever

---

## 2. High-level design

```
                             USER
                              │
              ┌───────────────▼────────────────┐
              │  INTERFACE                     │   built
              │  Catalogue · Ask · Boards ·    │   thin, no DB driver
              │  Ledger                        │
              └───────────────┬────────────────┘
                              │  internal API
              ┌───────────────▼────────────────┐
              │  GOVERNANCE SPINE              │
              │  ┌──────────────────────────┐  │
              │  │ policy  (AGT)            │  │   adopted
              │  ├──────────────────────────┤  │
              │  │ ledger  (AGT chain)      │  │   adopted
              │  ├──────────────────────────┤  │
              │  │ WITNESS + ANCHOR         │  │   built ◄── the moat
              │  └──────────────────────────┘  │
              └───────────────┬────────────────┘
                              │  QueryPort
              ┌───────────────▼────────────────┐
              │  GOVERNED QUERY  (WrenAI)      │   adopted
              │  MDL semantic layer, RLAC/CLAC │
              └───────────────┬────────────────┘
                              │
      ┌───────────────┬───────┴────────┬──────────────────┐
      ▼               ▼                ▼                  ▼
  SQL Server     schema-scout      Ollama            witness
  (read-only)    catalogue +       local model       (external,
                 readiness score                     customer-held)
```

Domain packs (BRC/HACCP) are configuration loaded into the semantic layer and the evidence
exporter — not code paths.

---

## 3. Data flow

**A question**

1. Interface posts the question and the caller's OIDC subject to the spine
2. Policy evaluates: is this actor permitted these metrics? does any referenced column carry a
   Blocked classification?
3. **Refusal path** — a ledger entry is written with `outcome: refused` and the rule that fired.
   No query is constructed. Response returns with the reason.
4. **Allow path** — the request passes through `QueryPort` to WrenAI, which resolves it against
   the MDL semantic layer and compiles SQL, applying its own row- and column-level controls
5. WrenAI executes against the read-only SQL Server account and returns rows
6. Spine writes the ledger entry: metrics used, query hash, row count, `outcome: answered`
7. Interface renders the answer with its provenance block

**An epoch close** — every N entries or T seconds: Merkle root computed, committed to the prior
root, signed, published to the witness, receipt stored. A failed publish writes its own entry.

**Verification** — reads the ledger and receipt directories from disk. No running service, no
network, no Assayer process required.

---

## 4. Key decisions

### AD-1 — Adopt WrenAI rather than build a semantic layer

*Decision.* WrenAI (Apache-2.0) is the governed query layer. We do not build one.

*Why.* It has row- and column-level access control enforced before SQL reaches the database, a
SQL Server connector, local-model support, and years of work behind it. A solo maintainer
building a competing semantic layer loses.

*Trade-offs.* We inherit someone else's roadmap. The LICENSE explicitly reserves AGPL-3.0 for
future modules, which would be a serious problem for a commercially-licensed interface. The
Wren trademarks are excluded from the licence grant.

*Mitigations.* Pin a commit; re-read the LICENSE on every upgrade as a release-checklist item.
Access WrenAI only through `QueryPort` (AD-7) so replacement is a week, not a rewrite. Never use
Wren branding; describe the relationship as "built on".

### AD-2 — Adopt agent-governance-toolkit for policy and the base chain

*Decision.* Microsoft's AGT (MIT) provides policy enforcement and the Merkle audit chain.

*Why.* It already maps evidence to EU AI Act, ISO/IEC 42001 and SOC 2, which is months of work
we would otherwise do badly.

*Trade-offs.* Created March 2026 — young, and its star count reflects Microsoft's distribution
rather than production hardening. Its policy model may not express compile-time PII blocking
the way we need.

*Mitigation.* Wrap it behind our own policy interface. If the model does not fit, keep
sql-steward's boundary and take only AGT's audit module. This is the Phase 1 kill criterion.

### AD-3 — Build the witness and anchoring layer ourselves

*Decision.* External witnessing, epoch anchoring and omission detection are ours.

*Why.* This is the only genuinely unoccupied ground. Mature transparency-log infrastructure
(Trillian, Rekor) has no agent integration; every agent-observability platform has no
cryptographic integrity; the ~15 single-author hash-chain projects — and AGT — keep the chain
local. **A chain the operator computes and stores proves nothing, because the operator can
recompute it.** Everything else in Assayer is assembly; this is the product.

*Trade-off.* Cryptographic code written by one person is a liability. Hence Apache-2.0 (AD-6)
and a spec (SPEC §4.3) precise enough for independent re-implementation.

*Prior art to read before writing any of it:* `halo-record`'s witness protocol, which addresses
omission detection directly and may be worth depending on rather than reimplementing.

### AD-4 — The interface holds no database driver

*Decision.* No DB driver, no connection string handling, anywhere in the interface's dependency
tree.

*Why.* A property provable from `package.json` and `requirements.txt` in under a minute beats
any amount of "we restrict access in the application layer" in a security review.

*Trade-off.* Every data need becomes an API call, including ones where direct reads would be
trivial. Accepted — the guarantee is worth more than the convenience.

### AD-5 — The ledger stores hashes, never payloads

*Decision.* Question text, result rows and column values never enter the ledger. Plaintext lives
in a separate, erasable side store keyed by entry ID.

*Why.* Two reasons, and the second is the one that matters commercially. First, an audit trail
that accumulates PII is a liability rather than an asset. Second, an append-only ledger and a
GDPR erasure request are in direct conflict — this design resolves it. Erasure removes the side
-store record; the ledger entry, its hash and the chain survive intact. The audit trail is not
falsified by exercising a data subject's rights.

*Trade-off.* An auditor cannot read what was asked, only that something was asked, by whom, and
what was decided. Acceptable — the audit question is whether governance held, not what people
were curious about.

### AD-6 — Open the crypto, licence the domain

*Decision.* Witness and verification libraries Apache-2.0. Interface and domain packs
commercially licensed.

*Why.* Tamper-evidence nobody can audit is a contradiction; the verification path must be open
or it is not evidence. Meanwhile the copyable part is the primitives and the defensible part is
the domain depth — nothing in the 2026 landscape survey has a food-safety compliance layer.

### AD-7 — Ports and adapters at every adopted boundary

*Decision.* `QueryPort`, `PolicyPort`, `CataloguePort`, `WitnessPort`. Adopted components sit
behind them.

*Why.* Two of our three adopted dependencies are governed by other people's licences and
roadmaps, and one of them (AD-1) has a signposted licence change coming. Microsoft's SQL MCP
Server is a credible alternative `QueryPort` implementation; a customer may mandate their own
witness. Swapability is a licensing and procurement requirement, not architectural purity.

*Trade-off.* Indirection that a single-implementation system does not need yet. Cheap now,
expensive to retrofit.

---

## 5. Integration points

| Port | Adopted implementation | Alternative | Notes |
|---|---|---|---|
| `QueryPort` | WrenAI MDL | Microsoft SQL MCP Server (Data API Builder) | The alternative is deterministic and NL2SQL-free — a stronger boundary, weaker UX |
| `PolicyPort` | AGT policy engine | sql-steward's existing boundary | Fallback if AGT cannot express compile-time PII blocking |
| `CataloguePort` | schema-scout | OpenMetadata | OpenMetadata has 120+ connectors but no readiness scoring |
| `WitnessPort` | customer WORM bucket | Rekor, auditor endpoint | Multiple witnesses supported; divergence is a hard failure |

Local inference is any OpenAI-compatible endpoint. Ollama by default, vLLM where a GPU exists.

---

## 6. Deployment

One `docker-compose.yml`. Services: interface, spine, WrenAI, Ollama. SQL Server is the
customer's, reached with a read-only account.

**Footprint is a requirement, not an outcome.** "Runs on a laptop with no cloud keys" is the
strongest claim in the pitch and this stack threatens it. Prefer WrenAI's CLI/pip installation
over its container stack. Defer SSO — Keycloak costs roughly a gigabyte and no first client
conversation has required it. Measure peak RSS at every release and treat regressions as bugs.

**Vendor every front-end asset locally from the first commit.** One CDN-loaded font breaks the
air-gap claim, and a buyer's security team checks the network tab. Retrofitting is miserable.

**Air-gap testing must cover first boot**, not just steady state. This is where RAGFlow, Onyx
and Open WebUI all fail, and where n8n's licence activation fails. Being able to demonstrate a
clean cold start on a disconnected host is a competitive advantage, so it needs a test.

---

## 7. Risks

| Risk | Response |
|---|---|
| WrenAI's RLAC/CLAC can be prompted around | Phase 0 exists to find out in week one. If it folds, `QueryPort` moves to Microsoft SQL MCP Server |
| AGPL module lands in a WrenAI path we depend on | Pinned commit; LICENSE re-read on every upgrade; `QueryPort` makes replacement bounded |
| Solo-authored cryptography is wrong | Open source it, spec it for independent re-implementation, prefer depending on `halo-record` over reinventing |
| Stack outgrows the laptop demo | Measure every release; CLI install over containers; defer Keycloak |
| Microsoft extends AGT into witnessed anchoring | Likely within a year. The domain pack, not the crypto, is the durable asset — which is why AD-6 splits them the way it does |
