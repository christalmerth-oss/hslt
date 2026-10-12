import os, sqlite3, zipfile, subprocess, signal, shutil, time, datetime, random, re, json, sys, threading
try:
    import psutil
except ImportError:
    psutil = None
    print("[!] psutil غير متاح. بعض ميزات المراقبة ستكون معطلة")
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit

# Global process tracker
running_procs = {}
start_times = {}
PUBLIC_IP = '78.154.103.26'
PUBLIC_PORT_MIN = 13506
PUBLIC_PORT_MAX = 13650
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'moreno')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'christalmerth@gmail.com')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '7826514908')

# Initialize SocketIO
socketio = SocketIO()

def get_db():
    # Always use the database shipped with this application, regardless of launch cwd.
    db_path = os.path.join(PROJECT_ROOT, 'storage', 'nehost.db')
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    storage_path = os.path.join(PROJECT_ROOT, 'storage')
    if not os.path.exists(storage_path): os.makedirs(storage_path)
    db = get_db()
    # User Table
    db.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        fname TEXT, lname TEXT, username TEXT, email TEXT, password TEXT, pfp TEXT DEFAULT 'default.png',
        role TEXT DEFAULT 'free', 
        status TEXT DEFAULT 'active',
        server_limit INTEGER DEFAULT 1,
        notifications TEXT DEFAULT '',
        premium_until TEXT DEFAULT NULL,
        plan_notice TEXT DEFAULT '',
        plan_notice_seen INTEGER DEFAULT 1
    )''')
    # Safe migration for databases created by older versions.
    user_columns = {row[1] for row in db.execute('PRAGMA table_info(users)').fetchall()}
    if 'premium_until' not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN premium_until TEXT")
    if 'plan_notice' not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN plan_notice TEXT DEFAULT ''")
    if 'plan_notice_seen' not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN plan_notice_seen INTEGER DEFAULT 1")

    # Server Table
    # Migration: on an existing installation, preserve the first registered account as admin.
    first_user = db.execute('SELECT id FROM users ORDER BY id ASC LIMIT 1').fetchone()
    has_admin = db.execute("SELECT id FROM users WHERE lower(role)='admin' LIMIT 1").fetchone()
    if first_user and not has_admin:
        db.execute("UPDATE users SET role='admin', server_limit=999 WHERE id=?", (first_user['id'],))

    db.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, name TEXT, folder TEXT, 
        status TEXT, startup TEXT, pid INTEGER,
        server_status TEXT DEFAULT 'active',
        public_ip TEXT DEFAULT '78.154.103.26',
        public_port INTEGER,
        description TEXT DEFAULT '',
        plan TEXT DEFAULT 'free',
        server_type TEXT DEFAULT 'python',
        custom_domain TEXT DEFAULT '',
        demo_ip TEXT DEFAULT ''
    )''')
    # Safe migration for databases created by older versions.
    server_columns = {row[1] for row in db.execute('PRAGMA table_info(servers)').fetchall()}
    if 'public_ip' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN public_ip TEXT DEFAULT '78.154.103.26'")
    if 'public_port' not in server_columns:
        db.execute('ALTER TABLE servers ADD COLUMN public_port INTEGER')
    if 'description' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN description TEXT DEFAULT ''")
    if 'plan' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN plan TEXT DEFAULT 'free'")
    if 'server_type' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN server_type TEXT DEFAULT 'python'")
    if 'custom_domain' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN custom_domain TEXT DEFAULT ''")
    if 'demo_ip' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN demo_ip TEXT DEFAULT ''")
    for demo_row in db.execute("SELECT id, demo_ip FROM servers").fetchall():
        if not (demo_row['demo_ip'] or '').strip():
            demo = f"10.20.{(demo_row['id'] // 250) % 250}.{(demo_row['id'] % 250) + 1}"
            db.execute('UPDATE servers SET demo_ip=? WHERE id=?', (demo, demo_row['id']))
    existing_ports = {row[0] for row in db.execute('SELECT public_port FROM servers WHERE public_port IS NOT NULL').fetchall()}
    legacy_servers = db.execute('SELECT id FROM servers WHERE public_port IS NULL').fetchall()
    for legacy in legacy_servers:
        candidate = next((p for p in range(PUBLIC_PORT_MIN, PUBLIC_PORT_MAX + 1) if p not in existing_ports), None)
        if candidate is not None:
            db.execute('UPDATE servers SET public_ip=?, public_port=? WHERE id=?', (PUBLIC_IP, candidate, legacy[0]))
            existing_ports.add(candidate)
    # Support Ticket Table
    db.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, subject TEXT, message TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Admin Settings Table
    db.execute('''CREATE TABLE IF NOT EXISTS admin_settings (
        id INTEGER PRIMARY KEY, 
        username TEXT, email TEXT, password TEXT,
        popup_title TEXT, popup_msg TEXT, popup_img TEXT, show_popup INTEGER DEFAULT 0
    )''')
    
    admin_columns = {row[1] for row in db.execute('PRAGMA table_info(admin_settings)').fetchall()}
    if 'email' not in admin_columns:
        db.execute("ALTER TABLE admin_settings ADD COLUMN email TEXT")
    if not db.execute('SELECT * FROM admin_settings WHERE id=1').fetchone():
        db.execute('INSERT INTO admin_settings (id, username, email, password) VALUES (1, ?, ?, ?)', (ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD))
    else:
        db.execute('UPDATE admin_settings SET username=?, email=?, password=? WHERE id=1', (ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD))
    
    db.commit()
    db.close()

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'nehost_ultra_pro_max_99'
    app.config['BASE_STORAGE'] = os.path.join(os.getcwd(), 'storage/instances')
    app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'static/uploads')

    @app.before_request
    def enforce_server_isolation():
        # Every server/file endpoint is restricted to the owning account.
        if request.path.startswith('/server/') or request.path.startswith('/files/'):
            folder = (request.view_args or {}).get('folder')
            if not folder: return None
            if session.get('admin_logged'): return None
            user_id = session.get('user_id')
            if not user_id: return jsonify({'status': 'error', 'msg': 'Authentication required.'}), 401
            db_guard = get_db()
            owned = db_guard.execute('SELECT 1 FROM servers WHERE folder=? AND user_id=?', (folder, user_id)).fetchone()
            db_guard.close()
            if not owned: return jsonify({'status': 'error', 'msg': 'Access denied for this server.'}), 403
            supplied_paths = [request.args.get('path', ''), request.form.get('path', '')]
            payload_guard = request.get_json(silent=True) or {}
            supplied_paths.append(payload_guard.get('path', ''))
            for relative in supplied_paths:
                normalized = os.path.normpath(str(relative or ''))
                if os.path.isabs(str(relative or '')) or normalized == '..' or normalized.startswith('..' + os.sep):
                    return jsonify({'status': 'error', 'msg': 'Unsafe path rejected.'}), 400
            route_name = (request.view_args or {}).get('name', '')
            if route_name and (route_name in {'.', '..'} or os.path.basename(route_name) != route_name or '\\' in route_name):
                return jsonify({'status': 'error', 'msg': 'Unsafe file name rejected.'}), 400
            for candidate in (payload_guard.get('old', ''), payload_guard.get('new', '')):
                if candidate and (os.path.basename(str(candidate)) != str(candidate) or str(candidate) in {'.', '..'}):
                    return jsonify({'status': 'error', 'msg': 'Unsafe file name rejected.'}), 400
            for candidate in payload_guard.get('names', []) or []:
                if os.path.basename(str(candidate)) != str(candidate) or str(candidate) in {'.', '..'}:
                    return jsonify({'status': 'error', 'msg': 'Unsafe file name rejected.'}), 400
        return None

    if not os.path.exists(app.config['BASE_STORAGE']):
        os.makedirs(app.config['BASE_STORAGE'])
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])

    def remove_user_servers(db, user_id, premium_only=False):
        """Stop and remove servers owned by one account, optionally only Premium servers."""
        query = 'SELECT folder, pid FROM servers WHERE user_id=?'
        params = [user_id]
        if premium_only:
            query += " AND lower(COALESCE(plan, 'free'))='premium'"
        rows = db.execute(query, params).fetchall()
        for server in rows:
            folder = server['folder']
            tracked = running_procs.get(folder)
            pid = tracked.pid if tracked else server['pid']
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            running_procs.pop(folder, None)
            start_times.pop(folder, None)
            path = os.path.join(app.config['BASE_STORAGE'], folder)
            if os.path.exists(path):
                shutil.rmtree(path)
        if premium_only:
            db.execute("DELETE FROM servers WHERE user_id=? AND lower(COALESCE(plan, 'free'))='premium'", (user_id,))
        else:
            db.execute('DELETE FROM servers WHERE user_id=?', (user_id,))

    def expire_premium_accounts():
        """Downgrade expired plans, remove their servers, and queue a one-time notice."""
        now = datetime.datetime.now(datetime.timezone.utc)
        db = get_db()
        rows = db.execute("SELECT id, premium_until FROM users WHERE lower(role)='premium' AND premium_until IS NOT NULL AND premium_until != ''").fetchall()
        changed = False
        for user in rows:
            try:
                expiry = datetime.datetime.fromisoformat(str(user['premium_until']).replace('Z', '+00:00'))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=datetime.timezone.utc)
                expiry = expiry.astimezone(datetime.timezone.utc)
            except (TypeError, ValueError):
                continue
            if expiry <= now:
                remove_user_servers(db, user['id'], premium_only=True)
                db.execute("UPDATE users SET role='free', premium_until=NULL, plan_notice=?, plan_notice_seen=0 WHERE id=?", ('تم إنهاء الخطة الأسطورية، وتم حذف السيرفرات المرتبطة بها.', user['id']))
                changed = True
        if changed:
            db.commit()
        db.close()

    init_db()
    socketio.init_app(app)

    @app.before_request
    def expire_plans_before_request():
        expire_premium_accounts()
        user_id = session.get('user_id')
        if not user_id or request.endpoint in {'login', 'admin_login'}:
            return None
        db_guard = get_db()
        user_guard = db_guard.execute('SELECT status FROM users WHERE id=?', (user_id,)).fetchone()
        db_guard.close()
        if user_guard and user_guard['status'] == 'banned':
            session.clear()
            return redirect(url_for('login', notice='banned'))
        return None

    def run_plan_expiry_worker():
        while True:
            try:
                expire_premium_accounts()
            except Exception:
                pass
            time.sleep(60)

    if os.environ.get('NEHOST_DISABLE_EXPIRY_WORKER') != '1':
        threading.Thread(target=run_plan_expiry_worker, name='nehost-plan-expiry', daemon=True).start()

    def get_docker_stats(folder):
        """Return live Docker metrics for one isolated container, or None when unavailable."""
        container = f"ne-server-{folder}"
        try:
            result = subprocess.run(
                ['docker', 'stats', '--no-stream', '--format', '{{json .}}', container],
                capture_output=True, text=True, timeout=3
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout.strip().splitlines()[-1])
            mem_usage = str(data.get('MemUsage') or '0MB').split('/', 1)[0].strip()
            mem_percent = str(data.get('MemPerc') or '').strip()
            return {'cpu': str(data.get('CPUPerc') or '0.00%'), 'ram': f"{mem_usage} {mem_percent}".strip(), 'source': 'docker'}
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def server_resource_limits(plan):
        return {'cpu_percent': 100.0 if (plan or 'free').lower() == 'free' else 200.0, 'ram_mb': 512 if (plan or 'free').lower() == 'free' else 2048, 'disk_mb': 1024 if (plan or 'free').lower() == 'free' else 10240}

    def parse_memory_mb(value):
        try:
            text = str(value or '0').split('%', 1)[0].strip().split()[0].upper()
            multiplier = 1
            if text.endswith('GIB') or text.endswith('GB'): multiplier, text = 1024, text[:-3] if text.endswith('GIB') else text[:-2]
            elif text.endswith('MIB') or text.endswith('MB'): text = text[:-3] if text.endswith('MIB') else text[:-2]
            elif text.endswith('KIB') or text.endswith('KB'): multiplier, text = 1 / 1024, text[:-3] if text.endswith('KIB') else text[:-2]
            return float(text) * multiplier
        except (TypeError, ValueError): return 0.0

    def folder_disk_mb(path):
        total = 0
        try:
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in {'.venv', 'node_modules', '__pycache__'}]
                for filename in files:
                    try: total += os.path.getsize(os.path.join(root, filename))
                    except OSError: pass
        except OSError: pass
        return round(total / (1024 * 1024), 1)

    def get_precise_uptime(start_timestamp):
        if not start_timestamp: return "Offline"
        diff = int(time.time() - start_timestamp)
        months, rem = divmod(diff, 2592000)
        days, rem = divmod(rem, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        
        parts = []
        if months > 0: parts.append(f"{months}mo")
        if days > 0: parts.append(f"{days}d")
        if hours > 0: parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)
    
    @app.route('/')
    def home():
      return render_template('index.html')

    # --- UPDATED SIGNUP ROUTE ---
    @app.route('/signup', methods=['GET', 'POST'])
    def signup():
        # The first account is the bootstrap administrator; later public signup is disabled.
        count_db = get_db()
        account_count = count_db.execute('SELECT COUNT(*) AS total FROM users').fetchone()['total']
        count_db.close()
        if not session.get('admin_logged'):
            if request.method == 'POST':
                return jsonify({'status': 'error', 'msg': 'Public account creation is disabled.'}), 403
            return redirect(url_for('login'))
        if request.method == 'POST':
            fname = request.form.get('fname')
            lname = request.form.get('lname')
            username = request.form.get('username')
            email = request.form.get('email')
            pwd = request.form.get('password')
            cpwd = request.form.get('confirm_password')
            pfp = request.files.get('pfp')

            if pwd != cpwd:
                return jsonify({'status': 'error', 'msg': 'Passwords do not match!'}), 400

            db = get_db()
            # Check if user already exists
            existing_user = db.execute('SELECT id FROM users WHERE email=? OR username=?', (email, username)).fetchone()
            if existing_user:
                db.close()
                return jsonify({'status': 'error', 'msg': 'Email or Username already taken!'}), 400

            pfp_name = 'default.png'
            if pfp:
                pfp_name = secure_filename(pfp.filename)
                pfp.save(os.path.join(app.config['UPLOAD_FOLDER'], pfp_name))

            # The first account becomes the sole bootstrap administrator.
            is_first_account = db.execute('SELECT COUNT(*) AS total FROM users').fetchone()['total'] == 0
            account_role = 'admin' if is_first_account else 'free'
            account_limit = 999 if is_first_account else 1
            db.execute('''INSERT INTO users
                (fname, lname, username, email, password, pfp, server_limit, role, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (fname, lname, username, email, pwd, pfp_name, account_limit, account_role, 'active'))
            
            db.commit()
            db.close()
            return jsonify({'status': 'success', 'url': url_for('login'), 'is_admin': is_first_account})
        
        return render_template('web/signup.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = request.form.get('email')
            pwd = request.form.get('password')
            email = (email or '').strip().lower()
            pwd = (pwd or '').strip()
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE (lower(email)=? OR lower(username)=?) AND password=?', (email, email, pwd)).fetchone()
            if not user and email == ADMIN_EMAIL.lower() and pwd == ADMIN_PASSWORD:
                # Bootstrap/login recovery: ensure the configured admin also has a user account.
                user = db.execute('SELECT * FROM users WHERE lower(email)=?', (ADMIN_EMAIL.lower(),)).fetchone()
                if user:
                    db.execute("UPDATE users SET role='admin', status='active', server_limit=999 WHERE id=?", (user['id'],))
                    db.commit()
                    user = db.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
                else:
                    db.execute('INSERT INTO users (fname, username, email, password, server_limit, role, status) VALUES (?,?,?,?,?,?,?)', ('Administrator', ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD, 999, 'admin', 'active'))
                    db.commit()
                    user = db.execute('SELECT * FROM users WHERE lower(email)=?', (ADMIN_EMAIL.lower(),)).fetchone()
            db.close()
            
            if user:
                if user['status'] == 'banned':
                    session.clear()
                    return jsonify({'status': 'banned', 'msg': 'هذا الحساب محظور ولا يمكنه تسجيل الدخول.'}), 403
                session['user_id'] = user['id']
                is_admin = (user['role'] or '').lower() == 'admin'
                if is_admin:
                    session['admin_logged'] = True
                return jsonify({'status': 'success', 'url': url_for('admin_panel' if is_admin else 'dashboard'), 'is_admin': is_admin}), 200
            else:
                return jsonify({'status': 'error', 'msg': 'Invalid credentials!'}), 401
        return render_template('web/login.html')

    @app.route('/my-files')
    def my_files():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return render_template('web/files.html')

    @app.route('/guide')
    def guide():
        if 'user_id' not in session: return redirect(url_for('login'))
        return render_template('web/guide.html')

    @app.route('/dashboard')
    def dashboard():
        if 'user_id' not in session: return redirect(url_for('login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        db.close()
        if not user or user['status'] != 'active':
            session.clear()
            return redirect(url_for('login'))
        return render_template('web/dashboard.html', user=user)

    @app.route('/profile/update', methods=['POST'])
    def update_profile():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        uid = session['user_id']
        fname = request.form.get('fname')
        lname = request.form.get('lname')
        pwd = request.form.get('password')
        db = get_db()
        if pwd:
            db.execute('UPDATE users SET fname=?, lname=?, password=? WHERE id=?', (fname, lname, pwd, uid))
        else:
            db.execute('UPDATE users SET fname=?, lname=? WHERE id=?', (fname, lname, uid))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/profile/details', methods=['POST'])
    def update_profile_details():
        if 'user_id' not in session: return jsonify({'status': 'error', 'msg': 'Authentication required.'}), 401
        data = request.get_json(silent=True) or {}
        fname = (data.get('fname') or '').strip()[:60]
        lname = (data.get('lname') or '').strip()[:60]
        if not fname: return jsonify({'status': 'error', 'msg': 'First name is required.'}), 400
        db = get_db(); db.execute('UPDATE users SET fname=?, lname=? WHERE id=?', (fname, lname, session['user_id'])); db.commit(); db.close()
        return jsonify({'status': 'success', 'msg': 'Profile updated successfully.'})

    @app.route('/account-settings')
    def account_settings():
        if 'user_id' not in session: return redirect(url_for('login'))
        db = get_db(); user = db.execute('SELECT fname, lname, email, username, pfp FROM users WHERE id=?', (session['user_id'],)).fetchone(); db.close()
        return render_template('web/account_settings.html', user=user)

    @app.route('/profile/password', methods=['POST'])
    def change_password():
        if 'user_id' not in session: return jsonify({'status': 'error', 'msg': 'Authentication required.'}), 401
        data = request.get_json(silent=True) or {}
        current = data.get('current_password') or ''; new_password = data.get('new_password') or ''; confirm = data.get('confirm_password') or ''
        if len(new_password) < 6: return jsonify({'status': 'error', 'msg': 'New password must be at least 6 characters.'}), 400
        if new_password != confirm: return jsonify({'status': 'error', 'msg': 'Passwords do not match.'}), 400
        db = get_db(); user = db.execute('SELECT password FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if not user or user['password'] != current:
            db.close(); return jsonify({'status': 'error', 'msg': 'Current password is incorrect.'}), 400
        db.execute('UPDATE users SET password=? WHERE id=?', (new_password, session['user_id'])); db.commit(); db.close()
        return jsonify({'status': 'success', 'msg': 'Password updated successfully.'})

    @app.route('/server-status/<folder>')
    def public_server_status(folder):
        db = get_db(); srv = db.execute('SELECT name, folder, public_ip, public_port, demo_ip, server_status, pid, server_type FROM servers WHERE folder=?', (folder,)).fetchone(); db.close()
        if not srv: return render_template('web/server_status.html', server=None), 404
        online = False
        if srv['pid'] and psutil.pid_exists(srv['pid']):
            try: online = psutil.Process(srv['pid']).is_running()
            except Exception: online = False
        if folder in running_procs and running_procs[folder].poll() is None: online = True
        return render_template('web/server_status.html', server={**dict(srv), 'online': online})

    @app.route('/ticket/create', methods=['POST'])
    def create_ticket():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('INSERT INTO tickets (user_id, subject, message) VALUES (?,?,?)', (session['user_id'], d['subject'], d['message']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/api/announcement')
    def get_announcement():
        db = get_db()
        conf = db.execute('SELECT popup_title, popup_msg, popup_img, show_popup FROM admin_settings WHERE id=1').fetchone()
        db.close()
        return jsonify(dict(conf))

    @app.route('/admin-login', methods=['GET', 'POST'])
    def admin_login():
        if request.method == 'POST':
            user = (request.form.get('username') or '').strip().lower()
            pwd = (request.form.get('password') or '').strip()
            db = get_db()
            admin = db.execute('SELECT * FROM admin_settings WHERE (lower(username)=? OR lower(email)=?) AND password=?', (user, user, pwd)).fetchone()
            db.close()
            # Accept the configured administrator credentials even if an older database is present.
            configured_admin = user == ADMIN_EMAIL.lower() and pwd == ADMIN_PASSWORD
            if admin or configured_admin:
                session['admin_logged'] = True
                return redirect(url_for('admin_panel'))
        return render_template('web/admin_login.html')

    @app.route('/admin')
    def admin_home():
        return redirect(url_for('admin_panel'))

    @app.route('/admin/panel')
    def admin_panel():
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/admin_panel.html')

    @app.route('/admin/stats')
    def admin_stats():
        if not session.get('admin_logged'): return jsonify({})
        db = get_db()
        users = db.execute('SELECT * FROM users').fetchall()
        user_list = []
        total_cpu = psutil.cpu_percent()
        total_ram = psutil.virtual_memory().percent
        total_servers = db.execute('SELECT COUNT(*) AS total FROM servers').fetchone()['total']
        active_servers = 0
        for u in users:
            srvs = db.execute('SELECT * FROM servers WHERE user_id=?', (u['id'],)).fetchall()
            active_srvs = 0
            for s in srvs:
                is_on = False
                if s['pid'] and psutil.pid_exists(s['pid']):
                    try:
                        proc = psutil.Process(s['pid'])
                        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                            is_on = True
                    except: pass
                elif s['folder'] in running_procs and running_procs[s['folder']].poll() is None:
                    is_on = True
                if is_on:
                    active_srvs += 1
                    active_servers += 1
            user_list.append({
                'id': u['id'], 'fname': u['fname'], 'email': u['email'], 
                'srv_count': len(srvs), 'active_srvs': active_srvs,
                'status': u['status'], 'role': u['role'], 'server_limit': u['server_limit'],
                'premium_until': u['premium_until'], 'plan_active': (u['role'] or '').lower() == 'premium'
            })
        db.close()
        banned_users = sum(1 for u in user_list if u['status'] == 'banned')
        return jsonify({'users': user_list, 'sys_cpu': f"{total_cpu}%", 'sys_ram': f"{total_ram}%", 'total_servers': total_servers, 'active_servers': active_servers, 'banned_users': banned_users})

    @app.route('/admin/user/update', methods=['POST'])
    def update_user():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        d = request.get_json(silent=True) or {}
        try:
            user_id = int(d.get('user_id'))
            limit = max(1, min(50, int(d.get('limit', 1))))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'Invalid user or server limit.'}), 400
        role = (d.get('role') or '').strip().lower()
        status = (d.get('status') or '').strip().lower()
        if role not in {'free', 'premium', 'admin'}:
            return jsonify({'status': 'error', 'msg': 'Invalid plan.'}), 400
        if status not in {'active', 'banned'}:
            return jsonify({'status': 'error', 'msg': 'Invalid account status.'}), 400
        db = get_db()
        target = db.execute('SELECT id, role FROM users WHERE id=?', (user_id,)).fetchone()
        if not target:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        if (target['role'] or '').lower() == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Administrator accounts cannot be changed here.'}), 403
        if role == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'A regular account cannot be promoted to administrator.'}), 403
        db.execute('UPDATE users SET role=?, status=?, server_limit=? WHERE id=?', (role, status, limit, user_id))
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'role': role, 'status_value': status, 'server_limit': limit})

    @app.route('/admin/user/plan', methods=['POST'])
    def update_user_plan():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        payload = request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get('user_id'))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'Invalid account.'}), 400
        action = (payload.get('action') or 'activate').strip().lower()
        if action not in {'activate', 'deactivate'}:
            return jsonify({'status': 'error', 'msg': 'Invalid plan action.'}), 400
        db = get_db()
        target = db.execute('SELECT id, role FROM users WHERE id=?', (user_id,)).fetchone()
        if not target:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        if (target['role'] or '').lower() == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Administrator accounts cannot be changed here.'}), 403
        if action == 'deactivate':
            db.execute("UPDATE users SET role='free', premium_until=NULL WHERE id=?", (user_id,))
            db.execute("UPDATE servers SET plan='free' WHERE user_id=?", (user_id,))
            message = 'The legendary plan was stopped for this account.'
            expiry = None
        else:
            try:
                duration_days = int(payload.get('duration_days', 0) or 0)
            except (TypeError, ValueError):
                db.close()
                return jsonify({'status': 'error', 'msg': 'Duration must be a number of days.'}), 400
            if duration_days < 0 or duration_days > 3650:
                db.close()
                return jsonify({'status': 'error', 'msg': 'Duration must be between 1 and 3650 days, or 0 for no expiry.'}), 400
            expiry = None
            if duration_days:
                expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=duration_days)).isoformat()
            db.execute("UPDATE users SET role='premium', premium_until=?, plan_notice='', plan_notice_seen=1 WHERE id=?", (expiry, user_id))
            message = 'The legendary plan was activated for this account.'
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'role': 'premium' if action == 'activate' else 'free', 'premium_until': expiry, 'msg': message})

    @app.route('/admin/user/password', methods=['POST'])
    def admin_change_user_password():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        payload = request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get('user_id'))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'Invalid account.'}), 400
        password = (payload.get('password') or '').strip()
        if len(password) < 6:
            return jsonify({'status': 'error', 'msg': 'Password must be at least 6 characters.'}), 400
        db = get_db()
        target = db.execute('SELECT id, role FROM users WHERE id=?', (user_id,)).fetchone()
        if not target:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        if (target['role'] or '').lower() == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Administrator passwords cannot be changed here.'}), 403
        db.execute('UPDATE users SET password=? WHERE id=?', (password, user_id))
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'msg': 'Password updated successfully.'})

    @app.route('/notifications/plan/ack', methods=['POST'])
    def acknowledge_plan_notice():
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'msg': 'Authentication required.'}), 401
        db = get_db()
        db.execute('UPDATE users SET plan_notice_seen=1 WHERE id=?', (session['user_id'],))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/set-popup', methods=['POST'])
    def set_popup():
        if not session.get('admin_logged'): return jsonify({'status':'error'})
        title, msg, show = request.form.get('title'), request.form.get('msg'), request.form.get('show')
        img = request.files.get('image')
        db = get_db()
        old_data = db.execute('SELECT popup_img FROM admin_settings WHERE id=1').fetchone()
        img_name = old_data['popup_img'] if old_data else None
        if img:
            img_name = secure_filename(img.filename)
            img.save(os.path.join(app.config['UPLOAD_FOLDER'], img_name))
        db.execute('UPDATE admin_settings SET popup_title=?, popup_msg=?, popup_img=?, show_popup=? WHERE id=1', (title, msg, img_name, 1 if show == 'true' else 0))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/send-warning', methods=['POST'])
    def send_warning():
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('UPDATE users SET notifications=? WHERE id=?', (d['message'], d['user_id']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/login-as/<int:uid>')
    def login_as(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        session['user_id'] = uid
        return redirect(url_for('dashboard'))

    @app.route('/admin/manage-user/<int:uid>')
    def admin_manage_user_servers(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if not user:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (uid,)).fetchall()
        db.close()
        servers = []
        for r in rows:
            f = r['folder']
            online = (f in running_procs and running_procs[f].poll() is None) or (r['pid'] and psutil.pid_exists(r['pid']))
            servers.append({'id': r['id'], 'name': r['name'], 'folder': f, 'online': online, 'status': r['server_status']})
        return render_template('web/admin_manage_user.html', user=user, servers=servers)

    @app.route('/admin/suspend-server/<int:sid>', methods=['POST'])
    def admin_suspend_server(sid):
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        payload = request.get_json(silent=True) or {}
        status = (payload.get('status') or '').strip().lower()
        if status not in {'active', 'suspended'}:
            return jsonify({'status': 'error', 'msg': 'Invalid server status.'}), 400
        db = get_db()
        srv = db.execute('SELECT folder, pid FROM servers WHERE id=?', (sid,)).fetchone()
        if not srv:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server not found.'}), 404
        if status == 'suspended':
            tracked = running_procs.get(srv['folder'])
            pid = tracked.pid if tracked else srv['pid']
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            running_procs.pop(srv['folder'], None)
            start_times.pop(srv['folder'], None)
            db.execute('UPDATE servers SET server_status=?, pid=NULL WHERE id=?', (status, sid))
        else:
            db.execute('UPDATE servers SET server_status=? WHERE id=?', (status, sid))
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'server_status': status})

    @app.route('/admin/delete-server/<int:sid>', methods=['POST'])
    def admin_delete_server(sid):
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        db = get_db()
        srv = db.execute('SELECT folder, pid FROM servers WHERE id=?', (sid,)).fetchone()
        if not srv:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server not found'}), 404
        folder = srv['folder']
        tracked = running_procs.get(folder)
        pid = tracked.pid if tracked else srv['pid']
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        running_procs.pop(folder, None)
        start_times.pop(folder, None)
        db.execute('DELETE FROM servers WHERE id=?', (sid,))
        db.commit()
        path = os.path.join(app.config['BASE_STORAGE'], folder)
        if os.path.exists(path):
            shutil.rmtree(path)
        db.close()
        return jsonify({'status': 'deleted'})

    @app.route('/admin/create-user', methods=['POST'])
    def admin_create_user():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        d = request.get_json(silent=True) or {}
        name = (d.get('name') or '').strip()[:80]
        email = (d.get('email') or '').strip().lower()[:160]
        password = (d.get('pass') or '').strip()
        try:
            limit = max(1, min(50, int(d.get('limit', 1) or 1)))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'Server limit must be a number.'}), 400
        if not name or not email or not password:
            return jsonify({'status': 'error', 'msg': 'Name, email and password are required.'}), 400
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email) or len(password) < 6:
            return jsonify({'status': 'error', 'msg': 'Enter a valid email and a password of at least 6 characters.'}), 400
        db = get_db()
        exists = db.execute('SELECT id FROM users WHERE lower(email)=? OR lower(username)=?', (email, email)).fetchone()
        if exists:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Email already exists.'}), 409
        base_username = re.sub(r'[^a-z0-9_]+', '', email.split('@', 1)[0].lower())[:32] or 'user'
        username = base_username
        suffix = 2
        while db.execute('SELECT id FROM users WHERE lower(username)=?', (username,)).fetchone():
            username = f'{base_username}{suffix}'[:40]
            suffix += 1
        db.execute('INSERT INTO users (fname, username, email, password, server_limit, role, status) VALUES (?,?,?,?,?,?,?)', (name, username, email, password, limit, 'free', 'active'))
        user_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'user_id': user_id})

    @app.route('/admin/delete-user/<int:uid>', methods=['POST'])
    def delete_user(uid):
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        db = get_db()
        user = db.execute('SELECT id, role FROM users WHERE id=?', (uid,)).fetchone()
        if not user:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        if (user['role'] or '').lower() == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Administrator accounts cannot be deleted.'}), 403
        srvs = db.execute('SELECT folder, pid FROM servers WHERE user_id=?', (uid,)).fetchall()
        for server in srvs:
            folder = server['folder']
            tracked = running_procs.get(folder)
            pid = tracked.pid if tracked else server['pid']
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            running_procs.pop(folder, None)
            start_times.pop(folder, None)
            path = os.path.join(app.config['BASE_STORAGE'], folder)
            if os.path.exists(path):
                shutil.rmtree(path)
        db.execute('DELETE FROM tickets WHERE user_id=?', (uid,))
        db.execute('DELETE FROM servers WHERE user_id=?', (uid,))
        db.execute('DELETE FROM users WHERE id=?', (uid,))
        db.commit()
        db.close()
        return jsonify({'status': 'deleted', 'user_id': uid})
        
    @app.route('/admin/files/<folder>')
    def admin_browse_files(folder):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/dashboard.html', user={'fname': 'Admin'}, is_admin_view=True, admin_folder=folder)

    @app.route('/files/list/<folder>')
    def flist(folder):
        sub_path = request.args.get('path', '')
        full_path = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path))
        if not full_path.startswith(app.config['BASE_STORAGE']): return jsonify([])
        if not os.path.exists(full_path): return jsonify([])
        items = []
        for f in sorted(os.listdir(full_path)):
            if f == 'console.log': continue
            p = os.path.join(full_path, f)
            items.append({'name': f, 'is_dir': os.path.isdir(p), 'is_zip': f.lower().endswith('.zip'), 'rel_path': os.path.join(sub_path, f)})
        return jsonify(items)

    @app.route('/files/content/<folder>/<name>')
    def fcontent(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name)
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f: return jsonify({'content': f.read()})
        except: return jsonify({'content': 'Error reading file'})

    @app.route('/files/save/<folder>/<name>', methods=['POST'])
    def fsave(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name)
        try:
            with open(p, 'w', encoding='utf-8') as f: f.write(request.json.get('content'))
            return jsonify({'status': 'saved'})
        except: return jsonify({'status': 'error'})

    @app.route('/files/delete-bulk/<folder>', methods=['POST'])
    def delete_bulk(folder):
        d = request.json
        sub_path, names = d.get('path', ''), d.get('names', [])
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        for name in names:
            p = os.path.join(base, name)
            if name == 'console.log': continue
            try:
                if os.path.isdir(p): shutil.rmtree(p)
                elif os.path.exists(p): os.remove(p)
            except: pass
        return jsonify({"status": "ok"})

    @app.route('/files/create-file/<folder>', methods=['POST'])
    def create_file(folder):
        d = request.json
        p = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''), secure_filename(d.get('name')))
        with open(p, 'w') as f: f.write("")
        return jsonify({'status': 'success'})

    @app.route('/files/create-folder/<folder>', methods=['POST'])
    def create_folder(folder):
        d = request.json
        p = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''), secure_filename(d.get('name')))
        os.makedirs(p, exist_ok=True)
        return jsonify({'status': 'success'})

    @app.route('/files/upload/<folder>', methods=['POST'])
    def upload_file(folder):
        sub_path = request.form.get('path', '')
        file = request.files['file']
        dest = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not os.path.exists(dest): os.makedirs(dest)
        file.save(os.path.join(dest, secure_filename(file.filename)))
        return jsonify({'status': 'success'})

    @app.route('/files/rename/<folder>', methods=['POST'])
    def rename_file(folder):
        d = request.json or {}
        base = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', '')))
        old_name, new_name = secure_filename(d.get('old', '')), secure_filename(d.get('new', ''))
        root = os.path.realpath(os.path.join(app.config['BASE_STORAGE'], folder))
        if not old_name or not new_name or os.path.realpath(base) != root and not os.path.realpath(base).startswith(root + os.sep):
            return jsonify({'status': 'error', 'msg': 'Invalid file name or path.'}), 400
        old_path, new_path = os.path.join(base, old_name), os.path.join(base, new_name)
        if not os.path.exists(old_path): return jsonify({'status': 'error', 'msg': 'File not found.'}), 404
        if os.path.exists(new_path): return jsonify({'status': 'error', 'msg': 'A file with this name already exists.'}), 409
        os.rename(old_path, new_path)
        return jsonify({'status': 'success'})

    @app.route('/files/download/<folder>/<name>')
    def download_file(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name))
        if not p.startswith(app.config['BASE_STORAGE']): return "Access Denied", 403
        return send_file(p, as_attachment=True)

    @app.route('/files/zip-bulk/<folder>', methods=['POST'])
    def zip_bulk(folder):
        d = request.json
        names, sub_path = d.get('names', []), d.get('path', '')
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        zip_name = f"archive_{int(time.time())}.zip"
        zip_path = os.path.join(base, zip_name)
        with zipfile.ZipFile(zip_path, 'w') as z:
            for n in names:
                p = os.path.join(base, n)
                if n == zip_name: continue
                if os.path.isdir(p):
                    for root, dirs, files in os.walk(p):
                        for file in files:
                            full_p = os.path.join(root, file)
                            z.write(full_p, os.path.relpath(full_p, base))
                elif os.path.exists(p): z.write(p, n)
        return jsonify({'status': 'success', 'zip': zip_name})

    @app.route('/files/unzip/<folder>', methods=['POST'])
    def unzip_file(folder):
        d = request.json
        zip_name = d.get('name')
        sub_path = d.get('path', '')
        
        # Path safety calculation
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        zip_path = os.path.join(base, zip_name)
        
        if os.path.exists(zip_path) and zipfile.is_zipfile(zip_path):
            try:
                base_real = os.path.realpath(base)
                with zipfile.ZipFile(zip_path, 'r') as z:
                    for member in z.infolist():
                        target = os.path.realpath(os.path.join(base, member.filename))
                        if target != base_real and not target.startswith(base_real + os.sep):
                            return jsonify({'status': 'error', 'msg': 'Unsafe archive path rejected.'}), 400
                    z.extractall(base)
                return jsonify({'status': 'success'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': str(e)})
        return jsonify({'status': 'error', 'msg': 'Invalid zip file'})

    @app.route('/server/action/<folder>/<act>', methods=['POST'])
    def server_action(folder, act):
        db = get_db()
        srv_data = db.execute('SELECT server_status FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv_data and srv_data['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'This server is suspended by Admin.'})

        path = os.path.join(app.config['BASE_STORAGE'], folder)
        log_file_path = os.path.join(path, 'console.log')
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if act == 'install':
            req_path = os.path.join(path, 'requirements.txt')
            if os.path.exists(req_path):
                f_log = open(log_file_path, 'a', buffering=1)
                venv_dir = os.path.join(path, '.venv')
                venv_python = os.path.join(venv_dir, 'bin', 'python')
                if not os.path.exists(venv_python):
                    f_log.write(f"\n[{now}] 🧰 Creating isolated Python environment...\n")
                    made = subprocess.run([sys.executable, '-m', 'venv', venv_dir], cwd=path, stdout=f_log, stderr=subprocess.STDOUT, timeout=120)
                    if made.returncode != 0:
                        f_log.write(f"[{now}] ❌ Could not create isolated environment.\n"); f_log.close(); db.close()
                        return jsonify({'status': 'error', 'msg': 'Could not create the Python environment.'}), 400
                f_log.write(f"\n[{now}] 📦 Installing requirements.txt in this server environment...\n")
                install = subprocess.Popen([venv_python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '-r', req_path], cwd=path, stdout=f_log, stderr=subprocess.STDOUT)
                db.close()
                return jsonify({'status': 'installing', 'pid': install.pid})
            db.close()
            return jsonify({'status': 'error', 'msg': 'requirements.txt missing'})

        if act in ['start', 'restart']:
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            old_pid = row['pid'] if row else None
            if folder in running_procs or (old_pid and psutil.pid_exists(old_pid)):
                try: 
                    t_pid = running_procs[folder].pid if folder in running_procs else old_pid
                    os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass
            srv = db.execute('SELECT startup, server_type FROM servers WHERE folder=?', (folder,)).fetchone()
            startup_file = srv['startup'] if srv and srv['startup'] else 'main.py'
            is_node = bool(srv and (srv['server_type'] or '').lower() in ('node', 'nodejs'))
            runtime = 'node' if is_node else 'python3'
            if not is_node:
                venv_dir = os.path.join(path, '.venv')
                venv_python = os.path.join(venv_dir, 'bin', 'python')
                if os.path.exists(os.path.join(path, 'requirements.txt')):
                    f_log = open(log_file_path, 'a', buffering=1)
                    f_log.write(f"\n[{now}] 🧰 Preparing isolated Python environment...\n")
                    if not os.path.exists(venv_python):
                        made = subprocess.run([sys.executable, '-m', 'venv', venv_dir], cwd=path, stdout=f_log, stderr=subprocess.STDOUT, timeout=120)
                        if made.returncode != 0:
                            f_log.write(f"[{now}] ❌ Failed to create isolated Python environment.\n"); f_log.close(); db.close()
                            return jsonify({'status': 'error', 'msg': 'Could not create the Python environment.'}), 400
                    f_log.write(f"[{now}] 📦 Installing requirements.txt before startup...\n")
                    install = subprocess.run([venv_python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '-r', os.path.join(path, 'requirements.txt')], cwd=path, stdout=f_log, stderr=subprocess.STDOUT, timeout=300)
                    if install.returncode != 0:
                        f_log.write(f"[{now}] ❌ requirements installation failed with code {install.returncode}.\n"); f_log.close(); db.close()
                        return jsonify({'status': 'error', 'msg': 'Python requirements could not be installed. Check the Console.'}), 400
                    f_log.write(f"[{now}] ✅ Python requirements installed in isolated environment.\n")
                    runtime = venv_python
                    f_log.close()
            f_log = open(log_file_path, 'a', buffering=1)
            f_log.write(f"\n[{now}] 🚀 Instance {act.upper()}ED Successfully ({runtime})\n")
            if runtime == 'node' and os.path.isfile(os.path.join(path, 'package.json')):
                f_log.write(f"[{now}] 📦 Installing Node.js dependencies from package.json...\n")
                try:
                    install = subprocess.run(['npm', 'install', '--no-audit', '--no-fund', '--omit=dev'], cwd=path, stdout=f_log, stderr=subprocess.STDOUT, timeout=180)
                except subprocess.TimeoutExpired:
                    f_log.write(f"[{now}] ❌ npm install timed out.\n")
                    f_log.close(); db.close()
                    return jsonify({'status': 'error', 'msg': 'Node.js dependency installation timed out.'}), 504
                if install.returncode != 0:
                    f_log.write(f"[{now}] ❌ npm install failed with code {install.returncode}.\n")
                    f_log.close(); db.close()
                    return jsonify({'status': 'error', 'msg': 'Node.js dependencies could not be installed. Check the Console.'}), 400
                f_log.write(f"[{now}] ✅ Node.js dependencies installed successfully.\n")
            proc = subprocess.Popen([runtime, startup_file], cwd=path, stdout=f_log, stderr=f_log, preexec_fn=os.setsid)
            running_procs[folder], start_times[folder] = proc, time.time()
            db.execute('UPDATE servers SET pid=? WHERE folder=?', (proc.pid, folder))
            db.commit()
            db.close()
            return jsonify({'status': 'started'})
        elif act == 'stop':
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            t_pid = running_procs[folder].pid if folder in running_procs else (row['pid'] if row else None)
            if t_pid:
                try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass
            if folder in running_procs: del running_procs[folder]
            db.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,))
            db.commit()
            db.close()
            with open(log_file_path, 'a') as f: f.write(f"\n[{now}] 🛑 Instance STOPPED\n")
            return jsonify({'status': 'stopped'})
        db.close()
        return jsonify({'status': 'ok'})

    @app.route('/server/log/<folder>')
    def server_log(folder):
        path = os.path.join(app.config['BASE_STORAGE'], folder, 'console.log')
        db = get_db()
        row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
        db.close()
        online = is_server_online(folder, row['pid'] if row else None)
        if os.path.exists(path):
            with open(path, 'r') as f: return jsonify({'log': f.read()[-5000:], 'online': online})
        return jsonify({'log': 'Waiting for logs...', 'online': online})

    @app.route('/server/set-startup/<folder>', methods=['POST'])
    def set_startup(folder):
        cmd = request.json.get('file')
        db = get_db()
        db.execute('UPDATE servers SET startup=? WHERE folder=?', (cmd, folder))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/server/delete/<folder>', methods=['POST'])
    def delete_server(folder):
        db = get_db()
        srv_data = db.execute('SELECT server_status, pid FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv_data and srv_data['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Suspended servers cannot be deleted!'})
        t_pid = running_procs[folder].pid if folder in running_procs else (srv_data['pid'] if srv_data else None)
        if t_pid:
            try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
            except: pass
        if folder in running_procs: del running_procs[folder]
        db.execute('DELETE FROM servers WHERE folder=?', (folder,))
        db.commit()
        db.close()
        path = os.path.join(app.config['BASE_STORAGE'], folder)
        if os.path.exists(path): shutil.rmtree(path)
        return jsonify({'status': 'deleted'})

    def is_server_online(folder, saved_pid=None):
        """Use the live child process or live Docker container, not a stale DB status."""
        proc = running_procs.get(folder)
        if proc is not None:
            try:
                if proc.poll() is None:
                    return True
            except Exception:
                pass
        if saved_pid:
            try:
                process = psutil.Process(int(saved_pid))
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
                pass
        return get_docker_stats(folder) is not None

    @app.route('/servers')
    def list_servers():
        if 'user_id' not in session: return jsonify({'servers': []})
        db = get_db()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (session['user_id'],)).fetchall()
        db.close()
        srvs = []
        for r in rows:
            f, saved_pid = r['folder'], r['pid']
            online = is_server_online(f, saved_pid)
            uptime = get_precise_uptime(start_times.get(f)) if online and f in start_times else ("Online" if online else "Offline")
            cpu, ram, metrics_source = "0%", "0MB", "process"
            docker_metrics = get_docker_stats(f)
            if docker_metrics:
                online = True
                cpu, ram, metrics_source = docker_metrics['cpu'], docker_metrics['ram'], docker_metrics['source']
            elif online:
                try:
                    p_pid = running_procs[f].pid if f in running_procs else saved_pid
                    process = psutil.Process(p_pid)
                    cpu, ram = f"{process.cpu_percent(interval=None)}%", f"{process.memory_info().rss / (1024 * 1024):.1f}MB"
                except: pass
            plan_name = (r['plan'] or 'free').lower()
            limits = server_resource_limits(plan_name)
            cpu_used = parse_memory_mb(str(cpu).replace('%', ''))
            ram_used_mb = round(parse_memory_mb(ram), 1)
            disk_used_mb = folder_disk_mb(os.path.join(app.config['BASE_STORAGE'], f))
            usage_percent = min(100.0, round(max((cpu_used / limits['cpu_percent']) * 100, (ram_used_mb / limits['ram_mb']) * 100, (disk_used_mb / limits['disk_mb']) * 100), 1))
            limit_reached = usage_percent >= 95
            if online and limit_reached:
                try:
                    if docker_metrics:
                        subprocess.run(['docker', 'stop', '--time', '3', f'ne-server-{f}'], capture_output=True, timeout=8)
                    else:
                        active_pid = running_procs[f].pid if f in running_procs else saved_pid
                        if active_pid: os.killpg(os.getpgid(active_pid), signal.SIGKILL)
                except Exception: pass
                running_procs.pop(f, None)
                start_times.pop(f, None)
                db_limit = get_db(); db_limit.execute('UPDATE servers SET pid=NULL WHERE folder=?', (f,)); db_limit.commit(); db_limit.close()
                online = False
                uptime = 'Stopped: resource limit'
            public_ip = r['public_ip'] or PUBLIC_IP
            public_port = r['public_port'] or (PUBLIC_PORT_MIN + (r['id'] % (PUBLIC_PORT_MAX - PUBLIC_PORT_MIN + 1)))
            custom_domain = (r['custom_domain'] or '').strip() if 'custom_domain' in r.keys() else ''
            demo_ip = (r['demo_ip'] or '').strip() if 'demo_ip' in r.keys() else ''
            if not demo_ip: demo_ip = f"10.20.{(r['id'] // 250) % 250}.{(r['id'] % 250) + 1}"
            srvs.append({'name': r['name'], 'folder': f, 'online': online, 'startup': r['startup'], 'uptime': uptime, 'cpu': cpu, 'ram': ram, 'status': r['server_status'], 'ip': public_ip, 'port': public_port, 'url': f'http://{public_ip}:{public_port}/', 'domain': custom_domain, 'description': r['description'] or '', 'plan': plan_name, 'type': r['server_type'] or 'python', 'metrics_source': metrics_source, 'ram_used_mb': ram_used_mb, 'disk_used_mb': disk_used_mb, 'usage_percent': usage_percent, 'limit_reached': limit_reached, 'limits': limits, 'status_url': url_for('public_server_status', folder=f, _external=True), 'demo_ip': demo_ip})
        return jsonify({'servers': srvs})

    @app.route('/add', methods=['POST'])
    def add_srv():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        count = db.execute('SELECT COUNT(*) as count FROM servers WHERE user_id=?', (session['user_id'],)).fetchone()['count']
        server_limit = max(1, int(user['server_limit'] or 1)) if user else 1
        if count >= server_limit:
            db.close()
            return jsonify({'status': 'error', 'msg': f'Your account allows up to {server_limit} server(s).'}), 409
        payload = request.get_json(silent=True) or {}
        name = (payload.get('name') or '').strip()
        if not name:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server name is required.'}), 400
        used_ports = {row[0] for row in db.execute('SELECT public_port FROM servers WHERE public_port IS NOT NULL').fetchall()}
        available_ports = [p for p in range(PUBLIC_PORT_MIN, PUBLIC_PORT_MAX + 1) if p not in used_ports]
        if not available_ports:
            db.close()
            return jsonify({'status': 'error', 'msg': 'No public ports are available right now.'}), 503
        public_port = random.choice(available_ports)
        description = (payload.get('description') or '').strip()[:240]
        requested_plan = (payload.get('plan') or 'free').strip().lower()
        account_role = (user['role'] or 'free').strip().lower() if user else 'free'
        plan = 'premium' if requested_plan == 'premium' and account_role in {'premium', 'admin'} else 'free'
        server_type = (payload.get('type') or 'python').strip().lower()
        if server_type not in {'python', 'nodejs'}: server_type = 'python'
        custom_domain = (payload.get('domain') or '').strip().lower()[:120]
        if custom_domain.startswith('http://') or custom_domain.startswith('https://'): custom_domain = custom_domain.split('://', 1)[1].split('/', 1)[0]
        if custom_domain and not re.match(r'^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$', custom_domain): custom_domain = ''
        startup = 'main.py' if server_type == 'python' else 'index.js'
        folder = secure_filename(name).lower() + "_" + str(int(time.time()))
        cursor = db.execute('INSERT INTO servers (user_id, name, folder, status, startup, public_ip, public_port, description, plan, server_type, custom_domain) VALUES (?,?,?,?,?,?,?,?,?,?,?)', (session['user_id'], name, folder, 'Offline', startup, PUBLIC_IP, public_port, description, plan, server_type, custom_domain))
        server_id = cursor.lastrowid
        demo_ip = f"10.20.{(server_id // 250) % 250}.{(server_id % 250) + 1}"
        db.execute('UPDATE servers SET demo_ip=? WHERE id=?', (demo_ip, server_id))
        db.commit()
        db.close()
        instance_path = os.path.join(app.config['BASE_STORAGE'], folder)
        os.makedirs(instance_path, exist_ok=True)
        if server_type == 'nodejs':
            with open(os.path.join(instance_path, 'index.js'), 'w', encoding='utf-8') as starter:
                starter.write("console.log('13X1 Node.js server is ready');\n")
        return jsonify({'status': 'success', 'ip': PUBLIC_IP, 'port': public_port, 'domain': custom_domain, 'type': server_type, 'url': f'http://{PUBLIC_IP}:{public_port}/'})

    return app

app = create_app()
if __name__ == "__main__":
    port = int(os.environ.get('PORT', '13506'))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
