# System design diagrams

Two views of the same design:

1. **[Current system](#1-current-system-what-this-repository-runs)**: what this repository actually runs.
2. **[One sync run](#2-one-sync-run-the-pipeline-inside-the-orchestrator)**: the pipeline inside the orchestrator, with every failure exit.
3. **[Production system](#3-production-system-how-it-scales-out)**: the same pipeline split into services for many vendors and tenants (described, not built).

The full reasoning behind each box is in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Current system (what this repository runs)

Two processes: the **gateway** and a **mock Acme EMR** that can be switched into undocumented-change scenarios.
Everything to the left of `app/vendors` is vendor-agnostic; only `app/vendors/acme/` knows Acme exists.

```mermaid
flowchart TB
    OP(["Operator, curl / cron / demo.py"])

    subgraph GW["Gateway process (app/)"]
        API["<b>HTTP API</b>, app/api<br/>sync_router: POST /sync/{resource}<br/>ops_router: /health /incidents /drift /quarantine /dlq /replay<br/>read_router: /patients /appointments /reports"]
        ORCH["<b>Orchestrator</b>, app/sync/orchestrator.py<br/>one per vendor, chosen by the tenant<br/>sync(): breaker -> run lock -> page loop -> checkpoint<br/>replay(): stored raw bytes -> current mapper"]
        CORE["<b>Core policy</b>, app/core, vendor-agnostic<br/>retry, breaker, drift, classifier<br/>idempotency, quarantine, incidents, pending"]
        PROTO{{"<b>VendorAdapter Protocol</b>, app/vendors/base.py<br/>fetch_page, parse_page, contract, map_record"}}
        ACME["<b>Acme adapter</b>, app/vendors/acme<br/>client: one HTTP GET per attempt<br/>contract: tiers + enum maps as data<br/>mapper: raw -> canonical (mapping_version)"]
    end

    DB[("<b>SQLite, WAL</b>, app/db<br/>canonical: patient, appointment, clinical_report, versions<br/>evidence: raw_payload, request_dlq<br/>triage: quarantine, incident, pending_record<br/>run state: tenant, checkpoint, breaker, drift")]

    MOCK["<b>Mock Acme EMR</b>, mock_emr, separate process<br/>GET /{practice_id}/{patients, appointments, reports}<br/>POST /_admin/scenario switches undocumented changes"]

    OP -->|HTTP| API
    API --> ORCH
    ORCH -->|decisions| CORE
    ORCH -->|vendor calls| PROTO
    PROTO -->|implemented by| ACME
    CORE -->|reads / writes| DB
    ACME -->|HTTP GET| MOCK
```

**How to read it**

| Box | Responsibility | Never does |
|---|---|---|
| `app/api` | Validate input, map outcomes to HTTP codes (404 unknown tenant, 409 run in progress, 503 + `Retry-After` breaker open) | Business logic |
| `orchestrator` | Runs one sync or replay end to end; owns the transaction and checkpoint boundaries | Know vendor field names |
| `app/core` | Policy: retry, drift, classify, write idempotently, quarantine, open incidents, trip breakers | Import anything from `acme/` |
| `app/vendors/acme` | Facts about Acme: URL shape, envelope, field paths, enum translations | Decide whether a record is written |
| SQLite | Everything durable, including the raw bytes, so replay never needs the vendor | |

---

## 2. One sync run (the pipeline inside the orchestrator)

Every way a run can stop is shown. **Nothing reaches the canonical tables without passing the classifier.**

```mermaid
flowchart TD
    START(["POST /sync/patient?tenant=practice_a"]) --> BRK{"Breaker allows a run?<br/>CLOSED, or OPEN past cooldown<br/>so HALF_OPEN probe"}
    BRK -->|no| BLOCKED["503 + Retry-After<br/>status BLOCKED"]
    BRK -->|yes| LOCK{"Run lock won?<br/>compare-and-set on sync_checkpoint<br/>stale after 900s"}
    LOCK -->|no| CONFLICT["409 already syncing"]
    LOCK -->|yes| CKPT["Load checkpoint<br/>cursor = resume point<br/>updated_since = last successful run start"]

    CKPT --> FETCH["fetch_with_retry<br/>adapter.fetch_page: one attempt<br/>retry 429 / 5xx / timeout / non-JSON 2xx"]
    FETCH -->|every 2xx body| RAW[("raw_payload<br/>stored BEFORE parsing")]
    FETCH -->|gave up| DLQ["request_dlq + incident<br/>breaker trips after 3 failed runs,<br/>4xx CONTRACT_OR_AUTH trips at once"]

    RAW --> ENV{"parse_page:<br/>envelope as contracted?"}
    ENV -->|no| PAGEQ["page quarantine + incident<br/>breaker OPEN, stop"]
    ENV -->|yes| DRIFTN["drift.observe_page<br/>new shape gives one drift_event<br/>observational, never blocks"]

    DRIFTN --> MAP["per record:<br/>adapter.map_record then classify"]
    MAP -->|"COMPATIBLE / DEGRADED"| UPSERT["idempotent upsert<br/>insert / update + version row / unchanged<br/>absent field keeps stored value"]
    MAP -->|INCOMPATIBLE| QUAR["quarantine record<br/>+ deduplicated incident"]
    MAP -->|PENDING_DEPENDENCY| PEND["park in pending_record<br/>until parent patient arrives"]

    UPSERT & QUAR & PEND --> BATCH{"Batch healthy?<br/>at least 10 records and<br/>over 20% bad means NO"}
    BATCH -->|no| TRIP["BATCH_THRESHOLD incident<br/>breaker OPEN, stop"]
    BATCH -->|yes| NEXT{"next_cursor?"}
    NEXT -->|seen before| LOOP["PAGINATION_LOOP incident<br/>breaker OPEN, stop"]
    NEXT -->|new cursor| SAVE["commit checkpoint cursor<br/>AFTER the page's writes"] --> FETCH
    NEXT -->|none| DONE["run COMPLETED<br/>breaker reset, stream incidents closed,<br/>retry pending children"]

    QUAR -.->|later: mapping v2 + POST /replay| REPLAYN["replay from stored raw bytes<br/>quarantine resolved, incident closed"]
```

---

## 3. Production system (how it scales out)

The **same pipeline steps**, split across services connected by a queue. Described in the README
(Q4, Q5) and ARCHITECTURE §10; not built in this repository.

```mermaid
flowchart TB
    SCHED["<b>Scheduler</b>, Temporal / cron<br/>jobs per tenant x vendor x resource<br/>+ nightly full reconciliation"]
    EMR["<b>EMR vendors</b><br/>Epic, athenahealth, Cerner, eCW, NextGen"]
    FW["<b>Fetch workers x N</b><br/>vendor adapter clients<br/>retry, backoff, Retry-After"]
    STATE[("<b>Redis + Vault</b><br/>rate limits, breaker state<br/>locks with fencing tokens, credentials")]
    PUSH["<b>Push ingress</b><br/>webhooks, HL7 v2 over MLLP"]
    OBJ[("<b>Object storage</b>, S3 / GCS<br/>raw payloads: encrypted, immutable, retention")]
    Q[["<b>Queue</b>, Kafka / RabbitMQ / SQS<br/>partition key = tenant:patient"]]
    PW["<b>Process workers x N</b><br/>parse, drift, map, classify, upsert<br/>stateless, idempotent"]
    PG[("<b>Postgres</b> + Alembic<br/>canonical, history, quarantine, incidents")]
    OUT["<b>Outbox / CDC</b><br/>-> clinical apps, analytics"]
    DLQ[["<b>Dead-letter queue</b>"]]
    OPS["<b>Ops API + review UI</b><br/>behind IdP + API gateway: OAuth2, scopes, tenant claim<br/>triage, approve mapping version, replay"]
    RW["<b>Replay workers</b><br/>stored raw + approved mapping"]

    SCHED --> FW
    FW <-->|pull pages| EMR
    FW -.- STATE
    EMR -->|push| PUSH
    FW -->|raw bytes first| OBJ
    PUSH --> OBJ
    OBJ -->|raw_payload_id| Q
    Q --> PW
    PW --> PG
    PG --> OUT
    PW -->|poison message| DLQ
    DLQ --> OPS
    OPS --> RW
    RW --> PG
```

**Cross-cutting, not drawn to keep the diagram readable:** OpenTelemetry traces from fetch to
database; Prometheus metrics (`sync_lag`, `quarantine_depth`, `drift_rate`, `breaker_state`,
`retry_count`) with Alertmanager to PagerDuty; an audit log and PHI-redacted logs; nightly contract
canaries against each vendor's sandbox, so drift is caught before production traffic sees it.

### What each current component becomes

| Current (this repo) | Production | Why it changes |
|---|---|---|
| `POST /sync` called by a person or cron | Scheduler (Temporal or cron) + nightly full reconciliation | Long syncs shouldn't hold an HTTP request; a scheduled full sync catches shape changes that never bump `updated_since` |
| `orchestrator.sync()` in one process | Fetch workers + queue + process workers | Fetching (network-bound) and processing (CPU/DB-bound) scale independently |
| `raw_payload` table (bytes in SQLite) | Object storage, encrypted, with a retention policy | Blobs are cheaper and safer outside the DB; PHI controls |
| Run lock on `sync_checkpoint` | Redis/DB lock **with fencing tokens** | A worker that wakes up after its lock expired must not advance the checkpoint |
| `circuit_state` table | Breaker state + rate-limit buckets in Redis | All workers for one tenant share one budget and one breaker |
| Optimistic lock on concurrent writes | Partition key `tenant:patient` | Prevent the race instead of resolving it: one patient's updates are processed in order |
| SQLite (WAL) | Postgres + Alembic | Concurrent writers, migrations, managed backups |
| `/replay` endpoint | Replay workers triggered from a review UI | Large replays run in the background, with an audit trail of who approved which mapping version |
| structlog JSON + `/health` | OpenTelemetry + Prometheus + Alertmanager | Alerts on quarantine depth, drift rate and sync lag instead of polling |
| No auth | IdP + API gateway: scopes (`sync:run`, `ops:replay`, `phi:read`) and a tenant claim | Least privilege; a caller only sees its own tenants |
| `ADAPTER_FACTORIES = {"acme": ...}` | One adapter package per vendor, plus contract canaries | New vendor = new adapter; the core and pipeline don't change |

**What does not change:** the pipeline order (raw before parse, classify before write, checkpoint after commit), the natural key `(vendor, tenant_id, vendor_id)`, the criticality tiers, and the rule that nothing is guessed. Production adds scale and operations around the same correctness core.
