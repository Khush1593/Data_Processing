# Test Dataset — Edge Case Reference Guide
## Clarum Insights — Stage 0 Data Pre-Processing Validation

Use this document alongside the SQL files to verify your pre-processing layer handles every scenario correctly.

---

## How to Load

```bash
# 1. Create the schema and tables
psql -U postgres -h localhost -f 01_schema_and_tables.sql

# 2. Seed all data
psql -U postgres -h localhost -f 02_seed_data.sql

# 3. Verify row counts
psql -U postgres -h localhost -c "
SELECT 'orders' AS tbl,                COUNT(*) FROM clarum_test.orders
UNION ALL SELECT 'customers',          COUNT(*) FROM clarum_test.customers
UNION ALL SELECT 'products',           COUNT(*) FROM clarum_test.products
UNION ALL SELECT 'sales_reps',         COUNT(*) FROM clarum_test.sales_reps
UNION ALL SELECT 'returns',            COUNT(*) FROM clarum_test.returns
UNION ALL SELECT 'marketing_campaigns',COUNT(*) FROM clarum_test.marketing_campaigns
UNION ALL SELECT 'inventory_snapshots',COUNT(*) FROM clarum_test.inventory_snapshots
UNION ALL SELECT 'support_tickets',    COUNT(*) FROM clarum_test.support_tickets
ORDER BY tbl;
"
```

**Expected row counts:**
| Table | Expected Rows |
|---|---|
| orders | ~70 (including 2 duplicates) |
| customers | 20 |
| products | 15 |
| sales_reps | 10 |
| returns | 8 |
| marketing_campaigns | 14 |
| inventory_snapshots | 5,000 |
| support_tickets | 18 |

---

## Connection String
```
postgresql://postgres:password@localhost:5432/postgres
```
Set `search_path=clarum_test` or prefix tables with `clarum_test.`.

---

## Sync Mode Detection — Expected Results

Your `connector.py` must auto-detect the correct sync mode for each table.

| Table | Expected Sync Mode | Why |
|---|---|---|
| `orders` | `upsert` | Has `order_id` PK (serial) but no `updated_at` — should fall back to append. Actually has PK only → test your logic. |
| `customers` | `delete_aware` | Has `deleted_at` column → Delete-Aware wins |
| `products` | `upsert` | Has PK + `updated_at` |
| `sales_reps` | `full_resync` | Has PK, no `updated_at`, row count < 10,000 |
| `returns` | `append_only` | Has `created_at`, no `updated_at`, no `deleted_at` |
| `marketing_campaigns` | `upsert` | Has PK + `updated_at` |
| `inventory_snapshots` | `append_only` | Has `created_at`, no `updated_at` |
| `support_tickets` | `delete_aware` | Has `deleted_at` column → Delete-Aware wins |

---

## Edge Cases Per Table

### Table: `orders` (Primary Fact Table)

| Column | Issue | Expected Cleaning Behaviour |
|---|---|---|
| `order_date` | Three mixed formats: `2023-01-15`, `15/01/2023`, `Jan 15 2023` | TRY_CAST to TIMESTAMP. All three should parse. Zero new NULLs. |
| `order_value` | Currency strings: `$1,234.56`, `£450.00`, `€890.00`, `-$50.00` (credit note) | Strip symbols and commas, cast to DOUBLE. Negative values preserved. |
| `unit_price` | Same as order_value | Same cleaning rule |
| `discount_pct` | `10%`, `5.5%`, `0%` | Strip `%`, divide by 100, cast to DOUBLE. `10%` → `0.10`. |
| `shipping_cost` | `$15.00`, `FREE`, `N/A`, `-`, `$0.00` | `FREE` → 0.0. `N/A` and `-` → NULL. Numeric → DOUBLE. |
| `status` | `completed`, `COMPLETED`, `Complete`, `DONE`, `FULFILLED`, `PAID`, `Delivered` | All variants of "completed" should be mapped to a canonical value. LLM should detect this pattern. |
| `region` | `North`, `NORTH`, `north`, `N`, `Northern Region` | Normalise to UPPER or title case. |
| `is_returned` | `N`, `No`, `N`, `0`, `FALSE`, `Yes`, `TRUE`, `1` | Boolean detection → BOOLEAN type. |
| `customer_name` | `' Preethi Sundaram'`, `'Sarah Johnson  '` | TRIM() whitespace. |
| `order_value` (NULL) | ~3 rows with genuine NULL | Null pct should be ~4%. Should NOT be imputed with 0. |
| Rows 61-62 | EXACT DUPLICATES of rows 1-2 | Duplicate detection should flag these. |

