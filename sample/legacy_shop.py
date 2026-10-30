#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
legacy_shop.py - Back-office system for "Northwind Outdoor Supply".

This module grew organically over several years.  It handles:

    * product catalog and inventory
    * pricing (tiers, coupons, bulk rules, promotions)
    * order processing and fulfilment
    * shipping cost estimation
    * invoicing and credit notes
    * customer loyalty points
    * monthly reporting
    * CSV import / export of catalog and orders

NOTE (2019): please do not split this file, the cron job imports it by path.
NOTE (2021): the cron job is gone, but nobody had time to split the file.
NOTE (2023): TODO refactor process_orders(), it is way too long.

Run directly to execute the nightly "demo" which prints a full trace of
everything the system does.  The output is deterministic and used by QA
to diff behaviour between releases.
"""

from __future__ import annotations

import csv
import io
import math
import random
import re
import textwrap
from collections import OrderedDict, defaultdict, Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Global configuration (historically read from shop.ini)
# ---------------------------------------------------------------------------

SHOP_NAME = "Northwind Outdoor Supply"
SHOP_ADDRESS = ["Hafenstrasse 12", "20457 Hamburg", "Germany"]
BASE_CURRENCY = "EUR"
DEFAULT_TAX_RATE = 0.19
REDUCED_TAX_RATE = 0.07
ZERO_TAX_COUNTRIES = ("CH", "NO", "US", "GB")
EU_COUNTRIES = ("DE", "AT", "FR", "NL", "BE", "IT", "ES", "PL", "DK", "SE")
FREE_SHIPPING_THRESHOLD = 150.0
MAX_LINES_PER_ORDER = 25
LOW_STOCK_WARNING = 5
LOYALTY_POINTS_PER_EURO = 1
LOYALTY_REDEEM_VALUE = 0.01  # one point is worth one cent
INVOICE_PREFIX = "NWO"
CREDIT_NOTE_PREFIX = "CRN"
PAYMENT_TERMS_DAYS = 14
FRAUD_SCORE_LIMIT = 70
REPORT_WIDTH = 78

TIER_DISCOUNTS = {
    "bronze": 0.00,
    "silver": 0.03,
    "gold": 0.06,
    "platinum": 0.10,
}

TIER_THRESHOLDS = OrderedDict(
    [
        ("platinum", 5000.0),
        ("gold", 2000.0),
        ("silver", 500.0),
        ("bronze", 0.0),
    ]
)

CATEGORY_TAX_CLASS = {
    "books": "reduced",
    "food": "reduced",
    "maps": "reduced",
}

COUPONS = {
    "WELCOME10": {"kind": "percent", "value": 10.0, "min_total": 0.0, "first_order_only": True},
    "SPRING5": {"kind": "fixed", "value": 5.0, "min_total": 40.0, "first_order_only": False},
    "TENTFEST": {"kind": "category", "value": 15.0, "category": "tents", "min_total": 0.0,
                 "first_order_only": False},
    "FREESHIP": {"kind": "shipping", "value": 0.0, "min_total": 60.0, "first_order_only": False},
    "BIGSPENDER": {"kind": "percent", "value": 7.5, "min_total": 400.0, "first_order_only": False},
    "EXPIRED20": {"kind": "percent", "value": 20.0, "min_total": 0.0, "first_order_only": False,
                  "expires": date(2023, 12, 31)},
}

SHIPPING_ZONES = {
    "DE": "domestic",
    "AT": "eu1", "NL": "eu1", "BE": "eu1", "DK": "eu1", "FR": "eu1",
    "IT": "eu2", "ES": "eu2", "PL": "eu2", "SE": "eu2",
    "CH": "world1", "NO": "world1", "GB": "world1",
    "US": "world2",
}

SHIPPING_RATES = {
    # zone: (base, per_kg, max_kg)
    "domestic": (4.90, 0.45, 31.5),
    "eu1": (8.90, 0.95, 31.5),
    "eu2": (11.50, 1.20, 31.5),
    "world1": (17.00, 2.10, 30.0),
    "world2": (29.00, 3.60, 30.0),
}

SHIP_METHOD_FACTORS = {
    "standard": 1.0,
    "express": 1.85,
    "economy": 0.8,
    "pickup": 0.0,
}

SUPPLIERS = {
    "ACME": {"lead_days": 7, "min_order_value": 200.0, "country": "DE"},
    "Bergwerk": {"lead_days": 12, "min_order_value": 500.0, "country": "AT"},
    "NordicGear": {"lead_days": 18, "min_order_value": 800.0, "country": "SE"},
    "TrailCo": {"lead_days": 25, "min_order_value": 1000.0, "country": "US"},
}

WAREHOUSES = ("HAM", "MUC")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def money(value: float) -> float:
    """Round to cents using classic commercial rounding (half up)."""
    sign = -1 if value < 0 else 1
    cents = math.floor(abs(value) * 100 + 0.5 + 1e-9)
    return sign * cents / 100.0


def fmt_money(value: float, currency: str = BASE_CURRENCY) -> str:
    return "%s %s" % (format(money(value), ",.2f"), currency)


def fmt_pct(value: float) -> str:
    return "%.1f%%" % (value * 100.0)


def clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    if not b:
        return default
    return a / b


def pad_right(text: str, width: int) -> str:
    text = str(text)
    if len(text) >= width:
        return text[:width]
    return text + " " * (width - len(text))


def pad_left(text: str, width: int) -> str:
    text = str(text)
    if len(text) >= width:
        return text[:width]
    return " " * (width - len(text)) + text


def center(text: str, width: int = REPORT_WIDTH, fill: str = " ") -> str:
    text = str(text)
    if len(text) >= width:
        return text
    left = (width - len(text)) // 2
    right = width - len(text) - left
    return fill * left + text + fill * right


def hr(char: str = "-", width: int = REPORT_WIDTH) -> str:
    return char * width


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def month_key(d: date) -> str:
    return "%04d-%02d" % (d.year, d.month)


def first_of_month(d: date) -> date:
    return date(d.year, d.month, 1)


def last_of_month(d: date) -> date:
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


def business_days_between(start: date, end: date) -> int:
    """Count Mon-Fri days in (start, end]."""
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        cursor = cursor + timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def add_business_days(start: date, n: int) -> date:
    cursor = start
    added = 0
    while added < n:
        cursor = cursor + timedelta(days=1)
        if cursor.weekday() < 5:
            added += 1
    return cursor


def parse_bool(text: Any) -> bool:
    if isinstance(text, bool):
        return text
    return str(text).strip().lower() in ("1", "y", "yes", "true", "t", "on")


def parse_float(text: Any, default: float = 0.0) -> float:
    if text is None:
        return default
    s = str(text).strip().replace(" ", "")
    if not s:
        return default
    # Legacy files come with German decimal commas sometimes.
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return default


def parse_int(text: Any, default: int = 0) -> int:
    try:
        return int(round(parse_float(text, float(default))))
    except (TypeError, ValueError):
        return default


def parse_date(text: str) -> Optional[date]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", text)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        # 2024-13-03 and friends
        return None
    return None


def is_valid_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or "." not in domain:
        return False
    if domain.startswith(".") or domain.endswith("."):
        return False
    return re.match(r"^[A-Za-z0-9._%+-]+$", local) is not None


def checksum_digit(digits: str) -> int:
    """Luhn style check digit, used for invoice numbers since 2017."""
    total = 0
    for idx, ch in enumerate(reversed(digits)):
        n = int(ch)
        if idx % 2 == 0:
            n = n * 2
            if n > 9:
                n -= 9
        total += n
    return (10 - total % 10) % 10


def stable_hash(text: str) -> int:
    """Deterministic string hash (Python's hash() is salted per process)."""
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def chunked(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def wrap_text(text: str, width: int = REPORT_WIDTH, indent: str = "") -> List[str]:
    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent) or [indent]


def tax_rate_for(category: str, country: str, is_business: bool = False, vat_id: str = "") -> float:
    if country in ZERO_TAX_COUNTRIES:
        return 0.0
    if is_business and vat_id and country != "DE" and country in EU_COUNTRIES:
        # intra-community reverse charge
        return 0.0
    if CATEGORY_TAX_CLASS.get(category) == "reduced":
        return REDUCED_TAX_RATE
    return DEFAULT_TAX_RATE


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Product:
    sku: str
    name: str
    category: str
    unit_cost: float
    list_price: float
    weight_kg: float
    dims_cm: Tuple[float, float, float] = (10.0, 10.0, 10.0)
    supplier: str = "ACME"
    active: bool = True
    tags: List[str] = field(default_factory=list)
    reorder_point: int = 10
    reorder_qty: int = 40

    def margin(self) -> float:
        return money(self.list_price - self.unit_cost)

    def margin_pct(self) -> float:
        return safe_div(self.list_price - self.unit_cost, self.list_price)

    def volume_liters(self) -> float:
        l, w, h = self.dims_cm
        return (l * w * h) / 1000.0

    def volumetric_weight(self) -> float:
        # carriers use 5000 cm3 per kg
        l, w, h = self.dims_cm
        return (l * w * h) / 5000.0

    def billable_weight(self) -> float:
        return max(self.weight_kg, self.volumetric_weight())

    def is_bulky(self) -> bool:
        return max(self.dims_cm) > 120 or self.weight_kg > 20


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    country: str
    city: str
    postal_code: str
    tier: str = "bronze"
    joined: date = date(2020, 1, 1)
    points: int = 0
    credit_limit: float = 0.0
    is_business: bool = False
    vat_id: str = ""
    blocked: bool = False
    newsletter: bool = False

    def display(self) -> str:
        return "%s <%s> [%s/%s]" % (self.name, self.email, self.country, self.tier)

    def tier_discount(self) -> float:
        return TIER_DISCOUNTS.get(self.tier, 0.0)


@dataclass
class OrderLine:
    sku: str
    qty: int
    unit_price: Optional[float] = None
    discount_pct: float = 0.0
    gift_wrap: bool = False


@dataclass
class Order:
    order_id: str
    customer_id: str
    order_date: date
    lines: List[OrderLine]
    ship_method: str = "standard"
    coupon: str = ""
    payment_method: str = "card"
    ship_country: str = ""
    status: str = "new"
    notes: str = ""
    redeem_points: int = 0
    warehouse: str = "HAM"

    def total_qty(self) -> int:
        return sum(line.qty for line in self.lines)

    def skus(self) -> List[str]:
        return sorted(set(line.sku for line in self.lines))


@dataclass
class OrderResult:
    order_id: str
    customer_id: str
    status: str
    subtotal: float = 0.0
    discount: float = 0.0
    shipping: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    cost: float = 0.0
    weight: float = 0.0
    points_earned: int = 0
    points_redeemed: int = 0
    messages: List[str] = field(default_factory=list)
    backordered: Dict[str, int] = field(default_factory=dict)
    invoice_no: str = ""
    order_date: Optional[date] = None
    ship_date: Optional[date] = None
    country: str = ""

    def profit(self) -> float:
        return money(self.total - self.tax - self.shipping - self.cost)


@dataclass
class Shipment:
    shipment_id: str
    order_id: str
    carrier: str
    method: str
    weight_kg: float
    cost: float
    ship_date: date
    eta: date
    parcels: int = 1
    tracking: str = ""


@dataclass
class InvoiceLine:
    description: str
    qty: int
    unit_price: float
    tax_rate: float
    net: float
    tax: float


@dataclass
class Invoice:
    number: str
    order_id: str
    customer_id: str
    issue_date: date
    due_date: date
    lines: List[InvoiceLine] = field(default_factory=list)
    net_total: float = 0.0
    tax_total: float = 0.0
    gross_total: float = 0.0
    paid: float = 0.0
    is_credit_note: bool = False

    def open_amount(self) -> float:
        return money(self.gross_total - self.paid)


@dataclass
class Payment:
    payment_id: str
    invoice_no: str
    amount: float
    received: date
    method: str = "transfer"
    reference: str = ""


@dataclass
class StockMovement:
    sku: str
    warehouse: str
    delta: int
    reason: str
    ref: str
    day: date


# ---------------------------------------------------------------------------
# Demo data (normally loaded from the ERP export)
# ---------------------------------------------------------------------------

CATALOG_SEED = [
    # sku, name, category, cost, price, weight, dims, supplier, tags
    ("TNT-100", "Trailhead 2P Tent", "tents", 89.0, 199.0, 2.4, (55, 18, 18), "NordicGear", ["2p", "3season"]),
    ("TNT-200", "Summit 4P Expedition Tent", "tents", 240.0, 549.0, 5.9, (65, 25, 25), "NordicGear", ["4p", "4season"]),
    ("TNT-300", "UltraLite Solo Tent", "tents", 120.0, 289.0, 0.9, (40, 12, 12), "TrailCo", ["1p", "ultralight"]),
    ("TNT-400", "Family Dome 6P", "tents", 150.0, 329.0, 11.5, (75, 30, 30), "ACME", ["6p", "family"]),
    ("SLP-100", "Down Sleeping Bag -5C", "sleeping", 95.0, 229.0, 1.1, (40, 25, 25), "Bergwerk", ["down"]),
    ("SLP-110", "Synthetic Bag +5C", "sleeping", 35.0, 79.0, 1.6, (45, 28, 28), "ACME", ["synthetic"]),
    ("SLP-200", "Inflatable Pad R4", "sleeping", 42.0, 109.0, 0.6, (30, 12, 12), "TrailCo", ["pad"]),
    ("SLP-210", "Foam Pad Classic", "sleeping", 9.0, 24.9, 0.4, (183, 55, 2), "ACME", ["pad", "foam"]),
    ("BCK-100", "Daypack 22L", "packs", 22.0, 59.0, 0.7, (48, 30, 20), "ACME", ["day"]),
    ("BCK-200", "Trekking Pack 55L", "packs", 70.0, 169.0, 1.9, (75, 35, 28), "Bergwerk", ["multi-day"]),
    ("BCK-300", "Expedition Pack 75L", "packs", 110.0, 249.0, 2.5, (85, 38, 30), "NordicGear", ["expedition"]),
    ("BCK-400", "Kids Pack 12L", "packs", 11.0, 29.9, 0.35, (35, 22, 14), "ACME", ["kids"]),
    ("CKW-100", "Titanium Pot 750ml", "cookware", 14.0, 39.9, 0.11, (12, 12, 11), "TrailCo", ["titanium"]),
    ("CKW-110", "Canister Stove", "cookware", 18.0, 49.0, 0.09, (10, 8, 8), "ACME", ["stove"]),
    ("CKW-120", "Gas Canister 230g", "cookware", 3.2, 7.9, 0.37, (11, 11, 9), "ACME", ["fuel", "hazmat"]),
    ("CKW-200", "Cook Set 4 pcs", "cookware", 26.0, 64.0, 0.8, (20, 20, 14), "Bergwerk", ["set"]),
    ("CLT-100", "Merino Base Layer", "clothing", 28.0, 75.0, 0.2, (30, 25, 3), "NordicGear", ["merino"]),
    ("CLT-110", "Fleece Jacket", "clothing", 30.0, 89.0, 0.45, (35, 30, 6), "ACME", ["fleece"]),
    ("CLT-200", "Hardshell Jacket", "clothing", 120.0, 299.0, 0.5, (35, 30, 6), "Bergwerk", ["goretex"]),
    ("CLT-210", "Rain Pants", "clothing", 25.0, 69.0, 0.3, (35, 25, 4), "ACME", ["rain"]),
    ("CLT-300", "Hiking Socks 3-pack", "clothing", 6.0, 19.9, 0.15, (20, 12, 4), "ACME", ["socks"]),
    ("FTW-100", "Approach Shoe", "footwear", 55.0, 139.0, 0.9, (33, 22, 13), "Bergwerk", ["approach"]),
    ("FTW-200", "Leather Hiking Boot", "footwear", 85.0, 219.0, 1.6, (35, 25, 15), "Bergwerk", ["boot"]),
    ("FTW-300", "Trail Runner", "footwear", 48.0, 129.0, 0.6, (32, 21, 12), "TrailCo", ["running"]),
    ("NAV-100", "Compass Pro", "navigation", 12.0, 34.9, 0.08, (12, 7, 3), "NordicGear", ["compass"]),
    ("NAV-200", "GPS Handheld", "navigation", 160.0, 349.0, 0.25, (18, 10, 6), "TrailCo", ["electronics"]),
    ("MAP-100", "Alps Map Set", "maps", 9.0, 24.0, 0.3, (25, 15, 2), "ACME", ["paper"]),
    ("MAP-110", "Scandinavia Map Set", "maps", 10.0, 26.0, 0.32, (25, 15, 2), "NordicGear", ["paper"]),
    ("BOK-100", "Wilderness First Aid Book", "books", 8.0, 22.0, 0.45, (23, 16, 3), "ACME", ["paper", "safety"]),
    ("BOK-110", "Alpine Routes Guide", "books", 11.0, 29.0, 0.6, (24, 17, 3), "Bergwerk", ["paper"]),
    ("FOD-100", "Freeze-Dried Meal Chili", "food", 3.5, 9.9, 0.15, (20, 15, 5), "TrailCo", ["meal"]),
    ("FOD-110", "Freeze-Dried Meal Pasta", "food", 3.4, 9.5, 0.14, (20, 15, 5), "TrailCo", ["meal"]),
    ("FOD-200", "Energy Bar Box (12)", "food", 7.0, 18.0, 0.65, (22, 12, 6), "ACME", ["snack"]),
    ("LGT-100", "Headlamp 400lm", "lighting", 15.0, 44.9, 0.09, (10, 8, 5), "NordicGear", ["electronics"]),
    ("LGT-200", "Camp Lantern", "lighting", 13.0, 34.0, 0.3, (15, 10, 10), "ACME", ["electronics"]),
    ("SAF-100", "First Aid Kit Pro", "safety", 16.0, 45.0, 0.4, (20, 14, 7), "ACME", ["safety"]),
    ("SAF-200", "Avalanche Beacon", "safety", 190.0, 329.0, 0.25, (12, 8, 3), "Bergwerk", ["winter", "electronics"]),
    ("SAF-210", "Snow Shovel", "safety", 25.0, 59.0, 0.6, (70, 25, 8), "Bergwerk", ["winter"]),
    ("FRN-100", "Camp Chair", "furniture", 14.0, 39.0, 1.1, (60, 15, 15), "ACME", ["camp"]),
    ("FRN-200", "Folding Table XL", "furniture", 38.0, 89.0, 6.8, (125, 70, 8), "ACME", ["camp", "bulky"]),
]

CITY_BY_COUNTRY = {
    "DE": [("Hamburg", "20095"), ("Munich", "80331"), ("Berlin", "10115"), ("Cologne", "50667")],
    "AT": [("Vienna", "1010"), ("Innsbruck", "6020")],
    "NL": [("Amsterdam", "1012"), ("Utrecht", "3511")],
    "FR": [("Lyon", "69001"), ("Grenoble", "38000")],
    "IT": [("Bolzano", "39100"), ("Turin", "10121")],
    "SE": [("Stockholm", "11120"), ("Kiruna", "98131")],
    "CH": [("Zurich", "8001"), ("Chamonix", "74400")],
    "GB": [("Leeds", "LS1"), ("Kendal", "LA9")],
    "US": [("Boulder", "80302"), ("Seattle", "98101")],
    "PL": [("Krakow", "30-001")],
    "DK": [("Aarhus", "8000")],
    "NO": [("Bergen", "5003")],
}

FIRST_NAMES = ["Anna", "Ben", "Clara", "David", "Elif", "Finn", "Greta", "Hugo", "Ines", "Jonas",
               "Katja", "Lars", "Mia", "Noah", "Olga", "Paul", "Quinn", "Rosa", "Sven", "Tara",
               "Ulla", "Viktor", "Wanda", "Xaver", "Yara", "Zeno"]
LAST_NAMES = ["Albrecht", "Berg", "Conti", "Dahl", "Eriksen", "Fischer", "Gruber", "Hansen",
              "Iversen", "Jung", "Keller", "Lind", "Moser", "Nyberg", "Olsen", "Peters"]
COMPANY_NAMES = ["Alpenverein Sektion Nord", "Scout Troop 7", "Outdoor School GmbH",
                 "Bergschule Tirol", "Fjell Guides AS"]


def build_catalog() -> List[Product]:
    products = []
    for row in CATALOG_SEED:
        sku, name, category, cost, price, weight, dims, supplier, tags = row
        reorder_point = 4 if price > 200 else (12 if price < 30 else 8)
        reorder_qty = 10 if price > 200 else (60 if price < 30 else 30)
        products.append(Product(
            sku=sku, name=name, category=category, unit_cost=cost, list_price=price,
            weight_kg=weight, dims_cm=tuple(float(d) for d in dims), supplier=supplier,
            tags=list(tags), reorder_point=reorder_point, reorder_qty=reorder_qty,
        ))
    # One discontinued product that still appears in old orders.
    products.append(Product("TNT-050", "Classic A-Frame (discontinued)", "tents", 40.0, 99.0, 3.2,
                            (60, 20, 20), "ACME", active=False, tags=["legacy"]))
    return products


def build_customers(rng: random.Random) -> List[Customer]:
    customers = []
    countries = sorted(CITY_BY_COUNTRY.keys())
    weights = {"DE": 9, "AT": 3, "NL": 2, "FR": 2, "IT": 2, "SE": 2, "CH": 2, "GB": 1,
               "US": 1, "PL": 1, "DK": 1, "NO": 1}
    weighted = []
    for c in countries:
        weighted.extend([c] * weights[c])
    for i in range(32):
        cid = "C%04d" % (1001 + i)
        country = rng.choice(weighted)
        city, postal = rng.choice(CITY_BY_COUNTRY[country])
        is_business = i % 7 == 3
        if is_business:
            name = COMPANY_NAMES[(i // 7) % len(COMPANY_NAMES)]
            email = "orders@%s.example" % slugify(name)[:18]
            vat_id = "%s%09d" % (country, 100000000 + i * 7919) if country in EU_COUNTRIES else ""
            credit_limit = 2500.0
        else:
            name = "%s %s" % (rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES))
            email = "%s@mail.example" % slugify(name).replace("-", ".")
            vat_id = ""
            credit_limit = 0.0
        if i == 13:
            email = "broken-address-at-example"  # known bad record from 2018 import
        joined = date(2019, 1, 1) + timedelta(days=rng.randint(0, 1700))
        spend_hint = rng.random()
        if spend_hint > 0.92:
            tier = "platinum"
        elif spend_hint > 0.75:
            tier = "gold"
        elif spend_hint > 0.45:
            tier = "silver"
        else:
            tier = "bronze"
        customers.append(Customer(
            customer_id=cid, name=name, email=email, country=country, city=city,
            postal_code=postal, tier=tier, joined=joined, points=rng.randint(0, 40) * 25,
            credit_limit=credit_limit, is_business=is_business, vat_id=vat_id,
            blocked=(i == 21), newsletter=rng.random() < 0.4,
        ))
    return customers


def build_orders(rng: random.Random, customers: List[Customer], products: List[Product]) -> List[Order]:
    orders = []
    active_skus = [p.sku for p in products if p.active]
    methods = ["standard"] * 6 + ["express"] * 2 + ["economy", "pickup"]
    payments = ["card"] * 5 + ["paypal"] * 3 + ["invoice"] * 2 + ["prepay"]
    coupons = [""] * 12 + ["WELCOME10", "SPRING5", "TENTFEST", "FREESHIP", "BIGSPENDER", "EXPIRED20", "BOGUS"]
    start = date(2024, 1, 2)
    for i in range(140):
        cust = customers[rng.randrange(len(customers))]
        day = start + timedelta(days=rng.randint(0, 89))
        n_lines = rng.choice([1, 1, 1, 2, 2, 3, 4, 6])
        lines = []
        used = set()
        for _ in range(n_lines):
            sku = rng.choice(active_skus)
            if sku in used:
                continue
            used.add(sku)
            qty = rng.choice([1, 1, 1, 2, 2, 3, 5, 12]) if not sku.startswith("FOD") else rng.choice([4, 8, 12, 24])
            line = OrderLine(sku=sku, qty=qty, gift_wrap=rng.random() < 0.08)
            if rng.random() < 0.05:
                line.discount_pct = 0.1
            lines.append(line)
        if i % 37 == 5:
            lines.append(OrderLine(sku="TNT-050", qty=1))  # discontinued
        if i % 53 == 11:
            lines.append(OrderLine(sku="XXX-999", qty=1))  # unknown sku from a typo
        if i % 41 == 7:
            lines[0].qty = 0  # zero quantity line, should be dropped
        order = Order(
            order_id="SO-%05d" % (24000 + i),
            customer_id=cust.customer_id,
            order_date=day,
            lines=lines,
            ship_method=rng.choice(methods),
            coupon=rng.choice(coupons),
            payment_method=rng.choice(payments),
            ship_country=cust.country if rng.random() > 0.06 else rng.choice(["DE", "AT", "US"]),
            notes="leave at door" if rng.random() < 0.1 else "",
            redeem_points=(rng.choice([0, 0, 0, 100, 250, 500]) if cust.points > 0 else 0),
            warehouse=rng.choice(WAREHOUSES),
        )
        orders.append(order)
    orders.sort(key=lambda o: (o.order_date, o.order_id))
    return orders


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class InventoryError(Exception):
    pass


class Inventory:
    """Multi-warehouse stock keeping.

    Stock is kept per (warehouse, sku).  Reservations are kept separately so
    that 'available' = on_hand - reserved.
    """

    def __init__(self, products: List[Product]):
        self.products = {p.sku: p for p in products}
        self.on_hand = defaultdict(int)  # type: Dict[Tuple[str, str], int]
        self.reserved = defaultdict(int)  # type: Dict[Tuple[str, str], int]
        self.movements = []  # type: List[StockMovement]
        self.today = date(2024, 1, 1)

    def seed_stock(self, rng: random.Random) -> None:
        for sku in sorted(self.products):
            product = self.products[sku]
            for wh in WAREHOUSES:
                if not product.active:
                    qty = 0
                elif product.list_price > 300:
                    qty = rng.randint(0, 6)
                elif product.list_price > 100:
                    qty = rng.randint(2, 15)
                else:
                    qty = rng.randint(5, 60)
                if wh == "MUC" and product.category in ("maps", "books"):
                    qty = qty // 3
                self.receive(sku, wh, qty, "initial", "SEED")

    def has_sku(self, sku: str) -> bool:
        return sku in self.products

    def product(self, sku: str) -> Product:
        if sku not in self.products:
            raise InventoryError("unknown sku %s" % sku)
        return self.products[sku]

    def available(self, sku: str, warehouse: str) -> int:
        key = (warehouse, sku)
        return self.on_hand[key] - self.reserved[key]

    def total_available(self, sku: str) -> int:
        return sum(self.available(sku, wh) for wh in WAREHOUSES)

    def total_on_hand(self, sku: str) -> int:
        return sum(self.on_hand[(wh, sku)] for wh in WAREHOUSES)

    def receive(self, sku: str, warehouse: str, qty: int, reason: str, ref: str) -> None:
        if qty <= 0:
            return
        self.on_hand[(warehouse, sku)] += qty
        self.movements.append(StockMovement(sku, warehouse, qty, reason, ref, self.today))

    def reserve(self, sku: str, warehouse: str, qty: int) -> int:
        """Reserve as much as possible, return the reserved amount."""
        can = max(0, self.available(sku, warehouse))
        take = min(can, qty)
        if take > 0:
            self.reserved[(warehouse, sku)] += take
        return take

    def release(self, sku: str, warehouse: str, qty: int) -> None:
        key = (warehouse, sku)
        self.reserved[key] = max(0, self.reserved[key] - qty)

    def ship(self, sku: str, warehouse: str, qty: int, ref: str) -> None:
        key = (warehouse, sku)
        if self.on_hand[key] < qty:
            raise InventoryError("cannot ship %d x %s from %s, only %d on hand"
                                 % (qty, sku, warehouse, self.on_hand[key]))
        self.on_hand[key] -= qty
        self.reserved[key] = max(0, self.reserved[key] - qty)
        self.movements.append(StockMovement(sku, warehouse, -qty, "ship", ref, self.today))

    def adjust(self, sku: str, warehouse: str, delta: int, reason: str) -> None:
        key = (warehouse, sku)
        new_value = self.on_hand[key] + delta
        if new_value < 0:
            delta = -self.on_hand[key]
            new_value = 0
        self.on_hand[key] = new_value
        self.movements.append(StockMovement(sku, warehouse, delta, reason, "ADJ", self.today))

    def transfer(self, sku: str, source: str, target: str, qty: int) -> int:
        movable = min(qty, max(0, self.available(sku, source)))
        if movable <= 0:
            return 0
        self.on_hand[(source, sku)] -= movable
        self.on_hand[(target, sku)] += movable
        ref = "TR-%s-%s" % (source, target)
        self.movements.append(StockMovement(sku, source, -movable, "transfer-out", ref, self.today))
        self.movements.append(StockMovement(sku, target, movable, "transfer-in", ref, self.today))
        return movable

    def best_warehouse(self, sku: str, qty: int, preferred: str) -> str:
        if self.available(sku, preferred) >= qty:
            return preferred
        best = preferred
        best_qty = self.available(sku, preferred)
        for wh in WAREHOUSES:
            avail = self.available(sku, wh)
            if avail > best_qty:
                best, best_qty = wh, avail
        return best

    def low_stock(self) -> List[Tuple[str, int, int]]:
        result = []
        for sku in sorted(self.products):
            product = self.products[sku]
            if not product.active:
                continue
            total = self.total_available(sku)
            if total <= product.reorder_point:
                result.append((sku, total, product.reorder_point))
        return result

    def valuation(self) -> Dict[str, float]:
        by_category = defaultdict(float)
        for (wh, sku), qty in sorted(self.on_hand.items()):
            if qty <= 0:
                continue
            product = self.products.get(sku)
            if product is None:
                continue
            by_category[product.category] += qty * product.unit_cost
        return {k: money(v) for k, v in sorted(by_category.items())}

    def snapshot(self) -> List[Tuple[str, str, int, int]]:
        rows = []
        for (wh, sku) in sorted(self.on_hand.keys()):
            rows.append((wh, sku, self.on_hand[(wh, sku)], self.reserved[(wh, sku)]))
        return rows

    def movement_summary(self) -> Dict[str, int]:
        summary = Counter()
        for mv in self.movements:
            summary[mv.reason] += mv.delta
        return dict(sorted(summary.items()))


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

@dataclass
class Promotion:
    code: str
    category: str
    pct: float
    start: date
    end: date
    min_qty: int = 1

    def active_on(self, day: date) -> bool:
        return self.start <= day <= self.end


class PricingEngine:
    """Computes customer prices.

    Order of application (do not change, finance relies on it):
        list price -> promotion -> bulk -> tier -> line discount -> coupon
    """

    BULK_STEPS = [(50, 0.12), (20, 0.08), (10, 0.05), (5, 0.02)]

    def __init__(self, products: Dict[str, Product]):
        self.products = products
        self.promotions = []  # type: List[Promotion]
        self.price_overrides = {}  # type: Dict[str, float]
        self.price_history = defaultdict(list)  # type: Dict[str, List[Tuple[date, float]]]
        self.coupon_usage = Counter()
        self.first_order_seen = set()

    def add_promotion(self, promo: Promotion) -> None:
        self.promotions.append(promo)

    def set_price(self, sku: str, price: float, day: date) -> None:
        self.price_history[sku].append((day, price))
        self.price_overrides[sku] = price

    def list_price(self, sku: str) -> float:
        if sku in self.price_overrides:
            return self.price_overrides[sku]
        return self.products[sku].list_price

    def promo_pct(self, sku: str, qty: int, day: date) -> float:
        product = self.products[sku]
        best = 0.0
        for promo in self.promotions:
            if promo.category != product.category and promo.category != "*":
                continue
            if not promo.active_on(day):
                continue
            if qty < promo.min_qty:
                continue
            best = max(best, promo.pct)
        return best

    def bulk_pct(self, qty: int) -> float:
        for threshold, pct in self.BULK_STEPS:
            if qty >= threshold:
                return pct
        return 0.0

    def unit_price(self, sku: str, qty: int, customer: Customer, day: date,
                   line_discount: float = 0.0) -> float:
        price = self.list_price(sku)
        price = price * (1 - self.promo_pct(sku, qty, day))
        price = price * (1 - self.bulk_pct(qty))
        price = price * (1 - customer.tier_discount())
        price = price * (1 - line_discount)
        # never sell below cost + 5% unless it is a clearance item
        floor_price = self.products[sku].unit_cost * 1.05
        if "clearance" not in self.products[sku].tags:
            price = max(price, floor_price)
        return money(price)

    def validate_coupon(self, code: str, customer: Customer, subtotal: float, day: date) -> Tuple[bool, str]:
        if not code:
            return False, "no coupon"
        spec = COUPONS.get(code.upper())
        if spec is None:
            return False, "unknown coupon %s" % code
        expires = spec.get("expires")
        if expires is not None and day > expires:
            return False, "coupon %s expired on %s" % (code, expires.isoformat())
        if subtotal < spec["min_total"]:
            return False, "coupon %s requires %s" % (code, fmt_money(spec["min_total"]))
        if spec["first_order_only"] and customer.customer_id in self.first_order_seen:
            return False, "coupon %s only valid on first order" % code
        return True, "ok"

    def coupon_discount(self, code: str, lines: List[Tuple[str, int, float]], subtotal: float) -> float:
        spec = COUPONS[code.upper()]
        kind = spec["kind"]
        if kind == "percent":
            return money(subtotal * spec["value"] / 100.0)
        if kind == "fixed":
            return money(min(spec["value"], subtotal))
        if kind == "category":
            eligible = 0.0
            for sku, qty, unit in lines:
                if self.products[sku].category == spec["category"]:
                    eligible += qty * unit
            return money(eligible * spec["value"] / 100.0)
        return 0.0

    def mark_order_seen(self, customer_id: str) -> None:
        self.first_order_seen.add(customer_id)

    def margin_check(self, sku: str, unit_price: float) -> float:
        cost = self.products[sku].unit_cost
        return safe_div(unit_price - cost, unit_price)


# ---------------------------------------------------------------------------
# Shipping
# ---------------------------------------------------------------------------

class ShippingCalculator:
    CARRIERS = {
        "domestic": "DHL",
        "eu1": "DHL",
        "eu2": "GLS",
        "world1": "UPS",
        "world2": "FedEx",
    }
    TRANSIT_DAYS = {
        "domestic": 1,
        "eu1": 2,
        "eu2": 4,
        "world1": 5,
        "world2": 8,
    }

    def __init__(self):
        self.quotes_issued = 0
        self.surcharges = {"hazmat": 6.50, "bulky": 19.00, "remote": 8.00}
        self.remote_postcodes = ("98", "LA", "5003")

    def zone_for(self, country: str) -> str:
        return SHIPPING_ZONES.get(country, "world2")

    def split_parcels(self, weight: float, zone: str) -> List[float]:
        max_kg = SHIPPING_RATES[zone][2]
        if weight <= max_kg:
            return [weight]
        parcels = []
        remaining = weight
        while remaining > 0:
            take = min(max_kg, remaining)
            parcels.append(round(take, 3))
            remaining = round(remaining - take, 3)
        return parcels

    def base_cost(self, weight: float, zone: str) -> float:
        base, per_kg, _ = SHIPPING_RATES[zone]
        total = 0.0
        for parcel in self.split_parcels(weight, zone):
            total += base + per_kg * math.ceil(max(parcel, 0.1))
        return money(total)

    def quote(self, weight: float, country: str, method: str, postal_code: str = "",
              hazmat: bool = False, bulky: bool = False) -> float:
        self.quotes_issued += 1
        if method == "pickup":
            return 0.0
        zone = self.zone_for(country)
        cost = self.base_cost(weight, zone) * SHIP_METHOD_FACTORS.get(method, 1.0)
        if hazmat:
            if zone in ("world1", "world2"):
                cost += self.surcharges["hazmat"] * 2
            else:
                cost += self.surcharges["hazmat"]
        if bulky:
            cost += self.surcharges["bulky"]
        for prefix in self.remote_postcodes:
            if postal_code.startswith(prefix):
                cost += self.surcharges["remote"]
                break
        return money(cost)

    def carrier_for(self, country: str, method: str) -> str:
        if method == "pickup":
            return "SELF"
        if method == "express" and self.zone_for(country) != "domestic":
            return "UPS"
        return self.CARRIERS[self.zone_for(country)]

    def eta(self, ship_date: date, country: str, method: str) -> date:
        days = self.TRANSIT_DAYS[self.zone_for(country)]
        if method == "express":
            days = max(1, days // 2)
        elif method == "economy":
            days = days + 3
        elif method == "pickup":
            days = 0
        return add_business_days(ship_date, days)

    def tracking_number(self, carrier: str, order_id: str, parcel: int) -> str:
        digits = "%010d" % (stable_hash(order_id + carrier) % 10 ** 10)
        return "%s%s%02d%d" % (carrier[:2].upper(), digits, parcel, checksum_digit(digits))


# ---------------------------------------------------------------------------
# Loyalty
# ---------------------------------------------------------------------------

class LoyaltyProgram:
    BONUS_CATEGORIES = {"tents": 2.0, "footwear": 1.5}
    EXPIRY_DAYS = 365

    def __init__(self):
        self.ledger = []  # type: List[Tuple[date, str, int, str]]
        self.lifetime_spend = defaultdict(float)

    def earn(self, customer: Customer, amount: float, categories: List[str], day: date, ref: str) -> int:
        if customer.is_business:
            return 0  # B2B customers are excluded from the program (contract 2020/04)
        multiplier = 1.0
        for cat in categories:
            multiplier = max(multiplier, self.BONUS_CATEGORIES.get(cat, 1.0))
        if customer.tier == "platinum":
            multiplier += 0.5
        points = int(amount * LOYALTY_POINTS_PER_EURO * multiplier)
        customer.points += points
        self.lifetime_spend[customer.customer_id] += amount
        self.ledger.append((day, customer.customer_id, points, ref))
        return points

    def redeem(self, customer: Customer, requested: int, max_value: float, day: date, ref: str) -> int:
        if requested <= 0 or customer.points <= 0:
            return 0
        usable = min(requested, customer.points)
        max_points = int(max_value / LOYALTY_REDEEM_VALUE)
        usable = min(usable, max_points)
        usable = usable - usable % 50  # only blocks of 50 points
        if usable <= 0:
            return 0
        customer.points -= usable
        self.ledger.append((day, customer.customer_id, -usable, ref))
        return usable

    def recompute_tier(self, customer: Customer) -> str:
        spend = self.lifetime_spend[customer.customer_id]
        for tier, threshold in TIER_THRESHOLDS.items():
            if spend >= threshold:
                # never downgrade inside the year
                if TIER_ORDER.index(tier) > TIER_ORDER.index(customer.tier):
                    customer.tier = tier
                break
        return customer.tier

    def expire_points(self, customers: Dict[str, Customer], today: date) -> Dict[str, int]:
        cutoff = today - timedelta(days=self.EXPIRY_DAYS)
        expired = {}
        earned_before = defaultdict(int)
        for day, cid, pts, ref in self.ledger:
            if day < cutoff and pts > 0:
                earned_before[cid] += pts
        for cid in sorted(earned_before):
            cust = customers.get(cid)
            if cust is None:
                continue
            lose = min(cust.points, earned_before[cid])
            if lose > 0:
                cust.points -= lose
                expired[cid] = lose
                self.ledger.append((today, cid, -lose, "EXPIRY"))
        return expired

    def statement(self, customer_id: str) -> List[str]:
        lines = []
        balance = 0
        for day, cid, pts, ref in self.ledger:
            if cid != customer_id:
                continue
            balance += pts
            lines.append("%s %-12s %+6d  bal %6d" % (day.isoformat(), ref, pts, balance))
        return lines


TIER_ORDER = ["bronze", "silver", "gold", "platinum"]


# ---------------------------------------------------------------------------
# Invoicing
# ---------------------------------------------------------------------------

class InvoiceBuilder:
    def __init__(self, start_number: int = 7000):
        self.next_number = start_number
        self.invoices = OrderedDict()  # type: Dict[str, Invoice]

    def _new_number(self, prefix: str) -> str:
        self.next_number += 1
        digits = "%06d" % self.next_number
        return "%s-%s-%d" % (prefix, digits, checksum_digit(digits))

    def build(self, order: Order, customer: Customer, priced_lines: List[Tuple[str, str, int, float, float]],
              shipping: float, shipping_tax_rate: float, discount: float, issue_date: date) -> Invoice:
        number = self._new_number(INVOICE_PREFIX)
        due = issue_date + timedelta(days=PAYMENT_TERMS_DAYS if customer.is_business else 0)
        inv = Invoice(number=number, order_id=order.order_id, customer_id=customer.customer_id,
                      issue_date=issue_date, due_date=due)
        for sku, desc, qty, unit, rate in priced_lines:
            net = money(qty * unit)
            tax = money(net * rate)
            inv.lines.append(InvoiceLine(desc, qty, unit, rate, net, tax))
        if discount:
            # discount is distributed at the standard rate (simplification approved by tax advisor)
            rate = priced_lines[0][4] if priced_lines else DEFAULT_TAX_RATE
            inv.lines.append(InvoiceLine("Discount", 1, -discount, rate, -discount, money(-discount * rate)))
        if shipping:
            inv.lines.append(InvoiceLine("Shipping", 1, shipping, shipping_tax_rate, shipping,
                                         money(shipping * shipping_tax_rate)))
        inv.net_total = money(sum(l.net for l in inv.lines))
        inv.tax_total = money(sum(l.tax for l in inv.lines))
        inv.gross_total = money(inv.net_total + inv.tax_total)
        self.invoices[number] = inv
        return inv

    def credit_note(self, original: Invoice, lines: List[InvoiceLine], issue_date: date) -> Invoice:
        number = self._new_number(CREDIT_NOTE_PREFIX)
        note = Invoice(number=number, order_id=original.order_id, customer_id=original.customer_id,
                       issue_date=issue_date, due_date=issue_date, is_credit_note=True)
        for line in lines:
            note.lines.append(InvoiceLine("Return: " + line.description, line.qty, line.unit_price,
                                          line.tax_rate, -line.net, -line.tax))
        note.net_total = money(sum(l.net for l in note.lines))
        note.tax_total = money(sum(l.tax for l in note.lines))
        note.gross_total = money(note.net_total + note.tax_total)
        self.invoices[number] = note
        return note

    def render(self, inv: Invoice, customer: Customer) -> str:
        out = []
        title = "CREDIT NOTE" if inv.is_credit_note else "INVOICE"
        out.append(hr("="))
        out.append(center("%s  %s" % (SHOP_NAME, title)))
        out.append(hr("="))
        out.append("No. %-20s Date %s   Due %s" % (inv.number, inv.issue_date.isoformat(),
                                                    inv.due_date.isoformat()))
        out.append("Order %s" % inv.order_id)
        out.append("Bill to: %s, %s %s, %s" % (customer.name, customer.postal_code, customer.city,
                                                customer.country))
        if customer.vat_id:
            out.append("VAT ID: %s" % customer.vat_id)
        out.append(hr())
        out.append("%-38s %4s %10s %5s %10s" % ("Description", "Qty", "Unit", "VAT", "Net"))
        for line in inv.lines:
            out.append("%-38s %4d %10.2f %4.0f%% %10.2f" % (line.description[:38], line.qty,
                                                            line.unit_price, line.tax_rate * 100, line.net))
        out.append(hr())
        out.append("%62s %10.2f" % ("Net total", inv.net_total))
        out.append("%62s %10.2f" % ("VAT", inv.tax_total))
        out.append("%62s %10.2f" % ("Total " + BASE_CURRENCY, inv.gross_total))
        if any(l.tax_rate == 0 for l in inv.lines) and customer.vat_id:
            out.append("Reverse charge: VAT liability rests with the recipient.")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Payments / accounts receivable
# ---------------------------------------------------------------------------

class PaymentLedger:
    def __init__(self):
        self.payments = []  # type: List[Payment]
        self.counter = 0
        self.unmatched = []  # type: List[Payment]

    def record(self, invoice: Optional[Invoice], amount: float, day: date, method: str,
               reference: str = "") -> Payment:
        self.counter += 1
        pay = Payment("PAY-%05d" % self.counter, invoice.number if invoice else "", money(amount),
                      day, method, reference)
        self.payments.append(pay)
        if invoice is None:
            self.unmatched.append(pay)
        else:
            invoice.paid = money(invoice.paid + amount)
        return pay

    def aging(self, invoices: Iterable[Invoice], today: date) -> Dict[str, float]:
        buckets = OrderedDict([("current", 0.0), ("1-30", 0.0), ("31-60", 0.0), ("61-90", 0.0), ("90+", 0.0)])
        for inv in invoices:
            if inv.is_credit_note:
                continue
            open_amt = inv.open_amount()
            if open_amt <= 0.004:
                continue
            overdue = (today - inv.due_date).days
            if overdue <= 0:
                buckets["current"] += open_amt
            elif overdue <= 30:
                buckets["1-30"] += open_amt
            elif overdue <= 60:
                buckets["31-60"] += open_amt
            elif overdue <= 90:
                buckets["61-90"] += open_amt
            else:
                buckets["90+"] += open_amt
        return OrderedDict((k, money(v)) for k, v in buckets.items())

    def total_received(self) -> float:
        return money(sum(p.amount for p in self.payments))


# ---------------------------------------------------------------------------
# Fraud screening (rule based, from the 2018 "quick fix")
# ---------------------------------------------------------------------------

class FraudScreen:
    def __init__(self):
        self.history = defaultdict(list)  # customer_id -> list of (date, total)
        self.flags = []  # type: List[Tuple[str, int, List[str]]]

    def score(self, order: Order, customer: Customer, total: float) -> Tuple[int, List[str]]:
        score = 0
        reasons = []
        if order.ship_country and order.ship_country != customer.country:
            score += 25
            reasons.append("ship country differs")
        if total > 1500:
            score += 20
            reasons.append("high value")
        if order.ship_method == "express" and total > 600:
            score += 10
            reasons.append("express high value")
        if not is_valid_email(customer.email):
            score += 30
            reasons.append("invalid email")
        recent = [t for d, t in self.history[customer.customer_id] if (order.order_date - d).days <= 3]
        if len(recent) >= 2:
            score += 15 * (len(recent) - 1)
            reasons.append("%d orders in 3 days" % (len(recent) + 1))
        if order.payment_method == "invoice" and not customer.is_business:
            score += 10
            reasons.append("consumer on invoice")
        if (order.order_date - customer.joined).days < 30:
            score += 10
            reasons.append("new account")
        self.history[customer.customer_id].append((order.order_date, total))
        if score >= 40:
            self.flags.append((order.order_id, score, reasons))
        return score, reasons


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

class AuditTrail:
    """Collects human readable log lines.  Replaced the logging module in 2016
    because the ops team wanted the log inside the nightly e-mail."""

    def __init__(self):
        self.entries = []  # type: List[str]
        self.depth = 0
        self.section_counts = Counter()

    def log(self, message: str) -> None:
        self.entries.append("  " * self.depth + message)

    def section(self, name: str) -> "_AuditSection":
        return _AuditSection(self, name)

    def dump(self, limit: int = 0) -> List[str]:
        if limit and len(self.entries) > limit:
            return self.entries[:limit] + ["... %d more lines" % (len(self.entries) - limit)]
        return list(self.entries)


class _AuditSection:
    def __init__(self, trail: AuditTrail, name: str):
        self.trail = trail
        self.name = name

    def __enter__(self) -> "_AuditSection":
        self.trail.section_counts[self.name] += 1
        self.trail.log("[begin %s]" % self.name)
        self.trail.depth += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.trail.depth -= 1
        if exc_type is not None:
            self.trail.log("[end %s: %s]" % (self.name, exc_type.__name__))
        else:
            self.trail.log("[end %s]" % self.name)
        return False


# ---------------------------------------------------------------------------
# THE order run.  Everything happens here.  Sorry.
# ---------------------------------------------------------------------------

def process_orders(orders: List[Order], customers: List[Customer], inventory: Inventory,
                   pricing: PricingEngine, shipping: ShippingCalculator, loyalty: LoyaltyProgram,
                   invoicer: InvoiceBuilder, ledger: PaymentLedger, fraud: FraudScreen,
                   audit: AuditTrail, run_date: date,
                   options: Optional[Dict[str, Any]] = None) -> Tuple[List[OrderResult], List[Shipment], Dict[str, Any]]:
    """Process a batch of orders end to end.

    Steps: validate, dedupe, price, apply coupons/points, fraud screen,
    credit check, reserve stock, rebalance warehouses, ship, tax, invoice,
    take payment, award loyalty points, build results.

    Returns (results, shipments, stats).
    """
    # --- options -----------------------------------------------------------
    if options is None:
        options = {}
    allow_backorder = bool(options.get("allow_backorder", True))
    allow_partial = bool(options.get("allow_partial", True))
    max_transfer_rounds = int(options.get("max_transfer_rounds", 3))
    min_order_value = float(options.get("min_order_value", 5.0))
    gift_wrap_fee = float(options.get("gift_wrap_fee", 3.5))
    verbose = bool(options.get("verbose", False))
    fraud_limit = int(options.get("fraud_limit", FRAUD_SCORE_LIMIT))
    if options.get("strict"):
        allow_backorder = False
        allow_partial = False
        audit.log("strict mode: no backorders, no partial shipments")
    audit.log("process_orders: %d orders, run date %s" % (len(orders), run_date.isoformat()))

    # --- customer index ----------------------------------------------------
    customer_by_id = {}
    blocked_ids = set()
    bad_email_ids = set()
    business_ids = set()
    for cust in customers:
        if cust.customer_id in customer_by_id:
            audit.log("duplicate customer record %s ignored" % cust.customer_id)
            continue
        customer_by_id[cust.customer_id] = cust
        if cust.blocked:
            blocked_ids.add(cust.customer_id)
        if not is_valid_email(cust.email):
            bad_email_ids.add(cust.customer_id)
        if cust.is_business:
            business_ids.add(cust.customer_id)
    audit.log("customers: %d indexed, %d blocked, %d bad e-mail, %d business"
              % (len(customer_by_id), len(blocked_ids), len(bad_email_ids), len(business_ids)))

    # --- nothing to do? ----------------------------------------------------
    if not orders:
        audit.log("no orders to process")
        empty_stats = {"processed": 0, "rejected": 0, "shipped": 0, "revenue": 0.0}
        return [], [], empty_stats

    # --- validation --------------------------------------------------------
    valid_orders = []  # type: List[Order]
    rejected = OrderedDict()  # type: Dict[str, str]
    line_warnings = defaultdict(list)  # type: Dict[str, List[str]]
    with audit.section("validate"):
        for order in orders:
            cust = customer_by_id.get(order.customer_id)
            if cust is None:
                rejected[order.order_id] = "unknown customer %s" % order.customer_id
                continue
            if order.customer_id in blocked_ids:
                rejected[order.order_id] = "customer blocked"
                continue
            if order.status not in ("new", "retry"):
                rejected[order.order_id] = "status %s not processable" % order.status
                continue
            if order.ship_method not in SHIP_METHOD_FACTORS:
                line_warnings[order.order_id].append("unknown ship method %s, using standard" % order.ship_method)
                order.ship_method = "standard"
            if not order.ship_country:
                order.ship_country = cust.country
            merged = OrderedDict()  # type: Dict[str, OrderLine]
            for line in order.lines:
                if line.qty <= 0:
                    line_warnings[order.order_id].append("dropped %s: qty %d" % (line.sku, line.qty))
                    continue
                if not inventory.has_sku(line.sku):
                    line_warnings[order.order_id].append("dropped %s: unknown sku" % line.sku)
                    continue
                if not inventory.product(line.sku).active:
                    line_warnings[order.order_id].append("dropped %s: discontinued" % line.sku)
                    continue
                if line.sku in merged:
                    merged[line.sku].qty += line.qty
                    line_warnings[order.order_id].append("merged duplicate line %s" % line.sku)
                else:
                    merged[line.sku] = OrderLine(line.sku, line.qty, line.unit_price, line.discount_pct,
                                                 line.gift_wrap)
            if not merged:
                rejected[order.order_id] = "no valid lines"
                continue
            if len(merged) > MAX_LINES_PER_ORDER:
                rejected[order.order_id] = "too many lines (%d)" % len(merged)
                continue
            order.lines = list(merged.values())
            valid_orders.append(order)
        audit.log("valid %d, rejected %d" % (len(valid_orders), len(rejected)))

    # --- duplicate detection ---------------------------------------------
    # Customers double-click "buy" a lot.  Same customer, same day, same lines
    # within one batch is treated as a duplicate.
    seen_signatures = {}  # type: Dict[Tuple, str]
    deduped_orders = []  # type: List[Order]
    duplicates = OrderedDict()  # type: Dict[str, str]
    for order in valid_orders:
        signature = (
            order.customer_id,
            order.order_date.toordinal(),
            tuple(sorted((l.sku, l.qty) for l in order.lines)),
            order.ship_method,
        )
        if signature in seen_signatures:
            original_id = seen_signatures[signature]
            duplicates[order.order_id] = original_id
            rejected[order.order_id] = "duplicate of %s" % original_id
            audit.log("duplicate %s -> %s" % (order.order_id, original_id))
        else:
            seen_signatures[signature] = order.order_id
            deduped_orders.append(order)

    # --- priority -----------------------------------------------------------
    # Express first, then platinum/gold, then B2B, then everything else by date.
    priority = {}  # type: Dict[str, int]
    for order in deduped_orders:
        cust = customer_by_id[order.customer_id]
        score = 0
        if order.ship_method == "express":
            score += 100
        if cust.tier == "platinum":
            score += 50
        elif cust.tier == "gold":
            score += 30
        elif cust.tier == "silver":
            score += 10
        if cust.is_business:
            score += 20
        if order.status == "retry":
            score += 15
        if "urgent" in order.notes.lower():
            score += 40
        age_days = (run_date - order.order_date).days
        score += min(age_days, 30)
        priority[order.order_id] = score
    processing_list = sorted(deduped_orders, key=lambda o: (-priority[o.order_id], o.order_date, o.order_id))
    if verbose:
        audit.log("top priorities: %s" % ", ".join(
            "%s=%d" % (o.order_id, priority[o.order_id]) for o in processing_list[:5]))

    # --- pricing ------------------------------------------------------------
    priced_lines_by_order = {}  # type: Dict[str, List[Tuple[str, int, float]]]
    subtotal_by_order = {}  # type: Dict[str, float]
    cost_by_order = {}  # type: Dict[str, float]
    weight_by_order = {}  # type: Dict[str, float]
    categories_by_order = {}  # type: Dict[str, List[str]]
    hazmat_orders = set()
    bulky_orders = set()
    gift_fee_by_order = {}  # type: Dict[str, float]
    low_margin_lines = []  # type: List[Tuple[str, str, float]]
    with audit.section("pricing"):
        for order in processing_list:
            cust = customer_by_id[order.customer_id]
            priced = []
            subtotal = 0.0
            cost = 0.0
            weight = 0.0
            cats = set()
            gift_fee = 0.0
            for line in order.lines:
                product = inventory.product(line.sku)
                if line.unit_price is not None:
                    unit = money(line.unit_price)
                else:
                    unit = pricing.unit_price(line.sku, line.qty, cust, order.order_date, line.discount_pct)
                priced.append((line.sku, line.qty, unit))
                subtotal += unit * line.qty
                cost += product.unit_cost * line.qty
                weight += product.billable_weight() * line.qty
                cats.add(product.category)
                if "hazmat" in product.tags:
                    hazmat_orders.add(order.order_id)
                if product.is_bulky() or "bulky" in product.tags:
                    bulky_orders.add(order.order_id)
                if line.gift_wrap:
                    gift_fee += gift_wrap_fee * line.qty
                margin = pricing.margin_check(line.sku, unit)
                if margin < 0.15:
                    low_margin_lines.append((order.order_id, line.sku, round(margin, 4)))
            priced_lines_by_order[order.order_id] = priced
            subtotal_by_order[order.order_id] = money(subtotal + gift_fee)
            cost_by_order[order.order_id] = money(cost)
            weight_by_order[order.order_id] = round(weight, 3)
            categories_by_order[order.order_id] = sorted(cats)
            gift_fee_by_order[order.order_id] = money(gift_fee)
        audit.log("priced %d orders, %d low-margin lines" % (len(priced_lines_by_order), len(low_margin_lines)))

    # --- coupons --------------------------------------------------------------
    discount_by_order = defaultdict(float)  # type: Dict[str, float]
    free_shipping_orders = set()
    coupon_messages = {}  # type: Dict[str, str]
    coupon_counter = Counter()
    for order in processing_list:
        if not order.coupon:
            continue
        cust = customer_by_id[order.customer_id]
        subtotal = subtotal_by_order[order.order_id]
        ok, reason = pricing.validate_coupon(order.coupon, cust, subtotal, order.order_date)
        if not ok:
            coupon_messages[order.order_id] = reason
            coupon_counter["rejected"] += 1
            continue
        code = order.coupon.upper()
        spec = COUPONS[code]
        if spec["kind"] == "shipping":
            free_shipping_orders.add(order.order_id)
            coupon_messages[order.order_id] = "free shipping via %s" % code
        else:
            amount = pricing.coupon_discount(code, priced_lines_by_order[order.order_id], subtotal)
            # never discount below the cost of goods
            max_discount = max(0.0, subtotal - cost_by_order[order.order_id])
            if amount > max_discount:
                coupon_messages[order.order_id] = "coupon %s capped from %.2f to %.2f" % (code, amount, max_discount)
                amount = money(max_discount)
            else:
                coupon_messages[order.order_id] = "coupon %s: -%.2f" % (code, amount)
            discount_by_order[order.order_id] += amount
        pricing.coupon_usage[code] += 1
        coupon_counter["accepted"] += 1
    for order in processing_list:
        pricing.mark_order_seen(order.customer_id)
    audit.log("coupons: %d accepted, %d rejected" % (coupon_counter["accepted"], coupon_counter["rejected"]))

    # --- loyalty points redemption ------------------------------------------
    redeemed_by_order = {}  # type: Dict[str, int]
    for order in processing_list:
        if order.redeem_points <= 0:
            continue
        cust = customer_by_id[order.customer_id]
        if cust.is_business:
            line_warnings[order.order_id].append("B2B customers cannot redeem points")
            continue
        remaining = subtotal_by_order[order.order_id] - discount_by_order[order.order_id]
        # points may pay at most 50% of the goods
        cap_value = money(remaining * 0.5)
        used = loyalty.redeem(cust, order.redeem_points, cap_value, order.order_date, order.order_id)
        if used > 0:
            value = money(used * LOYALTY_REDEEM_VALUE)
            discount_by_order[order.order_id] = money(discount_by_order[order.order_id] + value)
            redeemed_by_order[order.order_id] = used
        elif order.redeem_points > 0:
            line_warnings[order.order_id].append("could not redeem %d points" % order.redeem_points)

    # --- fraud screening ----------------------------------------------------
    fraud_scores = {}  # type: Dict[str, int]
    held_orders = OrderedDict()  # type: Dict[str, List[str]]
    with audit.section("fraud"):
        for order in processing_list:
            cust = customer_by_id[order.customer_id]
            estimate = subtotal_by_order[order.order_id] - discount_by_order[order.order_id]
            score, reasons = fraud.score(order, cust, estimate)
            fraud_scores[order.order_id] = score
            if score < fraud_limit:
                continue
            if cust.is_business and cust.tier in ("gold", "platinum"):
                audit.log("%s score %d but trusted B2B, passing" % (order.order_id, score))
                continue
            held_orders[order.order_id] = reasons
            audit.log("HOLD %s score %d: %s" % (order.order_id, score, "; ".join(reasons)))

    # --- credit check for invoice payers -----------------------------------
    credit_blocked = OrderedDict()  # type: Dict[str, str]
    open_by_customer = defaultdict(float)
    for inv in invoicer.invoices.values():
        if not inv.is_credit_note:
            open_by_customer[inv.customer_id] += inv.open_amount()
    for order in processing_list:
        if order.order_id in held_orders:
            continue
        if order.payment_method != "invoice":
            continue
        cust = customer_by_id[order.customer_id]
        estimate = subtotal_by_order[order.order_id] - discount_by_order[order.order_id]
        limit = cust.credit_limit if cust.is_business else 250.0
        exposure = open_by_customer[cust.customer_id] + estimate
        if exposure > limit:
            credit_blocked[order.order_id] = "exposure %.2f > limit %.2f" % (exposure, limit)
            audit.log("credit block %s (%s)" % (order.order_id, credit_blocked[order.order_id]))
        else:
            open_by_customer[cust.customer_id] = exposure

    # --- stock reservation --------------------------------------------------
    reservations_by_order = defaultdict(list)  # type: Dict[str, List[Tuple[str, str, int]]]
    backorders_by_order = defaultdict(dict)  # type: Dict[str, Dict[str, int]]
    with audit.section("reserve"):
        for order in processing_list:
            if order.order_id in held_orders or order.order_id in credit_blocked:
                continue
            for line in order.lines:
                wh = inventory.best_warehouse(line.sku, line.qty, order.warehouse)
                got = inventory.reserve(line.sku, wh, line.qty)
                if got > 0:
                    reservations_by_order[order.order_id].append((line.sku, wh, got))
                short = line.qty - got
                if short > 0:
                    # try the other warehouse for the remainder
                    for other in WAREHOUSES:
                        if other == wh or short <= 0:
                            continue
                        extra = inventory.reserve(line.sku, other, short)
                        if extra > 0:
                            reservations_by_order[order.order_id].append((line.sku, other, extra))
                            short -= extra
                if short > 0:
                    backorders_by_order[order.order_id][line.sku] = short
        audit.log("reserved for %d orders, %d with backorders"
                  % (len(reservations_by_order), len(backorders_by_order)))

    # --- warehouse rebalancing ---------------------------------------------
    # Move stock between warehouses for orders that are split across both.
    rounds = 0
    transfers_done = []  # type: List[Tuple[str, str, str, int]]
    while rounds < max_transfer_rounds:
        rounds += 1
        moved_this_round = 0
        for order_id in sorted(reservations_by_order):
            res = reservations_by_order[order_id]
            warehouses_used = sorted(set(wh for _, wh, _ in res))
            if len(warehouses_used) < 2:
                continue
            target = warehouses_used[0]
            for sku, wh, qty in list(res):
                if wh == target:
                    continue
                inventory.release(sku, wh, qty)
                moved = inventory.transfer(sku, wh, target, qty)
                if moved == qty:
                    inventory.reserve(sku, target, qty)
                    res.remove((sku, wh, qty))
                    res.append((sku, target, qty))
                    transfers_done.append((order_id, sku, "%s->%s" % (wh, target), qty))
                    moved_this_round += qty
                else:
                    # could not move everything, put the reservation back
                    if moved > 0:
                        inventory.transfer(sku, target, wh, moved)
                    inventory.reserve(sku, wh, qty)
        if moved_this_round == 0:
            break
        audit.log("rebalance round %d moved %d units" % (rounds, moved_this_round))

    # --- backorder / partial shipment policy -------------------------------
    cancelled_for_stock = OrderedDict()  # type: Dict[str, str]
    partial_orders = set()
    for order in processing_list:
        missing = backorders_by_order.get(order.order_id)
        if not missing:
            continue
        reserved_units = sum(q for _, _, q in reservations_by_order.get(order.order_id, []))
        if reserved_units == 0 and not allow_backorder:
            cancelled_for_stock[order.order_id] = "nothing in stock"
        elif reserved_units > 0 and not allow_partial:
            if allow_backorder:
                # hold everything until complete
                for sku, wh, qty in reservations_by_order[order.order_id]:
                    inventory.release(sku, wh, qty)
                reservations_by_order[order.order_id] = []
                line_warnings[order.order_id].append("waiting for complete delivery")
            else:
                cancelled_for_stock[order.order_id] = "partial not allowed"
        elif reserved_units > 0:
            partial_orders.add(order.order_id)
        if order.order_id in cancelled_for_stock:
            for sku, wh, qty in reservations_by_order.get(order.order_id, []):
                inventory.release(sku, wh, qty)
            reservations_by_order[order.order_id] = []
            audit.log("cancel %s: %s" % (order.order_id, cancelled_for_stock[order.order_id]))

    # --- shipping cost --------------------------------------------------------
    shipping_by_order = {}  # type: Dict[str, float]
    carrier_by_order = {}  # type: Dict[str, str]
    for order in processing_list:
        if order.order_id in held_orders or order.order_id in credit_blocked:
            continue
        if order.order_id in cancelled_for_stock:
            continue
        cust = customer_by_id[order.customer_id]
        goods_value = subtotal_by_order[order.order_id] - discount_by_order[order.order_id]
        country = order.ship_country or cust.country
        if order.order_id in free_shipping_orders:
            cost = 0.0
        elif goods_value >= FREE_SHIPPING_THRESHOLD and country == "DE" and order.ship_method == "standard":
            cost = 0.0
        else:
            cost = shipping.quote(
                weight_by_order[order.order_id], country, order.ship_method,
                postal_code=cust.postal_code if country == cust.country else "",
                hazmat=order.order_id in hazmat_orders,
                bulky=order.order_id in bulky_orders,
            )
        if order.order_id in partial_orders and cost > 0:
            # second parcel for the backorder is on us, but charge 50% now
            cost = money(cost * 1.5)
        shipping_by_order[order.order_id] = cost
        carrier_by_order[order.order_id] = shipping.carrier_for(country, order.ship_method)

    # --- tax ----------------------------------------------------------------
    tax_by_order = {}  # type: Dict[str, float]
    line_tax_rates = {}  # type: Dict[str, List[float]]
    shipping_tax_rate_by_order = {}  # type: Dict[str, float]
    for order_id, ship_cost in shipping_by_order.items():
        order = next(o for o in processing_list if o.order_id == order_id)
        cust = customer_by_id[order.customer_id]
        country = order.ship_country or cust.country
        rates = []
        tax = 0.0
        goods = subtotal_by_order[order_id] - gift_fee_by_order[order_id]
        discount_share = safe_div(discount_by_order[order_id], goods) if goods else 0.0
        for sku, qty, unit in priced_lines_by_order[order_id]:
            category = inventory.product(sku).category
            rate = tax_rate_for(category, country, cust.is_business, cust.vat_id)
            rates.append(rate)
            tax += unit * qty * (1 - discount_share) * rate
        std_rate = tax_rate_for("general", country, cust.is_business, cust.vat_id)
        tax += gift_fee_by_order[order_id] * std_rate
        # shipping follows the main rate of the goods (highest rate wins)
        ship_rate = max(rates) if rates else std_rate
        tax += ship_cost * ship_rate
        tax_by_order[order_id] = money(tax)
        line_tax_rates[order_id] = rates
        shipping_tax_rate_by_order[order_id] = ship_rate

    # --- totals ---------------------------------------------------------------
    total_by_order = {}  # type: Dict[str, float]
    below_minimum = OrderedDict()  # type: Dict[str, float]
    for order_id in tax_by_order:
        net_goods = subtotal_by_order[order_id] - discount_by_order[order_id]
        if net_goods < 0:
            net_goods = 0.0
        total = net_goods + shipping_by_order[order_id] + tax_by_order[order_id]
        total = money(total)
        if net_goods < min_order_value:
            below_minimum[order_id] = total
            line_warnings[order_id].append("below minimum order value %.2f" % min_order_value)
        total_by_order[order_id] = total
    for order_id in below_minimum:
        order = next(o for o in processing_list if o.order_id == order_id)
        for sku, wh, qty in reservations_by_order.get(order_id, []):
            inventory.release(sku, wh, qty)
        reservations_by_order[order_id] = []
        cancelled_for_stock[order_id] = "below minimum order value"
        del total_by_order[order_id]
        audit.log("cancel %s: below minimum (%.2f)" % (order.order_id, below_minimum[order_id]))

    # --- invoicing --------------------------------------------------------------
    invoice_by_order = {}  # type: Dict[str, Invoice]
    with audit.section("invoicing"):
        for order in processing_list:
            if order.order_id not in total_by_order:
                continue
            cust = customer_by_id[order.customer_id]
            rates = line_tax_rates[order.order_id]
            invoice_lines = []
            for (sku, qty, unit), rate in zip(priced_lines_by_order[order.order_id], rates):
                product = inventory.product(sku)
                invoice_lines.append((sku, "%s %s" % (sku, product.name), qty, unit, rate))
            if gift_fee_by_order[order.order_id]:
                std = tax_rate_for("general", order.ship_country, cust.is_business, cust.vat_id)
                invoice_lines.append(("GIFT", "Gift wrapping", 1, gift_fee_by_order[order.order_id], std))
            inv = invoicer.build(order, cust, invoice_lines, shipping_by_order[order.order_id],
                                 shipping_tax_rate_by_order[order.order_id],
                                 money(discount_by_order[order.order_id]), run_date)
            invoice_by_order[order.order_id] = inv
            if abs(inv.gross_total - total_by_order[order.order_id]) > 0.05:
                audit.log("rounding diff on %s: invoice %.2f vs order %.2f"
                          % (order.order_id, inv.gross_total, total_by_order[order.order_id]))
        audit.log("issued %d invoices" % len(invoice_by_order))

    # --- payment capture -------------------------------------------------------
    paid_orders = set()
    awaiting_payment = OrderedDict()  # type: Dict[str, str]
    payment_failures = OrderedDict()  # type: Dict[str, str]
    for order in processing_list:
        inv = invoice_by_order.get(order.order_id)
        if inv is None:
            continue
        method = order.payment_method
        if method in ("card", "paypal"):
            # Card declines are simulated from the order id for the demo.
            if stable_hash(order.order_id) % 23 == 0:
                payment_failures[order.order_id] = "%s declined" % method
                awaiting_payment[order.order_id] = "retry payment"
                continue
            ledger.record(inv, inv.gross_total, run_date, method, order.order_id)
            paid_orders.add(order.order_id)
        elif method == "prepay":
            awaiting_payment[order.order_id] = "waiting for bank transfer"
        elif method == "invoice":
            # ships on account
            paid_orders.add(order.order_id)
        else:
            payment_failures[order.order_id] = "unknown payment method %s" % method
            awaiting_payment[order.order_id] = "manual check"

    # --- fulfilment -------------------------------------------------------------
    shipments = []  # type: List[Shipment]
    shipped_orders = set()
    ship_errors = OrderedDict()  # type: Dict[str, str]
    ship_date = add_business_days(run_date, 0 if run_date.weekday() < 5 else 1)
    with audit.section("fulfilment"):
        for order in processing_list:
            if order.order_id not in paid_orders:
                continue
            res = reservations_by_order.get(order.order_id, [])
            if not res:
                continue
            by_warehouse = defaultdict(list)
            for sku, wh, qty in res:
                by_warehouse[wh].append((sku, qty))
            try:
                for wh in sorted(by_warehouse):
                    weight = 0.0
                    for sku, qty in by_warehouse[wh]:
                        inventory.ship(sku, wh, qty, order.order_id)
                        weight += inventory.product(sku).billable_weight() * qty
                    country = order.ship_country
                    carrier = carrier_by_order.get(order.order_id, "DHL")
                    zone = shipping.zone_for(country)
                    parcels = len(shipping.split_parcels(weight, zone))
                    shipment = Shipment(
                        shipment_id="SH-%s-%s" % (order.order_id[3:], wh),
                        order_id=order.order_id,
                        carrier=carrier,
                        method=order.ship_method,
                        weight_kg=round(weight, 2),
                        cost=shipping_by_order[order.order_id] if wh == sorted(by_warehouse)[0] else 0.0,
                        ship_date=ship_date,
                        eta=shipping.eta(ship_date, country, order.ship_method),
                        parcels=parcels,
                        tracking=shipping.tracking_number(carrier, order.order_id + wh, parcels),
                    )
                    shipments.append(shipment)
                shipped_orders.add(order.order_id)
            except InventoryError as exc:
                ship_errors[order.order_id] = str(exc)
                audit.log("ship error %s: %s" % (order.order_id, exc))
        audit.log("shipped %d orders in %d shipments" % (len(shipped_orders), len(shipments)))

    # --- loyalty points ------------------------------------------------------------
    points_by_order = {}  # type: Dict[str, int]
    tier_changes = []  # type: List[Tuple[str, str, str]]
    for order in processing_list:
        if order.order_id not in shipped_orders:
            continue
        cust = customer_by_id[order.customer_id]
        before = cust.tier
        net_goods = subtotal_by_order[order.order_id] - discount_by_order[order.order_id]
        earned = loyalty.earn(cust, net_goods, categories_by_order[order.order_id], run_date, order.order_id)
        points_by_order[order.order_id] = earned
        after = loyalty.recompute_tier(cust)
        if after != before:
            tier_changes.append((cust.customer_id, before, after))
            audit.log("tier upgrade %s: %s -> %s" % (cust.customer_id, before, after))

    # --- results -----------------------------------------------------------------
    results = []  # type: List[OrderResult]
    for order in orders:
        oid = order.order_id
        if oid in rejected:
            status = "rejected"
        elif oid in held_orders:
            status = "fraud-hold"
        elif oid in credit_blocked:
            status = "credit-hold"
        elif oid in cancelled_for_stock:
            status = "cancelled"
        elif oid in ship_errors:
            status = "error"
        elif oid in shipped_orders and oid in partial_orders:
            status = "partial"
        elif oid in shipped_orders:
            status = "shipped"
        elif oid in awaiting_payment:
            status = "awaiting-payment"
        else:
            status = "backorder"
        result = OrderResult(order_id=oid, customer_id=order.customer_id, status=status,
                             order_date=order.order_date, country=order.ship_country)
        if oid in total_by_order:
            result.subtotal = subtotal_by_order[oid]
            result.discount = money(discount_by_order[oid])
            result.shipping = shipping_by_order[oid]
            result.tax = tax_by_order[oid]
            result.total = total_by_order[oid]
            result.cost = cost_by_order[oid]
            result.weight = weight_by_order[oid]
        result.points_earned = points_by_order.get(oid, 0)
        result.points_redeemed = redeemed_by_order.get(oid, 0)
        result.backordered = dict(sorted(backorders_by_order.get(oid, {}).items()))
        if oid in invoice_by_order:
            result.invoice_no = invoice_by_order[oid].number
        if oid in shipped_orders:
            result.ship_date = ship_date
        for reason_map in (rejected, credit_blocked, cancelled_for_stock, ship_errors,
                           awaiting_payment, payment_failures, coupon_messages):
            if oid in reason_map:
                result.messages.append(str(reason_map[oid]))
        if oid in held_orders:
            result.messages.append("fraud: " + ", ".join(held_orders[oid]))
        result.messages.extend(line_warnings.get(oid, []))
        results.append(result)

    # --- statistics ---------------------------------------------------------------
    stats = OrderedDict()  # type: Dict[str, Any]
    status_counts = Counter(r.status for r in results)
    revenue = 0.0
    tax_total = 0.0
    shipping_total = 0.0
    discount_total = 0.0
    cogs = 0.0
    for r in results:
        if r.status not in ("shipped", "partial", "awaiting-payment"):
            continue
        revenue += r.total
        tax_total += r.tax
        shipping_total += r.shipping
        discount_total += r.discount
        cogs += r.cost
    stats["processed"] = len(results)
    stats["by_status"] = dict(sorted(status_counts.items()))
    stats["revenue"] = money(revenue)
    stats["tax"] = money(tax_total)
    stats["shipping"] = money(shipping_total)
    stats["discounts"] = money(discount_total)
    stats["cogs"] = money(cogs)
    stats["gross_margin"] = money(revenue - tax_total - shipping_total - cogs)
    stats["duplicates"] = len(duplicates)
    stats["transfers"] = len(transfers_done)
    stats["tier_changes"] = tier_changes
    stats["low_margin_lines"] = low_margin_lines
    stats["payment_failures"] = list(payment_failures.items())
    stats["fraud_scores_max"] = max(fraud_scores.values()) if fraud_scores else 0
    stats["coupon_usage"] = dict(sorted(pricing.coupon_usage.items()))
    audit.log("revenue %s, margin %s" % (fmt_money(stats["revenue"]), fmt_money(stats["gross_margin"])))
    return results, shipments, stats


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class ReportGenerator:
    """Builds the monthly management report (plain text, goes into the e-mail).

    The report format is parsed by the controlling team's Excel macro, so the
    section titles and column widths must stay exactly as they are.
    """

    SPARK_CHARS = " .:-=+*#%@"

    def __init__(self, products: List[Product], customers: List[Customer], inventory: Inventory,
                 orders: List[Order], audit: AuditTrail, width: int = REPORT_WIDTH):
        self.products = {p.sku: p for p in products}
        self.customers = {c.customer_id: c for c in customers}
        self.inventory = inventory
        self.orders_by_id = {o.order_id: o for o in orders}
        self.audit = audit
        self.width = width
        self.shop_name = SHOP_NAME
        self.currency = BASE_CURRENCY
        self.sla_days = {"standard": 3, "express": 1, "economy": 7, "pickup": 0}
        self.margin_alert = 0.25
        self.top_n = 8
        self.kpi_history = OrderedDict()  # type: Dict[str, Dict[str, float]]
        self.warnings = []  # type: List[str]
        self.reports_generated = 0
        self.abort_on_errors = False

    def money_col(self, value: float, width: int = 12) -> str:
        return pad_left(format(money(value), ",.2f"), width)

    def title(self, text: str) -> List[str]:
        return ["", text.upper(), "=" * min(self.width, len(text) + 4)]

    def table(self, headers: List[str], rows: List[List[Any]], widths: List[int]) -> List[str]:
        out = []
        out.append(" ".join(pad_right(h, w) if i == 0 else pad_left(h, w)
                            for i, (h, w) in enumerate(zip(headers, widths))))
        out.append(" ".join("-" * w for w in widths))
        for row in rows:
            cells = []
            for i, (cell, w) in enumerate(zip(row, widths)):
                if isinstance(cell, float):
                    cell = format(cell, ",.2f")
                cells.append(pad_right(cell, w) if i == 0 else pad_left(cell, w))
            out.append(" ".join(cells))
        return out

    def bar(self, value: float, max_value: float, width: int = 30) -> str:
        if max_value <= 0:
            return ""
        n = int(round(width * value / max_value))
        return "#" * n

    def sparkline(self, values: List[float]) -> str:
        if not values:
            return ""
        lo, hi = min(values), max(values)
        span = hi - lo or 1.0
        chars = []
        for v in values:
            idx = int((v - lo) / span * (len(self.SPARK_CHARS) - 1))
            chars.append(self.SPARK_CHARS[idx])
        return "".join(chars)

    def delta_str(self, now: float, before: Optional[float]) -> str:
        if before is None:
            return "n/a"
        if before == 0:
            return "new" if now else "="
        change = (now - before) / abs(before)
        return "%+.1f%%" % (change * 100)

    def generate_monthly_report(self, month: str, results: List[OrderResult], shipments: List[Shipment],
                                invoices: List[Invoice], returns: Optional[List[Dict[str, Any]]] = None) -> str:
        """Render the monthly report for `month` ("YYYY-MM").

        Historically each section was a separate SQL query.  When we moved off
        the database in 2017 everything was inlined here.
        """
        if returns is None:
            returns = []
        lines = []  # type: List[str]

        # --- select the month ---------------------------------------------------
        month_results = []
        for r in results:
            if r.order_date is None:
                continue
            if month_key(r.order_date) != month:
                continue
            month_results.append(r)
        month_results.sort(key=lambda r: (r.order_date, r.order_id))
        if not month_results:
            self.warnings.append("no orders in %s" % month)
            return "\n".join(self.title("Monthly report %s" % month) + ["(no data)"])

        # --- abort if the run had hard errors -----------------------------------
        for r in month_results:
            if r.status == "error" and self.abort_on_errors:
                self.audit.log("report %s aborted: %s has errors" % (month, r.order_id))
                return "REPORT ABORTED: order %s in error state" % r.order_id

        # --- header -----------------------------------------------------------------
        year, mon = int(month[:4]), int(month[5:7])
        period_start = date(year, mon, 1)
        period_end = last_of_month(period_start)
        lines.append("=" * self.width)
        lines.append(center(self.shop_name, self.width))
        lines.append(center("Monthly Management Report", self.width))
        lines.append(center("%s to %s" % (period_start.isoformat(), period_end.isoformat()), self.width))
        lines.append("=" * self.width)
        lines.append("Report no. %d, currency %s, %d orders in period"
                     % (self.reports_generated + 1, self.currency, len(month_results)))

        # --- status summary ------------------------------------------------------
        status_counts = Counter()
        status_value = defaultdict(float)
        realised = []  # type: List[OrderResult]
        for r in month_results:
            status_counts[r.status] += 1
            status_value[r.status] += r.total
            if r.status in ("shipped", "partial"):
                realised.append(r)
        lines.extend(self.title("1. Order status"))
        status_rows = []
        for status in sorted(status_counts):
            share = safe_div(status_counts[status], len(month_results))
            status_rows.append([status, status_counts[status], fmt_pct(share), money(status_value[status])])
        lines.extend(self.table(["Status", "Orders", "Share", "Value"], status_rows, [20, 8, 8, 14]))
        fulfilment_rate = safe_div(len(realised), len(month_results))
        lines.append("Fulfilment rate: %s" % fmt_pct(fulfilment_rate))
        if fulfilment_rate < 0.6:
            self.warnings.append("%s: fulfilment rate only %s" % (month, fmt_pct(fulfilment_rate)))

        # --- revenue per day -------------------------------------------------------
        daily_revenue = OrderedDict()  # type: Dict[date, float]
        cursor = period_start
        while cursor <= period_end:
            daily_revenue[cursor] = 0.0
            cursor = cursor + timedelta(days=1)
        for r in realised:
            daily_revenue[r.order_date] = daily_revenue.get(r.order_date, 0.0) + (r.total - r.tax)
        revenue_values = list(daily_revenue.values())
        best_day = max(daily_revenue.items(), key=lambda kv: (kv[1], -kv[0].toordinal()))
        worst_weekday = None
        for d, v in daily_revenue.items():
            if d.weekday() >= 5:
                continue
            if worst_weekday is None or v < worst_weekday[1]:
                worst_weekday = (d, v)
        net_revenue = money(sum(revenue_values))
        lines.extend(self.title("2. Revenue"))
        lines.append("Net revenue (excl. VAT): %s %s" % (self.money_col(net_revenue).strip(), self.currency))
        lines.append("Daily trend: [%s]" % self.sparkline(revenue_values))
        lines.append("Best day:  %s  %s" % (best_day[0].isoformat(), fmt_money(best_day[1], self.currency)))
        if worst_weekday is not None:
            lines.append("Weakest weekday: %s  %s" % (worst_weekday[0].isoformat(),
                                                       fmt_money(worst_weekday[1], self.currency)))

        # --- weekly aggregation ----------------------------------------------------
        weekly = OrderedDict()  # type: Dict[str, List[float]]
        for d, v in daily_revenue.items():
            iso_year, iso_week, _ = d.isocalendar()
            key = "W%02d" % iso_week
            if key not in weekly:
                weekly[key] = [0.0, 0]
            weekly[key][0] += v
            weekly[key][1] += 1
        max_week = max([w[0] for w in weekly.values()] or [0.0])
        for key, (value, days) in weekly.items():
            lines.append("  %s %3d days %12s  %s" % (key, days, format(money(value), ",.2f"),
                                                    self.bar(value, max_week, 24)))

        # --- by country ------------------------------------------------------------
        country_stats = defaultdict(lambda: [0, 0.0, 0.0])
        for r in realised:
            country = r.country or self.customers[r.customer_id].country
            stats = country_stats[country]
            stats[0] += 1
            stats[1] += r.total - r.tax
            stats[2] += r.shipping
        lines.extend(self.title("3. Revenue by country"))
        country_rows = []
        for country in sorted(country_stats, key=lambda c: (-country_stats[c][1], c)):
            n, rev, ship = country_stats[country]
            zone = SHIPPING_ZONES.get(country, "world2")
            country_rows.append([country, zone, n, money(rev), money(ship), fmt_pct(safe_div(ship, rev))])
        lines.extend(self.table(["Country", "Zone", "Orders", "Net rev", "Shipping", "Ship %"],
                                country_rows, [8, 9, 7, 12, 10, 8]))

        # --- by category -----------------------------------------------------------
        category_revenue = defaultdict(float)
        category_units = Counter()
        category_cost = defaultdict(float)
        product_units = Counter()
        product_revenue = defaultdict(float)
        for r in realised:
            order = self.orders_by_id.get(r.order_id)
            if order is None:
                continue
            gross_lines = 0.0
            for line in order.lines:
                gross_lines += self.products[line.sku].list_price * line.qty
            for line in order.lines:
                product = self.products[line.sku]
                backordered = r.backordered.get(line.sku, 0)
                shipped_qty = line.qty - backordered
                if shipped_qty <= 0:
                    continue
                share = safe_div(product.list_price * line.qty, gross_lines)
                line_net = (r.subtotal - r.discount) * share * safe_div(shipped_qty, line.qty)
                category_revenue[product.category] += line_net
                category_units[product.category] += shipped_qty
                category_cost[product.category] += product.unit_cost * shipped_qty
                product_units[line.sku] += shipped_qty
                product_revenue[line.sku] += line_net
        lines.extend(self.title("4. Categories"))
        cat_rows = []
        max_cat = max(list(category_revenue.values()) or [0.0])
        for cat in sorted(category_revenue, key=lambda c: (-category_revenue[c], c)):
            margin = safe_div(category_revenue[cat] - category_cost[cat], category_revenue[cat])
            cat_rows.append([cat, category_units[cat], money(category_revenue[cat]), fmt_pct(margin),
                             self.bar(category_revenue[cat], max_cat, 14)])
        lines.extend(self.table(["Category", "Units", "Net rev", "Margin", ""], cat_rows, [12, 6, 12, 8, 14]))

        # --- top products ------------------------------------------------------------
        lines.extend(self.title("5. Top products"))
        ranked = sorted(product_revenue, key=lambda s: (-product_revenue[s], s))
        product_rows = []
        for rank, sku in enumerate(ranked[:self.top_n], start=1):
            product = self.products[sku]
            stock = self.inventory.total_available(sku)
            product_rows.append(["%d. %s" % (rank, sku), product.name[:22], product_units[sku],
                                 money(product_revenue[sku]), stock])
        lines.extend(self.table(["Rank/SKU", "Name", "Units", "Net rev", "Stock"],
                                product_rows, [12, 22, 6, 12, 6]))
        never_sold = [s for s in sorted(self.products) if self.products[s].active and s not in product_units]
        if never_sold:
            lines.append("Not sold this month (%d): %s" % (len(never_sold), ", ".join(never_sold[:10])
                                                            + (" ..." if len(never_sold) > 10 else "")))

        # --- customers -----------------------------------------------------------------
        revenue_by_customer = defaultdict(float)
        orders_by_customer = Counter()
        for r in realised:
            revenue_by_customer[r.customer_id] += r.total - r.tax
            orders_by_customer[r.customer_id] += 1
        new_customers = []
        returning_customers = []
        for cid in sorted(revenue_by_customer):
            cust = self.customers[cid]
            if first_of_month(cust.joined) == period_start or cust.joined > period_start - timedelta(days=90):
                new_customers.append(cid)
            else:
                returning_customers.append(cid)
        top_customers = sorted(revenue_by_customer, key=lambda c: (-revenue_by_customer[c], c))[:5]
        lines.extend(self.title("6. Customers"))
        lines.append("Active customers: %d (new %d, returning %d)"
                     % (len(revenue_by_customer), len(new_customers), len(returning_customers)))
        avg_basket = safe_div(net_revenue, len(realised))
        lines.append("Average basket (net): %s" % fmt_money(avg_basket, self.currency))
        for cid in top_customers:
            cust = self.customers[cid]
            lines.append("  %-6s %-26s %-9s %2d orders %12s" % (cid, cust.name[:26], cust.tier,
                                                               orders_by_customer[cid],
                                                               format(money(revenue_by_customer[cid]), ",.2f")))
        repeat_buyers = [c for c in orders_by_customer if orders_by_customer[c] > 1]
        lines.append("Repeat buyers within month: %d" % len(repeat_buyers))

        # --- tier distribution -----------------------------------------------------------
        tier_counts = Counter()
        tier_revenue = defaultdict(float)
        for cid, value in revenue_by_customer.items():
            tier = self.customers[cid].tier
            tier_counts[tier] += 1
            tier_revenue[tier] += value
        lines.append("Tier mix:")
        for tier in TIER_ORDER:
            if tier_counts[tier] == 0:
                lines.append("  %-9s -" % tier)
                continue
            lines.append("  %-9s %3d customers  %12s  avg %10s"
                         % (tier, tier_counts[tier], format(money(tier_revenue[tier]), ",.2f"),
                            format(money(safe_div(tier_revenue[tier], tier_counts[tier])), ",.2f")))

        # --- shipping performance ---------------------------------------------------------
        month_order_ids = set(r.order_id for r in month_results)
        carrier_stats = defaultdict(lambda: {"shipments": 0, "parcels": 0, "weight": 0.0, "cost": 0.0, "late": 0})
        late_shipments = []
        for sh in shipments:
            if sh.order_id not in month_order_ids:
                continue
            cs = carrier_stats[sh.carrier]
            cs["shipments"] += 1
            cs["parcels"] += sh.parcels
            cs["weight"] += sh.weight_kg
            cs["cost"] += sh.cost
            transit = business_days_between(sh.ship_date, sh.eta)
            allowed = self.sla_days.get(sh.method, 3)
            order = self.orders_by_id.get(sh.order_id)
            if order is not None and SHIPPING_ZONES.get(order.ship_country, "world2") != "domestic":
                allowed += 3
            if transit > allowed:
                cs["late"] += 1
                late_shipments.append((sh.shipment_id, sh.carrier, transit, allowed))
        lines.extend(self.title("7. Shipping"))
        carrier_rows = []
        for carrier in sorted(carrier_stats):
            cs = carrier_stats[carrier]
            carrier_rows.append([carrier, cs["shipments"], cs["parcels"], round(cs["weight"], 1),
                                 money(cs["cost"]), cs["late"]])
        lines.extend(self.table(["Carrier", "Shipm.", "Parcels", "kg", "Charged", "Late"],
                                carrier_rows, [8, 7, 8, 9, 10, 5]))
        if late_shipments:
            lines.append("Expected late deliveries: %d" % len(late_shipments))
            for sid, carrier, transit, allowed in late_shipments[:5]:
                lines.append("  %s via %s: %d days (SLA %d)" % (sid, carrier, transit, allowed))

        # --- margin analysis ----------------------------------------------------------------
        margin_alerts = []
        gross_profit = 0.0
        for r in realised:
            profit = r.profit()
            gross_profit += profit
            net_goods = r.subtotal - r.discount
            margin = safe_div(net_goods - r.cost, net_goods)
            if margin < self.margin_alert:
                margin_alerts.append((r.order_id, margin, r.discount))
        lines.extend(self.title("8. Margin"))
        lines.append("Gross profit after shipping: %s" % fmt_money(gross_profit, self.currency))
        lines.append("Gross margin: %s" % fmt_pct(safe_div(gross_profit, net_revenue)))
        if margin_alerts:
            lines.append("Orders below %s margin: %d" % (fmt_pct(self.margin_alert), len(margin_alerts)))
            for oid, margin, disc in sorted(margin_alerts, key=lambda a: (a[1], a[0]))[:5]:
                lines.append("  %s margin %s discount %s" % (oid, fmt_pct(margin), format(disc, ".2f")))
            if len(margin_alerts) > len(realised) * 0.2:
                self.warnings.append("%s: %d low margin orders" % (month, len(margin_alerts)))

        # --- discounts and coupons -------------------------------------------------------------
        discount_total = 0.0
        points_redeemed = 0
        points_earned = 0
        coupon_mentions = Counter()
        for r in month_results:
            discount_total += r.discount
            points_redeemed += r.points_redeemed
            points_earned += r.points_earned
            for msg in r.messages:
                m = re.match(r"^coupon (\w+)", msg)
                if m:
                    coupon_mentions[m.group(1)] += 1
                elif msg.startswith("free shipping via "):
                    coupon_mentions[msg.split()[-1]] += 1
        lines.extend(self.title("9. Discounts & loyalty"))
        lines.append("Discounts granted: %s" % fmt_money(discount_total, self.currency))
        lines.append("Points earned %d, redeemed %d (value %s)"
                     % (points_earned, points_redeemed, fmt_money(points_redeemed * LOYALTY_REDEEM_VALUE)))
        for code, n in sorted(coupon_mentions.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append("  coupon %-12s used %d x" % (code, n))

        # --- backorders --------------------------------------------------------------------------
        open_backorders = defaultdict(int)
        waiting_orders = []
        for r in month_results:
            if not r.backordered:
                continue
            for sku, qty in r.backordered.items():
                open_backorders[sku] += qty
            if r.status in ("backorder", "partial"):
                waiting_orders.append(r.order_id)
        lines.extend(self.title("10. Backorders"))
        if not open_backorders:
            lines.append("No backorders.")
        else:
            for sku in sorted(open_backorders, key=lambda s: (-open_backorders[s], s)):
                product = self.products[sku]
                supplier = SUPPLIERS.get(product.supplier, {"lead_days": 14})
                eta = add_business_days(period_end, supplier["lead_days"])
                lines.append("  %-8s %-28s %4d pcs  supplier %-10s ETA %s"
                             % (sku, product.name[:28], open_backorders[sku], product.supplier, eta.isoformat()))
            lines.append("Orders waiting: %d" % len(waiting_orders))

        # --- returns ------------------------------------------------------------------------------
        returned_value = 0.0
        return_reasons = Counter()
        for ret in returns:
            if ret.get("month") != month:
                continue
            returned_value += ret.get("value", 0.0)
            return_reasons[ret.get("reason", "unknown")] += 1
        return_rate = safe_div(returned_value, net_revenue)
        lines.extend(self.title("11. Returns"))
        lines.append("Returned value: %s (%s of revenue)" % (fmt_money(returned_value), fmt_pct(return_rate)))
        for reason, n in sorted(return_reasons.items()):
            lines.append("  %-20s %d" % (reason, n))
        if return_rate > 0.08:
            self.warnings.append("%s: return rate %s" % (month, fmt_pct(return_rate)))

        # --- receivables ---------------------------------------------------------------------------
        issued = 0.0
        credited = 0.0
        open_items = 0.0
        overdue_items = []
        for inv in invoices:
            if month_key(inv.issue_date) != month and inv.issue_date <= period_end:
                # older invoices still count for open items
                if not inv.is_credit_note and inv.open_amount() > 0:
                    open_items += inv.open_amount()
                continue
            if inv.issue_date > period_end:
                continue
            if inv.is_credit_note:
                credited += -inv.gross_total
            else:
                issued += inv.gross_total
                open_items += inv.open_amount()
                if inv.due_date < period_end and inv.open_amount() > 0:
                    overdue_items.append(inv.number)
        lines.extend(self.title("12. Receivables"))
        lines.append("Invoiced %s, credited %s, open %s"
                     % (fmt_money(issued), fmt_money(credited), fmt_money(open_items)))
        if overdue_items:
            lines.append("Overdue: %s" % ", ".join(sorted(overdue_items)[:6]))

        # --- inventory ------------------------------------------------------------------------------
        with self.audit.section("report-inventory"):
            valuation = self.inventory.valuation()
            low = self.inventory.low_stock()
            stock_value = money(sum(valuation.values()))
            cogs_month = sum(category_cost.values())
            turnover = safe_div(cogs_month * 12, stock_value)
            lines.extend(self.title("13. Inventory"))
            lines.append("Stock value at cost: %s" % fmt_money(stock_value))
            lines.append("Annualised turnover: %.2f" % turnover)
            for cat, value in valuation.items():
                lines.append("  %-12s %12s" % (cat, format(value, ",.2f")))
            if low:
                lines.append("Below reorder point: %d SKUs" % len(low))
                for sku, have, point in low[:8]:
                    lines.append("  %-8s have %3d / reorder at %3d" % (sku, have, point))
            self.audit.log("inventory section: %d low-stock SKUs" % len(low))

        # --- order value distribution ------------------------------------------------------------------
        buckets = [25, 50, 100, 200, 400, 800, 1600]
        histogram = [0] * (len(buckets) + 1)
        for r in realised:
            value = r.total
            idx = 0
            while idx < len(buckets) and value >= buckets[idx]:
                idx += 1
            histogram[idx] += 1
        lines.extend(self.title("14. Order value distribution"))
        max_h = max(histogram) or 1
        for i, count in enumerate(histogram):
            if i == 0:
                label = "< %d" % buckets[0]
            elif i == len(buckets):
                label = ">= %d" % buckets[-1]
            else:
                label = "%d-%d" % (buckets[i - 1], buckets[i] - 1)
            lines.append("  %-10s %4d %s" % (label, count, self.bar(count, max_h, 40)))

        # --- KPIs and comparison --------------------------------------------------------------------------
        kpis = OrderedDict()
        kpis["orders"] = float(len(month_results))
        kpis["net_revenue"] = net_revenue
        kpis["avg_basket"] = money(avg_basket)
        kpis["gross_profit"] = money(gross_profit)
        kpis["fulfilment_rate"] = round(fulfilment_rate, 4)
        kpis["discounts"] = money(discount_total)
        kpis["return_rate"] = round(return_rate, 4)
        previous = None
        for key in reversed(list(self.kpi_history.keys())):
            if key < month:
                previous = self.kpi_history[key]
                break
        lines.extend(self.title("15. KPIs"))
        for name, value in kpis.items():
            before = previous.get(name) if previous else None
            lines.append("  %-16s %14s   vs prev %s" % (name, format(value, ",.4g") if value < 10 else
                                                       format(value, ",.2f"), self.delta_str(value, before)))
        try:
            growth = (kpis["net_revenue"] - previous["net_revenue"]) / previous["net_revenue"]
            projected = kpis["net_revenue"] * (1 + growth)
            lines.append("Naive projection next month: %s" % fmt_money(projected))
        except (TypeError, ZeroDivisionError):
            lines.append("Naive projection next month: n/a")

        # --- warnings ------------------------------------------------------------------------------------
        month_warnings = [w for w in self.warnings if w.startswith(month)]
        lines.extend(self.title("16. Warnings"))
        if not month_warnings:
            lines.append("None.")
        for w in month_warnings:
            lines.extend(wrap_text("* " + w, self.width, ""))

        # --- footer ---------------------------------------------------------------------------------------
        self.kpi_history[month] = kpis
        self.reports_generated += 1
        lines.append("")
        lines.append("-" * self.width)
        lines.append(center("end of report %s (%d lines)" % (month, len(lines) + 1), self.width))
        self.audit.log("report %s generated, %d lines" % (month, len(lines)))
        return "\n".join(lines)

    def weekly_flash(self, results: List[OrderResult], week_start: date) -> str:
        """Short Monday-morning flash for the sales team (fits in a chat message)."""
        week_end = week_start + timedelta(days=6)
        prev_start = week_start - timedelta(days=7)
        this_week = []
        last_week = []
        for r in results:
            if r.order_date is None:
                continue
            if week_start <= r.order_date <= week_end:
                this_week.append(r)
            elif prev_start <= r.order_date < week_start:
                last_week.append(r)

        def net(rs: List[OrderResult]) -> float:
            return money(sum(r.total - r.tax for r in rs if r.status in ("shipped", "partial", "awaiting-payment")))

        now_net, prev_net = net(this_week), net(last_week)
        out = ["FLASH %s..%s" % (week_start.isoformat(), week_end.isoformat())]
        out.append("orders %d (prev %d), net %s (%s)" % (len(this_week), len(last_week), fmt_money(now_net),
                                                        self.delta_str(now_net, prev_net)))
        problems = Counter(r.status for r in this_week if r.status not in ("shipped", "partial"))
        if problems:
            out.append("attention: " + ", ".join("%s %d" % kv for kv in sorted(problems.items())))
        best = None
        for r in this_week:
            if best is None or r.total > best.total:
                best = r
        if best is not None and best.total > 0:
            cust = self.customers.get(best.customer_id)
            out.append("largest: %s %s (%s)" % (best.order_id, fmt_money(best.total),
                                                cust.name if cust else best.customer_id))
        countries = Counter(r.country for r in this_week if r.country)
        if countries:
            top = sorted(countries.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            out.append("top countries: " + ", ".join("%s %d" % kv for kv in top))
        low = self.inventory.low_stock()
        if low:
            out.append("low stock: " + ", ".join(sku for sku, _, _ in low[:6]))
        return "\n".join(out)

    def customer_statement(self, customer_id: str, results: List[OrderResult], invoices: List[Invoice]) -> str:
        cust = self.customers[customer_id]
        lines = ["Statement for %s" % cust.display()]
        balance = 0.0
        for inv in invoices:
            if inv.customer_id != customer_id:
                continue
            balance += inv.gross_total - inv.paid
            lines.append("  %s %s %10.2f paid %10.2f" % (inv.issue_date.isoformat(), inv.number,
                                                         inv.gross_total, inv.paid))
        count = sum(1 for r in results if r.customer_id == customer_id)
        lines.append("  orders: %d, open balance: %s" % (count, fmt_money(balance)))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Catalog import / export
# ---------------------------------------------------------------------------

SKU_PATTERN = re.compile(r"^[A-Z]{3}-\d{3}$")


class CatalogImporter:
    """Imports supplier price lists.

    Suppliers send all kinds of CSV dialects.  Column names vary by supplier
    and by the mood of whoever exported the file, hence the alias table.
    """

    def __init__(self, inventory: Inventory, pricing: PricingEngine, audit: AuditTrail):
        self.inventory = inventory
        self.pricing = pricing
        self.audit = audit
        self.errors = []  # type: List[str]
        self.warnings = []  # type: List[str]
        self.dry_run = False
        self.deactivate_missing = False
        self.max_price_change = 0.35
        self.supplier_whitelist = sorted(SUPPLIERS.keys())
        self.required_columns = ["sku", "name", "category", "cost", "price"]
        self.column_aliases = {
            "sku": "sku", "artikelnummer": "sku", "item": "sku", "item no": "sku", "article": "sku",
            "name": "name", "bezeichnung": "name", "description": "name", "title": "name",
            "category": "category", "kategorie": "category", "group": "category",
            "cost": "cost", "ek": "cost", "purchase price": "cost", "einkaufspreis": "cost",
            "price": "price", "vk": "price", "retail": "price", "verkaufspreis": "price", "rrp": "price",
            "weight": "weight", "gewicht": "weight", "kg": "weight",
            "dims": "dims", "dimensions": "dims", "masse": "dims",
            "supplier": "supplier", "lieferant": "supplier", "vendor": "supplier",
            "tags": "tags", "keywords": "tags",
            "stock": "stock", "bestand": "stock", "qty": "stock",
            "active": "active", "aktiv": "active",
        }
        self.category_map = {
            "zelte": "tents", "tent": "tents", "tents": "tents",
            "schlafen": "sleeping", "sleeping": "sleeping", "sleep": "sleeping",
            "rucksack": "packs", "rucksaecke": "packs", "packs": "packs", "backpacks": "packs",
            "kochen": "cookware", "cookware": "cookware", "kitchen": "cookware",
            "bekleidung": "clothing", "clothing": "clothing", "apparel": "clothing",
            "schuhe": "footwear", "footwear": "footwear", "shoes": "footwear",
            "navigation": "navigation", "karten": "maps", "maps": "maps",
            "buecher": "books", "books": "books", "food": "food", "nahrung": "food",
            "licht": "lighting", "lighting": "lighting", "safety": "safety", "sicherheit": "safety",
            "moebel": "furniture", "furniture": "furniture",
        }
        self.stats = Counter()
        self.imported_batches = []  # type: List[Dict[str, Any]]

    def import_catalog(self, csv_text: str, effective_date: date, source: str = "upload") -> Dict[str, Any]:
        """Parse a supplier CSV and merge it into the catalog.

        Returns a summary dict.  Errors and warnings are collected on self.
        """
        summary = OrderedDict()  # type: Dict[str, Any]
        summary["source"] = source
        summary["date"] = effective_date.isoformat()
        batch_errors_before = len(self.errors)

        # --- empty file ---------------------------------------------------------
        if not csv_text or not csv_text.strip():
            self.errors.append("%s: empty file" % source)
            summary["status"] = "empty"
            return summary

        # --- detect delimiter ----------------------------------------------------
        first_line = ""
        for raw_line in csv_text.splitlines():
            if raw_line.strip() and not raw_line.lstrip().startswith("#"):
                first_line = raw_line
                break
        delimiter = ","
        best_count = 0
        for candidate in (";", ",", "\t", "|"):
            count = first_line.count(candidate)
            if count > best_count:
                best_count = count
                delimiter = candidate
        summary["delimiter"] = {"\t": "TAB"}.get(delimiter, delimiter)
        self.audit.log("import %s: delimiter %r" % (source, summary["delimiter"]))

        # --- header -----------------------------------------------------------------
        cleaned_lines = [l for l in csv_text.splitlines() if not l.lstrip().startswith("#")]
        reader = csv.reader(io.StringIO("\n".join(cleaned_lines)), delimiter=delimiter)
        header_raw = []
        for row in reader:
            if any(cell.strip() for cell in row):
                header_raw = row
                break
        columns = {}  # type: Dict[str, int]
        unknown_columns = []
        for idx, col in enumerate(header_raw):
            key = re.sub(r"\s+", " ", col.strip().lower().replace("_", " "))
            key = key.replace("﻿", "")
            canonical = self.column_aliases.get(key)
            if canonical is None:
                unknown_columns.append(col.strip())
                continue
            if canonical in columns:
                self.warnings.append("%s: column %s appears twice, using first" % (source, canonical))
                continue
            columns[canonical] = idx
        if unknown_columns:
            self.warnings.append("%s: ignored columns %s" % (source, ", ".join(unknown_columns)))

        # --- required columns -------------------------------------------------------
        missing_columns = []
        for col in self.required_columns:
            if col not in columns:
                missing_columns.append(col)
        if missing_columns:
            self.errors.append("%s: missing required columns %s" % (source, ", ".join(missing_columns)))
            summary["status"] = "bad-header"
            summary["missing"] = missing_columns
            return summary

        # --- read rows ----------------------------------------------------------------
        raw_records = []  # type: List[Tuple[int, Dict[str, str]]]
        short_rows = 0
        for line_no, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue
            if len(row) < len(header_raw):
                short_rows += 1
                row = row + [""] * (len(header_raw) - len(row))
            record = {}
            for name, idx in columns.items():
                record[name] = row[idx].strip() if idx < len(row) else ""
            raw_records.append((line_no, record))
        if short_rows:
            self.warnings.append("%s: %d short rows padded" % (source, short_rows))
        summary["rows"] = len(raw_records)

        # --- normalise fields --------------------------------------------------------------
        records = []  # type: List[Dict[str, Any]]
        row_errors = []  # type: List[str]
        for line_no, rec in raw_records:
            norm = {"line": line_no}
            norm["sku"] = rec.get("sku", "").upper().replace(" ", "").replace("_", "-")
            norm["name"] = re.sub(r"\s+", " ", rec.get("name", "")).strip()
            cat_raw = rec.get("category", "").strip().lower()
            cat_raw = cat_raw.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
            norm["category"] = self.category_map.get(cat_raw, "")
            if not norm["category"]:
                row_errors.append("line %d: unknown category %r" % (line_no, rec.get("category", "")))
                continue
            norm["cost"] = parse_float(rec.get("cost"), -1.0)
            norm["price"] = parse_float(rec.get("price"), -1.0)
            norm["weight"] = parse_float(rec.get("weight"), 0.0)
            norm["dims_raw"] = rec.get("dims", "")
            norm["supplier"] = rec.get("supplier", "").strip() or "ACME"
            norm["tags_raw"] = rec.get("tags", "")
            norm["stock"] = parse_int(rec.get("stock"), 0) if "stock" in columns else None
            norm["active"] = parse_bool(rec.get("active", "1")) if "active" in columns else True
            records.append(norm)

        # --- validate ------------------------------------------------------------------------
        valid_records = []
        for rec in records:
            problems = []
            if not SKU_PATTERN.match(rec["sku"]):
                problems.append("bad sku %r" % rec["sku"])
            if len(rec["name"]) < 3:
                problems.append("name too short")
            if rec["cost"] < 0:
                problems.append("missing cost")
            if rec["price"] <= 0:
                problems.append("missing price")
            elif rec["cost"] >= 0 and rec["price"] < rec["cost"]:
                problems.append("price %.2f below cost %.2f" % (rec["price"], rec["cost"]))
            if rec["weight"] < 0 or rec["weight"] > 70:
                problems.append("implausible weight %.2f" % rec["weight"])
            if rec["supplier"] not in self.supplier_whitelist:
                problems.append("supplier %s not approved" % rec["supplier"])
            if problems:
                row_errors.append("line %d (%s): %s" % (rec["line"], rec["sku"], "; ".join(problems)))
            else:
                valid_records.append(rec)
        for err in row_errors:
            self.errors.append("%s: %s" % (source, err))
        summary["invalid"] = len(row_errors)

        # --- duplicates inside the file: last one wins ------------------------------------------
        by_sku = OrderedDict()  # type: Dict[str, Dict[str, Any]]
        dup_skus = []
        for rec in valid_records:
            if rec["sku"] in by_sku:
                dup_skus.append(rec["sku"])
                del by_sku[rec["sku"]]
            by_sku[rec["sku"]] = rec
        if dup_skus:
            self.warnings.append("%s: duplicate SKUs (last wins): %s" % (source, ", ".join(sorted(set(dup_skus)))))

        # --- dimensions --------------------------------------------------------------------------------
        for sku, rec in by_sku.items():
            raw = rec["dims_raw"].lower().replace(" ", "").replace("cm", "")
            parts = re.split(r"[x*/]", raw) if raw else []
            dims = []
            for part in parts:
                value = parse_float(part, -1.0)
                if value > 0:
                    dims.append(value)
            if len(dims) == 3:
                rec["dims"] = (dims[0], dims[1], dims[2])
            else:
                existing = self.inventory.products.get(sku)
                rec["dims"] = existing.dims_cm if existing else (30.0, 20.0, 10.0)
                if raw:
                    self.warnings.append("%s: %s has unreadable dims %r" % (source, sku, rec["dims_raw"]))

        # --- tags ------------------------------------------------------------------------------------------
        tag_counter = Counter()
        for sku, rec in by_sku.items():
            tags = []
            for t in re.split(r"[,|/ ]+", rec["tags_raw"]):
                t = slugify(t)
                if t and t not in tags:
                    tags.append(t)
            if rec["category"] == "cookware" and "gas" in rec["name"].lower() and "hazmat" not in tags:
                tags.append("hazmat")
            if max(rec["dims"]) > 120 and "bulky" not in tags:
                tags.append("bulky")
            rec["tags"] = tags
            tag_counter.update(tags)

        # --- compare with current catalog --------------------------------------------------------------------
        new_skus = []
        changed = OrderedDict()  # type: Dict[str, List[str]]
        unchanged = []
        suspicious = []  # type: List[str]
        for sku, rec in by_sku.items():
            existing = self.inventory.products.get(sku)
            if existing is None:
                new_skus.append(sku)
                continue
            diffs = []
            if existing.name != rec["name"]:
                diffs.append("name")
            if existing.category != rec["category"]:
                diffs.append("category")
            if abs(existing.unit_cost - rec["cost"]) > 0.004:
                diffs.append("cost")
            current_price = self.pricing.list_price(sku)
            if abs(current_price - rec["price"]) > 0.004:
                diffs.append("price")
                change = safe_div(rec["price"] - current_price, current_price)
                if abs(change) > self.max_price_change:
                    suspicious.append(sku)
            if abs(existing.weight_kg - rec["weight"]) > 0.001 and rec["weight"] > 0:
                diffs.append("weight")
            if existing.supplier != rec["supplier"]:
                diffs.append("supplier")
            if diffs:
                changed[sku] = diffs
            else:
                unchanged.append(sku)

        # --- try to repair suspicious prices (classic decimal-shift typo) ------------------------------------
        repair_queue = list(suspicious)
        repaired = []
        rejected_prices = []
        while repair_queue:
            sku = repair_queue.pop(0)
            rec = by_sku[sku]
            current = self.pricing.list_price(sku)
            candidates = [rec["price"] / 100.0, rec["price"] / 10.0, rec["price"] * 10.0, rec["price"] * 100.0]
            fixed = None
            for cand in candidates:
                if abs(safe_div(cand - current, current)) <= self.max_price_change and cand > rec["cost"]:
                    fixed = money(cand)
                    break
            if fixed is not None:
                self.warnings.append("%s: %s price %.2f looks shifted, using %.2f"
                                     % (source, sku, rec["price"], fixed))
                rec["price"] = fixed
                repaired.append(sku)
            else:
                rejected_prices.append(sku)
                self.errors.append("%s: %s price change %.2f -> %.2f exceeds %s, kept old price"
                                   % (source, sku, current, rec["price"], fmt_pct(self.max_price_change)))
                rec["price"] = current
                if "price" in changed.get(sku, []):
                    changed[sku].remove("price")

        # --- apply ---------------------------------------------------------------------------------------------
        applied = 0
        stock_received = 0
        if self.dry_run:
            self.audit.log("dry run: would create %d and update %d products" % (len(new_skus), len(changed)))
        else:
            for sku, rec in by_sku.items():
                existing = self.inventory.products.get(sku)
                if existing is None:
                    product = Product(sku=sku, name=rec["name"], category=rec["category"], unit_cost=rec["cost"],
                                      list_price=rec["price"], weight_kg=rec["weight"] or 0.5,
                                      dims_cm=rec["dims"], supplier=rec["supplier"], active=rec["active"],
                                      tags=rec["tags"])
                    self.inventory.products[sku] = product
                    self.pricing.products[sku] = product
                    applied += 1
                elif sku in changed:
                    existing.name = rec["name"]
                    existing.category = rec["category"]
                    existing.unit_cost = rec["cost"]
                    if rec["weight"] > 0:
                        existing.weight_kg = rec["weight"]
                    existing.dims_cm = rec["dims"]
                    existing.supplier = rec["supplier"]
                    existing.active = rec["active"]
                    for t in rec["tags"]:
                        if t not in existing.tags:
                            existing.tags.append(t)
                    if "price" in changed[sku]:
                        self.pricing.set_price(sku, rec["price"], effective_date)
                    applied += 1
                if rec["stock"]:
                    self.inventory.receive(sku, "HAM", rec["stock"], "import", source)
                    stock_received += rec["stock"]

        # --- deactivate products missing from a full list -------------------------------------------------------
        deactivated = []
        if self.deactivate_missing and not self.dry_run:
            suppliers_in_file = set(rec["supplier"] for rec in by_sku.values())
            for sku in sorted(self.inventory.products):
                product = self.inventory.products[sku]
                if product.supplier not in suppliers_in_file:
                    continue
                if sku in by_sku or not product.active:
                    continue
                product.active = False
                deactivated.append(sku)

        # --- summary -------------------------------------------------------------------------------------------------
        with self.audit.section("import-summary"):
            summary["new"] = new_skus
            summary["changed"] = dict((k, v) for k, v in changed.items() if v)
            summary["unchanged"] = len(unchanged)
            summary["repaired_prices"] = repaired
            summary["rejected_prices"] = rejected_prices
            summary["deactivated"] = deactivated
            summary["applied"] = applied
            summary["stock_received"] = stock_received
            summary["top_tags"] = [t for t, _ in sorted(tag_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
            summary["errors"] = len(self.errors) - batch_errors_before
            summary["status"] = "ok" if summary["errors"] == 0 else "ok-with-errors"
            for key in ("new", "applied", "unchanged", "errors"):
                value = summary[key]
                self.audit.log("%s: %s" % (key, len(value) if isinstance(value, list) else value))
            self.stats["batches"] += 1
            self.stats["rows"] += len(raw_records)
            self.stats["applied"] += applied
            self.imported_batches.append({"source": source, "date": effective_date, "applied": applied})
        return summary

    def export_catalog(self, include_inactive: bool = False) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", lineterminator="\n")
        writer.writerow(["sku", "name", "category", "cost", "price", "weight", "dims", "supplier", "tags", "stock"])
        for sku in sorted(self.inventory.products):
            p = self.inventory.products[sku]
            if not p.active and not include_inactive:
                continue
            writer.writerow([
                p.sku, p.name, p.category,
                ("%.2f" % p.unit_cost).replace(".", ","),
                ("%.2f" % self.pricing.list_price(sku)).replace(".", ","),
                ("%.3f" % p.weight_kg).replace(".", ","),
                "x".join("%g" % d for d in p.dims_cm),
                p.supplier, "|".join(p.tags), self.inventory.total_on_hand(sku),
            ])
        return buf.getvalue()


def sample_supplier_csv() -> str:
    """A realistically messy supplier file (Bergwerk, spring 2024)."""
    return "\n".join([
        "# Bergwerk Preisliste Fruehjahr 2024",
        "# generated by WaWi 3.2",
        "Artikelnummer;Bezeichnung;Kategorie;EK;VK;Gewicht;Masse;Lieferant;Keywords;Bestand;Farbe",
        "SLP-100;Down Sleeping Bag  -5C;Schlafen;97,50;235,00;1,10;40x25x25;Bergwerk;down,winter;6;red",
        "BCK-200;Trekking Pack 55L;Rucksack;72,00;179,00;1,90;75x35x28;Bergwerk;multi-day;10;blue",
        "CLT-200;Hardshell Jacket;Bekleidung;120,00;29900;0,50;35x30x6;Bergwerk;goretex;4;black",
        "FTW-200;Leather Hiking Boot;Schuhe;85,00;219,00;1,60;35 x 25 x 15 cm;Bergwerk;boot;;brown",
        "FTW-100;Approach Shoe;Schuhe;55,00;139,00;0,90;33x22x13;Bergwerk;approach;8;grey",
        "SAF-200;Avalanche Beacon;Sicherheit;190,00;899,00;0,25;12x8x3;Bergwerk;winter|electronics;2;",
        "SAF-210;Snow Shovel;Sicherheit;25,00;59,00;0,60;70x25x8;Bergwerk;winter;5;orange",
        "SAF-220;Avalanche Probe 240;Sicherheit;18,00;49,00;0,30;45x5x5;Bergwerk;winter;12;",
        "SAF-230;Bivy Bag;Sicherheit;22,00;54,90;0,25;15x10x5;Bergwerk;emergency;15;",
        "BOK-110;Alpine Routes Guide;Buecher;11,00;29,00;0,60;24x17x3;Bergwerk;paper;0;",
        "BOK-120;Via Ferrata Atlas;Buecher;14,00;34,00;0,80;25x18x4;Bergwerk;paper;10;",
        "CKW-200;Cook Set 4 pcs;Kochen;26,00;24,00;0,80;20x20x14;Bergwerk;set;;",
        "CKW-210;Gas Canister 450g;Kochen;4,90;11,90;0,65;13x13x11;Bergwerk;fuel;20;",
        "XYZ1;Mystery Item;Zelte;1;2;0,1;;Bergwerk;;;",
        "TNT-500;Bivouac Tent;Zelte;95,00;229,00;1,30;45x15x15;ShadyImports;;3;",
        "MAP-120;Dolomites Map Set;Karten;9,50;25,00;0,30;;Bergwerk;paper;20",
        "SLP-100;Down Sleeping Bag -5C;Schlafen;97,50;239,00;1,10;40x25x25;Bergwerk;down;6;red",
        "SLP-300;Liner Silk;Bettwaren;9,00;29,00;0,12;10x10x8;Bergwerk;;10;",
        "",
    ])


# ---------------------------------------------------------------------------
# Order CSV exchange with the web shop
# ---------------------------------------------------------------------------

ORDER_EXPORT_COLUMNS = ["order_id", "date", "customer", "country", "status", "lines", "units",
                        "subtotal", "discount", "shipping", "tax", "total", "invoice", "messages"]


def export_orders_csv(orders: List[Order], results: List[OrderResult]) -> str:
    """Export processed orders for the accounting system (semicolon CSV, German decimals)."""
    result_by_id = {r.order_id: r for r in results}
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(ORDER_EXPORT_COLUMNS)

    def de(value: float) -> str:
        return ("%.2f" % value).replace(".", ",")

    exported = 0
    skipped = 0
    for order in orders:
        result = result_by_id.get(order.order_id)
        if result is None:
            skipped += 1
            continue
        if result.status == "rejected" and "duplicate" in " ".join(result.messages):
            # accounting does not want to see double clicks
            skipped += 1
            continue
        messages = " | ".join(m.replace(";", ",") for m in result.messages[:3])
        if len(result.messages) > 3:
            messages += " | +%d more" % (len(result.messages) - 3)
        writer.writerow([
            order.order_id,
            order.order_date.strftime("%d.%m.%Y"),
            order.customer_id,
            result.country or order.ship_country,
            result.status,
            len(order.lines),
            order.total_qty(),
            de(result.subtotal),
            de(result.discount),
            de(result.shipping),
            de(result.tax),
            de(result.total),
            result.invoice_no,
            messages,
        ])
        exported += 1
    writer.writerow(["#total", exported, "", "", "skipped", skipped, "", "", "", "", "", "", "", ""])
    return buf.getvalue()


def import_orders_csv(text: str, customers: List[Customer], first_number: int = 30000) -> Tuple[List[Order], List[str]]:
    """Read the web shop's flat order export (one row per order line).

    Columns: ext_ref, date, email, sku, qty, ship_method, coupon, payment
    Orders are grouped by ext_ref.  Customers are matched by e-mail.
    """
    by_email = {}
    for cust in customers:
        by_email[cust.email.lower()] = cust
    reader = csv.DictReader(io.StringIO(text))
    grouped = OrderedDict()  # type: Dict[str, Dict[str, Any]]
    problems = []
    for row_no, row in enumerate(reader, start=2):
        ref = (row.get("ext_ref") or "").strip()
        if not ref:
            problems.append("row %d: missing ext_ref" % row_no)
            continue
        email = (row.get("email") or "").strip().lower()
        cust = by_email.get(email)
        if cust is None:
            problems.append("row %d: unknown customer e-mail %s" % (row_no, email))
            continue
        day = parse_date(row.get("date", ""))
        if day is None:
            problems.append("row %d: bad date %r" % (row_no, row.get("date")))
            continue
        qty = parse_int(row.get("qty"), 0)
        sku = (row.get("sku") or "").strip().upper()
        entry = grouped.get(ref)
        if entry is None:
            entry = {
                "customer": cust,
                "date": day,
                "lines": [],
                "ship_method": (row.get("ship_method") or "standard").strip().lower(),
                "coupon": (row.get("coupon") or "").strip().upper(),
                "payment": (row.get("payment") or "card").strip().lower(),
            }
            grouped[ref] = entry
        elif entry["customer"].customer_id != cust.customer_id:
            problems.append("row %d: ext_ref %s used by two customers" % (row_no, ref))
            continue
        entry["lines"].append(OrderLine(sku=sku, qty=qty))
    orders = []
    number = first_number
    for ref, entry in grouped.items():
        number += 1
        cust = entry["customer"]
        orders.append(Order(
            order_id="SO-%05d" % number,
            customer_id=cust.customer_id,
            order_date=entry["date"],
            lines=entry["lines"],
            ship_method=entry["ship_method"],
            coupon=entry["coupon"],
            payment_method=entry["payment"],
            ship_country=cust.country,
            notes="web:%s" % ref,
        ))
    return orders, problems


def sample_webshop_csv(customers: List[Customer]) -> str:
    good = [c for c in customers if is_valid_email(c.email) and not c.blocked]
    rows = ["ext_ref,date,email,sku,qty,ship_method,coupon,payment"]
    rows.append("W-9001,2024-04-02,%s,TNT-100,1,standard,,card" % good[0].email)
    rows.append("W-9001,2024-04-02,%s,SLP-200,2,standard,,card" % good[0].email)
    rows.append("W-9002,02.04.2024,%s,CKW-110,1,express,SPRING5,paypal" % good[1].email.upper())
    rows.append("W-9002,02.04.2024,%s,CKW-120,4,express,SPRING5,paypal" % good[1].email)
    rows.append("W-9003,2024-04-03,nobody@nowhere.example,BCK-100,1,standard,,card")
    rows.append(",2024-04-03,%s,BCK-100,1,standard,,card" % good[2].email)
    rows.append("W-9004,2024-13-03,%s,MAP-100,2,economy,,prepay" % good[3].email)
    rows.append("W-9005,2024-04-04,%s,FOD-100,24,standard,,invoice" % good[4].email)
    rows.append("W-9005,2024-04-04,%s,FOD-110,24,standard,,invoice" % good[5].email)
    rows.append("W-9006,2024-04-05,%s,FRN-200,2,standard,FREESHIP,card" % good[6].email)
    return "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# Generic text table renderer (used by the CLI and the e-mails)
# ---------------------------------------------------------------------------

def render_text_table(headers: List[str], rows: List[List[Any]], aligns: Optional[str] = None,
                      max_width: int = REPORT_WIDTH, title: str = "") -> str:
    """Render rows as an ASCII table, shrinking the widest column to fit."""
    if aligns is None:
        aligns = "l" + "r" * (len(headers) - 1)
    cells = [[str(h) for h in headers]]
    for row in rows:
        rendered = []
        for value in row:
            if isinstance(value, float):
                rendered.append(format(value, ",.2f"))
            elif value is None:
                rendered.append("-")
            else:
                rendered.append(str(value))
        while len(rendered) < len(headers):
            rendered.append("")
        cells.append(rendered)
    widths = [0] * len(headers)
    for row in cells:
        for i, cell in enumerate(row[:len(headers)]):
            widths[i] = max(widths[i], len(cell))
    total = sum(widths) + 3 * (len(widths) - 1) + 4
    while total > max_width:
        widest = max(range(len(widths)), key=lambda i: (widths[i], -i))
        if widths[widest] <= 4:
            break
        widths[widest] -= 1
        total -= 1
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    out = []
    if title:
        out.append(title)
    out.append(sep)
    for idx, row in enumerate(cells):
        parts = []
        for i, w in enumerate(widths):
            cell = row[i] if i < len(row) else ""
            if len(cell) > w:
                cell = cell[:max(1, w - 1)] + "~"
            if aligns[i] == "r" and idx > 0:
                parts.append(pad_left(cell, w))
            elif aligns[i] == "c":
                parts.append(center(cell, w))
            else:
                parts.append(pad_right(cell, w))
        out.append("| " + " | ".join(parts) + " |")
        if idx == 0:
            out.append(sep.replace("-", "="))
    out.append(sep)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Customer segmentation (marketing asked for "RFM" in 2020)
# ---------------------------------------------------------------------------

def compute_customer_segments(customers: List[Customer], results: List[OrderResult],
                              today: date) -> Dict[str, List[str]]:
    """Classic recency / frequency / monetary scoring, quintiles 1..5."""
    last_order = {}
    frequency = Counter()
    monetary = defaultdict(float)
    for r in results:
        if r.status not in ("shipped", "partial", "awaiting-payment"):
            continue
        if r.order_date is None:
            continue
        if r.customer_id not in last_order or r.order_date > last_order[r.customer_id]:
            last_order[r.customer_id] = r.order_date
        frequency[r.customer_id] += 1
        monetary[r.customer_id] += r.total - r.tax

    def quintile_scores(values: Dict[str, float], reverse: bool = False) -> Dict[str, int]:
        ordered = sorted(values.items(), key=lambda kv: (kv[1], kv[0]), reverse=reverse)
        scores = {}
        n = len(ordered)
        for idx, (cid, _) in enumerate(ordered):
            scores[cid] = 1 + min(4, int(idx * 5 / max(n, 1)))
        return scores

    recency_days = {cid: float((today - d).days) for cid, d in last_order.items()}
    r_scores = quintile_scores(recency_days, reverse=True)  # fewer days = better = higher score
    f_scores = quintile_scores({cid: float(n) for cid, n in frequency.items()})
    m_scores = quintile_scores(dict(monetary))

    segments = OrderedDict([
        ("champions", []), ("loyal", []), ("big-spenders", []), ("promising", []),
        ("at-risk", []), ("hibernating", []), ("never-bought", []),
    ])  # type: Dict[str, List[str]]
    for cust in sorted(customers, key=lambda c: c.customer_id):
        cid = cust.customer_id
        if cid not in last_order:
            segments["never-bought"].append(cid)
            continue
        r, f, m = r_scores[cid], f_scores[cid], m_scores[cid]
        if r >= 4 and f >= 4 and m >= 4:
            segments["champions"].append(cid)
        elif f >= 4 and r >= 3:
            segments["loyal"].append(cid)
        elif m >= 5:
            segments["big-spenders"].append(cid)
        elif r >= 4 and f <= 2:
            segments["promising"].append(cid)
        elif r <= 2 and (f >= 3 or m >= 3):
            segments["at-risk"].append(cid)
        else:
            segments["hibernating"].append(cid)
    return segments


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------

RETURN_REASONS = ["size", "damaged", "not as described", "changed mind", "late delivery"]


def simulate_returns(results: List[OrderResult], orders: List[Order], inventory: Inventory,
                     invoicer: InvoiceBuilder, rng: random.Random, today: date) -> List[Dict[str, Any]]:
    """Generate customer returns for shipped orders and book credit notes.

    Clothing and footwear come back much more often (size issues).
    Damaged items go to the write-off bin instead of back to stock.
    """
    order_by_id = {o.order_id: o for o in orders}
    returns = []
    for r in results:
        if r.status not in ("shipped", "partial") or not r.invoice_no:
            continue
        order = order_by_id[r.order_id]
        invoice = invoicer.invoices.get(r.invoice_no)
        if invoice is None:
            continue
        returned_lines = []
        for line in order.lines:
            product = inventory.product(line.sku)
            base_rate = 0.04
            if product.category in ("clothing", "footwear"):
                base_rate = 0.18
            elif product.category in ("food", "maps", "books"):
                base_rate = 0.0  # hygiene / non-returnable
            if rng.random() >= base_rate:
                continue
            shipped_qty = line.qty - r.backordered.get(line.sku, 0)
            if shipped_qty <= 0:
                continue
            qty = 1 if shipped_qty == 1 else rng.randint(1, shipped_qty)
            reason = rng.choice(RETURN_REASONS)
            if product.category in ("clothing", "footwear") and rng.random() < 0.6:
                reason = "size"
            inv_line = None
            for il in invoice.lines:
                if il.description.startswith(line.sku + " "):
                    inv_line = il
                    break
            if inv_line is None:
                continue
            net = money(inv_line.unit_price * qty)
            returned_lines.append((line.sku, qty, reason, InvoiceLine(inv_line.description, qty,
                                                                     inv_line.unit_price, inv_line.tax_rate,
                                                                     net, money(net * inv_line.tax_rate))))
        if not returned_lines:
            continue
        note = invoicer.credit_note(invoice, [rl[3] for rl in returned_lines], today)
        for sku, qty, reason, _ in returned_lines:
            if reason == "damaged":
                inventory.movements.append(StockMovement(sku, order.warehouse, 0, "write-off", note.number, today))
            else:
                inventory.receive(sku, order.warehouse, qty, "return", note.number)
            returns.append({
                "order_id": r.order_id,
                "sku": sku,
                "qty": qty,
                "reason": reason,
                "credit_note": note.number,
                "value": money(sum(rl[3].net for rl in returned_lines if rl[0] == sku)),
                "month": month_key(r.order_date),
            })
    return returns


# ---------------------------------------------------------------------------
# Purchasing
# ---------------------------------------------------------------------------

def build_purchase_orders(inventory: Inventory, results: List[OrderResult], today: date) -> List[Dict[str, Any]]:
    """Suggest supplier purchase orders.

    Need per SKU = open backorders + max(0, reorder_point - available) +
    reorder_qty if we dropped under the reorder point.  Orders below the
    supplier's minimum order value are topped up with the fastest movers
    of that supplier, or postponed.
    """
    backorder_need = Counter()
    for r in results:
        if r.status in ("backorder", "partial"):
            for sku, qty in r.backordered.items():
                backorder_need[sku] += qty
    velocity = Counter()
    for mv in inventory.movements:
        if mv.reason == "ship":
            velocity[mv.sku] += -mv.delta
    lines_by_supplier = defaultdict(list)
    for sku in sorted(inventory.products):
        product = inventory.products[sku]
        if not product.active:
            continue
        available = inventory.total_available(sku)
        need = backorder_need[sku]
        if available - need <= product.reorder_point:
            need += product.reorder_qty
            need += max(0, product.reorder_point - available)
        if need <= 0:
            continue
        # round up to case sizes: cheap items come in cases of 6
        if product.list_price < 30 and need % 6:
            need += 6 - need % 6
        lines_by_supplier[product.supplier].append([sku, need, product.unit_cost])
    purchase_orders = []
    po_number = 500
    for supplier in sorted(lines_by_supplier):
        terms = SUPPLIERS.get(supplier, {"lead_days": 14, "min_order_value": 0.0, "country": "DE"})
        lines = lines_by_supplier[supplier]
        value = sum(qty * cost for _, qty, cost in lines)
        topped_up = []
        if value < terms["min_order_value"]:
            movers = sorted([l for l in lines], key=lambda l: (-velocity[l[0]], l[0]))
            guard = 0
            while value < terms["min_order_value"] and movers and guard < 50:
                line = movers[guard % len(movers)]
                extra = max(1, int(math.ceil((terms["min_order_value"] - value) / line[2] / len(movers))))
                line[1] += extra
                value += extra * line[2]
                topped_up.append(line[0])
                guard += 1
        po_number += 1
        status = "ready"
        if value < terms["min_order_value"]:
            status = "postponed"
        purchase_orders.append({
            "po": "PO-%04d" % po_number,
            "supplier": supplier,
            "lines": [(sku, qty) for sku, qty, _ in lines],
            "value": money(value),
            "eta": add_business_days(today, terms["lead_days"]).isoformat(),
            "topped_up": sorted(set(topped_up)),
            "status": status,
        })
    return purchase_orders


# ---------------------------------------------------------------------------
# Demand forecasting (single exponential smoothing per SKU and week)
# ---------------------------------------------------------------------------

def forecast_demand(orders: List[Order], products: Dict[str, Product], start: date, weeks: int,
                    alpha: float = 0.4, horizon: int = 4) -> Dict[str, Dict[str, Any]]:
    weekly = defaultdict(lambda: [0] * weeks)
    for order in orders:
        idx = (order.order_date - start).days // 7
        if idx < 0 or idx >= weeks:
            continue
        for line in order.lines:
            if line.sku in products and line.qty > 0:
                weekly[line.sku][idx] += line.qty
    forecasts = OrderedDict()
    for sku in sorted(weekly):
        series = weekly[sku]
        level = float(series[0])
        errors = []
        for actual in series[1:]:
            errors.append(actual - level)
            level = alpha * actual + (1 - alpha) * level
        mad = safe_div(sum(abs(e) for e in errors), len(errors))
        bias = safe_div(sum(errors), len(errors))
        nonzero = sum(1 for v in series if v)
        if nonzero <= weeks // 4:
            pattern = "lumpy"
        elif max(series) > 3 * max(1.0, safe_div(sum(series), weeks)):
            pattern = "spiky"
        else:
            pattern = "steady"
        safety = 1.65 * 1.25 * mad
        forecasts[sku] = {
            "history": series,
            "forecast_per_week": round(level, 2),
            "horizon_total": int(math.ceil(level * horizon)),
            "safety_stock": int(math.ceil(safety)),
            "mad": round(mad, 2),
            "bias": round(bias, 2),
            "pattern": pattern,
        }
    return forecasts


# ---------------------------------------------------------------------------
# Bank statement matching
# ---------------------------------------------------------------------------

def sample_bank_statement(invoicer: InvoiceBuilder, results: List[OrderResult]) -> str:
    """Builds a fake bank statement: some exact payments, some with typos,
    some partial payments, one overpayment and one unrelated booking."""
    rows = ["booking_date;amount;reference;payer"]
    awaiting = [r for r in results if r.status == "awaiting-payment" and r.invoice_no]
    awaiting_numbers = set(r.invoice_no for r in awaiting)
    on_account = [inv for inv in invoicer.invoices.values()
                  if not inv.is_credit_note and inv.open_amount() > 0 and inv.number not in awaiting_numbers]
    for i, r in enumerate(awaiting[:8]):
        inv = invoicer.invoices[r.invoice_no]
        amount = inv.gross_total
        ref = "Rechnung %s" % inv.number
        if i == 1:
            ref = "Order %s thanks" % r.order_id
        elif i == 2:
            amount = money(amount / 2)
        elif i == 3:
            ref = "inv " + inv.number.replace("-", "")
        elif i == 4:
            amount = money(amount + 10)
        rows.append("%s;%s;%s;%s" % ("2024-04-0%d" % (2 + i % 5), ("%.2f" % amount).replace(".", ","),
                                     ref, r.customer_id))
    for inv in on_account[:3]:
        rows.append("2024-04-10;%s;%s;%s" % (("%.2f" % inv.open_amount()).replace(".", ","),
                                            "RG " + inv.number, inv.customer_id))
    rows.append("2024-04-11;-89,90;Telekom Rechnung;TELEKOM")
    rows.append("2024-04-11;250,00;Spende Sektion Nord;UNKNOWN")
    return "\n".join(rows)


def reconcile_payments(text: str, invoicer: InvoiceBuilder, ledger: PaymentLedger,
                       results: List[OrderResult]) -> Dict[str, Any]:
    """Match incoming bank transfers to open invoices.

    Matching strategy (first hit wins):
      1. invoice number in reference (with or without dashes)
      2. order id in reference -> invoice of that order
      3. unique open invoice of the payer with exactly that amount
    """
    invoice_by_compact = {}
    for number, inv in invoicer.invoices.items():
        invoice_by_compact[number.replace("-", "")] = inv
    invoice_by_order = {r.order_id: invoicer.invoices[r.invoice_no] for r in results if r.invoice_no}
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    matched = []
    unmatched = []
    partial = []
    overpaid = []
    ignored = 0
    for row in reader:
        amount = parse_float(row["amount"])
        if amount <= 0:
            ignored += 1
            continue
        reference = row["reference"].upper()
        day = parse_date(row["booking_date"]) or date(2024, 4, 1)
        target = None
        for token in re.findall(r"[A-Z]{3}-?\d{6}-?\d", reference):
            target = invoice_by_compact.get(token.replace("-", ""))
            if target is not None:
                break
        if target is None:
            m = re.search(r"SO-\d{5}", reference)
            if m:
                target = invoice_by_order.get(m.group(0))
        if target is None:
            candidates = [inv for inv in invoicer.invoices.values()
                          if inv.customer_id == row["payer"] and not inv.is_credit_note
                          and abs(inv.open_amount() - amount) < 0.01]
            if len(candidates) == 1:
                target = candidates[0]
        if target is None or target.is_credit_note:
            ledger.record(None, amount, day, "transfer", row["reference"])
            unmatched.append((row["reference"], amount))
            continue
        open_before = target.open_amount()
        ledger.record(target, amount, day, "transfer", row["reference"])
        if amount + 0.005 < open_before:
            partial.append((target.number, amount, money(open_before - amount)))
        elif amount > open_before + 0.005:
            overpaid.append((target.number, money(amount - open_before)))
        matched.append((target.number, amount))
    return {
        "matched": matched,
        "partial": partial,
        "overpaid": overpaid,
        "unmatched": unmatched,
        "ignored": ignored,
    }


# ---------------------------------------------------------------------------
# Warehouse pick lists
# ---------------------------------------------------------------------------

def generate_pick_lists(shipments: List[Shipment], orders: List[Order], inventory: Inventory,
                        wave_size: int = 6) -> List[str]:
    """Group shipments into pick waves per warehouse and sort by bin location.

    Bin location is derived from the SKU prefix (aisle) and number (shelf),
    which is how the HAM warehouse was laid out in 2015.  MUC copied it.
    """
    order_by_id = {o.order_id: o for o in orders}
    by_wh = defaultdict(list)
    for sh in shipments:
        wh = sh.shipment_id.rsplit("-", 1)[-1]
        by_wh[wh].append(sh)
    aisle_order = ["TNT", "SLP", "BCK", "CKW", "CLT", "FTW", "NAV", "MAP", "BOK", "FOD", "LGT", "SAF", "FRN"]
    pages = []
    for wh in sorted(by_wh):
        waves = chunked(sorted(by_wh[wh], key=lambda s: s.shipment_id), wave_size)
        for wave_no, wave in enumerate(waves, start=1):
            picks = defaultdict(int)
            for sh in wave:
                order = order_by_id.get(sh.order_id)
                if order is None:
                    continue
                for line in order.lines:
                    qty = line.qty
                    picks[line.sku] += qty
            def bin_key(sku: str) -> Tuple[int, int]:
                prefix, _, num = sku.partition("-")
                aisle = aisle_order.index(prefix) if prefix in aisle_order else 99
                return aisle, int(num) if num.isdigit() else 0
            out = ["PICK LIST %s wave %d (%d shipments)" % (wh, wave_no, len(wave))]
            total_units = 0
            total_weight = 0.0
            for sku in sorted(picks, key=bin_key):
                aisle, shelf = bin_key(sku)
                product = inventory.products.get(sku)
                name = product.name if product else "?"
                out.append("  A%02d-S%03d  %-8s %-30s x%3d" % (aisle + 1, shelf // 10, sku, name[:30], picks[sku]))
                total_units += picks[sku]
                if product:
                    total_weight += product.weight_kg * picks[sku]
            out.append("  units %d, approx. %.1f kg, shipments: %s"
                       % (total_units, total_weight, ", ".join(s.shipment_id for s in wave)))
            pages.append("\n".join(out))
    return pages


# ---------------------------------------------------------------------------
# Address / customer master data checks
# ---------------------------------------------------------------------------

POSTAL_PATTERNS = {
    "DE": r"^\d{5}$", "AT": r"^\d{4}$", "CH": r"^\d{4,5}$", "NL": r"^\d{4}( ?[A-Z]{2})?$",
    "FR": r"^\d{5}$", "IT": r"^\d{5}$", "SE": r"^\d{3} ?\d{2}$", "PL": r"^\d{2}-\d{3}$",
    "DK": r"^\d{4}$", "NO": r"^\d{4}$", "US": r"^\d{5}(-\d{4})?$", "GB": r"^[A-Z]{1,2}\d[A-Z\d]?( ?\d[A-Z]{2})?$",
}


def validate_customer_master(customers: List[Customer]) -> List[Tuple[str, str]]:
    issues = []
    seen_emails = {}
    for cust in customers:
        cid = cust.customer_id
        if not is_valid_email(cust.email):
            issues.append((cid, "invalid e-mail %r" % cust.email))
        key = cust.email.lower()
        if key in seen_emails:
            issues.append((cid, "e-mail shared with %s" % seen_emails[key]))
        else:
            seen_emails[key] = cid
        pattern = POSTAL_PATTERNS.get(cust.country)
        if pattern is None:
            issues.append((cid, "no postal rule for %s" % cust.country))
        elif not re.match(pattern, cust.postal_code):
            issues.append((cid, "postal code %r does not match %s format" % (cust.postal_code, cust.country)))
        if cust.is_business and cust.country in EU_COUNTRIES and not cust.vat_id:
            issues.append((cid, "EU business without VAT id"))
        if cust.vat_id and not cust.vat_id.startswith(cust.country):
            issues.append((cid, "VAT id country prefix mismatch"))
        if cust.credit_limit and not cust.is_business:
            issues.append((cid, "consumer with credit limit"))
        if cust.points < 0:
            issues.append((cid, "negative points balance"))
    return issues


# ---------------------------------------------------------------------------
# Dunning
# ---------------------------------------------------------------------------

DUNNING_LEVELS = [
    (7, "Friendly reminder", 0.0),
    (21, "First reminder", 5.0),
    (35, "Second reminder", 10.0),
    (60, "Final notice before collection", 25.0),
]


def generate_dunning_letters(invoicer: InvoiceBuilder, customers: Dict[str, Customer], today: date) -> List[str]:
    letters = []
    by_customer = defaultdict(list)
    for inv in invoicer.invoices.values():
        if inv.is_credit_note or inv.open_amount() <= 0.004:
            continue
        overdue = (today - inv.due_date).days
        if overdue < DUNNING_LEVELS[0][0]:
            continue
        by_customer[inv.customer_id].append((inv, overdue))
    for cid in sorted(by_customer):
        cust = customers.get(cid)
        if cust is None:
            continue
        items = sorted(by_customer[cid], key=lambda t: t[0].number)
        worst = max(overdue for _, overdue in items)
        level_name, fee = DUNNING_LEVELS[0][1], 0.0
        for days, name, level_fee in DUNNING_LEVELS:
            if worst >= days:
                level_name, fee = name, level_fee
        total_open = sum(inv.open_amount() for inv, _ in items)
        body = []
        body.append("%s - %s" % (SHOP_NAME, level_name.upper()))
        body.append("To: %s, %s %s (%s)" % (cust.name, cust.postal_code, cust.city, cust.country))
        greeting = "Dear %s," % (cust.name if cust.is_business else cust.name.split()[0])
        body.append(greeting)
        text = ("according to our records the following invoices are still open. "
                "Please transfer the outstanding amount within 7 days.")
        if fee:
            text += " A dunning fee of %s has been added." % fmt_money(fee)
        body.extend(wrap_text(text, 70))
        for inv, overdue in items:
            body.append("  %-16s due %s  %3d days  %10s" % (inv.number, inv.due_date.isoformat(), overdue,
                                                           format(inv.open_amount(), ",.2f")))
        body.append("  %-16s %33s" % ("Total incl. fee", format(money(total_open + fee), ",.2f")))
        letters.append("\n".join(body))
    return letters


# ---------------------------------------------------------------------------
# Price history audit
# ---------------------------------------------------------------------------

def audit_price_history(pricing: PricingEngine, products: Dict[str, Product]) -> List[str]:
    findings = []
    for sku in sorted(pricing.price_history):
        history = sorted(pricing.price_history[sku])
        product = products.get(sku)
        if product is None:
            findings.append("%s: price history for unknown product" % sku)
            continue
        previous = product.list_price
        for day, price in history:
            change = safe_div(price - previous, previous)
            if abs(change) > 0.2:
                findings.append("%s: %s change %+.1f%% (%.2f -> %.2f)"
                                % (sku, day.isoformat(), change * 100, previous, price))
            if price < product.unit_cost:
                findings.append("%s: %s price %.2f below cost %.2f" % (sku, day.isoformat(), price, product.unit_cost))
            previous = price
        if len(history) > 3:
            findings.append("%s: %d price changes, consider a promotion instead" % (sku, len(history)))
    return findings


# ---------------------------------------------------------------------------
# Month-end close (finance).  Ported from the old Access database in 2018.
# ---------------------------------------------------------------------------

GL_ACCOUNTS = {
    "1200": "Receivables",
    "1360": "Suspense / unmatched payments",
    "1400": "Inventory",
    "1800": "Bank",
    "3720": "Loyalty points liability",
    "3806": "VAT payable 19%",
    "3801": "VAT payable 7%",
    "3070": "Accrued carrier costs",
    "4400": "Revenue 19%",
    "4300": "Revenue 7%",
    "4125": "Revenue intra-community (tax free)",
    "4120": "Revenue export (tax free)",
    "4730": "Shipping income",
    "5400": "Cost of goods sold",
    "4730X": "Carrier costs",
    "6930": "Bad debt provision expense",
    "4900": "Inventory shrinkage",
}

BAD_DEBT_RATES = {"current": 0.0, "1-30": 0.01, "31-60": 0.05, "61-90": 0.2, "90+": 0.5}


def run_month_end_close(month: str, inventory: Inventory, invoicer: InvoiceBuilder, ledger: PaymentLedger,
                        loyalty: LoyaltyProgram, customers: List[Customer], results: List[OrderResult],
                        shipments: List[Shipment], returns: List[Dict[str, Any]], audit: AuditTrail,
                        rng: random.Random, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Produce the month-end close package: VAT return, accruals, provisions,
    stock count adjustments and the journal.  Returns a dict with a rendered
    'report' string and the journal entries."""
    options = dict(options or {})
    closed_months = set(options.get("closed_months", []))
    approval_budget = float(options.get("approval_budget", 400.0))
    carrier_cost_ratio = float(options.get("carrier_cost_ratio", 0.72))
    count_error_rate = float(options.get("count_error_rate", 0.08))
    redemption_probability = float(options.get("redemption_probability", 0.6))
    close = OrderedDict()  # type: Dict[str, Any]
    close["month"] = month

    # --- period and lock check -------------------------------------------------
    year, mon = int(month[:4]), int(month[5:7])
    period_start = date(year, mon, 1)
    period_end = last_of_month(period_start)
    if month in closed_months:
        audit.log("close %s: period already closed" % month)
        close["status"] = "already-closed"
        close["report"] = "Period %s is already closed." % month
        return close
    customer_by_id = {c.customer_id: c for c in customers}
    audit.log("close %s: %s .. %s" % (month, period_start.isoformat(), period_end.isoformat()))

    # --- invoices of the period ---------------------------------------------------
    month_invoices = []  # type: List[Invoice]
    credit_notes = []  # type: List[Invoice]
    for inv in invoicer.invoices.values():
        if not (period_start <= inv.issue_date <= period_end):
            continue
        if inv.is_credit_note:
            credit_notes.append(inv)
        else:
            month_invoices.append(inv)
    close["invoices"] = len(month_invoices)
    close["credit_notes"] = len(credit_notes)

    # --- revenue and VAT by rate ------------------------------------------------------
    net_by_rate = defaultdict(float)
    vat_by_rate = defaultdict(float)
    shipping_income = 0.0
    zero_rated = defaultdict(float)  # reason -> net
    for inv in month_invoices + credit_notes:
        cust = customer_by_id.get(inv.customer_id)
        for line in inv.lines:
            if line.description == "Shipping" or line.description == "Return: Shipping":
                shipping_income += line.net
                vat_by_rate[line.tax_rate] += line.tax
                continue
            if line.tax_rate == 0.0:
                if cust is not None and cust.vat_id and cust.country in EU_COUNTRIES:
                    zero_rated["intra-community"] += line.net
                else:
                    zero_rated["export"] += line.net
                continue
            net_by_rate[line.tax_rate] += line.net
            vat_by_rate[line.tax_rate] += line.tax
    vat_payable = money(sum(vat_by_rate.values()))
    close["net_by_rate"] = OrderedDict((("%.0f%%" % (k * 100)), money(v)) for k, v in sorted(net_by_rate.items()))
    close["vat_payable"] = vat_payable

    # --- intra-community supplies (ZM / recapitulative statement) --------------------------
    ic_supplies = defaultdict(float)
    ic_missing_vat = []
    for inv in month_invoices:
        cust = customer_by_id.get(inv.customer_id)
        if cust is None or cust.country == "DE" or cust.country not in EU_COUNTRIES:
            continue
        zero_lines = [l for l in inv.lines if l.tax_rate == 0.0]
        if not zero_lines:
            continue
        if not cust.vat_id:
            ic_missing_vat.append(inv.number)
            continue
        ic_supplies[cust.vat_id] += sum(l.net for l in zero_lines)
    close["ic_supplies"] = OrderedDict((k, money(v)) for k, v in sorted(ic_supplies.items()))

    # --- carrier cost accrual -------------------------------------------------------------------
    month_order_ids = set(r.order_id for r in results if r.ship_date and period_start <= r.ship_date <= period_end)
    carrier_accrual = defaultdict(float)
    shipments_in_period = 0
    for sh in shipments:
        if sh.order_id not in month_order_ids:
            continue
        shipments_in_period += 1
        if sh.carrier == "SELF":
            continue
        if sh.cost > 0:
            estimate = sh.cost * carrier_cost_ratio
        else:
            # free shipping: estimate from the domestic rate card
            base, per_kg, _ = SHIPPING_RATES["domestic"]
            estimate = (base + per_kg * math.ceil(max(sh.weight_kg, 0.1))) * carrier_cost_ratio
        carrier_accrual[sh.carrier] += estimate * max(1, sh.parcels) / max(1, sh.parcels)
    total_carrier_accrual = money(sum(carrier_accrual.values()))
    close["carrier_accrual"] = OrderedDict((k, money(v)) for k, v in sorted(carrier_accrual.items()))

    # --- cost of goods sold -------------------------------------------------------------------------
    cogs = 0.0
    cogs_returns = 0.0
    for r in results:
        if r.order_id in month_order_ids and r.status in ("shipped", "partial"):
            cogs += r.cost
    for ret in returns:
        if ret.get("month") != month or ret.get("reason") == "damaged":
            continue
        product = inventory.products.get(ret["sku"])
        if product is not None:
            cogs_returns += product.unit_cost * ret["qty"]
    cogs = money(cogs - cogs_returns)
    close["cogs"] = cogs

    # --- stock count (cycle count of every 3rd SKU) -------------------------------------------------------
    count_diffs = []  # type: List[Tuple[str, str, int, int, float]]
    counted = 0
    for idx, sku in enumerate(sorted(inventory.products)):
        if idx % 3 != mon % 3:
            continue
        product = inventory.products[sku]
        for wh in WAREHOUSES:
            system_qty = inventory.on_hand[(wh, sku)]
            if system_qty <= 0:
                continue
            counted += 1
            physical = system_qty
            if rng.random() < count_error_rate:
                physical = max(0, system_qty + rng.choice([-3, -2, -1, -1, -1, 1]))
            if physical != system_qty:
                value = (physical - system_qty) * product.unit_cost
                count_diffs.append((sku, wh, system_qty, physical, money(value)))
    close["counted_locations"] = counted

    # --- book count differences within the approval budget ----------------------------------------------------
    pending_diffs = sorted(count_diffs, key=lambda d: (abs(d[4]), d[0], d[1]))
    booked_diffs = []
    needs_approval = []
    used_budget = 0.0
    while pending_diffs:
        sku, wh, system_qty, physical, value = pending_diffs.pop(0)
        if used_budget + abs(value) > approval_budget:
            needs_approval.append((sku, wh, value))
            needs_approval.extend((d[0], d[1], d[4]) for d in pending_diffs)
            break
        inventory.adjust(sku, wh, physical - system_qty, "count")
        used_budget += abs(value)
        booked_diffs.append((sku, wh, physical - system_qty, value))
    shrinkage = money(-sum(d[3] for d in booked_diffs))
    close["shrinkage"] = shrinkage
    close["needs_approval"] = needs_approval

    # --- receivables aging and bad debt provision -----------------------------------------------------------
    aging = ledger.aging(invoicer.invoices.values(), period_end)
    provision = 0.0
    for bucket, amount in aging.items():
        rate = BAD_DEBT_RATES.get(bucket, 0.0)
        provision += amount * rate
    # business customers with credit insurance get half the provision
    insured_open = 0.0
    for inv in invoicer.invoices.values():
        cust = customer_by_id.get(inv.customer_id)
        if cust is not None and cust.is_business and not inv.is_credit_note and inv.issue_date <= period_end:
            insured_open += inv.open_amount()
    total_open = sum(aging.values())
    if total_open > 0:
        provision = provision * (1 - 0.5 * safe_div(insured_open, total_open))
    provision = money(provision)
    close["aging"] = aging
    close["provision"] = provision

    # --- loyalty liability ---------------------------------------------------------------------------------
    outstanding_points = 0
    dormant_points = 0
    for cust in customers:
        if cust.points <= 0:
            continue
        last_activity = None
        for day, cid, pts, ref in loyalty.ledger:
            if cid == cust.customer_id and (last_activity is None or day > last_activity):
                last_activity = day
        if last_activity is None or (period_end - last_activity).days > 180:
            dormant_points += cust.points
        else:
            outstanding_points += cust.points
    loyalty_liability = money((outstanding_points * redemption_probability +
                               dormant_points * redemption_probability * 0.3) * LOYALTY_REDEEM_VALUE)
    close["loyalty_liability"] = loyalty_liability

    # --- suspense account ---------------------------------------------------------------------------------
    suspense = 0.0
    suspense_items = []
    for pay in ledger.unmatched:
        if pay.received > period_end:
            continue
        suspense += pay.amount
        suspense_items.append("%s %s %.2f %s" % (pay.payment_id, pay.received.isoformat(), pay.amount,
                                                 pay.reference[:30]))
    close["suspense"] = money(suspense)

    # --- journal ----------------------------------------------------------------------------------------------
    journal = []  # type: List[Tuple[str, float, float, str]]
    revenue_accounts = {DEFAULT_TAX_RATE: ("4400", "3806"), REDUCED_TAX_RATE: ("4300", "3801")}
    for rate in sorted(net_by_rate):
        rev_acc, vat_acc = revenue_accounts.get(rate, ("4400", "3806"))
        journal.append((rev_acc, 0.0, money(net_by_rate[rate]), "revenue %.0f%%" % (rate * 100)))
    for rate in sorted(vat_by_rate):
        if rate == 0.0:
            continue
        _, vat_acc = revenue_accounts.get(rate, ("4400", "3806"))
        journal.append((vat_acc, 0.0, money(vat_by_rate[rate]), "VAT %.0f%%" % (rate * 100)))
    if zero_rated.get("intra-community"):
        journal.append(("4125", 0.0, money(zero_rated["intra-community"]), "intra-community supplies"))
    if zero_rated.get("export"):
        journal.append(("4120", 0.0, money(zero_rated["export"]), "export supplies"))
    journal.append(("4730", 0.0, money(shipping_income), "shipping income"))
    gross_billed = money(sum(i.gross_total for i in month_invoices) + sum(i.gross_total for i in credit_notes))
    journal.append(("1200", gross_billed, 0.0, "receivables from billing"))
    journal.append(("5400", cogs, 0.0, "cost of goods sold"))
    journal.append(("1400", 0.0, cogs, "inventory consumption"))
    journal.append(("4730X", total_carrier_accrual, 0.0, "carrier costs (accrued)"))
    journal.append(("3070", 0.0, total_carrier_accrual, "carrier accrual"))
    if shrinkage:
        journal.append(("4900", shrinkage, 0.0, "count differences"))
        journal.append(("1400", 0.0, shrinkage, "count differences"))
    journal.append(("6930", provision, 0.0, "bad debt provision"))
    journal.append(("1200", 0.0, provision, "bad debt provision"))
    if suspense:
        journal.append(("1800", money(suspense), 0.0, "unmatched receipts"))
        journal.append(("1360", 0.0, money(suspense), "unmatched receipts"))
    close["journal"] = journal

    # --- trial balance -------------------------------------------------------------------------------------------
    balances = defaultdict(float)
    total_debit = 0.0
    total_credit = 0.0
    for account, debit, credit, text in journal:
        balances[account] += debit - credit
        total_debit += debit
        total_credit += credit
    imbalance = money(total_debit - total_credit)
    try:
        if abs(imbalance) > 0.05:
            raise ValueError("journal out of balance by %.2f" % imbalance)
        close["balanced"] = True
    except ValueError as exc:
        close["balanced"] = False
        audit.log("close %s: %s" % (month, exc))
    close["trial_balance"] = OrderedDict((k, money(v)) for k, v in sorted(balances.items()) if abs(v) > 0.004)

    # --- checklist ------------------------------------------------------------------------------------------------
    checks = [
        ("journal balanced", close["balanced"], False),
        ("no IC supplies without VAT id", not ic_missing_vat, True),
        ("count differences approved", not needs_approval, False),
        ("suspense below 1000", suspense < 1000.0, False),
        ("invoices issued", bool(month_invoices) or not month_order_ids, True),
    ]
    check_lines = []
    for name, ok, blocking in checks:
        check_lines.append("[%s] %s%s" % ("x" if ok else " ", name, " (blocking)" if blocking and not ok else ""))
        if not ok and blocking:
            close["status"] = "blocked"
            close["blocked_by"] = name
            close["report"] = "\n".join(["CLOSE %s BLOCKED" % month] + check_lines)
            audit.log("close %s blocked by %s" % (month, name))
            return close

    # --- render ------------------------------------------------------------------------------------------------------
    out = []
    out.append(hr("#"))
    out.append(center("MONTH-END CLOSE %s" % month))
    out.append(hr("#"))
    out.append("Invoices %d, credit notes %d, shipments %d" % (len(month_invoices), len(credit_notes),
                                                              shipments_in_period))
    out.append("VAT return:")
    for rate in sorted(net_by_rate):
        out.append("  net %5.0f%%  %12s   VAT %10s" % (rate * 100, format(money(net_by_rate[rate]), ",.2f"),
                                                    format(money(vat_by_rate[rate]), ",.2f")))
    for reason in sorted(zero_rated):
        out.append("  tax free (%s) %12s" % (reason, format(money(zero_rated[reason]), ",.2f")))
    out.append("  VAT payable total %s" % fmt_money(vat_payable))
    if ic_supplies:
        out.append("Recapitulative statement:")
        for vat_id, amount in close["ic_supplies"].items():
            out.append("  %-16s %12s" % (vat_id, format(amount, ",.2f")))
    out.append("Carrier accrual: %s" % fmt_money(total_carrier_accrual))
    out.append("COGS: %s (returns %s)" % (fmt_money(cogs), fmt_money(cogs_returns)))
    out.append("Cycle count: %d locations, %d differences, booked %d, shrinkage %s"
               % (counted, len(count_diffs), len(booked_diffs), fmt_money(shrinkage)))
    for sku, wh, value in needs_approval:
        out.append("  needs approval: %s @ %s %s" % (sku, wh, fmt_money(value)))
    out.append("Aging: " + ", ".join("%s %s" % (k, format(v, ",.2f")) for k, v in aging.items()))
    out.append("Bad debt provision: %s" % fmt_money(provision))
    out.append("Loyalty liability: %s (active %d pts, dormant %d pts)"
               % (fmt_money(loyalty_liability), outstanding_points, dormant_points))
    out.append("Suspense: %s" % fmt_money(suspense))
    for item in suspense_items[:5]:
        out.append("  " + item)
    out.append("Journal:")
    for account, debit, credit, text in journal:
        out.append("  %-6s %-34s %12s %12s" % (account, GL_ACCOUNTS.get(account, "?")[:34],
                                               format(debit, ",.2f") if debit else "",
                                               format(credit, ",.2f") if credit else ""))
    out.append("  %-41s %12s %12s" % ("Totals", format(total_debit, ",.2f"), format(total_credit, ",.2f")))
    out.append("Checklist:")
    out.extend("  " + l for l in check_lines)
    close["status"] = "closed" if all(ok for _, ok, _ in checks) else "closed-with-findings"
    out.append("Status: %s" % close["status"])
    close["report"] = "\n".join(out)
    return close


# ---------------------------------------------------------------------------
# Dynamic pricing rules (introduced for the 2022 clearance sale, never removed)
# ---------------------------------------------------------------------------

def apply_price_rules(pricing: PricingEngine, inventory: Inventory, forecasts: Dict[str, Dict[str, Any]],
                      today: date, max_markdown: float = 0.3) -> List[Tuple[str, float, float, str]]:
    """Adjust list prices based on stock cover.

    * slow movers with > 26 weeks of cover get marked down (and tagged clearance)
    * scarce items with < 2 weeks of cover and a long supplier lead time get +5%
    * prices end in .90 / .00 according to the house style
    """
    changes = []
    for sku in sorted(inventory.products):
        product = inventory.products[sku]
        if not product.active:
            continue
        stock = inventory.total_available(sku)
        fc = forecasts.get(sku)
        weekly = fc["forecast_per_week"] if fc else 0.0
        cover_weeks = safe_div(stock, weekly, default=999.0)
        current = pricing.list_price(sku)
        new_price = current
        reason = ""
        if cover_weeks > 26 and stock > product.reorder_point * 2:
            steps = min(3, int((cover_weeks - 26) // 13) + 1)
            markdown = min(max_markdown, 0.1 * steps)
            new_price = current * (1 - markdown)
            reason = "slow mover, %.0f weeks cover" % min(cover_weeks, 999)
            if "clearance" not in product.tags and markdown >= 0.2:
                product.tags.append("clearance")
        elif cover_weeks < 2 and SUPPLIERS.get(product.supplier, {}).get("lead_days", 0) >= 14:
            new_price = current * 1.05
            reason = "scarce, %.1f weeks cover" % cover_weeks
        if not reason:
            continue
        # house style rounding
        if new_price >= 100:
            new_price = math.floor(new_price) - 1 + 0.0 if new_price % 1 < 0.5 else math.floor(new_price)
            new_price = float(int(new_price))
        else:
            new_price = math.floor(new_price) + 0.9
        new_price = max(new_price, money(product.unit_cost * 1.05))
        if abs(new_price - current) < 0.5:
            continue
        pricing.set_price(sku, money(new_price), today)
        changes.append((sku, current, money(new_price), reason))
    return changes


# ---------------------------------------------------------------------------
# Shipping rate matrix (for the website FAQ page)
# ---------------------------------------------------------------------------

def compute_shipping_matrix(shipping: ShippingCalculator, weights: List[float],
                            methods: Tuple[str, ...] = ("economy", "standard", "express")) -> str:
    zone_country = OrderedDict()
    for country in sorted(SHIPPING_ZONES):
        zone = SHIPPING_ZONES[country]
        if zone not in zone_country:
            zone_country[zone] = country
    rows = []
    for zone, country in zone_country.items():
        for method in methods:
            row = ["%s/%s" % (zone, method)]
            for w in weights:
                row.append(shipping.quote(w, country, method))
            rows.append(row)
    headers = ["zone/method"] + ["%gkg" % w for w in weights]
    table = render_text_table(headers, rows, title="Shipping rates (EUR, incl. surcharges where applicable)")
    notes = []
    for zone, country in zone_country.items():
        eta_std = shipping.eta(date(2024, 4, 1), country, "standard")
        notes.append("%s via %s, standard ETA %s" % (zone, shipping.carrier_for(country, "standard"),
                                                     eta_std.isoformat()))
    return table + "\n" + "\n".join(notes)


# ---------------------------------------------------------------------------
# Marketing campaigns
# ---------------------------------------------------------------------------

def run_loyalty_campaign(customers: List[Customer], segments: Dict[str, List[str]], loyalty: LoyaltyProgram,
                         today: date) -> Dict[str, Any]:
    """Award bonus points per segment and produce mail-merge lines.

    Budget is capped; champions are served first, then at-risk (win-back),
    then promising.  Business customers and people without a valid e-mail
    are skipped.  Newsletter opt-out customers get points but no e-mail.
    """
    by_id = {c.customer_id: c for c in customers}
    plan = [("champions", 500, "Thank you for being a champion"),
            ("at-risk", 300, "We miss you - here are some points"),
            ("promising", 150, "Welcome back bonus"),
            ("loyal", 200, "Loyalty bonus")]
    budget_points = 4000
    awarded = OrderedDict()
    mails = []
    skipped = Counter()
    for segment, points, subject in plan:
        for cid in segments.get(segment, []):
            cust = by_id.get(cid)
            if cust is None:
                skipped["missing"] += 1
                continue
            if cust.is_business:
                skipped["business"] += 1
                continue
            if cid in awarded:
                skipped["already"] += 1
                continue
            if budget_points < points:
                skipped["budget"] += 1
                continue
            cust.points += points
            loyalty.ledger.append((today, cid, points, "CAMPAIGN-%s" % segment.upper()))
            budget_points -= points
            awarded[cid] = (segment, points)
            if not is_valid_email(cust.email):
                skipped["bad-email"] += 1
                continue
            if not cust.newsletter:
                skipped["no-optin"] += 1
                continue
            mails.append("%s;%s;%s;%d" % (cust.email, cust.name, subject, points))
    return {
        "awarded": awarded,
        "mails": mails,
        "skipped": dict(sorted(skipped.items())),
        "budget_left": budget_points,
    }


# ---------------------------------------------------------------------------
# Batch overview
# ---------------------------------------------------------------------------

def summarize_batches(batch_stats: List[Tuple[str, Dict[str, Any]]]) -> str:
    rows = []
    totals = Counter()
    money_totals = defaultdict(float)
    statuses = set()
    for label, stats in batch_stats:
        for s in stats.get("by_status", {}):
            statuses.add(s)
    status_cols = sorted(statuses)
    for label, stats in batch_stats:
        by_status = stats.get("by_status", {})
        row = [label, stats.get("processed", 0)]
        for s in status_cols:
            row.append(by_status.get(s, 0))
            totals[s] += by_status.get(s, 0)
        row.append(float(stats.get("revenue", 0.0)))
        row.append(float(stats.get("gross_margin", 0.0)))
        money_totals["revenue"] += stats.get("revenue", 0.0)
        money_totals["margin"] += stats.get("gross_margin", 0.0)
        totals["processed"] += stats.get("processed", 0)
        rows.append(row)
    rows.append(["TOTAL", totals["processed"]] + [totals[s] for s in status_cols] +
                [money(money_totals["revenue"]), money(money_totals["margin"])])
    headers = ["batch", "orders"] + [s[:8] for s in status_cols] + ["revenue", "margin"]
    return render_text_table(headers, rows, max_width=120, title="Order batches")


# ---------------------------------------------------------------------------
# Customer service desk
# ---------------------------------------------------------------------------

class CustomerService:
    """Thin layer the support team uses from the admin console."""

    GOODWILL_LIMIT = 1000

    def __init__(self, customers: List[Customer], loyalty: LoyaltyProgram):
        self.customers = {c.customer_id: c for c in customers}
        self.loyalty = loyalty
        self.tickets = []  # type: List[Dict[str, Any]]
        self.goodwill_given = Counter()

    def find(self, query: str) -> List[Customer]:
        q = query.lower()
        hits = []
        for cid in sorted(self.customers):
            c = self.customers[cid]
            if q in c.name.lower() or q in c.email.lower() or q == cid.lower():
                hits.append(c)
        return hits

    def open_ticket(self, customer_id: str, subject: str, day: date, priority: str = "normal") -> str:
        ticket_id = "T-%04d" % (len(self.tickets) + 1)
        self.tickets.append({"id": ticket_id, "customer": customer_id, "subject": subject,
                             "opened": day, "priority": priority, "status": "open", "notes": []})
        return ticket_id

    def add_note(self, ticket_id: str, text: str) -> None:
        for t in self.tickets:
            if t["id"] == ticket_id:
                t["notes"].append(text)
                return
        raise KeyError(ticket_id)

    def close_ticket(self, ticket_id: str, resolution: str) -> None:
        for t in self.tickets:
            if t["id"] == ticket_id:
                t["status"] = "closed"
                t["notes"].append("resolution: " + resolution)
                return
        raise KeyError(ticket_id)

    def goodwill_points(self, customer_id: str, points: int, day: date, ticket_id: str) -> int:
        cust = self.customers[customer_id]
        remaining = self.GOODWILL_LIMIT - self.goodwill_given[customer_id]
        granted = max(0, min(points, remaining))
        if granted:
            cust.points += granted
            self.goodwill_given[customer_id] += granted
            self.loyalty.ledger.append((day, customer_id, granted, "GOODWILL-" + ticket_id))
        return granted

    def change_email(self, customer_id: str, new_email: str) -> bool:
        if not is_valid_email(new_email):
            return False
        for c in self.customers.values():
            if c.email.lower() == new_email.lower() and c.customer_id != customer_id:
                return False
        self.customers[customer_id].email = new_email
        return True

    def merge_customers(self, keep_id: str, drop_id: str) -> Customer:
        keep = self.customers[keep_id]
        drop = self.customers.pop(drop_id)
        keep.points += drop.points
        if TIER_ORDER.index(drop.tier) > TIER_ORDER.index(keep.tier):
            keep.tier = drop.tier
        keep.joined = min(keep.joined, drop.joined)
        keep.newsletter = keep.newsletter or drop.newsletter
        for t in self.tickets:
            if t["customer"] == drop_id:
                t["customer"] = keep_id
        return keep

    def open_tickets_report(self, today: date) -> List[str]:
        lines = []
        for t in self.tickets:
            if t["status"] != "open":
                continue
            age = (today - t["opened"]).days
            flag = "!" if age > 2 or t["priority"] == "high" else " "
            lines.append("%s %s %-6s %-40s %2dd" % (flag, t["id"], t["customer"], t["subject"][:40], age))
        return lines


# ---------------------------------------------------------------------------
# Hamburg van deliveries (bulky items within the city are delivered by our own van)
# ---------------------------------------------------------------------------

HAMBURG_DISTRICTS = {
    "20457": (0.3, -0.8), "20099": (1.1, 0.4), "22767": (-3.2, -0.9),
    "22041": (5.0, 2.5), "21073": (1.5, -6.0), "22297": (1.0, 4.8), "20249": (0.5, 3.1),
}


def plan_van_routes(shipments: List[Shipment], orders: List[Order], customers: List[Customer],
                    van_capacity_kg: float = 400.0, max_stops: int = 6) -> List[Dict[str, Any]]:
    """Nearest-neighbour routing for the city van.

    Only domestic shipments to Hamburg postcodes qualify.  The depot is our
    warehouse at the harbour.  The van does several tours per day if needed;
    each tour respects weight capacity and a maximum number of stops.
    """
    order_by_id = {o.order_id: o for o in orders}
    customer_by_id = {c.customer_id: c for c in customers}
    depot = HAMBURG_DISTRICTS["20457"]
    stops = []
    for sh in shipments:
        order = order_by_id.get(sh.order_id)
        if order is None or order.ship_country != "DE":
            continue
        cust = customer_by_id.get(order.customer_id)
        if cust is None or cust.city != "Hamburg":
            continue
        coords = HAMBURG_DISTRICTS.get(cust.postal_code)
        if coords is None:
            # unknown district: spread deterministically around the centre
            h = stable_hash(cust.customer_id)
            coords = ((h % 100) / 20.0 - 2.5, ((h // 100) % 100) / 20.0 - 2.5)
        stops.append({"shipment": sh.shipment_id, "customer": cust.customer_id, "xy": coords,
                      "kg": sh.weight_kg})
    stops.sort(key=lambda s: s["shipment"])
    tours = []
    remaining = list(stops)
    while remaining:
        position = depot
        load = 0.0
        tour = []
        distance = 0.0
        while remaining and len(tour) < max_stops:
            candidates = [s for s in remaining if load + s["kg"] <= van_capacity_kg]
            if not candidates:
                break
            nearest = min(candidates, key=lambda s: (math.hypot(s["xy"][0] - position[0], s["xy"][1] - position[1]),
                                                     s["shipment"]))
            distance += math.hypot(nearest["xy"][0] - position[0], nearest["xy"][1] - position[1])
            position = nearest["xy"]
            load += nearest["kg"]
            tour.append(nearest)
            remaining.remove(nearest)
        if not tour:
            # a single shipment heavier than the van: hand it to the carrier
            oversize = remaining.pop(0)
            tours.append({"tour": len(tours) + 1, "stops": [oversize["shipment"]], "km": 0.0,
                          "kg": oversize["kg"], "note": "too heavy for van, use carrier"})
            continue
        distance += math.hypot(depot[0] - position[0], depot[1] - position[1])
        tours.append({
            "tour": len(tours) + 1,
            "stops": [s["shipment"] for s in tour],
            "km": round(distance * 1.3, 1),  # streets are not straight lines
            "kg": round(load, 1),
            "note": "",
        })
    return tours


# ---------------------------------------------------------------------------
# Customer dashboard (support console "360 view")
# ---------------------------------------------------------------------------

def render_customer_dashboard(customer: Customer, results: List[OrderResult], loyalty: LoyaltyProgram,
                              invoicer: InvoiceBuilder, returns: List[Dict[str, Any]]) -> str:
    out = [hr("="), "CUSTOMER %s  %s" % (customer.customer_id, customer.name), hr("=")]
    out.append("e-mail %s | %s %s %s | tier %s | joined %s" % (
        customer.email, customer.postal_code, customer.city, customer.country, customer.tier,
        customer.joined.isoformat()))
    if customer.is_business:
        out.append("business customer, VAT %s, credit limit %s" % (customer.vat_id or "-",
                                                                   fmt_money(customer.credit_limit)))
    mine = [r for r in results if r.customer_id == customer.customer_id]
    status_counts = Counter(r.status for r in mine)
    lifetime = sum(r.total - r.tax for r in mine if r.status in ("shipped", "partial"))
    out.append("orders %d (%s), lifetime net %s" % (
        len(mine), ", ".join("%s %d" % kv for kv in sorted(status_counts.items())), fmt_money(lifetime)))
    if mine:
        last = max(mine, key=lambda r: (r.order_date or date.min, r.order_id))
        out.append("last order %s on %s: %s %s" % (last.order_id, last.order_date.isoformat() if last.order_date
                                                   else "-", last.status, fmt_money(last.total)))
    open_invoices = []
    credit_total = 0.0
    for inv in invoicer.invoices.values():
        if inv.customer_id != customer.customer_id:
            continue
        if inv.is_credit_note:
            credit_total += -inv.gross_total
        elif inv.open_amount() > 0.004:
            open_invoices.append(inv)
    if open_invoices:
        out.append("open invoices:")
        for inv in open_invoices:
            out.append("  %s due %s open %s" % (inv.number, inv.due_date.isoformat(), fmt_money(inv.open_amount())))
    else:
        out.append("no open invoices")
    if credit_total:
        out.append("credited so far: %s" % fmt_money(credit_total))
    my_returns = [r for r in returns if any(m.order_id == r["order_id"] for m in mine)]
    if my_returns:
        reasons = Counter(r["reason"] for r in my_returns)
        out.append("returns: %d (%s)" % (len(my_returns), ", ".join("%s %d" % kv for kv in sorted(reasons.items()))))
        ordered_units = sum(1 for r in mine if r.status in ("shipped", "partial"))
        if ordered_units and len(my_returns) / float(ordered_units) > 0.3:
            out.append("  ! high return ratio")
    out.append("points balance %d" % customer.points)
    for line in loyalty.statement(customer.customer_id)[-3:]:
        out.append("  " + line)
    flags = []
    if customer.blocked:
        flags.append("BLOCKED")
    if not is_valid_email(customer.email):
        flags.append("bad e-mail")
    if any(r.status == "fraud-hold" for r in mine):
        flags.append("fraud hold")
    if any(r.status == "credit-hold" for r in mine):
        flags.append("credit hold")
    out.append("flags: %s" % (", ".join(flags) if flags else "none"))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Consistency checks (run by QA after every release)
# ---------------------------------------------------------------------------

def snapshot_diff(before: List[Tuple[str, str, int, int]], after: List[Tuple[str, str, int, int]],
                  min_change: int = 1) -> List[str]:
    """Compare two Inventory.snapshot() lists and describe what changed."""
    old = {(wh, sku): (qty, res) for wh, sku, qty, res in before}
    new = {(wh, sku): (qty, res) for wh, sku, qty, res in after}
    lines = []
    for key in sorted(set(old) | set(new)):
        q0, r0 = old.get(key, (0, 0))
        q1, r1 = new.get(key, (0, 0))
        dq = q1 - q0
        dr = r1 - r0
        if abs(dq) < min_change and abs(dr) < min_change:
            continue
        wh, sku = key
        if key not in old:
            lines.append("%s@%s: new location with %d" % (sku, wh, q1))
        elif q1 == 0 and q0 > 0:
            lines.append("%s@%s: sold out (was %d)" % (sku, wh, q0))
        else:
            parts = []
            if dq:
                parts.append("stock %+d -> %d" % (dq, q1))
            if dr:
                parts.append("reserved %+d -> %d" % (dr, r1))
            lines.append("%s@%s: %s" % (sku, wh, ", ".join(parts)))
    return lines


def check_data_integrity(inventory: Inventory, invoicer: InvoiceBuilder, ledger: PaymentLedger,
                         results: List[OrderResult], shipments: List[Shipment]) -> List[str]:
    problems = []
    for (wh, sku), qty in sorted(inventory.on_hand.items()):
        if qty < 0:
            problems.append("negative stock %s@%s: %d" % (sku, wh, qty))
        if inventory.reserved[(wh, sku)] > qty:
            problems.append("over-reserved %s@%s: %d > %d" % (sku, wh, inventory.reserved[(wh, sku)], qty))
    movement_totals = defaultdict(int)
    for mv in inventory.movements:
        movement_totals[(mv.warehouse, mv.sku)] += mv.delta
    for key in sorted(set(movement_totals) | set(inventory.on_hand)):
        if movement_totals.get(key, 0) != inventory.on_hand.get(key, 0):
            problems.append("movement mismatch %s@%s: moves %d vs stock %d"
                            % (key[1], key[0], movement_totals.get(key, 0), inventory.on_hand.get(key, 0)))
    invoiced_orders = set()
    for inv in invoicer.invoices.values():
        if inv.is_credit_note:
            continue
        if inv.order_id in invoiced_orders:
            problems.append("order %s invoiced twice" % inv.order_id)
        invoiced_orders.add(inv.order_id)
        net = money(sum(l.net for l in inv.lines))
        if abs(net - inv.net_total) > 0.01:
            problems.append("invoice %s net mismatch" % inv.number)
        if inv.paid > inv.gross_total + 50:
            problems.append("invoice %s heavily overpaid (%.2f > %.2f)" % (inv.number, inv.paid, inv.gross_total))
    shipped_ids = set(sh.order_id for sh in shipments)
    for r in results:
        if r.status in ("shipped", "partial") and r.order_id not in shipped_ids:
            problems.append("order %s marked %s but no shipment" % (r.order_id, r.status))
        if r.status in ("shipped", "partial") and not r.invoice_no:
            problems.append("order %s shipped without invoice" % r.order_id)
        if r.status == "rejected" and r.invoice_no:
            problems.append("rejected order %s has an invoice" % r.order_id)
    received = ledger.total_received()
    applied = money(sum(i.paid for i in invoicer.invoices.values()))
    unmatched = money(sum(p.amount for p in ledger.unmatched))
    if abs(received - applied - unmatched) > 0.01:
        problems.append("ledger mismatch: received %.2f, applied %.2f, unmatched %.2f"
                        % (received, applied, unmatched))
    return problems


# ---------------------------------------------------------------------------
# Nightly demo run
# ---------------------------------------------------------------------------

@dataclass
class ShopContext:
    rng: random.Random
    products: List[Product]
    customers: List[Customer]
    orders: List[Order]
    inventory: Inventory
    pricing: PricingEngine
    shipping: ShippingCalculator
    loyalty: LoyaltyProgram
    invoicer: InvoiceBuilder
    ledger: PaymentLedger
    fraud: FraudScreen
    audit: AuditTrail
    results: List[OrderResult] = field(default_factory=list)
    shipments: List[Shipment] = field(default_factory=list)
    returns: List[Dict[str, Any]] = field(default_factory=list)
    batch_stats: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    forecasts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    segments: Dict[str, List[str]] = field(default_factory=dict)


def banner(title: str) -> None:
    print()
    print(hr("*"))
    print("* " + pad_right(title, REPORT_WIDTH - 4) + " *")
    print(hr("*"))


def setup_shop(seed: int = 42) -> ShopContext:
    rng = random.Random(seed)
    products = build_catalog()
    customers = build_customers(rng)
    orders = build_orders(rng, customers, products)
    inventory = Inventory(products)
    inventory.seed_stock(rng)
    pricing = PricingEngine(inventory.products)
    pricing.add_promotion(Promotion("WINTER", "safety", 0.10, date(2024, 1, 1), date(2024, 1, 31)))
    pricing.add_promotion(Promotion("TENTWEEKS", "tents", 0.08, date(2024, 2, 12), date(2024, 2, 25)))
    pricing.add_promotion(Promotion("FOODBULK", "food", 0.05, date(2024, 1, 1), date(2024, 3, 31), min_qty=12))
    pricing.add_promotion(Promotion("EASTER", "*", 0.03, date(2024, 3, 25), date(2024, 3, 31)))
    ctx = ShopContext(
        rng=rng, products=products, customers=customers, orders=orders, inventory=inventory,
        pricing=pricing, shipping=ShippingCalculator(), loyalty=LoyaltyProgram(),
        invoicer=InvoiceBuilder(), ledger=PaymentLedger(), fraud=FraudScreen(), audit=AuditTrail(),
    )
    print("%s - nightly demo" % SHOP_NAME)
    print("products %d, customers %d, orders %d" % (len(products), len(customers), len(orders)))
    print("first order %s, last order %s" % (orders[0].order_date.isoformat(), orders[-1].order_date.isoformat()))
    return ctx


def demo_master_data(ctx: ShopContext) -> None:
    banner("Master data")
    issues = validate_customer_master(ctx.customers)
    print("customer master issues: %d" % len(issues))
    for cid, text in issues:
        print("  %s %s" % (cid, text))
    rows = []
    by_cat = defaultdict(list)
    for p in ctx.products:
        by_cat[p.category].append(p)
    for cat in sorted(by_cat):
        items = by_cat[cat]
        avg_margin = safe_div(sum(p.margin_pct() for p in items), len(items))
        stock = sum(ctx.inventory.total_on_hand(p.sku) for p in items)
        bulky = sum(1 for p in items if p.is_bulky())
        rows.append([cat, len(items), stock, fmt_pct(avg_margin), bulky,
                     round(sum(p.volume_liters() for p in items), 1)])
    print(render_text_table(["category", "skus", "stock", "avg margin", "bulky", "liters"], rows,
                            title="Catalog overview"))
    print(compute_shipping_matrix(ctx.shipping, [0.5, 2.0, 10.0, 35.0]))
    sample = ctx.inventory.product("CKW-120")
    print("hazmat quote DE vs US: %.2f / %.2f" % (
        ctx.shipping.quote(sample.weight_kg, "DE", "standard", hazmat=True),
        ctx.shipping.quote(sample.weight_kg, "US", "standard", hazmat=True)))
    print("remote surcharge Kiruna: %.2f" % ctx.shipping.quote(3.0, "SE", "standard", postal_code="98131"))
    print("misc: %s | %s | %s | %s" % (fmt_money(1234.565), slugify("  Hello, Wörld! "),
                                       parse_float("1.234,56"), parse_date("31.12.2023")))
    print("business days 2024-03-28 -> 2024-04-03: %d" % business_days_between(date(2024, 3, 28), date(2024, 4, 3)))


def demo_catalog_import(ctx: ShopContext) -> CatalogImporter:
    banner("Catalog import")
    importer = CatalogImporter(ctx.inventory, ctx.pricing, ctx.audit)
    importer.dry_run = True
    dry = importer.import_catalog(sample_supplier_csv(), date(2024, 1, 15), "bergwerk-dryrun.csv")
    print("dry run: status=%s new=%s changed=%s" % (dry["status"], dry["new"], list(dry["changed"].keys())))
    importer.dry_run = False
    importer.deactivate_missing = True
    importer.errors = []
    importer.warnings = []
    summary = importer.import_catalog(sample_supplier_csv(), date(2024, 1, 15), "bergwerk.csv")
    for key, value in summary.items():
        print("  %-16s %s" % (key, value))
    print("errors:")
    for e in importer.errors:
        print("  - " + e)
    print("warnings:")
    for w in importer.warnings:
        print("  - " + w)
    print("empty file -> %s" % importer.import_catalog("   \n", date(2024, 1, 15), "empty.csv")["status"])
    bad = importer.import_catalog("foo|bar|baz\n1|2|3\n", date(2024, 1, 15), "pipes.csv")
    print("bad header -> %s missing=%s" % (bad["status"], bad.get("missing")))
    importer.deactivate_missing = False
    tsv = "item\tdescription\tgroup\tek\tvk\tkg\nLGT-300\tStrobe Light\tlighting\t4.10\t12.90\t0.05\n"
    print("tsv import -> %s" % dict(importer.import_catalog(tsv, date(2024, 1, 16), "trailco.tsv")))
    exported = importer.export_catalog()
    print("export: %d lines, first 4:" % len(exported.splitlines()))
    for line in exported.splitlines()[:4]:
        print("  " + line)
    print("importer stats: %s" % dict(sorted(importer.stats.items())))
    return importer


def demo_order_batches(ctx: ShopContext) -> None:
    banner("Order batches")
    start = date(2024, 1, 1)
    weeks = defaultdict(list)
    for order in ctx.orders:
        weeks[(order.order_date - start).days // 7].append(order)
    for week in sorted(weeks):
        run_date = start + timedelta(days=7 * (week + 1))
        ctx.inventory.today = run_date
        options = {"fraud_limit": 55 if week % 3 == 0 else FRAUD_SCORE_LIMIT,
                   "allow_partial": week % 5 != 4,
                   "verbose": week == 0}
        results, shipments, stats = process_orders(
            weeks[week], ctx.customers, ctx.inventory, ctx.pricing, ctx.shipping, ctx.loyalty,
            ctx.invoicer, ctx.ledger, ctx.fraud, ctx.audit, run_date, options)
        ctx.results.extend(results)
        ctx.shipments.extend(shipments)
        label = "W%02d %s" % (week + 1, run_date.isoformat())
        ctx.batch_stats.append((label, stats))
        print("%s: %3d orders  %s  revenue %s" % (label, stats["processed"],
                                                 " ".join("%s=%d" % kv for kv in stats["by_status"].items()),
                                                 fmt_money(stats["revenue"])))
        for cid, before, after in stats["tier_changes"]:
            print("    tier %s %s -> %s" % (cid, before, after))
        if stats["payment_failures"]:
            print("    payment failures: %s" % ", ".join(oid for oid, _ in stats["payment_failures"]))
        # replenish every fourth week
        if week % 4 == 3:
            pos = build_purchase_orders(ctx.inventory, ctx.results, run_date)
            received = 0
            for po in pos:
                if po["status"] != "ready":
                    continue
                for sku, qty in po["lines"]:
                    ctx.inventory.receive(sku, "HAM", qty, "purchase", po["po"])
                    received += qty
            print("    replenishment: %d POs, %d units received" % (len(pos), received))
    empty = process_orders([], ctx.customers, ctx.inventory, ctx.pricing, ctx.shipping, ctx.loyalty,
                           ctx.invoicer, ctx.ledger, ctx.fraud, ctx.audit, date(2024, 4, 1))
    print("empty batch -> %s" % (empty[2],))
    web_orders, problems = import_orders_csv(sample_webshop_csv(ctx.customers), ctx.customers)
    print("web shop import: %d orders, %d problems" % (len(web_orders), len(problems)))
    for p in problems:
        print("  ! " + p)
    ctx.inventory.today = date(2024, 4, 8)
    results, shipments, stats = process_orders(
        web_orders, ctx.customers, ctx.inventory, ctx.pricing, ctx.shipping, ctx.loyalty,
        ctx.invoicer, ctx.ledger, ctx.fraud, ctx.audit, date(2024, 4, 8), {"strict": True})
    for r in results:
        print("  %s %-16s total %9.2f  %s" % (r.order_id, r.status, r.total, "; ".join(r.messages)))
    ctx.results.extend(results)
    ctx.shipments.extend(shipments)
    ctx.orders.extend(web_orders)
    ctx.batch_stats.append(("web 2024-04-08", stats))
    print(summarize_batches(ctx.batch_stats))
    print("fraud flags: %d" % len(ctx.fraud.flags))
    for oid, score, reasons in ctx.fraud.flags[:10]:
        print("  %s score %d: %s" % (oid, score, ", ".join(reasons)))
    csv_text = export_orders_csv(ctx.orders, ctx.results)
    lines = csv_text.splitlines()
    print("orders export: %d lines" % len(lines))
    for line in lines[:5] + lines[-2:]:
        print("  " + line)


def demo_invoices_and_payments(ctx: ShopContext) -> None:
    banner("Invoices and payments")
    customers = {c.customer_id: c for c in ctx.customers}
    invoices = list(ctx.invoicer.invoices.values())
    print("invoices issued: %d, total %s" % (len(invoices), fmt_money(sum(i.gross_total for i in invoices))))
    shown = 0
    for inv in invoices:
        cust = customers[inv.customer_id]
        if shown == 0 or (shown == 1 and cust.vat_id):
            print(ctx.invoicer.render(inv, cust))
            shown += 1
        if shown >= 2:
            break
    statement = sample_bank_statement(ctx.invoicer, ctx.results)
    recon = reconcile_payments(statement, ctx.invoicer, ctx.ledger, ctx.results)
    print("bank reconciliation:")
    for key in ("matched", "partial", "overpaid", "unmatched"):
        print("  %-9s %s" % (key, recon[key]))
    print("  ignored   %d" % recon["ignored"])
    print("total received: %s" % fmt_money(ctx.ledger.total_received()))
    for today in (date(2024, 4, 15), date(2024, 5, 31)):
        aging = ctx.ledger.aging(invoices, today)
        print("aging %s: %s" % (today.isoformat(), dict(aging)))
    letters = generate_dunning_letters(ctx.invoicer, customers, date(2024, 5, 31))
    print("dunning letters: %d" % len(letters))
    for letter in letters[:3]:
        print(letter)
        print(hr("."))


def demo_returns_and_service(ctx: ShopContext) -> None:
    banner("Returns and customer service")
    ctx.returns = simulate_returns(ctx.results, ctx.orders, ctx.inventory, ctx.invoicer, ctx.rng, date(2024, 4, 10))
    print("returns: %d lines" % len(ctx.returns))
    by_reason = Counter(r["reason"] for r in ctx.returns)
    for reason, n in sorted(by_reason.items()):
        print("  %-18s %d" % (reason, n))
    for ret in ctx.returns[:8]:
        print("  %s %s x%d %-16s %s %8.2f" % (ret["order_id"], ret["sku"], ret["qty"], ret["reason"],
                                             ret["credit_note"], ret["value"]))
    notes = [i for i in ctx.invoicer.invoices.values() if i.is_credit_note]
    if notes:
        note = notes[0]
        print(ctx.invoicer.render(note, {c.customer_id: c for c in ctx.customers}[note.customer_id]))
    service = CustomerService(ctx.customers, ctx.loyalty)
    hits = service.find("berg")
    print("search 'berg': %s" % [c.customer_id for c in hits])
    damaged = [r for r in ctx.returns if r["reason"] == "damaged"]
    late = [r for r in ctx.results if r.status == "backorder"]
    day = date(2024, 4, 10)
    for ret in damaged[:3]:
        order = next(o for o in ctx.orders if o.order_id == ret["order_id"])
        tid = service.open_ticket(order.customer_id, "Damaged item %s in %s" % (ret["sku"], ret["order_id"]), day,
                                  "high")
        granted = service.goodwill_points(order.customer_id, 300, day, tid)
        service.add_note(tid, "granted %d goodwill points" % granted)
        service.close_ticket(tid, "replacement shipped")
    for r in late[:4]:
        tid = service.open_ticket(r.customer_id, "Where is my order %s?" % r.order_id, day - timedelta(days=3))
        service.add_note(tid, "backordered: %s" % r.backordered)
    print("e-mail change ok? %s / %s" % (service.change_email(ctx.customers[13].customer_id, "fixed@mail.example"),
                                         service.change_email(ctx.customers[1].customer_id, "not-an-email")))
    kept = service.merge_customers(ctx.customers[2].customer_id, ctx.customers[30].customer_id)
    print("merged into %s: points %d tier %s" % (kept.customer_id, kept.points, kept.tier))
    for line in service.open_tickets_report(date(2024, 4, 12)):
        print("  " + line)
    print("goodwill: %s" % dict(sorted(service.goodwill_given.items())))
    for cust in (ctx.customers[4], ctx.customers[13], kept):
        print(render_customer_dashboard(cust, ctx.results, ctx.loyalty, ctx.invoicer, ctx.returns))


def demo_inventory_and_purchasing(ctx: ShopContext) -> None:
    banner("Inventory and purchasing")
    print("movements by reason: %s" % ctx.inventory.movement_summary())
    print("valuation: %s" % ctx.inventory.valuation())
    low = ctx.inventory.low_stock()
    print("low stock (%d):" % len(low))
    for sku, have, point in low:
        print("  %-8s %3d <= %3d" % (sku, have, point))
    before = ctx.inventory.snapshot()
    moved = ctx.inventory.transfer("BCK-100", "HAM", "MUC", 5)
    print("transfer BCK-100 HAM->MUC: %d" % moved)
    try:
        ctx.inventory.ship("SAF-200", "MUC", 999, "TEST")
    except InventoryError as exc:
        print("expected error: %s" % exc)
    ctx.inventory.adjust("LGT-100", "MUC", -2, "breakage")
    ctx.inventory.reserve("NAV-100", "HAM", 1)
    for line in snapshot_diff(before, ctx.inventory.snapshot()):
        print("  diff " + line)
    pos = build_purchase_orders(ctx.inventory, ctx.results, date(2024, 4, 10))
    for po in pos:
        print("  %s %-10s %-9s %10.2f eta %s lines %d topped-up %s" % (
            po["po"], po["supplier"], po["status"], po["value"], po["eta"], len(po["lines"]), po["topped_up"]))
    ctx.forecasts = forecast_demand(ctx.orders, ctx.inventory.products, date(2024, 1, 1), 13)
    print("forecasts for %d SKUs; sample:" % len(ctx.forecasts))
    for sku in list(ctx.forecasts)[:8]:
        fc = ctx.forecasts[sku]
        print("  %-8s %-7s %6.2f/wk  next4 %3d  safety %3d  hist %s" % (
            sku, fc["pattern"], fc["forecast_per_week"], fc["horizon_total"], fc["safety_stock"],
            "".join(str(min(9, h)) for h in fc["history"])))
    changes = apply_price_rules(ctx.pricing, ctx.inventory, ctx.forecasts, date(2024, 4, 10))
    print("price rule changes: %d" % len(changes))
    for sku, old, new, reason in changes:
        print("  %-8s %8.2f -> %8.2f  %s" % (sku, old, new, reason))
    for finding in audit_price_history(ctx.pricing, ctx.inventory.products):
        print("  audit: " + finding)
    picks = generate_pick_lists(ctx.shipments[-12:], ctx.orders, ctx.inventory, wave_size=4)
    print("pick lists: %d" % len(picks))
    for page in picks[:2]:
        print(page)
    tours = plan_van_routes(ctx.shipments, ctx.orders, ctx.customers, van_capacity_kg=120.0, max_stops=4)
    print("van tours: %d" % len(tours))
    for tour in tours:
        print("  tour %d: %d stops, %.1f km, %.1f kg %s" % (tour["tour"], len(tour["stops"]), tour["km"],
                                                          tour["kg"], tour["note"]))


def demo_reports(ctx: ShopContext) -> None:
    banner("Monthly reports")
    reporter = ReportGenerator(ctx.products, ctx.customers, ctx.inventory, ctx.orders, ctx.audit)
    invoices = list(ctx.invoicer.invoices.values())
    for month in ("2024-01", "2024-02", "2024-03"):
        print(reporter.generate_monthly_report(month, ctx.results, ctx.shipments, invoices, ctx.returns))
    print(reporter.generate_monthly_report("2023-12", ctx.results, ctx.shipments, invoices, ctx.returns))
    reporter.abort_on_errors = True
    fake = OrderResult("SO-99999", ctx.customers[0].customer_id, "error", order_date=date(2024, 3, 15))
    print(reporter.generate_monthly_report("2024-03", ctx.results + [fake], ctx.shipments, invoices))
    for monday in (date(2024, 1, 8), date(2024, 2, 19), date(2024, 3, 25)):
        print(reporter.weekly_flash(ctx.results, monday))
    print("KPI history: %s" % list(reporter.kpi_history.keys()))
    print("report warnings: %s" % reporter.warnings)
    print(reporter.customer_statement(ctx.customers[4].customer_id, ctx.results, invoices))


def demo_marketing(ctx: ShopContext) -> None:
    banner("Marketing and loyalty")
    ctx.segments = compute_customer_segments(ctx.customers, ctx.results, date(2024, 4, 15))
    for segment, ids in ctx.segments.items():
        print("  %-13s %2d  %s" % (segment, len(ids), " ".join(ids)))
    campaign = run_loyalty_campaign(ctx.customers, ctx.segments, ctx.loyalty, date(2024, 4, 15))
    print("campaign awarded %d customers, budget left %d, skipped %s"
          % (len(campaign["awarded"]), campaign["budget_left"], campaign["skipped"]))
    for mail in campaign["mails"]:
        print("  mail: " + mail)
    top = sorted(ctx.customers, key=lambda c: (-c.points, c.customer_id))[:3]
    for cust in top:
        print("statement %s (%d pts):" % (cust.customer_id, cust.points))
        for line in ctx.loyalty.statement(cust.customer_id)[-5:]:
            print("    " + line)
    expired = ctx.loyalty.expire_points({c.customer_id: c for c in ctx.customers}, date(2025, 4, 30))
    print("points expired at 2025-04-30: %s" % expired)


def demo_month_end(ctx: ShopContext) -> None:
    banner("Month-end close")
    closed = []
    for month in ("2024-01", "2024-02", "2024-03"):
        close = run_month_end_close(month, ctx.inventory, ctx.invoicer, ctx.ledger, ctx.loyalty, ctx.customers,
                                    ctx.results, ctx.shipments, ctx.returns, ctx.audit, ctx.rng,
                                    {"closed_months": closed})
        print(close["report"])
        print("status %s, VAT %s, provision %s" % (close["status"], fmt_money(close.get("vat_payable", 0.0)),
                                                   fmt_money(close.get("provision", 0.0))))
        if close["status"] != "blocked":
            closed.append(month)
    again = run_month_end_close("2024-01", ctx.inventory, ctx.invoicer, ctx.ledger, ctx.loyalty, ctx.customers,
                                ctx.results, ctx.shipments, ctx.returns, ctx.audit, ctx.rng,
                                {"closed_months": closed})
    print("re-close 2024-01 -> %s" % again["status"])
    tight = run_month_end_close("2024-04", ctx.inventory, ctx.invoicer, ctx.ledger, ctx.loyalty, ctx.customers,
                                ctx.results, ctx.shipments, ctx.returns, ctx.audit, ctx.rng,
                                {"approval_budget": 0.0, "count_error_rate": 0.5})
    print(tight["report"])


def demo_audit(ctx: ShopContext) -> None:
    banner("Audit trail")
    entries = ctx.audit.dump()
    print("audit entries: %d" % len(entries))
    print("sections: %s" % dict(sorted(ctx.audit.section_counts.items())))
    for line in ctx.audit.dump(40):
        print("  " + line)
    problems = check_data_integrity(ctx.inventory, ctx.invoicer, ctx.ledger, ctx.results, ctx.shipments)
    print("integrity problems: %d" % len(problems))
    for p in problems[:15]:
        print("  - " + p)
    digest = 0
    for line in entries:
        digest = (digest * 31 + stable_hash(line)) % 1000000007
    print("audit digest: %d" % digest)


def main() -> int:
    ctx = setup_shop(42)
    demo_master_data(ctx)
    demo_catalog_import(ctx)
    demo_order_batches(ctx)
    demo_invoices_and_payments(ctx)
    demo_returns_and_service(ctx)
    demo_inventory_and_purchasing(ctx)
    demo_reports(ctx)
    demo_marketing(ctx)
    demo_month_end(ctx)
    demo_audit(ctx)
    print()
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
