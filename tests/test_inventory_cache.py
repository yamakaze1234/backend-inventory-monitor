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

    def export_fixture(self):
        """三份有归档凭据的导出：历史、当前和最新；另含未完成下载。"""
        import hashlib, os
        from douyin_inventory import DouyinInventory
        store = DouyinInventory(self.root); ident = store.add_store('缓存测试店')
        folder = store.root/'exports'/ident; folder.mkdir(parents=True)
        downloads = store.root/'managed-downloads'/ident; downloads.mkdir(parents=True)
        groups = []
        for i in range(1,4):
            target = folder/f'2026010{i}_120000_abcdef.xlsx'
            raw = downloads/(str(i)*32+'.xlsx')
            data = ('导出'+str(i)).encode()
            target.write_bytes(data); raw.write_bytes(data)
            evidence = target.with_suffix('.json')
            evidence.write_text(json.dumps(dict(file=str(target),download_file=str(raw),sha256=hashlib.sha256(data).hexdigest())),encoding='utf-8')
            for path in (target,raw,evidence):os.utime(path,(self.old,self.old))
            groups.append((target,raw,evidence))
        unfinished = downloads/('f'*32+'.crdownload');unfinished.write_bytes(b'partial')
        with store.db() as db:
            db.execute('UPDATE stores SET export_file=? WHERE id=?',(str(groups[1][0]),ident))
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)',('manual-note','保留备注'))
        return store, ident, groups, unfinished

    def test_history_export_cache_recycles_only_verified_old_files(self):
        store,ident,groups,unfinished = self.export_fixture()
        plan = preview(self.service,0,now=self.now)
        self.assertEqual({x['path'] for x in plan['files']},{str(x.resolve()) for x in groups[0]})
        result = execute(self.service,plan,recycler=self.recycle)
        self.assertEqual(result['recycled_files'],3)
        self.assertTrue(all(x.exists() for group in groups[1:] for x in group))
        self.assertTrue(unfinished.exists())
        with store.db() as db:
            self.assertEqual(db.execute('SELECT value FROM preferences WHERE key=?',('manual-note',)).fetchone()[0],'保留备注')

    def test_export_becoming_current_after_preview_is_preserved(self):
        store,ident,groups,_ = self.export_fixture()
        plan = preview(self.service,0,now=self.now)
        with store.db() as db:db.execute('UPDATE stores SET export_file=? WHERE id=?',(str(groups[0][0]),ident))
        result = execute(self.service,plan,recycler=self.recycle)
        self.assertEqual(result['recycled_files'],0)
        self.assertTrue(all(x.exists() for x in groups[0]))

    def test_modified_history_workbook_and_unowned_download_are_preserved(self):
        _,_,groups,_ = self.export_fixture()
        groups[0][0].write_bytes(b'user-edited')
        plan = preview(self.service,0,now=self.now)
        self.assertEqual(plan['files'],[])

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
