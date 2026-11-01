# Resilient EMR Integration Gateway

A FastAPI service that pulls patients, appointments and clinical reports from a (simulated) EMR
vendor and normalises them into an internal FHIR-shaped model. It is built to **detect, tolerate
and recover from undocumented vendor API changes** without silently losing data or writing
incorrect clinical records.

The one rule behind every decision: **nothing we don't understand reaches the clinical record.**
Harmless changes are accepted (and reported); dangerous ones are quarantined with full evidence
and can be replayed after a fix.

- Architecture and every design decision, with diagrams: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- The vendor's documented contract and the undocumented changes it can switch on: [`mock_emr/CONTRACT.md`](mock_emr/CONTRACT.md)
- Step-by-step `curl` walkthrough of every scenario, with expected results: [`docs/MANUAL_TESTING.md`](docs/MANUAL_TESTING.md)

## Quick start

Requires Python 3.12. No paid services, no external network.

```bash
make install     # venv + dependencies
make test        # 107 unit + integration tests (~35 s; integration tests start the mock on a real port)
make demo        # the whole story in one command: baseline -> changes -> breaking change -> fix -> replay
```

### Run it by hand

```bash
make mock                      # mock vendor on :9000   (MOCK_PORT=9100 make mock to change)
make run                       # gateway on :8000       (GATEWAY_PORT=8100 MOCK_PORT=9100 make run)
```

Or with Docker: `make up` (gateway on `:8000`, mock on `:9000`; override with `GATEWAY_PORT` /
`MOCK_PORT`), `make down` to stop.

If 8000 or 9000 is already in use, pick other ports, e.g.
`MOCK_PORT=9200 make mock` and `MOCK_PORT=9200 GATEWAY_PORT=8200 make run`.
The full walkthrough of every scenario is in [`docs/MANUAL_TESTING.md`](docs/MANUAL_TESTING.md).

A breaking change, end to end:

```bash
curl -X POST 'localhost:8000/sync/patient?tenant=practice_a'           # baseline: 25 inserted
curl -X POST localhost:9000/_admin/scenario -H 'content-type: application/json' \
     -d '{"scenario": "breaking"}'                                    # vendor renames dob, nests mrn
curl -X POST 'localhost:8000/sync/patient?tenant=practice_b'           # halted: 10 quarantined, 0 written
curl localhost:8000/health                                            # degraded, breaker OPEN
curl localhost:8000/incidents                                         # one incident per field, count=10
curl 'localhost:8000/quarantine?tenant=practice_b'                    # diagnostics + diff vs baseline

# Ship the fix (mapping v2 adds the human-approved aliases) and restart the gateway:
EMR_ACME_MAPPING_VERSION=2 make run
curl -X POST 'localhost:8000/replay?resource=patient'                  # re-maps stored raw, no vendor call
curl -X POST 'localhost:8000/sync/patient?tenant=practice_b'           # completes on the fixed mapper
```

Configuration is environment variables prefixed `EMR_` (see [`app/config.py`](app/config.py)),
e.g. `EMR_DATABASE_URL`, `EMR_VENDOR_BASE_URL`, `EMR_ACME_MAPPING_VERSION`, `EMR_MAX_ATTEMPTS`.

## API

| Method & path | Purpose |
|---|---|
| `POST /sync/{patient\|appointment\|clinical_report}?tenant=&full=` | Run one sync. `200` with a SyncReport; `503` + `Retry-After` if the breaker is open; `409` if already running |
| `GET /health` | Breakers, open incidents by severity, quarantine / pending / DLQ depth, new drift events |
| `GET /incidents?status=open\|closed\|all` | Deduplicated incidents |
| `GET /drift`, `POST /drift/{id}/promote` | Detected schema changes; promote a reviewed shape to baseline |
| `GET /quarantine?status=&resource=&tenant=` | Refused records/pages with diagnostics |
| `GET /dlq` | Requests that never returned a usable page |
| `POST /replay?scope=record\|page&tenant=&resource=&error_code=` | Re-process quarantined data from stored raw payloads |
| `POST /breaker/reset?tenant=&resource=` | Manual breaker reset |
| `GET /patients`, `/appointments`, `/reports` (`?tenant=`), `GET /…/{id}` | Read the canonical data back |

## How it works (short version)

