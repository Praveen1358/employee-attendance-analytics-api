"""
Tests for health check, employee creation, and employee listing endpoints.
"""
import pytest


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# Employee creation – POST /employees
# ---------------------------------------------------------------------------
class TestCreateEmployee:
    BASE = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }

    def test_create_success(self, client):
        r = client.post("/employees", json=self.BASE)
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert data["name"] == "Asha Rao"
        assert isinstance(data["created_at"], int)
        assert data["created_at"] >= 100_000_000_000
        assert "_id" not in data

    def test_duplicate_emp_code(self, client):
        client.post("/employees", json=self.BASE)
        r = client.post("/employees", json=self.BASE)
        assert r.status_code == 409

    def test_invalid_email(self, client):
        bad = {**self.BASE, "email": "notanemail"}
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_invalid_shift_same(self, client):
        bad = {**self.BASE, "shift_start": "09:30", "shift_end": "09:30"}
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_missing_required_field(self, client):
        bad = {k: v for k, v in self.BASE.items() if k != "emp_code"}
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_missing_joined_on(self, client):
        bad = {k: v for k, v in self.BASE.items() if k != "joined_on"}
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_invalid_emp_code_format(self, client):
        bad = {**self.BASE, "emp_code": "EMP1"}  # too short
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_invalid_shift_time_format(self, client):
        bad = {**self.BASE, "shift_start": "9:30"}  # missing leading zero
        r = client.post("/employees", json=bad)
        assert r.status_code == 422

    def test_defaults_applied(self, client):
        """shift_start and shift_end should default if not provided."""
        emp = {k: v for k, v in self.BASE.items() if k not in ("shift_start", "shift_end")}
        r = client.post("/employees", json=emp)
        assert r.status_code == 201
        data = r.json()
        assert data["shift_start"] == "09:30"
        assert data["shift_end"] == "18:30"


# ---------------------------------------------------------------------------
# Employee listing – GET /employees
# ---------------------------------------------------------------------------
class TestListEmployees:
    EMPS = [
        {"emp_code": "EMP0001", "name": "A", "email": "a@x.com", "department": "Eng", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "name": "B", "email": "b@x.com", "department": "Sales", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0003", "name": "C", "email": "c@x.com", "department": "Eng", "joined_on": "2026-01-01"},
    ]

    def _create_all(self, client):
        for e in self.EMPS:
            client.post("/employees", json=e)

    def test_empty_list(self, client):
        r = client.get("/employees")
        assert r.status_code == 200
        data = r.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_all(self, client):
        self._create_all(client)
        r = client.get("/employees")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        # sorted by emp_code
        codes = [i["emp_code"] for i in data["items"]]
        assert codes == sorted(codes)

    def test_department_filter(self, client):
        self._create_all(client)
        r = client.get("/employees", params={"department": "Eng"})
        data = r.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["department"] == "Eng"

    def test_department_filter_total_filtered(self, client):
        """total must reflect the filtered result, not all employees."""
        self._create_all(client)
        r = client.get("/employees", params={"department": "Sales"})
        data = r.json()
        assert data["total"] == 1

    def test_pagination_first_page(self, client):
        self._create_all(client)
        r = client.get("/employees", params={"page": 1, "page_size": 2})
        data = r.json()
        assert len(data["items"]) == 2
        assert data["total"] == 3
        assert data["page"] == 1

    def test_pagination_second_page(self, client):
        self._create_all(client)
        r = client.get("/employees", params={"page": 2, "page_size": 2})
        data = r.json()
        assert len(data["items"]) == 1

    def test_invalid_page(self, client):
        r = client.get("/employees", params={"page": 0})
        assert r.status_code == 422

    def test_invalid_page_size_too_large(self, client):
        r = client.get("/employees", params={"page_size": 101})
        assert r.status_code == 422

    def test_page_size_max(self, client):
        r = client.get("/employees", params={"page_size": 100})
        assert r.status_code == 200
