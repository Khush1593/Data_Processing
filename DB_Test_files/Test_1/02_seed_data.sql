-- ============================================================
-- Clarum Insights — Seed Data (Messy, Real-World Edge Cases)
-- ============================================================
-- Run AFTER 01_schema_and_tables.sql
-- psql -U postgres -h localhost -f 02_seed_data.sql
-- ============================================================

SET search_path TO clarum_test;

-- ============================================================
-- SALES REPS (small table — load first, referenced by orders)
-- ============================================================
INSERT INTO sales_reps (rep_name, email, territory, commission_rate, hire_date, target_revenue, is_active, manager_id) VALUES
-- Clean records
('Arjun Mehta',       'arjun.mehta@company.com',    'North',            '5%',    '15-JAN-2020',  '$500,000',  'Active',   NULL),
('Priya Sharma',      'priya.sharma@company.com',    'South',            '6%',    '01-MAR-2019',  '$600,000',  'Active',   1),
('Ravi Patel',        'ravi.patel@company.com',      'East',             '5.5%',  '10-JUN-2021',  '$450,000',  'Active',   1),
('Sneha Iyer',        'sneha.iyer@company.com',      'West',             '0.06',  '22-SEP-2018',  '550K',      'Active',   1),
('Vikram Singh',      'vikram.singh@company.com',    'N',                '7%',    '05-DEC-2020',  '700000',    'Y',        1),
-- Inconsistent territory naming
('Kavitha Nair',      'kavitha.nair@company.com',    'Northern Region',  '0.055', '18-FEB-2022',  '$480,000',  'Inactive', 1),
('Rohan Das',         'rohan.das@company.com',       'S',                '5%',    '30-JUL-2017',  '$520,000',  'N',        1),
('Anjali Gupta',      'anjali.gupta@company.com',    'Eastern Zone',     '6.5%',  '12-APR-2023',  '0.6M',      'Y',        1),
-- Deliberate NULL/empty issues
('Suresh Kumar',      'suresh.kumar@company.com',    'West',             NULL,    '01-JAN-2021',  '$400,000',  'Active',   1),
('Deepa Reddy',       'deepa.reddy@company.com',     NULL,               '5%',    '14-AUG-2019',  '$550,000',  'Active',   1);


