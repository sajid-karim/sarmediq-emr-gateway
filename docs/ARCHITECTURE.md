# EMR Integration Gateway: Architecture & System Design

> A study guide as well as a design doc. Each section has:
> **Diagram** → **Walkthrough** (what each part means) → **Why** (the decision and what was rejected) → **Interview drill** (questions you may be asked, with short answers).
>
> §14 covers how it's tested. §15 says what gets built in the 4–6 hours and what is only documented.

---

## 0. The one-sentence idea

> **Never let something we don't understand reach the clinical record.**

Third-party EMR APIs change without warning. The dangerous failure isn't a crash (500 errors are loud and get noticed). The dangerous failure is a quiet one: the vendor renames `dob` to `birth_date`, the code reads `dob`, gets `None`, and stores a patient with no date of birth. Or a new appointment status shows up, and the code maps it to "the closest one" and marks a patient as seen when they weren't.

So the whole system is built to **fail safe**:
- **Fail open:** "best effort, keep going, guess if needed." Fine for analytics, dangerous for clinical data.
- **Fail safe:** "if in doubt, stop that record, keep the evidence, and tell a human." That's this system.

But it can't be paranoid either. If *every* small change stopped ingestion, the integration would be down all the time. The design has to tell **harmless changes** (accept them) apart from **dangerous changes** (quarantine them). That distinction is the core of the assignment (30% of the score).

### Glossary (terms used throughout)

| Term | Meaning |
|---|---|
| **Vendor** | The external EMR company (Epic, Athena, eCW…). Here it's our mock "Acme EMR". |
| **Tenant** | One connection to one vendor instance: a practice (Athena, eCW), a health system (Epic, Cerner) or an enterprise/practice (NextGen). Vendor resource IDs are only unique within a tenant (learned from Athena, see §6). `tenant_id` is *our* id for it; the vendor's own identifier is stored as the tenant's `locator`. |
| **Resource** | A type of data: `patient`, `appointment`, `clinical_report`. |
| **Operation** | What we did to get it: `list` (paginated fetch) or `get` (single fetch). |
| **Contract** | What we *expect* the vendor to send: fields, types, allowed values, response shape. |
| **Canonical model** | *Our* internal, vendor-independent representation of a patient/appointment/report. |
| **Adapter** | The vendor-specific code that turns the vendor's JSON into the canonical model. |
| **Drift** | Any difference between what the vendor sends and the contract. |
| **Quarantine / DLQ** | A holding area for records we refused to write. DLQ = Dead Letter Queue. |
| **Replay** | Re-processing quarantined records after we fix the code. |
| **Idempotent** | Doing the same operation twice gives the same result as doing it once. |
| **Circuit breaker** | A switch that stops calling a failing system so we don't pile on damage. |
| **Checkpoint / cursor** | A bookmark of how far we've synced, so we can resume after a crash. |

---

## 1. System context

### Diagram

```
 ┌─────────────────────────────┐          ┌──────────────────────────────────────────────────────┐
 │      MOCK EMR VENDOR        │          │              EMR INTEGRATION GATEWAY                 │
 │      (FastAPI :9000)        │          │                 (FastAPI :8000)                      │
 │                             │  HTTP    │                                                      │
 │  GET /{pid}/patients        │◄─────────┤  Sync jobs (pull)        Ops API (admin)             │
 │  GET /{pid}/appointments    │  JSON    │  ─────────────────       ───────────────────         │
 │  GET /{pid}/reports         ├─────────►│  POST /sync/{resource}   GET  /health                │
 │                             │          │                          GET  /incidents             │
 │  POST /_admin/scenario      │          │                          GET  /drift                 │
 │   ├─ v1          (contract) │          │                          GET  /quarantine            │
 │   ├─ additive    (compat)   │          │                          POST /replay                │
 │   ├─ optional_missing       │          │  Read API                POST /drift/{id}/promote    │
 │   ├─ breaking    (schema)   │          │  GET /patients[/{id}]    POST /breaker/reset         │
 │   ├─ envelope    (struct)   │          │  GET /appointments[/{id}]                            │
 │   ├─ report_change (report) │          │  GET /reports[/{id}]                                 │
 │   ├─ new_status  (behavior) │          └──────────────┬─────────────────────┬─────────────────┘
 │   ├─ rate_limit  (behavior) │                         │                     │
 │   ├─ pagination  (behavior) │                         ▼                     ▼
 │   ├─ malformed   (bad body) │                ┌─────────────────┐   ┌─────────────────┐
 │   └─ flaky       (5xx/slow) │                │  SQLite / PG    │   │ Structured logs │
 └─────────────────────────────┘                │  (state + data) │   │ (JSON → stdout) │
        ▲                                       └─────────────────┘   └─────────────────┘
        │ pytest drives scenarios
 ┌──────┴──────────────────────┐
 │  TEST HARNESS (pytest)      │
 │  scenario → sync → assert   │
 └─────────────────────────────┘
```

### Walkthrough

- **Mock EMR Vendor (port 9000).** A small, separate FastAPI app that *pretends* to be the vendor. It serves patients, appointments and reports.
  - `POST /_admin/scenario` is a **switch** that changes how the mock behaves. Setting `breaking` makes it start returning `birth_date` instead of `dob`. This is how we simulate "the vendor changed the API without telling us". The documented v1 contract the mock starts from is in §9.
  - It's a separate process (not just fixtures) so the gateway talks to it over **real HTTP**, which means timeouts, 429s and 503s are real network behavior, not mocks.
- **Gateway (port 8000).** The system we're actually building. It has two kinds of endpoints:
  - **Sync jobs:** `POST /sync/patient` (or `appointment`, `clinical_report`) starts a pull from the vendor. In production a scheduler would trigger this; for the assignment we trigger it manually or from tests.
  - **Ops API:** what an on-call engineer uses: health, open incidents, detected drift (with a button to promote a new response shape to baseline), quarantined records, a replay button, and a manual breaker reset.
  - **Read API:** `GET /patients`, `/appointments`, `/reports` (list, or one by `/{id}`) return canonical data, proving that what we stored is correct.
- **Database.** Stores everything: clean data, raw payloads, quarantine, incidents, checkpoints. SQLite by default (zero setup), Postgres optional.
- **Structured logs.** Every important event is one JSON line (`{"event": "integration.incompatible", "vendor": "acme", ...}`). Machines can search JSON; they can't reliably search free-text logs.
- **Test harness.** pytest flips scenarios on the mock, runs a sync, and asserts the outcome.

### Why

| Decision | Rejected alternative | Reason |
|---|---|---|
| Mock as a real HTTP service | Static JSON fixtures only | Retries, timeouts, 429s and pagination are *behaviors* over HTTP. Fixtures can't demonstrate them honestly. (We still use fixtures inside the mock for data.) |
| Scenario switch endpoint | Separate mock apps per version | One mock, many behaviors. Tests stay short: `set_scenario("breaking")`. |
| Pull model (we poll the vendor) | Push (webhooks) | Most EMR APIs are pull-based REST. Pull also gives us control over rate and resume. Push can feed the same pipeline later (see §10). |
| Separate Ops API | Only logs | The assignment asks for "structured logs **or** a simple monitoring endpoint". Doing both shows operational thinking. |

### Interview drill

- **Q: Why not just use fixtures?** A: Fixtures test the *mapping*. They can't test *transport* behavior like retries, `Retry-After`, timeouts or pagination loops. We use both: fixtures for data, a live mock for behavior.
- **Q: Who calls `/sync` in production?** A: A scheduler (cron, Celery beat, Temporal) per tenant × vendor × resource. See §10.

---

## 2. Layers inside the gateway

### Diagram

```
╔══════════════════════════════════════════════════════════════════════════════════════════════╗
║  API LAYER  (app/api)                                                                        ║
║  sync_router · ops_router · read_router        ← thin; no business logic                     ║
╠══════════════════════════════════════════════════════════════════════════════════════════════╣
║  ORCHESTRATION  (app/sync/orchestrator.py)                                                   ║
║  for each page: fetch → persist raw → adapt → classify → write/quarantine → checkpoint       ║
║  owns: circuit breaker per (vendor, tenant, resource), batch failure threshold, cursors      ║
╠═══════════════════════════════════════════╦══════════════════════════════════════════════════╣
║  VENDOR-SPECIFIC (app/vendors/acme/)      ║  VENDOR-AGNOSTIC CORE (app/core/)                 ║
║  ─────────────────────────────────────    ║  ──────────────────────────────────────────────── ║
║  client.py                                ║  canonical.py   Patient / Appointment / Report   ║
║   · tenant locator → URL/auth, timeouts   ║                 (Pydantic, FHIR-shaped, strict)  ║
║   · pagination strategy                   ║  classifier.py  COMPATIBLE | DEGRADED |          ║
║  contract.py                              ║                 INCOMPATIBLE + reason codes      ║
║   · expected fields + criticality         ║  drift.py       schema fingerprint + diff        ║
║   · known enums per field                 ║  idempotency.py natural key + content hash       ║
║   · envelope shape                        ║  quarantine.py  DLQ write / replay               ║
║  mapper.py   (mapping_version = 1)        ║  retry.py       backoff + jitter + Retry-After   ║
║   · raw → canonical, explicit aliases     ║  incidents.py   open / dedupe / resolve          ║
║   · status enum map (closed set)          ║                                                  ║
║                                           ║                                                  ║
║  implements VendorAdapter Protocol ──────►║  depends only on the Protocol, never on "acme"   ║
╠═══════════════════════════════════════════╩══════════════════════════════════════════════════╣
║  PERSISTENCE  (app/db)  SQLAlchemy · one transaction per record write                        ║
╚══════════════════════════════════════════════════════════════════════════════════════════════╝
```

```
            ┌──────────────────────────── VendorAdapter (Protocol) ───────────────────────────┐
            │ vendor_id: str                     mapping_version: int                          │
            │ fetch_page(tenant, resource, cursor) -> RawPage                                  │
            │ contract(resource) -> ContractSpec         # fields, criticality, enums, envelope│
            │ map(resource, raw_record) -> MapResult      # canonical | MappingError list      │
            └───────────────▲───────────────────────────────────────▲──────────────────────────┘
                            │                                       │
                  AcmeEMRAdapter (built)                GlobexEMRAdapter (stretch / documented)
```

### Walkthrough, layer by layer

**API layer:** HTTP in, HTTP out. Route handlers only parse the request, call the orchestrator or a query, and return JSON. *No business rules here*, so the same logic can later run from a queue worker or CLI without HTTP.

**Orchestration:** the "conductor". It runs the sync loop (described fully in §3). It owns the things that span many records:
- the **circuit breaker** (stop if the vendor is clearly broken),
- the **batch failure threshold** (stop if more than 20% of a page is bad),
- the **checkpoint** (remember where we got to).

**Vendor-specific (`app/vendors/acme/`):** *everything* that knows Acme exists lives here and only here.
- `client.py`: how to talk to Acme. URL, auth headers, timeouts, how Acme paginates (cursor? offset?). It turns the tenant's `locator` into a request: for Acme, `locator.practice_id` becomes the `/{practice_id}/` path segment. An Epic adapter would use `locator.base_url` instead.
- `contract.py`: what we *expect* from Acme, written as data: "patient has `dob`, it's critical, it's a date string"; "appointment `status` is one of `scheduled|completed|cancelled|no_show`"; "the list response is a JSON array".
- `mapper.py`: converts one Acme record into one canonical record. It has a `mapping_version` number that we bump whenever we change the mapping, so every stored record says *which version of the mapping produced it*.

**Vendor-agnostic core (`app/core/`):** knows *nothing* about Acme.
- `canonical.py`: our internal models. Strict Pydantic models shaped like a small subset of **FHIR** (the healthcare interoperability standard: Patient, Appointment, DiagnosticReport).
- `classifier.py`: decides COMPATIBLE / DEGRADED / INCOMPATIBLE (§4).
- `drift.py`: computes the "shape fingerprint" of responses and detects changes (§3, step ④).
- `idempotency.py`: natural keys and content hashes so duplicates don't happen (§6).
- `quarantine.py`, `incidents.py`: storing failures and grouping them into alerts (§7).
- `retry.py`: backoff logic (§5).

**The `VendorAdapter` Protocol:** a Python `typing.Protocol` (an interface). The core calls `adapter.fetch_page()`, `adapter.contract()`, `adapter.map()` and never `if vendor == "acme"`. Adding a vendor means writing a new class that satisfies this interface.

**Persistence:** SQLAlchemy, so the same code runs on SQLite or Postgres just by changing the connection string. One transaction per record write so a crash can't leave half a record (§6).

### Why

