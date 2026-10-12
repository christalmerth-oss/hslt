import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from app import create_app

app = create_app()
app.testing = True
client = app.test_client()
response = client.post('/login', data={
    'email': 'christalmerth@gmail.com',
    'password': '7826514908',
})
assert response.status_code == 200, response.data
payload = response.get_json()
assert payload.get('is_admin') is True, payload
assert payload.get('url') == '/admin/panel', payload
with client.session_transaction() as session:
    assert session.get('admin_logged') is True
print('ADMIN_NORMAL_LOGIN_OK')
