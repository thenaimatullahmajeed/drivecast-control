"""Portable, password-encrypted secret storage; password/key is never persisted."""
import base64
import json
import os
import ctypes
import copy
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


class Vault:
    def __init__(self, path):
        self.path = Path(path)
        self.key = None
        self.salt = None
        self.values = {}
        self.base_values = {}
        self.generation = 0

    @property
    def unlocked(self):
        return self.key is not None

    def unlock(self, password):
        if not isinstance(password, str) or len(password) < 12:
            raise ValueError("Use an unlock password of at least 12 characters. Keep it safe; a lost password cannot be recovered.")
        envelope = self._envelope()
        if envelope and envelope.get("format") != "drivecast-vault-v1":
            raise ValueError("Unsupported encrypted vault format.")
        salt = base64.b64decode(envelope["salt"]) if envelope else os.urandom(16)
        key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
        try:
            values = json.loads(AESGCM(key).decrypt(base64.b64decode(envelope["nonce"]),
                base64.b64decode(envelope["ciphertext"]), b"drivecast-vault-v1")) if envelope else {}
        except (InvalidTag, ValueError, KeyError):
            raise ValueError("Wrong unlock password or damaged encrypted backup. Existing data unchanged.") from None
        self.key, self.salt, self.values = key, salt, values
        self.base_values=copy.deepcopy(values)
        self._read_generation()
        if not envelope:
            self.save()

    def _read_generation(self):
        if os.getenv('DRIVECAST_HOSTED')=='1':
            from cloud_store import CloudStore
            self.generation=CloudStore(self.path.parent/'records.sqlite3').meta('vault:generation',0)

    def save(self, replace=False):
        if not self.unlocked:
            raise ValueError("Pehle Unlock connections with your unlock password.")
        def encrypt(values):
            nonce=os.urandom(12)
            ciphertext=AESGCM(self.key).encrypt(nonce,json.dumps(values,ensure_ascii=False).encode(),b'drivecast-vault-v1')
            return dict(format='drivecast-vault-v1',salt=base64.b64encode(self.salt).decode(),nonce=base64.b64encode(nonce).decode(),ciphertext=base64.b64encode(ciphertext).decode())
        if os.getenv('DRIVECAST_HOSTED')=='1':
            from cloud_store import CloudStore
            from workspace_db import merge
            with CloudStore(self.path.parent/'records.sqlite3').db(True) as db:
                generation_row=db.execute("SELECT value FROM cloud_meta WHERE key='vault:generation'").fetchone()
                generation=json.loads(generation_row['value']) if generation_row else 0
                if generation!=self.generation:
                    raise ValueError('Connections were reset. Refresh before authorizing again.')
                row=db.execute("SELECT value FROM cloud_meta WHERE key='vault:envelope'").fetchone()
                current={}
                if row:
                    old=json.loads(row['value'])
                    if old['salt']==base64.b64encode(self.salt).decode():
                        current=json.loads(AESGCM(self.key).decrypt(base64.b64decode(old['nonce']),base64.b64decode(old['ciphertext']),b'drivecast-vault-v1'))
                self.values=merge(current,self.base_values,self.values) if current and not replace else self.values
                if replace:
                    generation+=1
                    db.execute("INSERT OR REPLACE INTO cloud_meta VALUES('vault:generation',?)",(json.dumps(generation),))
                envelope=encrypt(self.values)
                db.execute("INSERT OR REPLACE INTO cloud_meta VALUES('vault:envelope',?)",(json.dumps(envelope),))
                from recovery import request
                request(db)
                self.generation=generation
        else: envelope=encrypt(self.values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.'+os.urandom(4).hex()+'.tmp')
        temporary.write_text(json.dumps(envelope), encoding="utf-8")
        temporary.replace(self.path)
        if os.getenv('DRIVECAST_HOSTED')=='1': self.path.chmod(0o600)
        self.base_values=copy.deepcopy(self.values)

    def _envelope(self):
        if os.getenv('DRIVECAST_HOSTED')=='1' and (self.path.parent/'records.sqlite3').exists():
            from cloud_store import CloudStore
            value=CloudStore(self.path.parent/'records.sqlite3').meta('vault:envelope',None)
            if value: return value
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def refresh(self):
        if not self.unlocked: return
        envelope=self._envelope()
        if envelope:
            self.values=json.loads(AESGCM(self.key).decrypt(base64.b64decode(envelope['nonce']),base64.b64decode(envelope['ciphertext']),b'drivecast-vault-v1'))
            self.base_values=copy.deepcopy(self.values)
            self._read_generation()

    def get(self, name):
        if not self.unlocked:
            raise ValueError("Unlock connections first.")
        value = self.values.get(name)
        if value is None:
            raise ValueError("Saved authorization is missing. Reconnect this connection.")
        return value

    def put(self, name, value):
        if not self.unlocked:
            raise ValueError("Unlock connections first.")
        self.values[name] = value
        self.save()

    def remember_windows(self):
        if os.name != "nt" or not self.unlocked:
            return False
        payload = self._dpapi(self.key, protect=True)
        self.path.with_name("unlock.windows").write_bytes(payload)
        return True

    def try_service_key(self):
        """Linux service unlock, explicitly provisioned per workspace; never DPAPI."""
        path = self.path.with_name('service-unlock.json')
        if not path.exists() or not self.path.exists(): return False
        try:
            if os.name != 'nt' and path.stat().st_mode & 0o077: return False
            key = base64.b64decode(json.loads(path.read_text())['key'], validate=True)
            envelope = self._envelope()
            values = json.loads(AESGCM(key).decrypt(base64.b64decode(envelope['nonce']),
                base64.b64decode(envelope['ciphertext']), b'drivecast-vault-v1'))
            self.key, self.salt, self.values = key, base64.b64decode(envelope['salt']), values
            self.base_values=copy.deepcopy(values)
            self._read_generation()
            return True
        except Exception: return False

    def try_remembered_windows(self):
        path = self.path.with_name("unlock.windows")
        if os.name != "nt" or not path.exists() or not self.path.exists():
            return False
        try:
            key = self._dpapi(path.read_bytes(), protect=False)
            envelope = json.loads(self.path.read_text())
            values = json.loads(AESGCM(key).decrypt(base64.b64decode(envelope["nonce"]),
                base64.b64decode(envelope["ciphertext"]),b"drivecast-vault-v1"))
            self.key,self.salt,self.values = key,base64.b64decode(envelope["salt"]),values
            self.base_values=copy.deepcopy(values)
            return True
        except Exception:
            return False  # A copied Windows-bound unlock is expected to fail on another laptop.

    @staticmethod
    def _dpapi(data,protect):
        class Blob(ctypes.Structure):
            _fields_=[("size",ctypes.c_ulong),("data",ctypes.POINTER(ctypes.c_ubyte))]
        buffer=ctypes.create_string_buffer(data)
        incoming=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
        outgoing=Blob()
        crypt=ctypes.windll.crypt32
        if protect:
            success=crypt.CryptProtectData(ctypes.byref(incoming),None,None,None,None,1,ctypes.byref(outgoing))
        else:
            success=crypt.CryptUnprotectData(ctypes.byref(incoming),None,None,None,None,1,ctypes.byref(outgoing))
        if not success:
            raise OSError("Windows unlock protection failed.")
        try:
            return ctypes.string_at(outgoing.data,outgoing.size)
        finally:
            ctypes.windll.kernel32.LocalFree.argtypes=[ctypes.c_void_p]
            ctypes.windll.kernel32.LocalFree(ctypes.cast(outgoing.data,ctypes.c_void_p))
