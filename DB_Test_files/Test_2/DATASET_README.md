# Clarum Insights — Stage 0 / Stage 0.5 Deep Validation Dataset

**File:** `clarum_test_dataset.sql`  
**Target DB:** PostgreSQL 14+  
**Total rows:** ~500,500 across 8 tables

---

## Table Summary

| Table | Rows | Role |
|---|---|---|
| `customers` | 50,000 | CRM — anchor table |
| `orders` | 200,000 | E-commerce — largest, dominates weighted majority votes |
| `payments` | 180,000 | Payment gateway — second largest |
| `employees` | 3,000 | HR — deliberate minority/outlier |
| `support_tickets` | 25,000 | Helpdesk — flagship cross-table ID test |
| `products` | 5,000 | Product catalog |
| `loyalty_accounts` | ~39,500 | Loyalty program (bolted-on later) |
| `payroll_records` | 3,000 | Payroll (separate from HR) |

---

## Deliberate Dirty Patterns (by column)

### customers
| Column | Pattern | Expected Classification |
|---|---|---|
| `phone_number` | `+CC-NNNNNNNNNN` (intl 12-digit) | PII + `format_signature="intl_12d"` |
| `signup_date` | Consistently `DD-MM-YYYY` | `mixed_date_format` → `date_format="%d-%m-%Y"` → `native_timestamp` |
| `account_status` | Mixed casing + trailing spaces (`"INACTIVE"`, `"inactive "`, `"Pending "`) | `inconsistent_casing` + `needs_trim` |
| `is_email_verified` | `"true"`, `"FALSE"`, `"Yes"`, `"No"`, `"1"`, `"0"`, `"Y"`, `"N"` | `inconsistent_boolean` |
| `account_balance` | `"$1,234.56"`, `"$0.00"`, `"($50.00)"` | `currency_string` (single symbol + accounting-negative branch) |
| `referral_discount` | Always `%`-suffixed: `"10%"`, `"25%"` | `percentage_string` → `CLEAN_DET` (not ambiguous) |
| `loyalty_account_id` | Zero-padded numeric: `"000123"` | `IDENTIFIER`, `format_signature="numeric"` (cross-table Group 5) |
| `preferred_contact_method` | ~15% null sentinels: `"N/A"`, `"-"`, `""`, `"NULL"` | `null_variant` |
| `tier_code` | Native `INTEGER`: `0`, `1`, `45`, `112` | Cross-table Group 6 canonical side |

### orders
| Column | Pattern | Expected Classification |
|---|---|---|
| `order_date` | Consistently `MM/DD/YYYY` | `mixed_date_format` → `date_format="%m/%d/%Y"` → `native_timestamp` |
| `order_status` | `"Pending"`, `"PENDING"`, `"shipped"`, `"Delivered"` | `inconsistent_casing` |
| `payment_method` | ~10% null sentinels: `"N/A"`, `"NULL"`, `"-"` | `null_variant` |
| `shipping_phone` | 10-digit, no punctuation: `"9876543210"` | PII + `format_signature="local_10d"` (cross-table Group 2) |
| `total_amount` | Multi-currency mixed: `"$120.00"`, `"€85.50"`, `"₹7,000"`, `"£45.00"` (all >5%) | `currency_string` → `CLEAN_AMBIG` (LLM resolver needed) |
| `discount_applied` | ~70% `"25%"`, ~30% bare `"0.3"`, `"5"`, `"15"` | `percentage_string` → `CLEAN_AMBIG` (mixed-scale, ratio >0.10) |
| `gift_wrap` | `"Y"`, `"N"`, `"yes"`, `"no"` + ~10% `"N/A"`/`""` | `inconsistent_boolean` + `null_variant` |
| `tracking_number` | `"TRK-7F3A9C2B"` | `IDENTIFIER` (`_number` suffix), `format_signature="alnum"` |