-- ============================================================
-- CUSTOMERS (with soft deletes + all the messy types)
-- ============================================================
INSERT INTO customers (full_name, email, phone_number, annual_revenue, signup_date, customer_tier, country, city, lifetime_value, is_active, deleted_at) VALUES
-- Clean records
('Rahul Verma',         'rahul.verma@email.com',      '+91-9876543210',   '1,200,000',  '2022-01-15',           'Gold',     'India',         'Mumbai',     '45000',   'T', NULL),
('Sarah Johnson',       'sarah.j@business.com',       '+1-555-234-5678',  '$2.3M',      '2021-06-20',           'Platinum', 'United States', 'New York',   '125000',  'T', NULL),
('Mohammed Al-Rashid',  'mal.rashid@corp.ae',         '+971-50-1234567',  '850000',     '2023-03-10',           'Silver',   'UAE',           'Dubai',      '28000',   'T', NULL),
('Preethi Sundaram',    'p.sundaram@techinc.in',      '9988776655',       '1.5M',       '2020-11-05',           'Gold',     'IN',            'Bangalore',  '67000',   'T', NULL),
-- Phone number format issues
('Tom Bradley',         'tom.b@widgets.co.uk',        '(020) 7946 0123',  '£500K',      '2022-08-30',           'Silver',   'UK',            'London',     '18000',   'T', NULL),
('Liu Wei',             'liu.wei@tech.cn',             '86-138-0013-8000', '$3.5M',      '1640000000',           'Platinum', 'CN',            'Shanghai',   '200000',  'T', NULL),  -- Unix timestamp
-- Country inconsistency
('Emma Müller',         'emma.m@firma.de',            '+49-89-12345678',  '1.8M EUR',   '2021-02-14',           'Gold',     'Germany',       'Munich',     '89000',   'T', NULL),
('Carlos Rivera',       'c.rivera@empresa.mx',        '+52-55-1234-5678', '900000',     '15/07/2022',           'Silver',   'MX',            'Mexico City','32000',   'T', NULL),  -- date format issue
-- customer_tier NULL vs empty string
('Aisha Patel',         'aisha.p@retail.com',         '07700900123',      '250000',     '2023-09-01',           '',         'United Kingdom','Birmingham', '5000',    'T', NULL),  -- empty tier
('James Chen',          'james.chen@startup.io',      '4155552671',       'N/A',        '2022-12-31',           NULL,       'United States', 'San Francisco','0',     'T', NULL),  -- NULL tier, N/A revenue
-- Soft deletes (should be excluded from active dashboard data)
('Deleted Customer A',  'del.a@old.com',              '1234567890',       '100000',     '2019-01-01',           'Bronze',   'India',         'Delhi',      '1000',    'F', '2023-06-15 10:30:00'),
('Deleted Customer B',  'del.b@old.com',              '0987654321',       '200000',     '2019-06-01',           'Bronze',   'India',         'Chennai',    '2000',    'F', '2023-08-20 14:00:00'),
-- lifetime_value edge cases
('Zero Value Client',   'zero@client.com',            '+91-8888888888',   '50000',      '2024-01-01',           'Bronze',   'India',         'Pune',       '0',       'T', NULL),  -- legitimate zero
('New Signup',          'new@signup.com',             '+91-7777777777',   '75000',      '2024-06-01',           'Bronze',   'India',         'Hyderabad',  '',        'T', NULL),  -- empty = missing
-- Invalid email (should be detected but not crashed on)
('Invalid Email User',  'not-an-email',               '+91-6666666666',   '300000',     '2023-05-10',           'Silver',   'India',         'Kolkata',    '12000',   'T', NULL),
-- Annual revenue as various formats
('Big Corp Inc',        'bigcorp@enterprise.com',     '+1-800-555-0100',  '$10,500,000','2020-03-15',           'Platinum', 'US',            'Chicago',    '750000',  'T', NULL),
('Mid Market Co',       'info@midmarket.co',          '+44-20-7946-9999', '€2.1M',      '2021-07-22',           'Gold',     'United Kingdom','Manchester', '95000',   'T', NULL),
('SMB Solutions',       'contact@smb.biz',            '1300 123 456',     'AUD 400K',   '2022-11-08',           'Silver',   'Australia',     'Sydney',     '21000',   'T', NULL),
('Startup XYZ',         'hello@startupxyz.com',       '+65-9123-4567',    '120000 SGD', '2024-02-28',           'Bronze',   'Singapore',     'Singapore',  '3500',    'T', NULL),
('Legacy Corp',         'legacy@oldcorp.com',         '555-0100',         '5000000',    '1577836800',           'Platinum', 'United States', 'Boston',     '320000',  'T', NULL);  -- Unix timestamp signup


