"""
Tests for analytics endpoints: monthly, department summary, leaderboard, trend, and admin/explain.
"""
import pytest
from datetime import datetime, timezone, timedelta

from tests.conftest import DEFAULT_EMPLOYEE, OVERNIGHT_EMPLOYEE


# ---------------------------------------------------------------------------
# Helper: create employees and punch-in/out records
# ---------------------------------------------------------------------------
def create_emp(client, emp):
    return client.post("/employees", json=emp)


def punch(client, emp_code, pin_ms, pout_ms=None, status="PRESENT"):
    r = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": pin_ms, "status": status})
    if pout_ms:
        client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": pout_ms})
    return r


# ---------------------- FIXED EPOCH MILLISECONDS ---------------------------
# 2026-07-06 09:28 IST = 2026-07-06T03:58:00Z
PIN_0706_0928 = 1783310280000
# 2026-07-06 18:30 IST = 2026-07-06T13:00:00Z
POUT_0706_1830 = 1783342800000
# 2026-07-06 10:05 IST → 35 late minutes
PIN_0706_1005 = 1783312500000
# 2026-07-06 19:10 IST → 40 min OT
POUT_0706_1910 = 1783345200000

# 2026-07-07 09:28 IST
PIN_0707_0928 = 1783396680000
POUT_0707_1830 = 1783429200000

# 2026-07-08 09:28 IST
PIN_0708_0928 = 1783483080000
POUT_0708_1830 = 1783515600000

# 2026-07-09 09:28 IST (Thursday)
PIN_0709_0928 = 1783569480000
# 2026-07-09 12:58 IST = 3.5h → half day
POUT_0709_1258 = 1783582080000


