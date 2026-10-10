"""
Tests for punch-in, punch-out, attendance listing, and manual correction endpoints.
"""
import pytest

from tests.conftest import (
    DEFAULT_EMPLOYEE, OVERNIGHT_EMPLOYEE,
    MS_0706_0928_IST, MS_0706_0940_00_IST, MS_0706_0940_01_IST,
    MS_0706_1015_59_IST, MS_0706_1830_IST, MS_0706_1910_IST,
    MS_0706_2155_IST, MS_0707_0500_IST, MS_0707_0640_IST,
    MS_0707_0928_IST,
)


def create_emp(client, emp=None):
    return client.post("/employees", json=emp or DEFAULT_EMPLOYEE)


# ---------------------------------------------------------------------------
# Punch-in – POST /attendance/punch-in
# ---------------------------------------------------------------------------
class TestPunchIn:

    def test_valid_punch_in(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_0928_IST})
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert data["date"] == "2026-07-06"
        assert data["status"] == "PRESENT"
        assert data["punch_in"] == MS_0706_0928_IST
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert data["late_minutes"] == 0
        assert data["overtime_minutes"] == 0
        assert data["half_day"] is False
        assert data["history"] == []

    def test_unknown_employee_404(self, client):
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": MS_0706_0928_IST})
        assert r.status_code == 404

    def test_duplicate_punch_in_409(self, client):
        create_emp(client)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_0928_IST})
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_0928_IST + 3600000})
        assert r.status_code == 409

    def test_on_time_arrival_zero_late(self, client):
        """09:40:00 -> 0 late_minutes (not strictly > 10 min)."""
        create_emp(client)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_0940_00_IST})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 0

    def test_late_arrival_boundary(self, client):
        """09:40:01 -> 10 late_minutes (strictly more than 10 min after 09:30)."""
        create_emp(client)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_0940_01_IST})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 10

    def test_late_arrival_45_minutes(self, client):
        """10:15:59 -> 45 late_minutes."""
        create_emp(client)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_1015_59_IST})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 45

    def test_seconds_value_rejected(self, client):
        """Values below 100000000000 (looks like seconds) should be rejected."""
        create_emp(client)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1_000_000})
        assert r.status_code == 422

    def test_status_wfh(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_0928_IST, "status": "WFH"})
        assert r.status_code == 201
        assert r.json()["status"] == "WFH"

    def test_status_absent_rejected(self, client):
        """ABSENT status cannot be created via punch-in."""
        create_emp(client)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_0928_IST, "status": "ABSENT"})
        assert r.status_code == 422

    def test_overnight_shift_date_correct(self, client):
        """
        EMP0005 has shift 22:00-06:00. Punch-in at 21:55 IST on Jul 6
        = 2026-07-06T16:25:00Z = 1751820300000.
        Date should be 2026-07-06.
        """
        create_emp(client, OVERNIGHT_EMPLOYEE)
        ms = MS_0706_2155_IST  # 2026-07-06 21:55:00 IST
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms})
        assert r.status_code == 201
        assert r.json()["date"] == "2026-07-06"

    def test_overnight_shift_early_morning_date(self, client):
        """
        EMP0005 shift 22:00-06:00. Punch-in at 05:00 IST on Jul 7
        (IST time < shift_end 06:00) → belongs to previous day's shift (Jul 6).
        2026-07-07T05:00:00 IST = 2026-07-06T23:30:00Z = 1751849400000
        """
        create_emp(client, OVERNIGHT_EMPLOYEE)
        ms = MS_0707_0500_IST  # 2026-07-07 05:00:00 IST (< 06:00 shift end -> 2026-07-06)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms})
        assert r.status_code == 201
        assert r.json()["date"] == "2026-07-06"

    def test_punched_at_defaults_to_now(self, client):
        """If punched_at is omitted, should use server time."""
        create_emp(client)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
        assert r.status_code == 201
        data = r.json()
        assert isinstance(data["punch_in"], int)


