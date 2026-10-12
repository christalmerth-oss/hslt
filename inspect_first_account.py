import sqlite3
from pathlib import Path

DB = Path(__file__).parent / 'storage' / 'nehost.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, fname, lname, username, email, role, status, server_limit FROM users ORDER BY id ASC').fetchall()
for row in rows:
    print(dict(row))
conn.close()
