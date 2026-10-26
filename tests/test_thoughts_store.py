"""Thought storage and v5 copy migration; memory/TemporaryDirectory only."""
from __future__ import annotations

from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engine import deployment
from engine.backup import backup_database, restore_database
from engine.runtime import probe_database
from engine.store import SCHEMA_VERSION, SqliteStore, Store, database_schema_ready, migrate_v5_to_v6


# Frozen v5 DDL: deliberately independent of today's initialization schema.
# Includes every v5 runtime column and the durable control tables.
_V5_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '5');
CREATE TABLE shop (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE workspace_meta (shop_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL DEFAULT 'local',
 profile_id TEXT NOT NULL DEFAULT 'restaurant', timezone TEXT NOT NULL DEFAULT 'local', created_at TEXT NOT NULL);
CREATE TABLE record (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, raw_text TEXT NOT NULL, source TEXT NOT NULL,
 peer TEXT, dedup_ref TEXT, occurred_at TEXT, received_at TEXT NOT NULL, media_path TEXT, media_sha256 TEXT,
 parse_status TEXT NOT NULL DEFAULT 'pending', parser_version TEXT, batch_id TEXT, parse_base_date TEXT,
 context TEXT NOT NULL DEFAULT '', intent TEXT NOT NULL DEFAULT 'unknown', routing_status TEXT NOT NULL DEFAULT 'unclassified',
 profile_id TEXT NOT NULL DEFAULT 'restaurant', ledger_refs TEXT NOT NULL DEFAULT '[]', correction_of TEXT,
 correction_detail TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX idx_record_dedup ON record(shop_id, dedup_ref) WHERE dedup_ref IS NOT NULL;
CREATE INDEX idx_record_time ON record(shop_id, occurred_at);
CREATE INDEX idx_record_correction ON record(shop_id, correction_of);
CREATE TABLE entry (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, date TEXT NOT NULL, type TEXT NOT NULL,
 category TEXT NOT NULL, amount REAL NOT NULL, item TEXT, qty REAL, unit TEXT, note TEXT, source TEXT NOT NULL,
 raw_text TEXT, batch_id TEXT, created_at TEXT NOT NULL, period_month TEXT, platform TEXT NOT NULL DEFAULT '',
 channel TEXT NOT NULL DEFAULT '', amount_basis TEXT NOT NULL DEFAULT 'gross', external_id TEXT,
 source_system TEXT NOT NULL DEFAULT '', granularity TEXT NOT NULL DEFAULT 'daily');
CREATE INDEX idx_entry_shop_date ON entry(shop_id, date);
CREATE TABLE sale (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, date TEXT NOT NULL, product_id TEXT,
 product_name TEXT NOT NULL, qty REAL NOT NULL, amount REAL, source TEXT NOT NULL, batch_id TEXT,
 created_at TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '',
 product_category TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', amount_basis TEXT NOT NULL DEFAULT 'gross',
 external_id TEXT, source_system TEXT NOT NULL DEFAULT '', granularity TEXT NOT NULL DEFAULT 'daily');
CREATE INDEX idx_sale_shop_date ON sale(shop_id, date);
CREATE TABLE observation (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, record_id TEXT, occurred_at TEXT NOT NULL,
 kind TEXT NOT NULL, subject TEXT, measure TEXT NOT NULL, value_num REAL, value_text TEXT, unit TEXT,
 confidence REAL, source_span TEXT, dedup_key TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX idx_obs_dedup ON observation(shop_id, dedup_key);
CREATE INDEX idx_obs_time ON observation(shop_id, occurred_at);
CREATE INDEX idx_obs_kind ON observation(shop_id, kind, measure);
CREATE INDEX idx_obs_subject ON observation(shop_id, subject);
CREATE TABLE product (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]',
 price REAL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', UNIQUE(shop_id, name));
CREATE TABLE ingredient (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, name TEXT NOT NULL,
 aliases TEXT NOT NULL DEFAULT '[]', base_unit TEXT, created_at TEXT NOT NULL, UNIQUE(shop_id, name));
CREATE TABLE recipe_line (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, product_id TEXT NOT NULL,
 ingredient_id TEXT NOT NULL, qty REAL NOT NULL, units REAL NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
 UNIQUE(product_id, ingredient_id));
CREATE TABLE recipe_version (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, product_id TEXT NOT NULL,
 effective_date TEXT NOT NULL, lines TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(shop_id, product_id, effective_date));
CREATE TABLE discrepancy (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL,
 subject TEXT NOT NULL, expected REAL, actual REAL, unit TEXT, question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 resolution TEXT, created_at TEXT NOT NULL, resolved_at TEXT);
CREATE INDEX idx_disc_shop_status ON discrepancy(shop_id, status);
CREATE TABLE import_batch (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
 kind TEXT NOT NULL, rows INTEGER NOT NULL, note TEXT, created_at TEXT NOT NULL);
CREATE INDEX idx_batch_hash ON import_batch(shop_id, sha256);
CREATE TABLE import_identity (shop_id TEXT NOT NULL, identity TEXT NOT NULL, fingerprint TEXT NOT NULL,
 batch_id TEXT NOT NULL, PRIMARY KEY(shop_id, identity));
CREATE TABLE dimension (id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, kind TEXT NOT NULL, canonical TEXT NOT NULL,
 aliases TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, UNIQUE(shop_id, kind, canonical));
CREATE TABLE agent_request (principal_id TEXT NOT NULL, workspace_id TEXT NOT NULL, operation TEXT NOT NULL,
 request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL, lease_token TEXT,
 PRIMARY KEY(principal_id, workspace_id, operation, request_id));
CREATE TABLE agent_import_preview (token TEXT PRIMARY KEY, principal_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
 sha256 TEXT NOT NULL, plan TEXT NOT NULL, created_at TEXT NOT NULL, consumed_request_id TEXT);
"""


class ThoughtStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(':memory:')
        self.addCleanup(self.store.close)
        self.store.ensure_shop('a', owner_id='owner-a')
        self.store.ensure_shop('b', owner_id='owner-b')
        self.raw = '今天营业额1300元。记个想法：明天试南瓜甜品 #新品 #南瓜'
        self.capture = self.store.record_add('a', self.raw, occurred_at='2026-10-26',
                                             received_at='2026-10-26T20:00:00+08:00')
        self.row = {'date': '2026-10-26', 'body': '明天试南瓜甜品',
                    'source_quote': '明天试南瓜甜品', 'tags': ['新品', '南瓜']}

    def add(self, **changes):
        return self.store.thought_add('a', self.capture['id'], [{**self.row, **changes}])[0]

    def test_interface_new_schema_public_rows_and_tag_order(self):
        self.assertTrue({'thought_add', 'thought_for_record', 'thought_list'} <= Store.__abstractmethods__)
        self.assertEqual(SCHEMA_VERSION, 6)
        self.assertTrue(database_schema_ready(self.store.conn))
        self.assertEqual(self.store.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        first = self.add(tags=['南瓜', '新品', 'New', 'new'])
        second = self.add(tags=[], title='南瓜新品')
        rows = self.store.thought_for_record('a', self.capture['id'])
        self.assertEqual([row['id'] for row in rows], [first, second])
        self.assertEqual(rows[0]['tags'], ['南瓜', '新品', 'New', 'new'])
        self.assertEqual(set(rows[0]), {'id', 'workspace_id', 'capture_id', 'date', 'body',
                                      'source_quote', 'tags', 'created_at', 'updated_at'})
        self.assertEqual(rows[0]['workspace_id'], 'a')
        self.assertEqual(rows[0]['created_at'], rows[0]['updated_at'])
        self.assertEqual(rows[1]['title'], '南瓜新品')
        self.assertEqual(rows[1]['tags'], [])
        self.assertEqual(self.store.thought_for_record('b', self.capture['id']), [])
        detail = self.store.record_detail('a', self.capture['id'])
        self.assertEqual(detail['thoughts'], rows)
        self.assertEqual(detail['record']['raw_text'], self.raw)
        self.assertEqual(detail['record']['ledger_refs'], '[]')
        self.assertEqual(detail['entries'], [])
        self.assertIsNone(self.store.record_detail('b', self.capture['id']))

    def test_source_validation_and_batch_rollback(self):
        for workspace, capture in [('b', self.capture['id']), ('a', 'missing')]:
            with self.subTest(workspace=workspace, capture=capture), self.assertRaises(ValueError):
                self.store.thought_add(workspace, capture, [self.row])
        before = self.store.export_all('a')
        for quote in ['南瓜明天', '', None, '   ']:
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                self.store.thought_add('a', self.capture['id'], [self.row, {**self.row, 'source_quote': quote}])
            self.assertEqual(self.store.export_all('a'), before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.thought_add('a', self.capture['id'], [self.row, {**self.row, 'tags': ['重复', '重复']}])
        self.assertEqual(self.store.export_all('a'), before)

    def test_foreign_keys_reject_cross_workspace_and_orphans(self):
        thought = self.add()
        attempts = [
            ('UPDATE thoughts SET shop_id=? WHERE id=?', ('b', thought)),
            ('UPDATE thoughts SET capture_id=? WHERE id=?', ('missing', thought)),
            ('INSERT INTO thought_tags VALUES(?,?,?,?)', ('b', thought, '非法', 0)),
            ('INSERT INTO thought_tags VALUES(?,?,?,?)', ('a', 'missing', '非法', 0)),
            ('DELETE FROM record WHERE id=?', (self.capture['id'],)),
            ('DELETE FROM shop WHERE id=?', ('a',)),
        ]
        for sql, args in attempts:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                with self.store.transaction():
                    self.store.conn.execute(sql, args)
        self.assertEqual(self.store.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_outer_transaction_rolls_back_capture_finance_tags_and_receipt(self):
        before = self.store.export_all('a')
        with self.assertRaisesRegex(RuntimeError, 'receipt failed'):
            with self.store.transaction():
                token = self.store.agent_request_claim('agent', 'a', 'capture', 'new', 'fingerprint')
                capture = self.store.record_add('a', self.raw)
                self.store.add_entries([{'shop_id': 'a', 'date': '2026-10-26', 'type': 'revenue',
                                         'category': '营业额', 'amount': 1300}], record_id=capture['id'])
                self.store.thought_add('a', capture['id'], [self.row])
                self.store.agent_request_finish('agent', 'a', 'capture', 'new', token, {'thoughts': []})
                raise RuntimeError('receipt failed')
        self.assertEqual(self.store.export_all('a'), before)

    def test_indexed_and_tags_literal_unicode_search_and_inclusive_dates(self):
        wanted = self.add(title='Straße 100%_', body='南瓜新品')
        self.add(date='2026-10-25', tags=['新品'])
        self.add(date='2026-10-27', tags=['南瓜'])
        self.add(date='2026-10-28', tags=['新品', '南瓜'])
        other = self.store.record_add('b', self.raw)
        self.store.thought_add('b', other['id'], [self.row])
        result = self.store.thought_list('a', start='2026-10-26', end='2026-10-26', tags=['南瓜', '新品', '新品'])
        self.assertEqual([row['id'] for row in result['items']], [wanted])
        for term in ['STRASSE', '100%_', '南瓜新品']:
            self.assertEqual([row['id'] for row in self.store.thought_list('a', q=term)['items']], [wanted])
        for term in ['.*', "' OR 1=1 --", 'new']:
            self.assertEqual(self.store.thought_list('a', q=term)['items'], [])
        self.assertEqual(self.store.thought_list('a', tags=['missing'])['items'], [])
        self.assertEqual(self.store.thought_list('missing')['items'], [])
        plan = self.store.conn.execute('EXPLAIN QUERY PLAN SELECT thought_id FROM thought_tags '
                                       'WHERE shop_id=? AND tag=?', ('a', '南瓜')).fetchall()
        self.assertIn('idx_thought_tags_lookup', ' '.join(str(tuple(row)) for row in plan))

    def test_keyset_pagination_breaks_same_date_and_timestamp_ties(self):
        with patch('engine.store.now_iso', return_value='2026-10-26T20:00:00'):
            ids = [self.add() for _ in range(8)]
        expected = sorted(ids, reverse=True)
        actual, cursor = [], None
        while True:
            page = self.store.thought_list('a', page_size=3, before=cursor)
            actual.extend(row['id'] for row in page['items'])
            if not page['has_more']:
                self.assertIsNone(page['next_before'])
                break
            cursor = page['next_before']
            self.assertIsInstance(cursor, tuple)
            self.assertEqual(len(cursor), 3)
        self.assertEqual(actual, expected)
        self.assertEqual(self.store.thought_list('a', before=('0001-01-01', '', ''))['items'], [])
        for size in [0, 101, True, '2']:
            with self.assertRaises(ValueError):
                self.store.thought_list('a', page_size=size)
        with self.assertRaises(ValueError):
            self.store.thought_list('a', before=('bad',))

    def test_timeline_counts_previews_search_event_dates_and_existing_cursor(self):
        self.store.add_entries([{'shop_id': 'a', 'date': '2026-10-26', 'type': 'revenue',
                                 'category': '营业额', 'amount': 1300}], record_id=self.capture['id'])
        summary = self.store.summary('a', '2026-10-01', '2026-10-31')
        for number in range(4):
            self.add(date='2026-10-20', title=f'新品试作{number}', body='很长的想法' * 50,
                     tags=['独立标签'])
        other = self.store.record_add('a', '单独原话', occurred_at='2026-10-25')
        first = self.store.record_timeline('a', page_size=1)
        item = first['items'][0]
        self.assertEqual(item['record']['id'], self.capture['id'])
        self.assertEqual(item['projection_counts']['thoughts'], 4)
        self.assertEqual(item['projection_counts']['entries'], 1)
        self.assertEqual(item['ledger_preview'][0]['amount'], 1300)
        self.assertEqual(len(item['thought_preview']), 3)
        self.assertTrue(all(len(row['body']) == 120 for row in item['thought_preview']))
        self.assertEqual(item['event_time'], '2026-10-26')
        self.assertEqual(self.store.summary('a', '2026-10-01', '2026-10-31'), summary)
        cursor = first['next_before']
        self.assertEqual(cursor, (item['event_time'], item['record']['created_at'], self.capture['id']))
        self.assertEqual(self.store.record_timeline('a', before=cursor)['items'][0]['record']['id'], other['id'])
        for term in ['新品试作3', '很长的想法', '独立标签']:
            self.assertEqual(len(self.store.record_timeline('a', q=term)['items']), 1)
        self.assertEqual(len(self.store.record_timeline('a', types='thought')['items']), 1)
        self.assertEqual(self.store.record_timeline('a', types='thought', end='2026-10-20')['items'], [])
        self.assertEqual(self.store.record_timeline('b', types='thought')['items'], [])
        with self.assertRaises(ValueError):
            self.store.record_timeline('a', types='entry')

    def test_export_and_sqlite_backup_restore_include_ordered_tags(self):
        self.add(tags=['第二', '第一'])
        expected = self.store.export_all('a')
        self.assertEqual(expected['schema_version'], 6)
        self.assertEqual(expected['tables']['thoughts'][0]['shop_id'], 'a')
        self.assertEqual([row['position'] for row in expected['tables']['thought_tags']], [0, 1])
        self.assertEqual(self.store.export_all('b')['tables']['thoughts'], [])
        with tempfile.TemporaryDirectory() as tmp:
            source, backup, restored = [str(Path(tmp) / name) for name in ('source.db', 'backup.db', 'restored.db')]
            with closing(sqlite3.connect(source)) as target:
                self.store.conn.backup(target)
            backup_database(source, backup)
            restore_database(backup, restored)
            with closing(SqliteStore(restored, migrate=False)) as store:
                self.assertEqual(store.export_all('a'), expected)
                self.assertEqual(store.thought_for_record('a', self.capture['id'])[0]['tags'], ['第二', '第一'])


class ThoughtMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.destination = self.root / 'v5.db', self.root / 'v6.db'
        self.receipt = '{\n "capture": {"id": "old", "raw_text": "原话不改"}, "duplicate": false\n}'
        with closing(sqlite3.connect(self.source)) as conn:
            conn.executescript(_V5_SCHEMA)
            conn.execute("INSERT INTO shop VALUES('a', '测试店', '2026-10-25')")
            conn.execute("INSERT INTO workspace_meta VALUES('a', 'owner', 'restaurant', 'Asia/Shanghai', '2026-10-25')")
            conn.execute("INSERT INTO record(id, shop_id, raw_text, source, received_at, created_at, ledger_refs) "
                         "VALUES('old', 'a', '原话不改', 'agent', '2026-10-25', '2026-10-25', '[\"entry:e\"]')")
            conn.execute("INSERT INTO entry(id, shop_id, date, type, category, amount, source, created_at) "
                         "VALUES('e', 'a', '2026-10-25', 'revenue', '营业额', 1300, 'agent', '2026-10-25')")
            conn.execute('INSERT INTO agent_request VALUES(?,?,?,?,?,?,?,?)',
                         ('agent', 'a', 'capture', 'old-request', 'exact-old-fingerprint', self.receipt, '2026-10-25', 'old-lease'))
            conn.execute('INSERT INTO agent_import_preview VALUES(?,?,?,?,?,?,?)',
                         ('preview', 'agent', 'a', 'old-sha', '{"rows": []}', '2026-10-25', 'import-request'))
            conn.commit()
            self.assertTrue(database_schema_ready(conn, expected_version=5))
        self.original = self.source.read_bytes()

    def snapshot(self, path):
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='meta'")]
            return {table: conn.execute(f'SELECT * FROM {table}').fetchall() for table in tables}

    def test_migration_preserves_every_legacy_row_and_source_bytes(self):
        before = self.snapshot(self.source)
        result = deployment.migrate_database(str(self.source), str(self.destination))
        self.assertEqual((result['status'], result['source_schema_version'], result['schema_version']), ('migrated', 5, 6))
        self.assertEqual(self.source.read_bytes(), self.original)
        after = self.snapshot(self.destination)
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertEqual(after['thoughts'], [])
        self.assertEqual(after['thought_tags'], [])
        self.assertTrue(probe_database(str(self.destination), 'owner', 'a')['ok'])
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        with closing(SqliteStore(str(self.destination), migrate=False)) as store:
            self.assertEqual(store.conn.execute('SELECT response FROM agent_request').fetchone()[0], self.receipt)
            self.assertEqual(store.agent_request_get('agent', 'a', 'capture', 'old-request')['fingerprint'], 'exact-old-fingerprint')
            store.thought_add('a', 'old', [{'date': '2026-10-26', 'body': '保留原意', 'source_quote': '原话不改'}])
            self.assertEqual(store.record_detail('a', 'old')['record']['raw_text'], '原话不改')
        published = self.destination.read_bytes()
        with self.assertRaises(ValueError):
            deployment.migrate_database(str(self.source), str(self.destination))
        with self.assertRaises(ValueError):
            deployment.migrate_database(str(self.destination), str(self.root / 'another.db'))
        self.assertFalse((self.root / 'another.db').exists())
        self.assertEqual(self.destination.read_bytes(), published)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_startup_probe_and_strict_store_refuse_v5_without_mutation(self):
        self.assertFalse(probe_database(str(self.source), 'owner', 'a')['ok'])
        env = {'SUIKOUJI_DB': str(self.source), 'SUIKOUJI_OWNER_ID': 'owner', 'SUIKOUJI_WORKSPACE_ID': 'a'}
        with self.assertRaises(ValueError):
            deployment.initialize_database(env, allow_existing=True)
        with self.assertRaises(ValueError):
            SqliteStore(str(self.source), migrate=False)
        with self.assertRaises(ValueError):
            SqliteStore(str(self.root / 'missing.db'), migrate=False)
        self.assertFalse((self.root / 'missing.db').exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_failed_migration_rolls_back_schema_and_version(self):
        from engine import store as module
        create = module._create_thought_schema

        def fail_after_ddl(conn):
            create(conn)
            raise RuntimeError('injected migration interruption')

        # Exercise in-place transactional rollback on a disposable copy. SQLite
        # may update header counters during rollback; deployment never opens the
        # actual source writable, so its stronger byte-preservation check follows.
        rollback_copy = self.root / 'rollback.db'
        with closing(sqlite3.connect(self.source)) as source_conn, \
                closing(sqlite3.connect(rollback_copy)) as target_conn:
            source_conn.backup(target_conn)
        with closing(sqlite3.connect(rollback_copy)) as conn:
            with patch('engine.store._create_thought_schema', side_effect=fail_after_ddl):
                with self.assertRaises(RuntimeError):
                    migrate_v5_to_v6(conn)
            self.assertTrue(database_schema_ready(conn, expected_version=5))
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='thoughts'").fetchone())
            self.assertFalse(conn.in_transaction)
        # Copy migration also cleans up its private work file on any failure.
        with patch('engine.store._create_thought_schema', side_effect=fail_after_ddl):
            with self.assertRaises(RuntimeError):
                deployment.migrate_database(str(self.source), str(self.destination))
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.migrate-*')), [])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertTrue(deployment.migrate_database(str(self.source), str(self.destination))['ok'])

    def test_invalid_source_and_post_migration_validation_never_publish(self):
        with patch('engine.deployment.runtime.probe_database', return_value={'ok': False}):
            with self.assertRaises(ValueError):
                deployment.migrate_database(str(self.source), str(self.destination))
        self.assertFalse(self.destination.exists())
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute('ALTER TABLE product RENAME COLUMN category TO broken_category')
            conn.commit()
        invalid_before = self.source.read_bytes()
        with self.assertRaises(ValueError):
            deployment.migrate_database(str(self.source), str(self.destination))
        self.assertEqual(self.source.read_bytes(), invalid_before)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.migrate-*')), [])

    def test_publication_cannot_overwrite_racing_target(self):
        import os
        link = os.link

        def racing_link(source, destination):
            Path(destination).write_bytes(b'concurrent database')
            link(source, destination)

        with patch('engine.deployment.os.link', side_effect=racing_link):
            with self.assertRaises(FileExistsError):
                deployment.migrate_database(str(self.source), str(self.destination))
        self.assertEqual(self.destination.read_bytes(), b'concurrent database')
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob('.migrate-*')), [])

    def test_wal_backup_migrates_committed_pages_and_cli_is_explicit(self):
        with closing(sqlite3.connect(self.source)) as writer:
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            writer.execute("UPDATE shop SET name='WAL 中已提交的名称'")
            writer.commit()
            main_before = self.source.read_bytes()
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(deployment.main(['migrate', str(self.source), str(self.destination)]), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'migrated')
            self.assertEqual(self.source.read_bytes(), main_before)
            self.assertEqual(writer.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '5')
        with closing(SqliteStore(str(self.destination), migrate=False)) as store:
            self.assertEqual(store.workspace_get('a')['name'], 'WAL 中已提交的名称')
            self.assertEqual(store.conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')

    def test_probe_rejects_missing_thought_table_index_or_foreign_key(self):
        deployment.migrate_database(str(self.source), str(self.destination))
        for change, undo in [
            ('DROP INDEX idx_thought_tags_lookup', 'CREATE INDEX idx_thought_tags_lookup ON thought_tags(shop_id, tag, thought_id)'),
            ('ALTER TABLE thoughts RENAME COLUMN body TO wrong_body', 'ALTER TABLE thoughts RENAME COLUMN wrong_body TO body'),
        ]:
            with closing(sqlite3.connect(self.destination)) as conn:
                conn.execute(change)
                conn.commit()
            before = self.destination.read_bytes()
            self.assertFalse(probe_database(str(self.destination))['ok'])
            self.assertEqual(self.destination.read_bytes(), before)
            with closing(sqlite3.connect(self.destination)) as conn:
                conn.execute(undo)
                conn.commit()
        with closing(sqlite3.connect(self.destination)) as conn:
            conn.execute('DROP TABLE thought_tags')
            conn.execute('CREATE TABLE thought_tags(shop_id TEXT, thought_id TEXT, tag TEXT, position INTEGER)')
            conn.execute('CREATE INDEX idx_thought_tags_lookup ON thought_tags(shop_id, tag, thought_id)')
            conn.commit()
        self.assertFalse(probe_database(str(self.destination))['ok'])

    def test_probe_rejects_tag_table_without_uniqueness_and_dangling_rows(self):
        deployment.migrate_database(str(self.source), str(self.destination))
        with closing(sqlite3.connect(self.destination)) as conn:
            conn.execute('DROP TABLE thought_tags')
            conn.execute('CREATE TABLE thought_tags(shop_id TEXT NOT NULL, thought_id TEXT NOT NULL, '
                         'tag TEXT NOT NULL, position INTEGER NOT NULL, '
                         'FOREIGN KEY(shop_id, thought_id) REFERENCES thoughts(shop_id, id))')
            conn.execute('CREATE INDEX idx_thought_tags_lookup ON thought_tags(shop_id, tag, thought_id)')
            conn.commit()
        self.assertFalse(probe_database(str(self.destination))['ok'])
        with closing(sqlite3.connect(self.destination)) as conn:
            conn.execute('CREATE UNIQUE INDEX tag_identity ON thought_tags(shop_id, thought_id, tag)')
            conn.execute('CREATE UNIQUE INDEX tag_order ON thought_tags(shop_id, thought_id, position)')
            conn.commit()
            self.assertTrue(database_schema_ready(conn))
            # External SQLite clients can disable FK enforcement; readiness must
            # still detect corruption instead of presenting cross-workspace tags.
            conn.execute("INSERT INTO thought_tags VALUES('a', 'missing', 'orphan', 0)")
            conn.commit()
        self.assertFalse(probe_database(str(self.destination))['ok'])


if __name__ == '__main__':
    unittest.main()
