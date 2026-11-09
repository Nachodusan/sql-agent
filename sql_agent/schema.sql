-- Deliberately shaped like a real operational database, not a tutorial one.
--
-- The traps are the point. An agent that guesses a schema instead of reading it
-- will write SQL that either errors or returns a confidently wrong number:
--
--   1. `orders` has no total column. Order value lives in `order_items`.
--   2. Money is INTEGER cents everywhere. Averaging without /100.0 is wrong.
--   3. `order_items.unit_price_cents` is the price paid at the time of the order.
--      `products.list_price_cents` is today's catalogue price. They differ.
--   4. `orders.status` includes 'cancelled'. Those are not revenue.
--   5. `refunds` exists. Net spend has to subtract them.
--   6. `support_tickets` is a distractor: plausible, rarely relevant.

CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    email       TEXT    NOT NULL UNIQUE,
    country     TEXT    NOT NULL,
    signed_up_at TEXT   NOT NULL          -- ISO-8601 date
);

CREATE TABLE products (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL,
    category         TEXT    NOT NULL,
    list_price_cents INTEGER NOT NULL,    -- today's catalogue price
    discontinued     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ordered_at  TEXT    NOT NULL,         -- ISO-8601 date
    status      TEXT    NOT NULL,         -- 'completed' | 'shipped' | 'cancelled'
    channel     TEXT    NOT NULL          -- 'web' | 'mobile' | 'phone'
    -- No total column. On purpose.
);

CREATE TABLE order_items (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES orders(id),
    product_id       INTEGER NOT NULL REFERENCES products(id),
    quantity         INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL     -- price paid then, not products.list_price_cents
);

CREATE TABLE refunds (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL REFERENCES orders(id),
    amount_cents INTEGER NOT NULL,
    refunded_at  TEXT    NOT NULL,
    reason       TEXT    NOT NULL
);

CREATE TABLE support_tickets (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    opened_at   TEXT    NOT NULL,
    severity    TEXT    NOT NULL,         -- 'low' | 'medium' | 'high'
    resolved    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_date     ON orders(ordered_at);
CREATE INDEX idx_items_order     ON order_items(order_id);
CREATE INDEX idx_refunds_order   ON refunds(order_id);
