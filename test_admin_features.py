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
    session['admin_logged'] = True
panel = client.get('/admin')
assert panel.status_code == 302
page = client.get('/admin/panel')
assert page.status_code == 200
for text in (b'Create account', b'Ban', b'Premium', b'Servers', b'Delete'):
    assert text in page.data, text
public_signup = app.test_client().get('/signup')
assert public_signup.status_code == 302
print('ADMIN_FEATURES_OK')
