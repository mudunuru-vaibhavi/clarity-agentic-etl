"""Reference data for "Cedar Valley Health", a fictional health system.

ICD-10, CPT and CARC codes are public code numbers; the descriptions are short paraphrases.
Names, payors, departments and every number here are made up.
"""

from __future__ import annotations

from dataclasses import dataclass

SERVICE_AREA = "Cedar Valley Health"

ENC_TYPES = {101: "Office Visit", 76: "Telemedicine", 51: "Emergency", 3: "Hospital Encounter"}
APPT_STATUSES = {1: "Scheduled", 2: "Completed", 3: "Canceled", 4: "No Show"}
FIN_CLASSES = {1: "Commercial", 2: "Medicare", 3: "Medicaid", 4: "Self-Pay", 5: "Workers Comp"}
BILL_STATUSES = {1: "Not Billed", 2: "Billed", 3: "Closed", 4: "Denial Rework", 5: "Bad Debt"}

# PROC_ID: (code, name, base charge)
PROCEDURES: dict[int, tuple[str, str, float]] = {
    1001: ("99203", "New patient office visit, low complexity", 210.00),
    1002: ("99213", "Established patient office visit, low complexity", 165.00),
    1003: ("99214", "Established patient office visit, moderate complexity", 245.00),
    1004: ("99283", "Emergency department visit, moderate", 620.00),
    1005: ("99285", "Emergency department visit, high", 1850.00),
    1006: ("99213-95", "Telehealth office visit", 140.00),
    1007: ("93000", "Electrocardiogram with interpretation", 95.00),
    1008: ("71046", "Chest X-ray, 2 views", 310.00),
    1009: ("80053", "Comprehensive metabolic panel", 85.00),
    1010: ("85025", "Complete blood count", 45.00),
    1011: ("36415", "Venipuncture", 20.00),
    1012: ("90471", "Immunization administration", 40.00),
    1013: ("73610", "Ankle X-ray, 3 views", 240.00),
    1014: ("77067", "Screening mammography", 360.00),
    1015: ("90834", "Psychotherapy, 45 minutes", 180.00),
    1016: ("47562", "Laparoscopic cholecystectomy", 9800.00),
    1017: ("96413", "Chemotherapy infusion, first hour", 1450.00),
    1018: ("59400", "Routine obstetric care package", 4200.00),
    1019: ("99223", "Initial hospital care, high complexity", 520.00),
    1020: ("ROOM01", "Inpatient room and board, per day", 2800.00),
    1021: ("93306", "Echocardiogram, complete", 1150.00),
    1022: ("74177", "CT abdomen and pelvis with contrast", 2400.00),
    1023: ("81002", "Urinalysis, non-automated", 25.00),
    1024: ("20610", "Joint injection, major joint", 290.00),
}
TELEHEALTH_PROC = 1006
ROOM_PROC = 1020
ADMIT_PROC = 1019

# DX_ID: (name, ICD-10 code)
DIAGNOSES: dict[int, tuple[str, str]] = {
    2001: ("Type 2 diabetes without complications", "E11.9"),
    2002: ("Essential hypertension", "I10"),
    2003: ("Acute upper respiratory infection", "J06.9"),
    2004: ("Low back pain", "M54.50"),
    2005: ("Adult general medical exam", "Z00.00"),
    2006: ("Generalized anxiety disorder", "F41.1"),
    2007: ("Chest pain, unspecified", "R07.9"),
    2008: ("Asthma, uncomplicated", "J45.909"),
    2009: ("Urinary tract infection", "N39.0"),
    2010: ("Malignant neoplasm of breast", "C50.911"),
    2011: ("Ankle sprain, initial encounter", "S93.401A"),
    2012: ("Gastro-esophageal reflux disease", "K21.9"),
    2013: ("Hyperlipidemia", "E78.5"),
    2014: ("Encounter for immunization", "Z23"),
    2015: ("Routine child health exam", "Z00.129"),
    2016: ("Supervision of normal pregnancy", "Z34.90"),
    2017: ("Calculus of gallbladder", "K80.20"),
    2018: ("Atrial fibrillation", "I48.91"),
    2019: ("Major depressive disorder, single episode", "F32.9"),
    2020: ("Osteoarthritis of knee", "M17.9"),
    2021: ("Abdominal pain, unspecified", "R10.9"),
    2022: ("Encounter for screening mammogram", "Z12.31"),
    2023: ("Heart failure, unspecified", "I50.9"),
}


@dataclass(frozen=True)
class Department:
    id: int
    name: str
    specialty: str
    location: str
    enc_type: int
    weekday_weight: float
    weekend_weight: float
    em_procs: tuple[int, ...]
    ancillary_procs: tuple[int, ...]
    diagnoses: tuple[int, ...]
    telehealth_share: float = 0.0
    providers: int = 3


HOSPITAL = "Cedar Valley Medical Center"
NORTHGATE = "Northgate Clinic"
LAKESIDE = "Lakeside Clinic"

