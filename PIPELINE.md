# Assayer — Delivery Pipeline

Version 0.1 (draft) · 28 July 2026

Audience: whoever maintains the pipeline, and the buyer's security reviewer who wants to run
the gates themselves before signing.

This is the CI/CD contract, the decisions behind it, and the order of work — one file, because
a gate spec split across three documents is a gate spec nobody reads. The product docs
(`SPEC.md`, `ARCHITECTURE.md`, `buildplan.md`, `README.md`) are untouched; requirement IDs like
`AC-7`, `AD-4`, `W-1`, `C-1` refer to them.

---

## 1. The invariant

> Every property Assayer claims in a sales conversation is enforced by a gate that has been
> observed to fail, and every release carries signed evidence of which gates ran against which
> commit.

Assayer's thesis is that a green tick is a log and a log is not evidence. A release process that
ends in a green tick and asks the buyer to take it on faith fails its own thesis. The release
must produce the same class of artefact the product produces: something a third party can check
without trusting us.

Two consequences, both load-bearing:

- **A claim without a gate is not a claim.** If it cannot be gated, it comes out of the README.
- **A gate that has never been seen to fail is not a gate.** §7 makes this testable.

---

## 2. What is actually in the repository

The pipeline is being specified against real code, not a sketch. Everything in this section was
read off disk on 28 July 2026 and is the reason several gates below exist in the shape they do.

```
ARCHITECTURE.md  buildplan.md  README.md  SPEC.md      ← product docs, no code
opsmind/
  backend/       FastAPI · SQLAlchemy async · Alembic · Postgres · 17 test files
    app/{auth,conduct,db,llm,runs,templates,users}/
    pyproject.toml · uv.lock · Dockerfile
  docker-compose.yml · .env.example
  .github/workflows/{ci,live-conduct}.yml
```

### 2.1 What the existing CI already does well

`opsmind/.github/workflows/ci.yml` runs on every push and pull request, and it is a better
starting point than most: a **real Postgres 17 service rather than SQLite**, so the repository
layer is exercised against the engine it ships on; **migrations applied from scratch** on every
run, so a broken Alembic chain fails immediately; then `ruff` (with `E,F,I,UP,B,ASYNC`),
`black --check`, `mypy` in **strict** mode, and `pytest`. Dependencies install with
`uv sync --locked`, so the lockfile is authoritative rather than decorative.

The workflow also carries an explicit design comment worth keeping: *"No `ANTHROPIC_API_KEY`:
the suite fakes the LLM at the client wrapper, so it must pass without one. If a test ever needs
a key, that is the bug."* That is a stated invariant with no enforcement — G-2 turns it into one.

Everything below is additive. None of it replaces what is there.

### 2.2 Finding 1 — OpsMind is not what `buildplan.md` says it is

`buildplan.md` describes OpsMind as *"Operator surface: fleet health, scan scheduling, metric
promotion workflow."* The code is a **survey and conduct service**: `app/templates/` (generation,
estimation, snapshots, visibility), `app/runs/` (execution, summaries), `app/conduct/` (an
engine, validation, a repository), plus `app/users/` and `app/auth/`. It generates survey
templates from natural language and conducts conversational survey runs.

That is a different product from the one the build plan schedules. Nothing in the pipeline can
fix a scope mismatch, but every gate below is written against the code that exists, and the
build plan should be reconciled with it before Phase 1.

### 2.3 Finding 2 — OpsMind calls the Anthropic API, and that breaks the strongest claim

`backend/pyproject.toml` declares `anthropic>=0.40`. `app/config.py` carries
`anthropic_api_key` and `anthropic_model`. `docker-compose.yml` passes `ANTHROPIC_API_KEY`
through to the backend. `.github/workflows/live-conduct.yml` runs real conversations against the
real model and documents the cost.

`README.md` says: **"Runs entirely on your own hardware. No cloud API keys. No data leaves the
building."** `SPEC.md` AC-7 requires a cold start on an air-gapped host with no outbound network
attempt. As it stands, OpsMind sends survey content to `api.anthropic.com`.

**The local path already exists, and this is the important half of the finding.**
`app/llm/factory.py` assembles an ordered failover chain, and `app/llm/backup.py` provides
`OpenAICompatibleLLMClient` with a configurable `base_url`. `.env.example` names Ollama and vLLM
as supported backups, notes that `api_key` "may be blank for keyless local servers", and sets a
120-second read timeout with the comment *"Local CPU-served models can need >60s on a cold
load."* Somebody already thought about running this against local inference.

The problem is precedence. `factory.get_llm()` appends the Anthropic client **first** whenever
`settings.anthropic_api_key` is non-empty; backups are only reached when the primary fails. So
the air-gap property reduces to *an environment variable happening to be empty*. That is a hope,
not a guarantee — one populated `.env` on a customer host and the deployment calls out, silently
and successfully.

One thing is already right: with nothing configured at all, `factory.py` returns a bare
`LLMClient()` specifically so construction raises *"ANTHROPIC_API_KEY is not configured"* rather
than degrading silently. The failure is loud. G-3 and G-4 extend that principle from "loud at
runtime" to "impossible at build time."

### 2.4 Finding 3 — the workflows will not run any more

GitHub Actions reads workflows from `.github/workflows/` **at the repository root only**. Now
that OpsMind is nested at `assayer/opsmind/`, its workflows sit at
`opsmind/.github/workflows/` and will never fire. If `assayer/` becomes the repository, CI is
currently off — silently, with no error anywhere.

Related, and independent of the move: `live-conduct.yml` invokes
`python3 scripts/live_conversation.py`, and there is **no `scripts/` directory anywhere in this
tree**. That step would fail on `workflow_dispatch` today. The step also runs from the repository
root while every other step sets `working-directory: backend`, so even a restored script would
need its path checked.

### 2.5 Finding 4 — the compose file is a development file, not a deployment artefact

`opsmind/docker-compose.yml` bind-mounts `./backend:/app`, runs `uvicorn --reload`, executes
`python -m app.seed` on every boot, builds from source rather than a pinned image, pins Postgres
by the mutable tag `postgres:17-alpine` rather than by digest, and publishes `5432:5432` to the
host.

