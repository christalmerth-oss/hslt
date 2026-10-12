import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from app import create_app

app = create_app()
app.testing = True
client = app.test_client()
with client.session_transaction() as session:
    session['user_id'] = 1
page = client.get('/dashboard')
assert page.status_code == 200
assert 'لوحة الأدمن'.encode('utf-8') in page.data
assert b'fa-shield-halved' in page.data
print('ADMIN_PANEL_VISIBLE_OK')