### payments
| Column | Pattern | Expected Classification |
|---|---|---|
| `payment_id` | UUID v4 | `IDENTIFIER`, `format_signature="alnum"` |
| `payment_date` | Native `TIMESTAMP` | `"native_timestamp"` — adds 180k to date group majority |
| `amount` | Mixed: plain `"450.00"`, `"$99.99"`, `"1.2K"`, `"2.5M"`, `"($30.00)"` | `currency_string` (K/M magnitude + single-symbol + accounting-negative) |
| `card_last_four` | Zero-padded 4-digit: `"0042"`, `"0007"` | `numeric_as_string` → **preserved as text** (leading-zero branch) |
| `refund_pct` | Bare fractions ≤1.0 only: `"0.05"`, `"0.25"` | `OBSERVE` (clean control) |
| `gateway_response_code` | Whitespace-padded: `" 00"`, `"00 "`, `"00"` | `needs_trim` + `numeric_as_string` + leading-zero preservation |

### employees  *(the deliberate minority/outlier)*
| Column | Pattern | Expected Classification |
|---|---|---|
| `employee_id` | `"EMP00123"` (alnum PK) | `IDENTIFIER`, `format_signature="alnum"` (cross-table Group 7 canonical) |
| `emp_phone` | `"(987) 654-3210"` (local 10d, punctuated) | PII + `format_signature="local_10d"` + punctuation patch (Group 2) |
| `hire_date` | **Every** value: day AND month both ≤12 → genuinely ambiguous | `mixed_date_format` → `date_format=None` → `CLEAN_AMBIG` → "needs manual review" |
| `department` | `"Sales"`, `"sales"`, `"SALES"`, `"Engineering "` | `inconsistent_casing` + `needs_trim` |
| `salary` | European: `"1.234,56 €"`, `"85.000,00 €"` | `currency_string` (European decimal-comma branch) |
| `is_manager` | Native `BOOLEAN` | `OBSERVE` immediately — BOOLEAN bug-fix regression test |
| `performance_score` | `"85%"` + null variants `"N/A"`, `"-"` | `percentage_string` + `null_variant` |

### support_tickets
| Column | Pattern | Expected Classification |
|---|---|---|
| `customer_id` | Zero-padded 6-digit VARCHAR: `"000045"` | `format_signature="numeric"` — **headline cross-table ID mismatch** (Group 4) |
| `agent_id` | Bare integer `123` corresponding to `"EMP00123"` | Deliberately unresolvable — Stage 0.5 must NOT attempt to fix |
| `priority` | `"High"`, `"HIGH"`, `"low"`, `"Medium"` | `inconsistent_casing` |
| `channel` | ~12% null sentinels: `"N/A"`, `"NaN"`, `""` | `null_variant` (NaN style) |
| `satisfaction_score` | `"85%"` + null sentinels `"N/A"`, `"-"` | `percentage_string` + `null_variant` |
| `attachment_metadata` | JSON blob | `STRUCTURAL` — excluded entirely |

### products
| Column | Pattern | Expected Classification |
|---|---|---|
| `product_name` | ~2,000 distinct values (below 10,000 threshold) | NOT `FREE_TEXT` — boundary test |
| `category` | Clean categorical, no issues | `OBSERVE` (clean control) |
| `list_price` | `"$1,200.00"`, `"$45.99"`, `"$1200"` (with/without comma) | `currency_string` |
| `weight_kg` | Plain numeric strings: `"2.5"`, `"0.75"` | `numeric_as_string` → `DOUBLE` |
| `in_stock` | Native `BOOLEAN` | `OBSERVE` — second BOOLEAN bug-fix instance |
| `last_restocked` | Unix epoch INTEGER: `1690000000`–`1740000000` | `mixed_date_format` → epoch-seconds branch → `native_timestamp` |
| `discount_rate` | Bare fractions ≤0.10: `"0.05"`, `"0.10"` | `percentage_string` → `CLEAN_DET` |

### loyalty_accounts
| Column | Pattern | Expected Classification |
|---|---|---|
| `loyalty_account_id` | Native `INTEGER`: `123`, `45678` | Canonical side of cross-table Group 5 |
| `tier_code` | Zero-padded VARCHAR incl. `"000"`, `"00"`, `"045"` | Cross-table Group 6 — all-zeros regex edge case |
| `points_balance` | Clean `INTEGER` | `OBSERVE` (clean control) |

### payroll_records
| Column | Pattern | Expected Classification |
|---|---|---|
| `employee_id` | Zero-padded numeric, no letters: `"00123"` | `format_signature="numeric"` — cross-table Group 7 non-canonical |

---

## Cross-Table Consistency Groups (Stage 0.5)

