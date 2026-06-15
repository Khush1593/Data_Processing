-- ============================================================
-- Clarum Insights — Complex Test Dataset for PostgreSQL
-- ============================================================
-- Purpose: Test the hybrid data pre-processing layer (Stage 0)
-- against real-world messy data scenarios.
--
-- Schema: clarum_test
-- Tables: 8 tables with deliberate data quality issues
--
-- HOW TO LOAD:
--   psql -U postgres -h localhost -f 01_schema_and_tables.sql
--   psql -U postgres -h localhost -f 02_seed_data.sql
--
-- Connection string for your .env:
--   DB_URI=postgresql://postgres:password@localhost:5432/postgres
-- ============================================================

-- Drop and recreate schema for clean slate
DROP SCHEMA IF EXISTS clarum_test CASCADE;
CREATE SCHEMA clarum_test;
SET search_path TO clarum_test;

-- ============================================================
-- TABLE 1: orders
-- Primary fact table. Simulates an e-commerce orders export.
--
-- DELIBERATE ISSUES:
--   - order_value stored as VARCHAR with currency symbols ($, £, €)
--   - order_date mixed formats ('2023-01-15', '15/01/2023', 'Jan 15 2023')
--   - status column has legacy values + current values (same meaning)
--   - customer_name has leading/trailing whitespace
--   - discount_pct stored as '12%', '5.5%', '0%'
--   - shipping_cost has 'N/A', '-', 'FREE' as null variants
--   - region has inconsistent casing ('NORTH', 'North', 'north')
--   - is_returned stored as 'Y','N','Yes','No','1','0','TRUE','FALSE'
--   - ~15% of rows have NULL order_value (genuine missing data)
--   - ~3% of rows are exact duplicates (ETL re-run artifacts)
-- ============================================================
CREATE TABLE orders (
    order_id        SERIAL PRIMARY KEY,
    customer_id     INTEGER,
    order_date      VARCHAR(50),        -- ISSUE: mixed date formats
    order_value     VARCHAR(30),        -- ISSUE: currency strings "$1,234.56"
    discount_pct    VARCHAR(10),        -- ISSUE: percentage strings "12%"
    shipping_cost   VARCHAR(20),        -- ISSUE: null variants "N/A", "FREE", "-"
    status          VARCHAR(30),        -- ISSUE: legacy + current values
    region          VARCHAR(30),        -- ISSUE: inconsistent casing
    product_category VARCHAR(50),
    quantity        INTEGER,
    unit_price      VARCHAR(20),        -- ISSUE: currency strings
    is_returned     VARCHAR(10),        -- ISSUE: boolean variants Y/N/Yes/No/1/0
    customer_name   VARCHAR(100),       -- ISSUE: leading/trailing whitespace
    sales_rep_id    INTEGER,
    notes           TEXT                -- ISSUE: free text, sometimes JSON fragments
);

-- ============================================================
-- TABLE 2: customers
-- Customer master table. Simulates CRM export.
--
-- DELIBERATE ISSUES:
--   - email has invalid formats mixed with valid ones
--   - phone_number stored inconsistently (+91-9876543210, 9876543210, (987) 654-3210)
--   - annual_revenue stored as VARCHAR ('1.2M', '$500K', '2,300,000')
--   - signup_date has Unix timestamps mixed with ISO dates
--   - customer_tier has NULL and empty string used interchangeably
--   - country has full names and ISO codes mixed ('India', 'IN', 'United States', 'US')
--   - lifetime_value has some rows as '0' (legitimate) and some as '' (missing)
--   - deleted_at present (soft delete pattern — triggers Delete-Aware sync mode)
-- ============================================================
CREATE TABLE customers (
    customer_id     SERIAL PRIMARY KEY,
    full_name       VARCHAR(100),
    email           VARCHAR(150),       -- ISSUE: mixed valid/invalid
    phone_number    VARCHAR(30),        -- ISSUE: inconsistent formats
    annual_revenue  VARCHAR(30),        -- ISSUE: '1.2M', '$500K', '2,300,000'
    signup_date     VARCHAR(50),        -- ISSUE: Unix timestamps + ISO dates mixed
    customer_tier   VARCHAR(20),        -- ISSUE: NULL vs empty string
    country         VARCHAR(50),        -- ISSUE: full names vs ISO codes
    city            VARCHAR(50),
    lifetime_value  VARCHAR(20),        -- ISSUE: '' used as NULL
    is_active       VARCHAR(5),         -- ISSUE: boolean as 'T'/'F'
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    deleted_at      TIMESTAMP           -- soft delete — triggers Delete-Aware mode
);

