import json
from pathlib import Path
import tempfile
import time
import unittest
from inventory_monitor import InventoryService
from inventory_cache import preview, execute


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.service=InventoryService(self.root)
        self.now=time.time();self.old=self.now-40*86400
        self.trash=self.root/'fake-recycle-bin';self.trash.mkdir()
        with self.service.db() as db:
            self.service.put(db,'daily_source_root',str(self.root))
            self.service.put(db,'daily_settings',{'enabled':True})
            self.service.put(db,'history_coverage',{'count':2})
            db.execute('INSERT INTO messages VALUES (?,?,?,?)',('old',None,'{"text":"旧消息"}',self.old))
            db.execute('INSERT INTO messages VALUES (?,?,?,?)',('new',None,'{"text":"新消息"}',self.now))
            for status in ('sent','pending','unknown','failed','partial','sending','cancelled'):
                db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',(status,'{}',self.old,status,0,0))
            db.execute('INSERT INTO checks VALUES (?,?)',('keep-dedup',self.old))
            db.execute('INSERT INTO products VALUES (?,?)',('keep-product','{}'))

    def recycle(self,path):
        path.replace(self.trash/path.name)

    def test_only_old_cache_removed_after_recycling_backup(self):
        plan=preview(self.service,30,now=self.now)
        self.assertEqual(plan['row_count'],3)
        result=execute(self.service,plan,recycler=self.recycle)
        self.assertEqual(result['deleted_rows'],3)
        self.assertFalse(result['errors'])
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT id FROM messages').fetchall(),[('new',)])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checks').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM products').fetchone()[0],1)
            self.assertTrue(self.service.get(db,'daily_settings')['enabled'])
            self.assertIsNone(self.service.get(db,'history_coverage'))
        archive=json.loads(next(self.trash.glob('*.json')).read_text('utf-8'))
        self.assertEqual(len(archive['records']),3)

    def test_recycle_failure_prevents_database_deletion(self):
        def fail(path): raise OSError('no recycle bin')
        result=execute(self.service,preview(self.service,30,now=self.now),recycler=fail)
        self.assertEqual(result['deleted_rows'],0)
        self.assertTrue(result['errors'])
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],2)

    def test_changed_rows_and_new_rows_are_not_deleted(self):
        plan=preview(self.service,30,now=self.now)
        with self.service.db() as db:
            db.execute("UPDATE messages SET data='changed' WHERE id='old'")
            db.execute('INSERT INTO messages VALUES (?,?,?,?)',('new-old',None,'{}',self.old))
        result=execute(self.service,plan,recycler=self.recycle)
        self.assertEqual(result['deleted_rows'],2)
        self.assertEqual(result['skipped'],1)
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],3)

    def test_file_allowlist_preserves_markdown_and_receipts(self):
        import os
        directory=self.root/'weekly_reports';directory.mkdir()
        stem='货源周报_2026-09-01_2026-09-01_120000_123456'
        for suffix in ('.json','.md','.delivery.json'):
            p=directory/(stem+suffix);p.write_text('{}',encoding='utf-8');os.utime(p,(self.old,self.old))
        plan=preview(self.service,30,now=self.now)
        self.assertEqual(plan['file_count'],1)
        execute(self.service,plan,recycler=self.recycle)
        self.assertTrue((directory/(stem+'.md')).exists())
        self.assertTrue((directory/(stem+'.delivery.json')).exists())

    def test_tampered_plan_rejected(self):
        plan=preview(self.service,30,now=self.now)
        plan['files'].append({'path':str(self.root/'daily_ai_key.dat'),'size':1})
        with self.assertRaises(ValueError):execute(self.service,plan,recycler=self.recycle)

    def test_new_scope_and_cutoff(self):
        for days in (7,30,90,0):
            plan=preview(self.service,days,now=self.now+1)
            self.assertEqual(plan['row_count'],4 if days==0 else 0 if days==90 else 3)
        for days in (-1,None,1):
            with self.assertRaises(ValueError):preview(self.service,days)

    def test_completed_record_becoming_pending_is_preserved(self):
        plan=preview(self.service,30,now=self.now)
        with self.service.db() as db:db.execute("UPDATE events SET status='pending' WHERE id='sent'")
        result=execute(self.service,plan,recycler=self.recycle)
        self.assertEqual(result['deleted_rows'],2)
        with self.service.db() as db:self.assertEqual(db.execute("SELECT status FROM events WHERE id='sent'").fetchone()[0],'pending')

    def test_changed_file_preserved(self):
        import os
        folder=self.root/'weekly_reports';folder.mkdir()
        path=folder/'货源周报_2026-09-01.json';path.write_text('old');os.utime(path,(self.old,self.old))
        plan=preview(self.service,30,now=self.now)
        path.write_text('updated')
        result=execute(self.service,plan,recycler=self.recycle)
        self.assertEqual(result['recycled_files'],0)
        self.assertTrue(path.exists())