# ---------------------------------------------------------------------------
# Monthly analytics – GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------
class TestEmployeeMonthly:

    def test_valid_month(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        punch(client, "EMP0001", PIN_0706_0928, POUT_0706_1830)
        r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        assert r.status_code == 200
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert data["month"] == "2026-07"
        assert data["present_days"] >= 1
        assert isinstance(data["working_days"], int)
        assert data["working_days"] > 0

    def test_unknown_employee_404(self, client):
        r = client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"})
        assert r.status_code == 404

    def test_no_records_in_month(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-06"})
        assert r.status_code == 200
        data = r.json()
        assert data["present_days"] == 0
        assert data["late_count"] == 0

    def test_late_count(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        punch(client, "EMP0001", PIN_0706_1005, POUT_0706_1910)  # 35 late, 40 OT
        r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        assert r.status_code == 200
        data = r.json()
        assert data["late_count"] == 1
        assert data["total_late_minutes"] == 35
        assert data["total_overtime_minutes"] == 40

    def test_leave_day_counted(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        # Insert LEAVE record via patch (first create a punch-in then correct it)
        # We'll directly insert via seed script approach - instead, test via API
        punch(client, "EMP0001", PIN_0706_0928, POUT_0706_1830)
        # Regularize to LEAVE
        client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "LEAVE",
            "reason": "Employee was on approved leave",
            "regularized_by": "hr.admin",
        })
        r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        data = r.json()
        assert data["leave_days"] == 1
        assert data["present_days"] == 0

    def test_half_day_present(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        punch(client, "EMP0001", PIN_0709_0928, POUT_0709_1258)  # 3.5h → half day
        r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        data = r.json()
        # 2026-07-09 is Wednesday → working day, half day → 0.5
        assert data["present_days"] == 0.5

    def test_attendance_pct(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        punch(client, "EMP0001", PIN_0706_0928, POUT_0706_1830)
        r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        data = r.json()
        assert data["attendance_pct"] is not None
        assert 0 <= data["attendance_pct"] <= 100

    def test_attendance_pct_null_no_working_days(self, client):
        """If joined_on is after month end, working_days = 0, attendance_pct = null."""
        emp = {**DEFAULT_EMPLOYEE, "joined_on": "2026-08-01"}
        create_emp(client, emp)
        r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        data = r.json()
        assert data["working_days"] == 0
        assert data["attendance_pct"] is None

    def test_invalid_month_422(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Department summary – GET /analytics/departments/summary
# ---------------------------------------------------------------------------
class TestDepartmentSummary:

    def test_basic_summary(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        punch(client, "EMP0001", PIN_0706_0928, POUT_0706_1830)
        r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        assert r.status_code == 200
        data = r.json()
        assert data["month"] == "2026-07"
        assert isinstance(data["items"], list)
        assert len(data["items"]) >= 1
        item = next(i for i in data["items"] if i["department"] == "Engineering")
        assert item["headcount"] >= 1

    def test_zero_log_employees_counted(self, client):
        """R9: employees with joined_on <= month end are counted even with 0 logs."""
        create_emp(client, DEFAULT_EMPLOYEE)  # No punch-in
        r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        data = r.json()
        item = next((i for i in data["items"] if i["department"] == "Engineering"), None)
        assert item is not None
        assert item["headcount"] == 1
        assert item["present_days"] == 0

    def test_dept_filter(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        create_emp(client, {**DEFAULT_EMPLOYEE, "emp_code": "EMP0003", "department": "Sales",
                            "email": "s@x.com"})
        r = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"})
        data = r.json()
        assert all(i["department"] == "Engineering" for i in data["items"])

    def test_sorted_by_department(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        create_emp(client, {**DEFAULT_EMPLOYEE, "emp_code": "EMP0003", "department": "Sales", "email": "s@x.com"})
        r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        depts = [i["department"] for i in r.json()["items"]]
        assert depts == sorted(depts)

    def test_avg_work_hours_null_no_punchout(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        punch(client, "EMP0001", PIN_0706_0928)  # no punch-out
        r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        item = next(i for i in r.json()["items"] if i["department"] == "Engineering")
        assert item["avg_work_hours"] is None

    def test_missing_month_422(self, client):
        r = client.get("/analytics/departments/summary")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Late leaderboard – GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------
class TestLateLeaderboard:

    def test_basic_ranking(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        punch(client, "EMP0001", PIN_0706_1005, POUT_0706_1910)  # 35 late
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        assert r.status_code == 200
        data = r.json()
        assert data["month"] == "2026-07"
        assert len(data["items"]) == 1
        assert data["items"][0]["rank"] == 1
        assert data["items"][0]["emp_code"] == "EMP0001"
        assert data["items"][0]["total_late_minutes"] == 35

    def test_employees_with_zero_late_excluded(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        punch(client, "EMP0001", PIN_0706_0928, POUT_0706_1830)  # 0 late
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        data = r.json()
        assert len(data["items"]) == 0

    def test_tie_handling(self, client):
        """Both employees with same late_minutes should share rank 1."""
        create_emp(client, DEFAULT_EMPLOYEE)
        create_emp(client, {**DEFAULT_EMPLOYEE, "emp_code": "EMP0002", "email": "b@x.com"})
        punch(client, "EMP0001", PIN_0706_1005, POUT_0706_1910)  # 35 late
        punch(client, "EMP0002", PIN_0706_1005, POUT_0706_1910)  # 35 late
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1})
        data = r.json()
        # Both tie at rank 1, both should appear despite limit=1
        assert len(data["items"]) == 2
        assert data["items"][0]["rank"] == 1
        assert data["items"][1]["rank"] == 1

    def test_ranking_order(self, client):
        """Higher total_late_minutes → lower rank number."""
        create_emp(client, DEFAULT_EMPLOYEE)
        create_emp(client, {**DEFAULT_EMPLOYEE, "emp_code": "EMP0002", "email": "b@x.com"})
        punch(client, "EMP0001", PIN_0706_1005, POUT_0706_1910)  # 35 late
        # EMP0002: 09:50:01 IST = 09:30 + 20:01 → 20 late
        pin_0706_0950 = PIN_0706_0928 + int(22 * 60 * 1000) + 1000  # ~22 min late
        punch(client, "EMP0002", pin_0706_0950, POUT_0706_1830)
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        items = r.json()["items"]
        assert items[0]["total_late_minutes"] >= items[1]["total_late_minutes"]

    def test_limit_param(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        create_emp(client, {**DEFAULT_EMPLOYEE, "emp_code": "EMP0002", "email": "b@x.com"})
        create_emp(client, {**DEFAULT_EMPLOYEE, "emp_code": "EMP0003", "email": "c@x.com"})
        punch(client, "EMP0001", PIN_0706_1005)  # 35 late
        punch(client, "EMP0002", PIN_0706_1005 + 600000)  # different time → different late mins
        punch(client, "EMP0003", PIN_0706_0928)  # 0 late, excluded
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1})
        items = r.json()["items"]
        # All with rank <= 1 returned (no ties here, so just 1)
        assert all(i["rank"] <= 1 for i in items)

    def test_empty_results(self, client):
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        data = r.json()
        assert data["items"] == []

    def test_department_filter(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)  # Engineering
        create_emp(client, {**DEFAULT_EMPLOYEE, "emp_code": "EMP0002", "email": "b@x.com", "department": "Sales"})
        punch(client, "EMP0001", PIN_0706_1005)
        punch(client, "EMP0002", PIN_0706_1005)
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Engineering"})
        items = r.json()["items"]
        assert all(i["department"] == "Engineering" for i in items)


# ---------------------------------------------------------------------------
# Department trend – GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------
class TestDepartmentTrend:

    def test_basic_trend(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        punch(client, "EMP0001", PIN_0706_0928, POUT_0706_1830)
        r = client.get("/analytics/departments/Engineering/trend",
                       params={"from": "2026-07-06", "to": "2026-07-08"})
        assert r.status_code == 200
        data = r.json()
        assert data["department"] == "Engineering"
        assert len(data["items"]) == 3  # 3 days

    def test_all_days_returned(self, client):
        """Even days with no records are included."""
        create_emp(client, DEFAULT_EMPLOYEE)
        r = client.get("/analytics/departments/Engineering/trend",
                       params={"from": "2026-07-01", "to": "2026-07-07"})
        data = r.json()
        assert len(data["items"]) == 7

    def test_is_working_day(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        # 2026-07-06 is Monday, 2026-07-05 is Sunday
        r = client.get("/analytics/departments/Engineering/trend",
                       params={"from": "2026-07-04", "to": "2026-07-06"})
        items = {i["date"]: i for i in r.json()["items"]}
        assert items["2026-07-04"]["is_working_day"] is False  # Saturday
        assert items["2026-07-05"]["is_working_day"] is False  # Sunday
        assert items["2026-07-06"]["is_working_day"] is True   # Monday

    def test_unknown_department_404(self, client):
        r = client.get("/analytics/departments/Nonexistent/trend",
                       params={"from": "2026-07-01", "to": "2026-07-07"})
        assert r.status_code == 404

    def test_to_before_from_422(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        r = client.get("/analytics/departments/Engineering/trend",
                       params={"from": "2026-07-08", "to": "2026-07-06"})
        assert r.status_code == 422

    def test_range_exceeds_92_days_422(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        r = client.get("/analytics/departments/Engineering/trend",
                       params={"from": "2026-01-01", "to": "2026-04-10"})  # > 92 days
        assert r.status_code == 422

    def test_attendance_rate_null_on_weekend(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        # 2026-07-05 is Sunday
        r = client.get("/analytics/departments/Engineering/trend",
                       params={"from": "2026-07-05", "to": "2026-07-05"})
        item = r.json()["items"][0]
        assert item["is_working_day"] is False
        assert item["attendance_rate"] is None

    def test_moving_avg_7d(self, client):
        """Moving average uses up to 7 preceding non-null rates."""
        create_emp(client, DEFAULT_EMPLOYEE)
        r = client.get("/analytics/departments/Engineering/trend",
                       params={"from": "2026-07-06", "to": "2026-07-12"})
        items = r.json()["items"]
        # First day: moving_avg_7d = attendance_rate of that day (or null if non-working)
        # All rates are 0 (no punches), so moving_avg should be 0 on working days
        for item in items:
            if item["attendance_rate"] is not None:
                assert item["moving_avg_7d"] is not None


# ---------------------------------------------------------------------------
# Admin explain – GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------
class TestAdminExplain:

    def test_explain_attendance_list(self, client):
        r = client.get("/admin/explain/attendance_list")
        assert r.status_code == 200
        data = r.json()
        assert "endpoint" in data
        assert "collection" in data
        assert "explain" in data
        assert data["endpoint"] == "attendance_list"

    def test_explain_employee_monthly(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        r = client.get("/admin/explain/employee_monthly",
                       params={"emp_code": "EMP0001", "month": "2026-07"})
        assert r.status_code == 200
        data = r.json()
        assert data["endpoint"] == "employee_monthly"

    def test_explain_missing_params_422(self, client):
        """employee_monthly requires emp_code and month."""
        r = client.get("/admin/explain/employee_monthly")
        assert r.status_code == 422

    def test_explain_invalid_endpoint_422(self, client):
        r = client.get("/admin/explain/nonexistent_endpoint")
        assert r.status_code == 422

    def test_explain_department_summary(self, client):
        r = client.get("/admin/explain/department_summary",
                       params={"month": "2026-07"})
        assert r.status_code == 200

    def test_explain_late_leaderboard(self, client):
        r = client.get("/admin/explain/late_leaderboard",
                       params={"month": "2026-07"})
        assert r.status_code == 200

    def test_explain_department_trend(self, client):
        create_emp(client, DEFAULT_EMPLOYEE)
        r = client.get("/admin/explain/department_trend",
                       params={"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"})
        assert r.status_code == 200

    def test_no_sensitive_info_in_response(self, client):
        """Explain response must not contain MongoDB URI or credentials."""
        r = client.get("/admin/explain/attendance_list")
        text = r.text
        assert "MONGO_URI" not in text
        assert "password" not in text.lower()
        assert "mongodb://" not in text
