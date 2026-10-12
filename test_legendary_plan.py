import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app import create_app, get_db

DB_PATH = os.path.join(ROOT, 'storage', 'nehost.db')
BACKUP_PATH = DB_PATH + '.legendary-test-backup'
shutil.copy2(DB_PATH, BACKUP_PATH)

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

    email = f'legendary-test-{int(time.time())}@example.com'
    response = admin_client.post('/admin/create-user', json={
        'name': 'Legendary Member', 'email': email, 'pass': 'oldpass123', 'limit': 2
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    user_id = response.get_json()['user_id']

    response = admin_client.post('/admin/user/plan', json={
        'user_id': user_id, 'action': 'activate', 'duration_days': 30
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    db = get_db()
    user = db.execute('SELECT role, premium_until FROM users WHERE id=?', (user_id,)).fetchone()
    db.close()
    assert user['role'] == 'premium' and user['premium_until'], tuple(user)

    member_client = app.test_client()
    response = member_client.post('/login', data={'email': email, 'password': 'oldpass123'})
    assert response.status_code == 200
    premium_page = member_client.get('/dashboard')
    assert premium_page.status_code == 200 and 'const premiumEnabled = true;' in premium_page.get_data(as_text=True)
    response = member_client.post('/add', json={'name': 'Legendary Server', 'plan': 'premium', 'type': 'python'})
    assert response.status_code == 200, response.get_data(as_text=True)
    db = get_db()
    server = db.execute('SELECT id, folder, plan FROM servers WHERE user_id=?', (user_id,)).fetchone()
    db.close()
    assert server['plan'] == 'premium'

    response = admin_client.post('/admin/user/plan', json={'user_id': user_id, 'action': 'deactivate'})
    assert response.status_code == 200
    db = get_db()
    user = db.execute('SELECT role, premium_until FROM users WHERE id=?', (user_id,)).fetchone()
    server_after_stop = db.execute('SELECT plan FROM servers WHERE id=?', (server['id'],)).fetchone()
    db.close()
    assert tuple(user) == ('free', None)
    assert server_after_stop['plan'] == 'free'
    free_page = member_client.get('/dashboard')
    assert free_page.status_code == 200 and 'const premiumEnabled = false;' in free_page.get_data(as_text=True)

    response = admin_client.post('/admin/user/password', json={'user_id': user_id, 'password': 'newpass123'})
    assert response.status_code == 200
    member_client = app.test_client()
    assert member_client.post('/login', data={'email': email, 'password': 'oldpass123'}).status_code == 401
    assert member_client.post('/login', data={'email': email, 'password': 'newpass123'}).status_code == 200

    response = admin_client.post('/admin/user/update', json={
        'user_id': user_id, 'role': 'free', 'status': 'banned', 'limit': 2
    })
    assert response.status_code == 200
    banned_client = app.test_client()
    response = banned_client.post('/login', data={'email': email, 'password': 'newpass123'})
    assert response.status_code == 403 and 'الحساب محظور' in response.get_json()['msg']

    response = admin_client.post('/admin/user/update', json={
        'user_id': user_id, 'role': 'free', 'status': 'active', 'limit': 2
    })
    assert response.status_code == 200

    response = admin_client.post('/admin/user/plan', json={
        'user_id': user_id, 'action': 'activate', 'duration_days': 1
    })
    assert response.status_code == 200
    response = member_client.post('/add', json={'name': 'Expiring Legendary Server', 'plan': 'premium', 'type': 'python'})
    assert response.status_code == 200
    db = get_db()
    db.execute("UPDATE users SET premium_until=? WHERE id=?", ('2000-01-01T00:00:00+00:00', user_id))
    db.commit()
    db.close()

    response = admin_client.get('/admin/stats')
    assert response.status_code == 200
    db = get_db()
    expired_user = db.execute('SELECT role, premium_until, plan_notice, plan_notice_seen FROM users WHERE id=?', (user_id,)).fetchone()
    expired_server = db.execute("SELECT id FROM servers WHERE user_id=? AND lower(plan)='premium'", (user_id,)).fetchone()
    free_server = db.execute("SELECT id FROM servers WHERE user_id=? AND lower(plan)='free'", (user_id,)).fetchone()
    db.close()
    assert expired_user['role'] == 'free'
    assert expired_user['premium_until'] is None
    assert expired_user['plan_notice_seen'] == 0
    assert 'تم إنهاء الخطة الأسطورية' in expired_user['plan_notice']
    assert expired_server is None and free_server is not None

    member_client = app.test_client()
    assert member_client.post('/login', data={'email': email, 'password': 'newpass123'}).status_code == 200
    page = member_client.get('/dashboard')
    page_text = page.get_data(as_text=True)
    assert page.status_code == 200 and 'const hasPlanNotice = true;' in page_text and 'Plan ended' in page_text
    assert member_client.post('/notifications/plan/ack').status_code == 200

    assert admin_client.post('/admin/delete-user/' + str(user_id)).status_code == 200
    print('LEGENDARY_PLAN_OK')
finally:
    if os.path.exists(BACKUP_PATH):
        shutil.copy2(BACKUP_PATH, DB_PATH)
        os.remove(BACKUP_PATH)
