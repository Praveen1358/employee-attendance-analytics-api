# Employee Attendance & Analytics API

A robust, production-ready FastAPI backend for managing employee profiles, tracking attendance records with business rules R1–R10, and computing departmental analytics and leaderboards over MongoDB.

---

## Features
- **Strict Compliance with `openapi.yaml`**: Full implementation of all endpoints, parameters, models, status codes, and error representations.
- **Robust Business Rules (R1–R10)**:
  - **R1**: IST attendance dates, overnight shift rollover (early morning punches belonging to previous calendar day shift), second-precision truncation.
  - **R2**: 10-minute grace period with strict boundary condition (> 10 min); late minutes calculated from shift start.
  - **R3**: Overtime calculation for late punch-outs with overnight shift handling; minimum 30-minute threshold.
  - **R4 & R5**: Half-up rounded work hours, half-day detection (< 4.50 hours).
  - **R6 & R7**: Working day calendar calculation (Mon–Fri within employment tenure), partial day presence weight (0.5 for half day).
  - **R8**: Half-up arithmetic rounding (no banker's rounding bias).
  - **R9 & R10**: Active staff headcount preservation for employees with zero logs, server-side epoch millisecond timestamps.
- **Full Concurrency & Race Condition Protection**:
  - Unique compound index `{ emp_code: 1, date: 1 }` on `attendance_logs` ensures atomic, race-safe punch-ins with `409 Conflict` on duplicates.
  - Atomic `find_one_and_update` on punch-outs prevents double punch-outs.
  - Audited `history` logging on manual regularization updates.
- **Explain & Performance Optimization**: Custom admin explain endpoint supporting index verification and execution stats.

---

## Project Structure
```
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py             # Single-file complete FastAPI application
├── tests/
│   ├── __init__.py
│   ├── conftest.py         # Pytest fixtures and test environment setup
│   ├── test_employees.py   # Employee CRUD and health tests
│   ├── test_attendance.py  # Punch-in, punch-out, listing, and regularization tests
│   └── test_analytics.py   # Monthly, department summary, leaderboard, and trend tests
├── sample_data/            # Sample documents for seeding
├── sample_seed.py          # Database seeding script
├── requirements.txt        # Production dependencies
├── .env.example            # Sample environment configuration
├── .gitignore              # Git ignore rules
├── openapi.yaml            # OpenAPI specification
├── DATA_MODEL.md           # MongoDB data model definition
├── REVIEW.md               # Detailed defect review of starter code
├── DECISIONS.md            # Architecture and design decisions
└── README.md               # Project documentation
```

---

## Setup & Running

### 1. Prerequisites
- Python 3.10+
- MongoDB 6.0+ (Local instance or MongoDB Atlas)

### 2. Install Dependencies
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
pip install pytest httpx pytest-cov
```

### 3. Environment Variables
Copy `.env.example` to `.env` or set environment variables:
```bash
cp .env.example .env
```
Default values:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 4. Seed Database (Optional)
```bash
python sample_seed.py
```

### 5. Start Application
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive API documentation will be available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

## Running the Test Suite
Run all 101 automated unit and integration tests:
```bash
pytest tests/ -v
```

Run tests with code coverage:
```bash
pytest tests/ --cov=app --cov-report=term-missing
```

---

## Documentation
- Refer to `REVIEW.md` for a comprehensive list of all defects identified and fixed from the starter template.
- Refer to `DECISIONS.md` for detailed rationale on index choices, race condition handling, tie resolution, and scaling strategies.
