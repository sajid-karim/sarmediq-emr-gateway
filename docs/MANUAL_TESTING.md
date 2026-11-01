# Manual testing walkthrough

Every undocumented-change scenario, driven by hand with `curl` against real servers.
The comment after each command is the result observed on a clean run (2026-11-01).
Run the steps **in order**: later steps build on data from earlier ones.

The automated tests cover the same ground (`make test`); `make demo` runs a shorter version of
this story in one command.

## 0. Start the servers

Use any free ports. The examples use 8200 (gateway) and 9200 (mock vendor).

```bash
# Terminal 1: the fake EMR vendor
MOCK_PORT=9200 make mock

# Terminal 2: the gateway (delete the old database for a clean run)
rm -f gateway.db*
MOCK_PORT=9200 GATEWAY_PORT=8200 make run
```

In a third terminal, define three shortcuts. They only last for that terminal session.

```bash
GW=localhost:8200; MOCK=localhost:9200
scenario() { curl -s -X POST $MOCK/_admin/scenario -H 'content-type: application/json' -d "{\"scenario\":\"$1\"}"; echo; }
pp() { python3 -m json.tool; }
```

- `GW` and `MOCK` are the two base URLs.
- `scenario <name>` tells the mock vendor to start behaving differently (an undocumented change).
- `pp` pretty-prints JSON: `curl ... | pp`.

Every sync returns a **SyncReport**: `status` (`completed`, `halted`, `blocked`), `halt_reason`,
and counts (`fetched`, `inserted`, `updated`, `unchanged`, `degraded`, `pending`, `quarantined`,
`retries`, `drift_events`).

---

## 1. Baseline: the documented v1 contract

```bash
scenario v1
curl -s -X POST "$GW/sync/patient?tenant=practice_a" | pp          # completed, inserted 25
curl -s -X POST "$GW/sync/appointment?tenant=practice_a" | pp      # completed, inserted 25
curl -s -X POST "$GW/sync/clinical_report?tenant=practice_a" | pp  # completed, inserted 15
curl -s "$GW/patients?tenant=practice_a&limit=2" | pp              # P-1001 / MRN-11001, P-1002 / MRN-11002
```

The first patient page ever seen becomes the schema **baseline** used for drift detection.

## 2. Idempotency: no duplicates

```bash
curl -s -X POST "$GW/sync/patient?tenant=practice_a&full=true" | pp   # fetched 25, unchanged 25
curl -s -X POST "$GW/sync/patient?tenant=practice_a" | pp             # fetched 0 (incremental: nothing changed)
```

`full=true` re-reads everything; without it the gateway asks only for records changed since the
last successful run (`updated_since`).

## 3. Tenant isolation: the same IDs in two practices

```bash
curl -s -X POST "$GW/sync/patient?tenant=practice_b" | pp   # completed, inserted 25
curl -s "$GW/patients?tenant=practice_a&limit=1" | pp       # P-1001, MRN-11001, John Smith
curl -s "$GW/patients?tenant=practice_b&limit=1" | pp       # P-1001, MRN-21001, Maria Haddad
```

Same vendor ID, different people, two rows. The natural key is `(vendor, tenant_id, vendor_id)`.

## 4. Backward-compatible change: new fields

```bash
scenario additive
curl -s -X POST "$GW/sync/patient?tenant=practice_a&full=true" | pp   # completed, unchanged 25, drift_events [1]
curl -s "$GW/drift" | pp        # added: portal_enabled, preferred_language
curl -s "$GW/incidents" | pp    # []  (harmless: reported as drift, no alert)
```

## 5. Missing optional field

```bash
scenario optional_missing
curl -s -X POST "$GW/sync/patient?tenant=practice_a&full=true" | pp   # completed, degraded 25, unchanged 25
curl -s "$GW/incidents" | pp    # MISSING_OPTIONAL_FIELD, name.middle, severity info, count 25
```

`unchanged 25`: the vendor *stopped sending* `name.middle`, so the stored middle names are kept.
Absence is not deletion.

## 6. Behavioural change: unknown appointment status

```bash
scenario new_status
curl -s -X POST "$GW/sync/appointment?tenant=practice_a&full=true" | pp   # completed, unchanged 22, quarantined 3
curl -s "$GW/quarantine?resource=appointment" | pp                       # A-2005, A-2015, A-2025: UNKNOWN_ENUM_VALUE
```

`checked_in_virtual` is never mapped to the "closest" status. Only those 3 records are held; 1 bad
record in a page of 10 is data, not a contract break, so the breaker stays closed.

## 7. Clinical report changes

```bash
scenario report_change
curl -s -X POST "$GW/sync/clinical_report?tenant=practice_a&full=true" | pp   # completed, updated 1, unchanged 12, quarantined 2
curl -s "$GW/quarantine?resource=clinical_report" | pp                       # R-3002 MISSING_CRITICAL_FIELD (units), R-3003 UNKNOWN_ENUM_VALUE
```

- R-3001 went `final → amended`: a known status, so it is a normal update (version 2 + diff).
- R-3002's `units` moved to `result.units`: units are critical, so it is quarantined.
- R-3003's status `corrected` is unknown: quarantined.

## 8. Breaking change: `dob → birth_date`, `mrn → identifiers[]`