DEPARTMENTS: list[Department] = [
    Department(10101, "CVMC EMERGENCY", "Emergency Medicine", HOSPITAL, 51, 22, 55,
               (1004, 1005), (1007, 1008, 1009, 1010, 1011, 1022, 1023, 1013),
               (2007, 2021, 2003, 2011, 2009, 2018, 2008), providers=6),
    Department(10102, "CVMC INPATIENT MEDICINE", "Hospital Medicine", HOSPITAL, 3, 3, 4,
               (1019,), (1009, 1010, 1008, 1021), (2023, 2018, 2007, 2009), providers=4),
    Department(10103, "CVMC GENERAL SURGERY", "General Surgery", HOSPITAL, 101, 3, 0.5,
               (1002,), (1016, 1022), (2017, 2021), providers=2),
    Department(10104, "CVMC ONCOLOGY INFUSION", "Oncology", HOSPITAL, 101, 4, 0,
               (1003,), (1017, 1010, 1009), (2010,), providers=2),
    Department(10105, "CVMC RADIOLOGY", "Radiology", HOSPITAL, 101, 6, 2,
               (), (1014, 1008, 1022), (2022, 2007, 2021), providers=2),
    Department(20101, "NORTHGATE FAMILY MEDICINE", "Family Medicine", NORTHGATE, 101, 22, 2,
               (1001, 1002, 1003), (1009, 1010, 1011, 1012, 1023),
               (2001, 2002, 2003, 2004, 2005, 2012, 2013, 2014, 2009),
               telehealth_share=0.15, providers=6),
    Department(20102, "NORTHGATE PEDIATRICS", "Pediatrics", NORTHGATE, 101, 12, 1,
               (1001, 1002), (1012,), (2015, 2014, 2003, 2008), providers=4),
    Department(20103, "NORTHGATE BEHAVIORAL HEALTH", "Behavioral Health", NORTHGATE, 101, 8, 0,
               (1015,), (), (2006, 2019), telehealth_share=0.40, providers=3),
    Department(30101, "LAKESIDE INTERNAL MEDICINE", "Internal Medicine", LAKESIDE, 101, 16, 0,
               (1002, 1003), (1009, 1010, 1011, 1007),
               (2001, 2002, 2013, 2023, 2018, 2012), telehealth_share=0.12, providers=5),
    Department(30102, "LAKESIDE CARDIOLOGY", "Cardiology", LAKESIDE, 101, 7, 0,
               (1003,), (1007, 1021), (2018, 2023, 2002, 2007), providers=3),
    Department(30103, "LAKESIDE ORTHOPEDICS", "Orthopedics", LAKESIDE, 101, 8, 0,
               (1001, 1003), (1013, 1024), (2020, 2004, 2011), providers=3),
    Department(30104, "LAKESIDE OB/GYN", "Obstetrics & Gynecology", LAKESIDE, 101, 7, 0,
               (1002,), (1023, 1014, 1018), (2016, 2022, 2009), providers=3),
]


@dataclass(frozen=True)
class Payor:
    id: int
    name: str
    fin_class: int
    pay_rate: float  # share of charges paid on a clean claim
    lag_days: tuple[int, int]  # days from bill drop to remittance
    denial_rate: float


PAYORS: list[Payor] = [
    Payor(1, "MEDICARE PART A/B", 2, 0.36, (14, 28), 0.05),
    Payor(2, "SUNRISE MEDICARE ADVANTAGE", 2, 0.40, (20, 40), 0.11),
    Payor(3, "STATE MEDICAID", 3, 0.28, (25, 55), 0.09),
    Payor(4, "HARBOR COMMUNITY MEDICAID PLAN", 3, 0.30, (25, 50), 0.12),
    Payor(5, "BLUE MERIDIAN PPO", 1, 0.58, (18, 35), 0.07),
    Payor(6, "NORTHSTAR HEALTH PLAN", 1, 0.55, (20, 40), 0.10),
    Payor(7, "APEX COMMERCIAL HMO", 1, 0.50, (20, 45), 0.14),
    Payor(8, "WORKERS COMP MUTUAL", 5, 0.65, (30, 60), 0.08),
    Payor(9, "SELF-PAY", 4, 0.55, (25, 80), 0.0),
]
SELF_PAY_ID = 9

# Claim Adjustment Reason Codes (public X12 codes, paraphrased descriptions) and their weight.
DENIAL_REASONS: list[tuple[str, str, float]] = [
    ("CO-16", "Claim missing information needed for adjudication", 0.26),
    ("CO-197", "Prior authorization absent", 0.20),
    ("CO-50", "Not deemed medically necessary", 0.15),
    ("CO-11", "Diagnosis inconsistent with procedure", 0.12),
    ("CO-27", "Service after coverage ended", 0.10),
    ("CO-18", "Duplicate claim", 0.09),
    ("CO-29", "Timely filing limit expired", 0.08),
]

FIRST_NAMES = (
    "Avery Jordan Riley Morgan Casey Quinn Harper Rowan Emerson Parker Reese Skyler "
    "Dakota Finley Hayden Sage Logan Blake Cameron Drew Elliot Jamie Kendall Micah "
    "Noel Peyton Remy Shawn Taylor Alexis Blair Charlie Devon Frankie Gray Jessie "
    "Kai Lane Marley Oakley"
).split()
LAST_NAMES = (
    "Ashford Bellamy Carver Delacroix Ellison Fairbanks Galloway Hartwell Ingram "
    "Jansen Kettering Lockhart Merriweather Northcott Okafor Pemberton Quintero "
    "Ravensworth Sterling Thornbury Underhill Vasquez Whitlock Yardley Zimmerman "
    "Abernathy Blackwood Castellan Dunmore Everhart Fenwick Greystone Holloway "
    "Ivanova Jakobsen Kowalczyk Lindqvist Moreau Nakamura Oduya"
).split()
CITIES: list[tuple[str, str]] = [
    ("COLUMBUS", "43215"), ("DUBLIN", "43017"), ("WESTERVILLE", "43081"),
    ("GROVE CITY", "43123"), ("HILLIARD", "43026"), ("DELAWARE", "43015"),
    ("NEWARK", "43055"), ("LANCASTER", "43130"),
]
STATE = "OH"
PROV_TYPES = ("Physician", "Physician", "Physician", "Nurse Practitioner", "Physician Assistant")