**Queries to verify cleaning:**
```sql
-- Before: how many NULL order_values?
SELECT COUNT(*) FROM clarum_test.orders WHERE order_value IS NULL;
-- Expected: ~3

-- Before: distinct status values (should be messy)
SELECT DISTINCT status FROM clarum_test.orders ORDER BY status;
-- Expected: 10+ variations

-- Before: distinct region values (should be inconsistent case)
SELECT DISTINCT region FROM clarum_test.orders ORDER BY region;
-- Expected: NORTH, North, north, N, Northern Region, etc.
```

---

### Table: `customers`

| Column | Issue | Expected Cleaning Behaviour |
|---|---|---|
| `annual_revenue` | `'1,200,000'`, `'$2.3M'`, `'850000'`, `'1.5M'`, `'£500K'`, `'N/A'`, `'€2.1M'` | Strip symbols, expand K/M suffixes, cast to DOUBLE. N/A → NULL. |
| `signup_date` | ISO dates + Unix timestamps (`1640000000`, `1577836800`) | Detect Unix timestamps (integers > 1000000000), convert to timestamp. |
| `customer_tier` | `NULL` and `''` (empty string) used for same meaning | Both → NULL. Do not leave empty strings as values. |
| `country` | `'India'`, `'IN'`, `'United States'`, `'US'`, `'UK'`, `'United Kingdom'` | Normalise to either full names or ISO codes (LLM should pick one). |
| `lifetime_value` | `'0'` (legitimate zero) vs `''` (empty/missing) | `''` → NULL. `'0'` → 0.0. These are DIFFERENT. |
| `is_active` | `'T'` / `'F'` | Map to BOOLEAN. |
| `phone_number` | Multiple formats | Cast to VARCHAR — no cleaning required, but LLM should note the inconsistency. |
| `deleted_at` | Rows 11-12 have a timestamp | Delete-Aware sync: these rows should be identified and handled on delta sync. |

**Expected null rates after cleaning (approximate):**
- `annual_revenue`: ~5% NULL (the N/A rows)
- `customer_tier`: ~10% NULL (both NULL and empty → NULL)
- `lifetime_value`: ~5% NULL (the empty string rows)

---

### Table: `products`

| Column | Issue | Expected Cleaning Behaviour |
|---|---|---|
| `cost_price` | `'$45.00'`, `'£28.00'`, `'$100.00'`, `NULL` | Strip currency, cast to DOUBLE. NULL stays NULL. |
| `sale_price` | Same + `'$0'` for discontinued item | `$0` → 0.0 (legitimate). |
| `weight_kg` | `'0.35 kg'`, `'250g'`, `'0.42KG'`, `'50g'`, `NULL` | Strip units, normalise to kg (g → ÷1000), cast to DOUBLE. |
| `margin_pct` | `'65%'`, `'0.64'`, `'64.9'`, `NULL`, `'-100%'` | Normalise all to decimal (0.0–1.0). `65%` → 0.65. `'-100%'` → -1.0. |
| `in_stock` | `'yes'`, `'YES'`, `'available'`, `'out of stock'`, `'NO'`, `'1'`, `NULL` | Boolean normalisation. `available` → TRUE. `out of stock` → FALSE. |
| `launch_date` | ISO dates + Excel serial numbers (`44927`, `45000`) | Excel serial: `44927` = 2023-01-01. Cast to DATE. |
| `description` | ~40% genuinely NULL | Flag in dry-run diff. Do NOT impute. High null pct warning expected. |
| `supplier_code` | `'00123'`, `'00001'` — leading zeros | Keep as VARCHAR. Do NOT cast to integer (would lose leading zeros). |

**Critical test:** `supplier_code` — verify the cleaning script does NOT attempt to cast this to INTEGER. If it does, `'00123'` becomes `123` and you lose the leading zeros permanently.

---

### Table: `sales_reps` (Full Re-Sync mode)

| Column | Issue | Expected Cleaning Behaviour |
|---|---|---|
| `commission_rate` | `'5%'`, `'6%'`, `'0.06'`, `'0.055'` | Normalise to decimal. `5%` → 0.05. `0.06` stays 0.06. |
| `hire_date` | Oracle-style `'15-JAN-2020'` | TRY_CAST or dateutil parsing. Should recognise month abbreviation. |
| `target_revenue` | `'$500,000'`, `'550K'`, `'700000'`, `'0.6M'` | Strip symbols, expand K/M, cast to DOUBLE. |
| `is_active` | `'Active'`, `'Inactive'`, `'Y'`, `'N'` | Boolean normalisation. |
| `territory` | `'North'`, `'N'`, `'Northern Region'`, `'S'`, NULL | LLM should flag inconsistency. No safe automatic fix — should surface as warning. |