| Decision | Rejected alternative | Reason |
|---|---|---|
| Adapter / Ports-and-Adapters (Hexagonal) | One big `sync_patients()` function with `if vendor` branches | Isolates vendor changes. When Acme breaks, you edit one folder. The assignment explicitly asks to "isolate vendor-specific behavior". |
| Contract as **data** (`contract.py`) separate from mapping **code** | Validation hidden inside the mapper | The classifier can reason over a declarative contract generically ("which critical fields are missing?"). Also readable by non-engineers. |
| `mapping_version` on every record | No versioning | Lets us answer "which records were produced by the buggy mapper?" and lets replay be safe and traceable. |
| FHIR-shaped canonical model | Copy the vendor's shape | If the internal model mirrors Vendor A, adding Vendor B means rewriting the core. FHIR is the industry vocabulary, so reviewers in healthcare recognize it immediately. |
| Thin API layer | Logic in route handlers | Testable without HTTP; reusable from workers. |

### Interview drill

- **Q: What changes when Acme renames a field?** A: Only `vendors/acme/mapper.py` (add the alias) and `contract.py`. Bump `mapping_version`. Core untouched.
- **Q: Why a Protocol and not an abstract base class?** A: Either works. A Protocol is structural typing: adapters don't need to inherit anything, mypy checks they have the right methods. ABC is fine too; it's a style choice.
- **Q: Why not use full FHIR?** A: Full FHIR is huge. A subset gives the stable vocabulary without the weight. In production you might use `fhir.resources` models.

---

## 3. Ingestion pipeline: one sync run

### Diagram

```
 POST /sync/{resource}
        │
        ▼
 ┌────────────────┐   OPEN    ┌─────────────────────────────────────────┐
 │Circuit breaker ├──────────►│ 503 + pointer to the open incident      │
 │ (vendor,tenant,│           └─────────────────────────────────────────┘
 │  resource)     │
 └──────┬─────────┘ CLOSED / HALF_OPEN
        ▼
 ┌──────────────────┐  cursor NULL → fresh run: page 1, updated_since = last success start
 │ Load checkpoint  │  cursor set  → last run crashed: resume from that cursor
 └──────┬───────────┘
        ▼
 ┌─ PAGE LOOP ──────────────────────────────────────────────────────────────────────────
 │
 │  ① FETCH            vendor client: throttle, timeouts, retries (§5)
 │        ├── retries exhausted / non-retryable 4xx / body still not JSON
 │        │      └──► request_dlq + incident → STOP        (recovered by re-fetch, §7)
 │        ▼ 2xx and body parsed as JSON
 │  ② PERSIST RAW PAGE raw_payload(vendor, tenant_id, resource, op, cursor, sha256, body)
 │        │            append-only; BEFORE any interpretation
 │        ▼
 │  ③ ENVELOPE CHECK   top-level shape = contract?  (JSON array + X-Next-Cursor header)
 │        ├── no ──► quarantine(scope=PAGE) + incident ENVELOPE_CHANGED
 │        │          → breaker OPEN → STOP              (recovered by page replay, §7)
 │        ▼ yes
 │  ④ DRIFT FINGERPRINT  path → set of JSON types (nulls ignored, array indexes dropped)
 │        │              hash never seen for (vendor, resource)? → one drift_event
 │        ▼              observational only: never blocks
 │  ⑤ FOR EACH ELEMENT OF THE ARRAY
 │     │  not a JSON object? ──────────────► ⑦ QUARANTINE  MALFORMED_RECORD
 │     │  adapter.map(raw) → classifier.classify(result)                       (§4)
 │     │     ├── COMPATIBLE / DEGRADED ────► ⑥ IDEMPOTENT UPSERT (§6)
 │     │     │                                  + record_version + processing_ledger
 │     │     ├── PENDING_DEPENDENCY ───────► pending_record (retried after parent sync)
 │     │     └── INCOMPATIBLE ─────────────► ⑦ QUARANTINE(scope=RECORD) + incident
 │     ▼
 │  ⑧ BATCH HEALTH     page has ≥ 10 records AND bad / page size > 20% ?
 │        ├── yes ──► incident BATCH_THRESHOLD → breaker OPEN → STOP
 │        ▼ no
 │  ⑨ COMMIT CHECKPOINT  cursor = X-Next-Cursor        ← only after ⑥/⑦ committed
 │        ├── cursor already seen in this run? ──► incident PAGINATION_LOOP → STOP
 │        ▼
 │     more pages? ── yes ──► back to ①
 │        │ no
 └────────┼──────────────────────────────────────────────────────────────────────────────
          ▼
   RUN COMPLETE   cursor = NULL, last_success_started_at = this run's start time
   SyncReport { fetched, inserted, updated, unchanged, degraded, pending, quarantined,
                drift_events, incidents }
```

### Walkthrough, step by step

**Before the loop: circuit breaker check.** If we've already decided Acme's patient endpoint is broken (breaker OPEN), we don't even try. We return 503 and point at the existing incident. Why: calling a broken API again just produces more garbage and more quarantine noise.

**Load checkpoint.** The checkpoint for `(acme, tenant, patient)` answers one question: *did the last run finish?*
- `cursor IS NULL` → it finished. Start a fresh run at page 1 and ask only for records changed since the last successful run (`updated_since = last_success_started_at`). We use the last run's *start* time, not its end, so anything changed while that run was in progress gets read again. Reading something twice is harmless (idempotent writes); missing it is not.
- `cursor IS NOT NULL` → the last run crashed mid-way. Resume from that cursor instead of starting over.

