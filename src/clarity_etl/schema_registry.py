"""The extract contract: which Clarity tables we receive, their columns, types and keys.

The loader validates every incoming file against this registry, and the agent reads it
to explain schema drift. Table and column names follow Epic Clarity conventions, but the
column sets are simplified for a proof of concept.

Types: int, str, date, datetime, decimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TableSpec:
    name: str
    kind: str  # "lookup", "dimension" or "fact"
    primary_key: tuple[str, ...]
    columns: dict[str, str]
    description: str
    foreign_keys: dict[str, str] = field(default_factory=dict)  # column -> "TABLE.COLUMN"
    required: tuple[str, ...] = ()  # columns that must never be blank

    @property
    def column_names(self) -> list[str]:
        return list(self.columns)


_SPECS = [
    # ---- Category lookup (ZC_) tables -------------------------------------------------
    TableSpec(
        "ZC_DISP_ENC_TYPE", "lookup", ("DISP_ENC_TYPE_C",),
        {"DISP_ENC_TYPE_C": "int", "NAME": "str"},
        "Encounter type categories.",
    ),
    TableSpec(
        "ZC_APPT_STATUS", "lookup", ("APPT_STATUS_C",),
        {"APPT_STATUS_C": "int", "NAME": "str"},
        "Appointment status categories.",
    ),
    TableSpec(
        "ZC_FIN_CLASS", "lookup", ("FIN_CLASS_C",),
        {"FIN_CLASS_C": "int", "NAME": "str"},
        "Financial class categories (Medicare, Commercial, ...).",
    ),
    TableSpec(
        "ZC_ACCT_BILLSTS_HA", "lookup", ("ACCT_BILLSTS_HA_C",),
        {"ACCT_BILLSTS_HA_C": "int", "NAME": "str"},
        "Hospital account billing status categories.",
    ),
    # ---- Dimensions -------------------------------------------------------------------
    TableSpec(
        "CLARITY_DEP", "dimension", ("DEPARTMENT_ID",),
        {
            "DEPARTMENT_ID": "int", "DEPARTMENT_NAME": "str", "SPECIALTY": "str",
            "LOC_NAME": "str", "SERV_AREA_NAME": "str",
        },
        "Departments and the locations they belong to.",
        required=("DEPARTMENT_ID", "DEPARTMENT_NAME"),
    ),
    TableSpec(
        "CLARITY_SER", "dimension", ("PROV_ID",),
        {
            "PROV_ID": "str", "PROV_NAME": "str", "PROV_TYPE": "str", "SPECIALTY": "str",
            "DEPARTMENT_ID": "int", "ACTIVE_YN": "str",
        },
        "Providers.",
        foreign_keys={"DEPARTMENT_ID": "CLARITY_DEP.DEPARTMENT_ID"},
        required=("PROV_ID", "PROV_NAME"),
    ),
    TableSpec(
        "CLARITY_EPM", "dimension", ("PAYOR_ID",),
        {"PAYOR_ID": "int", "PAYOR_NAME": "str", "FIN_CLASS_C": "int"},
        "Payors (insurance companies).",
        foreign_keys={"FIN_CLASS_C": "ZC_FIN_CLASS.FIN_CLASS_C"},
        required=("PAYOR_ID", "PAYOR_NAME"),
    ),
    TableSpec(
        "CLARITY_EDG", "dimension", ("DX_ID",),
        {"DX_ID": "int", "DX_NAME": "str", "ICD10_CODE": "str"},
        "Diagnoses.",
        required=("DX_ID", "ICD10_CODE"),
    ),
    TableSpec(
        "CLARITY_EAP", "dimension", ("PROC_ID",),
        {"PROC_ID": "int", "PROC_CODE": "str", "PROC_NAME": "str", "BASE_CHARGE": "decimal"},
        "Procedures and their chargemaster price.",
        required=("PROC_ID", "PROC_CODE"),
    ),
    TableSpec(
        "PATIENT", "dimension", ("PAT_ID",),
        {
            "PAT_ID": "str", "PAT_MRN_ID": "str", "PAT_FIRST_NAME": "str",
            "PAT_LAST_NAME": "str", "BIRTH_DATE": "date", "SEX": "str", "ZIP": "str",
            "CITY": "str", "STATE": "str", "CREATE_DATE": "date", "UPDATE_DATE": "datetime",
        },
        "Patients (synthetic).",
        required=("PAT_ID", "PAT_MRN_ID", "BIRTH_DATE"),
    ),
    # ---- Facts ------------------------------------------------------------------------
    TableSpec(
        "PAT_ENC", "fact", ("PAT_ENC_CSN_ID",),
        {
            "PAT_ENC_CSN_ID": "int", "PAT_ID": "str", "CONTACT_DATE": "date",
            "DEPARTMENT_ID": "int", "VISIT_PROV_ID": "str", "ENC_TYPE_C": "int",
            "APPT_STATUS_C": "int", "CHECKIN_TIME": "datetime", "CHECKOUT_TIME": "datetime",
            "HSP_ACCOUNT_ID": "int", "UPDATE_DATE": "datetime",
        },
        "Patient encounters (visits, ED arrivals, admissions).",
        foreign_keys={
            "PAT_ID": "PATIENT.PAT_ID",
            "DEPARTMENT_ID": "CLARITY_DEP.DEPARTMENT_ID",
            "VISIT_PROV_ID": "CLARITY_SER.PROV_ID",
            "ENC_TYPE_C": "ZC_DISP_ENC_TYPE.DISP_ENC_TYPE_C",
            "APPT_STATUS_C": "ZC_APPT_STATUS.APPT_STATUS_C",
        },
        required=("PAT_ENC_CSN_ID", "PAT_ID", "CONTACT_DATE", "DEPARTMENT_ID"),
    ),
    TableSpec(
        "PAT_ENC_DX", "fact", ("PAT_ENC_CSN_ID", "LINE"),
        {
            "PAT_ENC_CSN_ID": "int", "LINE": "int", "DX_ID": "int", "PRIMARY_DX_YN": "str",
            "CONTACT_DATE": "date",
        },
        "Diagnoses recorded on an encounter.",
        foreign_keys={"PAT_ENC_CSN_ID": "PAT_ENC.PAT_ENC_CSN_ID", "DX_ID": "CLARITY_EDG.DX_ID"},
        required=("PAT_ENC_CSN_ID", "LINE", "DX_ID"),
    ),
    TableSpec(
        "ORDER_PROC", "fact", ("ORDER_PROC_ID",),
        {
            "ORDER_PROC_ID": "int", "PAT_ENC_CSN_ID": "int", "PAT_ID": "str", "PROC_ID": "int",
            "ORDERING_DATE": "date", "ORDER_STATUS": "str", "AUTHRZING_PROV_ID": "str",
        },
        "Orders for labs, imaging and other procedures.",
        foreign_keys={
            "PAT_ENC_CSN_ID": "PAT_ENC.PAT_ENC_CSN_ID",
            "PROC_ID": "CLARITY_EAP.PROC_ID",
            "AUTHRZING_PROV_ID": "CLARITY_SER.PROV_ID",
        },
        required=("ORDER_PROC_ID", "PAT_ENC_CSN_ID", "PROC_ID"),
    ),
    TableSpec(
        "HSP_ACCOUNT", "fact", ("HSP_ACCOUNT_ID",),
        {
            "HSP_ACCOUNT_ID": "int", "PAT_ID": "str", "PRIM_ENC_CSN_ID": "int",
            "ACCT_CLASS": "str", "PRIMARY_PAYOR_ID": "int", "ACCT_FIN_CLASS_C": "int",
            "ADM_DATE_TIME": "datetime", "DISCH_DATE_TIME": "datetime",
            "BILL_DROP_DATE": "date", "ACCT_BILLSTS_HA_C": "int", "TOT_CHGS": "decimal",
            "TOT_PMTS": "decimal", "TOT_ADJ": "decimal", "TOT_ACCT_BAL": "decimal",
            "UPDATE_DATE": "datetime",
        },
        "Hospital billing accounts, one per billable encounter, re-sent when balances change.",
        foreign_keys={
            "PAT_ID": "PATIENT.PAT_ID",
            "PRIM_ENC_CSN_ID": "PAT_ENC.PAT_ENC_CSN_ID",
            "PRIMARY_PAYOR_ID": "CLARITY_EPM.PAYOR_ID",
            "ACCT_BILLSTS_HA_C": "ZC_ACCT_BILLSTS_HA.ACCT_BILLSTS_HA_C",
        },
        required=("HSP_ACCOUNT_ID", "PAT_ID", "TOT_CHGS"),
    ),
    TableSpec(
        "HSP_TRANSACTIONS", "fact", ("TX_ID",),
        {
            "TX_ID": "int", "HSP_ACCOUNT_ID": "int", "TX_TYPE": "str", "SERVICE_DATE": "date",
            "TX_POST_DATE": "date", "TX_AMOUNT": "decimal", "PROC_ID": "int",
            "PAYOR_ID": "int", "ADJ_REASON": "str",
        },
        "Charges (+), payments (-) and adjustments (-) posted to hospital accounts.",
        foreign_keys={
            "HSP_ACCOUNT_ID": "HSP_ACCOUNT.HSP_ACCOUNT_ID",
            "PROC_ID": "CLARITY_EAP.PROC_ID",
            "PAYOR_ID": "CLARITY_EPM.PAYOR_ID",
        },
        required=("TX_ID", "HSP_ACCOUNT_ID", "TX_TYPE", "TX_AMOUNT"),
    ),
    TableSpec(
        "CL_REMIT", "fact", ("REMIT_ID",),
        {
            "REMIT_ID": "int", "HSP_ACCOUNT_ID": "int", "PAYOR_ID": "int", "REMIT_DATE": "date",
            "BILLED_AMT": "decimal", "PAID_AMT": "decimal", "DENIED_YN": "str",
            "CARC_CODE": "str", "CARC_DESC": "str",
        },
        "Payer remittances (835), including denials with their reason code.",
        foreign_keys={
            "HSP_ACCOUNT_ID": "HSP_ACCOUNT.HSP_ACCOUNT_ID",
            "PAYOR_ID": "CLARITY_EPM.PAYOR_ID",
        },
        required=("REMIT_ID", "HSP_ACCOUNT_ID", "PAYOR_ID"),
    ),
]

REGISTRY: dict[str, TableSpec] = {spec.name: spec for spec in _SPECS}

# Load order respects foreign keys: lookups, then dimensions, then facts.
LOAD_ORDER: list[str] = [s.name for s in _SPECS]