```bash
scenario breaking
curl -s -X POST "$GW/sync/patient?tenant=practice_b&full=true" | pp   # halted, BATCH_THRESHOLD, quarantined 10
curl -s -i -X POST "$GW/sync/patient?tenant=practice_b" | head -5    # HTTP 503, retry-after: 300 (breaker OPEN)
curl -s "$GW/health" | pp                                            # status degraded, practice_b/patient breaker open
curl -s "$GW/incidents" | pp                                         # MISSING_CRITICAL_FIELD dob (count 10), mrn (count 10), BATCH_THRESHOLD
curl -s "$GW/quarantine?tenant=practice_b&resource=patient" | pp     # 10 rows; fingerprint_diff: removed dob, mrn; added birth_date, identifiers...
curl -s -X POST "$GW/replay?resource=patient" | pp                   # replayed 10, resolved 0, still_quarantined 10
```

- practice_b's patients were stored in step 3. Nothing was overwritten with guessed data.
- 10 out of 10 bad records is a contract change, so the run stops after one page and the breaker
  opens (per practice and resource; practice_a is unaffected).
- Ten broken records produce one incident per field, not ten alerts.
- Replay with the *old* mapper changes nothing: the gateway never guesses.

## 9. Ship the fix and replay

In terminal 2, stop the gateway (Ctrl+C) and restart it with mapping version 2, which adds the
human-approved aliases (`birth_date → dob`, `identifiers[system=MRN] → mrn`). Keep the database.

```bash
EMR_ACME_MAPPING_VERSION=2 MOCK_PORT=9200 GATEWAY_PORT=8200 make run
```

Then, in terminal 3:

```bash
curl -s -X POST "$GW/replay?resource=patient" | pp                    # replayed 10, resolved 10, breakers_reset [practice_b/patient]
curl -s -X POST "$GW/sync/patient?tenant=practice_b&full=true" | pp   # completed, unchanged 25 (vendor still sends the new shape)
curl -s "$GW/health" | pp                                            # practice_b/patient breaker closed
```

Replay re-processed the **stored** raw payloads: no call to the vendor. The incidents for `dob`,
`mrn` and the batch threshold closed. `/health` is still `degraded` because the appointment and
report records from steps 6–7 are still quarantined.

## 10. Envelope change: `[...]` becomes `{"data": [...], "meta": {...}}`

```bash
scenario envelope
curl -s -X POST "$GW/sync/appointment?tenant=practice_b&full=true" | pp   # completed, inserted 25
```

Mapping v2 also understands the new envelope. With v1 this sync would halt with
`ENVELOPE_CHANGED` and quarantine the whole page (covered by `test_envelope_*` tests).

## 11. Transport problems

```bash
scenario rate_limit
curl -s -X POST "$GW/sync/patient?tenant=practice_b&full=true" | pp       # completed, retries 1 (the 3rd call got 429 + Retry-After)

scenario malformed
curl -s -X POST "$GW/sync/patient?tenant=practice_a&full=true" | pp       # completed, unchanged 24, quarantined 1, retries 1
#   one page came back as an HTML error page with 200 OK: stored, retried, recovered
#   one array element was null: quarantined as MALFORMED_RECORD

scenario malformed_always
curl -s -X POST "$GW/sync/appointment?tenant=practice_a&full=true" | pp   # halted, MALFORMED_RESPONSE, retries 4
curl -s "$GW/dlq" | pp                                                   # appointment, MALFORMED_RESPONSE, attempts 5, status open

scenario pagination
curl -s -X POST "$GW/sync/clinical_report?tenant=practice_a&full=true" | pp   # halted, PAGINATION_LOOP
```

A real timeout (the mock answers after 12 s; the gateway's read timeout is 10 s). Takes ~11 s:

```bash
curl -s -X POST $MOCK/_admin/scenario -H 'content-type: application/json' -d '{"scenario":"flaky","slow_seconds":12}'; echo
curl -s -X POST "$GW/sync/clinical_report?tenant=practice_b&full=true" | pp   # completed, unchanged 15, retries 2 (a 503, then a timeout)
```

## 12. Recovery and operator controls

```bash
scenario v1
curl -s -X POST "$GW/breaker/reset?tenant=practice_a&resource=clinical_report" | pp   # state closed (it opened on the pagination loop)
curl -s -X POST "$GW/sync/clinical_report?tenant=practice_a&full=true" | pp          # completed, unchanged 15
curl -s -X POST "$GW/sync/appointment?tenant=practice_a&full=true" | pp              # completed, unchanged 25
curl -s -X POST "$GW/drift/1/promote" | pp                                          # status acked: new shape accepted as baseline
curl -s "$GW/incidents?status=all" | pp
curl -s "$GW/health" | pp                                                           # quarantined_records 1
```

What the last two calls show:

- The appointment sync completed, which closed the `MALFORMED_RESPONSE` incident and marked the
  request DLQ row `refetched`. Valid versions of A-2005/A-2015/A-2025 and R-3002/R-3003 arrived,
  so their quarantine rows were resolved as *superseded* and their incidents closed.
- Still open: the `null` element from the `malformed` scenario (it has no vendor ID, so nothing can
  ever supersede it; a human decides) and the info-level `MISSING_OPTIONAL_FIELD` incident.

Instead of waiting for a manual reset, an open breaker also lets one **probe** run through after
the cooldown (`EMR_BREAKER_COOLDOWN_SECONDS`, default 300): if it completes the breaker closes,
if it fails it re-opens.

---

## Handy extras

```bash
curl -s "$GW/quarantine?status=resolved" | pp       # what was resolved, and how (resolution_note)
curl -s "$GW/incidents?status=closed" | pp
curl -s $MOCK/_admin/scenario                       # which change the vendor is currently simulating
sqlite3 gateway.db 'select id, resource, http_status, length(body) from raw_payload'   # the stored evidence
```

Interactive API docs: `http://localhost:8200/docs`.
