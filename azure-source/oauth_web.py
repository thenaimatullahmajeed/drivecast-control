"""Hosted OAuth callbacks, routed to the pending isolated workspace by the gateway.

No browser cookie is required on the provider return; unpredictable one-time
OAuth state binds the callback to its provider and workspace process.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
from google_identity import has_scopes
from urllib.parse import urlencode
from urllib.request import Request,urlopen

PENDING={}
LOCK=threading.RLock()


def callback_url(provider):
    base=os.getenv('DRIVECAST_PUBLIC_CALLBACK_BASE','')
    if not base.startswith('https://') or provider != 'google':
        raise ValueError('Web OAuth requires the configured HTTPS accounts gateway. Use a Desktop app for local mode.')
    return os.getenv('DRIVECAST_OAUTH_REDIRECT') or base+'/'+provider


def receive(body):
    state=str(body.get('state',''));provider=body.get('provider')
    with LOCK:
        pending=PENDING.get(state)
        if not pending or pending['provider']!=provider or pending['deadline']<time.monotonic() or pending['event'].is_set():
            raise ValueError('OAuth link expired or was already used. Create a new authorization link.')
        if body.get('error'): pending['error']='Authorization was declined.'
        elif body.get('code'): pending['code']=str(body['code'])
        else: raise ValueError('Provider authorization code is missing.')
        pending['event'].set()
    return dict(ok=True)


def wait(provider, parameters, base_url, announce, timeout=600):
    state=(os.environ['DRIVECAST_WORKSPACE_ID']+'.' if os.getenv('DRIVECAST_WORKSPACE_ID') else '')+secrets.token_urlsafe(40)
    pending=dict(provider=provider,event=threading.Event(),deadline=time.monotonic()+timeout)
    with LOCK:PENDING[state]=pending
    try:
        announce(base_url+'?'+urlencode(dict(parameters,state=state)))
        if not pending['event'].wait(timeout): raise ValueError('Authorization timed out. Create a new link.')
        if pending.get('error'): raise ValueError(pending['error'])
        return pending['code']
    finally:
        with LOCK:PENDING.pop(state,None)


def google(config, scopes, announce):
    data=config.get('web',{})
    if not data.get('client_id') or not data.get('client_secret'): raise ValueError('Set a Web OAuth client ID and secret.')
    redirect=callback_url('google')
    verifier=secrets.token_urlsafe(64)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    code=wait('google',dict(client_id=data['client_id'],redirect_uri=redirect,response_type='code',scope=' '.join(scopes),access_type='offline',
        prompt='consent select_account',include_granted_scopes='false',code_challenge=challenge,code_challenge_method='S256'),
        'https://accounts.google.com/o/oauth2/v2/auth',announce)
    body=dict(client_id=data['client_id'],client_secret=data['client_secret'],redirect_uri=redirect,code=code,
              code_verifier=verifier,grant_type='authorization_code')
    try:
        with urlopen(Request('https://oauth2.googleapis.com/token',data=urlencode(body).encode(),
            headers={'Content-Type':'application/x-www-form-urlencoded'}),timeout=45) as response:tokens=json.load(response)
    except Exception:raise ValueError('Google token exchange was not confirmed. Check the registered Web OAuth redirect and app credentials.') from None
    if not tokens.get('refresh_token'):raise ValueError('Google did not grant offline access. Reconnect and approve all requested permissions.')
    if tokens.get('scope') and not has_scopes(tokens['scope'],scopes):raise ValueError('Grant every requested permission before reconnecting.')
    return dict(CLIENT_ID=data['client_id'],CLIENT_SECRET=data['client_secret'],REFRESH_TOKEN=tokens['refresh_token'])


