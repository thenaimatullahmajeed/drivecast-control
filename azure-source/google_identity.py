"""Read the consented Google sign-in email; never infer a channel's creator/owner.

This uses minimal OpenID/email scopes, not the Gmail API or mailbox access.
"""
import json
import re
from urllib.request import Request,urlopen

SCOPES=('openid','https://www.googleapis.com/auth/userinfo.email')
FIELDS=('google_email','google_subject','google_email_verified','google_email_checked_at',
        'google_email_status','google_email_notice','google_email_last_attempt_at')


def scope_set(values):
    aliases={'email':'https://www.googleapis.com/auth/userinfo.email','profile':'https://www.googleapis.com/auth/userinfo.profile'}
    return {aliases.get(value,value) for value in (values.split() if isinstance(values,str) else values)}


def has_scopes(granted, requested):
    return scope_set(requested).issubset(scope_set(granted))


def fetch(access_token):
    if not access_token:raise ValueError('Google access token is unavailable.')
    try:
        request=Request('https://openidconnect.googleapis.com/v1/userinfo',headers={
            'Authorization':'Bearer '+access_token,'Accept':'application/json','User-Agent':'DriveCast'})
        with urlopen(request,timeout=20) as response:
            data=json.loads(response.read(65536))
    except Exception:
        raise ValueError('Google email could not be verified. Reconnect with email permission if needed.') from None
    if not isinstance(data,dict):raise ValueError('Google did not return a verified account email.')
    email=data.get('email','')
    if data.get('email_verified') is not True or not isinstance(email,str) or not re.fullmatch(r'[^\s@]{1,128}@[^\s@]{1,189}',email) or not data.get('sub'):
        raise ValueError('Google did not return a verified account email.')
    return dict(email=email,subject=str(data['sub']))


def update(row, credentials, scopes, checked_at):
    """Identity failures do not invalidate a separately verified channel upload grant."""
    row['google_email_last_attempt_at']=checked_at
    try:
        identity=fetch(getattr(credentials,'token',None))
    except ValueError:
        consented=has_scopes(scopes or (),SCOPES)
        row.update(google_email_status='Check needed' if consented else 'Needs permission',
            google_email_notice=('Google email check unavailable. Last verified email is retained, if present.' if consented else
                                 'Reconnect this channel once to share its Google sign-in email. Existing publishing access is unchanged.'))
        return False
    row.update(google_email=identity['email'],google_subject=identity['subject'],google_email_verified=True,
        google_email_checked_at=checked_at,google_email_status='Verified',google_email_notice='')
    return True
