# Code Review & Defect Catalog

### Defect Log for Starter `app/main.py`

| # | Where (function / line) | What was wrong | How noticed (test / symptom) | How fixed |
|---|---|---|---|---|
| 1 | `dt_to_ms` | Naive UTC datetimes were localized against system local time instead of UTC, skewing epoch ms | Epoch values were offset by local timezone hours (+5:30) | Ensured `dt.tzinfo` is forced to `timezone.utc` if naive before calling `.timestamp()` |
| 2 | `round_half_up` | Used Python's built-in `round()`, which applies banker's rounding (round to nearest even) | Numbers like 2.5 rounded down to 2 instead of 3 | Implemented standard arithmetic half-up: `math.floor(val * 10**decimals + 0.5) / 10**decimals` |
| 3 | `is_overnight` | Compared hours only (`eh <= sh`), ignoring shift minutes (e.g. 09:30 vs 09:15) | Failed detection when start and end hour matched but end minute was earlier | Compared total minutes from midnight: `(eh*60 + em) <= (sh*60 + sm)` |
| 4 | `compute_attendance_date` | Did not attribute early morning punches on overnight shifts to previous day | Punch-in at 05:00 for a 22:00-06:00 shift was assigned to current day rather than previous day shift | If overnight shift and IST punch time < `shift_end`, subtracted 1 day per R1 |
| 5 | `compute_late_minutes` | Counted minutes from grace end rather than shift start, and used `>=` instead of `>` for grace | A punch-in at 09:40:01 got 0 or 1 minute instead of 10 minutes | Enforced strict grace boundary (`> 10.0` min); computed `late_minutes = floor(elapsed_from_shift_start)` |
| 6 | `compute_overtime` | Did not shift `shift_end` to the next calendar day for overnight shifts | Computed negative or multi-hour erroneous overtime | Shifted `shift_end` by `+1 day` for overnight shifts; floor minutes; only `>= 30` counted per R3 |
| 7 | `compute_half_day` | Used threshold `<= 4.0` hours or incorrect comparison | A 4.2-hour shift was marked as full day | Strictly implemented `work_hours < 4.50` per R5 |
| 8 | `list_employees` | `count_documents({})` ignored query filters | Total count remained static even when filtering by `department` | Passed the query filter `q` into `count_documents(q)` |
| 9 | `punch_in` | Used check-then-insert without unique DB constraint; vulnerable to TOCTOU race | Concurrent requests created duplicate attendance logs for same date | Added unique compound index `{emp_code: 1, date: 1}` and caught `DuplicateKeyError` to return 409 |
| 10 | `punch_out` | Allowed punch-out before punch-in, multiple punch-outs, and lacked atomic update | Subsequent punch-outs overwrote original punch-out time; negative work hours | Added atomic `find_one_and_update({"_id": id, "punch_out": None})`, 409 if already closed, and 422 if `punch_out <= punch_in` or `> 24h` |
| 11 | `list_attendance` | Did not validate `date_from <= date_to`, sorting was unindexed | `date_from > date_to` returned empty list instead of 422; slow queries on 100k docs | Added validation returning 422; added compound index `{date: -1, emp_code: 1}` |
| 12 | `regularize_attendance` | Endpoint was stubbed or missing critical R1-R5 validations and audit history | Missing audit log, allowed invalid statuses and mismatched dates | Implemented full patch handler: validates punch constraints, checks date immutability, appends immutable `{at, by, reason, changes}` entry |
| 13 | `employee_monthly` | Working days ignored join date and weekend filtering per R7 | Employees joining mid-month had inflated working days; weekend logs miscounted | Filtered working days to Mon-Fri in range `[max(month_start, joined_on), month_end]`; half days counted as 0.5 |
| 14 | `department_summary` | Headcount derived from logs; employees with 0 logs omitted | Active staff without logs were excluded from department headcount and metrics | Queried `employees` collection first for active staff (`joined_on <= month_end`), defaulting absent staff to 0 present days |
| 15 | `late_leaderboard` | Did not implement competition ranking (1224) and truncated ties at limit cutoff | Tied employees at cutoff boundary were arbitrarily excluded | Implemented dense competition ranking and preserved all ties at the boundary |
| 16 | `department_trend` | Did not cap date range at 92 days, missed 7-day moving average | Date ranges > 92 days accepted; weekend attendance rate was not null | Enforced `<= 92 days` (422 if exceeded); set weekend rate to null; computed rolling 7-day moving average |
| 17 | `admin_explain` | Missing query parameter validation per endpoint; exposed internal DB credentials | Unhandled endpoints returned 500 instead of 422; explain output unformatted | Validated required parameters per endpoint; sanitized sensitive attributes; returned standard explain structure |
| 18 | `create_indexes` | Used deprecated `@app.on_event("startup")` | Deprecation warnings on modern FastAPI; potential failure in lifespan test harnesses | Replaced with modern `@asynccontextmanager lifespan(app: FastAPI)` |

---

### Non-Defects (Considered and Retained)
1. **Server-Generated `created_at`**: The employee creation endpoint generates `created_at` on the server using current UTC epoch milliseconds. This is intentional per R10 and OpenAPI specification (`readOnly: true`).
2. **Exclusion of `_id`**: MongoDB's internal `_id` is excluded from all API responses, identifying entities via `emp_code` and `(emp_code, date)`. This strictly complies with the assignment design rules.
3. **No Automatic Weekend Creation**: The API does not create dummy records for weekends or unrecorded days. A day with no record is distinct from an `ABSENT` record, per design contract.