# ---------------------------------------------------------------------------
# Punch-out – POST /attendance/punch-out
# ---------------------------------------------------------------------------
class TestPunchOut:

    def _do_punch_in(self, client, ms=MS_0706_0928_IST):
        create_emp(client)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms})

    def test_successful_punch_out(self, client):
        self._do_punch_in(client)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS_0706_1830_IST})
        assert r.status_code == 200
        data = r.json()
        assert data["punch_out"] == MS_0706_1830_IST
        # work_hours: (18:30 - 09:28) = 9h 2m = 9.033... → round_half_up = 9.03
        assert data["work_hours"] is not None
        assert data["half_day"] is False

    def test_punch_out_unknown_employee(self, client):
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": MS_0706_1830_IST})
        assert r.status_code == 404

    def test_punch_out_no_punch_in(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS_0706_1830_IST})
        assert r.status_code == 404

    def test_duplicate_punch_out_409(self, client):
        self._do_punch_in(client)
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS_0706_1830_IST})
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS_0706_1830_IST + 3600000})
        assert r.status_code == 409

    def test_punch_out_before_punch_in_422(self, client):
        self._do_punch_in(client, ms=MS_0706_1015_59_IST)
        # punch out before punch in
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS_0706_0928_IST})
        assert r.status_code == 422

    def test_punch_out_more_than_24h_422(self, client):
        self._do_punch_in(client)
        # 25 hours after punch-in
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS_0706_0928_IST + 90_000_000})
        assert r.status_code == 422

    def test_half_day_computed(self, client):
        """3.5h work → half_day = true."""
        self._do_punch_in(client)
        # 09:28 + 3.5h = 12:58 IST = 2026-07-06T07:28:00Z
        punch_out_ms = MS_0706_0928_IST + int(3.5 * 3600 * 1000)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out_ms})
        assert r.status_code == 200
        data = r.json()
        assert data["half_day"] is True

    def test_overtime_computed(self, client):
        """Punch-out at 19:10 IST (40 min after 18:30 shift end) → overtime = 40."""
        self._do_punch_in(client)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS_0706_1910_IST})
        assert r.status_code == 200
        data = r.json()
        assert data["overtime_minutes"] == 40

    def test_overtime_under_30_is_zero(self, client):
        """25 min overtime → 0 (below 30-min threshold)."""
        self._do_punch_in(client)
        # 18:55 IST = 18:30 + 25min → 25 min OT < 30, so 0
        punch_out_ms = MS_0706_1830_IST + 25 * 60 * 1000
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out_ms})
        assert r.status_code == 200
        assert r.json()["overtime_minutes"] == 0

    def test_overnight_shift_punch_out(self, client):
        """EMP0005: punch-in 21:55 IST Jul 6, punch-out 06:40 IST Jul 7 → 40 min OT."""
        create_emp(client, OVERNIGHT_EMPLOYEE)
        pin_ms = MS_0706_2155_IST   # 2026-07-06 21:55:00 IST
        pout_ms = MS_0707_0640_IST  # 2026-07-07 06:40:00 IST (shift end 06:00 -> 40 min OT)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": pin_ms})
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": pout_ms})
        assert r.status_code == 200
        data = r.json()
        assert data["overtime_minutes"] == 40


# ---------------------------------------------------------------------------
# Attendance listing – GET /attendance
# ---------------------------------------------------------------------------
class TestListAttendance:

    def _setup(self, client):
        """Create employee and 3 attendance records."""
        create_emp(client)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_0928_IST})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS_0706_1830_IST})
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0707_0928_IST})

    def test_list_all(self, client):
        self._setup(client)
        r = client.get("/attendance")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2

    def test_emp_code_filter(self, client):
        self._setup(client)
        r = client.get("/attendance", params={"emp_code": "EMP0001"})
        assert r.json()["total"] == 2

    def test_date_from_filter(self, client):
        self._setup(client)
        r = client.get("/attendance", params={"date_from": "2026-07-07"})
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["date"] == "2026-07-07"

    def test_date_to_filter(self, client):
        self._setup(client)
        r = client.get("/attendance", params={"date_to": "2026-07-06"})
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["date"] == "2026-07-06"

    def test_combined_date_filter(self, client):
        self._setup(client)
        r = client.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-06"})
        assert r.json()["total"] == 1

    def test_date_from_after_date_to_422(self, client):
        r = client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-06"})
        assert r.status_code == 422

    def test_status_filter(self, client):
        self._setup(client)
        r = client.get("/attendance", params={"status": "PRESENT"})
        data = r.json()
        for item in data["items"]:
            assert item["status"] == "PRESENT"

    def test_pagination(self, client):
        self._setup(client)
        r = client.get("/attendance", params={"page": 1, "page_size": 1})
        data = r.json()
        assert len(data["items"]) == 1
        assert data["total"] == 2

    def test_sorted_date_desc(self, client):
        self._setup(client)
        r = client.get("/attendance")
        dates = [i["date"] for i in r.json()["items"]]
        assert dates == sorted(dates, reverse=True)

    def test_empty_results(self, client):
        r = client.get("/attendance")
        data = r.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_invalid_date_format(self, client):
        r = client.get("/attendance", params={"date_from": "07-06-2026"})
        assert r.status_code == 422

    def test_invalid_page_size(self, client):
        r = client.get("/attendance", params={"page_size": 200})
        assert r.status_code == 422

    def test_serialization_epoch_ms(self, client):
        """punch_in and punch_out must be epoch milliseconds."""
        self._setup(client)
        r = client.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-06"})
        item = r.json()["items"][0]
        assert isinstance(item["punch_in"], int)
        assert item["punch_in"] >= 100_000_000_000
        assert isinstance(item["punch_out"], int)