-- ============================================================
-- TABLE 3: products
-- Product catalogue. Simulates ERP export.
--
-- DELIBERATE ISSUES:
--   - cost_price and sale_price stored as VARCHAR with currency symbols
--   - weight_kg stored as '1.5 kg', '500g', '2.3KG' (unit embedded)
--   - margin_pct stored as '45%', '0.45', '45' (inconsistent representation)
--   - in_stock stored as 'yes','no','YES','NO','1','0','available','out of stock'
--   - launch_date has Excel serial numbers mixed with ISO dates (e.g. 44927)
--   - ~40% of description column is NULL (genuinely missing — should be flagged)
--   - supplier_code has leading zeros that must be preserved ('00123')
-- ============================================================
CREATE TABLE products (
    product_id      SERIAL PRIMARY KEY,
    product_name    VARCHAR(200),
    category        VARCHAR(50),
    subcategory     VARCHAR(50),
    cost_price      VARCHAR(20),        -- ISSUE: '$45.00', '45', '£32.50'
    sale_price      VARCHAR(20),        -- ISSUE: currency strings
    weight_kg       VARCHAR(20),        -- ISSUE: unit embedded '1.5 kg', '500g'
    margin_pct      VARCHAR(10),        -- ISSUE: '45%', '0.45', '45'
    in_stock        VARCHAR(20),        -- ISSUE: boolean variants
    launch_date     VARCHAR(30),        -- ISSUE: Excel serial numbers + ISO dates
    description     TEXT,              -- ISSUE: ~40% genuinely NULL
    supplier_code   VARCHAR(20),        -- ISSUE: leading zeros '00123'
    sku             VARCHAR(50),
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- TABLE 4: sales_reps
-- Small dimension table (<10k rows) — triggers Full Re-Sync mode.
--
-- DELIBERATE ISSUES:
--   - commission_rate as '5%', '7.5%', '0.05' (mixed)
--   - territory stored inconsistently (abbreviations + full names)
--   - hire_date has 'DD-MON-YYYY' Oracle-style format ('15-JAN-2020')
--   - target_revenue as '$500,000' or '500000' or '500K'
--   - is_active as 'Active'/'Inactive'/'Y'/'N'
-- ============================================================
CREATE TABLE sales_reps (
    rep_id          SERIAL PRIMARY KEY,
    rep_name        VARCHAR(100),
    email           VARCHAR(150),
    territory       VARCHAR(50),        -- ISSUE: 'North' vs 'N' vs 'Northern Region'
    commission_rate VARCHAR(10),        -- ISSUE: '5%' vs '0.05'
    hire_date       VARCHAR(30),        -- ISSUE: Oracle-style '15-JAN-2020'
    target_revenue  VARCHAR(20),        -- ISSUE: '$500,000' vs '500K'
    is_active       VARCHAR(15),        -- ISSUE: 'Active'/'Inactive'/'Y'/'N'
    manager_id      INTEGER,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- TABLE 5: returns
-- Returns/refunds table. Append-only (no updated_at, has created_at).
-- Triggers Append-Only sync mode.
--
-- DELIBERATE ISSUES:
--   - refund_amount as VARCHAR with currency
--   - return_reason has free-text mixed with coded values
--   - days_to_return stored as '3 days', '3', '72 hours' (unit embedded)
--   - restocking_fee as percentage string or NULL
-- ============================================================
CREATE TABLE returns (
    return_id       SERIAL PRIMARY KEY,
    order_id        INTEGER,
    customer_id     INTEGER,
    return_date     VARCHAR(30),        -- ISSUE: mixed date formats
    refund_amount   VARCHAR(20),        -- ISSUE: currency strings
    return_reason   VARCHAR(200),       -- ISSUE: free text + coded values mixed
    days_to_return  VARCHAR(20),        -- ISSUE: '3 days', '72 hours', '3'
    restocking_fee  VARCHAR(10),        -- ISSUE: percentage string, sometimes NULL
    approved_by     INTEGER,
    created_at      TIMESTAMP DEFAULT NOW()   -- no updated_at → Append-Only mode
);

-- ============================================================
-- TABLE 6: marketing_campaigns
-- Campaign performance data. Has updated_at → Upsert mode.
--
-- DELIBERATE ISSUES:
--   - budget stored as '$10,000', '10000', '10K'
--   - ctr (click-through rate) as '2.3%', '0.023', '2.3'
--   - roas (return on ad spend) as '3.2x', '3.2', '320%'
--   - start_date and end_date are NULL for draft campaigns (legitimate)
--   - channel has inconsistent capitalisation ('Google Ads', 'google ads', 'GOOGLE')
--   - ~25% of rows have NULL spend (campaigns not yet activated)
-- ============================================================
CREATE TABLE marketing_campaigns (
    campaign_id     SERIAL PRIMARY KEY,
    campaign_name   VARCHAR(200),
    channel         VARCHAR(50),        -- ISSUE: capitalisation inconsistency
    budget          VARCHAR(20),        -- ISSUE: '$10,000', '10K', '10000'
    spend           VARCHAR(20),        -- ISSUE: ~25% NULL (not yet activated)
    impressions     INTEGER,
    clicks          INTEGER,
    conversions     INTEGER,
    ctr             VARCHAR(10),        -- ISSUE: '2.3%' vs '0.023' vs '2.3'
    roas            VARCHAR(10),        -- ISSUE: '3.2x', '3.2', '320%'
    start_date      DATE,
    end_date        DATE,
    is_active       BOOLEAN,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()   -- has updated_at → Upsert mode
);

-- ============================================================
-- TABLE 7: inventory_snapshots
-- Daily warehouse inventory. Large table (>1M rows target).
-- Has created_at, no updated_at → Append-Only mode.
--
-- DELIBERATE ISSUES:
--   - quantity_on_hand has negative values (legitimate returns processing)
--     mixed with clearly erroneous negatives (-999999)
--   - unit_cost stored as VARCHAR with mixed currency symbols
--   - warehouse_id has inconsistent formatting ('WH-01', 'wh01', '1', 'Warehouse 1')
--   - snapshot_date is stored without timezone, causing potential UTC offset issues
-- ============================================================
CREATE TABLE inventory_snapshots (
    snapshot_id     SERIAL PRIMARY KEY,
    product_id      INTEGER,
    warehouse_id    VARCHAR(20),        -- ISSUE: 'WH-01', 'wh01', '1', 'Warehouse 1'
    snapshot_date   VARCHAR(30),        -- ISSUE: no timezone info
    quantity_on_hand INTEGER,           -- ISSUE: legitimate negatives + garbage negatives
    unit_cost       VARCHAR(20),        -- ISSUE: currency strings
    reorder_level   INTEGER,
    created_at      TIMESTAMP DEFAULT NOW()   -- no updated_at → Append-Only
);

-- ============================================================
-- TABLE 8: support_tickets
-- Customer support data. Has both created_at and updated_at.
-- Has deleted_at → Delete-Aware sync mode.
--
-- DELIBERATE ISSUES:
--   - resolution_time_hrs stored as '2.5 hours', '150 minutes', '2.5', NULL
--   - satisfaction_score as '4/5', '4', '80%', 'Good' (wildly inconsistent)
--   - priority stored as '1','2','3' AND 'High','Medium','Low' (legacy + current)
--   - ticket_value (estimated revenue at risk) as currency string
--   - ~60% of resolution_time_hrs is NULL (open tickets — genuinely missing)
-- ============================================================
CREATE TABLE support_tickets (
    ticket_id       SERIAL PRIMARY KEY,
    customer_id     INTEGER,
    order_id        INTEGER,
    subject         VARCHAR(300),
    priority        VARCHAR(20),        -- ISSUE: '1'/'2'/'3' and 'High'/'Medium'/'Low'
    status          VARCHAR(30),
    resolution_time_hrs VARCHAR(30),    -- ISSUE: '2.5 hours', '150 minutes', NULL
    satisfaction_score  VARCHAR(20),    -- ISSUE: '4/5', '4', '80%', 'Good'
    ticket_value    VARCHAR(20),        -- ISSUE: currency string
    assigned_to     INTEGER,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    deleted_at      TIMESTAMP           -- soft delete → Delete-Aware mode
);

-- Indexes for realistic query performance
CREATE INDEX idx_orders_customer    ON orders(customer_id);
CREATE INDEX idx_orders_date        ON orders(order_date);
CREATE INDEX idx_orders_rep         ON orders(sales_rep_id);
CREATE INDEX idx_returns_order      ON returns(order_id);
CREATE INDEX idx_inventory_product  ON inventory_snapshots(product_id);
CREATE INDEX idx_inventory_date     ON inventory_snapshots(snapshot_date);
CREATE INDEX idx_tickets_customer   ON support_tickets(customer_id);
CREATE INDEX idx_tickets_order      ON support_tickets(order_id);
CREATE INDEX idx_campaigns_channel  ON marketing_campaigns(channel);
