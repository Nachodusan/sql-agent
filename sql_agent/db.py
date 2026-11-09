"""Deterministic sample database.

The seed is fixed, so every run of the evaluation sees byte-identical data and
the golden answers stay valid. Regenerating the database on another machine
produces the same numbers.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "shop.db"

SEED = 20261109
START = date(2026, 1, 1)
DAYS = 300

COUNTRIES = ["AR", "CL", "MX", "ES", "US", "BR"]
CHANNELS = ["web", "mobile", "phone"]
SEVERITIES = ["low", "medium", "high"]
REFUND_REASONS = ["damaged", "wrong item", "late delivery", "changed mind"]

FIRST = ["Ignacio", "Camila", "Lucas", "Valentina", "Mateo", "Sofía", "Benjamín",
         "Isabella", "Thiago", "Emilia", "Joaquín", "Renata", "Bruno", "Olivia",
         "Facundo", "Martina", "Tomás", "Agustina", "Dante", "Paz"]
LAST = ["Duque", "Sandoval", "Rojas", "Figueroa", "Muñoz", "Correa", "Vera",
        "Bustos", "Peralta", "Ibarra", "Salazar", "Cabrera", "Ponce", "Lagos"]

CATALOGUE = [
    ("Mate imperial",            "drinkware",  18_900),
    ("Bombilla de alpaca",       "drinkware",   7_500),
    ("Termo 1L acero",           "drinkware",  32_000),
    ("Yerba orgánica 1kg",       "pantry",      4_200),
    ("Alfajores artesanales x6", "pantry",      6_800),
    ("Café de especialidad 500g", "pantry",     9_900),
    ("Cuaderno cosido A5",       "stationery",  5_400),
    ("Pluma estilográfica",      "stationery", 24_500),
    ("Tinta azul 30ml",          "stationery",  3_900),
    ("Mochila 20L",              "gear",       48_000),
    ("Riñonera impermeable",     "gear",       21_000),
    ("Gorra de lona",            "gear",       11_500),
    ("Taza de cerámica",         "drinkware",   8_900),
    ("Set de posavasos",         "home",        6_200),
    ("Lámpara de escritorio",    "home",       39_900),
]


def build(db_path: Path | str = DEFAULT_DB_PATH, *, overwrite: bool = True) -> Path:
    """Create the sample database and return its path."""
    db_path = Path(db_path)
    if db_path.exists():
        if not overwrite:
            return db_path
        db_path.unlink()

    rng = random.Random(SEED)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    # --- products -------------------------------------------------------
    products: list[tuple[int, str, str, int, int]] = []
    for pid, (name, category, price) in enumerate(CATALOGUE, start=1):
        discontinued = 1 if rng.random() < 0.13 else 0
        products.append((pid, name, category, price, discontinued))
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?)", products)

    # --- customers ------------------------------------------------------
    customers: list[tuple[int, str, str, str, str]] = []
    seen_emails: set[str] = set()
    for cid in range(1, 121):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        base = name.lower().replace(" ", ".").replace("í", "i").replace("á", "a")
        base = base.replace("é", "e").replace("ó", "o").replace("ú", "u").replace("ñ", "n")
        email = f"{base}{cid}@example.com"
        assert email not in seen_emails
        seen_emails.add(email)
        signed_up = START - timedelta(days=rng.randint(0, 540))
        customers.append((cid, name, email, rng.choice(COUNTRIES), signed_up.isoformat()))
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)

    # --- orders and items -----------------------------------------------
    orders: list[tuple[int, int, str, str, str]] = []
    items: list[tuple[int, int, int, int, int]] = []
    refunds: list[tuple[int, int, int, str, str]] = []

    order_id = 0
    item_id = 0
    refund_id = 0

    for cid, *_ in customers:
        # A long tail: most customers order rarely, a few order a lot — and a
        # real customer table always has people who signed up and never bought.
        n_orders = 0 if rng.random() < 0.15 else min(int(rng.paretovariate(1.4)), 14)
        for _ in range(n_orders):
            order_id += 1
            ordered = START + timedelta(days=rng.randint(0, DAYS - 1))
            roll = rng.random()
            status = "cancelled" if roll < 0.09 else ("shipped" if roll < 0.35 else "completed")
            orders.append((order_id, cid, ordered.isoformat(), status, rng.choice(CHANNELS)))

            order_cents = 0
            for _ in range(rng.randint(1, 4)):
                item_id += 1
                pid, _n, _c, list_price, _d = rng.choice(products)
                qty = rng.randint(1, 3)
                # Price actually paid drifts from today's catalogue price.
                drift = rng.choice([0.80, 0.85, 0.90, 1.00, 1.00, 1.05])
                paid = int(round(list_price * drift / 100.0)) * 100
                items.append((item_id, order_id, pid, qty, paid))
                order_cents += qty * paid

            # Refunds only ever attach to orders that were not cancelled.
            if status != "cancelled" and rng.random() < 0.18:
                refund_id += 1
                partial = rng.choice([0.25, 0.5, 1.0])
                refunded = ordered + timedelta(days=rng.randint(1, 30))
                refunds.append((
                    refund_id, order_id, int(order_cents * partial),
                    refunded.isoformat(), rng.choice(REFUND_REASONS),
                ))

    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
    conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)
    conn.executemany("INSERT INTO refunds VALUES (?,?,?,?,?)", refunds)

    # --- support tickets (distractor) ------------------------------------
    tickets: list[tuple[int, int, str, str, int]] = []
    for tid in range(1, 181):
        cid = rng.randint(1, len(customers))
        opened = START + timedelta(days=rng.randint(0, DAYS - 1))
        tickets.append((tid, cid, opened.isoformat(), rng.choice(SEVERITIES),
                        1 if rng.random() < 0.72 else 0))
    conn.executemany("INSERT INTO support_tickets VALUES (?,?,?,?,?)", tickets)

    conn.commit()
    conn.close()
    return db_path


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database read-only, so no agent can write to it."""
    db_path = Path(db_path)
    if not db_path.exists():
        build(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    path = build()
    conn = sqlite3.connect(path)
    counts = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("customers", "products", "orders", "order_items", "refunds", "support_tickets")
    }
    conn.close()
    print(f"built {path}")
    for table, n in counts.items():
        print(f"  {table:<16} {n:>6}")
