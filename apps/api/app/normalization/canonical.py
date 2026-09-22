"""The canonical institutional data model.

Every institution keeps its own column names. This module is the single place
that says what the platform's own entities, fields, and types are. The mapping
engine proposes header-to-field mappings against it, the validator checks
values against its field types, and the institution data store derives its
tables from it, so the three can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FieldType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    PERCENT = "percent"
    PHONE = "phone"
    EMAIL = "email"
    IDENTIFIER = "identifier"


@dataclass(frozen=True, slots=True)
class CanonicalField:
    name: str
    field_type: FieldType
    description: str
    synonyms: tuple[str, ...] = ()
    required: bool = False
    pii: bool = False
    contact: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("canonical field name must not be blank")
        object.__setattr__(self, "synonyms", tuple(dict.fromkeys(item.lower() for item in self.synonyms)))


@dataclass(frozen=True, slots=True)
class CanonicalEntity:
    name: str
    table: str
    description: str
    natural_key: tuple[str, ...]
    fields: tuple[CanonicalField, ...]
    # Headers that strongly indicate this entity when detecting what a file holds.
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        names = [item.name for item in self.fields]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate field in canonical entity {self.name}")
        for key in self.natural_key:
            if key not in names:
                raise ValueError(f"natural key {key} is not a field of {self.name}")

    def field(self, name: str) -> CanonicalField:
        for item in self.fields:
            if item.name == name:
                return item
        raise KeyError(f"unknown canonical field: {self.name}.{name}")

    def field_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields)

    def required_fields(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields if item.required)

    def contact_fields(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields if item.contact)


def _f(name: str, field_type: FieldType, description: str, *synonyms: str, required: bool = False, pii: bool = False, contact: bool = False) -> CanonicalField:
    return CanonicalField(name=name, field_type=field_type, description=description, synonyms=synonyms, required=required, pii=pii, contact=contact)


_STUDENT_ID = ("student id", "student_id", "usn", "roll no", "roll number", "roll", "register number", "reg no", "registration number", "regno", "enrollment no", "enrolment no", "enrollment number", "admission no", "admission number", "prn", "sid", "student number", "id")
_PROGRAM = ("program", "programme", "course", "degree", "program code", "programme code", "course name", "stream")
_SEMESTER = ("semester", "sem", "term", "current semester", "semester no")
_DEPARTMENT = ("department", "dept", "branch", "dept name", "department name")

STUDENT = CanonicalEntity(
    name="student",
    table="students",
    description="A student enrolled in the institution",
    natural_key=("student_id",),
    signals=("usn", "roll no", "roll number", "student name", "student id", "register number", "admission no", "prn"),
    fields=(
        _f("student_id", FieldType.IDENTIFIER, "Institution-issued student identifier", *_STUDENT_ID, required=True),
        _f("name", FieldType.STRING, "Full name", "student name", "name", "full name", "candidate name", "name of student", "name of the student", required=True, pii=True),
        _f("first_name", FieldType.STRING, "Given name", "first name", "given name", pii=True),
        _f("last_name", FieldType.STRING, "Family name", "last name", "surname", "family name", pii=True),
        _f("program", FieldType.STRING, "Program or degree code", *_PROGRAM),
        _f("department", FieldType.STRING, "Department", *_DEPARTMENT),
        _f("semester", FieldType.INTEGER, "Current semester", *_SEMESTER),
        _f("section", FieldType.STRING, "Section or division", "section", "sec", "div", "division", "class"),
        _f("batch", FieldType.STRING, "Admission batch or academic year label", "batch", "academic year", "admission batch", "year of study", "year"),
        _f("admission_year", FieldType.INTEGER, "Year of admission", "admission year", "year of admission", "joining year", "year of joining"),
        _f("date_of_birth", FieldType.DATE, "Date of birth", "dob", "date of birth", "birth date", "birthdate", pii=True, contact=True),
        _f("gender", FieldType.STRING, "Gender", "gender", "sex", pii=True),
        _f("phone", FieldType.PHONE, "Student mobile number", "phone", "mobile", "mobile no", "mobile number", "contact", "contact no", "contact number", "phone number", "phone no", "student mobile", "cell", pii=True, contact=True),
        _f("email", FieldType.EMAIL, "Student email", "email", "e-mail", "email id", "mail id", "email address", "student email", pii=True, contact=True),
        _f("guardian_name", FieldType.STRING, "Parent or guardian name", "father name", "father's name", "parent name", "guardian", "guardian name", "mother name", "mother's name", "parent/guardian", pii=True),
        _f("guardian_phone", FieldType.PHONE, "Parent or guardian phone", "parent phone", "parent mobile", "father mobile", "father phone", "guardian phone", "guardian contact", "parent contact", "mother mobile", pii=True, contact=True),
        _f("address", FieldType.STRING, "Postal address", "address", "permanent address", "residential address", "communication address", pii=True, contact=True),
        _f("category", FieldType.STRING, "Admission or social category", "category", "caste category", "quota", "reservation category", pii=True),
        _f("blood_group", FieldType.STRING, "Blood group", "blood group", "blood grp", pii=True),
        _f("status", FieldType.STRING, "Enrollment status", "status", "student status", "active", "enrollment status"),
    ),
)

FACULTY = CanonicalEntity(
    name="faculty",
    table="faculty",
    description="Teaching faculty member",
    natural_key=("faculty_id",),
    signals=("designation", "faculty name", "faculty id", "qualification", "employee id", "emp id", "specialization"),
    fields=(
        _f("faculty_id", FieldType.IDENTIFIER, "Faculty or employee identifier", "faculty id", "employee id", "emp id", "emp code", "employee code", "staff id", "faculty code", "id", required=True),
        _f("name", FieldType.STRING, "Full name", "faculty name", "name", "employee name", "name of faculty", "full name", required=True, pii=True),
        _f("designation", FieldType.STRING, "Designation", "designation", "position", "post", "title", "rank", "role"),
        _f("department", FieldType.STRING, "Department", *_DEPARTMENT),
        _f("qualification", FieldType.STRING, "Highest qualification", "qualification", "highest qualification", "qualifications", "degree", "education"),
        _f("specialization", FieldType.STRING, "Specialization", "specialization", "specialisation", "area", "area of expertise", "subject"),
        _f("email", FieldType.EMAIL, "Official email", "email", "e-mail", "email id", "mail id", "official email", pii=True, contact=True),
        _f("phone", FieldType.PHONE, "Phone", "phone", "mobile", "mobile no", "contact", "contact no", "phone number", pii=True, contact=True),
        _f("joining_date", FieldType.DATE, "Date of joining", "date of joining", "doj", "joining date", "joined on"),
        _f("experience_years", FieldType.NUMBER, "Years of experience", "experience", "years of experience", "exp", "experience (years)"),
        _f("is_hod", FieldType.BOOLEAN, "Head of department flag", "hod", "head of department", "is hod"),
        _f("status", FieldType.STRING, "Employment status", "status", "employment status", "active"),
    ),
)

STAFF = CanonicalEntity(
    name="staff",
    table="staff",
    description="Non-teaching staff member",
    natural_key=("staff_id",),
    signals=("staff name", "staff id", "non teaching", "non-teaching"),
    fields=(
        _f("staff_id", FieldType.IDENTIFIER, "Staff identifier", "staff id", "employee id", "emp id", "emp code", "staff code", "id", required=True),
        _f("name", FieldType.STRING, "Full name", "staff name", "name", "employee name", "full name", required=True, pii=True),
        _f("designation", FieldType.STRING, "Designation", "designation", "position", "post", "role"),
        _f("department", FieldType.STRING, "Department or office", *_DEPARTMENT, "office", "section"),
        _f("email", FieldType.EMAIL, "Email", "email", "e-mail", "email id", "mail id", pii=True, contact=True),
        _f("phone", FieldType.PHONE, "Phone", "phone", "mobile", "mobile no", "contact", "contact no", pii=True, contact=True),
        _f("joining_date", FieldType.DATE, "Date of joining", "date of joining", "doj", "joining date"),
        _f("status", FieldType.STRING, "Employment status", "status", "active"),
    ),
)

PROGRAM = CanonicalEntity(
    name="program",
    table="programs",
    description="Academic program",
    natural_key=("code",),
    signals=("program code", "programme", "intake", "duration", "program name"),
    fields=(
        _f("code", FieldType.IDENTIFIER, "Program code", "program code", "programme code", "code", "program", "programme", "short name", "abbreviation", required=True),
        _f("name", FieldType.STRING, "Program name", "program name", "programme name", "name", "title", "full name", "course name", required=True),
        _f("department", FieldType.STRING, "Owning department", *_DEPARTMENT),
        _f("level", FieldType.STRING, "UG/PG/Diploma level", "level", "degree level", "type"),
        _f("duration_semesters", FieldType.INTEGER, "Duration in semesters", "duration", "semesters", "duration (semesters)", "no of semesters", "total semesters"),
        _f("intake", FieldType.INTEGER, "Sanctioned intake", "intake", "seats", "sanctioned intake", "capacity"),
        _f("status", FieldType.STRING, "Status", "status", "active"),
    ),
)

DEPARTMENT = CanonicalEntity(
    name="department",
    table="departments",
    description="Academic or administrative department",
    natural_key=("code",),
    signals=("department code", "hod", "head of department", "department name"),
    fields=(
        _f("code", FieldType.IDENTIFIER, "Department code", "department code", "dept code", "code", "department", "dept", "short name", required=True),
        _f("name", FieldType.STRING, "Department name", "department name", "dept name", "name", "title", required=True),
        _f("hod_id", FieldType.IDENTIFIER, "HOD faculty identifier", "hod id", "hod faculty id", "head id"),
        _f("hod_name", FieldType.STRING, "HOD name", "hod", "hod name", "head of department", "head", pii=True),
        _f("hod_email", FieldType.EMAIL, "HOD email", "hod email", "head email", pii=True, contact=True),
        _f("status", FieldType.STRING, "Status", "status", "active"),
    ),
)

COURSE = CanonicalEntity(
    name="course",
    table="courses",
    description="A course or subject taught within a program",
    natural_key=("code",),
    signals=("subject code", "course code", "credits", "subject name"),
    fields=(
        _f("code", FieldType.IDENTIFIER, "Course/subject code", "course code", "subject code", "code", "paper code", required=True),
        _f("name", FieldType.STRING, "Course/subject name", "course name", "subject name", "subject", "name", "title", "paper", required=True),
        _f("program", FieldType.STRING, "Program", "program", "programme", "degree", "stream"),
        _f("department", FieldType.STRING, "Department", *_DEPARTMENT),
        _f("semester", FieldType.INTEGER, "Semester", *_SEMESTER),
        _f("credits", FieldType.NUMBER, "Credits", "credits", "credit", "credit points"),
        _f("faculty_id", FieldType.IDENTIFIER, "Assigned faculty", "faculty id", "faculty", "instructor", "teacher", "handled by", "employee id"),
        _f("course_type", FieldType.STRING, "Core/elective", "type", "course type", "category"),
    ),
)

ATTENDANCE = CanonicalEntity(
    name="attendance",
    table="attendance",
    description="Attendance for a student in a course or period",
    natural_key=("student_id", "course_code", "period"),
    signals=("attendance", "classes held", "classes attended", "present", "absent", "attendance %", "attendance percentage", "total classes"),
    fields=(
        _f("student_id", FieldType.IDENTIFIER, "Student identifier", *_STUDENT_ID, required=True),
        _f("student_name", FieldType.STRING, "Student name as recorded", "student name", "name", "name of student", pii=True),
        _f("program", FieldType.STRING, "Program", "program", "programme", "degree", "stream", "programme name"),
        _f("department", FieldType.STRING, "Department", *_DEPARTMENT),
        _f("semester", FieldType.INTEGER, "Semester", *_SEMESTER),
        _f("section", FieldType.STRING, "Section", "section", "sec", "division"),
        _f("course_code", FieldType.STRING, "Course/subject code", "course code", "subject code", "subject", "course", "paper", "subject name"),
        _f("period", FieldType.STRING, "Reporting period (month, term, or date range)", "period", "month", "term", "for the month", "reporting period", "academic year", "as on"),
        _f("classes_held", FieldType.INTEGER, "Classes conducted", "classes held", "classes conducted", "total classes", "held", "conducted", "total", "no of classes", "classes", "total hours", "hours conducted"),
        _f("classes_attended", FieldType.INTEGER, "Classes attended", "classes attended", "attended", "present", "no of classes attended", "hours attended", "attended classes"),
        _f("classes_absent", FieldType.INTEGER, "Classes missed", "absent", "classes absent", "missed"),
        _f("attendance_percent", FieldType.PERCENT, "Attendance percentage", "attendance %", "attendance percentage", "percentage", "%", "attendance", "att %", "att%", "percent", "attendance(%)"),
        _f("recorded_on", FieldType.DATE, "Date the figure was recorded", "date", "recorded on", "as on date", "updated on"),
    ),
)

FEE = CanonicalEntity(
    name="fee",
    table="fees",
    description="Fee dues and payments for a student",
    natural_key=("student_id", "fee_type", "academic_year", "semester", "receipt_no"),
    signals=("fee", "fees", "amount paid", "balance", "receipt", "due date", "total fee", "paid", "outstanding"),
    fields=(
        _f("student_id", FieldType.IDENTIFIER, "Student identifier", *_STUDENT_ID, required=True),
        _f("student_name", FieldType.STRING, "Student name as recorded", "student name", "name", "name of student", pii=True),
        _f("program", FieldType.STRING, "Program", "program", "programme", "course", "degree", "stream"),
        _f("semester", FieldType.INTEGER, "Semester", *_SEMESTER),
        _f("academic_year", FieldType.STRING, "Academic year", "academic year", "year", "session", "ay"),
        _f("fee_type", FieldType.STRING, "Fee head", "fee type", "fee head", "type", "particulars", "head", "fee category", "description", "fee name"),
        _f("amount_due", FieldType.NUMBER, "Total amount due", "total fee", "fee amount", "amount due", "fees", "total", "amount", "total amount", "fee", "payable", "demand"),
        _f("amount_paid", FieldType.NUMBER, "Amount paid", "paid", "amount paid", "received", "paid amount", "collected"),
        _f("balance", FieldType.NUMBER, "Outstanding balance", "balance", "due", "pending", "outstanding", "balance due", "pending amount", "dues", "remaining"),
        _f("due_date", FieldType.DATE, "Due date", "due date", "last date", "deadline"),
        _f("paid_on", FieldType.DATE, "Payment date", "payment date", "paid on", "date of payment", "date", "receipt date"),
        _f("receipt_no", FieldType.STRING, "Receipt or transaction reference", "receipt", "receipt no", "receipt number", "transaction id", "txn id", "reference no", "utr"),
        _f("payment_mode", FieldType.STRING, "Payment mode", "mode", "payment mode", "mode of payment"),
        _f("status", FieldType.STRING, "Payment status", "status", "payment status", "fee status"),
    ),
)

EXAM = CanonicalEntity(
    name="exam",
    table="exams",
    description="Exam or assessment result",
    natural_key=("student_id", "course_code", "exam_name"),
    signals=("marks", "grade", "result", "max marks", "exam", "obtained", "cgpa", "sgpa", "internal"),
    fields=(
        _f("student_id", FieldType.IDENTIFIER, "Student identifier", *_STUDENT_ID, required=True),
        _f("student_name", FieldType.STRING, "Student name as recorded", "student name", "name", "name of student", pii=True),
        _f("program", FieldType.STRING, "Program", "program", "programme", "degree", "stream"),
        _f("semester", FieldType.INTEGER, "Semester", *_SEMESTER),
        _f("course_code", FieldType.STRING, "Course/subject code", "course code", "subject code", "subject", "course", "paper", "paper code"),
        _f("course_name", FieldType.STRING, "Course/subject name", "course name", "subject name", "paper name"),
        _f("exam_name", FieldType.STRING, "Exam or assessment name", "exam", "exam name", "examination", "test", "assessment", "internal", "ia", "cie", "see", "exam type"),
        _f("marks_obtained", FieldType.NUMBER, "Marks obtained", "marks", "marks obtained", "obtained", "score", "obtained marks", "total marks obtained"),
        _f("max_marks", FieldType.NUMBER, "Maximum marks", "max marks", "maximum marks", "out of", "total marks", "max"),
        _f("grade", FieldType.STRING, "Grade", "grade", "letter grade", "grade point"),
        _f("result_status", FieldType.STRING, "Pass/fail status", "result", "pass/fail", "status", "result status", "p/f"),
        _f("exam_date", FieldType.DATE, "Exam date", "exam date", "date", "date of exam"),
    ),
)

ENROLLMENT = CanonicalEntity(
    name="enrollment",
    table="enrollments",
    description="A student's enrollment in a program for a term",
    natural_key=("student_id", "academic_year", "semester"),
    signals=("enrollment", "enrolment", "registered", "registration"),
    fields=(
        _f("student_id", FieldType.IDENTIFIER, "Student identifier", *_STUDENT_ID, required=True),
        _f("program", FieldType.STRING, "Program", *_PROGRAM),
        _f("semester", FieldType.INTEGER, "Semester", *_SEMESTER),
        _f("academic_year", FieldType.STRING, "Academic year", "academic year", "year", "session", "ay"),
        _f("section", FieldType.STRING, "Section", "section", "sec", "division"),
        _f("status", FieldType.STRING, "Enrollment status", "status", "enrollment status", "registration status"),
    ),
)

ADMISSION = CanonicalEntity(
    name="admission",
    table="admissions",
    description="Admission application",
    natural_key=("application_id",),
    signals=("application", "applicant", "admission status", "counselling", "seat"),
    fields=(
        _f("application_id", FieldType.IDENTIFIER, "Application identifier", "application id", "application no", "application number", "app id", "app no", "form no", "id", required=True),
        _f("name", FieldType.STRING, "Applicant name", "applicant name", "name", "candidate name", "student name", required=True, pii=True),
        _f("program", FieldType.STRING, "Program applied", *_PROGRAM, "program applied", "course applied"),
        _f("status", FieldType.STRING, "Application status", "status", "admission status", "application status", "stage"),
        _f("applied_on", FieldType.DATE, "Application date", "date", "application date", "applied on", "submitted on"),
        _f("phone", FieldType.PHONE, "Applicant phone", "phone", "mobile", "contact", "mobile no", pii=True, contact=True),
        _f("email", FieldType.EMAIL, "Applicant email", "email", "e-mail", "email id", pii=True, contact=True),
        _f("category", FieldType.STRING, "Category", "category", "quota", "reservation", pii=True),
        _f("entrance_score", FieldType.NUMBER, "Entrance exam score", "score", "entrance score", "rank", "cet rank", "merit score"),
        _f("academic_year", FieldType.STRING, "Academic year", "academic year", "year", "session", "ay"),
    ),
)

EVENT = CanonicalEntity(
    name="event",
    table="events",
    description="Institutional event",
    natural_key=("title", "event_date"),
    signals=("event", "venue", "organizer", "organiser", "event date", "seminar", "workshop"),
    fields=(
        _f("title", FieldType.STRING, "Event title", "event", "event name", "title", "name", "program name", "activity", required=True),
        _f("event_date", FieldType.DATE, "Event date", "date", "event date", "on", "start date", "from", required=True),
        _f("end_date", FieldType.DATE, "End date", "end date", "to", "till"),
        _f("category", FieldType.STRING, "Category", "type", "category", "event type", "nature"),
        _f("organizer", FieldType.STRING, "Organizer", "organizer", "organiser", "organized by", "organised by", "department", "dept", "club"),
        _f("venue", FieldType.STRING, "Venue", "venue", "location", "place", "hall"),
        _f("description", FieldType.STRING, "Description", "description", "details", "remarks", "about"),
        _f("participants", FieldType.INTEGER, "Participant count", "participants", "no of participants", "attendees", "count"),
    ),
)

CANONICAL_ENTITIES: dict[str, CanonicalEntity] = {
    entity.name: entity
    for entity in (STUDENT, FACULTY, STAFF, PROGRAM, DEPARTMENT, COURSE, ATTENDANCE, FEE, EXAM, ENROLLMENT, ADMISSION, EVENT)
}

# Common institutional program aliases resolved during cleaning. Keys are
# normalized (lowercase, alphanumeric only).
PROGRAM_ALIASES: dict[str, str] = {
    "mba": "MBA", "masterofbusinessadministration": "MBA", "mbaprogram": "MBA", "mbaprogramme": "MBA",
    "bba": "BBA", "bachelorofbusinessadministration": "BBA",
    "bca": "BCA", "bachelorofcomputerapplications": "BCA", "bachelorofcomputerapplication": "BCA",
    "mca": "MCA", "masterofcomputerapplications": "MCA", "masterofcomputerapplication": "MCA",
    "be": "BE", "bachelorofengineering": "BE", "btech": "BTECH", "bachelooftechnology": "BTECH", "bacheloroftechnology": "BTECH",
    "mtech": "MTECH", "masteroftechnology": "MTECH", "me": "ME", "masterofengineering": "ME",
    "bsc": "BSC", "bachelorofscience": "BSC", "msc": "MSC", "masterofscience": "MSC",
    "bcom": "BCOM", "bachelorofcommerce": "BCOM", "mcom": "MCOM", "masterofcommerce": "MCOM",
    "ba": "BA", "bachelorofarts": "BA", "ma": "MA", "masterofarts": "MA",
    "bed": "BED", "bachelorofeducation": "BED", "med": "MED", "masterofeducation": "MED",
    "llb": "LLB", "bachelooflaws": "LLB", "bacheloroflaws": "LLB", "llm": "LLM",
    "mbbs": "MBBS", "bds": "BDS", "bpharm": "BPHARM", "bachelorofpharmacy": "BPHARM", "mpharm": "MPHARM",
    "barch": "BARCH", "bachelorofarchitecture": "BARCH", "phd": "PHD", "doctorofphilosophy": "PHD",
    "diploma": "DIPLOMA", "puc": "PUC", "preuniversity": "PUC",
}


def entity(name: str) -> CanonicalEntity:
    try:
        return CANONICAL_ENTITIES[name]
    except KeyError as exc:
        raise KeyError(f"unknown canonical entity: {name}") from exc


__all__ = [
    "ADMISSION", "ATTENDANCE", "CANONICAL_ENTITIES", "COURSE", "DEPARTMENT", "ENROLLMENT", "EVENT", "EXAM",
    "FACULTY", "FEE", "PROGRAM", "PROGRAM_ALIASES", "STAFF", "STUDENT", "CanonicalEntity", "CanonicalField",
    "FieldType", "entity",
]