Every one of those is correct for local development and wrong for a customer install. A release
needs a second, separate compose file — digest-pinned images, no bind mount, no `--reload`, no
unconditional seed, no exposed database port. Until that exists, there is no artefact for the
air-gap and footprint gates to test, and both will correctly report `PENDING` (§5.2).

### 2.6 Finding 5 — a stated import boundary with nothing enforcing it

`app/llm/client.py` opens with: *"The single Anthropic client wrapper. It owns the model id, API
key, retries, and token logging. **Nothing else imports the anthropic SDK.**"*

That is exactly the kind of invariant that holds until the afternoon somebody needs a type and
imports it from the nearest module. It is also trivially checkable. G-2 checks it.

There is precedent in the repo for this class of bug being real. `pyproject.toml` carries this
comment on the `httpx` dependency: *"Imported at module scope by `app/llm/backup.py`, which is on
the import chain from `app.main`. It was dev-only, so the image (`uv sync --no-dev`) booted only
because `anthropic` happens to pull `httpx` in transitively."* An undeclared dependency that
worked by accident, in production, until someone noticed. That is the exact failure G-1 exists
to catch, and it has already happened once here.

### 2.7 Finding 6 — the footprint claim is under more pressure than the docs admit

`ARCHITECTURE.md` §6 already flags the stack as threatening the laptop claim, and `README.md`
states a ~8 GB RAM requirement. The service list is now: interface, spine, WrenAI, Ollama,
OpsMind backend, **and Postgres 17** — because OpsMind uses Postgres while the estate the product
queries is SQL Server. That is two database engines in one deployment.

This may be correct (OpsMind's own state has no business living in the customer's estate), but it
is a service nobody has counted, and G-9 will be the thing that says so with a number.

---

## 3. Scope

### In

- A gate harness: twelve declared invariants, each runnable locally and in CI by the same code
- Repository-root CI that actually fires (§2.4), preserving everything `opsmind/ci.yml` does
- A production deployment artefact distinct from the development compose file (§2.5)
- Hermetic, byte-reproducible builds of every published artefact
- Automation of `SPEC.md` AC-1 … AC-8 as blocking gates
- Air-gapped first-boot verification on a provably cold host
- Peak-RSS budgeting with regression as a build failure
- SBOM, provenance attestation and signature for every published artefact
- A signed release report binding gates to commit to artefact digests

### Out (v1)

Multi-architecture builds (`linux/amd64` only) · staged rollouts — Assayer is customer-installed,
there is nothing to roll out to · performance benchmarking beyond memory · end-to-end UI tests ·
a hosted download service · automated dependency upgrades · any path by which an air-gapped site
reports telemetry to us.

`publish an artefact that no gate ran against` is out permanently, not just for v1.

### Build targets

| Artefact | Ships to customer | Licence | State today |
|---|---|---|---|
| `assayer-verify` | yes, and to anyone | Apache-2.0 | not written |
| `spine` | yes | commercial | not written |
| `interface` | yes | commercial | not written |
| `opsmind-backend` | yes | commercial | **exists** — FastAPI, Postgres, 17 test files |
| `domain-packs` | yes | commercial | not written |

Four of five are `PENDING` subjects. §5.2 exists so that fact stays visible instead of scoring as
twelve passes against an almost-empty repository.

---

## 4. Decisions

### D-1 — Gate logic lives in scripts, never in CI configuration

*Decision.* Every gate is an executable in `ci/gates/`. Workflow files may set up a runner and
call a script. They may not contain a check, a threshold, or a comparison over results.

*Why.* **A gate that only runs in CI is a gate you cannot debug** — push, wait, read a truncated
log, guess. **A gate a buyer cannot run is not evidence**; the whole pitch is "verify without
trusting the operator", and a security team that must take our workflow YAML on faith has
verified nothing. And CI vendors change; logic in YAML is logic you rewrite when you move.

*Trade-off.* You lose matrix expansion, native caching keyed on step inputs, and per-step
annotations. Re-implementing slices of those in shell is real work and the shell version is
worse.

*Mitigation.* Accept the worse version — the orchestration surface is small enough that losing
matrix syntax costs a loop. PC-2 makes the property testable: every gate must produce the same
verdict locally as in CI.

*Applies immediately.* The existing `ci.yml` already keeps its logic in tool config
(`pyproject.toml` holds the ruff, black, mypy and pytest settings) rather than in YAML flags.
Keep that; it is the same principle.

### D-2 — Workflows move to the repository root; OpsMind's content is preserved verbatim

*Decision.* `.github/workflows/` moves to the repository root. The Postgres service, the
`uv sync --locked` step, the migrations-from-scratch step, and the ruff/black/mypy/pytest chain
are carried over unchanged, with `working-directory: opsmind/backend`.

*Why.* Finding 2.4 — nested workflow directories are inert. This is not a redesign; it is making
the existing CI run again.

*Trade-off.* One workflow file now spans multiple components, and per-component path filters
become necessary to avoid running the Python suite on a docs-only change.

*Also fix in the same commit.* `live-conduct.yml` references a script that does not exist
(§2.4). Either restore `scripts/live_conversation.py` or delete the workflow. A dispatch-only
workflow that fails on invocation is worse than no workflow, because it looks like a capability.

### D-3 — Two runner classes, and the split is a real weakening

*Decision.* Ten gates run on hosted runners and may see untrusted input. G-8 (air-gap) and G-9
(footprint) run on a self-hosted runner reset from a snapshot per job, and never execute code
from a fork pull request.

*Why.* G-8 must prove the host is cold — no image cache, no model directory, no package cache —
then observe packets at the physical interface. G-9 needs cgroup v2 `memory.peak` across the
whole stack. Neither works on a shared hosted runner, and a version of G-8 that runs on a warm
host passes vacuously, which is worse than not running it.

*Trade-offs.* A self-hosted runner is a standing supply-chain liability if it persists state
between jobs. And structurally: **the two gates protecting the most distinctive claims cannot
block a merge.** Someone can merge a change that breaks the air-gap and nothing objects until
that night.