-- ============================================================
-- PRODUCTS
-- ============================================================
INSERT INTO products (product_name, category, subcategory, cost_price, sale_price, weight_kg, margin_pct, in_stock, launch_date, description, supplier_code, sku) VALUES
-- Electronics
('Wireless Noise-Cancelling Headphones', 'Electronics', 'Audio',     '$45.00',  '$129.99', '0.35 kg',  '65%',  'yes',          '2023-01-15',  'Premium wireless headphones with ANC', '00123', 'ELEC-AUD-001'),
('USB-C Charging Hub 7-Port',            'Electronics', 'Accessories','$12.50',  '$34.99',  '250g',     '0.64', 'YES',          '44927',       NULL,                                   '00124', 'ELEC-ACC-002'),  -- Excel serial date
('4K Webcam Pro',                        'Electronics', 'Video',      '£28.00',  '£79.99',  '0.42KG',   '64.9', 'available',    '2022-09-01',  'Professional 4K webcam for streaming',  '00125', 'ELEC-VID-003'),
('Mechanical Keyboard RGB',              'Electronics', 'Peripherals','$25.00',  '$89.99',  '1.1 kg',   '72%',  'out of stock', '2023-06-10',  NULL,                                   '00126', 'ELEC-PER-004'),
('Smart Watch Series 5',                 'Electronics', 'Wearables',  '$85.00',  '$249.99', '50g',      '65.9', '1',            '45000',       'Advanced fitness and health tracking',  '00127', 'ELEC-WEA-005'),  -- Excel serial date
-- Apparel
('Cotton Formal Shirt',                  'Apparel',     'Mens',       '$8.50',   '$29.99',  '0.25 kg',  '71.6%','yes',          '2023-02-01',  NULL,                                   '00200', 'APP-MEN-001'),
('Womens Running Jacket',                'Apparel',     'Womens',     '$22.00',  '$74.99',  '400g',     '70.7', 'YES',          '2023-03-15',  'Lightweight waterproof running jacket',  '00201', 'APP-WOM-002'),
('Kids Denim Jeans',                     'Apparel',     'Kids',       '$10.00',  '$34.99',  '0.5KG',    '71.4%','available',    '01/02/2023',  NULL,                                   '00202', 'APP-KID-003'),  -- mixed date format
-- Home & Kitchen
('Stainless Steel Cookware Set',         'Home',        'Kitchen',    '$35.00',  '$99.99',  '3.5 kg',   '64.9', 'yes',          '2022-11-01',  '10-piece professional cookware set',    '00300', 'HOME-KIT-001'),
('Bamboo Cutting Board Set',             'Home',        'Kitchen',    '$5.50',   '$19.99',  '800g',     '72.4%','1',            '2023-04-20',  NULL,                                   '00301', 'HOME-KIT-002'),
('Memory Foam Pillow',                   'Home',        'Bedroom',    '$12.00',  '$44.99',  '1.2 kg',   '73.3', 'NO',           '2023-01-30',  'Ergonomic cervical support pillow',     '00302', 'HOME-BED-001'),
('Smart LED Desk Lamp',                  'Home',        'Lighting',   '$15.00',  '$49.99',  '0.8 kg',   '69.9%','yes',          '2023-05-01',  NULL,                                   '00303', 'HOME-LIT-001'),
-- Edge case products
('Mystery Bundle Pack',                  'Other',       'Bundles',    NULL,      '$49.99',  NULL,       NULL,   NULL,           NULL,          NULL,                                   '00999', 'OTH-BUN-001'),  -- all nulls
('Discontinued Item X',                  'Electronics', 'Legacy',     '$100.00', '$0',      '2 kg',     '-100%','no',           '2010-01-01',  'Discontinued product',                  '00001', 'ELEC-LEG-001'),  -- negative margin
('Oversized Furniture Item',             'Home',        'Furniture',  '$250.00', '$599.99', '45.5 kg',  '58.4%','yes',          '2023-07-15',  'Large 3-seater sofa with storage',     '00400', 'HOME-FUR-001');


