"""Clean, contract-valid records. These become the ground truth of a case."""

from __future__ import annotations

import csv
import datetime as dt
import random
from decimal import Decimal
from pathlib import Path

from . import pools

CITIES_FILE = Path(__file__).resolve().parents[2] / "reference" / "cities.csv"


def _cities_by_country() -> dict[str, list[str]]:
    by_country: dict[str, list[str]] = {}
    with CITIES_FILE.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            by_country.setdefault(row["country_code"], []).append(row["city"])
    return by_country


CITIES = _cities_by_country()


def _weighted(rng: random.Random, weights: dict[str, int]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys])[0]


def random_date(
    rng: random.Random, start: dt.date, end: dt.date, max_day: int | None = None
) -> dt.date:
    while True:
        day = start + dt.timedelta(days=rng.randint(0, (end - start).days))
        if max_day is None or day.day <= max_day:
            return day


def _phone(rng: random.Random, country: str) -> str:
    prefix, length = pools.PHONE_SHAPES[country]
    return prefix + "".join(str(rng.randint(0, 9)) for _ in range(length))


def _person(rng: random.Random, arabic: bool) -> tuple[str, str]:
    """Return (full name in canonical form, Latin handle for the email)."""
    first = rng.choice(pools.FIRST_NAMES)
    last = rng.choice(pools.LAST_NAMES)
    latin = f"{first[0]} {last[0]}"
    name = f"{first[1]} {last[1]}" if arabic else latin
    handle = f"{first[0]}.{last[0]}{rng.randint(1, 99)}".lower()
    return name, handle


def customers(
    count: int,
    rng: random.Random,
    *,
    start_id: int = 100001,
    arabic_share: float = 0.3,
    max_day: int | None = None,
) -> list[dict]:
    rows = []
    for offset in range(count):
        country = _weighted(rng, pools.COUNTRY_WEIGHTS)
        business = rng.random() < 0.25
        arabic = rng.random() < arabic_share

        if business:
            latin, native = rng.choice(pools.COMPANIES)
            name = native if arabic else latin
            slug = latin.lower().replace(" ", "")
            email = f"{rng.choice(['info', 'sales', 'accounts'])}@{slug}.com.sa"
            credit = Decimal(rng.randrange(10_000, 500_001, 5_000))
        else:
            name, handle = _person(rng, arabic)
            email = f"{handle}@{rng.choice(pools.EMAIL_DOMAINS)}"
            credit = None if rng.random() < 0.6 else Decimal(rng.randrange(1_000, 50_001, 500))

        rows.append({
            "customer_id": f"C-{start_id + offset:06d}",
            "full_name": name,
            "email": None if rng.random() < 0.12 else email,
            "phone": None if rng.random() < 0.10 else _phone(rng, country),
            "country_code": country,
            "city": None if rng.random() < 0.08 else rng.choice(CITIES[country]),
            "customer_type": "BUSINESS" if business else "INDIVIDUAL",
            "signup_date": random_date(rng, dt.date(2005, 1, 1), dt.date(2026, 9, 30), max_day),
            "credit_limit": credit,
            "is_active": rng.random() < 0.85,
        })
    return rows


def orders(
    count: int,
    rng: random.Random,
    customer_ids: list[str],
    *,
    start_id: int = 500001,
    max_day: int | None = None,
) -> list[dict]:
    rows = []
    for offset in range(count):
        rows.append({
            "order_id": f"SO-{start_id + offset:06d}",
            "customer_id": rng.choice(customer_ids),
            "order_date": random_date(rng, dt.date(2018, 1, 1), dt.date(2026, 9, 30), max_day),
            "currency": _weighted(rng, pools.CURRENCY_WEIGHTS),
            "amount": Decimal(rng.randint(1_000, 2_000_000)) / 100,
            "status": _weighted(rng, pools.STATUS_WEIGHTS),
            "channel": _weighted(rng, pools.CHANNEL_WEIGHTS),
        })
    return rows