*Mitigations.* Ephemeral by construction — reset from snapshot per job, never reused, which is
also what makes G-8's coldness assertion honest, so the security requirement and the correctness
requirement have the same implementation. Fork PRs never reach it. And G-3, the static half of
the air-gap property, is PR-blocking and catches the common case in thirty seconds.

*Kill criterion.* If snapshot reset cannot be made to produce a provably cold host, **delete
G-8** rather than ship it. A gate that reports `PASS` without checking is the exact failure the
product exists to name.

### D-4 — Structural enforcement over assertive checking

*Decision.* Where a property can be enforced by *building from a filtered tree* rather than by
*inspecting the result*, do that. G-6 builds the Apache-2.0 artefact from Apache-2.0 paths only.
G-4 builds the air-gapped image from a tree with the cloud SDK removed.

*Why.* An inspection can be weakened — an allowlist entry, a skipped case, a regex that stopped
matching after a bundler upgrade. A filtered build cannot: a cross-boundary dependency stops
being a check that fails and becomes a build that does not import. The failure is unambiguous
and nobody can quiet it without deleting the dependency.

*Trade-off.* Filtered builds are slower, and they force the source layout to match the boundary.

### D-5 — Three key classes, and the deployment key never exists in CI

| Key | Generated | Signs | In CI? |
|---|---|---|---|
| Release signing | CI, keyless via workload identity where available | published artefacts | yes |
| Deployment ledger (Ed25519) | on the customer host, at install | epoch roots | **never** |
| Witness | by the witness operator | receipts | no |

*Why.* A deployment ledger key baked into an image compromises every installation at once, and
in an air-gapped estate there is no revocation path — you would be telephoning customers. G-11
scans every artefact and image layer for private key material with no allowlist.

*Trade-off.* Keyless release signing normally requires the verifier to query a public
transparency log. Our customers cannot: they are air-gapped, which is the point.

*Mitigation.* The release archive carries the detached signature, certificate chain and
inclusion proof **inside it**. Offline verification then needs the archive and the log's public
key, both carryable. Same shape as the product's own argument — publish the proof, don't require
a call to the prover.

### D-6 — Pinned digests and lockfiles, not Nix

*Decision.* Hermeticity comes from base images pinned by **digest** (not `postgres:17-alpine`,
§2.5), a digest-pinned toolchain image, `uv.lock`, and `SOURCE_DATE_EPOCH`.

*Why.* Nix would make G-7 nearly free and imposes a second build system, a second packaging model
for every dependency, and a body of knowledge a solo maintainer either learns properly or
half-learns. The half-learned version is worse than pinned Docker because it looks rigorous.

*Trade-off.* Weaker hermeticity. Reproducibility failures will arrive from locale, timezone,
filesystem ordering, a compiler that stamps a build path.

*Mitigation.* `diffoscope` on every mismatch, attached to the failure, plus a documented triage
order (archive metadata → embedded paths → timestamps → build IDs). And G-7's degraded pass, so a
stubborn non-determinism does not block a release *and* does not get quietly forgotten.

### D-7 — Gates are data; the release report is generated from the manifest

*Decision.* `ci/gates.toml` declares every gate: id, level, budget, and which requirement IDs it
proves. The report is generated from the manifest joined to the verdict stream. A gate absent
from the manifest contributes nothing even if its script ran.

*Why.* It closes the loop the product's own thesis demands: the release does not assert "we
tested it", it enumerates twelve invariants, names the requirement each discharges, and shows the
verdict. It also makes two kinds of rot impossible — a silently retired gate (PC-4) and a
`proves` reference to a requirement that no longer exists upstream (PC-5).

### D-8 — Mutation testing on two directories, and nowhere else

*Decision.* 100 % mutant kill required on `verify/` and `ledger/` once they exist. No mutation
threshold anywhere else, and no coverage threshold anywhere at all.

*Why.* G-5's tamper cases prove the fixtures are broken in the ways we expected. They do not
prove the verifier would notice if *it* broke. On the one path where the product's only
defensible claim lives, a surviving mutant is a code path no test constrains — which means a
future refactor can silently disable tamper detection while CI stays green.

The 100 % threshold is only affordable because the scope is two directories. Repo-wide it would
be a tax paid forever, and the usual outcome is a threshold lowered once and then ignored.

*Trade-off.* Equivalent mutants force a suppression file, and suppression files rot.

*Mitigation.* `ci/policy/mutation-suppressions.toml`, each entry with a written justification,
reviewed at every release. Past roughly a dozen entries the verifier is too clever and should be
made duller. Dull code on this path is a feature.

### D-9 — The model is a build input, and OpsMind's LLM tier is a release-time decision

*Decision.* The shipped artefact resolves its inference endpoint to a local, in-deployment model.
Cloud model weights, cloud API keys and cloud SDKs are not present in the air-gapped build —
enforced structurally by G-4, not by an empty environment variable.

*Why.* Three reasons, and the second is the one that gets missed.

Egress: §2.3 — survey content going to `api.anthropic.com` contradicts the README's strongest
sentence and fails AC-7.

Erasure: `SPEC.md` §7 resolves immutability-versus-GDPR by keeping plaintext in an erasable side
store. Content that has left the building is outside that store's reach, and an erasure request
cannot recall it. The prepared answer to the question every compliance buyer asks quietly stops
being true — long after the architecture review that would have caught it.

Provenance: `SPEC.md` B-2 requires a board to declare a changed metric definition. A model that
changes underneath a saved answer is the same failure one layer down, with no mechanism to
declare it.

*Trade-off.* Local CPU inference is slower and less capable. `.env.example` already anticipates
this with its 120-second backup timeout and its note about cold loads on CPU.

*Mitigation.* Reframe rather than fix, because the constraint is correct. Assayer does not learn;
it is taught, and every teaching act writes a ledger entry naming the approver (`SPEC.md` C-1).
For a compliance buyer that is the stronger claim.

*Open, and load-bearing.* Whether WrenAI's semantic indexing embeds schema metadata only or
customer content is unverified, and it decides whether "no data leaves the building" is sayable
without qualification. `buildplan.md` Phase 0 stands the stack up in week one — answer it there.

---

## 5. The gate model

### 5.1 Anatomy