# ---------------------------------------------------------------------------
# Manual correction – PATCH /attendance/{emp_code}/{date}
# ---------------------------------------------------------------------------
class TestRegularize:

    def _setup(self, client):
        create_emp(client)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": MS_0706_0940_01_IST})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": MS_0706_1830_IST})

    def test_valid_correction(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": MS_0706_0928_IST,
            "reason": "Biometric glitch correction",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["punch_in"] == MS_0706_0928_IST
        assert data["late_minutes"] == 0  # was 10, now 0
        assert len(data["history"]) == 1
        h = data["history"][0]
        assert h["by"] == "hr.admin"
        assert h["reason"] == "Biometric glitch correction"
        assert "punch_in" in h["changes"]
        assert "late_minutes" in h["changes"]

    def test_unknown_employee_404(self, client):
        r = client.patch("/attendance/EMP9999/2026-07-06", json={
            "reason": "Test reason here",
            "regularized_by": "admin",
        })
        assert r.status_code == 404

    def test_unknown_record_404(self, client):
        create_emp(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "Test reason here",
            "regularized_by": "admin",
        })
        assert r.status_code == 404

    def test_absent_with_punch_in_422(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": MS_0706_0928_IST,
            "reason": "Test reason here",
            "regularized_by": "admin",
        })
        assert r.status_code == 422

    def test_no_change_422(self, client):
        """Request that changes nothing must be 422."""
        self._setup(client)
        # punch_in same as current
        r_get = client.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-06", "date_to": "2026-07-06"})
        current_pi = r_get.json()["items"][0]["punch_in"]
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": current_pi,
            "reason": "No actual change test",
            "regularized_by": "admin",
        })
        assert r.status_code == 422

    def test_audit_history_preserved(self, client):
        """Second correction preserves first history entry."""
        self._setup(client)
        client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": MS_0706_0928_IST,
            "reason": "First correction reason",
            "regularized_by": "hr.admin",
        })
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": MS_0706_0928_IST - 60000,  # 1 min earlier
            "reason": "Second correction reason",
            "regularized_by": "hr.admin",
        })
        data = r.json()
        assert len(data["history"]) == 2

    def test_reason_too_short_422(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "bad",
            "regularized_by": "admin",
        })
        assert r.status_code == 422

    def test_set_absent_clears_punch_times(self, client):
        """Setting ABSENT clears punch_in and punch_out."""
        self._setup(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "reason": "Employee was actually absent",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ABSENT"
        assert data["punch_in"] is None
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert data["late_minutes"] == 0

    def test_history_changes_only_changed_fields(self, client):
        """history.changes must only contain fields that actually changed."""
        self._setup(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": MS_0706_0928_IST,
            "reason": "Fixing punch_in time only",
            "regularized_by": "hr.admin",
        })
        data = r.json()
        changes = data["history"][0]["changes"]
        # punch_in changed, late_minutes changed (10 -> 0), work_hours changed
        assert "punch_in" in changes
        # status did not change - should NOT be in changes
        assert "status" not in changes