```
POST /sync/{resource}
  breaker open? -> 503
  acquire run lock (409 if another run holds it), load checkpoint
  per page:
    ① fetch (one attempt per call; core retry policy: backoff + jitter, Retry-After, timeouts)
    ② store the raw bytes BEFORE parsing                      -> replay never needs the vendor
    ③ envelope check (array + X-Next-Cursor)                   -> page quarantine, breaker OPEN
    ④ drift fingerprint vs baseline (observational only)       -> one drift_event per new shape
    ⑤ per record: map -> classify
         COMPATIBLE / DEGRADED  -> ⑥ idempotent upsert (natural key + content hash + version history)
         INCOMPATIBLE           -> ⑦ quarantine + deduplicated incident
         PENDING_DEPENDENCY     -> park until the parent arrives
    ⑧ batch health: ≥10 records and >20% bad                  -> breaker OPEN, stop
    ⑨ commit checkpoint (only after the page's writes)
```

| Layer | Where | Knows about Acme? |
|---|---|---|
| HTTP API | `app/api/` | no |
| Orchestration | `app/sync/orchestrator.py` | no |
| Core (classify, drift, retry, idempotency, quarantine, incidents, breaker) | `app/core/` | no |
| Vendor adapter (client, contract-as-data, mapper) | `app/vendors/acme/` | **only here** |
| Persistence (SQLAlchemy 2, SQLite; Postgres by DSN) | `app/db/` | no |

Key decisions (full reasoning in the architecture doc):

- **Field criticality tiers.** A missing/invalid *critical* field (identity, DOB, status, units,
  result) quarantines the record; a missing *important/optional* field writes it as DEGRADED with
  a warning. Absence is not deletion: a field the vendor stops sending keeps its stored value.
- **No guessing.** No type coercion (an epoch number is not a date), closed enum maps (an unknown
  status is never mapped to the nearest one), renames are never auto-followed. A fix is a new
  `mapping_version` a human approved; every row records which version produced it.
- **Tenant isolation.** Natural key is `(vendor, tenant_id, vendor_id)` because EMR IDs repeat
  across practices (an Athena lesson). `tenant_id` is our own id; each adapter turns the tenant's
  `locator` into that vendor's addressing (practice id in the path here).
- **Effectively-once.** At-least-once fetching + idempotent writes + DB unique constraints.
  Checkpoint after commit, so a crash re-reads a page instead of skipping one.
- **Fail loud, not noisy.** Incidents are deduplicated per (tenant, resource, code, field); the
  breaker trips on HTTP failures *and* on data failures (a 200 full of garbage is still broken).

## Undocumented changes simulated

| Scenario | Category | Gateway outcome | Tests |
|---|---|---|---|
| `additive` | backward-compatible | written; one drift event; no incident | `test_additive_fields_are_accepted_and_reported_once` |
| `optional_missing` | backward-compatible | DEGRADED, info incident, stored values kept | `test_missing_optional_field_degrades_but_keeps_stored_value` |
| `breaking` (`dob→birth_date`, `mrn→identifiers[]`) | breaking schema | quarantined, breaker OPEN; fixed by v2 + replay | `test_breaking_change_…`, `test_replay.py` |
| `envelope` (`[...]` → `{data, meta}`) | breaking structure | page quarantined, breaker OPEN; page replay | `test_envelope_change_…`, `test_envelope_page_replay` |
| `report_change` | breaking + behavioural | units-moved and unknown-status reports quarantined; `final→amended` versioned | `test_report_change` |
| `new_status` | behavioural | only those appointments quarantined; others written | `test_unknown_status_quarantines_only_those_records` |
| `rate_limit` | behavioural | honours `Retry-After`, completes | `test_rate_limit_…` |
| `pagination` (cursor loops) | behavioural | loop detected, run stops, no duplicates | `test_pagination_loop_is_detected` |
| `malformed` / `malformed_always` | malformed payloads | HTML body kept + retried; `null` element quarantined; persistent → request DLQ | `test_resilience.py` |
| `flaky` (503 + slow) | transient | real timeout + retries, no duplicates | `test_flaky_…` |
| `orphan` (appointment → unknown patient) | referential | waits as pending; escalated to `ORPHAN_REFERENCE` quarantine after 5 patient syncs | `test_child_whose_parent_never_arrives_…` |

