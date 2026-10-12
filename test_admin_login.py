import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir('/tmp')
sys.path.insert(0, PROJECT_ROOT)
from app import create_app

app = create_app()
app.testing = True
client = app.test_client()
response = client.post('/admin-login', data={
    'username': 'christalmerth@gmail.com',
    'password': '7826514908',
}, follow_redirects=False)
assert response.status_code in (301, 302), response.status_code
assert '/admin/panel' in response.headers.get('Location', ''), response.headers.get('Location')
print('ADMIN_LOGIN_OK')
