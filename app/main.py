"""
Employee Attendance & Analytics API - COMPLETE IMPLEMENTATION

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

All business rules R1-R10 are implemented as defined in openapi.yaml.
All instants are stored as BSON dates (UTC datetimes) in MongoDB,
and are returned/accepted as epoch milliseconds at the HTTP boundary.
"""
import os
import math
import calendar
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date as date_type
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# Constants & timezone
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))

# Valid epoch milliseconds range per OpenAPI EpochMillis schema
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

# ---------------------------------------------------------------------------
# MongoDB connection
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
def create_indexes():
    """Create all required indexes at startup, idempotently."""
    # employees: unique index on emp_code (guarantees concurrent-create safety)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    # employees: department lookup for analytics
    db.employees.create_index([("department", ASCENDING)], name="department_asc")
    # employees: joined_on for R9 headcount calculations
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on_asc")

    # attendance_logs: unique compound index on (emp_code, date)
    # This prevents concurrent duplicate punch-ins at the DB level (no check-then-insert)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="emp_date_unique"
    )
    # attendance_logs: sort/filter index for listing (date desc, emp_code asc)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_emp_asc"
    )
    # attendance_logs: status filter support
    db.attendance_logs.create_index([("status", ASCENDING)], name="status_asc")
    # attendance_logs: analytics queries (emp_code + date range + status)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="emp_date_status"
    )


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------
def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC datetime (aware or naive-UTC) to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime, truncated to whole seconds (R1)."""
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def serialize_record(doc: dict) -> dict:
    """Serialize a MongoDB attendance_logs document for the HTTP response."""
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_ms(doc.get("punch_out"))
    # Fill in missing fields from legacy records (DATA_MODEL.md §3)
    if "history" not in doc:
        doc["history"] = []
    if "half_day" not in doc:
        doc["half_day"] = False
    if "late_minutes" not in doc:
        doc["late_minutes"] = 0
    if "overtime_minutes" not in doc:
        doc["overtime_minutes"] = 0
    # Serialize history entries; punch_in/punch_out changes stored as BSON dates
    history = []
    for entry in doc.get("history", []):
        h = dict(entry)
        h["at"] = dt_to_ms(h.get("at"))
        changes = {}
        for field, cv in h.get("changes", {}).items():
            from_val = cv.get("from")
            to_val = cv.get("to")
            if field in ("punch_in", "punch_out"):
                from_val = dt_to_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = dt_to_ms(to_val) if isinstance(to_val, datetime) else to_val
            changes[field] = {"from": from_val, "to": to_val}
        h["changes"] = changes
        history.append(h)
    doc["history"] = history
    return doc


def serialize_employee(doc: dict) -> dict:
    """Serialize a MongoDB employees document for the HTTP response."""
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc.get("created_at"))
    return doc


# ---------------------------------------------------------------------------
# Business rule helpers
# ---------------------------------------------------------------------------
def round_half_up(value: float, decimals: int) -> float:
    """Round half-up (not banker's rounding) to the given number of decimal places (R8)."""
    multiplier = 10 ** decimals
    return math.floor(value * multiplier + 0.5) / multiplier


def get_ist_datetime(dt: datetime) -> datetime:
    """Convert a UTC datetime to IST (aware)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """Return True if shift_end <= shift_start (overnight shift per R1)."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh * 60 + em) <= (sh * 60 + sm)


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Compute the IST attendance date per R1.
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time
    is earlier than shift_end belongs to the shift that started the previous day.
    """
    ist_dt = get_ist_datetime(punch_in_utc)
    if is_overnight(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        punch_time_minutes = ist_dt.hour * 60 + ist_dt.minute
        shift_end_minutes = eh * 60 + em
        if punch_time_minutes < shift_end_minutes:
            return (ist_dt.date() - timedelta(days=1)).isoformat()
    return ist_dt.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period is 10 minutes. Late only if strictly more than 10:00 after shift_start.
    late_minutes = floor(minutes elapsed since shift_start), NOT since grace period end.
    Examples (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    h, m = map(int, shift_start.split(":"))
    date_obj = date_type.fromisoformat(attendance_date)
    shift_start_ist = datetime(date_obj.year, date_obj.month, date_obj.day, h, m, 0, tzinfo=IST)
    shift_start_utc = shift_start_ist.astimezone(timezone.utc)

    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    # R1: truncate to whole seconds
    punch_in_sec = punch_in_utc.replace(microsecond=0)

    elapsed_seconds = (punch_in_sec - shift_start_utc).total_seconds()
    elapsed_minutes = elapsed_seconds / 60

    # Strictly more than 10 minutes → late
    if elapsed_minutes > 10:
        return int(elapsed_minutes)  # floor
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out_utc: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    """
    R3: overtime_minutes = floor(minutes between shift_end and punch_out),
    counted only when >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    h, m = map(int, shift_end.split(":"))
    date_obj = date_type.fromisoformat(attendance_date)
    if is_overnight(shift_start, shift_end):
        end_date = date_obj + timedelta(days=1)
    else:
        end_date = date_obj
    shift_end_ist = datetime(end_date.year, end_date.month, end_date.day, h, m, 0, tzinfo=IST)
    shift_end_utc = shift_end_ist.astimezone(timezone.utc)

    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    # R1: truncate to whole seconds
    punch_out_sec = punch_out_utc.replace(microsecond=0)

    ot_seconds = (punch_out_sec - shift_end_utc).total_seconds()
    ot_minutes = int(ot_seconds / 60)  # floor
    return ot_minutes if ot_minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def is_working_day(d: date_type) -> bool:
    """R7: Mon-Fri counts as a working day."""
    return d.weekday() < 5


def working_days_in_range(from_date: date_type, to_date: date_type, joined_on_str: str) -> int:
    """
    R7: Count Mon-Fri days in [from_date, to_date], starting from max(from_date, joined_on).
    """
    joined_on = date_type.fromisoformat(joined_on_str)
    start = max(from_date, joined_on)
    count = 0
    d = start
    while d <= to_date:
        if is_working_day(d):
            count += 1
        d += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Invalid email format")
        if len(v) > 120:
            raise ValueError("email must be at most 120 characters")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v: str) -> str:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date_type.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return v

    @model_validator(mode="after")
    def validate_shift_differs(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v) -> Optional[int]:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("punched_at must be an integer epoch milliseconds value")
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(f"punched_at must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v) -> Optional[int]:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("punched_at must be an integer epoch milliseconds value")
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(f"punched_at must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}")
        return v


class RegularizeIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch(cls, v) -> Optional[int]:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("must be an integer epoch milliseconds value")
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(f"must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}")
        return v


# ---------------------------------------------------------------------------
# Month param helper
# ---------------------------------------------------------------------------
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def parse_month(month: str) -> tuple:
    """Parse a YYYY-MM string and return (year, month_int, first_day_str, last_day_str)."""
    if not MONTH_RE.match(month):
        raise HTTPException(422, detail=[{"msg": "month must be YYYY-MM", "loc": ["query", "month"]}])
    year, m = int(month[:4]), int(month[5:])
    last_day = calendar.monthrange(year, m)[1]
    first_day_str = f"{year:04d}-{m:02d}-01"
    last_day_str = f"{year:04d}-{m:02d}-{last_day:02d}"
    return year, m, first_day_str, last_day_str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# --- Health ---
@app.get("/health")
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


# --- Employees ---
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """
    Create an employee.
    FIX: unique emp_code enforced by DB unique index (prevents concurrent duplicates).
    FIX: DuplicateKeyError handled explicitly to return 409.
    FIX: created_at stored as UTC datetime.
    """
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List employees, sorted by emp_code ascending.
    FIX: skip was (page * page_size) - corrected to (page-1) * page_size.
    FIX: total now reflects the filtered set, not whole collection.
    FIX: added DB-level sort by emp_code ascending.
    """
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size  # BUG FIX: was page * page_size
    total = db.employees.count_documents(q)  # BUG FIX: was count_documents({}) ignoring filter
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        item["created_at"] = dt_to_ms(item.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --- Attendance ---
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """
    Record a punch-in. Creates the day's attendance record.
    FIX: emp_code validated against employees collection (404 if unknown).
    FIX: timestamps handled as epoch ms (truncated to seconds per R1).
    FIX: attendance date computed per R1 (including overnight shift logic).
    FIX: late_minutes computed correctly per R2.
    FIX: DB unique index prevents concurrent duplicate punch-ins (no check-then-insert race).
    FIX: punch_in stored as UTC BSON date, not naive datetime.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = ms_to_dt(body.punched_at)
    else:
        now_ms = int(datetime.now(tz=timezone.utc).timestamp()) * 1000
        punch_in_utc = ms_to_dt(now_ms)

    attendance_date = compute_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")

    return serialize_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    """
    Record a punch-out. Closes the most recent open punch-in record.
    Computes work_hours (R4), overtime_minutes (R3), half_day (R5).
    Uses find_one_and_update with punch_out=null check for concurrency safety.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = ms_to_dt(body.punched_at)
    else:
        now_ms = int(datetime.now(tz=timezone.utc).timestamp()) * 1000
        punch_out_utc = ms_to_dt(now_ms)

    # Find the most recent open record (punch_out is null)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
        },
        sort=[("date", DESCENDING)],
    )

    if record is None:
        # Check if the most recent record has already been punched out
        recent = db.attendance_logs.find_one(
            {"emp_code": body.emp_code},
            sort=[("date", DESCENDING)],
        )
        if recent and recent.get("punch_out") is not None:
            raise HTTPException(409, detail="Already punched out for this record")
        raise HTTPException(404, detail="No open punch-in found for this employee")

    punch_in_utc = record.get("punch_in")
    if punch_in_utc is None:
        raise HTTPException(422, detail=[{"msg": "Cannot punch out on a record without punch_in", "loc": ["body"]}])
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # R4: punch_out must be after punch_in
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(422, detail=[{"msg": "punched_at must be after punch_in", "loc": ["body", "punched_at"]}])

    # Max 24 hours
    if (punch_out_utc - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(422, detail=[{"msg": "punch_out must be within 24 hours of punch_in", "loc": ["body", "punched_at"]}])

    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_mins = compute_overtime(punch_out_utc, emp["shift_end"], record["date"], emp["shift_start"])
    hd = compute_half_day(work_hrs)

    # Atomic update: only succeeds if punch_out is still null
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_utc,
            "work_hours": work_hrs,
            "overtime_minutes": ot_mins,
            "half_day": hd,
        }},
        return_document=True,
    )

    if result is None:
        # Another request beat us to it
        raise HTTPException(409, detail="Already punched out for this record")

    return serialize_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records. Sorted by date desc, emp_code asc.
    FIX: Uses DB-level skip/limit (not load-all-into-memory).
    FIX: total reflects the filtered set.
    FIX: Validates date formats and rejects date_from > date_to.
    """
    if date_from:
        try:
            date_type.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(422, detail=[{"msg": "date_from is not a valid date", "loc": ["query", "date_from"]}])
    if date_to:
        try:
            date_type.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(422, detail=[{"msg": "date_to is not a valid date", "loc": ["query", "date_to"]}])
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail=[{"msg": "date_from must not be after date_to", "loc": ["query", "date_from"]}])

    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=[{"msg": f"status must be one of {sorted(ALL_STATUSES)}", "loc": ["query", "status"]}])

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_record(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    Manually correct an attendance record. Recomputes derived values and appends audit history.
    Uses optimistic concurrency to prevent silent history loss.
    """
    try:
        date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, detail=[{"msg": "date is not a valid YYYY-MM-DD date", "loc": ["path", "date"]}])

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, detail="Attendance record not found")

    # Determine final values
    new_status = body.status if body.status is not None else record["status"]
    new_punch_in_dt = ms_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    new_punch_out_dt = ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    # Rule: ABSENT/LEAVE clears punch times; supplying punch times with them is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail=[{"msg": "punch_in/punch_out cannot be set with ABSENT or LEAVE status", "loc": ["body"]}])
        new_punch_in_dt = None
        new_punch_out_dt = None

    # Presence status requires punch_in
    if new_status in PRESENCE_STATUSES and new_punch_in_dt is None:
        raise HTTPException(422, detail=[{"msg": "A presence status requires punch_in", "loc": ["body", "punch_in"]}])

    # punch_in must stay on the record's attendance date (R1)
    if new_punch_in_dt is not None:
        computed_date = compute_attendance_date(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422, detail=[{"msg": f"punch_in date resolves to {computed_date}, must stay on {date}", "loc": ["body", "punch_in"]}])

    # punch_out must be after punch_in and within 24 hours
    if new_punch_out_dt is not None and new_punch_in_dt is not None:
        pi = new_punch_in_dt if new_punch_in_dt.tzinfo else new_punch_in_dt.replace(tzinfo=timezone.utc)
        po = new_punch_out_dt if new_punch_out_dt.tzinfo else new_punch_out_dt.replace(tzinfo=timezone.utc)
        if po <= pi:
            raise HTTPException(422, detail=[{"msg": "punch_out must be after punch_in", "loc": ["body", "punch_out"]}])
        if (po - pi).total_seconds() > 86400:
            raise HTTPException(422, detail=[{"msg": "punch_out must be within 24 hours of punch_in", "loc": ["body", "punch_out"]}])

    # Recompute derived values
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in_dt, emp["shift_start"], date) if new_punch_in_dt else 0

        if new_punch_in_dt is not None and new_punch_out_dt is not None:
            pi = new_punch_in_dt if new_punch_in_dt.tzinfo else new_punch_in_dt.replace(tzinfo=timezone.utc)
            po = new_punch_out_dt if new_punch_out_dt.tzinfo else new_punch_out_dt.replace(tzinfo=timezone.utc)
            new_work_hours = compute_work_hours(pi, po)
            new_overtime_minutes = compute_overtime(po, emp["shift_end"], date, emp["shift_start"])
            new_half_day = compute_half_day(new_work_hours)
        elif new_punch_out_dt is None:
            # No punch_out
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False
        else:
            # punch_in changed, existing punch_out still applies
            old_punch_out = record.get("punch_out")
            if old_punch_out is not None and new_punch_in_dt is not None:
                pi = new_punch_in_dt if new_punch_in_dt.tzinfo else new_punch_in_dt.replace(tzinfo=timezone.utc)
                po = old_punch_out if old_punch_out.tzinfo else old_punch_out.replace(tzinfo=timezone.utc)
                new_work_hours = compute_work_hours(pi, po)
                new_overtime_minutes = compute_overtime(po, emp["shift_end"], date, emp["shift_start"])
                new_half_day = compute_half_day(new_work_hours)
            else:
                new_work_hours = None
                new_overtime_minutes = 0
                new_half_day = False

    # Build changes dict - only fields that actually changed
    def dt_eq(a, b):
        """Compare two datetimes (or None) ignoring microseconds."""
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        a_norm = a.replace(tzinfo=None, microsecond=0) if a.tzinfo else a.replace(microsecond=0)
        b_norm = b.replace(tzinfo=None, microsecond=0) if b.tzinfo else b.replace(microsecond=0)
        return a_norm == b_norm

    def ms_or_none(dt):
        return dt_to_ms(dt) if isinstance(dt, datetime) else dt

    changes = {}
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")

    if record["status"] != new_status:
        changes["status"] = {"from": record["status"], "to": new_status}
    if not dt_eq(old_punch_in, new_punch_in_dt):
        changes["punch_in"] = {"from": ms_or_none(old_punch_in), "to": ms_or_none(new_punch_in_dt)}
    if not dt_eq(old_punch_out, new_punch_out_dt):
        changes["punch_out"] = {"from": ms_or_none(old_punch_out), "to": ms_or_none(new_punch_out_dt)}
    if record.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if record.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if record.get("half_day", False) != new_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(422, detail=[{"msg": "No fields would change", "loc": ["body"]}])

    # Convert API change values to DB storage format (BSON dates for punch_in/punch_out)
    def to_db_change_val(field, val):
        if field in ("punch_in", "punch_out"):
            if val is None:
                return None
            if isinstance(val, int):
                return ms_to_dt(val)
            return val
        return val

    db_changes = {
        field: {
            "from": to_db_change_val(field, cv["from"]),
            "to": to_db_change_val(field, cv["to"]),
        }
        for field, cv in changes.items()
    }

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": db_changes,
    }

    update_set = {
        "status": new_status,
        "punch_in": new_punch_in_dt,
        "punch_out": new_punch_out_dt,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    # Optimistic concurrency: check history hasn't grown since we read it
    current_history_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": update_set,
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if result is None:
        raise HTTPException(409, detail="Concurrent edit detected; please retry")

    return serialize_record(result)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee.
    working_days per R7. present_days per R5-R7 (half day = 0.5, Mon-Fri only).
    Analytics read the stored derived values per DATA_MODEL.md §3.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    year, m, first_day_str, last_day_str = parse_month(month)
    first_day = date_type(year, m, 1)
    last_day = date_type(year, m, calendar.monthrange(year, m)[1])

    wd = working_days_in_range(first_day, last_day, emp["joined_on"])

    records = list(db.attendance_logs.find(
        {"emp_code": emp_code, "date": {"$gte": first_day_str, "$lte": last_day_str}}
    ))

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for rec in records:
        rec_date = date_type.fromisoformat(rec["date"])
        status = rec.get("status", "")
        half_day = rec.get("half_day", False)
        late_minutes = rec.get("late_minutes", 0)
        overtime_minutes = rec.get("overtime_minutes", 0)

        # present_days: Mon-Fri only, presence statuses, read stored half_day (R5-R7)
        if status in PRESENCE_STATUSES and is_working_day(rec_date):
            present_days += 0.5 if half_day else 1.0

        if status == "LEAVE":
            leave_days += 1

        if late_minutes > 0:
            late_count += 1
            total_late_minutes += late_minutes

        total_overtime_minutes += overtime_minutes

    present_days = round_half_up(present_days, 2)
    attendance_pct = round_half_up(present_days / wd * 100, 2) if wd > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month. One item per department sorted by department ascending.
    R9: headcount = employees with joined_on <= last day of month (even with 0 logs).
    """
    year, m, first_day_str, last_day_str = parse_month(month)
    first_day = date_type(year, m, 1)
    last_day = date_type(year, m, calendar.monthrange(year, m)[1])

    emp_filter: dict = {"joined_on": {"$lte": last_day_str}}
    if department:
        emp_filter["department"] = department

    # Build headcount and employee lists per department
    dept_headcount: dict = {}
    dept_employees: dict = {}
    emp_joined: dict = {}
    for emp in db.employees.find(emp_filter):
        dept = emp["department"]
        ec = emp["emp_code"]
        dept_headcount[dept] = dept_headcount.get(dept, 0) + 1
        dept_employees.setdefault(dept, []).append(ec)
        emp_joined[ec] = emp["joined_on"]

    result_items = []
    for dept in sorted(dept_headcount.keys()):
        headcount = dept_headcount[dept]
        emp_codes = dept_employees[dept]

        logs = list(db.attendance_logs.find({
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": first_day_str, "$lte": last_day_str},
        }))

        present_days = 0.0
        work_hours_sum = 0.0
        work_hours_count = 0
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0

        for rec in logs:
            rec_date = date_type.fromisoformat(rec["date"])
            status = rec.get("status", "")
            half_day = rec.get("half_day", False)
            late_mins = rec.get("late_minutes", 0)
            wh = rec.get("work_hours")
            ec = rec["emp_code"]

            if status in PRESENCE_STATUSES and is_working_day(rec_date):
                joined = emp_joined.get(ec)
                if joined and rec["date"] >= joined:
                    present_days += 0.5 if half_day else 1.0

            if status == "LEAVE":
                leave_count += 1
            if status == "ON_DUTY":
                on_duty_count += 1
            if late_mins > 0:
                late_count += 1
                total_late_minutes += late_mins
            # avg_work_hours: mean over presence-status records with work_hours value
            if status in PRESENCE_STATUSES and wh is not None:
                work_hours_sum += wh
                work_hours_count += 1

        avg_work_hours = round_half_up(work_hours_sum / work_hours_count, 2) if work_hours_count > 0 else None
        present_days = round_half_up(present_days, 2)

        result_items.append({
            "department": dept,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": result_items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month. Standard competition ranking.
    Ties share a rank; limit applies after ranking (all employees with rank <= limit included).
    """
    year, m, first_day_str, last_day_str = parse_month(month)

    match_stage: dict = {
        "date": {"$gte": first_day_str, "$lte": last_day_str},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_stage},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Assign standard competition ranking; return all rows with rank <= limit
    items = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        row["rank"] = rank
        if rank <= limit:
            items.append(row)
        else:
            break

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    """
    Daily attendance trend for a department.
    Returns exactly one row per calendar day from 'from' to 'to' inclusive.
    Uses Python gap-filling for compatibility across MongoDB 6.x versions.
    R9 headcount per day; 7-day moving average of non-null attendance_rates.
    """
    try:
        from_date = date_type.fromisoformat(from_)
    except ValueError:
        raise HTTPException(422, detail=[{"msg": "from is not a valid date", "loc": ["query", "from"]}])
    try:
        to_date = date_type.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, detail=[{"msg": "to is not a valid date", "loc": ["query", "to"]}])

    if to_date < from_date:
        raise HTTPException(422, detail=[{"msg": "to must be >= from", "loc": ["query", "to"]}])

    days_range = (to_date - from_date).days + 1
    if days_range > 92:
        raise HTTPException(422, detail=[{"msg": "date range must not exceed 92 days", "loc": ["query", "to"]}])

    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, detail=f"Department '{department}' not found")

    from_str = from_date.isoformat()
    to_str = to_date.isoformat()

    all_dept_emps = list(db.employees.find(
        {"department": department},
        {"emp_code": 1, "joined_on": 1, "_id": 0}
    ))
    emp_codes = [e["emp_code"] for e in all_dept_emps]

    logs = list(db.attendance_logs.find({
        "emp_code": {"$in": emp_codes},
        "date": {"$gte": from_str, "$lte": to_str},
    }))

    # Build per-day data
    day_data: dict = {}
    for log in logs:
        d = log["date"]
        if d not in day_data:
            day_data[d] = {"present_count": 0.0, "late_count": 0}
        status = log.get("status", "")
        half_day = log.get("half_day", False)
        late_mins = log.get("late_minutes", 0)

        if status in PRESENCE_STATUSES:
            day_data[d]["present_count"] += 0.5 if half_day else 1.0
        if late_mins > 0:
            day_data[d]["late_count"] += 1

    items = []
    attendance_rates = []

    current = from_date
    while current <= to_date:
        d_str = current.isoformat()
        is_wd = is_working_day(current)

        # R9: headcount = employees with joined_on <= this day
        headcount = sum(1 for e in all_dept_emps if e["joined_on"] <= d_str)

        ddata = day_data.get(d_str, {"present_count": 0.0, "late_count": 0})
        present_count = round_half_up(ddata["present_count"], 2)
        late_count = ddata["late_count"]

        # attendance_rate: only on working days with headcount > 0
        if is_wd and headcount > 0:
            rate = round_half_up(present_count / headcount, 4)
        else:
            rate = None

        attendance_rates.append(rate)
        items.append({
            "date": d_str,
            "is_working_day": is_wd,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": None,
        })
        current += timedelta(days=1)

    # Compute 7-day moving average (current + up to 6 preceding, non-null rates only)
    for i, item in enumerate(items):
        window_start = max(0, i - 6)
        window_rates = [attendance_rates[j] for j in range(window_start, i + 1) if attendance_rates[j] is not None]
        item["moving_avg_7d"] = round_half_up(sum(window_rates) / len(window_rates), 4) if window_rates else None

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin: explain endpoint
# ---------------------------------------------------------------------------
VALID_EXPLAIN_ENDPOINTS = frozenset({
    "attendance_list", "employee_monthly", "department_summary",
    "late_leaderboard", "department_trend"
})

MONTH_RE_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=MONTH_RE_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Run explain with executionStats for the named endpoint's main query.
    Returns {endpoint, collection, explain}. Does not expose credentials or secrets.
    """
    if endpoint not in VALID_EXPLAIN_ENDPOINTS:
        raise HTTPException(422, detail=[{"msg": f"endpoint must be one of {sorted(VALID_EXPLAIN_ENDPOINTS)}", "loc": ["path", "endpoint"]}])

    def clean_explain(obj):
        """Remove non-JSON-serializable MongoDB types from explain output."""
        if isinstance(obj, dict):
            return {k: clean_explain(v) for k, v in obj.items()
                    if not isinstance(v, (bytes, bytearray))}
        elif isinstance(obj, list):
            return [clean_explain(x) for x in obj]
        elif isinstance(obj, datetime):
            return obj.isoformat()
        elif hasattr(obj, '__class__') and obj.__class__.__name__ in (
                'ObjectId', 'Timestamp', 'Int64', 'Decimal128', 'Int32'):
            return str(obj)
        return obj

    try:
        if endpoint == "attendance_list":
            collection = "attendance_logs"
            q: dict = {}
            if emp_code:
                q["emp_code"] = emp_code
            if date_from or date_to:
                q["date"] = {}
                if date_from:
                    q["date"]["$gte"] = date_from
                if date_to:
                    q["date"]["$lte"] = date_to
            if status:
                q["status"] = status
            skip = (page - 1) * page_size
            explain_doc = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": q,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": skip,
                    "limit": page_size,
                },
                verbosity="executionStats",
            )

        elif endpoint == "employee_monthly":
            if not month or not emp_code:
                raise HTTPException(422, detail=[{"msg": "emp_code and month are required for employee_monthly", "loc": ["query"]}])
            _, _, first_day_str, last_day_str = parse_month(month)
            collection = "attendance_logs"
            explain_doc = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": {"emp_code": emp_code, "date": {"$gte": first_day_str, "$lte": last_day_str}},
                },
                verbosity="executionStats",
            )

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(422, detail=[{"msg": "month is required for department_summary", "loc": ["query", "month"]}])
            _, _, first_day_str, last_day_str = parse_month(month)
            collection = "attendance_logs"
            match_filter: dict = {"date": {"$gte": first_day_str, "$lte": last_day_str}}
            pipeline = [
                {"$match": match_filter},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }},
                {"$unwind": "$emp"},
            ]
            if department:
                pipeline.append({"$match": {"emp.department": department}})
            pipeline.append({"$group": {"_id": "$emp.department", "count": {"$sum": 1}}})
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(422, detail=[{"msg": "month is required for late_leaderboard", "loc": ["query", "month"]}])
            _, _, first_day_str, last_day_str = parse_month(month)
            collection = "attendance_logs"
            match_stage = {
                "date": {"$gte": first_day_str, "$lte": last_day_str},
                "late_minutes": {"$gt": 0},
            }
            pipeline = [
                {"$match": match_stage},
                {"$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }},
                {"$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }},
                {"$unwind": "$emp"},
            ]
            if department:
                pipeline.append({"$match": {"emp.department": department}})
            pipeline.append({"$sort": {"total_late_minutes": -1, "_id": 1}})
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

        elif endpoint == "department_trend":
            if not from_ or not to or not department:
                raise HTTPException(422, detail=[{"msg": "department, from, and to are required for department_trend", "loc": ["query"]}])
            # Get emp_codes for the department to explain the actual query
            emp_codes_for_dept = [
                e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})
            ]
            collection = "attendance_logs"
            explain_doc = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": {
                        "emp_code": {"$in": emp_codes_for_dept},
                        "date": {"$gte": from_, "$lte": to},
                    },
                },
                verbosity="executionStats",
            )

        else:
            raise HTTPException(422, detail=[{"msg": "Unknown endpoint", "loc": ["path", "endpoint"]}])

        return {
            "endpoint": endpoint,
            "collection": collection,
            "explain": clean_explain(explain_doc),
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail="Explain operation failed") from exc
