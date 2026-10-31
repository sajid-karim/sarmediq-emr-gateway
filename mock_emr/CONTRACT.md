# Acme EMR API — documented contract (v1)

This is what the vendor told us. Anything that deviates from it is an undocumented change.

## Endpoints

```
GET /{practice_id}/patients
GET /{practice_id}/appointments
GET /{practice_id}/reports

Query:  limit=<1..100, default 10>  cursor=<opaque>  updated_since=<ISO-8601>
```

`practice_id` identifies the clinic. Record IDs are unique **within a practice only**.

## Response

```
200 OK
Content-Type: application/json
X-Next-Cursor: <opaque>        ← absent on the last page
[ {record}, {record}, ... ]    ← top-level JSON array
```

| Status | Meaning |
|---|---|
| 200 | Success |
| 400 | Invalid cursor |
| 404 | Unknown practice or resource |
| 429 | Rate limited; honour `Retry-After` (seconds) |
| 5xx | Transient; safe to retry (all calls are GETs) |

## Records (✱ = always present and non-null)

**patient**

| Field | Type |
|---|---|
| `id` ✱ | string |
| `mrn` ✱ | string |
| `dob` ✱ | string, `YYYY-MM-DD` |
| `name.family` ✱, `name.given` ✱ | string |
| `name.middle` | string or null |
| `sex` ✱ | `male` / `female` / `other` / `unknown` |
| `contact.phone`, `contact.email` | string or null |
| `address`, `language` | string or null |

**appointment**

| Field | Type |
|---|---|
| `id` ✱, `patient_id` ✱ | string |
| `start_time` ✱ | ISO-8601 datetime with offset (UTC) |
| `status` ✱ | `scheduled` / `completed` / `cancelled` / `no_show` |
| `provider_id`, `location`, `notes`, `reason` | string or null |

**report**

| Field | Type |
|---|---|
| `id` ✱, `patient_id` ✱, `type` ✱, `body` ✱, `units` ✱ | string |
| `status` ✱ | `preliminary` / `final` / `amended` / `cancelled` |
| `author`, `summary` | string or null |
| `issued_at` | ISO-8601 datetime with offset, or null |

## Mock-only admin API

Not part of the vendor contract; used by tests and the demo to switch on undocumented changes.

```
POST /_admin/scenario   {"scenario": "<name>", "slow_seconds": 2.0}
GET  /_admin/scenario
```

| Scenario | Undocumented change |
|---|---|
| `v1` | none (the contract above) |
| `additive` | patients gain `preferred_language`, `portal_enabled` |
| `optional_missing` | patients stop sending `name.middle` |
| `breaking` | `dob` → `birth_date`; `mrn` → `identifiers[{system: "MRN", value}]` |
| `envelope` | body becomes `{"data": [...], "meta": {"next_cursor": ...}}`, no header |
| `report_change` | R-3001 becomes `amended`; R-3002 `units` moves to `result.units`; R-3003 status `corrected` |
| `new_status` | every 10th appointment (index 4, 14, 24) gets status `checked_in_virtual` |
| `rate_limit` | every 3rd call → `429 Retry-After: 1` |
| `pagination` | `X-Next-Cursor` repeats the cursor just used (loops) |
| `malformed` | every 3rd call → `200` with an HTML body; patient index 3 is `null` |
| `malformed_always` | every call → `200` with an HTML body |
| `flaky` | calls 1, 5, 9… → `503`; calls 2, 6, 10… respond after `slow_seconds` |
| `orphan` | appointment A-2001 references patient `P-9999`, which is never returned |

Fixtures: two practices (`practice_a`, `practice_b`) with 25 patients, 25 appointments and
15 reports each, sharing ID ranges. All fixtures are "last modified" on 2026-10-01, so an
`updated_since` at or after that date returns an empty list.
