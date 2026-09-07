from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')


class State:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / 'scraper.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS items (
          id TEXT PRIMARY KEY, url TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
          source TEXT, product TEXT, model TEXT, published_at TEXT,
          error TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS published_intents (key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0);
        ''')

    def add(self, record):
        with self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO items(id,url,kind,title,created_at) VALUES(?,?,?,?,?)',
                (record['id'],record['url'],record['kind'],record['title'],now()))
        return cur.rowcount == 1

    def count(self):
        return self.db.execute("SELECT count(*) FROM items").fetchone()[0]

    def rows(self, limit):
        return self.db.execute('SELECT * FROM items ORDER BY rowid LIMIT ?', (limit,)).fetchall()

    def update(self, item_id, **values):
        allowed = {'source','product','model','published_at','error','attempts'}
        if not values or set(values) - allowed:
            raise ValueError('Invalid checkpoint fields')
        with self.db:
            self.db.execute('UPDATE items SET '+','.join(k+'=?' for k in values)+' WHERE id=?',
                (*values.values(), item_id))

    def fail(self, row, stage, error):
        # Page exceptions may include DOM/credential values. Retain controlled reason only.
        reason = str(error)[:300] if isinstance(error, ValueError) else type(error).__name__
        self.update(row['id'], error=f'{stage}: {reason}', attempts=row['attempts']+1)

    def name_exists(self, name):
        return any(json.loads(r[0])['name'].casefold() == name.casefold()
                   for r in self.db.execute('SELECT product FROM items WHERE product IS NOT NULL'))

    def claim_budget(self, maximum):
        day = now()[:10]
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO budget(day) VALUES(?)', (day,))
            updated = self.db.execute('UPDATE budget SET attempts=attempts+1 WHERE day=? AND attempts<?', (day,maximum))
            if not updated.rowcount:
                raise ValueError('Daily ingest attempt budget reached; resume tomorrow (UTC).')

    def export(self, path=None):
        path = Path(path or self.directory / 'products.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix+'.tmp')
        with tmp.open('w',encoding='utf-8') as f:
            for row in self.db.execute('SELECT product FROM items WHERE product IS NOT NULL ORDER BY rowid'):
                f.write(row[0]+'\n')
        tmp.replace(path)
        return path

    def summary(self):
        counts = {'discovered':0,'scraped':0,'generated':0,'published':0,'failed':0}
        for row in self.db.execute('SELECT source,product,published_at,error FROM items'):
            counts['discovered'] += 1
            for key,field in [('scraped','source'),('generated','product'),('published','published_at'),('failed','error')]:
                counts[key] += bool(row[field])
        return counts