A gate is an executable at `ci/gates/<id>.sh` taking no arguments and reading configuration only
from `ci/policy/`. It emits a JSON verdict to stdout and human-readable diagnostics to stderr.

```json
{
  "gate": "G-2",
  "state": "FAIL",
  "subject": { "commit": "a3f9...", "component": "opsmind-backend" },
  "duration_ms": 1840,
  "detail": "app/conduct/engine.py imports `anthropic` (only app/llm/client.py may)",
  "proves": ["AD-4"]
}
```

### 5.2 States and exit codes

| State | Exit | Meaning |
|---|---|---|
| `PASS` | 0 | The invariant held, **and the gate had a subject to check** |
| `FAIL` | 1 | The invariant was violated. `detail` names the violation specifically |
| `PENDING` | 2 | **No subject existed to check** |
| `ERROR` | 3 | No verdict reachable — missing tool, timeout, infrastructure fault |

**`PENDING` must never be reported as `PASS`.** Four of five build targets do not exist yet
(§3). A harness that scores an almost-empty repository as twelve passes is worse than no harness,
because it manufactures exactly the false assurance the product exists to eliminate. `PENDING`
blocks release; it does not block a pull request.

`ERROR` blocks everything and is never retried automatically. A gate that passes on the third
attempt has told you something, and it is not that the invariant held.

### 5.3 Blocking levels

| Level | Runs on | `FAIL` means |
|---|---|---|
| **PR** | every pull request, including forks | merge blocked |
| **MAIN** | every push to `main`, and nightly | `main` broken; release blocked until green |
| **RELEASE** | release candidates only | publication blocked |

Fork PRs never reach the privileged runner (D-3), so those gates cannot be PR-level. §8 states
that plainly rather than burying it.

### 5.4 Manifest

```toml
[[gate]]
id       = "G-4"
name     = "No cloud inference path in the shipped artefact"
level    = "PR"
budget_s = 120
proves   = ["AC-7", "README:no-cloud-api-keys"]
negative = "ci/negatives/G-4/"
```

Every gate declares what it proves, referencing requirement IDs in `SPEC.md` or
`ARCHITECTURE.md`. A gate proving nothing named is deleted. An ID that no longer exists upstream
fails the harness meta-check (PC-5).

---

## 6. The gate catalogue

Twelve gates. Each states what it does *not* prove, because a gate whose limits are undocumented
gets over-trusted at exactly the moment it matters.

---

### G-1 · Dependency closure

**Proves** that what ships is what the lockfile says, and that no undeclared transitive is
load-bearing.

**Subject** `opsmind/backend/uv.lock`, and the equivalent for every component as it appears.

**Procedure**

1. Resolve the closure from the lockfile only — `uv sync --locked` already enforces this in the
   existing CI; the gate reads the resolved set rather than the declared set
2. Assert every module imported at module scope from the `app.main` import chain resolves to a
   **declared** dependency, not one that arrives transitively
3. Fail closed on native extensions: any package shipping a `.so`, `.pyd` or `.node` not present
   in `ci/policy/native-reviewed.txt` with a recorded `sha256` is a `FAIL`, not a warning

**Why step 2 is the point.** This already happened here. `pyproject.toml` records that
`app/llm/backup.py` imports `httpx` at module scope while `httpx` was dev-only, and the
production image booted only because `anthropic` pulled it in transitively (§2.6). The fix is in
the tree; the gate is what stops the next one.

**Fail diagnostic** The module, the importing file, and the full path from the root manifest.

**Does not prove** That declared dependencies are safe or maintained. It proves the shipped set
is the resolved set.

**Level** PR · **Budget** 30 s

---

### G-2 · Import boundary

**Proves** AD-4, and the invariant `app/llm/client.py` already claims in its own docstring:
*"Nothing else imports the anthropic SDK."*

**Subject** The import graph of `opsmind/backend/app/`, plus every component's graph as it
appears.

**Procedure**

1. `import anthropic` (or `from anthropic import …`) may appear in **exactly one** file, listed
   in `ci/policy/import-boundaries.toml`. Today: `app/llm/client.py`
2. No database driver — `asyncpg`, `psycopg`, `pyodbc`, `pymssql`, `tedious` — reachable from any
   interface-tier module. The spine and OpsMind's repository layer own their connections; the
   presentation tier does not
3. Boundaries are declared as data, one line per rule with a rationale, so the check is
   extensible without editing the script

**Why.** A stated boundary with no enforcement holds until the afternoon someone needs a type and
imports it from the nearest module. This one is trivially checkable and currently unchecked
(§2.6). It is also the precondition for G-4: you cannot strip a cloud SDK cleanly from a tree
that imports it in six places.

**Fail diagnostic** The offending import, its file and line, and the boundary rule it broke.

**Does not prove** That the permitted file uses the SDK correctly, or that no HTTP call reaches
the same endpoint by hand. G-8 catches the second; this one closes the accidental path.

**Level** PR · **Budget** 20 s

---

### G-3 · Configuration cannot silently enable egress

**Proves** that the air-gapped deployment's inference path is local **by configuration**, and
that a stray environment variable cannot change that.

**Subject** The production compose file (§2.5) and `app/config.py`'s resolution order.

**Procedure**

1. In the production compose file, `ANTHROPIC_API_KEY` is **not passed through at all** — not
   defaulted empty, not present. `${ANTHROPIC_API_KEY:-}` is a hole; an operator who sets the
   variable on the host changes the model's precedence without editing a file
2. Exactly one LLM tier is enabled, and its `base_url` resolves to a host inside the deployment
   network (`ci/policy/permitted-inference-hosts.txt`)
3. A boot-time assertion in the application fails closed if the resolved chain contains any
   client whose endpoint is not on that list — and the gate asserts the assertion exists
4. Replay `factory.get_llm()` against the production environment and assert the resolved chain
   is exactly one `OpenAICompatibleLLMClient` pointing at the local endpoint

**Why step 4 rather than a config grep.** `factory.py` appends the Anthropic client first
whenever the key is non-empty (§2.3). The property is a function of the resolved chain, not of
any single variable, so the gate resolves the chain.