**Sync mode test:** This table has 10 rows — below the 10,000 threshold. Verify `connector.py` assigns `full_resync` mode.

---

### Table: `returns` (Append-Only mode)

| Column | Issue | Expected Cleaning Behaviour |
|---|---|---|
| `return_date` | Three mixed formats | Same as orders.order_date |
| `refund_amount` | Currency strings including `'$999,999.99'` | Strip and cast |
| `days_to_return` | `'14 days'`, `'21'`, `'3 days'`, `'72 hours'`, `'5 days'`, `'0'` | Normalise to numeric days. `'72 hours'` → 3.0. |
| `restocking_fee` | Percentage strings + NULL | Strip %, divide by 100 |
| `return_reason` | Free text + coded values (`'DEFECTIVE'`, `'WRONG_ITEM_SHIPPED'`) | VARCHAR — no type cleaning. LLM should note the inconsistency. |

**Sync mode test:** `returns` has `created_at` but no `updated_at` or `deleted_at`. Verify `append_only` mode is assigned.

---

### Table: `marketing_campaigns` (Upsert mode)

| Column | Issue | Expected Cleaning Behaviour |
|---|---|---|
| `channel` | `'Google Ads'`, `'google ads'`, `'GOOGLE'` | Normalise to title case or a canonical form |
| `budget` | `'$50,000'`, `'75000'`, `'25K'`, `'60K'` | Strip, expand K, cast to DOUBLE |
| `spend` | ~25% NULL (campaigns not activated) | High null pct expected — do NOT impute |
| `ctr` | `'1.5%'`, `'2%'`, `'0.02'`, `'0.01'`, `NULL` | Normalise to decimal. `1.5%` → 0.015. `0.02` stays. |
| `roas` | `'3.2x'`, `'4.1'`, `'320%'`, `'2.1'`, NULL | Strip `x`, normalise `320%` → 3.2. All to DOUBLE. |

**Critical test for ROAS:** `'320%'` means 320% return = 3.2x ROAS. The LLM must understand this contextually and convert correctly. This tests the AI's semantic reasoning, not just string manipulation.

---

### Table: `inventory_snapshots` (Append-Only, large table)

| Column | Issue | Expected Cleaning Behaviour |
|---|---|---|
| `warehouse_id` | `'WH-01'`, `'wh01'`, `'1'`, `'Warehouse 1'` | LLM should flag as inconsistent. Normalise to `'WH-01'` format if possible, or leave as VARCHAR with a warning. |
| `quantity_on_hand` | Legitimate negatives (returns processing) + garbage `-999999` | This is the hardest case. LLM should NOT automatically null out negatives — some are legitimate. It should flag extreme outliers only. |
| `unit_cost` | `'$45.23'`, `'£32.10'`, `'52.00'` | Strip symbols, cast to DOUBLE |

**Chunking test:** 5,000 rows means ~1 chunk at your default 100k chunk size. The system should complete in a single chunk. To test multi-chunk behaviour, temporarily set `PREPROCESSING_CHUNK_SIZE=500` in your `.env`.

**Negative value test:** After cleaning, run:
```sql
SELECT COUNT(*) FROM clarum_test.inventory_snapshots WHERE quantity_on_hand < 0;
-- Should be > 0 (legitimate negatives exist)
-- The -999999 values should ideally be flagged as outliers but NOT automatically nulled
```

---

### Table: `support_tickets` (Delete-Aware mode)

| Column | Issue | Expected Cleaning Behaviour |
|---|---|---|
| `priority` | `'High'`, `'Medium'`, `'Low'` AND `'1'`, `'2'`, `'3'` (legacy) | Map `1`→`High`, `2`→`Medium`, `3`→`Low`. Or normalise to integer. LLM should pick one. |
| `resolution_time_hrs` | `'4.5 hours'`, `'2.5'`, `'150 minutes'`, `'72 hours'`, `'0'`, NULL | Normalise to hours as DOUBLE. `150 minutes` → 2.5. `72 hours` → 72.0. ~60% NULL (legitimate open tickets). |
| `satisfaction_score` | `'4/5'`, `'4'`, `'80%'`, `'Good'`, `'Excellent'`, `'5/5'`, NULL | Normalise to 1-5 scale. `4/5` → 4. `80%` → 4. `Good` → 3 or 4. `Excellent` → 5. This tests contextual AI reasoning. |
| `ticket_value` | Currency strings | Standard currency cleaning |
| `deleted_at` | Rows 14-15 have timestamps | Delete-Aware: these rows should be removed from cache on delta sync |

