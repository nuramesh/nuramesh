-- Sample data for the Data analysis template (fictional, invented for the walkthrough).
-- team.py loads this file into an in-memory SQLite database at run time; amounts are whole cents.
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  region TEXT NOT NULL,
  product TEXT NOT NULL,
  units INTEGER NOT NULL,
  unit_price_cents INTEGER NOT NULL,
  ordered_on TEXT NOT NULL
);
INSERT INTO orders VALUES (1, 'North', 'Standing desk', 12, 41900, '2026-01-06');
INSERT INTO orders VALUES (2, 'North', 'Desk lamp', 40, 3450, '2026-01-19');
INSERT INTO orders VALUES (3, 'North', 'Monitor arm', 18, 8900, '2026-02-02');
INSERT INTO orders VALUES (4, 'South', 'Standing desk', 9, 41900, '2026-01-12');
INSERT INTO orders VALUES (5, 'South', 'Desk lamp', 55, 3450, '2026-02-09');
INSERT INTO orders VALUES (6, 'South', 'Monitor arm', 7, 8900, '2026-02-23');
INSERT INTO orders VALUES (7, 'East', 'Standing desk', 15, 41900, '2026-01-27');
INSERT INTO orders VALUES (8, 'East', 'Desk lamp', 22, 3450, '2026-02-04');
INSERT INTO orders VALUES (9, 'East', 'Monitor arm', 30, 8900, '2026-03-02');
INSERT INTO orders VALUES (10, 'West', 'Standing desk', 6, 41900, '2026-02-16');
INSERT INTO orders VALUES (11, 'West', 'Desk lamp', 31, 3450, '2026-03-09');
INSERT INTO orders VALUES (12, 'West', 'Monitor arm', 11, 8900, '2026-03-16');
