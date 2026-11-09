from __future__ import annotations

from sql_agent.tools import Tools


def test_list_tables_reports_every_table_with_counts(shop_db):
    out = Tools(shop_db).list_tables()
    assert out["ok"]
    names = {t["table"] for t in out["tables"]}
    assert names == {
        "customers", "products", "orders", "order_items", "refunds", "support_tickets",
    }
    assert all(t["rows"] > 0 for t in out["tables"])


def test_describe_table_includes_sample_rows(shop_db):
    out = Tools(shop_db).describe_table("order_items")
    assert out["ok"]
    cols = {c["name"] for c in out["columns"]}
    assert {"order_id", "product_id", "quantity", "unit_price_cents"} <= cols
    # The sample rows are what reveal that money is in cents.
    assert len(out["sample_rows"]) == 3
    assert out["foreign_keys"]


def test_describe_unknown_table_lists_the_real_ones(shop_db):
    out = Tools(shop_db).describe_table("ordrs")
    assert not out["ok"]
    assert "no such table" in out["error"]
    assert "orders" in out["hint"]


def test_orders_really_has_no_total_column(shop_db):
    """The trap the whole project is built around. If this ever passes a
    `total` column, the recovery demo is staged rather than real."""
    cols = {c["name"] for c in Tools(shop_db).describe_table("orders")["columns"]}
    assert "total" not in cols
    assert "total_cents" not in cols


def test_run_sql_returns_the_database_error_verbatim(shop_db):
    out = Tools(shop_db).run_sql("SELECT total FROM orders")
    assert not out["ok"]
    assert "no such column" in out["error"]
    # The agent is expected to read this string, so it must survive intact.
    assert "total" in out["error"]


def test_run_sql_happy_path(shop_db):
    out = Tools(shop_db).run_sql("SELECT count(*) AS n FROM orders")
    assert out["ok"]
    assert out["rows"][0]["n"] > 0


def test_run_sql_caps_rows(shop_db):
    out = Tools(shop_db).run_sql("SELECT * FROM order_items")
    assert out["ok"]
    assert out["row_count"] == 50
    assert out["truncated"] is True


def test_writes_are_refused(shop_db):
    tools = Tools(shop_db)
    for query in (
        "DELETE FROM orders",
        "UPDATE orders SET status='x'",
        "DROP TABLE orders",
        "INSERT INTO orders VALUES (1,1,'2026-01-01','completed','web')",
        "PRAGMA table_info(orders)",
    ):
        out = tools.run_sql(query)
        assert not out["ok"], query


def test_database_is_opened_read_only(shop_db):
    """Even if the guard above were bypassed, the connection itself cannot write."""
    import sqlite3
    from sql_agent import db
    conn = db.connect(shop_db)
    try:
        conn.execute("DELETE FROM orders")
    except sqlite3.OperationalError as exc:
        assert "readonly" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("the connection accepted a write")
    finally:
        conn.close()


def test_unknown_tool_name_is_an_error_not_a_crash(shop_db):
    out = Tools(shop_db).call("drop_everything", {})
    assert not out["ok"]
    assert "unknown tool" in out["error"]
