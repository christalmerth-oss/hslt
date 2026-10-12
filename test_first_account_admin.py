import sqlite3
from pathlib import Path

from app import create_app

create_app()
db = sqlite3.connect(Path(__file__).parent / 'storage' / 'nehost.db')
db.row_factory = sqlite3.Row
first = db.execute('SELECT id, email, role, server_limit FROM users ORDER BY id ASC LIMIT 1').fetchone()
if first:
    assert first['role'].lower() == 'admin', dict(first)
    assert first['server_limit'] == 999, dict(first)
print('FIRST_ACCOUNT_ADMIN_OK' if first else 'NO_USERS_YET')
db.close()