**Does not prove** anything about the development compose file, which may keep the cloud path.
The two configurations are separate artefacts and only one ships.

**Level** PR · **Budget** 30 s

---

### G-4 · No cloud inference path in the shipped artefact

**Proves** AC-7 and the README's *"No cloud API keys"* — structurally rather than by
configuration.

**Why both this and G-3.** G-3 proves the deployment is *configured* not to call out. G-4 proves
it *cannot*. Configuration is a runtime property an operator can change on a host you will never
see again; absence from the image is not.

**Procedure**

1. Build `opsmind-backend`'s air-gapped variant from a filtered dependency set with the cloud SDK
   excluded — **filtering, not asserting** (D-4). A remaining import becomes a build that does
   not start, which is unambiguous and cannot be quieted
2. Assert `anthropic` is absent from the built image's site-packages
3. Assert no `api.anthropic.com` or other cloud-inference hostname appears as a literal in any
   shipped layer (`ci/policy/cloud-endpoints.txt`)
4. Boot the image and assert the health endpoint comes up with the local tier only

**Prerequisite.** G-2 must be green. A tree that imports the SDK from six modules cannot be
filtered cleanly, so the boundary gate is what makes this one cheap.

**Does not prove** that no request is constructed dynamically at runtime. G-8 catches that;
this runs in two minutes and gives a legible error instead of a pcap.

**Level** PR · **Budget** 120 s

---

### G-5 · Tamper and omission detection

**Proves** AC-3, AC-4, AC-5, W-3 — the moat.

**Subject** `verify/` and `ledger/`. Currently `PENDING`; this is the gate to build first once
they exist.

**Procedure** Property tests over generated ledgers, not example tests. Each fixture is ≥ 500
entries spanning ≥ 3 epoch boundaries, generated deterministically from a seed recorded in the
verdict.

| Case | Assertion |
|---|---|
| **T-1** bit flip | For random entry *i* and byte offset, `verify` exits non-zero **and names entry *i*** |
| **T-2** interior deletion | Removing entry *i* fails with a **sequence-gap** diagnostic, distinct from a chain break |
| **T-3** tail truncation | Removing the last *k* entries fails. Its own case because **a hash chain alone cannot detect a cleanly removed tail** — this is AC-4, the criterion that distinguishes Assayer, and it must not ride on T-2's coat-tails |
| **T-4** epoch erasure | Deleting a whole epoch *and its local receipt* still fails against the witness's copy (AC-5) |
| **T-5** witness divergence | Two witnesses returning different roots for one epoch is a `FAIL`, not a warning (W-3) |
| **T-6** reordering | Two entries transposed with `seq` swapped to match fails on `prev_hash` |

**Mutation testing** over `verify/` and `ledger/` only, 100 % kill rate, every survivor listed by
location and mutation (D-8).

**Does not prove** that the cryptography is sound — only that the implementation detects the
attacks we thought of. Soundness comes from AD-6's open-sourcing and G-12's vectors.

**Level** PR · **Budget** 8 min

---

### G-6 · Licence split

**Proves** AD-6 — the Apache-2.0 artefact contains no commercially-licensed code.

**Why it matters.** If open-published code imports from a commercial tree, that commercial code
has been distributed under Apache-2.0. That is not recoverable by a later correction.

**Procedure**

1. Build the open artefact from a **filtered** tree containing only Apache-2.0 paths, so the
   property is structural rather than asserted (D-4)
2. A build failure means a real cross-boundary dependency exists
3. Analyse the open artefact's import graph; any symbol resolving into a commercial path fails
4. Confirm every file in the filtered tree carries the expected SPDX identifier

**Direction matters.** `opsmind-backend` may import `assayer-verify` — Apache-2.0 flowing into a
commercial product is the intended arrangement. The gate is one-directional and must not be
written as a symmetric check.

**Does not prove** that the commercial tree's own use of open dependencies is compliant. That is
a human review item on the release checklist.

**Level** PR · **Budget** 30 s

---

### G-7 · Reproducible build

**Proves** that the artefact corresponds to the source.

**Why this is not optional here.** The product's claim is "verify without trusting the operator."
If we cannot rebuild our own verifier to the same bytes, the customer verifies the ledger using a
binary they cannot check against its source — and the trust story has a hole at its root. Every
other project can treat reproducibility as hygiene; this one cannot.

**Procedure**

1. Build twice, varying what a bad build absorbs: different runner, working directory, wall-clock
   date, hostname
2. Compare digests of every published artefact
3. On mismatch, run `diffoscope` and attach its output to the failure

**Requires** base images pinned by **digest** — `postgres:17-alpine` is a mutable tag and must
become `postgres@sha256:…` (§2.5); toolchain image pinned by digest; `SOURCE_DATE_EPOCH` from the
commit timestamp; deterministic ordering and zeroed mtimes in archives; build-ID and path
stamping stripped.

**Degraded pass.** If byte-identity proves unreachable, an explicitly enumerated normalisation
list may be applied — and the release report states, on its face, that the build is reproducible
*after normalisation* and lists what was normalised. An honest weaker claim beats a strong false
one. The tempting move under deadline is to normalise quietly and keep the strong claim; that is
precisely the behaviour the product accuses competitors of.

**Does not prove** that the source is trustworthy, or that the build environment was not
compromised — a compromised toolchain reproduces perfectly. §8 addresses this.

**Level** RELEASE · **Budget** 25 min

---

### G-8 · Air-gapped first boot

**Proves** AC-7 — a cold start on a disconnected host with no outbound network attempt. The gate
competitors fail, so the one most worth building carefully and the easiest to build vacuously.

**A definition the product docs owe the reader.** "Air-gapped" here means *no traffic leaves the
customer's network*, not *no traffic at all*. The witness must be reachable (W-1), and in an
air-gapped deployment it lives on the customer's own network. **A configuration pointing
`ASSAYER_WITNESS_URL` at a public Rekor instance is not air-gap-compatible**, and this gate is
the reason we know that. The permitted-peer set is explicit and the witness is its only member.

**Procedure**

1. **Prove the host is cold.** Runner reset from snapshot. Assert and record: `docker images -q`
   empty, no Ollama model directory, no uv/npm cache. A warm cache turns every later step into
   theatre
