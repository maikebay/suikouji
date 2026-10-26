-- Fixed fixture generated from released 0.1.1 source; synthetic data only.
BEGIN TRANSACTION;
CREATE TABLE agent_import_preview (
  token TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  plan TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_request_id TEXT
);
CREATE TABLE agent_request (
  principal_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  response TEXT,
  created_at TEXT NOT NULL,
  lease_token TEXT,
  PRIMARY KEY(principal_id, workspace_id, operation, request_id)
);
INSERT INTO "agent_request" VALUES('agent','shop','capture','v5-structured','f3a72e4dc46bd1d0c0f2b0c6db185f2e757fcc36eca9162af4b2c1ad11b82f8d','{"duplicate": false, "status": "parsed", "capture": {"id": "rec_7a04700faf38", "shop_id": "shop", "raw_text": "今天营业额1300元，阴天", "source": "agent", "peer": null, "dedup_ref": "agent-v2:3fdc592d1c96e2a005fcb07f34d3953c45130cdea8b56c2dcbeb5f33a74e9ccb", "occurred_at": "2026-10-26", "received_at": "2026-10-26T20:00:00+08:00", "media_path": null, "media_sha256": null, "parse_status": "parsed", "parser_version": "agent-structured-1", "batch_id": null, "parse_base_date": "2026-10-26", "context": "", "intent": "record", "routing_status": "classified", "profile_id": "restaurant", "ledger_refs": "[\"entry:e_cb95300a7ccc\"]", "correction_of": null, "correction_detail": null, "created_at": "2026-10-26T14:59:10.633204"}, "parsed": {"intent": "record", "entries": [{"date": "2026-10-26", "type": "revenue", "category": "营业额", "amount": 1300, "amount_basis": "gross", "granularity": "daily", "assertion": "actual", "source_quote": "今天营业额1300元"}], "sales": [], "leftovers": [], "mode": "agent_structured", "parser_version": "agent-structured-1"}, "entries": [{"id": "e_cb95300a7ccc", "shop_id": "shop", "date": "2026-10-26", "type": "revenue", "category": "营业额", "amount": 1300.0, "item": null, "qty": null, "unit": "", "note": "今天营业额1300元", "source": "agent", "raw_text": "今天营业额1300元，阴天", "batch_id": null, "created_at": "2026-10-26T14:59:10.633278", "period_month": null, "platform": "", "channel": "", "amount_basis": "gross", "external_id": null, "source_system": "", "granularity": "daily"}], "sales": [], "observations": [{"id": "o_432bc07dd22d", "shop_id": "shop", "record_id": "rec_7a04700faf38", "occurred_at": "2026-10-26", "kind": "营业额", "subject": null, "measure": "金额", "value_num": 1300.0, "value_text": null, "unit": "元", "confidence": 1.0, "source_span": "今天营业额1300元", "dedup_key": "entry:e_cb95300a7ccc:0", "created_at": "2026-10-26T14:59:10.633686"}, {"id": "o_27ac01f5a9ce", "shop_id": "shop", "record_id": "rec_7a04700faf38", "occurred_at": "2026-10-26", "kind": "天气", "subject": null, "measure": "天气", "value_num": null, "value_text": "阴", "unit": "", "confidence": 1.0, "source_span": "阴天", "dedup_key": "record:e8e6416f8db5b352c5b59f475df73d1fa6e87ba390be66ec0b6db14461831a5e:0", "created_at": "2026-10-26T14:59:10.633787"}], "warnings": [], "provenance": {"source": "agent", "principal_id": "agent", "request_id": "v5-structured", "dedup_ref": "agent-v2:3fdc592d1c96e2a005fcb07f34d3953c45130cdea8b56c2dcbeb5f33a74e9ccb", "workspace_id": "shop"}, "normalization": {"entries": [{"date": "2026-10-26", "type": "revenue", "category": "营业额", "amount": 1300, "amount_basis": "gross", "granularity": "daily", "assertion": "actual", "source_quote": "今天营业额1300元"}], "sales": [], "observations": [{"date": "2026-10-26", "kind": "天气", "measure": "天气", "value_text": "阴", "assertion": "actual", "source_quote": "阴天"}], "leftovers": []}}','2026-10-26T14:59:10.632887','claim_ae83fcfed8ac');
INSERT INTO "agent_request" VALUES('agent','shop','capture','v5-native','d2205096d0ab8b860abeb23e0198df29ecb0d6fdb6660d26213a57486fd467a5','{"duplicate": false, "status": "unparsed", "capture": {"id": "rec_c86e5c161f47", "shop_id": "shop", "raw_text": "记个想法：明天试南瓜甜品", "source": "agent", "peer": null, "dedup_ref": "agent-v2:77a3a621211f2bf439154f16139044f567738847848553b3886db1057f42c252", "occurred_at": "2026-10-27", "received_at": "2026-10-26T20:00:00+08:00", "media_path": null, "media_sha256": null, "parse_status": "unparsed", "parser_version": "observe-1", "batch_id": null, "parse_base_date": "2026-10-26", "context": "", "intent": "unknown", "routing_status": "unclassified", "profile_id": "restaurant", "ledger_refs": "[]", "correction_of": null, "correction_detail": null, "created_at": "2026-10-26T14:59:10.634319"}, "parsed": {"intent": "unknown", "entries": [], "sales": [], "leftovers": ["记个想法：", "明天试南瓜甜品"], "raw": "记个想法：明天试南瓜甜品"}, "entries": [], "sales": [], "observations": [], "warnings": [], "provenance": {"source": "agent", "principal_id": "agent", "request_id": "v5-native", "dedup_ref": "agent-v2:77a3a621211f2bf439154f16139044f567738847848553b3886db1057f42c252", "workspace_id": "shop"}}','2026-10-26T14:59:10.634125','claim_ec928c2ab949');
CREATE TABLE dimension (
  id         TEXT PRIMARY KEY,
  shop_id    TEXT NOT NULL,
  kind       TEXT NOT NULL,
  canonical  TEXT NOT NULL,
  aliases    TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(shop_id, kind, canonical)
);
CREATE TABLE discrepancy (
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
CREATE TABLE entry (
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
, period_month TEXT, platform TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '', amount_basis TEXT NOT NULL DEFAULT 'gross', external_id TEXT, source_system TEXT NOT NULL DEFAULT '', granularity TEXT NOT NULL DEFAULT 'daily');
INSERT INTO "entry" VALUES('e_cb95300a7ccc','shop','2026-10-26','revenue','营业额',1300.0,NULL,NULL,'','今天营业额1300元','agent','今天营业额1300元，阴天',NULL,'2026-10-26T14:59:10.633278',NULL,'','','gross',NULL,'','daily');
CREATE TABLE import_batch (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  kind TEXT NOT NULL,
  rows INTEGER NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE import_identity (
                shop_id TEXT NOT NULL, identity TEXT NOT NULL, fingerprint TEXT NOT NULL,
                batch_id TEXT NOT NULL, PRIMARY KEY(shop_id, identity));
CREATE TABLE ingredient (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  base_unit TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(shop_id, name)
);
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT INTO "meta" VALUES('schema_version','5');
CREATE TABLE observation (
  id          TEXT PRIMARY KEY,
  shop_id     TEXT NOT NULL,
  record_id   TEXT,
  occurred_at TEXT NOT NULL,
  kind        TEXT NOT NULL,
  subject     TEXT,
  measure     TEXT NOT NULL,
  value_num   REAL,
  value_text  TEXT,
  unit        TEXT,
  confidence  REAL,
  source_span TEXT,
  dedup_key   TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
INSERT INTO "observation" VALUES('o_432bc07dd22d','shop','rec_7a04700faf38','2026-10-26','营业额',NULL,'金额',1300.0,NULL,'元',1.0,'今天营业额1300元','entry:e_cb95300a7ccc:0','2026-10-26T14:59:10.633686');
INSERT INTO "observation" VALUES('o_27ac01f5a9ce','shop','rec_7a04700faf38','2026-10-26','天气',NULL,'天气',NULL,'阴','',1.0,'阴天','record:e8e6416f8db5b352c5b59f475df73d1fa6e87ba390be66ec0b6db14461831a5e:0','2026-10-26T14:59:10.633787');
CREATE TABLE product (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  price REAL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
  UNIQUE(shop_id, name)
);
CREATE TABLE recipe_line (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  ingredient_id TEXT NOT NULL,
  qty REAL NOT NULL,
  units REAL NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(product_id, ingredient_id)
);
CREATE TABLE recipe_version (
                id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, product_id TEXT NOT NULL,
                effective_date TEXT NOT NULL, lines TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(shop_id, product_id, effective_date));
CREATE TABLE record (
  id             TEXT PRIMARY KEY,
  shop_id        TEXT NOT NULL,
  raw_text       TEXT NOT NULL,
  source         TEXT NOT NULL,
  peer           TEXT,
  dedup_ref      TEXT,
  occurred_at    TEXT,
  received_at    TEXT NOT NULL,
  media_path     TEXT,
  media_sha256   TEXT,
  parse_status   TEXT NOT NULL DEFAULT 'pending',
  parser_version TEXT,
  batch_id       TEXT,
  parse_base_date TEXT,
  context        TEXT NOT NULL DEFAULT '',
  intent         TEXT NOT NULL DEFAULT 'unknown',
  routing_status TEXT NOT NULL DEFAULT 'unclassified',
  profile_id     TEXT NOT NULL DEFAULT 'restaurant',
  ledger_refs    TEXT NOT NULL DEFAULT '[]',
  -- 修正来源：本条原话改的是哪一行账目（entry:<id> / sale:<id>），
  -- correction_detail 存改动前后的字段值，账目本身不保留旧值也能追溯。
  correction_of  TEXT,
  correction_detail TEXT,
  created_at     TEXT NOT NULL
);
INSERT INTO "record" VALUES('rec_7a04700faf38','shop','今天营业额1300元，阴天','agent',NULL,'agent-v2:3fdc592d1c96e2a005fcb07f34d3953c45130cdea8b56c2dcbeb5f33a74e9ccb','2026-10-26','2026-10-26T20:00:00+08:00',NULL,NULL,'parsed','agent-structured-1',NULL,'2026-10-26','','record','classified','restaurant','["entry:e_cb95300a7ccc"]',NULL,NULL,'2026-10-26T14:59:10.633204');
INSERT INTO "record" VALUES('rec_c86e5c161f47','shop','记个想法：明天试南瓜甜品','agent',NULL,'agent-v2:77a3a621211f2bf439154f16139044f567738847848553b3886db1057f42c252','2026-10-27','2026-10-26T20:00:00+08:00',NULL,NULL,'unparsed','observe-1',NULL,'2026-10-26','','unknown','unclassified','restaurant','[]',NULL,NULL,'2026-10-26T14:59:10.634319');
CREATE TABLE sale (
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
, platform TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '', product_category TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', amount_basis TEXT NOT NULL DEFAULT 'gross', external_id TEXT, source_system TEXT NOT NULL DEFAULT '', granularity TEXT NOT NULL DEFAULT 'daily');
CREATE TABLE shop (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
INSERT INTO "shop" VALUES('shop','v5 migration fixture','2026-10-26T14:59:10.632496');
CREATE TABLE workspace_meta (
  shop_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL DEFAULT 'local',
  profile_id TEXT NOT NULL DEFAULT 'restaurant',
  timezone TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL
);
INSERT INTO "workspace_meta" VALUES('shop','owner','restaurant','Asia/Shanghai','2026-10-26T14:59:10.632526');
CREATE INDEX idx_entry_shop_date ON entry(shop_id, date);
CREATE INDEX idx_sale_shop_date ON sale(shop_id, date);
CREATE INDEX idx_disc_shop_status ON discrepancy(shop_id, status);
CREATE INDEX idx_batch_hash ON import_batch(shop_id, sha256);
CREATE UNIQUE INDEX idx_record_dedup
  ON record(shop_id, dedup_ref) WHERE dedup_ref IS NOT NULL;
CREATE INDEX idx_record_time ON record(shop_id, occurred_at);
CREATE UNIQUE INDEX idx_obs_dedup ON observation(shop_id, dedup_key);
CREATE INDEX idx_obs_time ON observation(shop_id, occurred_at);
CREATE INDEX idx_obs_kind ON observation(shop_id, kind, measure);
CREATE INDEX idx_obs_subject ON observation(shop_id, subject);
CREATE INDEX idx_record_correction ON record(shop_id, correction_of);
COMMIT;
