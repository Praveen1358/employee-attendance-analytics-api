# DECISIONS.md

1. **Indexes.** On `employees`: `{emp_code: 1}` (unique) prevents duplicate creates; `{department: 1}` speeds lookups. On `attendance_logs`: `{emp_code: 1, date: 1}` (unique compound) guarantees at most one record per employee per day; `{date: -1, emp_code: 1}` directly powers pagination sort order. I rejected an index on `{punch_in: 1, punch_out: 1}` as low-cardinality and wasteful of write performance.

2. **Punch-in race.** When two identical requests hit concurrently, both attempt to insert. The unique compound index `{emp_code: 1, date: 1}` lets only the first insert commit; the second triggers a `DuplicateKeyError`, which the API catches and returns as `409 Conflict`. There is no check-then-insert gap.

3. **Ties.** The leaderboard computes standard competition ranking (1224). If employees tie at the cutoff of `limit`, all tied candidates are retained (e.g., both appear at rank 1 with `limit=1`) to avoid arbitrary exclusion.

4. **Headcount.** The department summary queries the `employees` collection first for all staff joined on or before the month's end, ensuring employees with zero logs are included with 0 present days.

5. **100x Data.** I would pre-aggregate daily department and employee totals asynchronously via MongoDB change streams, serving analytics from O(1) roll-up collections rather than aggregating millions of raw logs on request.