2. Load images from the release tarball; assert every image referenced by the **production**
   compose file is present and its digest matches the release manifest
3. Bring the stack up on a Docker network declared `internal: true` — no gateway on the bridge,
   so containers reach each other and nothing else. The witness stub joins as the single
   permitted peer
4. Assert no resolver: `getent hosts example.com` fails inside every service container
5. Host-side belt and braces: `nft` rules logging and dropping any packet from the bridge subnet
   to a destination outside it, plus `tcpdump` on the physical interface
6. Run the fixed workload at `ci/workloads/airgap.yaml`: seed, ask one answerable question, ask
   one touching a Blocked column, **conduct one OpsMind survey run end to end** (this is the step
   that would have called `api.anthropic.com` before G-4), promote one metric (which must write a
   ledger entry naming the approver — C-1), force an epoch close, publish to the witness stub
7. Run `assayer verify` against the resulting ledger

**Pass** Zero logged drops, zero packets to any destination outside the bridge other than the
witness, workload completed, `verify` exits 0.

**Fail diagnostic** The destination address and port, the container that emitted it, and the path
to the retained pcap. "Something phoned home" is not a diagnostic.

**Does not prove** that no code *would* reach the network given the opportunity — only that this
workload on this build did not. Coverage is bounded by step 6's script, which is versioned and
reviewed whenever a surface is added.

**Level** RELEASE, plus nightly on `main` · **Budget** 12 min

---

### G-9 · Footprint

**Proves** AC-8, and the strongest sentence in the pitch.

**Subject** Peak RSS of the running stack under `ci/workloads/footprint.yaml`. **Six services**,
not four: interface, spine, WrenAI, Ollama, `opsmind-backend`, and Postgres (§2.7).

**Procedure** Measure per service from cgroup v2 `memory.peak`, read after the workload completes
and before teardown. Compare against `ci/policy/budgets.toml`.

| Threshold | Value |
|---|---|
| Total stack ceiling | 8 GiB peak RSS — the README's stated requirement, so it is a contract |
| Per-service regression | > 5 % over recorded baseline |
| Total regression | > 3 % over recorded baseline |
| Total image size | > 10 % regression fails |

**Expect this to fail on first run.** `ARCHITECTURE.md` §6 already calls the ceiling threatened,
and the service count has since grown by two — OpsMind's backend and its Postgres. When it fails,
the response is to cut scope, not to raise the ceiling: the ceiling is published in the README and
OpsMind is the newest thing in the room. Whether OpsMind's Postgres can be collapsed into an
existing engine is the first question to ask.

**Does not prove** behaviour under real customer data volumes. The workload is fixed so the
number tracks the code rather than the test; the cost of that choice is that it says nothing
about scale.

**Level** MAIN · **Budget** 10 min

---

### G-10 · Upstream licence drift

**Proves** the AD-1 mitigation — WrenAI's multi-licence tree does not quietly relicense
underneath us.

**Procedure**

1. Assert the dependency is pinned to a **commit SHA**, not a tag. A tag is mutable
2. Recompute the `sha256` of every `LICENSE`/`COPYING`/`NOTICE` in the pinned tree; compare
   against `ci/policy/upstream-licences.lock`
3. Build the import graph of what we actually load from upstream and check every imported path
   against the multi-licence path map. **An AGPL-3.0 path entering our import graph is a hard
   `FAIL`**, independent of whether licence text changed

Step 3 is the gate. Steps 1–2 are the tripwire that tells you to go and look.

**Fail** Prints a unified diff of the changed licence text, or the offending import path with its
consumer. Clearing it requires a human to update the lock in the same commit that moves the pin —
the acknowledgement is the point, so it is deliberately not automatable.

**Does not prove** that our use of the Apache-2.0 portion is compliant, or that the trademark
position is respected. Both are human judgements on the release checklist.

**Level** MAIN · **Budget** 15 s

---

### G-11 · Release integrity

**Proves** that a downloaded artefact is ours, and that no private key material shipped.

**Procedure**

1. Generate a CycloneDX SBOM per artefact over the **full closure**, not the top level
2. Generate a build provenance attestation naming source commit, builder identity and materials
3. Sign artefact, SBOM and attestation
4. **Scan every artefact and image layer for private key material** — PEM private-key headers,
   OpenSSH private keys, PKCS#8 blocks, and any file matching the deployment key's on-disk name.
   A deployment key baked into an image would compromise every installation simultaneously, so
   this is a `FAIL` on a single hit with no allowlist
5. Bundle the detached signature, certificate chain and transparency-log inclusion proof **inside
   the release archive**, so an air-gapped customer can verify offline (D-5)

**Also scan for API keys.** `.env.example` documents four separate key slots. A populated `.env`
committed or baked into a layer is the same class of failure as a private key and gets the same
no-allowlist treatment.

**Does not prove** that the signing identity was not compromised. Transparency-log inclusion gives
that its own detection path — the same argument the product makes about witnesses.

**Level** RELEASE · **Budget** 5 min

---

### G-12 · Spec conformance vectors

**Proves** `SPEC.md` §4.3 — *"re-implementable from this spec in an afternoon."* Currently an
aspiration; this makes it a test.

**Procedure** A frozen corpus at `vectors/` in the open repository: each vector a directory
containing a ledger, receipts, and an expected result — pass, or fail with a named cause. Our
verifier runs the corpus and must match every expectation exactly, **including the failure
cause**. The corpus is append-only; removing a vector requires the same justification as removing
a requirement.

**Does not prove** that an independent implementation exists. It provides the instrument such an
implementation would be measured with — which converts the spec's boast into something a sceptic
can act on.

**Level** PR · **Budget** 60 s

---

### Summary

