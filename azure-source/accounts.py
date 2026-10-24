"""Optional account gateway. Every user gets a separate backend process and directory.

The legacy loopback panel remains the default. This gateway also binds loopback;
public ingress/TLS and mail must be configured separately before hosted use.
"""
import hashlib
import hmac
import base64
import struct
import http.client
import json
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


class Accounts:
    def __init__(self, root, origin, hosted=False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.origin = origin.rstrip('/')
        self.hosted = hosted
        self.db_path = self.root / 'accounts.sqlite3'
        self.lock = threading.RLock()
        self.children = {}
        if hosted and not self.origin.startswith('https://'):
            raise ValueError('Hosted accounts require HTTPS origin.')
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,verified INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT,csrf TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS tickets(token TEXT PRIMARY KEY,user_id TEXT,kind TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS attempts(key TEXT PRIMARY KEY,failures INTEGER,last REAL,locked REAL);
            CREATE TABLE IF NOT EXISTS account_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS invites(token TEXT PRIMARY KEY,email TEXT NOT NULL,expires REAL NOT NULL,used REAL);
            CREATE TABLE IF NOT EXISTS totp(user_id TEXT PRIMARY KEY,secret TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,last_counter INTEGER NOT NULL DEFAULT -1);
            ''')
            if 'workspace_id' not in {r[1] for r in db.execute('PRAGMA table_info(invites)')}:
                db.execute("ALTER TABLE invites ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def workspace(self, uid):
        if not re.fullmatch('[a-f0-9]{32}', uid):
            raise ValueError('Invalid workspace.')
        path = self.root / 'users' / uid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def limited(self, key, failure=False, maximum=8):
        key = self.digest(key)
        with self.lock, self.db() as db:
            row = db.execute('SELECT * FROM attempts WHERE key=?', (key,)).fetchone()
            if row and row['locked'] > time.time():
                raise ValueError('Too many attempts. Try again in 15 minutes.')
            if failure:
                count = row['failures'] + 1 if row and row['last'] > time.time()-900 else 1
                db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?)',
                           (key, count, time.time(), time.time()+900 if count >= maximum else 0))

    def public_signup(self):
        with self.db() as db:
            row=db.execute("SELECT value FROM account_settings WHERE key='public_signup'").fetchone()
        return row['value']=='1' if row else not self.hosted

    def invite(self, email, workspace_id=''):
        if workspace_id and not re.fullmatch('[a-f0-9]{32}',workspace_id): raise ValueError('Invalid invited workspace.')
        token=secrets.token_urlsafe(40)
        with self.db() as db:
            db.execute('INSERT INTO invites(token,email,expires,used,workspace_id) VALUES(?,?,?,NULL,?)',
                (self.digest(token),str(email).strip().lower(),time.time()+86400,workspace_id))
        return token

    def signup(self, email, password, invitation=''):
        email = str(email).strip().lower()
        if not re.fullmatch(r'[^\s@]{1,64}@[^\s@]{1,189}\.[^\s@]{2,63}', email) or not 12 <= len(password) <= 256:
            raise ValueError('Use a valid email and a password of 12–256 characters.')
        uid = secrets.token_hex(16)
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            invite=db.execute('SELECT * FROM invites WHERE token=? AND email=? AND expires>? AND used IS NULL',
                (self.digest(invitation),email,time.time())).fetchone() if invitation else None
            if not self.public_signup() and not invite: raise ValueError('Registration is closed. Use a valid invitation for this email.')
            if invite and invite['workspace_id']: uid=invite['workspace_id']
            try:
                db.execute('INSERT INTO users VALUES(?,?,?,?)', (uid,email,HASHER.hash(password),int(not self.hosted or bool(invite))))
                if invite: db.execute('UPDATE invites SET used=? WHERE token=?',(time.time(),invite['token']))
            except sqlite3.IntegrityError:
                raise ValueError('Unable to create this account. Try signing in or resetting your password.') from None
        self.workspace(uid)  # Empty directory; never copy the legacy workspace.
        if self.hosted and not invite:
            self.mail_ticket(uid,email,'verify')
        return uid

    def login(self, email, password, address, otp=''):
        email = str(email).strip().lower()
        self.limited('ip:'+address)
        self.limited('login:'+email)
        with self.db() as db:
            user = db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        try:
            # Dummy verification keeps nonexistent-user failures similarly expensive.
            HASHER.verify(user['password'] if user else DUMMY_HASH, str(password))
            valid = user is not None
        except (VerificationError, InvalidHashError):
            valid = False
        if not valid:
            self.limited('ip:'+address,True)
            self.limited('login:'+email,True)
            raise ValueError('Email or password is incorrect.')
        if not user['verified']:
            raise ValueError('Verify your email before signing in. Check your inbox.')
        with self.db() as db: second=db.execute('SELECT * FROM totp WHERE user_id=? AND enabled=1',(user['id'],)).fetchone()
        if second and not self.verify_totp(user['id'],otp):
            self.limited('ip:'+address,True);self.limited('login:'+email,True)
            raise ValueError('Authenticator code is required or incorrect.')
        token,csrf = secrets.token_urlsafe(48),secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM attempts WHERE key=?',(self.digest('login:'+email),))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(self.digest(token),user['id'],csrf,time.time()+43200))
        return token,csrf

    def session(self, token):
        with self.db() as db:
            row = db.execute('SELECT s.*,u.email FROM sessions s JOIN users u ON u.id=s.user_id WHERE token=? AND expires>?',
                             (self.digest(token),time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token=?',(self.digest(token),))

    def sessions(self, uid):
        with self.db() as db:
            return [dict(expires=r['expires']) for r in db.execute('SELECT expires FROM sessions WHERE user_id=? AND expires>?',(uid,time.time()))]

    def logout_all(self, uid):
        with self.db() as db: db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))

    def verify_totp(self, uid, code):
        if not re.fullmatch(r'\d{6}',str(code)): return False
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM totp WHERE user_id=?',(uid,)).fetchone()
            if not row: return False
            for counter in range(int(time.time()//30)-1,int(time.time()//30)+2):
                if counter<=row['last_counter']: continue
                digest=hmac.new(base64.b32decode(row['secret']),struct.pack('>Q',counter),hashlib.sha1).digest()
                offset=digest[-1]&15
                expected=str((struct.unpack('>I',digest[offset:offset+4])[0]&0x7fffffff)%1000000).zfill(6)
                if secrets.compare_digest(expected,str(code)):
                    db.execute('UPDATE totp SET last_counter=? WHERE user_id=?',(counter,uid));return True
        return False

    def mail_ticket(self, uid, email, kind):
        if not os.getenv('DRIVECAST_SMTP_HOST'):
            raise ValueError('Email delivery is not configured. Set SMTP before using email reset.')
        token = secrets.token_urlsafe(48)
        with self.db() as db:
            db.execute('DELETE FROM tickets WHERE user_id=? AND kind=?',(uid,kind))
            db.execute('INSERT INTO tickets VALUES(?,?,?,?)',(self.digest(token),uid,kind,time.time()+1800))
        message = EmailMessage()
        message['From'] = os.environ['DRIVECAST_SMTP_FROM']
        message['To'] = email
        message['Subject'] = 'DriveCast: '+('verify your email' if kind=='verify' else 'reset your password')
        message.set_content('Open this link within 30 minutes:\n'+self.origin+'/login#'+kind+'='+token+
                            '\nIf you did not request this, ignore this email.')
        try:
            with smtplib.SMTP(os.environ['DRIVECAST_SMTP_HOST'],int(os.getenv('DRIVECAST_SMTP_PORT','587')),timeout=20) as smtp:
                smtp.starttls()
                if os.getenv('DRIVECAST_SMTP_USER'):
                    smtp.login(os.environ['DRIVECAST_SMTP_USER'],os.environ['DRIVECAST_SMTP_PASSWORD'])
                smtp.send_message(message)
        except Exception:
            raise ValueError('Email could not be delivered. Check SMTP configuration.') from None

    def consume(self, token, kind, password=''):
        if kind not in ('verify','reset'):
            raise ValueError('Invalid email link.')
        if kind=='reset' and not 12 <= len(password) <= 256:
            raise ValueError('Use a password of 12–256 characters.')
        with self.lock, self.db() as db:
            row = db.execute('SELECT * FROM tickets WHERE token=? AND kind=? AND expires>?',
                             (self.digest(token),kind,time.time())).fetchone()
            if not row:
                raise ValueError('This email link expired or was already used.')
            if kind=='reset':
                db.execute('UPDATE users SET password=? WHERE id=?',(HASHER.hash(password),row['user_id']))
                db.execute('DELETE FROM sessions WHERE user_id=?',(row['user_id'],))
            else:
                db.execute('UPDATE users SET verified=1 WHERE id=?',(row['user_id'],))
            db.execute('DELETE FROM tickets WHERE token=?',(self.digest(token),))

    def backend(self, uid):
        with self.lock:
            existing = self.children.get(uid)
            if existing and existing[0].poll() is None:
                return existing[1:]
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                port = sock.getsockname()[1]
            env = dict(os.environ,DRIVECAST_WORK_DIR=str(self.workspace(uid)),DRIVECAST_ISOLATED_USER='1',GH_CONFIG_DIR=str(self.workspace(uid)/'github-cli'))
            env['DRIVECAST_WORKSPACE_ID']=uid
            env.pop('DRIVECAST_AUTH_MODE',None)
            env.pop('DRIVECAST_PUBLIC_CALLBACK_BASE',None)
            for key in list(env):
                if key.startswith('DRIVECAST_SMTP_'): env.pop(key,None)
            if self.hosted:env['DRIVECAST_PUBLIC_CALLBACK_BASE']=self.origin+'/oauth/'+uid
            child = subprocess.Popen([sys.executable,str(Path(__file__).with_name('app.py')),'--port',str(port),'--no-browser'],
                env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            for _ in range(100):
                try:
                    connection = http.client.HTTPConnection('127.0.0.1',port,timeout=2)
                    connection.request('GET','/')
                    response = connection.getresponse()
                    html = response.read().decode()
                    connection.close()
                    token = re.search(r'name="drivecast-token" content="([^"]+)"',html)[1]
                    self.children[uid] = (child,port,token)
                    return port,token
                except (OSError,TypeError,http.client.HTTPException):
                    if child.poll() is not None:
                        break
                    time.sleep(.1)
            child.terminate()
            raise ValueError('Workspace could not start. Check local dependencies and storage.')

    def close(self):
        for child,_,_ in self.children.values():
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


DUMMY_HASH = HASHER.hash('dummy-'+secrets.token_urlsafe(32))


def serve(root, port, ready=None):
    hosted = os.getenv('DRIVECAST_HOSTED')=='1'
    origin = os.getenv('DRIVECAST_ORIGIN',f'http://127.0.0.1:{port}')
    accounts = Accounts(root,origin,hosted)
    assets = Path(__file__).parent

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self,*_): pass

        def send(self,status,body,kind='application/json',cookie=None):
            raw = body if isinstance(body,bytes) else (json.dumps(body).encode() if kind=='application/json' else body.encode())
            self.send_response(status)
            for key,value in {'Content-Type':kind+'; charset=utf-8','Content-Length':str(len(raw)),
                'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
                self.send_header(key,value)
            if cookie: self.send_header('Set-Cookie',cookie)
            self.end_headers()
            self.wfile.write(raw)

        def session(self):
            cookie=SimpleCookie()
            try: cookie.load(self.headers.get('Cookie',''))
            except Exception: return '',None
            token=cookie.get('drivecast_session')
            token=token.value if token else ''
            return token,accounts.session(token) if token else None

        def handle_request(self,post=False):
            if self.headers.get('Host')!=urlparse(origin).netloc:
                return self.send(403,{'error':'Invalid host'})
            token,session=self.session()
            path=urlparse(self.path).path
            if not post and path=='/healthz': return self.send(200,{'status':'ok','version':'azure-v1'})
            match=re.fullmatch(r'/oauth/([a-f0-9]{32})/(google)',path)
            if not post and path=='/oauth/callback':
                params=parse_qs(urlparse(self.path).query)
                state=params.get('state',[''])[0]
                match=re.fullmatch(r'/oauth/([a-f0-9]{32})/(google)','/oauth/'+state.split('.')[0]+'/google')
            if not post and match:
                child=accounts.children.get(match[1])
                if not hosted or not child or child[0].poll() is not None:
                    return self.send(410,{'error':'Authorization workspace is no longer waiting. Sign in and create a new link.'})
                if len(self.path)>16000:return self.send(400,{'error':'Callback too large.'})
                params=parse_qs(urlparse(self.path).query)
                callback=dict(provider=match[2],state=params.get('state',[''])[0],code=params.get('code',[''])[0],error=params.get('error',[''])[0])
                try:
                    c=http.client.HTTPConnection('127.0.0.1',child[1],timeout=15)
                    c.request('POST','/api/oauth/callback',json.dumps(callback),headers={'X-Setup-Token':child[2],
                        'Origin':f'http://127.0.0.1:{child[1]}','Content-Type':'application/json'})
                    response=c.getresponse();response.read();status=response.status;c.close()
                    if status!=200:return self.send(400,{'error':'Authorization link expired or did not match. Create a new link.'})
                    self.send_response(303);self.send_header('Location','/login#authorization-received');self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer');self.end_headers();return
                except Exception:return self.send(503,{'error':'Authorization callback could not be received. Create a new link.'})
            if post and self.headers.get('Origin')!=origin:
                return self.send(403,{'error':'Invalid origin'})
            if not post and path in ('/login','/auth.js','/panel.css'):
                file={'/login':'login.html','/auth.js':'auth.js','/panel.css':'panel.css'}[path]
                return self.send(200,(assets/file).read_bytes(),'text/javascript' if path.endswith('.js') else 'text/css' if path.endswith('.css') else 'text/html')
            if not post and path=='/auth/session':
                return self.send(200,dict(signed_in=bool(session),email=session['email'] if session else '',
                    csrf=session['csrf'] if session else '',mail_ready=bool(os.getenv('DRIVECAST_SMTP_HOST')),hosted=hosted,public_signup=accounts.public_signup()))
            if not post and path=='/auth/sessions' and session:
                return self.send(200,dict(sessions=accounts.sessions(session['user_id'])))
            if path.startswith('/auth/') and post:
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<8192 or self.headers.get('Content-Type')!='application/json': raise ValueError('Small JSON request required.')
                    body=json.loads(self.rfile.read(size))
                    accounts.limited('auth:'+self.client_address[0],True,maximum=60)
                    if path=='/auth/signup':
                        uid=accounts.signup(body.get('email',''),body.get('password',''),body.get('invitation',''))
                        with accounts.db() as db: verified=db.execute('SELECT verified FROM users WHERE id=?',(uid,)).fetchone()[0]
                        return self.send(200,{'notice':'Account created. '+('Sign in to your workspace.' if verified else 'Check your email to verify it.')})
                    if path=='/auth/login':
                        # The sole trusted reverse proxy is Caddy on the private Docker network.
                        address=self.headers.get('X-Real-IP',self.client_address[0]) if os.getenv('DRIVECAST_TRUST_PROXY')=='1' else self.client_address[0]
                        token,csrf=accounts.login(body.get('email',''),body.get('password',''),address,body.get('otp',''))
                        cookie='drivecast_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'+('; Secure' if origin.startswith('https:') else '')
                        return self.send(200,{'csrf':csrf},cookie=cookie)
                    if path=='/auth/forgot':
                        if not os.getenv('DRIVECAST_SMTP_HOST'): raise ValueError('Email reset is unavailable until SMTP is configured.')
                        with accounts.db() as db: user=db.execute('SELECT * FROM users WHERE email=?',(str(body.get('email','')).strip().lower(),)).fetchone()
                        if user: accounts.mail_ticket(user['id'],user['email'],'reset')
                        return self.send(200,{'notice':'If that account exists, an email reset link has been sent.'})
                    if path=='/auth/resend':
                        if not hosted: raise ValueError('Email verification is required only in hosted mode.')
                        with accounts.db() as db: user=db.execute('SELECT * FROM users WHERE email=?',(str(body.get('email','')).strip().lower(),)).fetchone()
                        if user and not user['verified']: accounts.mail_ticket(user['id'],user['email'],'verify')
                        return self.send(200,{'notice':'If verification is needed, a new email link has been sent.'})
                    if path in ('/auth/reset','/auth/verify'):
                        accounts.consume(body.get('ticket',''),path.rsplit('/',1)[1],body.get('password',''))
                        return self.send(200,{'notice':'Done. You can now sign in.'})
                    if path=='/auth/logout' and session and secrets.compare_digest(self.headers.get('X-Setup-Token',''),session['csrf']):
                        accounts.logout(token)
                        return self.send(200,{'ok':True},cookie='drivecast_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                    if session and secrets.compare_digest(self.headers.get('X-Setup-Token',''),session['csrf']):
                        if path=='/auth/logout-all':
                            accounts.logout_all(session['user_id']);return self.send(200,{'ok':True})
                        if path=='/auth/invite':
                            # Workspace users cannot invite into somebody else's workspace.
                            invite=accounts.invite(body.get('email',''))
                            return self.send(200,{'invitation':invite})
                        if path=='/auth/totp/setup':
                            with accounts.db() as db:
                                old=db.execute('SELECT enabled FROM totp WHERE user_id=?',(session['user_id'],)).fetchone()
                                if old and old['enabled']: raise ValueError('Authenticator is already enabled.')
                                secret=base64.b32encode(secrets.token_bytes(20)).decode()
                                db.execute('INSERT OR REPLACE INTO totp VALUES(?,?,0,-1)',(session['user_id'],secret))
                            return self.send(200,{'secret':secret})
                        if path=='/auth/totp/enable':
                            if not accounts.verify_totp(session['user_id'],body.get('otp','')): raise ValueError('Incorrect authenticator code.')
                            with accounts.db() as db: db.execute('UPDATE totp SET enabled=1 WHERE user_id=?',(session['user_id'],))
                            accounts.logout_all(session['user_id']);return self.send(200,{'ok':True})
                    return self.send(403,{'error':'Invalid account action'})
                except (ValueError,TypeError,KeyError) as error: return self.send(400,{'error':str(error)})
            if not session:
                if path.startswith('/api/') or post: return self.send(401,{'error':'Sign in to your workspace.'})
                self.send_response(303);self.send_header('Location','/login');self.end_headers();return
            if post and not secrets.compare_digest(self.headers.get('X-Setup-Token',''),session['csrf']):
                return self.send(403,{'error':'Invalid workspace session. Refresh this page.'})
            try:
                backend_port,backend_token=accounts.backend(session['user_id'])
                length=int(self.headers.get('Content-Length','0'))
                if length>48*1024*1024: raise ValueError('Request too large.')
                body=self.rfile.read(length) if post else None
                connection=http.client.HTTPConnection('127.0.0.1',backend_port,timeout=120)
                connection.request('POST' if post else 'GET',self.path,body=body,headers={
                    'Host':f'127.0.0.1:{backend_port}','Origin':f'http://127.0.0.1:{backend_port}',
                    'X-Setup-Token':backend_token,'Content-Type':'application/json'})
                response=connection.getresponse();raw=response.read();kind=response.getheader('Content-Type','application/json').split(';')[0]
                if kind=='text/html': raw=raw.replace(backend_token.encode(),session['csrf'].encode())
                self.send(response.status,raw,kind)
                connection.close()
            except Exception: self.send(503,{'error':'Workspace temporarily unavailable. Existing data preserved.'})

        def do_GET(self): self.handle_request()
        def do_POST(self): self.handle_request(True)

    server=ThreadingHTTPServer((os.getenv('DRIVECAST_BIND','127.0.0.1'),port),Gateway)
    if ready: ready(server,accounts)
    print('DriveCast accounts:',origin,flush=True)
    try: server.serve_forever()
    finally: server.server_close();accounts.close()
