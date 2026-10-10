"""
conftest.py – pytest configuration for Employee Attendance & Analytics API tests.

Uses an isolated test database to avoid polluting production data.
All tests share a single MongoClient; the test DB is wiped between test modules.
"""
import os
import pytest

# Force test DB before importing the app
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "attendance_test_db"

from fastapi.testclient import TestClient
from pymongo import MongoClient

# Now import the app (it reads MONGO_DB from env)
from app.main import app, db as app_db

TEST_MONGO_URI = os.environ["MONGO_URI"]
TEST_DB_NAME = os.environ["MONGO_DB"]


@pytest.fixture(scope="session")
def mongo_client():
    c = MongoClient(TEST_MONGO_URI)
    yield c
    c.close()


@pytest.fixture(scope="function", autouse=True)
def clean_db(mongo_client):
    """Wipe the test database before each test."""
    test_db = mongo_client[TEST_DB_NAME]
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})
    yield
    # Optionally clean up after test too
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})


@pytest.fixture(scope="session")
def client():
    """FastAPI TestClient that hits the in-process app."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper data factories
# ---------------------------------------------------------------------------
DEFAULT_EMPLOYEE = {
    "emp_code": "EMP0001",
    "name": "Asha Rao",
    "email": "asha@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-05",
}

OVERNIGHT_EMPLOYEE = {
    "emp_code": "EMP0005",
    "name": "Meera Nair",
    "email": "meera@example.com",
    "department": "Support",
    "shift_start": "22:00",
    "shift_end": "06:00",
    "joined_on": "2026-03-10",
}


def create_employee(client, emp=None):
    emp = emp or DEFAULT_EMPLOYEE
    r = client.post("/employees", json=emp)
    return r


# Epoch milliseconds for specific IST times in 2026:
# 2026-07-06 09:28:00 IST = 1783310280000 (within grace, 0 late)
MS_0706_0928_IST = 1783310280000
# 2026-07-06 09:40:00 IST = 1783311000000 (0 late)
MS_0706_0940_00_IST = 1783311000000
# 2026-07-06 09:40:01 IST = 1783311001000 (10 late_minutes)
MS_0706_0940_01_IST = 1783311001000
# 2026-07-06 10:15:59 IST = 1783313159000 (45 late_minutes)
MS_0706_1015_59_IST = 1783313159000
# Punch-out: 2026-07-06 18:30:00 IST = 1783342800000
MS_0706_1830_IST = 1783342800000
# 2026-07-06 19:10:00 IST = 1783345200000 (40 min OT)
MS_0706_1910_IST = 1783345200000
# Overnight shift punch-in: 2026-07-06 21:55:00 IST = 1783355100000
MS_0706_2155_IST = 1783355100000
# Overnight early morning punch-in: 2026-07-07 05:00:00 IST = 1783380600000
MS_0707_0500_IST = 1783380600000
# Overnight shift punch-out: 2026-07-07 06:40:00 IST = 1783386600000 (40 min OT)
MS_0707_0640_IST = 1783386600000
# 2026-07-07 09:28:00 IST = 1783396680000
MS_0707_0928_IST = 1783396680000
# 2026-07-07 18:30:00 IST = 1783429200000
MS_0707_1830_IST = 1783429200000
# 2026-07-08 09:28:00 IST = 1783483080000
MS_0708_0928_IST = 1783483080000
# 2026-07-08 18:30:00 IST = 1783515600000
MS_0708_1830_IST = 1783515600000
