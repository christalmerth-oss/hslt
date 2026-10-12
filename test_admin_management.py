import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app import create_app, get_db

DB_PATH = os.path.join(ROOT, 'storage', 'nehost.db')
BACKUP_PATH = DB_PATH + '.admin-test-backup'
shutil.copy2(DB_PATH, BACKUP_PATH)
created_folders = []

try:
    app = create_app()
    app.testing = True
    admin_client = app.test_client()
    with admin_client.session_transaction() as session:
        session['admin_logged'] = True

    db = get_db()
    admin = db.execute("SELECT id FROM users WHERE lower(role)='admin' ORDER BY id LIMIT 1").fetchone()
    db.close()
    assert admin, 'an administrator account is required'

    created_email = f'admin-test-{int(time.time())}@example.com'
    response = admin_client.post('/admin/create-user', json={
        'name': 'Temporary Member', 'email': created_email, 'pass': 'secret123', 'limit': 3
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    user_id = response.get_json()['user_id']

    response = admin_client.post('/admin/user/update', json={
        'user_id': user_id, 'role': 'premium', 'status': 'banned', 'limit': 3
    })
    assert response.status_code == 200
    db = get_db()
    user = db.execute('SELECT role, status, server_limit FROM users WHERE id=?', (user_id,)).fetchone()
    db.close()
    assert tuple(user) == ('premium', 'banned', 3), tuple(user)

    user_client = app.test_client()
    response = user_client.post('/login', data={'email': created_email, 'password': 'secret123'})
    assert response.status_code == 403 and response.get_json()['status'] == 'banned'

    response = admin_client.post('/admin/user/update', json={
        'user_id': user_id, 'role': 'premium', 'status': 'active', 'limit': 3
    })
    assert response.status_code == 200
    response = user_client.post('/login', data={'email': created_email, 'password': 'secret123'})
    assert response.status_code == 200 and response.get_json()['is_admin'] is False

    for index in range(3):
        response = user_client.post('/add', json={
            'name': f'Test Server {index + 1}', 'plan': 'premium', 'type': 'python'
        })
        assert response.status_code == 200, response.get_data(as_text=True)
        db = get_db()
        server = db.execute('SELECT id, folder, plan FROM servers WHERE user_id=? ORDER BY id DESC LIMIT 1', (user_id,)).fetchone()
        db.close()
        created_folders.append(server['folder'])
        assert server['plan'] == 'premium'

    response = user_client.post('/add', json={'name': 'Too Many', 'plan': 'premium'})
    assert response.status_code == 409

    db = get_db()
    servers = db.execute('SELECT id FROM servers WHERE user_id=? ORDER BY id', (user_id,)).fetchall()
    db.close()
    assert len(servers) == 3
    server_id = servers[0]['id']

    response = admin_client.post(f'/admin/suspend-server/{server_id}', json={'status': 'suspended'})
    assert response.status_code == 200
    db = get_db()
    status = db.execute('SELECT server_status FROM servers WHERE id=?', (server_id,)).fetchone()['server_status']
    db.close()
    assert status == 'suspended'

    response = admin_client.post(f'/admin/suspend-server/{server_id}', json={'status': 'active'})
    assert response.status_code == 200

    response = admin_client.get(f'/admin/manage-user/{user_id}')
    assert response.status_code == 200 and b'Suspend' in response.data

    response = admin_client.post(f'/admin/delete-user/{user_id}')
    assert response.status_code == 200
    db = get_db()
    assert db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone() is None
    assert db.execute('SELECT id FROM servers WHERE user_id=?', (user_id,)).fetchone() is None
    db.close()

    assert admin_client.post('/admin/user/update', json={
        'user_id': admin['id'], 'role': 'admin', 'status': 'active', 'limit': 999
    }).status_code == 403
    assert admin_client.post(f"/admin/delete-user/{admin['id']}").status_code == 403
    print('ADMIN_MANAGEMENT_OK')
finally:
    if os.path.exists(BACKUP_PATH):
        shutil.copy2(BACKUP_PATH, DB_PATH)
        os.remove(BACKUP_PATH)