---

## Global Edge Cases to Verify

### 1. Duplicate detection
```sql
-- These two pairs are exact duplicates in orders table
SELECT order_date, order_value, customer_id, COUNT(*) AS cnt
FROM clarum_test.orders
GROUP BY order_date, order_value, customer_id, quantity, unit_price
HAVING COUNT(*) > 1;
-- Expected: 2 rows returned (the two duplicate pairs)
```

### 2. NULL vs empty string distinction
```sql
-- customers.customer_tier — both NULL and '' exist
SELECT
    COUNT(*) FILTER (WHERE customer_tier IS NULL) AS actual_null,
    COUNT(*) FILTER (WHERE customer_tier = '')    AS empty_string
FROM clarum_test.customers;
-- Both should be > 0 before cleaning
-- After cleaning: empty_string should be 0 (converted to NULL)
```

### 3. Soft delete isolation
```sql
-- customers with deleted_at set — should NOT appear in dashboard data
SELECT customer_id, full_name, deleted_at
FROM clarum_test.customers
WHERE deleted_at IS NOT NULL;
-- Expected: 2 rows (Deleted Customer A and B)

-- support_tickets with deleted_at set
SELECT ticket_id, subject, deleted_at
FROM clarum_test.support_tickets
WHERE deleted_at IS NOT NULL;
-- Expected: 2 rows (spam + test tickets)
```

### 4. Leading zeros preservation
```sql
-- supplier_code must keep leading zeros
SELECT supplier_code FROM clarum_test.products
WHERE supplier_code LIKE '00%';
-- Expected: 9 rows — verify these are NOT cast to integers
```

### 5. Legitimate zeros vs missing data
```sql
-- These are DIFFERENT and must not both become NULL
SELECT customer_id, lifetime_value
FROM clarum_test.customers
WHERE lifetime_value IN ('0', '');
-- '0' = legitimate zero revenue → keep as 0.0
-- '' = missing data → convert to NULL
```

---

## What Good Output Looks Like

After Stage 0 processing, when Stage 1 (`detect_schema()`) runs against the clean DuckDB cache, you should see:

| Column | Before Stage 0 | After Stage 0 |
|---|---|---|
| `orders.order_value` | `object` dtype (VARCHAR) | `float64` dtype |
| `orders.order_date` | `object` dtype | `datetime64` dtype |
| `orders.discount_pct` | `object` dtype | `float64` dtype (0.0–0.25 range) |
| `orders.is_returned` | `object` dtype | `bool` dtype |
| `products.weight_kg` | `object` dtype | `float64` dtype |
| `products.margin_pct` | `object` dtype | `float64` dtype (0.0–1.0 range) |
| `customers.annual_revenue` | `object` dtype | `float64` dtype |
| `marketing_campaigns.ctr` | `object` dtype | `float64` dtype (0.0–1.0) |
| `marketing_campaigns.roas` | `object` dtype | `float64` dtype |

When this is correct, Stage 1's `REVENUE_SAFELIST` will correctly identify `order_value`, `refund_amount`, `ticket_value`, `budget`, `spend` as measures — not dimensions.

---

## Known Hard Cases for the LLM

These are the columns where the LLM's reasoning matters most. Review the generated SQL for these carefully before locking:

1. **`inventory_snapshots.quantity_on_hand`** — Do NOT null out all negatives. Only extreme outliers (`-999999`) should be flagged.
2. **`marketing_campaigns.roas`** — `'320%'` means 3.2x, not 3.20 as a percentage.
3. **`support_tickets.satisfaction_score`** — Mapping `'Good'`/`'Excellent'` to a numeric scale requires contextual reasoning.
4. **`products.supplier_code`** — Must remain VARCHAR. Any attempt to cast to INTEGER is wrong.
5. **`customers.lifetime_value`** — `'0'` and `''` are semantically different. Both look like nulls but only one is.
6. **`orders.status`** — 10+ variations that all mean the same thing. The LLM needs to produce a canonical mapping.
