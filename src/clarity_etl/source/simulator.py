"""Day-by-day simulation of a Clarity database for a fictional health system.

Each simulated day produces the rows that Clarity would send in that night's incremental
extract: new and changed patients, the day's encounters, diagnoses and orders, charges,
payments, adjustments and payer remittances, plus every hospital account whose balance
changed. Day one also carries a full load of lookups, dimensions and the existing
patient panel.

The simulation is deterministic for a given seed, so tests and demos are repeatable.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import reference as ref

Row = dict[str, object]


@dataclass
class DayExtract:
    extract_date: date
    tables: dict[str, list[Row]] = field(default_factory=lambda: defaultdict(list))

    @property
    def row_count(self) -> int:
        return sum(len(rows) for rows in self.tables.values())


def _money(x: float) -> float:
    return round(x, 2) + 0.0  # + 0.0 turns -0.0 into 0.0


class ClaritySimulator:
    def __init__(
        self,
        start: date,
        end: date,
        seed: int = 42,
        base_patients: int = 4000,
        weekday_visits: int = 170,
    ) -> None:
        if end < start:
            raise ValueError("end must be on or after start")
        self.start, self.end = start, end
        self.rng = random.Random(seed)
        self.base_patients = base_patients
        self.weekday_visits = weekday_visits

        self.payors = {p.id: p for p in ref.PAYORS}
        self.patients: dict[str, Row] = {}
        self.patient_payor: dict[str, int] = {}
        self.providers: dict[int, list[str]] = {}
        self.accounts: dict[int, Row] = {}
        self.events: dict[date, list[Callable[[date, DayExtract], None]]] = defaultdict(list)
        self._touched: set[int] = set()
        self._ids = {"pat": 100000, "mrn": 5000000, "csn": 700000000, "har": 3100000,
                     "order": 880000000, "tx": 410000000, "remit": 9200000}

    # ---- helpers ------------------------------------------------------------------------
    def _next(self, key: str) -> int:
        self._ids[key] += 1
        return self._ids[key]

    def _stamp(self, d: date) -> datetime:
        """Clarity UPDATE_DATE: rows change during the day and are extracted overnight."""
        return datetime.combine(d, time(self.rng.randint(6, 22), self.rng.randint(0, 59)))

    def _schedule(self, d: date, fn: Callable[[date, DayExtract], None]) -> None:
        if d <= self.end:
            self.events[d].append(fn)

    # ---- full-load data -----------------------------------------------------------------
    def _emit_reference(self, out: DayExtract) -> None:
        t = out.tables
        t["ZC_DISP_ENC_TYPE"] = [{"DISP_ENC_TYPE_C": k, "NAME": v} for k, v in ref.ENC_TYPES.items()]
        t["ZC_APPT_STATUS"] = [{"APPT_STATUS_C": k, "NAME": v} for k, v in ref.APPT_STATUSES.items()]
        t["ZC_FIN_CLASS"] = [{"FIN_CLASS_C": k, "NAME": v} for k, v in ref.FIN_CLASSES.items()]
        t["ZC_ACCT_BILLSTS_HA"] = [
            {"ACCT_BILLSTS_HA_C": k, "NAME": v} for k, v in ref.BILL_STATUSES.items()
        ]
        t["CLARITY_DEP"] = [
            {"DEPARTMENT_ID": d.id, "DEPARTMENT_NAME": d.name, "SPECIALTY": d.specialty,
             "LOC_NAME": d.location, "SERV_AREA_NAME": ref.SERVICE_AREA}
            for d in ref.DEPARTMENTS
        ]
        prov_rows = []
        prov_num = 1000
        for dep in ref.DEPARTMENTS:
            ids = []
            for _ in range(dep.providers):
                prov_num += 1
                pid = f"E{prov_num}"
                ptype = self.rng.choice(ref.PROV_TYPES)
                suffix = {"Physician": "MD", "Nurse Practitioner": "NP",
                          "Physician Assistant": "PA"}[ptype]
                name = (f"{self.rng.choice(ref.LAST_NAMES).upper()}, "
                        f"{self.rng.choice(ref.FIRST_NAMES).upper()} {suffix}")
                prov_rows.append({"PROV_ID": pid, "PROV_NAME": name, "PROV_TYPE": ptype,
                                  "SPECIALTY": dep.specialty, "DEPARTMENT_ID": dep.id,
                                  "ACTIVE_YN": "Y"})
                ids.append(pid)
            self.providers[dep.id] = ids
        t["CLARITY_SER"] = prov_rows
        t["CLARITY_EPM"] = [
            {"PAYOR_ID": p.id, "PAYOR_NAME": p.name, "FIN_CLASS_C": p.fin_class}
            for p in ref.PAYORS
        ]
        t["CLARITY_EDG"] = [
            {"DX_ID": k, "DX_NAME": name, "ICD10_CODE": code}
            for k, (name, code) in ref.DIAGNOSES.items()
        ]
        t["CLARITY_EAP"] = [
            {"PROC_ID": k, "PROC_CODE": code, "PROC_NAME": name, "BASE_CHARGE": charge}
            for k, (code, name, charge) in ref.PROCEDURES.items()
        ]

    def _new_patient(self, d: date, created: date) -> Row:
        rng = self.rng
        age = min(int(abs(rng.gauss(42, 22))), 99)
        birth = created - timedelta(days=age * 365 + rng.randint(0, 364))
        city, zipcode = rng.choice(ref.CITIES)
        pat_id = f"Z{self._next('pat')}"
        row: Row = {
            "PAT_ID": pat_id, "PAT_MRN_ID": str(self._next("mrn")),
            "PAT_FIRST_NAME": rng.choice(ref.FIRST_NAMES).upper(),
            "PAT_LAST_NAME": rng.choice(ref.LAST_NAMES).upper(),
            "BIRTH_DATE": birth, "SEX": rng.choice(("F", "M")),
            # ~1% of registrations arrive without a ZIP: a realistic data-quality warning.
            "ZIP": "" if rng.random() < 0.01 else zipcode,
            "CITY": city, "STATE": ref.STATE, "CREATE_DATE": created,
            "UPDATE_DATE": self._stamp(d),
        }
        self.patients[pat_id] = row
        self.patient_payor[pat_id] = self._pick_payor(age)
        return row

    def _pick_payor(self, age: int) -> int:
        r = self.rng.random()
        if age >= 65:
            return 1 if r < 0.45 else 2 if r < 0.88 else self.rng.choice((5, 6))
        if age < 18:
            return self.rng.choice((3, 4)) if r < 0.45 else self.rng.choice((5, 6, 7))
        if r < 0.55:
            return self.rng.choice((5, 6, 7))
        if r < 0.83:
            return self.rng.choice((3, 4))
        if r < 0.97:
            return ref.SELF_PAY_ID
        return 8

    # ---- daily activity -----------------------------------------------------------------
    def _daily_patients(self, d: date, out: DayExtract) -> None:
        # A few address changes on existing patients: changed rows are re-sent with a new
        # UPDATE_DATE. Picked before today's registrations so a patient appears once per file.
        for pat_id in self.rng.sample(sorted(self.patients), k=self.rng.randint(2, 8)):
            row = self.patients[pat_id]
            row["CITY"], row["ZIP"] = self.rng.choice(ref.CITIES)
            row["UPDATE_DATE"] = self._stamp(d)
            out.tables["PATIENT"].append(dict(row))
        for _ in range(self.rng.randint(8, 20)):
            out.tables["PATIENT"].append(dict(self._new_patient(d, d)))

    def _daily_encounters(self, d: date, out: DayExtract) -> None:
        rng = self.rng
        weekend = d.weekday() >= 5
        volume = self.weekday_visits * (0.45 if weekend else 1.0) * rng.uniform(0.88, 1.12)
        deps = ref.DEPARTMENTS
        weights = [dep.weekend_weight if weekend else dep.weekday_weight for dep in deps]
        patient_ids = sorted(self.patients)
        for dep in rng.choices(deps, weights=weights, k=int(volume)):
            self._encounter(d, out, dep, rng.choice(patient_ids))

    def _encounter(self, d: date, out: DayExtract, dep: ref.Department, pat_id: str) -> None:
        rng = self.rng
        csn = self._next("csn")
        enc_type = dep.enc_type
        if dep.telehealth_share and rng.random() < dep.telehealth_share:
            enc_type = 76
        scheduled = enc_type in (101, 76)
        r = rng.random()
        status = 2 if not scheduled or r < 0.84 else 4 if r < 0.92 else 3

        if dep.enc_type == 51:
            arrive = datetime.combine(d, time(rng.randint(0, 23), rng.randint(0, 59)))
            minutes = int(rng.lognormvariate(5.3, 0.45))  # ED length of stay, median ~3.3h
        else:
            arrive = datetime.combine(d, time(rng.randint(7, 17), rng.choice((0, 15, 30, 45))))
            minutes = int(rng.uniform(20, 75))
        admitted = dep.enc_type == 3 or (dep.enc_type == 51 and status == 2 and rng.random() < 0.10)
        los_days = rng.randint(1, 6) if admitted else 0
        depart = arrive + timedelta(days=los_days, minutes=minutes)

        prov_id = rng.choice(self.providers[dep.id])
        if rng.random() < 0.005:
            prov_id = ""  # an occasional encounter with no visit provider recorded
        completed = status == 2
        har = self._next("har") if completed else None
        # Patients still in the building at extract time have no checkout yet; the row is
        # re-sent with CHECKOUT_TIME on the day they leave (an incremental update).
        still_here = completed and depart.date() > d
        enc_row: Row = {
            "PAT_ENC_CSN_ID": csn, "PAT_ID": pat_id, "CONTACT_DATE": d,
            "DEPARTMENT_ID": dep.id, "VISIT_PROV_ID": prov_id,
            "ENC_TYPE_C": 3 if admitted else enc_type, "APPT_STATUS_C": status,
            "CHECKIN_TIME": arrive if completed else None,
            "CHECKOUT_TIME": depart if completed and not still_here else None,
            "HSP_ACCOUNT_ID": har, "UPDATE_DATE": self._stamp(d),
        }
        out.tables["PAT_ENC"].append(dict(enc_row))
        if not completed:
            return
        if still_here:
            self._schedule(depart.date(), lambda day, o, r=enc_row, h=har, t=depart:
                           self._discharge(day, o, r, h, t))

        # Diagnoses
        for line, dx in enumerate(rng.sample(dep.diagnoses, k=min(len(dep.diagnoses),
                                                                    rng.randint(1, 3))), 1):
            out.tables["PAT_ENC_DX"].append({
                "PAT_ENC_CSN_ID": csn, "LINE": line, "DX_ID": dx,
                "PRIMARY_DX_YN": "Y" if line == 1 else "N", "CONTACT_DATE": d,
            })

        # Billable services: one E&M visit code plus ordered ancillaries
        procs: list[int] = []
        if enc_type == 76:
            procs.append(ref.TELEHEALTH_PROC)
        elif dep.em_procs:
            procs.append(rng.choice(dep.em_procs))
        if dep.ancillary_procs:
            n = rng.randint(1 if not dep.em_procs else 0, min(3, len(dep.ancillary_procs)))
            for proc in rng.sample(dep.ancillary_procs, k=n):
                procs.append(proc)
                out.tables["ORDER_PROC"].append({
                    "ORDER_PROC_ID": self._next("order"), "PAT_ENC_CSN_ID": csn,
                    "PAT_ID": pat_id, "PROC_ID": proc, "ORDERING_DATE": d,
                    "ORDER_STATUS": "Completed", "AUTHRZING_PROV_ID": prov_id or None,
                })
        if admitted:
            procs.append(ref.ADMIT_PROC)
            procs.extend([ref.ROOM_PROC] * los_days)

        payor = self.payors[self.patient_payor[pat_id]]
        acct_class = "Inpatient" if admitted else "Emergency" if dep.enc_type == 51 else "Outpatient"
        self.accounts[har] = {
            "HSP_ACCOUNT_ID": har, "PAT_ID": pat_id, "PRIM_ENC_CSN_ID": csn,
            "ACCT_CLASS": acct_class, "PRIMARY_PAYOR_ID": payor.id,
            "ACCT_FIN_CLASS_C": payor.fin_class, "ADM_DATE_TIME": arrive,
            "DISCH_DATE_TIME": None if still_here else depart, "BILL_DROP_DATE": None, "ACCT_BILLSTS_HA_C": 1,
            "TOT_CHGS": 0.0, "TOT_PMTS": 0.0, "TOT_ADJ": 0.0, "TOT_ACCT_BAL": 0.0,
            "UPDATE_DATE": None,
        }
        self._touched.add(har)

        # Charges post at discharge; ~20% arrive a day or two late (charge lag).
        post = depart.date() + timedelta(days=rng.choices((0, 1, 2), weights=(80, 15, 5))[0])
        self._schedule(post, lambda day, o, h=har, p=procs, s=d: self._post_charges(day, o, h, p, s))

    def _discharge(self, day: date, out: DayExtract, enc_row: Row, har: int, depart: datetime) -> None:
        enc_row["CHECKOUT_TIME"] = depart
        enc_row["UPDATE_DATE"] = self._stamp(day)
        out.tables["PAT_ENC"].append(dict(enc_row))
        self.accounts[har]["DISCH_DATE_TIME"] = depart
        self._touched.add(har)

    # ---- revenue cycle events -----------------------------------------------------------
    def _tx(self, out: DayExtract, har: int, day: date, tx_type: str, amount: float,
            service: date | None = None, proc: int | None = None, payor: int | None = None,
            reason: str | None = None) -> None:
        acct = self.accounts[har]
        amount = _money(amount)
        out.tables["HSP_TRANSACTIONS"].append({
            "TX_ID": self._next("tx"), "HSP_ACCOUNT_ID": har, "TX_TYPE": tx_type,
            "SERVICE_DATE": service or acct["ADM_DATE_TIME"].date(), "TX_POST_DATE": day,
            "TX_AMOUNT": amount, "PROC_ID": proc, "PAYOR_ID": payor, "ADJ_REASON": reason,
        })
        key = {"Charge": "TOT_CHGS", "Payment": "TOT_PMTS", "Adjustment": "TOT_ADJ"}[tx_type]
        acct[key] = _money(acct[key] + abs(amount))
        acct["TOT_ACCT_BAL"] = _money(acct["TOT_CHGS"] - acct["TOT_PMTS"] - acct["TOT_ADJ"])
        if acct["TOT_ACCT_BAL"] <= 0.005 and acct["TOT_CHGS"] > 0:
            acct["ACCT_BILLSTS_HA_C"] = 3
        self._touched.add(har)

    def _post_charges(self, day: date, out: DayExtract, har: int, procs: list[int],
                      service: date) -> None:
        for proc in procs:
            charge = ref.PROCEDURES[proc][2] * self.rng.uniform(0.97, 1.03)
            self._tx(out, har, day, "Charge", charge, service=service, proc=proc)
        # Coding and claim scrubbing take 1-6 days before the claim drops (DNFB window).
        drop = day + timedelta(days=self.rng.choices((1, 2, 3, 4, 6), weights=(35, 30, 18, 10, 7))[0])
        self._schedule(drop, lambda d, o, h=har: self._bill_drop(d, o, h))

    def _bill_drop(self, day: date, out: DayExtract, har: int) -> None:
        acct = self.accounts[har]
        acct["BILL_DROP_DATE"] = day
        acct["ACCT_BILLSTS_HA_C"] = 2
        self._touched.add(har)
        payor = self.payors[acct["PRIMARY_PAYOR_ID"]]
        remit_day = day + timedelta(days=self.rng.randint(*payor.lag_days))
        self._schedule(remit_day, lambda d, o, h=har: self._remit(d, o, h, resubmitted=False))

    def _remit(self, day: date, out: DayExtract, har: int, resubmitted: bool) -> None:
        rng = self.rng
        acct = self.accounts[har]
        payor = self.payors[acct["PRIMARY_PAYOR_ID"]]
        balance = acct["TOT_ACCT_BAL"]
        if balance <= 0:
            return

        if payor.id == ref.SELF_PAY_ID:
            if rng.random() < 0.55:  # self-pay patients who pay, with a prompt-pay discount
                paid = balance * payor.pay_rate
                self._tx(out, har, day, "Payment", -paid, payor=payor.id)
                self._tx(out, har, day, "Adjustment", -(balance - paid), payor=payor.id,
                         reason="SELF-PAY DISCOUNT")
            return

        denied = not resubmitted and rng.random() < payor.denial_rate
        carc = rng.choices(ref.DENIAL_REASONS, weights=[w for *_, w in ref.DENIAL_REASONS])[0]
        paid = 0.0 if denied else balance * payor.pay_rate * rng.uniform(0.95, 1.05)
        out.tables["CL_REMIT"].append({
            "REMIT_ID": self._next("remit"), "HSP_ACCOUNT_ID": har, "PAYOR_ID": payor.id,
            "REMIT_DATE": day, "BILLED_AMT": balance, "PAID_AMT": _money(paid),
            "DENIED_YN": "Y" if denied else "N",
            "CARC_CODE": carc[0] if denied else None, "CARC_DESC": carc[1] if denied else None,
        })
        if denied:
            acct["ACCT_BILLSTS_HA_C"] = 4
            self._touched.add(har)
            if rng.random() < 0.62:  # worked and resubmitted, then paid
                again = day + timedelta(days=rng.randint(12, 35))
                self._schedule(again, lambda d, o, h=har: self._remit(d, o, h, resubmitted=True))
            else:  # written off after appeal window
                writeoff = day + timedelta(days=rng.randint(20, 45))
                self._schedule(writeoff, lambda d, o, h=har, c=carc[0]: self._tx(
                    o, h, d, "Adjustment", -self.accounts[h]["TOT_ACCT_BAL"], payor=payor.id,
                    reason=f"DENIAL WRITE-OFF {c}"))
            return

        self._tx(out, har, day, "Payment", -paid, payor=payor.id)
        self._tx(out, har, day, "Adjustment", -(balance - paid), payor=payor.id,
                 reason="CONTRACTUAL")

    # ---- driver -------------------------------------------------------------------------
    def run(self) -> Iterator[DayExtract]:
        """Yield one DayExtract per simulated day, oldest first."""
        day = self.start
        first = True
        while day <= self.end:
            out = DayExtract(day)
            self._touched = set()
            if first:
                self._emit_reference(out)
                for _ in range(self.base_patients):
                    created = day - timedelta(days=self.rng.randint(30, 3650))
                    out.tables["PATIENT"].append(dict(self._new_patient(day, created)))
                first = False
            else:
                self._daily_patients(day, out)
            self._daily_encounters(day, out)
            for event in self.events.pop(day, []):
                event(day, out)
            for har in sorted(self._touched):
                acct = self.accounts[har]
                acct["UPDATE_DATE"] = self._stamp(day)
                out.tables["HSP_ACCOUNT"].append(dict(acct))
            yield out
            day += timedelta(days=1)