-- ============================================================
-- ORDERS (the main messy fact table — ~100 rows with all edge cases)
-- ============================================================
-- Helper: We seed enough variety to cover all issue types
-- Batch 1: Standard records with currency string issues
INSERT INTO orders (customer_id, order_date, order_value, discount_pct, shipping_cost, status, region, product_category, quantity, unit_price, is_returned, customer_name, sales_rep_id, notes) VALUES
(1,  '2023-01-15',      '$1,234.56', '10%',   '$15.00',  'completed',  'North',   'Electronics', 3,  '$411.52', 'N',    'Rahul Verma',        1, NULL),
(2,  '2023-01-20',      '$890.00',   '5%',    '$10.00',  'completed',  'South',   'Electronics', 2,  '$445.00', 'No',   'Sarah Johnson  ',    2, NULL),  -- trailing space in name
(3,  '2023-02-01',      '£450.00',   '0%',    'FREE',    'completed',  'East',    'Apparel',     5,  '£90.00',  'N',    'Mohammed Al-Rashid', 3, NULL),  -- FREE shipping
(4,  '15/01/2023',      '$2,100.00', '15%',   'N/A',     'completed',  'West',    'Home',        7,  '$300.00', 'Yes',  '  Preethi Sundaram', 4, NULL),  -- different date format, leading space
(5,  'Jan 15 2023',     '$340.00',   '0%',    '$8.50',   'shipped',    'NORTH',   'Electronics', 1,  '$340.00', '1',    'Tom Bradley',        5, NULL),  -- 3rd date format, CAPS region
(6,  '2023-02-14',      '$12,500.00','20%',   '$0.00',   'COMPLETED',  'north',   'Electronics', 5,  '$2,500.00','No',  'Liu Wei',            1, NULL),  -- lowercase region, CAPS status
(7,  '2023-03-01',      '$560.50',   '7.5%',  '$12.00',  'Complete',   'South',   'Apparel',     2,  '$280.25', 'N',    'Emma Müller',        2, NULL),  -- yet another status variant
(8,  '2023-03-10',      '$95.99',    '5%',    '-',       'completed',  'EAST',    'Home',        3,  '$31.99',  '0',    'Carlos Rivera',      3, NULL),  -- '-' shipping null variant
(9,  '01/04/2023',      '$1,750.00', '10%',   '$20.00',  'pending',    'West',    'Electronics', 5,  '$350.00', 'N',    'Aisha Patel',        4, NULL),
(10, '2023-04-15',      '$440.00',   '0%',    '$9.99',   'completed',  'North',   'Apparel',     4,  '$110.00', 'FALSE','James Chen',         5, NULL),
-- Batch 2: More date format variations
(1,  'April 20 2023',   '$2,890.00', '12%',   '$25.00',  'completed',  'North',   'Electronics', 7,  '$412.86', 'N',    'Rahul Verma',        1, NULL),
(2,  '2023-05-01',      '$670.00',   '5%',    '$10.00',  'returned',   'South',   'Home',        3,  '$223.33', 'TRUE', 'Sarah Johnson',      2, NULL),
(3,  '05/05/2023',      '$320.00',   '0%',    '$7.50',   'completed',  'East',    'Apparel',     4,  '$80.00',  'N',    'Mohammed Al-Rashid', 3, NULL),
(4,  '2023-05-20',      '$5,600.00', '18%',   'N/A',     'completed',  'West',    'Electronics', 14, '$400.00', 'N',    'Preethi Sundaram',   4, NULL),
(5,  '2023-06-01',      '$199.50',   '0%',    'FREE',    'processing', 'north',   'Home',        5,  '$39.90',  'No',   'Tom Bradley',        5, NULL),
(6,  'Jun 15 2023',     '$8,750.00', '10%',   '$0.00',   'completed',  'South',   'Electronics', 3,  '$2,916.67','0',   'Liu Wei',            1, NULL),
(7,  '2023-06-20',      '$455.00',   '5%',    '$11.00',  'shipped',    'East',    'Apparel',     5,  '$91.00',  'N',    'Emma Müller',        2, NULL),
(8,  '20/06/2023',      '$125.00',   '0%',    '$8.00',   'completed',  'North',   'Home',        4,  '$31.25',  'Yes',  'Carlos Rivera',      3, NULL),
(9,  '2023-07-04',      '$3,200.00', '15%',   '$30.00',  'completed',  'WEST',    'Electronics', 8,  '$400.00', 'N',    'Aisha Patel',        4, NULL),
(10, '2023-07-10',      '$88.00',    '0%',    '$6.50',   'cancelled',  'South',   'Apparel',     2,  '$44.00',  'N',    'James Chen',         5, NULL),
-- Batch 3: NULL value records (genuine missing data)
(11, '2023-07-15',      NULL,        '5%',    '$12.00',  'completed',  'North',   'Electronics', 2,  NULL,      'N',    'Deleted Customer A', 1, NULL),  -- NULL order_value
(12, '2023-07-20',      NULL,        NULL,    NULL,      'pending',    'South',   'Home',        1,  NULL,      NULL,   'Deleted Customer B', 2, NULL),  -- multiple NULLs
(1,  '2023-08-01',      NULL,        '0%',    '$15.00',  'completed',  'East',    'Apparel',     3,  NULL,      'N',    'Rahul Verma',        3, NULL),
(2,  '2023-08-10',      '$920.00',   NULL,    '$10.00',  'completed',  'West',    'Electronics', 2,  '$460.00', 'No',   'Sarah Johnson',      4, NULL),
(3,  '15/08/2023',      '$1,100.00', '8%',    NULL,      'completed',  'North',   'Home',        4,  '$275.00', 'N',    'Mohammed Al-Rashid', 5, NULL),
-- Batch 4: Legacy status values
(4,  '2023-08-20',      '$780.00',   '5%',    '$10.00',  'DONE',       'South',   'Apparel',     3,  '$260.00', 'N',    'Preethi Sundaram',   1, NULL),  -- legacy status
(5,  '2023-09-01',      '$2,450.00', '12%',   '$25.00',  'FULFILLED',  'East',    'Electronics', 6,  '$408.33', 'No',   'Tom Bradley',        2, NULL),  -- legacy status
(6,  '01/09/2023',      '$390.00',   '0%',    'FREE',    'PAID',       'West',    'Home',        7,  '$55.71',  'N',    'Liu Wei',            3, NULL),  -- legacy status
(7,  'Sep 15 2023',     '$175.00',   '5%',    '$8.00',   'Delivered',  'NORTH',   'Apparel',     2,  '$87.50',  'No',   'Emma Müller',        4, NULL),  -- legacy status
(8,  '2023-09-20',      '$3,600.00', '15%',   '$35.00',  'complete',   'South',   'Electronics', 9,  '$400.00', 'FALSE','Carlos Rivera',      5, NULL),
-- Batch 5: Extreme values + outliers
(1,  '2023-10-01',      '$999,999.99','0%',   '$500.00', 'completed',  'North',   'Electronics', 1000,'$999.99','N',   'Rahul Verma',        1, 'Large bulk order - verified'),
(9,  '2023-10-05',      '$0.01',     '0%',    '$0.00',   'completed',  'West',    'Apparel',     1,  '$0.01',   'N',   'Aisha Patel',        2, 'Test order'),
(10, '2023-10-10',      '-$50.00',   '0%',    '$0.00',   'refunded',   'South',   'Home',        1,  '-$50.00', 'Yes', 'James Chen',         3, 'Credit note'),  -- negative value (legitimate credit)
(2,  '10/10/2023',      '$12,750.00','25%',   '$100.00', 'completed',  'East',    'Electronics', 30, '$425.00', 'No',  'Sarah Johnson',      4, NULL),
(3,  '2023-10-20',      '€890.00',   '10%',   '€10.00',  'completed',  'North',   'Electronics', 2,  '€445.00', 'N',   'Mohammed Al-Rashid', 5, NULL),  -- Euro currency
-- Batch 6: Records that should trigger duplicate detection (~3% of total)
(1,  '2023-01-15',      '$1,234.56', '10%',   '$15.00',  'completed',  'North',   'Electronics', 3,  '$411.52', 'N',   'Rahul Verma',        1, NULL),  -- EXACT DUPLICATE of row 1
(2,  '2023-01-20',      '$890.00',   '5%',    '$10.00',  'completed',  'South',   'Electronics', 2,  '$445.00', 'No',  'Sarah Johnson  ',    2, NULL),  -- EXACT DUPLICATE of row 2
-- Batch 7: Notes column with embedded JSON fragments (real-world ERP export artifact)
(4,  '2023-11-01',      '$2,300.00', '10%',   '$20.00',  'completed',  'West',    'Electronics', 5,  '$460.00', 'N',   'Preethi Sundaram',   1, '{"warehouse":"WH-01","picker":"ID-334"}'),
(5,  '2023-11-10',      '$560.00',   '5%',    '$12.00',  'shipped',    'North',   'Home',        4,  '$140.00', 'No',  'Tom Bradley',        2, 'Customer requested gift wrap'),
(6,  '15/11/2023',      '$4,200.00', '20%',   '$45.00',  'completed',  'South',   'Electronics', 10, '$420.00', 'N',   'Liu Wei',            3, '{"priority":"urgent","vip":true}'),
(7,  '2023-11-20',      '$280.00',   '0%',    'FREE',    'completed',  'East',    'Apparel',     4,  '$70.00',  'Yes', 'Emma Müller',        4, NULL),
(8,  '2023-11-25',      '$1,890.00', '8%',    '$18.00',  'completed',  'North',   'Home',        6,  '$315.00', 'N',   'Carlos Rivera',      5, NULL),
-- Batch 8: December records (year-end patterns)
(9,  '2023-12-01',      '$5,400.00', '20%',   '$50.00',  'completed',  'West',    'Electronics', 12, '$450.00', 'No',  'Aisha Patel',        1, NULL),
(10, '01/12/2023',      '$145.00',   '5%',    '$7.50',   'completed',  'South',   'Apparel',     3,  '$48.33',  'N',   'James Chen',         2, NULL),
(1,  'Dec 15 2023',     '$7,800.00', '15%',   '$75.00',  'completed',  'North',   'Electronics', 15, '$520.00', 'N',   'Rahul Verma',        3, NULL),
(2,  '2023-12-20',      '$430.00',   '0%',    '$9.00',   'processing', 'East',    'Home',        8,  '$53.75',  'No',  'Sarah Johnson',      4, NULL),
(3,  '2023-12-28',      '$980.00',   '10%',   '$15.00',  'completed',  'South',   'Apparel',     7,  '$140.00', 'N',   'Mohammed Al-Rashid', 5, NULL),
-- Batch 9: 2024 records
(4,  '2024-01-05',      '$3,150.00', '10%',   '$30.00',  'completed',  'West',    'Electronics', 7,  '$450.00', 'No',  'Preethi Sundaram',   1, NULL),
(5,  '05/01/2024',      '$225.00',   '5%',    '$8.00',   'completed',  'North',   'Home',        5,  '$45.00',  'N',   'Tom Bradley',        2, NULL),
(6,  '2024-01-15',      '$9,750.00', '20%',   '$100.00', 'completed',  'South',   'Electronics', 25, '$390.00', 'No',  'Liu Wei',            3, NULL),
(7,  'Jan 20 2024',     '$330.00',   '0%',    '$7.00',   'shipped',    'East',    'Apparel',     3,  '$110.00', 'N',   'Emma Müller',        4, NULL),
(8,  '2024-01-25',      '$1,670.00', '8%',    '$18.00',  'completed',  'North',   'Home',        5,  '$334.00', 'No',  'Carlos Rivera',      5, NULL),
(9,  '2024-02-01',      '$4,500.00', '15%',   '$45.00',  'completed',  'West',    'Electronics', 10, '$450.00', 'N',   'Aisha Patel',        1, NULL),
(10, '01/02/2024',      '$88.50',    '0%',    '$6.00',   'completed',  'South',   'Apparel',     2,  '$44.25',  'No',  'James Chen',         2, NULL),
(15, '2024-02-15',      '$12,000.00','25%',   '$120.00', 'completed',  'North',   'Electronics', 30, '$400.00', 'N',   'Big Corp Inc',       3, NULL),
(16, '15/02/2024',      '$3,400.00', '12%',   '$35.00',  'completed',  'East',    'Electronics', 8,  '$425.00', 'No',  'Mid Market Co',      4, NULL),
(1,  '2024-03-01',      '$2,800.00', '10%',   '$28.00',  'completed',  'West',    'Electronics', 6,  '$466.67', 'N',   'Rahul Verma',        5, NULL);