Also tested: crash mid-run and resume from the checkpoint, tenant isolation, child-before-parent
(pending then resolved), run lock and stale-lock takeover, replay idempotency, breaker HALF_OPEN
probe (success and failure), HTTP API end to end.

## README questions

**1. How would your solution detect a vendor change that does not produce an HTTP error?**
Every page's shape (field paths → JSON types, with nulls and array indexes normalised) is compared
with a baseline per vendor and resource; any difference becomes a drift event with an exact
added/removed/changed diff, even when the change is harmless and the response was `200 OK`. On top
of that, the envelope check, strict critical-field validation without coercion, closed enum maps
and the per-page quarantine rate catch changes that are semantically breaking but still return
`200`. The breaker trips on those data failures, not only on HTTP errors. In production, nightly
contract checks against each vendor's sandbox would catch drift before live traffic does.

**2. How would you prevent incorrect patient or clinical data from being written?**
Field criticality tiers with strict validation; no automatic mapping of renamed fields, unknown
enum values or re-typed values; malformed bodies and non-object elements never reach the mapper;
quarantine instead of guessing. References are resolved within the same tenant, so an appointment
never attaches to another practice's patient, and children whose parent is missing wait instead of
becoming orphans. Identity is the vendor's ID scoped by tenant, never name + DOB matching, and DB
unique and foreign-key constraints are the last guard. Every write is versioned and traceable to
its raw payload and mapping version.

**3. How would you recover records that failed during an undocumented API change?**
Every response is stored before parsing, and every refused record is quarantined with its
diagnostics and a diff against the baseline, grouped into one incident per problem. An engineer
ships a mapping fix (a new `mapping_version`, human-approved), then `POST /replay` re-processes the
stored payloads through the same classifier and idempotent writer, without calling the vendor.
Replay is safe to repeat; anything still broken stays quarantined; incidents close and the breaker
resets when the quarantine drains; the next sync resumes from the checkpoint. Envelope changes are
replayed at page level, and requests that never produced a usable body sit in a request DLQ and
are recovered by re-fetching.

**4. How would you scale this approach to support multiple EMR vendors?**
Each vendor is an adapter behind one `VendorAdapter` protocol (fetch one page, parse the envelope,
map a record, declare the contract as data); the core never branches on vendor. Tenants are our own
ids for vendor connections, and the adapter translates a tenant's locator into that vendor's
addressing (Athena practice id in the path, Cerner tenant GUID or Epic per-organisation base URL,
NextGen enterprise/practice session), so keys and core logic are identical for every vendor. For
throughput: split fetching and processing with a queue partitioned by tenant and patient, run
stateless idempotent workers, share rate limits and breaker state in Redis, keep raw payloads in
encrypted object storage, and isolate everything per tenant so one failing vendor or practice can't
starve the others. (Architecture doc §10.)

**5. What would you improve before deploying this into production?**
PHI controls (encryption at rest for raw payloads, retention, log redaction, access audit);
Postgres with Alembic migrations; the queue/worker split above with shared rate limiting and breaker state
in Redis; real alerting (metrics such as quarantine depth, drift rate and sync lag
into Prometheus/Alertmanager) and tracing; secrets management and OAuth token refresh; a review UI
for quarantine with alias-suggestion approval; a parent incident that rolls up per-tenant incidents
for vendor-wide breaks; deletion handling; per-vendor contract canaries; load and chaos testing;
and runbooks per incident type.

## Trade-offs and what is not built

Built for clarity within the time box; designed for the rest (architecture doc §10, §15, §16):

- **Synchronous, single process.** One sync is inherently sequential (each cursor comes from the
  previous page); throughput in production comes from more workers, not async. `def` endpoints run
  in FastAPI's threadpool, so a running sync doesn't block other requests.
- **SQLite** (WAL mode) for zero-setup review; the SQLAlchemy layer swaps to Postgres by DSN.
- **Breaker recovery.** After a cooldown (`EMR_BREAKER_COOLDOWN_SECONDS`, default 300) the next
  sync runs as a HALF_OPEN probe: success closes the breaker, any failure re-opens it. A blocked
  sync returns `503` with `Retry-After`. A successful replay or a manual reset also closes it.
- **The first shape ever seen becomes the drift baseline.** Production would seed it from the
  documented contract.
- Not built: token-bucket throttling, alias-suggestion engine, second vendor adapter, deletions,
  queue/workers/Redis/object storage, metrics, PHI controls.
