"""A deterministic demo database (an online store) and a gold-labelled benchmark for it."""

from __future__ import annotations

import random
import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

CITIES = ["Chennai", "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Coimbatore", "Pune", "Kochi"]
FIRST = ["Aarav", "Diya", "Kavin", "Meera", "Rohan", "Sana", "Vikram", "Anjali", "Arjun", "Priya",
         "Karthik", "Nila", "Ishaan", "Tara", "Dev", "Lakshmi"]  # fmt: skip
LAST = ["Iyer", "Sharma", "Reddy", "Nair", "Patel", "Kumar", "Menon", "Rao", "Singh", "Das"]
PRODUCTS = [
    ("Wireless Earbuds", "Electronics", 2499), ("Mechanical Keyboard", "Electronics", 5499),
    ("USB-C Hub", "Electronics", 1899), ("4K Monitor", "Electronics", 23999),
    ("Running Shoes", "Sports", 3999), ("Yoga Mat", "Sports", 899), ("Cricket Bat", "Sports", 2799),
    ("Filter Coffee Powder", "Grocery", 349), ("Basmati Rice 5kg", "Grocery", 749),
    ("Masala Chai Mix", "Grocery", 279), ("Cotton Kurta", "Fashion", 1299),
    ("Denim Jacket", "Fashion", 2999), ("Silk Saree", "Fashion", 8999),
    ("Desk Lamp", "Home", 1199), ("Pressure Cooker", "Home", 2199), ("Bedsheet Set", "Home", 1599),
]  # fmt: skip
STATUSES = ["delivered"] * 8 + ["shipped", "cancelled", "returned"]

SCHEMA = """
CREATE TABLE customers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, signup_date TEXT NOT NULL
);
CREATE TABLE products (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price REAL NOT NULL
);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  order_date TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE order_items (
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL,
  unit_price REAL NOT NULL
);
"""


def build_demo(path: Path, seed: int = 7) -> Path:
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    start = date(2025, 1, 1)
    customers = [
        (i, f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.choice(CITIES),
         str(start + timedelta(days=rng.randrange(365))))
        for i in range(1, 121)
    ]  # fmt: skip
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?)", customers)
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?)", [(i, *p) for i, p in enumerate(PRODUCTS, 1)]
    )
    item_id = 1
    for order_id in range(1, 601):
        cust = rng.choice(customers)
        signup = date.fromisoformat(cust[3])
        when = signup + timedelta(days=rng.randrange(1, 300))
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?)",
            (order_id, cust[0], str(when), rng.choice(STATUSES)),
        )
        for pid in rng.sample(range(1, len(PRODUCTS) + 1), rng.randint(1, 3)):
            price = PRODUCTS[pid - 1][2] * rng.choice([1.0, 1.0, 0.9, 0.85])
            conn.execute(
                "INSERT INTO order_items VALUES (?,?,?,?,?)",
                (item_id, order_id, pid, rng.randint(1, 3), round(price, 2)),
            )
            item_id += 1
    conn.commit()
    conn.close()
    return path


# (question, gold SQL). Covers filters, joins, aggregation, ranking, dates and subqueries.
BENCHMARK = [
    ("How many customers are there?", "SELECT COUNT(*) FROM customers"),
    (
        "How many customers live in Chennai?",
        "SELECT COUNT(*) FROM customers WHERE city = 'Chennai'",
    ),
    (
        "List all products in the Sports category.",
        "SELECT name FROM products WHERE category = 'Sports'",
    ),
    (
        "What is the most expensive product?",
        "SELECT name FROM products ORDER BY price DESC LIMIT 1",
    ),
    ("How many orders were cancelled?", "SELECT COUNT(*) FROM orders WHERE status = 'cancelled'"),
    ("How many customers are in each city?", "SELECT city, COUNT(*) FROM customers GROUP BY city"),
    (
        "What is the average product price per category?",
        "SELECT category, AVG(price) FROM products GROUP BY category",
    ),
    (
        "Which city has the most customers?",
        "SELECT city FROM customers GROUP BY city ORDER BY COUNT(*) DESC LIMIT 1",
    ),
    (
        "What is the total revenue from delivered orders?",
        "SELECT SUM(oi.quantity * oi.unit_price) FROM order_items oi "
        "JOIN orders o ON o.id = oi.order_id WHERE o.status = 'delivered'",
    ),
    (
        "Which product sold the most units?",
        "SELECT p.name FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "GROUP BY p.id ORDER BY SUM(oi.quantity) DESC LIMIT 1",
    ),
    (
        "How many orders were placed in 2025?",
        "SELECT COUNT(*) FROM orders WHERE strftime('%Y', order_date) = '2025'",
    ),
    (
        "Which 3 customers placed the most orders? Give their names.",
        "SELECT c.name FROM orders o JOIN customers c ON c.id = o.customer_id "
        "GROUP BY c.id ORDER BY COUNT(*) DESC LIMIT 3",
    ),
    (
        "What is the total revenue per product category?",
        "SELECT p.category, SUM(oi.quantity * oi.unit_price) FROM order_items oi "
        "JOIN products p ON p.id = oi.product_id GROUP BY p.category",
    ),
    (
        "How many customers have never placed an order?",
        "SELECT COUNT(*) FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)",
    ),
    (
        "Which products have never been returned?",
        "SELECT name FROM products WHERE id NOT IN (SELECT oi.product_id FROM order_items oi "
        "JOIN orders o ON o.id = oi.order_id WHERE o.status = 'returned')",
    ),
    (
        "How many orders did customers from Bengaluru place?",
        "SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE c.city = 'Bengaluru'",
    ),
]


def _normalize(rows: list[tuple]) -> Counter:
    def cell(v):
        return round(v, 2) if isinstance(v, float) else v

    return Counter(tuple(cell(v) for v in row) for row in rows)


def same_result(predicted: list[tuple], gold: list[tuple]) -> bool:
    """Execution match: same multiset of rows. Extra columns in the prediction are tolerated
    when one of its column projections matches gold exactly (e.g. name + count vs name)."""
    if _normalize(predicted) == _normalize(gold):
        return True
    if not predicted or not gold or len(predicted) != len(gold):
        return False
    width, gold_width = len(predicted[0]), len(gold[0])
    if width <= gold_width:
        return False
    from itertools import combinations

    for cols in combinations(range(width), gold_width):
        if _normalize([tuple(r[i] for i in cols) for r in predicted]) == _normalize(gold):
            return True
    return False