**① Fetch.** Ask the vendor client for one page. All the throttling, timeout, retry and backoff handling is inside (§5). Three outcomes:
- **2xx and the body parses as JSON** → continue.
- **2xx but the body isn't JSON** (an HTML error page from a proxy, an empty body, JSON cut off mid-transfer) → treated as transient, like a 5xx. The bytes are saved for evidence, then backoff and retry (§5). An unparseable body never reaches the mapper.
- **Gave up** (retries exhausted, body still malformed after retries, or a 400/401/403/404/422 that won't fix itself) → write a `request_dlq` row saying *which* request failed (resource, cursor, status, attempts), open an incident (`MALFORMED_RESPONSE` or `CONTRACT_OR_AUTH`), stop. There's no usable payload, so recovery is a **re-fetch**, not a replay: the checkpoint still points at this page, so the next run asks for it again (§7).

**② Persist raw page, before parsing.** Save the exact bytes the vendor sent, with a SHA-256 hash, timestamp, resource, operation, vendor and tenant_id. **This is the single most important decision in the system.** Whatever happens next (bad mapping, crash, bug), we still have the original evidence and can reprocess it later *without asking the vendor again*.

**③ Envelope check.** "Envelope" = the wrapper around the records. The contract says `GET /patients` returns `[ {...}, {...} ]`. If it suddenly returns `{ "data": [...], "meta": {...} }`, that's not a per-record problem; *the whole response format changed*. We can't safely interpret any record, so: critical incident, open the breaker, stop.

The envelope also covers *where the next-page cursor lives* (the `X-Next-Cursor` header in v1, §9). Records aren't quarantined one by one here, because none of them can be read reliably. Instead the **page** is quarantined (`scope = PAGE`, pointing at the stored raw payload) so the whole page can be replayed once the adapter understands the new envelope (§7).

**④ Drift fingerprint.** For every record on the page, collect each field path with its JSON type (`name.family: string`, `identifiers[].value: string`…). Three rules keep the fingerprint stable:
- **Union across records.** Each path maps to the *set* of types seen on the page, not one type per record.
- **Nulls are ignored.** `middle: null` on one record and `middle: "Robert"` on the next must not look like a type change. Whether a field may be null is the classifier's question, not drift's.
- **Array indexes are dropped.** `identifiers[0].value` and `identifiers[3].value` both become `identifiers[].value`, so a longer list isn't a new schema.

Sort, hash, and compare with the fingerprints already known for (vendor, resource):
- **Matches the baseline** → nothing to do.
- **A hash never seen before** → store it and write **one** `drift_event` with the diff against the baseline (added / removed / type-changed paths).
- **A hash seen before but not yet baseline** → no new event, just update `last_seen`. Without this, an added field would raise a new drift event on every sync, forever.

A human reviews the drift event and **promotes** the new shape to baseline (`POST /drift/{id}/promote`) once it's understood. Fingerprints are per (vendor, resource), not per tenant: the API's shape belongs to the vendor, so a change seen at one practice is news for all of them.

- **It never blocks by itself.** It's a smoke detector, not a sprinkler. Blocking is the classifier's job. Why separate? Because the fingerprint catches changes that *pass* validation (e.g., a new field appears). Those are safe to accept, but humans should still know the vendor changed something. **This answers README Q1: "how do you detect a vendor change that does not produce an HTTP error?"**

**⑤ For each element of the array.** First: is it a JSON object at all? A `null`, a string or a nested array where a record should be can't be mapped, so it's quarantined as `MALFORMED_RECORD` and the rest of the page carries on. Objects go to `adapter.map()`, and the classifier decides the outcome (full logic in §4).

**⑥ Idempotent upsert** for COMPATIBLE and DEGRADED records. Insert if new, update if changed, do nothing if identical. Also write a version-history row and a ledger row (§6).

**PENDING_DEPENDENCY → `pending_record`.** An appointment whose patient we haven't stored yet is parked, not quarantined. After each patient sync, records waiting on patients are retried. If one is still waiting after N attempts or 24 hours, it's escalated to quarantine as `ORPHAN_REFERENCE`: a vendor that sends appointments for patients it never returns *does* have a contract problem.

**⑦ Quarantine** for INCOMPATIBLE and MALFORMED_RECORD records. Store the record with full diagnostics, and create or update an incident (§7). *Nothing* is written to the clinical tables.

**⑧ Batch health.** If one record in 100 is bad, that's probably a data issue: quarantine it, keep going. If 60 of 100 are bad, the *vendor changed their contract*. Continuing would just fill quarantine with thousands of records. So above 20%, open the breaker and stop. Two details:
- **Minimum sample size.** The rule only applies to pages with at least 10 records. On a 4-record page, one bad record is already 25%; without a floor, a single new status value would halt the whole resource, which is exactly what record-level quarantine exists to avoid.
- **What counts as bad:** INCOMPATIBLE and MALFORMED_RECORD only. DEGRADED records were written, and PENDING is a timing issue.

**⑨ Commit checkpoint.** Save the cursor *only after* this page's writes are committed. If we crash between ⑥ and ⑨, next run re-reads the same page. That's safe because writes are idempotent (duplicates become no-ops). Before moving on, check that the next cursor hasn't already been seen in this run: a vendor bug that hands back an earlier cursor would loop forever. A repeat → `PAGINATION_LOOP` incident, stop.

**Run complete.** Set the cursor back to `NULL` and record `last_success_started_at`. A cursor only means something inside one run; reusing yesterday's cursor tomorrow could skip or repeat data.

**SyncReport.** Every run returns counts. Visible in the API response and logs. Useful for tests (`assert report.quarantined == 3`).

### Why

| Decision | Rejected alternative | Reason |
|---|---|---|
| Raw store **before** parsing | Parse first, log errors | If the parser crashes or is wrong, the evidence is gone. Raw-first means replay never depends on the vendor. |
| Envelope check separate from record checks | Treat everything per-record | A structural change makes *every* record uninterpretable. Detect it once, stop once, one clear incident. |
| Fingerprint is observational | Fingerprint blocks on any change | Blocking on every additive change = constant outages. We *notice* everything but *block* only what's dangerous. |
| Record-level quarantine **plus** batch breaker | Only one of the two | Record-level alone: a systemic break floods quarantine. Batch-level alone: one bad record blocks 99 good ones. Both together isolate small problems and stop fast on big ones. |
| Checkpoint after commit | Checkpoint before processing | Checkpoint-before + crash = skipped records (data loss). Checkpoint-after + crash = re-read (harmless with idempotency). We pick "maybe twice" over "maybe never". |
| 20% threshold | Fixed count / 0% | Tunable per resource. 0% would be too sensitive; a count doesn't scale with page size. In the code it's a config value. |
| Minimum sample (≥ 10 records) before the 20% rule | Percentage alone | One bad record in a 4-record page is 25%. Without a floor, one new status value would halt a whole resource. |
| Malformed body = transient, retried, then request DLQ | Fail the run on the first unparseable body | HTML error pages and cut-off bodies usually come from proxies or dropped connections, and retrying fixes most of them. The bytes are kept; if it persists it still becomes an incident. |
| Page-level quarantine for envelope changes | Record-level quarantine only | An envelope failure happens before there are readable records, so record replay can't recover it. Page scope makes it replayable. |
| Clear the checkpoint when a run completes; incremental via `updated_since` | Keep the last cursor between runs | Cursors are only valid inside one run. Reusing a stale cursor can skip or loop. |

### Interview drill

- **Q: The vendor adds a field. What happens?** A: ④ records drift, ⑤ classifies COMPATIBLE, ⑥ writes. No incident, but `/drift` shows the new field. The new field stays in the raw payload; we don't map it until someone decides it matters.
- **Q: Why not checkpoint every record?** A: Possible, but more DB writes. A page is a natural unit and re-reading one page is cheap thanks to idempotency.
- **Q: What if the crash happens mid-page after 50 of 100 records were written?** A: The 50 are committed (one transaction per record). Next run re-reads the page: the 50 are no-ops (same hash), the other 50 get written. No duplicates, no loss.
- **Q: Is 20% magic?** A: It's a tunable heuristic. In production you'd set it per vendor and resource from observed baselines.
- **Q: The vendor returns an HTML error page with 200 OK. What happens?** A: JSON parsing fails inside fetch, so it never reaches the mapper. We keep the bytes, back off and retry like a 5xx. If it keeps happening: `request_dlq` row + `MALFORMED_RESPONSE` incident, and the next run re-fetches from the checkpoint.
- **Q: One element of the array is `null`.** A: That element is quarantined as `MALFORMED_RECORD`; the other records on the page are processed normally.
- **Q: A page has 3 records and 1 is bad. Does the breaker trip?** A: No. Below 10 records the batch rule doesn't apply; the bad record is quarantined and the rest are written.

---

## 4. Classification decision tree

### Diagram

```
                                   raw record
                                       │
                         ┌─────────────▼──────────────┐
                         │ all CRITICAL fields found   │──── no ───► INCOMPATIBLE
                         │ at their contracted path?   │             MISSING_CRITICAL_FIELD
                         └─────────────┬──────────────┘             (incl. renamed/moved:
                                       │ yes                         dob → birth_date,
                         ┌─────────────▼──────────────┐              mrn → identifiers[0])
                         │ critical field types and    │──── no ───► INCOMPATIBLE
                         │ formats valid? (no coerce)  │             TYPE_CHANGED / BAD_FORMAT
                         └─────────────┬──────────────┘
                                       │ yes
                         ┌─────────────▼──────────────┐
                         │ every enum value is in the  │──── no ───► INCOMPATIBLE
                         │ KNOWN map? (status, etc.)   │             UNKNOWN_ENUM_VALUE
                         └─────────────┬──────────────┘             (never "closest match")
                                       │ yes
                         ┌─────────────▼──────────────┐
                         │ referenced entities exist?  │──── no ───► PENDING_DEPENDENCY
                         │ (appt → patient, looked up  │             (parked in pending_record,
                         │  by vendor+tenant+vendor_id)│              retried, not orphaned)
                         └─────────────┬──────────────┘
                                       │ yes
                         ┌─────────────▼──────────────┐
                         │ any OPTIONAL fields missing │──── yes ──► DEGRADED
                         │ / IMPORTANT fields missing? │             write + warn incident
                         └─────────────┬──────────────┘
                                       │ no
                         ┌─────────────▼──────────────┐
                         │ unknown extra fields?       │──── yes ──► COMPATIBLE + drift_event
                         └─────────────┬──────────────┘             (stored in raw, not mapped)
                                       │ no
                                       ▼
                                  COMPATIBLE
```

### Criticality matrix

```
 ┌────────────────┬──────────────────────────────┬─────────────────────────┬──────────────────┐
 │ Resource       │ CRITICAL (quarantine)        │ IMPORTANT (degraded +   │ OPTIONAL         │
 │                │                              │  high-sev warn)         │ (degraded)       │
 ├────────────────┼──────────────────────────────┼─────────────────────────┼──────────────────┤
 │ Patient        │ vendor_id, mrn, dob,         │ phone, address          │ middle_name,     │
 │                │ family_name, given_name, sex │                         │ email, language  │
 │ Appointment    │ vendor_id, patient_id,       │ provider_id, location   │ notes, reason    │
 │                │ start_time, status           │                         │                  │
 │                │ (vendor_id is also the       │                         │                  │
 │                │  write-back handle — we POST │                         │                  │
 │                │  notes/charges against it)   │                         │                  │
 │ ClinicalReport │ vendor_id, patient_id, type, │ author, issued_at       │ summary, tags    │
 │                │ status, body/result, units   │                         │                  │
 └────────────────┴──────────────────────────────┴─────────────────────────┴──────────────────┘
```

### Walkthrough

The checks run **in order, most dangerous first**. The first failing check decides the outcome (but we collect *all* errors for diagnostics).

0. **Is it even an object?** Checked before the tree (§3 ⑤): a `null`, string or array in place of a record is `MALFORMED_RECORD`.
1. **Critical fields present at the contracted path?** "Contracted path" matters: if `mrn` moved from top-level to `identifiers[0].value`, then from the contract's point of view `mrn` is **missing**. We don't go hunting for it elsewhere. A rename looks exactly like a missing field plus an unexpected new field, and we treat it that way.
2. **Types and formats valid, with no coercion?** If `dob` was `"1985-03-12"` and is now `479433600` (epoch seconds), we do **not** auto-convert. Is it seconds? Milliseconds? UTC or local? A wrong guess shifts a birthday. Pydantic runs in **strict mode** for critical fields. One implementation catch: Pydantic's strict mode, validating a Python `dict`, rejects the *string* `"1985-03-12"` for a `date` field (it wants a real `date` object). So the mapper checks the raw type itself (must be a `str`), parses it with `date.fromisoformat`, and hands Pydantic a real `date`. ISO string accepted, epoch number rejected.
3. **Enum values known?** Appointment `status` must be in our closed map (`scheduled→booked`, `completed→fulfilled`, `cancelled→cancelled`, `no_show→noshow`). A new value like `checked_in_virtual` is quarantined. We never map it to the nearest value or a generic `other`, because status drives clinical workflow (billing, follow-ups, no-show policies). Clinical reports have their own closed map: `preliminary`, `final`, `amended`, `cancelled` (the same words FHIR's DiagnosticReport uses). `amended` is a *known* value, so a report going `final → amended` is a normal update with a new version row, not drift. An unknown value like `corrected` is quarantined.
4. **Referenced entities exist?** The vendor gives us `patient_id = "P-1001"`. We look it up by `(vendor, tenant_id, vendor_id = "P-1001")`, **within the same tenant only**; looking across tenants would bring back the Athena cross-practice bug. We store both our internal `patient_id` FK and the vendor's `vendor_patient_id`. If the patient isn't there yet, we don't create an orphan: the record is parked in `pending_record` as `PENDING_DEPENDENCY` and retried after the next patient sync. That's a *timing* problem, not a contract problem, so it isn't an incident unless it never resolves (§3).
5. **Optional or important fields missing?** `middle_name` missing → write the record with `middle_name = null` explicitly, mark it DEGRADED, log a warning. *Important* fields (phone, address) also write but raise a higher-severity warning because a missing phone matters operationally.
6. **Extra unknown fields?** Fine. COMPATIBLE. They're preserved in the raw payload (never lost), and the drift detector already noted them.

**Criticality matrix:** this is the *policy*. It's per resource and lives in the vendor contract. Rule of thumb:
- **CRITICAL** = getting it wrong could harm a patient or attach data to the wrong person (identity, DOB, clinical result, units, status).
- **IMPORTANT** = operationally significant but not identity- or safety-critical.
- **OPTIONAL** = nice to have.

Note **`units`** on reports is critical: a lab value of `5.4` means nothing (or something dangerous) without knowing if it's mmol/L or mg/dL.

Note **appointment `vendor_id`** is critical because it's the **write-back handle**: when we need to push notes, charges, or status updates back to the EMR, we use this ID. Losing it or mapping it wrong means downstream actions hit the wrong appointment.

### Error codes

One vocabulary, used in quarantine rows, incidents and logs.

| Code | Raised at | Result |
|---|---|---|
| `MALFORMED_RESPONSE` | ① fetch: body still not JSON after retries | `request_dlq` + incident |
| `CONTRACT_OR_AUTH` | ① fetch: 400/401/403/404/422 | `request_dlq` + incident, no retry |
| `ENVELOPE_CHANGED` | ③ envelope check | page quarantine + breaker OPEN |
| `MALFORMED_RECORD` | ⑤ element isn't a JSON object | record quarantine |
| `MISSING_CRITICAL_FIELD` | classifier | record quarantine |
| `TYPE_CHANGED` / `BAD_FORMAT` | classifier | record quarantine |
| `UNKNOWN_ENUM_VALUE` | classifier | record quarantine |
| `ORPHAN_REFERENCE` | pending record gave up waiting | record quarantine |
| `PAGINATION_LOOP` | ⑨ cursor repeated within a run | incident, run stops |
| `BATCH_THRESHOLD` | ⑧ batch health | breaker OPEN |
| `UPSTREAM_UNAVAILABLE` | ① fetch: 5xx / 429 / timeout after all retries | `request_dlq` + incident; breaker OPEN after 3 failed runs |
| `MISSING_IMPORTANT_FIELD` | classifier | written as DEGRADED, warning incident |
| `MISSING_OPTIONAL_FIELD` | classifier | written as DEGRADED, info incident |
| `INTERNAL_ERROR` | any unexpected exception inside a run (gateway bug, DB failure) | incident + breaker failure count; run rolled back, error re-raised (HTTP 500) |

### Why

| Decision | Rejected alternative | Reason |
|---|---|---|
| Three outcomes (+ pending) | Binary valid/invalid | Binary forces a bad choice: reject harmless changes (outages) or accept dangerous ones (bad data). Three outcomes let us be lenient *and* safe. |
| Rename = missing + new field | Auto-detect and follow renames | Heuristics can be wrong. `birth_date` *might* be the patient's DOB or a family member's. A human confirms (see suggestion engine, §11 D3). |
| No type coercion on critical fields | Pydantic lax mode | Lax mode turns `"1"` into `True`, `479433600` into a date. Silent conversion = silent corruption. |
| Closed enum maps | Default `other` / fuzzy match | A new status is a *semantic* change from the vendor. It needs a human decision about what it means for us. |
| Collect all errors, decide by worst | Fail on first error | Better diagnostics: the incident shows *every* problem with the record, so one fix covers all of them. |

### Interview drill

- **Q: `birth_date` obviously means `dob`. Why not map it automatically?** A: "Obviously" is a guess. If we're wrong once, a patient's age-based dosing or identity match is wrong. Quarantining costs minutes of delay; a wrong DOB can cost patient safety. We *suggest* the alias automatically and a human approves it in seconds.
- **Q: How do you decide what's critical?** A: With clinical and product stakeholders. The matrix is configuration, not code, so it's reviewable.
- **Q: A new *optional* field appears. Do we alert?** A: No alert. A drift event is visible in `/drift`. Alert fatigue is a real reliability risk.
- **Q: Why is appointment vendor_id critical?** A: It's the write-back handle. We POST notes and charges against it. A wrong or missing appointment ID means we write clinical notes to the wrong visit or lose the ability to push data back.

---

## 5. Vendor Client: retries, rate limits, circuit breaker

### Diagram

```
  fetch_page()
      │
      ▼
 ┌──────────┐  token bucket per (vendor, tenant): e.g. 10 req/s
 │ Throttle │──── wait if empty
 └────┬─────┘
      ▼
 ┌──────────┐   timeout: connect 3s / read 10s
 │ HTTP GET │
 └────┬─────┘
      │
      ├── 2xx ──► body is valid JSON? ── yes ──────────────────────► return RawPage
      │                     └─ no ─► MALFORMED_RESPONSE: keep bytes → backoff ─┐
      │                                                                        │
      ├── 429 ──► Retry-After present? ── yes ─► sleep(min(Retry-After, cap)) ─┤
      │                     └─ no ─► backoff ──────────────────────────────────┤
      │                                                                        │
      ├── 5xx / timeout / conn reset ──► backoff ──────────────────────────────┤
      │                                                                        │
      │        backoff = random(0, min(cap=30s, base=0.5s · 2^attempt))        │
      │        (full jitter, avoids thundering herd)                           │
      │                                                                        ▼
      │                                                   attempt < max(5)? ── yes ─► retry
      │                                                          │ no
      │                                                          ▼
      │                                         request_dlq + incident + breaker.record_failure()
      │
      └── 400/401/403/404/422 ──► NO RETRY ──► incident CONTRACT_OR_AUTH (critical) → STOP


  Circuit breaker per (vendor, tenant, resource):

        ┌──────────┐  N consecutive failures      ┌──────────┐
        │  CLOSED  │  or batch quarantine > 20%   │   OPEN   │
        │ (normal) ├─────────────────────────────►│ (reject) │
        └────▲─────┘                              └────┬─────┘
             │ probe succeeds                          │ cooldown elapsed (e.g. 60s)
             │                                         ▼     or manual reset after fix
             │                                  ┌────────────┐
             └──────────────────────────────────┤ HALF_OPEN  │── probe fails ──► OPEN
                                                │ (1 probe)  │
                                                └────────────┘
```

### Walkthrough

**Throttle (token bucket).** Picture a bucket that refills at 10 tokens per second. Each request takes a token; if the bucket is empty, we wait. This keeps us *under* the vendor's limit so we rarely see 429s at all. Prevention beats retry.

**Timeouts.** *Connect* timeout (3s): can we reach the server? *Read* timeout (10s): once connected, how long do we wait for a response? Without timeouts, one hung request blocks the sync forever. Both are settings, so tests can use a 1s read timeout and make the `flaky` scenario's slow response (2s) really time out without slowing the suite.

**Response categories: the key idea is "is retrying *likely to help*?"**

| Response | Retry? | Why |
|---|---|---|
| 2xx | — | Success. |
| 429 Too Many Requests | Yes, honoring `Retry-After` | The vendor *told us* when to come back. Respect it (capped, so a buggy `Retry-After: 99999` can't freeze us). |
| 5xx, timeout, connection reset | Yes, with backoff | Usually transient: server restarting, overloaded, network blip. |
| 2xx but body isn't JSON (HTML error page, empty, cut off) | Yes, with backoff | Usually a proxy, CDN or load balancer answering for the vendor, or a dropped connection. The bytes are saved as a raw payload first so the evidence survives. Still malformed after max attempts → `request_dlq` + `MALFORMED_RESPONSE` incident. |
| 400, 401, 403, 404, 422 | **No** | Retrying the same wrong request gives the same wrong answer. 401/403 = credentials problem. 404 = endpoint moved. 400/422 = our request no longer matches their API. These need a human. |

**We don't trust `Content-Type`.** Some vendors send JSON labelled `text/plain`, and some send HTML labelled `application/json`. We try to parse the body and let that decide. A mismatched header is logged as a warning, nothing more.

**Exponential backoff with full jitter:**
`delay = random(0, min(30s, 0.5s × 2^attempt))`
- attempt 0 → 0–0.5s, attempt 1 → 0–1s, attempt 2 → 0–2s, attempt 3 → 0–4s, attempt 4 → 0–8s …capped at 30s.
- **Exponential:** each failure waits longer, giving the vendor time to recover.
- **Jitter (randomness):** if 50 tenants all fail at the same moment and all retry at exactly 1s, 2s, 4s, they hammer the vendor in synchronized waves (the **thundering herd**). Random delays spread them out. "Full jitter" (random between 0 and the cap) is the variant AWS's architecture blog showed works best.

**After max attempts (5):** write the failed *request* to a request DLQ (so we know which page/cursor wasn't fetched), open an incident, tell the breaker.

**Circuit breaker:** three states, like an electrical breaker.
- **CLOSED** (normal): requests flow.
- **OPEN** (tripped): after N consecutive failures *or* the batch-quarantine threshold from §3, we stop calling the vendor for this resource. Calls fail immediately with a clear reason.
- **HALF_OPEN**: after a cooldown (or a manual reset once we've deployed a fix), let **one** probe request through. Success → CLOSED. Failure → back to OPEN.

It's **per (vendor, tenant, resource)**: if Acme's practice-123 reports endpoint is broken, practice-456 patient syncs keep running.

### Why

| Decision | Rejected alternative | Reason |
|---|---|---|
| Classify retryable vs non-retryable | Retry everything | Retrying a 401 wastes time, burns rate limit, and can lock the account. |
| Full jitter | Fixed delay / exponential without jitter | Avoids synchronized retry storms across tenants. |
| Honor `Retry-After`, capped | Ignore it / trust blindly | Respect the vendor's guidance, but protect ourselves from absurd values. |
| Client-side token bucket | Only react to 429s | Fewer 429s, smoother load, better vendor relationship. |
| Breaker per (vendor, tenant, resource) | Global breaker | Failure isolation: one broken endpoint shouldn't stop healthy ones. One tenant's broken API shouldn't block others. |
| Breaker also trips on *data* failures (quarantine rate) | Only on HTTP failures | A vendor returning HTTP 200 with garbage is still broken. This is unusual and worth pointing out in the interview. |

Library choice: **`tenacity`** for retries (well-known, declarative) and **`httpx`** for HTTP (async-capable, explicit timeouts). The breaker is small enough to write ourselves (~40 lines), which also shows understanding.

### Interview drill

- **Q: Why not retry 400?** A: It's deterministic. Same request, same answer. It signals a contract change and needs a human.
- **Q: What about 408 or 409?** A: 408 (request timeout) is retryable. 409 (conflict) depends on the API. For reads it's unusual, so treat it as non-retryable and alert.
- **Q: What if `Retry-After` is an HTTP date instead of seconds?** A: The spec allows both formats. We parse both.
- **Q: Isn't the breaker redundant with retries?** A: Retries handle *one request's* bad luck. The breaker handles *a system that is down*: it stops all requests from each burning 5 retries.

---

## 6. Idempotent write path

### Diagram

```
  canonical record
        │
        ▼
  natural_key  = (vendor, tenant_id, vendor_resource_id)
  content_hash = sha256(canonical JSON, sorted keys, volatile fields excluded e.g. fetched_at)
        │
        ▼
 ┌──────────────────────────────── BEGIN TRANSACTION ───────────────────────────────┐
 │                                                                                   │
 │   SELECT current_hash FROM <resource> WHERE natural_key = ?                       │
 │        │                                                                          │
 │        ├── not found ──────────► INSERT row (version=1)                           │
 │        │                         INSERT record_version(v1, hash, raw_payload_id)  │
 │        │                                                                          │
 │        ├── found, hash equal ──► NO-OP  (duplicate delivery / replay / re-page)   │
 │        │                                                                          │
 │        └── found, hash differs ► UPDATE row (version += 1)  WHERE version = old   │
 │                                  INSERT record_version(vN, hash, diff)            │
 │                                   ↑ optimistic lock: concurrent writer → retry    │
 │                                                                                   │
 │   INSERT processing_ledger(raw_payload_id, record_key, outcome, mapping_version)  │
 │        UNIQUE(raw_payload_id, record_key, mapping_version)  ← replay safety       │
 │                                                                                   │
 └──────────────────────────────── COMMIT ───────────────────────────────────────────┘

  Guarantees:  at-least-once fetch  +  idempotent write  =  effectively-once records
               DB UNIQUE(vendor, tenant_id, vendor_resource_id) is the last line of defence
```

### Walkthrough

**Natural key** = *who* this record is, in the vendor's world: `(acme, practice_123, "P-1001")`. It's how we recognize "we've seen this before". The key includes `tenant_id` (practice/clinic ID) because **vendor resource IDs are only unique within a tenant, not globally**. In Athena, for example, patient IDs, provider IDs and appointment IDs can repeat across practices — practice A and practice B can both have patient `P-1001`, but they're different people. Without the tenant dimension, one silently overwrites the other. We never use name+DOB as identity because two different patients can share them, and one patient can have typos.

**What a tenant is, across EHRs.** Every EHR scopes its IDs to some kind of customer, but each one addresses it differently:

| EHR | Tenant boundary | How the API addresses it |
|---|---|---|
| athenahealth | practice | `practiceid` in the path: `/v1/{practiceid}/patients` (departments live *inside* a practice) |
| Oracle Health (Cerner) | health-system tenant | tenant GUID in the FHIR base URL: `.../r4/{tenant-guid}/Patient` |
| Epic | customer organization | no id in the path; each organization has its own FHIR base URL (listed in Epic's endpoint directory) |
| eClinicalWorks | practice | practice code in the FHIR base URL: `.../fhir/r4/{practice-code}` |
| NextGen Enterprise | enterprise + practice | set once per session; later calls carry an `X-NG-SESSIONID` header |

What they all share is a *connection*: one base URL, one set of credentials, one scope inside which vendor IDs are unique. So `tenant_id` is **our own id for that connection** (a row in the `tenant` table, §8). The vendor-specific part is stored as the tenant's `locator`: `{"practice_id": "195900"}` for Athena, `{"base_url": "…"}` for Epic or Cerner. Only the vendor adapter reads the locator. The core and every natural key only see `tenant_id`, so they look the same for every vendor.

Two things are deliberately **not** the tenant:
- **Department / location** (Athena department, Epic department, NextGen location) sits *inside* a tenant. IDs are unique per tenant, not per department, so it's a column on the appointment, not part of the key. Putting it in the key would split one patient into several rows.
- **NPI** identifies organizations and providers for billing. A practice can have several, and no EHR API scopes data by it, so it's data on a record, not a tenant.

**Content hash** = *what* the record says right now. SHA-256 of the canonical JSON with keys sorted (so field order doesn't change the hash) and **volatile fields excluded** (like `fetched_at`, which changes every time even if the data didn't).

**Three cases:**
1. **New** → insert with `version = 1`, plus a history row pointing to the raw payload it came from.
2. **Same hash** → do nothing. This is what makes duplicates harmless: the same page fetched twice, a retry, a replay, a crash-resume.
3. **Different hash** → real change (e.g., patient changed phone). Update the row, bump `version`, write a history row with what changed.

**Optimistic lock (`WHERE version = old`).** If two workers update the same patient at once, only one `UPDATE` matches the old version; the other updates 0 rows, detects it, re-reads and retries. No lost updates, no heavy locking. In the assignment this is defense-in-depth (single process), but in production it matters. The real answer to concurrent writers is to partition the queue by patient so the race never happens (§10).

**Processing ledger.** A record of "raw payload X, record Y, processed with mapping version Z → outcome". The `UNIQUE` constraint means the exact same processing can't happen twice. On replay with a *new* mapping version, it's a different row, so the replay is allowed and auditable.

**Transaction.** Upsert, history and ledger commit together or not at all. No half-written records.

**"At-least-once + idempotent = effectively-once."** Networks and crashes mean we can't guarantee we fetch something exactly once. We *can* guarantee we fetch it **at least** once (retries, checkpoints). Then idempotent writes turn "maybe twice" into "same as once". This is the standard pattern in distributed systems, because true exactly-once delivery is generally impossible.

**DB `UNIQUE` constraint.** Even if application logic had a bug, the database refuses a second row with the same `(vendor, tenant_id, vendor_id)`. Defense in depth.

### Why

| Decision | Rejected alternative | Reason |
|---|---|---|
| Natural key includes `tenant_id` | Key on `(vendor, vendor_id)` only | Vendor resource IDs repeat across practices (seen in Athena). Without `tenant_id`, practice B's patient silently overwrites practice A's — wrong patient data. |
| `tenant_id` is our own id; the vendor's identifier is the tenant's `locator` | Use the vendor's practice id directly as the key | Only Athena has a practice id. Cerner puts a tenant GUID in the URL, Epic gives each organization its own base URL, eCW uses a practice code, NextGen uses an enterprise/practice session. Our own id keeps keys identical across vendors. |
| Department / location is a column | Department in the natural key | IDs are unique per tenant, not per department. Keying on department would split one patient into several rows. |
| Natural key from vendor ID | Name + DOB matching | Demographic matching causes wrong-patient merges, a serious patient-safety event. MPI/EMPI matching is a separate, specialized system. |
| Content hash | Always overwrite | Lets us skip no-op writes, detect real changes, and keep meaningful history. |
| Version history (SCD Type 2 style) | Overwrite in place | Clinical data needs an audit trail: "what did we believe about this patient on date X, and where did it come from?" |
| Optimistic locking | `SELECT ... FOR UPDATE` / none | Cheap in the common (no conflict) case; correct under concurrency. |
| Ledger with mapping_version | No ledger | Proves which code version processed which payload. Makes replay safe and traceable. |

### Interview drill

- **Q: How do you prevent duplicates?** A: Three layers: (1) natural key lookup, (2) content-hash no-op, (3) DB unique constraint.
- **Q: Why is `tenant_id` in the key?** A: EMR resource IDs are only unique within a practice. In Athena, patient IDs repeat across practices. Without `tenant_id`, we'd overwrite one practice's patient with another's — a patient-safety issue I've encountered directly.
- **Q: Vendor sends the same patient with a different ID?** A: That's a vendor-side duplicate (identity resolution). We don't auto-merge; it would be flagged by an MPI process. Out of scope, but worth mentioning.
- **Q: Epic has no practice id. What's `tenant_id` there?** A: Our own id for the connection. For Epic its locator is the organization's FHIR base URL; for Cerner the base URL with the tenant GUID; for Athena the practice id. The adapter turns the locator into requests; nothing else changes.
- **Q: Why exclude `fetched_at` from the hash?** A: Otherwise every sync looks like a change and you'd write a new version every time.

---

## 7. Quarantine, incident and replay lifecycle

### Diagram

```
                       INCOMPATIBLE record
                               │
                               ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ quarantine row                                                     │
   │  id · vendor · tenant_id · resource · operation (list/get)         │
   │  endpoint · raw_payload_id · record_index                          │
   │  mapping_version_at_failure                                        │
   │  error_codes[] · error_detail (path, expected, actual)             │
   │  fingerprint_diff · first_seen · attempts · status                 │
   └───────────────┬────────────────────────────────────────────────────┘
                   │ grouped by (vendor, tenant_id, resource, error_code, field_path)
                   ▼
   ┌────────────────────────────────┐          ┌──────────────────────────────┐
   │ incident (deduplicated)        │─────────►│ GET /incidents, /health      │
   │  severity · count · first/last │          │ structured log: event=       │
   │  sample_payload_ids · status   │          │  "integration.incompatible"  │
   └────────────────────────────────┘          └──────────────────────────────┘


   Quarantine record state machine:

   ┌─────────────┐   engineer ships mapper fix    ┌────────────┐
   │ QUARANTINED ├───── (mapping_version 1→2) ───►│ REPLAYING  │
   └──────▲──────┘     POST /replay?filters       └─────┬──────┘
          │                                             │
          │  still incompatible                         │ classify with NEW mapper
          │  (new error, attempts++)                    │ from STORED raw payload
          └─────────────────────────────────────────────┤ (no vendor call)
                                                        │ ok → idempotent upsert
                                                        ▼
                                                 ┌────────────┐     all records of an
                                                 │  RESOLVED  ├───► incident resolved
                                                 └────────────┘     → incident CLOSED
                                                                    → breaker reset

   ┌────────────┐
   │ DISCARDED  │◄── manual: confirmed vendor test data / true garbage (audit reason kept)
   └────────────┘
```

### Walkthrough

**Quarantine row: everything an engineer needs at 3 AM.**
- *Where*: vendor, tenant_id, resource, operation, endpoint. The assignment asks to "identify the affected resource and operation".
- *What*: a pointer to the raw payload and which record in that page (`record_index`).
- *Why*: error codes (`MISSING_CRITICAL_FIELD`) and details (`path=dob, expected=string(date), actual=<absent>`), plus the fingerprint diff (`removed: dob; added: birth_date`). Usually that diff alone tells you the fix.
- *Which code*: `mapping_version_at_failure`.
- *History*: first seen, number of attempts, current status.

**Incident = deduplicated alert.** If 5,000 patients all fail for the same reason, that's **one** incident ("acme / practice_123 / patient / MISSING_CRITICAL_FIELD / dob", count = 5000), not 5,000 alerts. The group key is `(vendor, tenant_id, resource, error_code, field_path)`. Incidents have severity, counts, first and last seen, and a few sample payload IDs. Including `tenant_id` in the key has a cost: a vendor-wide break across 50 practices opens 50 incidents. At assignment scale that's acceptable and shows exactly which practices are hit. In production you'd add a parent incident keyed without `tenant_id` that rolls them up, and page on the parent.

**State machine:**
1. **QUARANTINED**: parked, nothing written to clinical tables.
2. An engineer reads the incident, fixes `mapper.py` (adds the `birth_date` alias), bumps `mapping_version` 1 → 2, deploys.
3. **`POST /replay?resource=patient&error_code=MISSING_CRITICAL_FIELD`** → records move to **REPLAYING**. Each one is re-processed from the **stored raw payload** with the **new** mapper. No vendor call.
4. Passes → goes through the normal idempotent upsert → **RESOLVED**.
   Still fails (maybe a second problem) → back to **QUARANTINED** with the new error, `attempts++`.
5. When every record in an incident is resolved → incident **CLOSED** → breaker reset.
6. **DISCARDED**: a manual escape hatch for genuinely bad data (vendor test records). It requires a reason, and the reason is kept for audit.

**Why replay from stored raw instead of re-fetching?** The vendor may have changed *again*, the records may no longer be returned (e.g., "only last 24h" endpoints), or the vendor may be rate-limiting. Stored raw is the exact data that failed.

**Four kinds of failure, four ways back.**

| What failed | Where it's kept | How it's recovered |
|---|---|---|
| One record (INCOMPATIBLE, MALFORMED_RECORD) | `quarantine`, `scope = RECORD` | `POST /replay?scope=record&resource=…&error_code=…` re-maps it from stored raw |
| A whole page (ENVELOPE_CHANGED) | `quarantine`, `scope = PAGE` | `POST /replay?scope=page…` re-runs steps ③–⑦ on the stored page with the fixed adapter; its records then go through normal classification |
| A request (retries exhausted, MALFORMED_RESPONSE, 4xx) | `request_dlq` | **Re-fetch**, not replay: there's no usable payload. The checkpoint still points at that page, so the next sync asks again; the DLQ row closes when it succeeds |
| A record waiting for its parent (PENDING_DEPENDENCY) | `pending_record` | Retried automatically after the parent resource syncs; escalated to quarantine as `ORPHAN_REFERENCE` after N attempts / 24h |

### Why

| Decision | Rejected alternative | Reason |
|---|---|---|
| Rich diagnostics per record | Just the error message | Speeds up root cause from hours to minutes; the fingerprint diff usually *is* the answer. |
| Incident dedup by group key | One alert per record | Alert fatigue. On-call engineers ignore noisy systems. |
| Replay from stored raw | Re-fetch from vendor | Deterministic, vendor-independent, no rate-limit cost. |
| Explicit state machine | Boolean `processed` flag | Clear lifecycle, auditable, supports partial fixes and repeated attempts. |
| DISCARDED needs a reason | Delete rows | Healthcare data handling needs an audit trail for *why* data was dropped. |

### Interview drill

- **Q: Walk me through recovering from `dob → birth_date`.** A: Sync quarantines all patients, the breaker opens, an incident shows the diff `removed dob / added birth_date`. Fix the mapper alias, bump to v2, deploy, `POST /replay`. Records re-map from stored raw, upsert idempotently, incident closes, breaker resets, next sync resumes from the checkpoint. **This is README Q3.**
- **Q: What if the replay is run twice?** A: Second run: records already RESOLVED are skipped; any re-processing hits the content hash and does nothing.
- **Q: Can replay make things worse?** A: It runs the same classifier, so a still-broken mapping just re-quarantines. Nothing bypasses validation.

---

## 8. Data model

### Diagram

```
 TENANTS  (one row per vendor connection; every tenant_id column below → tenant.id)

 ┌────────────────────────┐
 │ tenant                 │
 │────────────────────────│
 │ id PK                  │
 │ vendor                 │
 │ name                   │
 │ external_key           │
 │ locator (JSON)         │
 │ credentials_ref        │
 │ active                 │
 │ created_at             │
 └────────────────────────┘

 CLINICAL TABLES  (only COMPATIBLE / DEGRADED records land here)

 ┌────────────────────────┐    ┌────────────────────────┐    ┌────────────────────────┐
 │ patient                │    │ appointment            │    │ clinical_report        │
 │────────────────────────│    │────────────────────────│    │────────────────────────│
 │ id PK                  │◄─┐ │ id PK                  │    │ id PK                  │
 │ vendor                 │  ├─┤ patient_id FK          │ ┌──┤ patient_id FK          │
 │ tenant_id              │  │ │ vendor_patient_id      │ │  │ vendor_patient_id      │
 │ vendor_id              │  │ │ vendor, tenant_id      │ │  │ vendor, tenant_id      │
 │ mrn, dob, sex          │  │ │ vendor_id (write-back) │ │  │ vendor_id              │
 │ given/family/middle    │  │ │ start_time, status     │ │  │ type, status, units    │
 │ phone, email, address  │  │ │ provider_id, location  │ │  │ body, author, issued_at│
 │ content_hash, version  │  │ │ content_hash, version  │ │  │ content_hash, version  │
 │ mapping_version        │  │ │ mapping_version        │ │  │ mapping_version        │
 │ updated_at             │  │ │ updated_at             │ │  │ updated_at             │
 └────────────────────────┘  │ └────────────────────────┘ │  └────────────────────────┘
                             └────────────────────────────┘

 EVIDENCE AND PROCESSING

 ┌────────────────────────┐    ┌────────────────────────┐    ┌────────────────────────┐
 │ raw_payload            │    │ record_version         │    │ processing_ledger      │
 │────────────────────────│    │────────────────────────│    │────────────────────────│
 │ id PK                  │◄─┐ │ id PK                  │    │ id PK                  │
 │ vendor, tenant_id      │  │ │ resource, record_id    │ ┌──┤ raw_payload_id FK      │
 │ resource, operation    │  │ │ version                │ │  │ record_key             │
 │ endpoint, cursor       │  │ │ content_hash           │ │  │ mapping_version        │
 │ http_status            │  │ │ diff (JSON)            │ │  │ outcome                │
 │ sha256 (indexed)       │  ├─┤ raw_payload_id FK      │ │  │ processed_at           │
 │ body (raw bytes/JSON)  │  │ │ mapping_version        │ │  │ UQ(raw,key,mapver)     │
 │ fetched_at             │  │ │ created_at             │ │  │                        │
 └────────────────────────┘  │ └────────────────────────┘ │  └────────────────────────┘
                             └────────────────────────────┘

 FAILURE HANDLING

 ┌────────────────────────┐    ┌────────────────────────┐    ┌────────────────────────┐
 │ quarantine             │    │ incident               │    │ request_dlq            │
 │────────────────────────│    │────────────────────────│    │────────────────────────│
 │ id PK                  │ ┌─►│ id PK                  │◄─┐ │ id PK                  │
 │ incident_id FK         ├─┘  │ group_key UQ           │  └─┤ incident_id FK         │
 │ raw_payload_id FK      │    │ vendor, tenant_id      │    │ raw_payload_id FK null │
 │ scope: RECORD | PAGE   │    │ resource, error_code   │    │ vendor, tenant_id      │
 │ record_index (null=pg) │    │ field_path             │    │ resource, operation    │
 │ vendor, tenant_id      │    │ severity, status       │    │ endpoint, cursor       │
 │ resource, operation    │    │ count                  │    │ failure_kind           │
 │ error_codes, detail    │    │ first_seen, last_seen  │    │ http_status, attempts  │
 │ fingerprint_diff       │    │ sample_payload_ids     │    │ status (OPEN|REFETCHED)│
 │ mapping_version        │    │ resolved_at            │    │ created_at             │
 │ status, attempts       │    │                        │    │                        │
 │ first_seen, last_tried │    │                        │    │                        │
 └────────────────────────┘    └────────────────────────┘    └────────────────────────┘

 DRIFT, PENDING AND SYNC STATE

 ┌────────────────────────┐    ┌────────────────────────┐    ┌────────────────────────┐
 │ schema_fingerprint     │    │ drift_event            │    │ pending_record         │
 │────────────────────────│    │────────────────────────│    │────────────────────────│
 │ id PK                  │◄─┐ │ id PK                  │    │ id PK                  │
 │ vendor, resource       │  └─┤ fingerprint_id FK UQ   │    │ raw_payload_id FK      │
 │ hash                   │    │ vendor, resource       │    │ vendor, tenant_id      │
 │ field_types (JSON)     │    │ first_seen_tenant_id   │    │ resource, vendor_id    │
 │ is_baseline            │    │ added/removed/changed  │    │ waiting_on (resource,  │
 │ first_seen, last_seen  │    │ raw_payload_id FK      │    │   vendor_id)           │
 │ promoted_at            │    │ status (NEW|ACKED)     │    │ attempts, next_try_at  │
 │                        │    │ created_at             │    │ status, created_at     │
 └────────────────────────┘    └────────────────────────┘    └────────────────────────┘

 ┌────────────────────────┐    ┌────────────────────────┐
 │ sync_checkpoint        │    │ circuit_state          │
 │────────────────────────│    │────────────────────────│
 │ id PK                  │    │ id PK                  │
 │ vendor, tenant_id      │    │ vendor, tenant_id      │
 │ resource               │    │ resource               │
 │ cursor (NULL = idle)   │    │ state                  │
 │ run_started_at         │    │ consecutive_failures   │
 │ last_success_started_at│    │ opened_at, open_until  │
 │ updated_at             │    │ incident_id FK         │
 └────────────────────────┘    └────────────────────────┘

 UNIQUE constraints (natural keys and dedupe keys)
   patient, appointment, clinical_report   (vendor, tenant_id, vendor_id)
   processing_ledger                       (raw_payload_id, record_key, mapping_version)
   incident                                (group_key)
   pending_record                          (vendor, tenant_id, resource, vendor_id)
   schema_fingerprint                      (vendor, resource, hash)
   drift_event                             (fingerprint_id)
   sync_checkpoint, circuit_state          (vendor, tenant_id, resource)
   tenant                                  (vendor, external_key)
   raw_payload                             none; INDEX(vendor, tenant_id, resource, sha256)

 Foreign keys
   every tenant_id column                              → tenant.id
   appointment.patient_id, clinical_report.patient_id  → patient.id
        (resolved via vendor + tenant_id + vendor_patient_id, same tenant only)
   record_version, processing_ledger, quarantine, pending_record, drift_event
        .raw_payload_id                                 → raw_payload.id
   request_dlq.raw_payload_id (nullable)                → raw_payload.id
   quarantine.incident_id, request_dlq.incident_id,
        circuit_state.incident_id (nullable)            → incident.id
   drift_event.fingerprint_id                           → schema_fingerprint.id
```

### Walkthrough, table by table

| Table | Purpose | Key constraint |
|---|---|---|
| `tenant` | One row per vendor connection. `external_key` is the vendor's identifier as a string (Athena practice id, Cerner tenant GUID, Epic base URL); `locator` is whatever the adapter needs to build requests; `credentials_ref` points at a secret, never holds it. | `UQ(vendor, external_key)`; every `tenant_id` column is an FK to it |
| `raw_payload` | One row per fetch: the bytes exactly as received (malformed bodies included), plus which tenant, resource and cursor, and when. Append-only evidence. | `INDEX(vendor, tenant_id, resource, sha256)`, deliberately **not** unique (see Why) |
| `patient`, `appointment`, `clinical_report` | **Canonical** clinical data, the "clean" tables. Only COMPATIBLE/DEGRADED records get here. | `UQ(vendor, tenant_id, vendor_id)` blocks duplicates at the DB level. `tenant_id` is required because vendor resource IDs are only unique within a practice/clinic, not globally (learned from Athena). The `patient_id` FK on appointment/report blocks orphans; it's resolved within the same tenant, and the vendor's `vendor_patient_id` is kept alongside it. |
| `record_version` | History of every change to a canonical record, linked to the raw payload that caused it. | (resource, record_id, version) |
| `processing_ledger` | Which payload + record + mapping version was processed, and the outcome. | `UQ(raw, key, mapver)` for replay safety. |
| `quarantine` | Data we refused, with diagnostics and lifecycle status. `scope = RECORD` (one element) or `PAGE` (a whole page after an envelope change; `record_index` is NULL). | FK → raw_payload (the evidence), FK → incident |
| `incident` | Deduplicated alerts. | `UQ(key)`: one incident per problem type. |
| `pending_record` | Records waiting for a parent (an appointment that arrived before its patient). Retried automatically; escalated to quarantine as `ORPHAN_REFERENCE` if it never resolves. | `UQ(vendor, tenant_id, resource, vendor_id)` |
| `request_dlq` | Requests that never produced a usable page: retries exhausted, `MALFORMED_RESPONSE`, 4xx. Recovered by re-fetching. | FK → incident; FK → raw_payload, nullable (set when malformed bytes were saved) |
| `schema_fingerprint` | Every distinct response shape seen per (vendor, resource); one is the baseline. | `UQ(vendor, resource, hash)` |
| `drift_event` | One row per *new* shape: its diff against the baseline and its review status. Feeds `GET /drift`. | `UQ(fingerprint_id)`: one event per new shape, not per sync |
| `sync_checkpoint` | `cursor` (NULL = no run in progress; set = resume point after a crash) and `last_success_started_at` for incremental syncs. | `UQ(vendor, tenant_id, resource)` |
| `circuit_state` | Breaker state per (vendor, tenant, resource). Persisted so a restart doesn't forget the vendor is broken. | `UQ(vendor, tenant_id, resource)` |

**`mapping_version` on canonical rows:** lets you run `SELECT * FROM patient WHERE mapping_version = 1` to find everything produced by a mapper later found buggy.

### Why

- **`tenant_id` in every key.** Vendor resource IDs (patient, appointment, provider) repeat across practices. Without `tenant_id`, practice B's patient silently overwrites practice A's — a data corruption bug we've hit in production with Athena.
- **A `tenant` table with our own id**, not the vendor's practice id in every row. Only Athena has a practice id; other EHRs identify the customer by base URL, tenant GUID, practice code or session (§6). An FK from every `tenant_id` column also means a typo'd tenant can't create data out of nowhere.
- **`raw_payload` has no unique constraint on the hash.** Each row is one *fetch*: when, which tenant, which cursor. Two tenants can return byte-identical pages (an empty `[]` is the obvious case), and one tenant can get the same page on two runs. `UNIQUE(sha256)` would either fail the insert or attach tenant B's fetch to tenant A's row. We index the hash for lookups instead. In production the blob can be content-addressed in object storage while the metadata rows stay one per fetch.
- **Child records keep `vendor_patient_id` next to the FK.** The FK is our internal id (fast joins, enforced integrity). The vendor's id lets us re-resolve after a replay, answer "which patient did the vendor mean?", and write back using the vendor's identifiers.
- **`schema_fingerprint` is per vendor, not per tenant.** The API's shape belongs to the vendor. `drift_event` records which tenant saw a new shape first. If a vendor ever runs different API versions per practice, adding `tenant_id` here is a one-column change.
- **One `quarantine` table with a `scope` column**, not one table per failure type: record and page failures share diagnostics, incidents and the replay endpoint.
- **`pending_record` is separate from `quarantine`.** Waiting for a parent isn't a refusal. Mixing them would inflate quarantine counts and raise incidents for a timing issue.
- **Breaker and checkpoint state in the DB, not memory.** A restart (deploy, crash) must not reset "the vendor is broken" or "we were on page 7".
- **JSON column for raw body.** We don't know the vendor's future shape, so we store it schemaless. SQLite stores it as text; Postgres would use `JSONB` (queryable and indexable).
- **Normalized canonical tables** (not a single JSON blob) so DB constraints (FK, UNIQUE, enum CHECK) enforce integrity, not just application code.

### Interview drill

- **Q: Where would you store raw payloads at scale?** A: Object storage (S3/GCS), encrypted, with a retention policy. The DB keeps the pointer and hash.
- **Q: Is raw payload storage a PHI risk?** A: Yes. It's a full copy of patient data. Encrypt at rest, restrict access, set retention, audit reads. This is a production item (§11, README Q5).
- **Q: Why `tenant_id` and not just `vendor`?** A: In Athena, patient ID "P-1001" in practice A and "P-1001" in practice B are different people. Without `tenant_id` in the key, practice B's sync overwrites practice A's patient data. This was a real bug I've encountered.

---

## 9. Mock vendor scenarios and expected gateway response

### The documented v1 contract (`mock_emr/CONTRACT.md`)

The assignment says to *begin* with a documented contract. Everything the gateway expects is written here; any deviation is an undocumented change.

```
GET /{practice_id}/patients | /appointments | /reports
    ?limit=10&cursor=<opaque>&updated_since=<ISO-8601>

200 OK
Content-Type: application/json
X-Next-Cursor: <opaque>        ← header; absent on the last page
[ {record}, {record}, ... ]    ← body is a top-level JSON array (the envelope)

429 Too Many Requests          Retry-After: <seconds>
5xx                            transient; safe to retry (every call is a GET)
```

| Resource | Fields (✱ = always present) |
|---|---|
| patient | `id`✱, `mrn`✱, `dob`✱ (`YYYY-MM-DD`), `name{family✱, given✱, middle}`, `sex`✱ (`male` / `female` / `other` / `unknown`), `contact{phone, email}`, `address`, `language` |
| appointment | `id`✱, `patient_id`✱, `start_time`✱ (ISO-8601, UTC), `status`✱ (`scheduled` / `completed` / `cancelled` / `no_show`), `provider_id`, `location`, `notes`, `reason` |
| report | `id`✱, `patient_id`✱, `type`✱, `status`✱ (`preliminary` / `final` / `amended` / `cancelled`), `body`✱, `units`✱, `author`, `issued_at`, `summary` |

**Practice in the path.** The mock follows Athena's style. The gateway's Acme adapter builds the path from `tenant.locator.practice_id`; nothing outside the adapter knows the practice id exists.

**Fixture size:** two practices (`practice_a`, `practice_b`), each with 25 patients, 25 appointments and 15 reports, served 10 per page. The two practices deliberately reuse some IDs (`P-1001`, `A-2001`), so the tenant-isolation test (§14) has real collisions to catch. Every scenario runs across several pages (so the checkpoint is exercised), and pages are big enough for the batch-health minimum sample (§3 ⑧).

### Table

```
 ┌─────────────┬────────────────┬──────────────────────────────────┬──────────────────────────────┐
 │ Scenario    │ Change type    │ What the vendor does             │ Gateway outcome              │
 ├─────────────┼────────────────┼──────────────────────────────────┼──────────────────────────────┤
 │ v1          │ baseline       │ documented contract              │ all COMPATIBLE, baseline     │
 │             │                │                                  │ fingerprint recorded         │
 │ additive    │ backward-compat│ adds `preferred_language`,       │ COMPATIBLE, drift_event,     │
 │             │                │ `portal_enabled`                 │ data written, no incident    │
 │ optional_   │ backward-compat│ drops `middle_name`              │ DEGRADED, written, warn      │
 │ missing     │                │                                  │ incident                     │
 │ breaking    │ schema break   │ `dob`→`birth_date`,              │ INCOMPATIBLE, quarantined,   │
 │             │                │ `mrn`→`identifiers[{system,val}]`│ breaker opens (>20%), 0 rows │
 │             │                │                                  │ → fix mapper v2 → replay ✓   │
 │ envelope    │ structural     │ list → {data:[], meta:{next}}    │ ENVELOPE_CHANGED: page       │
 │             │                │                                  │ quarantined, sync halts;     │
 │             │                │                                  │ page replay after fix        │
 │ report_     │ breaking +     │ `units` → `result.units`,        │ those reports quarantined    │
 │ change      │ behavioral     │ new status `corrected`           │ (MISSING_CRITICAL_FIELD,     │
 │             │                │                                  │ UNKNOWN_ENUM_VALUE);         │
 │             │                │                                  │ `final → amended` updates    │
 │             │                │                                  │ normally (version row)       │
 │ new_status  │ behavioral     │ 1 appt per page has status       │ that record quarantined,     │
 │             │                │ `checked_in_virtual`             │ others written; 1/10 is      │
 │             │                │                                  │ under 20%: breaker stays     │
 │             │                │                                  │ closed                       │
 │ rate_limit  │ behavioral     │ 429 + Retry-After: 1 every 3rd   │ waits, retries, completes,   │
 │             │                │ call                             │ no data loss                 │
 │ pagination  │ behavioral     │ `X-Next-Cursor` returns a cursor │ PAGINATION_LOOP incident,    │
 │             │                │ already served (loops back)      │ run halts, no duplicates     │
 │ malformed   │ malformed      │ every 3rd call: 200 + HTML       │ retried with backoff, then   │
 │             │                │ body; one array element is       │ succeeds; `null` element →   │
 │             │                │ `null`                           │ MALFORMED_RECORD, rest       │
 │             │                │                                  │ written. HTML on every try   │
 │             │                │                                  │ → request_dlq + incident     │
 │ flaky       │ transient      │ random 503 / response slower     │ backoff + retry, idempotent  │
 │             │                │ than the read timeout            │ on re-fetch, no duplicates   │
 └─────────────┴────────────────┴──────────────────────────────────┴──────────────────────────────┘
```

### How this maps to the assignment

The assignment requires **at least three** changes: one backward-compatible, one breaking schema, one behavioral. We cover each category more than once:

| Assignment requirement | Our scenarios |
|---|---|
| Backward-compatible change | `additive`, `optional_missing` |
| Breaking response-schema change | `breaking` (rename + nesting), `envelope` (structure), `report_change` (`units` nested) |
| Behavioral change | `new_status`, `report_change` (new report status), `rate_limit`, `pagination` |
| Temporary failures/timeouts | `flaky` |
| Malformed payloads (§1D) | `malformed` |

And the bullet list in section 1B of the assignment ("renamed or nested fields, missing optional fields, unexpected new fields, changed status values, altered response structures, temporary failures/timeouts/rate limits") is covered **one-for-one**:

| Assignment wording | Scenario |
|---|---|
| renamed fields | `breaking` (`dob → birth_date`) |
| nested fields | `breaking` (`mrn → identifiers[]`) |
| missing optional fields | `optional_missing` |
| unexpected new fields | `additive` |
| changed status values | `new_status`, `report_change` |
| altered response structures | `envelope` |
| temporary failures, timeouts | `flaky` |
| rate limits | `rate_limit` |
| malformed payloads (§1D) | `malformed` |

**Pagination loop detection:** if the vendor's `next` cursor returns a cursor we've already seen in this run, we'd loop forever. We track seen cursors per run and halt with an incident.

**Each scenario = at least one test.** Tests assert *outcomes*, not just "no crash": row counts, quarantine counts, incident codes, no duplicates, breaker state.

---

## 10. Production scale-out (described in the README, not built)

### Diagram

```
                                       ┌──────────────────────────┐
                                       │  Scheduler (cron/Temporal)│
                                       │  per tenant × vendor ×    │
                                       │  resource sync jobs       │
                                       └────────────┬─────────────┘
                                                    ▼
  ┌──────────┐ webhooks/HL7 ┌───────────────┐  ┌───────────────────────┐
  │ EMR A    │─────────────►│ Ingress API   │  │  FETCH WORKERS        │◄── per-tenant rate-limit
  │ (Epic)   │◄─────────────│ (push)        │  │  (adapter.client)     │    + breaker in Redis
  ├──────────┤    pull      └──────┬────────┘  └──────────┬────────────┘
  │ EMR B    │◄────────────────────┼──────────────────────┘
  │ (Athena) │                     │                      │
  ├──────────┤                     ▼                      ▼
  │ EMR C    │            ┌──────────────────────────────────────────┐
  │ (eCW)    │            │ RAW STORE: object storage (S3/GCS),      │
  └──────────┘            │ encrypted, immutable, retention policy   │
                          └───────────────────┬──────────────────────┘
                                              │ event: raw_payload_id
                                              ▼
                          ┌──────────────────────────────────────────┐
                          │ QUEUE  (RabbitMQ / Kafka / SQS)          │
                          │ partition key = tenant:patient           │ ← per-patient ordering
                          └───────┬───────────────────────┬──────────┘
                                  ▼                       ▼
                       ┌────────────────────┐   ┌────────────────────┐
                       │ PROCESS WORKERS ×N │   │ DLQ topic          │
                       │ map·classify·upsert│──►│ quarantine service │
                       │ (stateless, idemp.)│   │ + review UI        │
                       └─────────┬──────────┘   └─────────┬──────────┘
                                 ▼                        │ replay
                       ┌────────────────────┐             │
                       │ Postgres (canonical│◄────────────┘
                       │ + ledger + history)│
                       └─────────┬──────────┘
                                 │ CDC / outbox
                                 ▼
                       downstream consumers (clinical apps, analytics)

  Cross-cutting:  OpenTelemetry traces · Prometheus metrics (drift_rate, quarantine_depth,
                  sync_lag, retry_count, breaker_state) · Alertmanager → PagerDuty
                  · Vault/KMS secrets · PHI-redacting log processor · audit log
                  · nightly contract canaries against each vendor sandbox
```

### Walkthrough

**What changes from the assignment version:** the *same* pipeline steps, split across processes connected by a queue.

1. **Scheduler**: triggers syncs per **tenant × vendor × resource** (tenant = a clinic or customer with its own EMR instance and credentials).
2. **Fetch workers**: run the vendor *client* only. Rate-limit buckets and breaker state move to **Redis** so all workers share them (one tenant can't exceed its budget by running on 10 workers).
3. **Push ingress**: some EMRs push (webhooks, HL7 v2 over MLLP). They land in the **same raw store**, so everything after it is identical regardless of pull or push.
4. **Raw store → object storage**: S3/GCS is cheaper and more scalable than a DB for blobs; encrypted, immutable, with retention.
5. **Queue** carries `raw_payload_id` events. **Partition key = `tenant:patient`** means all updates for one patient go to one partition, processed **in order**. Otherwise an older update could overwrite a newer one. This is how we prevent the concurrent-writer race from §6 — we don't resolve it, we prevent it.
6. **Process workers**: map, classify, upsert. **Stateless and idempotent**, so you scale by adding workers, and a redelivered message is harmless.
7. **DLQ + quarantine service + review UI**: a human reviews incidents, approves alias suggestions, triggers replay.
8. **Postgres** for canonical data; **CDC/outbox** publishes "patient updated" events downstream reliably (the outbox pattern avoids "DB committed but event lost").

**Cross-cutting:**
- **OpenTelemetry**: trace one record from fetch → queue → worker → DB.
- **Prometheus metrics**: `drift_rate`, `quarantine_depth`, `sync_lag` (how stale our data is), `retry_count`, `breaker_state`. Alert on thresholds.
- **Contract canaries**: a nightly job hits each vendor's sandbox and runs contract checks, so drift is caught *before* production traffic hits it.

### Why: scaling to many vendors (README Q4)

- New vendor = new adapter package + contract spec + contract tests. **Core unchanged.**
- Declarative contracts (YAML: field paths, criticality, enums) mean simple mappings need little code.
- Per-tenant isolation (credentials, rate limits, breakers, checkpoints) means one bad tenant or vendor can't starve others (**bulkhead pattern**).
- Horizontal scaling comes from idempotency: any worker can process any message, any number of times.

---

## 11. Design decisions and trade-offs (summary)

```
 ┌────┬────────────────────────────┬──────────────────────────────┬─────────────────────────────────┐
 │ #  │ Decision                   │ Alternative rejected         │ Why                             │
 ├────┼────────────────────────────┼──────────────────────────────┼─────────────────────────────────┤
 │ D1 │ Store raw before parsing   │ Parse in-flight, log errors  │ Replay without the vendor;      │
 │    │                            │                              │ audit trail; forensic diffing   │
 │ D2 │ Field criticality tiers    │ Strict all-or-nothing schema │ Tolerate harmless drift without │
 │    │                            │ / fully lenient              │ risking clinical correctness    │
 │ D3 │ No auto-alias of renames   │ Fuzzy / heuristic remap      │ A wrong guess on dob or result  │
 │    │ (suggest only)             │                              │ is a patient-safety issue       │
 │ D4 │ Closed enum maps           │ Default unknown → "other"    │ Status drives clinical workflow;│
 │    │                            │                              │ a new value is a semantic change│
 │ D5 │ Record-level quarantine +  │ Fail entire batch always     │ Isolate bad records, but stop   │
 │    │ batch circuit breaker      │ / never stop                 │ fast on systemic contract break │
 │ D6 │ Natural key + content hash │ Insert-only / last-write-wins│ Duplicates become no-ops; real  │
 │    │ + version history          │                              │ changes are audited             │
 │ D7 │ Checkpoint after commit    │ Checkpoint before processing │ Crash → re-read page; idempotent│
 │    │                            │                              │ writes make that safe           │
 │ D8 │ Fingerprint drift detection│ Rely on validation errors    │ Catches silent changes on HTTP  │
 │    │ (observational)            │                              │ 200 that still validate         │
 │ D9 │ Full-jitter backoff,       │ Fixed delay retries          │ Respects vendor; avoids herd    │
 │    │ honour Retry-After         │                              │ effects across tenants          │
 │ D10│ Adapter Protocol + core    │ if vendor == ... branching   │ New vendor = new package +      │
 │    │ independent of vendor      │                              │ contract tests, core unchanged  │
 │ D11│ FHIR-shaped canonical      │ Vendor-shaped internal model │ Stable internal contract;       │
 │    │ subset                     │                              │ interoperable downstream        │
 │ D12│ Replay re-maps stored raw  │ Re-fetch from vendor         │ Vendor may have changed again / │
 │    │ (mapping_version tracked)  │                              │ data may no longer be served    │
 │ D13│ SQLite for assignment;     │ Postgres from day one        │ Zero-setup review; same SQL     │
 │    │ Postgres-ready via SQLAlch.│                              │ layer swaps by DSN              │
 │ D14│ tenant_id in all natural   │ Key on (vendor, vendor_id)   │ Vendor IDs repeat across        │
 │    │ keys and isolation         │ only                         │ practices (Athena); without it  │
 │    │                            │                              │ one practice overwrites another │
 │ D15│ Malformed body = transient │ Fail the run on the first    │ HTML/empty/truncated bodies     │
 │    │ (retry, keep bytes), then  │ unparseable body             │ are usually proxies or dropped  │
 │    │ request_dlq + incident     │                              │ connections; persistent ones    │
 │    │                            │                              │ still raise an incident         │
 │ D16│ Page-level quarantine for  │ Record-level quarantine only │ Envelope breaks happen before   │
 │    │ envelope changes           │                              │ records exist; page scope       │
 │    │                            │                              │ makes them replayable           │
 │ D17│ Batch threshold only on    │ Percentage on any page size  │ 1 bad record in a 4-record      │
 │    │ pages with ≥ 10 records    │                              │ page is 25%: would halt a       │
 │    │                            │                              │ resource for one bad value      │
 │ D18│ Fingerprint = type union,  │ Hash each page as-is         │ Nullable fields would flap;     │
 │    │ nulls ignored, baseline    │                              │ additive drift would alert      │
 │    │ promoted by a human        │                              │ on every sync forever           │
 │ D19│ Checkpoint cleared when a  │ Keep last cursor between     │ Cursors are only valid inside   │
 │    │ run completes; incremental │ runs                         │ one run; reuse can skip or      │
 │    │ via updated_since          │                              │ loop                            │
 │ D20│ Own tenant_id; vendor      │ Use the vendor's practice id │ Only Athena has a practice id;  │
 │    │ locator kept on `tenant`   │ as the key                   │ others use a URL, GUID or       │
 │    │                            │                              │ session; keys stay uniform      │
 │ D21│ Department / location is a │ Department in the natural key│ IDs are unique per tenant, not  │
 │    │ column, not part of the key│                              │ per department; adding it would │
 │    │                            │                              │ split one patient into rows     │
 └────┴────────────────────────────┴──────────────────────────────┴─────────────────────────────────┘
```

### Trade-offs we are *consciously* accepting

| Trade-off | Cost | Why acceptable |
|---|---|---|
| Quarantining renames instead of guessing | Data delayed until a human approves | Minutes or hours of delay vs. wrong clinical data. Clear choice in healthcare. |
| Storing all raw payloads | Storage cost + PHI exposure surface | Mitigated with encryption, retention, and access control; replay and audit value is high. |
| Breaker halts a whole resource | Good records in later pages also wait | Systemic breaks would otherwise flood quarantine; replay recovers everything after the fix. |
| Synchronous sync in the assignment | Doesn't scale horizontally | Simple to review; the production design (§10) shows the path. |

---

## 12. README questions: one-paragraph answers to memorize

**Q1. How would your solution detect a vendor change that does not produce an HTTP error?**
Every response is fingerprinted (sorted field paths + JSON types, hashed) and compared to a baseline per resource, so any shape change, even a harmless new field on a 200 OK, creates a drift event with an exact diff. On top of that, the envelope check, strict critical-field validation, closed enum maps, and the batch quarantine rate catch changes that are *semantically* breaking but still return 200. In production, nightly contract canaries against vendor sandboxes catch drift before live traffic does.

**Q2. How would you prevent incorrect patient or clinical data from being written?**
Field criticality tiers with strict (non-coercing) validation; no automatic mapping of renamed fields or unknown enum values; quarantine instead of guessing; malformed bodies and non-object elements never reach the mapper; referential checks within the same tenant so no orphan appointments or reports; natural-key identity with tenant_id (never demographic matching); and DB-level unique, foreign key and enum constraints as a final guard. Everything written is versioned and traceable to its raw payload and mapping version.

**Q3. How would you recover records that failed during an undocumented API change?**
Failed records are quarantined with their raw payload and full diagnostics, grouped into one incident. After the adapter is fixed and `mapping_version` bumped, `POST /replay` re-processes the stored raw payloads through the same classifier and idempotent writer, with no vendor call needed. Replay is safe to repeat, still-broken records stay quarantined, and the incident closes when its count reaches zero. If the envelope changed, the whole stored page is quarantined and replayed the same way. Requests that never returned a usable body sit in a request DLQ and are recovered by re-fetching from the saved checkpoint.

**Q4. How would you scale this approach to support multiple EMR vendors?**
Each vendor is an adapter implementing one Protocol with a declarative contract; the core pipeline never changes. Tenants are our own ids for vendor connections, and each adapter turns a tenant's locator into that vendor's addressing (Athena practice id in the path, Cerner tenant GUID or Epic per-organization base URL, NextGen enterprise/practice session), so keys and core logic look the same for every vendor. For throughput: split fetch and process via a queue partitioned by tenant and patient, stateless idempotent workers, shared rate-limit and breaker state in Redis, raw payloads in object storage, and per-tenant isolation (credentials, rate limits, breakers, checkpoints scoped by tenant_id) so one failing vendor or tenant can't affect others.

**Q5. What would you improve before deploying this into production?**
PHI controls (encryption at rest, retention, log redaction, access audit); real alerting (Prometheus + Alertmanager/PagerDuty) and tracing; contract canaries per vendor; a quarantine review UI with alias-suggestion approval; secrets management and OAuth token refresh; Postgres with migrations (Alembic); the queue-based worker architecture; load and chaos testing; and runbooks per incident type.

---

## 13. Proposed repository layout

```
sarmediq-emr-gateway/
├── README.md                    ← setup, demo, answers to the 5 questions
├── docs/
│   └── ARCHITECTURE.md          ← this file
├── docker-compose.yml           ← gateway + mock (+ optional postgres)
├── Makefile                     ← make install / test / demo / run
├── pyproject.toml
├── mock_emr/                    ← simulated vendor (separate FastAPI app)
│   ├── app.py
│   ├── scenarios.py
│   ├── fixtures/                ← fake patients/appointments/reports
│   └── CONTRACT.md              ← the "documented" v1 API contract
├── app/
│   ├── main.py
│   ├── config.py
│   ├── api/                     ← sync, ops, read routers
│   ├── sync/orchestrator.py
│   ├── core/                    ← canonical, classifier, drift, idempotency,
│   │                               quarantine, incidents, retry, breaker
│   ├── vendors/
│   │   ├── base.py              ← VendorAdapter Protocol
│   │   └── acme/                ← client, contract, mapper
│   ├── db/                      ← models, session
│   └── logging.py               ← structured JSON logs
├── tests/
│   ├── unit/                    ← classifier, mapper, drift, hashing, backoff
│   ├── integration/             ← one test per scenario against the live mock
│   └── conftest.py
└── scripts/demo.py              ← end-to-end scenario walkthrough
```

---

## 14. Testing strategy

Testing is 15% of the score, and "automated tests covering normal operation and API changes" is a named deliverable.

### How the tests run

```
 pytest session
   ├─ fixture: mock EMR started with uvicorn in a background thread on a free port
   │           → real HTTP, so timeouts, 429s and slow responses are real
   ├─ fixture: a fresh SQLite file per test (tmp_path) → no state leaks between tests
   ├─ fixture: fast settings → backoff base 0.01s, cap 0.05s, read timeout 1s,
   │           Retry-After cap 1s; the mock's "slow" response takes 2s
   │           → the whole suite runs in seconds
   └─ each test: set the mock scenario → run a sync (calling the orchestrator directly)
                 → assert on DB rows, the SyncReport, incidents and breaker state
```

**Why a real server and not httpx's in-process `ASGITransport`?** In-process is faster, but its timeouts aren't real network timeouts, so the `flaky` test wouldn't prove anything. Unit tests don't touch the network at all.

### Unit tests (pure functions, no I/O)

| Area | What we assert |
|---|---|
| classifier | each error code in §4 is produced by a minimal bad record; DEGRADED vs COMPATIBLE |
| mapper | v1 record → expected canonical; ISO date accepted, epoch number rejected; unknown enum → error, never mapped |
| fingerprint | a null doesn't change the hash; `identifiers[0]` and `identifiers[3]` hash the same; an added field changes it; key order doesn't |
| content hash | stable under key order; ignores `fetched_at` |
| backoff | delay always within `[0, min(cap, base × 2^n)]`; `Retry-After` parsed as seconds or HTTP-date, and capped |
| breaker | CLOSED → OPEN after N failures; OPEN rejects; reset → CLOSED |
| batch health | 1 bad of 4 does **not** trip (below the sample size); 3 bad of 10 trips |

### Integration tests (each scenario, plus recovery and integrity)

| Test | Asserts |
|---|---|
| v1 baseline | 25 / 25 / 15 rows per practice; nothing quarantined; baseline fingerprint stored |
| re-sync v1 | everything `unchanged`; row counts identical (no duplicates) |
| additive | rows written; exactly one drift_event; no incident; a second sync still leaves one drift_event |
| optional_missing | rows written with NULLs; DEGRADED count; warning incident |
| breaking | 0 patient rows written; breaker OPEN; one incident whose diff shows `dob` removed and `birth_date` added |
| breaking → fix → replay | switch the adapter to mapping_version 2 (alias added); `POST /replay`; all rows present with `mapping_version = 2`; incident CLOSED; replaying again changes nothing |
| envelope | page quarantined (`scope = PAGE`); 0 rows; ENVELOPE_CHANGED; page replay after the adapter fix writes the rows |
| report_change | affected reports quarantined with both codes; `final → amended` creates version 2 of the same row, not a new row |
| new_status | only the bad appointments quarantined; the rest written; breaker stays CLOSED |
| rate_limit | sync completes; all rows present; retry count > 0 in the report |
| pagination | PAGINATION_LOOP incident; run stops; no duplicates |
| malformed | occasional HTML → completes after retries; `null` element → MALFORMED_RECORD; HTML on every attempt → request_dlq + incident |
| flaky | completes despite 503s and a timed-out request; no duplicates |
| crash and resume | raise an exception after N records (monkeypatch); rerun resumes from the checkpoint; final counts exact |
| tenant isolation | two tenants both have `P-1001` → two patient rows, neither overwritten |
| pending dependency | appointments synced before patients → pending; after the patient sync they're written |

**The "fix" in the recovery test is a real code path, not a mock.** The Acme mapper picks its alias table by mapping version. The test runs version 1 (no alias, so records are quarantined), then version 2 (`birth_date` accepted as `dob`, the human-approved fix) against the same stored raw payloads. That's what happens in production after a deploy.

---

## 15. Build scope for the 4–6 hours

The assignment asks us to prioritise "the parts that best demonstrate production reliability" and document the trade-offs. The scoring weights set the order. Everything in the right-hand column is already designed above, so the README can say exactly what's missing and why.

```
 BUILD (in this order)                                  DOCUMENT ONLY
 ─────────────────────────────────────────────          ─────────────────────────────────────
  1. Mock EMR: v1 contract, fixtures, scenarios          · token-bucket throttle (§5)
  2. Canonical models + Acme adapter                     · alias-suggestion engine (§4)
     (client, contract, mapper, mapping_version)
  3. tenant, raw_payload, idempotent upsert,
     record_version, processing_ledger                   · second vendor adapter
  4. Classifier, quarantine (record + page),             · Postgres + Alembic migrations
     incidents, pending_record                           · queue, workers, Redis, object
  5. Drift fingerprint, drift_event, promote               storage (§10)
  6. Retries, backoff, Retry-After, malformed            · parent incidents across tenants
     bodies, request_dlq                                 · PHI controls, metrics, tracing
  7. Breaker (CLOSED / OPEN / HALF_OPEN probe, reset),
     batch health, checkpoint, pagination-loop check
  8. Replay (record + page)
  9. Ops + read endpoints, structured JSON logs
 10. Tests (§14) and the README with the five answers

 STRETCH, also built: docker-compose, HALF_OPEN probe
```

---

## 16. Implementation notes: where the code refines this design

Decisions made while building (2026-11-01). Where they differ from an earlier section, this section wins.

| # | Refinement | Why | Code |
|---|---|---|---|
| 1 | The adapter makes **one HTTP attempt**; the retry policy lives in the core (§2, §5 said "vendor client + retries"). | Raw-before-parse must hold on *every* attempt, including malformed bodies; retry rules are the same for every vendor. | `app/core/retry.py`, `app/vendors/acme/client.py` |
| 2 | New error codes `UPSTREAM_UNAVAILABLE`, `MISSING_IMPORTANT_FIELD`, `MISSING_OPTIONAL_FIELD`; incident severity comes from field criticality (critical / warning / info). | Every outcome needs a code; degraded writes still deserve a visible, lower-severity signal. | `app/core/errors.py` |
| 3 | **Absence is not deletion.** A field whose key the vendor stops sending keeps its stored value (§4 point 5 said "write null"). A key sent as `null` still clears it. | Overwriting middle names with null because the vendor regressed would be silent data loss. | `upsert_record(keep_existing=…)` |
| 4 | A drift is identified by **what changed** (the diff's hash), not the page's hash. The first shape seen becomes the baseline. | The same vendor change on many pages and tenants yields one event; data-dependent nulls don't create false drifts. | `app/core/drift.py` |
| 5 | Quarantine ↔ incident is **many-to-many** (`quarantine_incident`). | A record missing both `dob` and `mrn` belongs to two incidents; each closes when *its* records drain. | `app/db/models.py` |
| 6 | **One open quarantine row per broken record**, not per fetch; a later valid version resolves it ("superseded"). | Re-syncing a broken vendor must not multiply quarantine rows. | `app/core/quarantine.py` |
| 7 | `POST /sync/...?full=true` ignores `updated_since`. Default is incremental. | Operators (and the demo) need a forced full re-read. | `sync_router.py` |
| 8 | The run lock is `run_id` + `heartbeat_at` on `sync_checkpoint`, taken by compare-and-set; a lock older than the TTL (15 min) is taken over. Second caller gets `409`. | A crashed worker must not lock a stream forever. | `orchestrator._acquire_lock` |
| 9 | `raw_payload.body` is bytes, and stores 2xx bodies and non-retryable 4xx bodies (not 429/5xx bodies). | Malformed bodies aren't valid JSON; we keep what we act on as evidence. | `app/db/models.py` |
| 10 | Breaker: data-level failures (envelope, batch threshold, pagination loop) and `CONTRACT_OR_AUTH` trip immediately; upstream failures trip after 3 consecutive failed runs. Replay that drains the quarantine closes envelope/batch incidents and resets the breaker. After `EMR_BREAKER_COOLDOWN_SECONDS` (300) the next sync is a HALF_OPEN probe: completes → CLOSED, any failure → OPEN with a fresh cooldown. A blocked sync returns `503` with `Retry-After`. | Contract breaks need a human now; outages often heal by themselves. | `app/core/breaker.py`, `orchestrator.replay` |
| 11 | A fix is deployed as `EMR_ACME_MAPPING_VERSION=2`: v2 adds the approved aliases (`birth_date`, `identifiers[system=MRN]`) and the `{data, meta}` envelope. v1 behaviour is unchanged. | Shows recovery as a real, versioned code path. | `app/vendors/acme/mapper.py`, `adapter.py` |
| 12 | Pending records are retried after each patient sync and escalated to `ORPHAN_REFERENCE` after 5 attempts. | Timing problems resolve themselves; permanent ones become incidents. | `orchestrator._retry_pending` |
| 13 | Mock adds `malformed_always` (HTML on every call) to exercise the request DLQ. | The transient `malformed` scenario always recovers. | `mock_emr/scenarios.py` |
| 14 | SQLite runs with `synchronous=NORMAL` alongside WAL. | Safe with WAL and avoids an fsync per record commit. | `app/db/session.py` |
| 15 | Mock adds `orphan` (appointment A-2001 references a patient never returned) to exercise pending → `ORPHAN_REFERENCE` escalation. | The escalation path needs a real trigger to be tested. | `mock_emr/scenarios.py` |
| 16 | A run that **completes** closes the stream's open run-level incidents (`UPSTREAM_UNAVAILABLE`, `MALFORMED_RESPONSE`, `CONTRACT_OR_AUTH`, `PAGINATION_LOOP`, `ENVELOPE_CHANGED`, `BATCH_THRESHOLD`). Record-level incidents stay open until their quarantined records drain. | A finished run is proof the stream recovered; leaving those open would keep `/health` degraded forever. | `orchestrator._close_stream_incidents` |
| 17 | **One orchestrator per vendor.** `app/vendors/registry.py` maps a vendor name to an adapter factory; `Services.orchestrator_for(tenant)` picks the orchestrator by the tenant's `vendor`. Startup fails if a configured tenant's vendor has no adapter. `POST /replay` needs `tenant` or `vendor` when several vendors are loaded. | Adding a vendor touches the registry and its own package; the core and the API stay unchanged. | `app/vendors/registry.py`, `app/main.py` |
| 18 | Read API uses **keyset pagination** (`?after=<vendor_id>`, next cursor in `X-Next-Cursor`) instead of `OFFSET`, backed by a `(tenant_id, vendor_id)` index. | `OFFSET n` scans and discards n rows, and rows inserted between pages shift the results. | `app/api/read_router.py` |
| 19 | `GET /quarantine` loads incidents with `selectinload` (2 queries total instead of 1 + N). Indexes on every `status` column and on the hot lookups (`quarantine(tenant_id, resource, status)`, `quarantine(tenant_id, resource, vendor_record_id)`, `incident(tenant_id, resource, status)`, `pending_record(tenant_id, waiting_on_resource, status)`). | `/health` and the pipeline filter by status on every call; without indexes those are full table scans. | `app/db/models.py`, `ops_router.py` |
| 20 | **Fenced checkpoint writes.** Every cursor advance and the completion update run as `UPDATE sync_checkpoint ... WHERE run_id = <this run>`. If 0 rows match, another run took over the stale lock: this run rolls back and stops as HALTED (`run lock lost`), with no breaker change. | A run that stalled past the lock TTL must not overwrite the new owner's cursor or mark the stream successful. `run_id` acts as the fencing token. | `orchestrator._save_checkpoint` |
| 21 | **Unexpected errors are recorded.** Any exception other than the handled cases rolls back the run, opens an `INTERNAL_ERROR` incident, counts a breaker failure, releases the lock and is re-raised. A later completed run closes the incident. | A crash must be visible in `/incidents` and `/health`, and repeated crashes stop the stream like repeated upstream failures. | `orchestrator._record_internal_error` |
| 22 | `UNKNOWN_ENUM_VALUE` issues list the allowed values in `expected` (`one of: male, female, other, unknown`). | An operator can act on the incident without opening the contract source. | `vendors/acme/mapper.py` |