| Gate | Level | Budget | Subject exists today? |
|---|---|---|---|
| G-1 dependency closure | PR | 30 s | **yes** — `uv.lock` |
| G-2 import boundary | PR | 20 s | **yes** — `app/` |
| G-3 config cannot enable egress | PR | 30 s | partly — needs prod compose |
| G-4 no cloud path in artefact | PR | 120 s | **yes** — `backend/Dockerfile` |
| G-5 tamper and omission | PR | 8 min | no — `PENDING` |
| G-6 licence split | PR | 30 s | no — `PENDING` |
| G-7 reproducible build | RELEASE | 25 min | partly |
| G-8 air-gapped first boot | RELEASE + nightly | 12 min | no — needs prod compose |
| G-9 footprint | MAIN | 10 min | no — needs prod compose |
| G-10 upstream licence drift | MAIN | 15 s | no — WrenAI not pinned yet |
| G-11 release integrity | RELEASE | 5 min | partly |
| G-12 spec vectors | PR | 60 s | no — `PENDING` |

**PR-blocking total: ≈ 11 min 40 s**, against PC-7's 10-minute cap — over budget before most
subjects exist. G-5 is the cost. Options, in preference order: run G-5's mutation pass on MAIN and
keep only T-1…T-6 on PR; or raise PC-7 to 15 minutes and say so. Decide in Phase 2 rather than
letting it drift — the failure mode is a slow PR suite people learn to bypass.

---

## 7. The release report and the pipeline's own acceptance criteria

### The report

Generated from `ci/gates.toml` and the verdict stream, signed by the release key, published
alongside the release. Contents: source commit, build timestamp, builder identity; every gate
with state, duration and what it proves — **including `PENDING` gates, listed first**; artefact
digests with SBOM, attestation and signature references; the reproducibility result and the
normalisation list if the pass was degraded; the peak-RSS table against budget; the air-gap
capture summary.

**R-1.** A report whose gate set is incomplete says so on its first page. Publishing with a
`PENDING` or `FAIL` gate requires an explicit recorded override naming a person, rendered at the
top rather than in a footnote. This mirrors `SPEC.md` E-1.

**R-2.** The report regenerates byte-identically from the same verdict stream, or it is a
narrative rather than a record. `report.json` is the artefact; any rendered page is a view of it.

### Acceptance criteria

A gate suite is untested code until these hold.

| # | Criterion |
|---|---|
| PC-1 | **Every gate has a negative fixture at `ci/negatives/<id>/` that makes it fail, and CI asserts the failure.** A gate never observed failing is unverified — this is the pipeline's AC-3 |
| PC-2 | Every gate runs locally via `make gate G=<id>` with no CI-specific environment, producing the same verdict as CI |
| PC-3 | Today's repository reports `PENDING` for every gate with no subject, and `PASS` for none |
| PC-4 | Removing a gate from `ci/gates.toml` while its script remains fails the manifest meta-check — gates cannot be silently retired |
| PC-5 | A `proves` reference to a requirement absent from `SPEC.md` or `ARCHITECTURE.md` fails the harness |
| PC-6 | The release report regenerates byte-identically from the same verdict stream |
| PC-7 | Total PR-blocking runtime stays under 10 minutes wall-clock; exceeding it is a build failure, not an annoyance |

PC-1 and PC-3 catch a pipeline lying to you. Build them first.

---

## 8. Known tensions

**Privileged gates cannot block merges.** G-8 and G-9 need a scrubbed, privileged, ephemeral host
(D-3), and fork PRs must never reach it. So the two gates protecting the most distinctive claims
are release-and-nightly gates, and a contributor can merge a change that breaks the air-gap
without CI objecting until that night. Mitigated by G-3 and G-4 being PR-blocking and catching
the common case in under three minutes.

**Nobody witnesses the build.** The product's argument is that a chain the operator computes
proves nothing because the operator can recompute it. The same applies here: we compute our own
gate verdicts and sign our own report. G-7 narrows the gap — an independent party can rebuild
from source and compare digests without our cooperation — and G-12 lets them check the verifier
against vectors rather than against our word. What remains is that our *gate results* are
self-asserted. Publishing the release report to the same class of external witness the product
uses would close it. That is the honest next step and it is not in v1.

State this before a reviewer finds it. Being the party that raises it is worth more than the gate
would be.

**The product's identity is unresolved, and no gate fixes that.** `buildplan.md` describes
OpsMind as an operator surface for fleet health and metric promotion; the code is a survey and
conduct service (§2.2). G-8's workload script has to pick one, because it can only exercise what
exists. Reconcile the plan with the code before Phase 1, or the gates will be measuring a
product the roadmap does not describe.

**Reproducibility versus toolchain currency.** Pinning by digest makes security updates a
deliberate act rather than a background process — patch latency traded for determinism.
Mitigation: a weekly job reporting available base-image updates without applying them, so the
latency is visible rather than merely accepted.

---

## 9. Order of work

Cheapest and most certain first; anything needing special infrastructure last. Static analysis
before correctness, correctness before hermeticity, hermeticity before environment, environment
before release mechanics. Each phase leaves the pipeline usable, not half-migrated.

Roughly two thousand lines of shell and policy across eight weeks. If a phase starts producing a
framework, it has gone wrong — the gates should be boring enough that a buyer's engineer reads
one in two minutes and believes it.

### Phase 0 — Make CI run again, and build the harness (week 1)

Two things, and the first is a bug fix, not a feature.

- **Move `.github/workflows/` to the repository root** (D-2), carrying over the Postgres service,
  `uv sync --locked`, migrations-from-scratch, ruff, black, mypy and pytest unchanged, with
  `working-directory: opsmind/backend`. Add path filters so a docs change doesn't run the suite
- **Resolve `live-conduct.yml`**: restore `scripts/live_conversation.py` or delete the workflow
  (§2.4). A dispatch workflow that fails on invocation looks like a capability and isn't
- `ci/gates.toml` with all twelve declared, every script a stub returning `PENDING`
- `make gate G=<id>` and `make gates` — the same entry point CI uses (D-1)
- Verdict schema; report generator reading manifest + verdict stream
- Meta-checks PC-4 and PC-5; `ci/negatives/` scaffolding and the PC-1 runner

**Deliverable.** CI runs on push again, and `make gates` prints twelve honest states.

**Kill criterion.** If the harness reports a single `PASS` against a gate with no subject, stop
and fix it before writing gate one. That failure mode is the harness lying, and every number
downstream inherits it.

### Phase 1 — The gates the current code can actually take (weeks 2–3)