-- ============================================================
-- RETURNS
-- ============================================================
INSERT INTO returns (order_id, customer_id, return_date, refund_amount, return_reason, days_to_return, restocking_fee, approved_by) VALUES
(2,  2,  '2023-02-05',    '$890.00',   'Wrong size ordered',          '14 days',   '10%',   1),
(7,  7,  '07/03/2023',    '$455.00',   'DEFECTIVE',                   '21',        '15%',   2),  -- coded reason, numeric days
(12, 2,  'Mar 25 2023',   '$670.00',   'Changed mind',                '3 days',    NULL,    1),  -- 3rd date format, NULL fee
(18, 8,  '2023-07-05',    '$125.00',   'Not as described',            '72 hours',  '5%',    3),  -- hours format
(25, 1,  '15/10/2023',    '$999,999.99','Bulk order cancelled',       '5 days',    '0%',    1),  -- matches the big order
(28, 7,  '2023-11-28',    '$280.00',   'WRONG_ITEM_SHIPPED',          '3',         NULL,    2),  -- coded reason, just number
(32, 9,  '2024-01-10',    '$4,500.00', 'Quality not acceptable',      '9 days',    '10%',   3),
(10, 10, '2023-07-18',    '$88.00',    'Cancelled before delivery',   '0',         '0%',    1);  -- 0 days (cancelled pre-ship


-- ============================================================
-- MARKETING CAMPAIGNS
-- ============================================================
INSERT INTO marketing_campaigns (campaign_name, channel, budget, spend, impressions, clicks, conversions, ctr, roas, start_date, end_date, is_active) VALUES
('Q1 Brand Awareness',      'Google Ads',   '$50,000',   '$48,234',   2500000, 37500, 750,  '1.5%',  '3.2x',  '2023-01-01', '2023-03-31', false),
('Summer Sale Push',        'google ads',   '75000',     '$72,100',   4000000, 80000, 2000, '2%',    '4.1',   '2023-06-01', '2023-08-31', false),  -- lowercase channel, roas without 'x'
('Festive Season Campaign', 'GOOGLE',       '$100,000',  '95K',       6000000, 120000,3500, '0.02',  '320%',  '2023-10-01', '2023-12-31', false),  -- all ctr/roas format variations
('LinkedIn B2B Outreach',   'LinkedIn',     '$30,000',   '$28,500',   500000,  7500,  150,  '1.5%',  '2.8x',  '2023-02-01', '2023-04-30', false),
('Instagram Stories',       'instagram',    '$20,000',   '$19,800',   1500000, 22500, 450,  '1.5%',  '3.5',   '2023-03-01', '2023-05-31', false),
('Facebook Retargeting',    'Facebook',     '$15,000',   '$14,200',   800000,  16000, 640,  '2%',    '3.8x',  '2023-04-01', '2023-06-30', false),
('YouTube Pre-Roll',        'YouTube',      '25K',       '$22,400',   3000000, 30000, 300,  '0.01',  '2.1',   '2023-05-01', '2023-07-31', false),  -- budget as '25K'
('Email Newsletter Q3',     'Email',        '$5,000',    '$4,800',    0,       45000, 900,  '20%',   '6.2x',  '2023-07-01', '2023-09-30', false),
('SEO Content Push',        'SEO',          '$10,000',   '$9,500',    800000,  32000, 160,  '4%',    '1.9',   '2023-01-01', '2023-12-31', false),
-- Draft campaigns (not yet activated — NULL spend)
('Q1 2024 Brand Campaign',  'Google Ads',   '$80,000',   NULL,        NULL,    NULL,  NULL, NULL,    NULL,    '2024-01-01', '2024-03-31', true),
('New Product Launch',      'Instagram',    '60K',       NULL,        NULL,    NULL,  NULL, NULL,    NULL,    '2024-02-01', '2024-04-30', true),  -- budget as '60K'
('B2B Lead Gen Q1',         'LinkedIn',     '$45,000',   NULL,        NULL,    NULL,  NULL, NULL,    NULL,    '2024-01-15', '2024-03-15', true),
('Influencer Partnership',  'Instagram',    '$25,000',   NULL,        NULL,    NULL,  NULL, NULL,    NULL,    NULL,         NULL,         true),  -- draft - no dates
('Affiliate Program',       'Affiliate',    '$35,000',   NULL,        NULL,    NULL,  NULL, NULL,    NULL,    NULL,         NULL,         false); -- draft - no dates


-- ============================================================
-- INVENTORY SNAPSHOTS (enough rows for chunking behaviour)
-- Using generate_series for volume
-- ============================================================
-- Clean snapshots with warehouse format issues
INSERT INTO inventory_snapshots (product_id, warehouse_id, snapshot_date, quantity_on_hand, unit_cost, reorder_level)
SELECT
    (s % 15) + 1                                           AS product_id,
    CASE s % 4
        WHEN 0 THEN 'WH-01'
        WHEN 1 THEN 'wh01'                                 -- inconsistent format
        WHEN 2 THEN '1'                                    -- just the number
        ELSE        'Warehouse 1'                          -- full name
    END                                                    AS warehouse_id,
    (DATE '2023-01-01' + (s / 15 || ' days')::INTERVAL)::TEXT  AS snapshot_date,
    -- Legitimate negatives for some products (returns processing), garbage for tiny %
    CASE
        WHEN s % 200 = 0 THEN -999999                      -- garbage value (0.5% of rows)
        WHEN s % 50  = 0 THEN -(s % 25 + 1)               -- legitimate negative (returns)
        ELSE                   (s % 200) + 10              -- normal stock level
    END                                                    AS quantity_on_hand,
    CASE s % 3
        WHEN 0 THEN '$' || ROUND((20 + (s % 80))::NUMERIC, 2)::TEXT
        WHEN 1 THEN '£' || ROUND((15 + (s % 60))::NUMERIC, 2)::TEXT
        ELSE        ROUND((18 + (s % 70))::NUMERIC, 2)::TEXT  -- no currency symbol
    END                                                    AS unit_cost,
    (s % 50) + 5                                           AS reorder_level
FROM generate_series(0, 4999) AS s;  -- 5000 rows — large enough for chunking tests


-- ============================================================
-- SUPPORT TICKETS
-- ============================================================
INSERT INTO support_tickets (customer_id, order_id, subject, priority, status, resolution_time_hrs, satisfaction_score, ticket_value, assigned_to, deleted_at) VALUES
-- Priority inconsistency: numeric + text
(1,  1,  'Order not received after 2 weeks',      'High',   'resolved',   '4.5 hours',   '4/5',   '$1,234.56', 1, NULL),
(2,  2,  'Wrong item shipped',                     '1',      'resolved',   '2.5',         '4',     '$890.00',   2, NULL),  -- numeric priority, no unit
(3,  3,  'Refund not processed',                   'High',   'resolved',   '150 minutes', '80%',   '£450.00',   1, NULL),  -- minutes format, percentage score
(4,  4,  'Product quality issue',                  '2',      'resolved',   '8 hours',     'Good',  '$2,100.00', 3, NULL),  -- numeric priority, text score
(5,  5,  'Website not loading',                    'Medium', 'resolved',   '0.5',         '5/5',   '$340.00',   2, NULL),
(6,  6,  'Payment declined',                       '1',      'resolved',   '1 hour',      '3',     '$12,500.00',1, NULL),
(7,  7,  'Tracking number invalid',                'Low',    'resolved',   '6.5 hours',   '4/5',   '$560.50',   3, NULL),
(8,  8,  'Package damaged on arrival',             '3',      'resolved',   '72 hours',    '2',     '$95.99',    2, NULL),  -- 72 hours format
(9,  9,  'Account access issues',                  'Medium', 'in_progress',NULL,          NULL,    '$1,750.00', 1, NULL),  -- NULL resolution (open)
(10, 10, 'Duplicate charge on account',            '1',      'in_progress',NULL,          NULL,    '$88.00',    3, NULL),  -- NULL resolution (open)
(11, 11, 'Delivery address update needed',         'Low',    'resolved',   '2 hours',     '5/5',   '$0.00',     2, NULL),
(1,  12, 'Bulk order status inquiry',              'High',   'resolved',   '1.5',         'Excellent','$890.00',1, NULL), -- text score variant
(15, 45, 'Enterprise SLA breach complaint',        '1',      'escalated',  NULL,          NULL,    '$12,000.00',1, NULL), -- NULL resolution (escalated)
-- Soft deleted tickets (GDPR removal)
(11, NULL,'Spam ticket',                           'Low',    'closed',     '0',           '1',     '$0.00',     2, '2023-06-01 09:00:00'),
(12, NULL,'Test ticket from dev env',              'Low',    'closed',     '0',           NULL,    '$0.00',     2, '2023-07-15 11:00:00'),
-- Open tickets with various NULL patterns
(4,  38, 'Payment terms dispute',                  '2',      'open',       NULL,          NULL,    '$3,150.00', 3, NULL),
(16, 44, 'Invoice discrepancy',                    'Medium', 'open',       NULL,          NULL,    '$3,400.00', 1, NULL),
(9,  32, 'Subscription cancellation request',      '3',      'pending',    NULL,          NULL,    '$4,500.00', 2, NULL);


-- ============================================================
-- VERIFICATION QUERIES
-- Run these to confirm data loaded correctly
-- ============================================================
-- SELECT 'orders' AS tbl,            COUNT(*) AS row_count FROM clarum_test.orders
-- UNION ALL SELECT 'customers',      COUNT(*) FROM clarum_test.customers
-- UNION ALL SELECT 'products',       COUNT(*) FROM clarum_test.products
-- UNION ALL SELECT 'sales_reps',     COUNT(*) FROM clarum_test.sales_reps
-- UNION ALL SELECT 'returns',        COUNT(*) FROM clarum_test.returns
-- UNION ALL SELECT 'marketing_campaigns', COUNT(*) FROM clarum_test.marketing_campaigns
-- UNION ALL SELECT 'inventory_snapshots', COUNT(*) FROM clarum_test.inventory_snapshots
-- UNION ALL SELECT 'support_tickets',COUNT(*) FROM clarum_test.support_tickets
-- ORDER BY tbl;