| Group | Members | Expected canonical | Tables needing patch |
|---|---|---|---|
| **1 — Date** | `customers.signup_date` (DD-MM-YYYY→native), `orders.order_date` (MM/DD/YYYY→native), `payments.payment_date` (native), `employees.hire_date` (ambiguous) | `native_timestamp` (orders+payments dominate) | `employees` → "needs manual review", no SQL patch |
| **2 — Phone** | `customers.phone_number` (intl_12d), `orders.shipping_phone` (local_10d), `employees.emp_phone` (local_10d, punctuated) | `local_10d` (orders 200k rows dominates) | `employees` → strip `(` `)` `-` punctuation; `customers` → flagged, no patch (digit-count mismatch) |
| **3 — customer_id sanity** | `customers.customer_id` (INT), `orders.customer_id` (INT), `loyalty_accounts.customer_id` (INT) | `numeric` | none — already consistent |
| **4 — customer_id flagship** | Groups 3 + `support_tickets.customer_id` (VARCHAR `"000045"`) | `numeric` (INT precedence) | `support_tickets` → `REGEXP_REPLACE('^0+(?=[0-9])','')` → `"000045"`→`"45"` |
| **5 — loyalty_account_id** | `customers.loyalty_account_id` (VARCHAR `"000123"`), `loyalty_accounts.loyalty_account_id` (INT `123`) | `numeric` | `customers` → zero-strip patch |
| **6 — tier_code** | `loyalty_accounts.tier_code` (VARCHAR `"000"`/`"045"`), `customers.tier_code` (INT) | `numeric` (INT side) | `loyalty_accounts` → zero-strip: `"000"`→`"0"`, `"045"`→`"45"` |
| **7 — employee_id** | `employees.employee_id` (VARCHAR `"EMP00123"`, alnum), `payroll_records.employee_id` (VARCHAR `"00123"`, numeric) | `alnum` (no native-INT member) | both → cast+trim only; payroll gets `extra_note` about leading-zero NOT stripped |

---

## Deliberately Unresolvable Case (§10)

`support_tickets.agent_id` (INTEGER `123`) vs `employees.employee_id` (VARCHAR `"EMP00123"`) — name mismatch means Stage 0.5 must **not** form a group and must **not** strip the `"EMP"` prefix. This is your regression test for "stay conservative."

---

## PII Columns (values must NEVER appear in LLM prompt logs)

`customers.full_name`, `customers.email`, `customers.phone_number`,
`customers.shipping_address`, `customers.date_of_birth`, `customers.last_login_ip`,
`customers.loyalty_account_id` (raw values), `orders.shipping_phone`,
`employees.full_name`, `employees.work_email`, `employees.emp_phone`,
`payments.payer_email`, `payments.card_last_four` (raw values),
`products.supplier_email`, `support_tickets.customer_id` (raw zero-padded values)

---

## Load Instructions

```bash
# Create a fresh database and load
psql -U postgres -c "CREATE DATABASE clarum_test;"
psql -U postgres -d clarum_test -f clarum_test_dataset.sql

# Quick row-count sanity check
psql -U postgres -d clarum_test -c "
  SELECT 'customers' AS tbl, COUNT(*) FROM customers
  UNION ALL SELECT 'orders', COUNT(*) FROM orders
  UNION ALL SELECT 'payments', COUNT(*) FROM payments
  UNION ALL SELECT 'employees', COUNT(*) FROM employees
  UNION ALL SELECT 'support_tickets', COUNT(*) FROM support_tickets
  UNION ALL SELECT 'products', COUNT(*) FROM products
  UNION ALL SELECT 'loyalty_accounts', COUNT(*) FROM loyalty_accounts
  UNION ALL SELECT 'payroll_records', COUNT(*) FROM payroll_records;
"
```

---

## Key Ratios to Verify After Load

| Check | Expected |
|---|---|
| `orders.discount_applied` bare-number rows | ~30% (ratio > 0.10 → CLEAN_AMBIG) |
| `orders.total_amount` distinct currency symbols | 4 (each > 5% of rows) |
| `employees.hire_date` values with any part > 12 | 0 (all genuinely ambiguous) |
| `customers.notes` distinct values | > 10,000 (FREE_TEXT) |
| `products.product_name` distinct values | ~2,000 (NOT FREE_TEXT) |
| `employees` row count vs `orders`+`payments` | 3,000 vs 380,000 — minority |
