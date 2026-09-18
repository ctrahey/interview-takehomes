-- retail schema: multi-hop joins, aggregates, date filtering
-- SQLite dialect.

CREATE TABLE customers (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT,                 -- nullable: some customers opted out of email capture
    city          TEXT NOT NULL,
    state         TEXT NOT NULL,
    signup_date   TEXT NOT NULL         -- ISO date 'YYYY-MM-DD'
);

CREATE TABLE products (
    id                  INTEGER PRIMARY KEY,
    sku                 TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    category            TEXT NOT NULL,
    unit_price          REAL NOT NULL,
    discontinued_date   TEXT            -- nullable: NULL means still active
);

CREATE TABLE orders (
    id            INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    order_date    TEXT NOT NULL,        -- ISO date
    status        TEXT NOT NULL CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled')),
    shipped_date  TEXT                  -- nullable: NULL means not yet shipped (pending/cancelled)
);

CREATE TABLE order_items (
    id            INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(id),
    product_id    INTEGER NOT NULL REFERENCES products(id),
    quantity      INTEGER NOT NULL,
    unit_price    REAL NOT NULL         -- price at time of sale, copied from products.unit_price
);

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);
