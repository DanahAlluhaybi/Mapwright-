"""The benchmark cases.

D01–D08 are development cases. H01–H03 are held-out cases: they use the
held-out variant pool and header styles that never appear in development.
H04 (from a real public dataset) is added separately.
"""

from __future__ import annotations

import random

from . import render as r
from .spec import Case, Column, Injection

C = Column
I = Injection


def _hex(length: int):
    return lambda rng: "".join(rng.choice("0123456789abcdef") for _ in range(length))


def _pick(*values: str):
    return lambda rng: rng.choice(values)


def _terminal(rng: random.Random) -> str:
    return f"T-{rng.randint(1, 60):04d}"


YES_NO = {"true": "Y", "false": "N"}

CASES: list[Case] = [
    Case(
        id="D01", split="dev", entity="customer", style="crm_export", seed=101, rows=250,
        description="Modern CRM export with readable English headers.",
        columns=(
            C("Customer ID", "customer_id", "M1"),
            C("Name", "full_name", "M1"),
            C("Email", "email", "M1"),
            C("Phone Number", "phone", "M1"),
            C("Country", "country_code", "M1"),
            C("City", "city", "M1"),
            C("Type", "customer_type", "M1"),
            C("Signup Date", "signup_date", "M1"),
            C("Credit Limit", "credit_limit", "M1"),
            C("Active", "is_active", "M1"),
        ),
        injections=(
            I("S4", "full_name", 0.08, r.whitespace_or_case),
            I("S4", "email", 0.08, r.whitespace_or_case),
            I("S6", "email", 0.04, r.invalid_email),
            I("S3", "customer_type", 0.03, r.required_missing),
        ),
    ),
    Case(
        id="D02", split="dev", entity="customer", style="legacy_erp", seed=102, rows=300,
        description="Legacy ERP: abbreviated headers, upper-case names, coded flags, day-first dates.",
        columns=(
            C("CUST_NO", "customer_id", "M1"),
            C("CUST_NM", "full_name", "M1", fmt=r.upper_case, fmt_issue="S4"),
            C("EMAIL_ADDR", "email", "M1"),
            C("TEL_NO", "phone", "M1"),
            C("CTRY", "country_code", "M1"),
            C("CITY_NM", "city", "M1"),
            C("CUST_TYP", "customer_type", "M1",
              fmt=r.fixed_map({"INDIVIDUAL": "I", "BUSINESS": "B"}), fmt_issue="V1"),
            C("OPEN_DT", "signup_date", "M1", fmt=r.date_format("dmy", "/"), fmt_issue="V2"),
            C("CR_LMT", "credit_limit", "M1"),
            C("ACTV_FLG", "is_active", "M1", fmt=r.fixed_map(YES_NO), fmt_issue="V5"),
            C("ROW_HASH", extra=_hex(16)),
            C("LAST_UPD_BY", extra=_pick("ERPBATCH", "SYSADM", "MIGR01", "FINOPS2")),
        ),
        injections=(
            I("V1", "country_code", 0.35, r.variant("country_code")),
            I("V1", "city", 0.20, r.variant("city")),
        ),
    ),
    Case(
        id="D03", split="dev", entity="customer", style="arabic_export", seed=103, rows=250,
        arabic_share=0.6,
        description="Arabic headers, mixed Arabic and English values, placeholder nulls.",
        columns=(
            C("رقم العميل", "customer_id", "M2"),
            C("اسم العميل", "full_name", "M2"),
            C("البريد الإلكتروني", "email", "M2"),
            C("الجوال", "phone", "M2"),
            C("الدولة", "country_code", "M2"),
            C("المدينة", "city", "M2"),
            C("نوع العميل", "customer_type", "M2",
              fmt=r.fixed_map({"INDIVIDUAL": "فرد", "BUSINESS": "شركة"}), fmt_issue="V1"),
            C("تاريخ التسجيل", "signup_date", "M2", fmt=r.date_format("ymd", "/"), fmt_issue="S9"),
            C("الحد الائتماني", "credit_limit", "M2"),
            C("نشط", "is_active", "M2",
              fmt=r.fixed_map({"true": "نعم", "false": "لا"}), fmt_issue="V5"),
        ),
        injections=(
            I("S3", "customer_type", 0.03, r.required_missing),
            I("V1", "country_code", 0.50, r.variant("country_code")),
            I("V1", "city", 0.50, r.variant("city")),
            I("V3", "email", 0.70, r.disguised_missing),
            I("V3", "phone", 0.70, r.disguised_missing),
            I("V3", "city", 0.70, r.disguised_missing),
            I("V3", "credit_limit", 0.50, r.disguised_missing),
        ),
    ),
    Case(
        id="D04", split="dev", entity="customer", style="split_names", seed=104, rows=250,
        description="Names split into two columns, Saudi mobile numbers in local formats.",
        columns=(
            C("customer_id", "customer_id"),
            C("first_name", "full_name", "M3", split_part=0),
            C("last_name", "full_name", "M3", split_part=1),
            C("email", "email"),
            C("mobile", "phone", "M1"),
            C("country", "country_code", "M1"),
            C("city", "city"),
            C("customer_type", "customer_type"),
            C("created_at", "signup_date", "M1"),
            C("credit_limit", "credit_limit"),
            C("active", "is_active", "M1"),
        ),
        injections=(
            I("V7", "email", 0.40, r.phone_in_email_column),
            I("S9", "phone", 0.60, r.local_phone_format),
            I("S6", "phone", 0.04, r.invalid_phone),
            I("S6", "email", 0.04, r.invalid_email),
            I("V3", "email", 0.60, r.disguised_missing),
            I("V3", "phone", 0.60, r.disguised_missing),
        ),
    ),
    Case(
        id="D05", split="dev", entity="customer", style="branch_merge", seed=105, rows=280,
        exact_duplicate_rate=0.06, conflicting_duplicate_rate=0.03,
        description="Two branch exports merged: duplicates, conflicting records, sync columns.",
        columns=(
            C("customer_id", "customer_id"),
            C("full_name", "full_name"),
            C("email", "email"),
            C("phone", "phone"),
            C("country_code", "country_code"),
            C("city", "city"),
            C("customer_type", "customer_type"),
            C("signup_date", "signup_date"),
            C("credit_limit", "credit_limit"),
            C("is_active", "is_active"),
            C("branch_code", extra=_pick("JED01", "JED02", "RUH01", "DMM01")),
            C("source_system", extra=_pick("CRM-A", "POS-LEGACY")),
        ),
        injections=(
            I("S4", "customer_id", 0.05, r.id_noise),
            I("S4", "full_name", 0.10, r.whitespace_or_case),
            I("S4", "email", 0.10, r.whitespace_or_case),
        ),
    ),
    Case(
        id="D06", split="dev", entity="sales_order", style="finance_export", seed=106, rows=350,
        description="Finance export: month-first dates, formatted amounts, free-text statuses.",
        columns=(
            C("ORDER_NO", "order_id", "M1"),
            C("CUSTOMER", "customer_id", "M1"),
            C("DOC_DATE", "order_date", "M1", fmt=r.date_format("mdy", "/"), fmt_issue="V2"),
            C("CURR", "currency", "M1"),
            C("NET_AMOUNT", "amount", "M1"),
            C("STATUS", "status", "M1", fmt=r.any_variant("status"), fmt_issue="V6"),
            C("SALES_CHANNEL", "channel", "M1"),
        ),
        injections=(
            I("S1", "amount", 0.30, r.thousands_separator),
            I("V4", "amount", 0.10, r.embedded_unit()),
            I("V1", "currency", 0.20, r.variant("currency")),
            I("V1", "channel", 0.50, r.variant("channel")),
        ),
    ),
    Case(
        id="D07", split="dev", entity="sales_order", style="pos_export", seed=107, rows=350,
        orphan_rate=0.04,
        description="Point-of-sale export: timestamps, sign errors, unknown customers.",
        columns=(
            C("txn_id", "order_id", "M1"),
            C("cust_ref", "customer_id", "M1"),
            C("txn_datetime", "order_date", "M4", fmt=r.datetime_with_time),
            C("currency", "currency"),
            C("total", "amount", "M1"),
            C("status", "status"),
            C("channel", "channel"),
            C("terminal_id", extra=_terminal),
        ),
        injections=(
            I("S5", "amount", 0.03, r.negative_sign),
            I("V6", "status", 0.40, r.variant("status")),
            I("V1", "channel", 0.30, r.variant("channel")),
        ),
    ),
    Case(
        id="D08", split="dev", entity="sales_order", style="spreadsheet", seed=108, rows=300,
        encoding="cp1256",
        title_rows=("Sales Orders Export", "Generated 2026-09-30 by Finance"),
        description="Spreadsheet saved as Windows-1256 CSV with title rows and no channel column.",
        columns=(
            C("Order #", "order_id", "M1"),
            C("Customer Code", "customer_id", "M1"),
            C("Date", "order_date", "M1", fmt=r.date_with_month_name, fmt_issue="S9"),
            C("Currency", "currency", "M1"),
            C("Amount", "amount", "M1"),
            C("Status", "status", "M1"),
        ),
        injections=(
            I("S1", "amount", 0.30, r.thousands_separator),
            I("V4", "amount", 0.10, r.embedded_unit(("code_prefix", "thousands_suffix"))),
            I("V1", "currency", 0.40, r.variant("currency")),
            I("V6", "status", 0.30, r.variant("status")),
        ),
    ),
    Case(
        id="H01", split="heldout", entity="customer", style="camel_transliterated", seed=201,
        rows=250, pool="heldout", max_day=12,
        description="camelCase and transliterated Arabic headers; every date is day/month ambiguous.",
        columns=(
            C("custId", "customer_id", "M1"),
            C("ismAlAmil", "full_name", "M2"),
            C("emailAddress", "email", "M1"),
            C("jawwal", "phone", "M2"),
            C("countryName", "country_code", "M1"),
            C("madina", "city", "M2"),
            C("customerCategory", "customer_type", "M1",
              fmt=r.any_variant("customer_type"), fmt_issue="V1"),
            C("registeredOn", "signup_date", "M1", fmt=r.date_format("dmy", "."), fmt_issue="V2"),
            C("creditLimitSar", "credit_limit", "M1"),
            C("isEnabled", "is_active", "M1",
              fmt=r.fixed_map({"true": "Enabled", "false": "Disabled"}), fmt_issue="V5"),
        ),
        injections=(
            I("V1", "country_code", 0.50, r.variant("country_code")),
            I("V1", "city", 0.40, r.variant("city")),
            I("V3", "email", 0.50, r.disguised_missing),
            I("V3", "phone", 0.50, r.disguised_missing),
            I("V3", "city", 0.50, r.disguised_missing),
        ),
    ),
    Case(
        id="H02", split="heldout", entity="customer", style="high_noise", seed=202, rows=300,
        pool="heldout", exact_duplicate_rate=0.10, conflicting_duplicate_rate=0.05,
        description="Most cell-level customer issues at once, at 10–20% noise.",
        columns=(
            C("CLIENT_REF", "customer_id", "M1"),
            C("CLIENT_NAME", "full_name", "M1"),
            C("E_MAIL", "email", "M1"),
            C("CONTACT_NO", "phone", "M1"),
            C("NATION", "country_code", "M1"),
            C("TOWN", "city", "M1"),
            C("SEGMENT", "customer_type", "M1"),
            C("JOIN_DATE", "signup_date", "M1"),
            C("LIMIT_AMT", "credit_limit", "M1"),
            C("STATUS_FLAG", "is_active", "M1"),
            C("SYNC_ID", extra=_hex(12)),
            C("CREATED_BY", extra=_pick("import_job", "admin", "branch_user")),
        ),
        injections=(
            I("V7", "email", 0.40, r.phone_in_email_column),
            I("S3", "customer_type", 0.05, r.required_missing),
            I("S4", "customer_id", 0.10, r.id_noise),
            I("S4", "full_name", 0.15, r.whitespace_or_case),
            I("S4", "email", 0.10, r.whitespace_or_case),
            I("S6", "email", 0.08, r.invalid_email),
            I("S9", "phone", 0.40, r.local_phone_format),
            I("S6", "phone", 0.08, r.invalid_phone),
            I("V1", "country_code", 0.20, r.variant("country_code")),
            I("V1", "city", 0.20, r.variant("city")),
            I("V1", "customer_type", 0.15, r.variant("customer_type")),
            I("V4", "credit_limit", 0.15, r.embedded_unit()),
            I("S1", "credit_limit", 0.15, r.thousands_separator),
            I("S5", "credit_limit", 0.10, r.negative_sign),
            I("V5", "is_active", 0.20, r.bool_variant),
            I("V3", "email", 0.50, r.disguised_missing),
            I("V3", "phone", 0.50, r.disguised_missing),
            I("V3", "city", 0.50, r.disguised_missing),
            I("V3", "credit_limit", 0.40, r.disguised_missing),
        ),
    ),
    Case(
        id="H03", split="heldout", entity="sales_order", style="marketplace_export", seed=203,
        rows=350, pool="heldout", orphan_rate=0.04,
        description="Unseen status phrasings, currency notations and channel names.",
        columns=(
            C("OrderRef", "order_id", "M1"),
            C("ClientRef", "customer_id", "M1"),
            C("OrderedOn", "order_date", "M1"),
            C("Ccy", "currency", "M1"),
            C("GrossValue", "amount", "M1"),
            C("OrderState", "status", "M1", fmt=r.any_variant("status"), fmt_issue="V6"),
            C("SalesRoute", "channel", "M1"),
        ),
        injections=(
            I("V1", "currency", 0.40, r.variant("currency")),
            I("V4", "amount", 0.15, r.embedded_unit()),
            I("S1", "amount", 0.20, r.thousands_separator),
            I("V1", "channel", 0.60, r.variant("channel")),
        ),
    ),
]

CASES_BY_ID = {case.id: case for case in CASES}
