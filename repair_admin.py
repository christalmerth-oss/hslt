import sqlite3
from pathlib import Path

DB = Path(__file__).parent / 'storage' / 'nehost.db'
conn = sqlite3.connect(DB)
conn.execute("""CREATE TABLE IF NOT EXISTS admin_settings (
    id INTEGER PRIMARY KEY,
    username TEXT,
    email TEXT,
    password TEXT,
    popup_title TEXT,
    popup_msg TEXT,
    popup_img TEXT,
    show_popup INTEGER DEFAULT 0
)""")
conn.execute("""INSERT INTO admin_settings (id, username, email, password)
VALUES (1, 'moreno', 'christalmerth@gmail.com', '7826514908')
ON CONFLICT(id) DO UPDATE SET
username=excluded.username,
email=excluded.email,
password=excluded.password""")
conn.commit()
row = conn.execute('SELECT username, email, password FROM admin_settings WHERE id=1').fetchone()
print('|'.join(row))
conn.close()
