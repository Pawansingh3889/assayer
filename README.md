# Assayer

**One interface between your people and all of your data — where every answer is governed at
the boundary and every access leaves evidence a third party can verify.**

Assayer sits in front of your data estate. Users see everything that exists and ask questions
in plain language. The agent can only compute pre-approved metrics, never free-form SQL. Every
question, decision and refusal is written to a hash-chained ledger whose roots are published to
an external witness — so the audit trail is evidence, not just a log you happen to keep.

Runs entirely on your own hardware. No cloud API keys. No data leaves the building.

---

## Why this exists

Governed database access for AI agents is a solved problem — [WrenAI][wren] solves it well, and
Assayer uses it rather than reimplementing it. What is *not* solved is proving to an auditor
that the governance held.

Every mainstream agent-observability platform (Langfuse, Phoenix, AgentOps) records agent
activity in an ordinary database table. Anyone with write access can revise history. The
handful of projects that hash-chain their logs still compute and store the chain themselves,
which proves nothing — a dishonest operator recomputes the whole chain in seconds.

Assayer publishes signed Merkle roots to an external witness on a fixed cadence. Tampering
breaks the chain; **deletion breaks the sequence**; both are detectable by anyone holding the
published roots, offline, without trusting the operator or the vendor.

That is the whole product. Everything else is assembly.

---

## Quick start

Requires Docker, ~8 GB free RAM, and an Ollama-compatible model. No API keys.

```bash
git clone https://github.com/<you>/assayer && cd assayer
cp .env.example .env          # set ASSAYER_DB_DSN to a read-only SQL Server account
make up                       # brings up Assayer, WrenAI, Ollama
make seed                     # loads the demo BRC dataset
open http://localhost:8080
```

First success in under five minutes:

1. **Catalogue** — every table in the estate, badged Approved / Candidate / Blocked
2. **Ask** — *"how many CCP deviations were logged last month?"* → answer, with the metrics
   used and the compiled query shown
3. **Ask a blocked question** — anything touching a PII column → refused before any query runs
4. **Ledger** — click **Verify chain**. It recomputes locally and checks against the published
   witness roots

```bash
# the same verification, offline, from the CLI — no network, no Assayer running
assayer verify --ledger ./data/ledger --receipts ./data/receipts
```

```
✓ chain integrity      4,471 entries, no breaks
✓ sequence complete    no gaps, no omissions
✓ epoch continuity     37 epochs, each committing to its predecessor
✓ witness signatures   37/37 roots co-signed, latest 2026-07-28T09:14:02Z
```

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ASSAYER_DB_DSN` | — | Read-only SQL Server account. Assayer never opens this itself; it is passed to WrenAI |
| `ASSAYER_MODEL_ENDPOINT` | `http://ollama:11434/v1` | Any OpenAI-compatible endpoint. vLLM and LM Studio work |
| `ASSAYER_WITNESS_URL` | — | Witness endpoint. May be a customer-controlled WORM bucket, a Rekor instance, or a counterparty |
| `ASSAYER_EPOCH_ENTRIES` | `100` | Entries per epoch before a root is published |
| `ASSAYER_EPOCH_SECONDS` | `900` | Or this long, whichever comes first |
| `ASSAYER_DOMAIN_PACK` | `brc` | Which compliance pack to load |

Full reference: [`docs/configuration.md`](docs/configuration.md).

---

## What Assayer is not

- **Not a BI tool.** No ad-hoc SQL, no user-authored queries, no pivot builder. If you want
  self-serve analytics, use a BI tool — and accept that it has no governance boundary.
- **Not a semantic layer.** WrenAI is the semantic layer. Assayer governs and records it.
- **Not write-capable.** Read-only by construction. No DML, no DDL, not on the roadmap.
- **Not a replacement for your DBA's access controls.** It is a second boundary, not the first.

---

## Documentation

| Doc | Read it when |
|---|---|
| [SPEC.md](SPEC.md) | You need the functional contract — surfaces, ledger format, acceptance criteria |
| [ARCHITECTURE.md](ARCHITECTURE.md) | You need the design decisions and their trade-offs |
| [docs/verification.md](docs/verification.md) | You are the auditor and want to check the evidence yourself |
| [docs/domain-packs.md](docs/domain-packs.md) | You are adapting Assayer to a standard other than BRC |

---

## Built on

[WrenAI][wren] (Apache-2.0) for the governed semantic layer ·
[agent-governance-toolkit][agt] (MIT) for policy enforcement and the base audit chain ·
[Ollama][ollama] for local inference.

Assayer is not affiliated with or endorsed by Canner, Inc. or Microsoft. "Wren" and "WrenAI"
are trademarks of Canner, Inc.

## Licence

The witness and verification libraries are Apache-2.0 — tamper-evidence nobody can audit is a
contradiction, so those are open by design. The interface and the compliance domain packs are
commercially licensed. See [LICENSE](LICENSE).

[wren]: https://github.com/Canner/WrenAI
[agt]: https://github.com/microsoft/agent-governance-toolkit
[ollama]: https://ollama.com
