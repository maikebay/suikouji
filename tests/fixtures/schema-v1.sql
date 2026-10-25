
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shop (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entry (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  date TEXT NOT NULL,
  type TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  item TEXT,
  qty REAL,
  unit TEXT,
  note TEXT,
  source TEXT NOT NULL,
  raw_text TEXT,
  batch_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entry_shop_date ON entry(shop_id, date);

CREATE TABLE IF NOT EXISTS product (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  price REAL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(shop_id, name)
);

CREATE TABLE IF NOT EXISTS ingredient (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  base_unit TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(shop_id, name)
);

CREATE TABLE IF NOT EXISTS recipe_line (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  ingredient_id TEXT NOT NULL,
  qty REAL NOT NULL,
  units REAL NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(product_id, ingredient_id)
);

CREATE TABLE IF NOT EXISTS sale (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  date TEXT NOT NULL,
  product_id TEXT,
  product_name TEXT NOT NULL,
  qty REAL NOT NULL,
  amount REAL,
  source TEXT NOT NULL,
  batch_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sale_shop_date ON sale(shop_id, date);

CREATE TABLE IF NOT EXISTS discrepancy (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  date TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  expected REAL,
  actual REAL,
  unit TEXT,
  question TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  resolution TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_disc_shop_status ON discrepancy(shop_id, status);

CREATE TABLE IF NOT EXISTS import_batch (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  kind TEXT NOT NULL,
  rows INTEGER NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batch_hash ON import_batch(shop_id, sha256);