G-2, G-1, G-3, G-4 — in that order, because each is the precondition for the next.

1. **G-2 import boundary** first. It is twenty lines, it enforces an invariant already written in
   `client.py`'s docstring, and it is what makes G-4 cheap
2. **G-1 dependency closure**, seeded with the `httpx` near-miss (§2.6) as its first negative
   fixture — a real historical failure is a better test than an invented one
3. **G-3 config cannot enable egress**, which requires the production compose file to exist:
   digest-pinned images, no bind mount, no `--reload`, no unconditional seed, no exposed Postgres
   port (§2.5)
4. **G-4 no cloud path in the artefact** — the filtered build

**Deliverable.** A shipped artefact that cannot call `api.anthropic.com`, proven structurally,
with four negative fixtures.

**Kill criterion.** If the cloud SDK cannot be filtered out because a local-only inference path
does not meet the product's quality bar, **stop and escalate to a product decision**. The options
are: accept degraded local inference; drop the README's "no cloud API keys" sentence and rewrite
the pitch; or drop the conversational surface from the air-gapped edition. All three are
survivable. Shipping the claim while the code calls out is not.

### Phase 2 — The moat gate (weeks 4–5)

G-5 and G-12, tracking `buildplan.md` Phase 2. Build order: deterministic seeded fixture
generator → T-1…T-6 as property tests, with **T-3 (tail truncation) given its own case and
fixture** because it is AC-4 and the differentiator must not ride on T-2's coat-tails → witness
stub → T-4 and T-5 → publish `vectors/` → mutation testing at 100 %.

**Kill criterion.** If non-equivalent mutants survive, the verifier is under-specified — stop and
fix `SPEC.md` §4.3 before writing more tests. A surviving mutant here means a future refactor can
disable tamper detection while CI stays green.

**Decide here.** PC-7 is already breached (§6 summary). Move the mutation pass to MAIN, or raise
the cap and record why.

*Publish `vectors/` before the verifier is finished.* Shipping the instrument before the
implementation forecloses on quietly adjusting a vector to match a bug, and it is a strong signal
to exactly the audience that matters.

### Phase 3 — Hermeticity (week 6)

G-7 and G-6. Pin every base image by digest, starting with `postgres:17-alpine`. Set
`SOURCE_DATE_EPOCH`; zero mtimes; strip build-ID and path stamping. Build twice on deliberately
different runners, paths, dates and hostnames. `diffoscope` on mismatch.

**Kill criterion.** None that stops the project, but a hard fork in the road: if byte-identity is
not reached by end of week, take the degraded pass, enumerate the normalisations, and make the
report say *reproducible after normalisation* on its face. Do not spend a second week chasing a
stamped path, and do not normalise quietly.

### Phase 4 — Environment (week 7)

G-8 and G-9 — infrastructure before gate work. Ephemeral snapshot-reset runner; coldness
assertion recorded in the verdict; `internal: true` network with the witness stub as sole peer;
host-side `nft` log-drop and `tcpdump`; the workload script including a full OpsMind survey run.
Then G-9's per-service `memory.peak` across six services.

**Kill criterion.** If the runner cannot be made provably cold, **delete G-8**. Do not ship a
version that runs on a warm host — a gate reporting `PASS` without checking is the exact failure
the product exists to name, and running one inside our own pipeline would be indefensible the
first time anyone looked.

**Expect G-9 to fail on first run** (§2.7). When it does, cut scope. Start by asking whether
OpsMind's Postgres can be collapsed into an engine already in the deployment.

### Phase 5 — Release mechanics (week 8)

G-10, G-11, the signed report. Pin WrenAI by SHA and lock its licence hashes; build the import
graph; hard-fail on an AGPL path. SBOM over the full closure; provenance attestation; signing;
the private-key and API-key scan with no allowlist; the offline verification bundle inside the
archive.

**Kill criterion.** If anything acquires a path to author or override a verdict, stop. A record
the operator can revise is the failure the product is built to name, and reproducing it in our
own tooling would end the sales conversation it was meant to support.

### Week one checklist

- [ ] Move `.github/workflows/` to the repository root; confirm CI actually fires on a test PR
- [ ] Restore or delete `live-conduct.yml`'s missing `scripts/live_conversation.py`
- [ ] `ci/gates.toml` with twelve `PENDING` stubs; `make gate G=<id>` working locally
- [ ] Verdict schema and report generator; PC-3 asserted
- [ ] PC-1 runner, `ci/negatives/` scaffolding, PC-4 and PC-5 meta-checks
- [ ] Write G-2 — it is the cheapest real gate here and unblocks G-4
- [ ] **Decide OpsMind's inference tier for the air-gapped edition** (D-9). This is a product
      decision with a pipeline deadline: G-4 cannot be written until it is made
- [ ] **Answer the WrenAI indexing question** — schema metadata only, or customer content? Decides
      whether "no data leaves the building" is sayable without qualification
- [ ] Record where the privileged runner will live and whether snapshot reset is achievable —
      D-3's kill criterion, and finding out in week seven is too late

---

## 10. Open questions

1. **Does the release report get witnessed?** It closes §8's second tension — our gate results are
   currently self-asserted, which is the criticism the product levels at everyone else. Roughly a
   fortnight, not in v1, and the most defensible thing this project could do.
2. **What is OpsMind?** The build plan and the code disagree (§2.2). Every gate below G-4 is
   written against the code; the plan should be reconciled or the code repositioned.
3. **Can the air-gapped edition keep the conversational surface?** D-9's kill criterion. If local
   inference cannot carry survey conduct at acceptable quality, something in the pitch changes —
   better to know in week two than in a customer demo.
4. **Two database engines, or one?** OpsMind uses Postgres; the estate is SQL Server. Defensible,
   and it costs a service against an 8 GiB ceiling that `ARCHITECTURE.md` already calls
   threatened.
5. **Is `PC-7` 10 minutes or 15?** Already breached before most subjects exist. Decide in Phase 2.
6. **Who is the second pair of eyes on the cryptographic path?** Mutation testing at 100 % is a
   floor, not a substitute, and `ARCHITECTURE.md` AD-3 already names solo-authored crypto as a
   liability.
