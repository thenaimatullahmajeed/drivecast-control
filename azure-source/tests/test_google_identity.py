import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import google_identity as identity
import upgrade
import test_control_v2 as base


class IdentityTests(unittest.TestCase):
    def test_verified_google_identity_uses_header_not_url(self):
        response=MagicMock()
        response.__enter__.return_value.read.return_value=json.dumps(dict(email='manager@example.com',email_verified=True,sub='123')).encode()
        with patch.object(identity,'urlopen',return_value=response) as get:
            self.assertEqual(identity.fetch('private-access-token'),dict(email='manager@example.com',subject='123'))
        request=get.call_args.args[0]
        self.assertEqual(request.full_url,'https://openidconnect.googleapis.com/v1/userinfo')
        self.assertEqual(request.get_header('Authorization'),'Bearer private-access-token')

    def test_unverified_missing_and_malformed_identity_are_rejected(self):
        for data in (dict(email='a@example.com',email_verified=False,sub='1'),dict(email='a@example.com',email_verified=True),[],dict(email='<bad>',email_verified=True,sub='1')):
            response=MagicMock()
            response.__enter__.return_value.read.return_value=json.dumps(data).encode()
            with self.subTest(data=data),patch.object(identity,'urlopen',return_value=response),self.assertRaises(ValueError):
                identity.fetch('private-access-token')

    def test_provider_errors_do_not_leak_access_token(self):
        with patch.object(identity,'urlopen',side_effect=OSError('private-access-token')),self.assertRaises(ValueError) as error:
            identity.fetch('private-access-token')
        self.assertNotIn('private-access-token',str(error.exception))

    def test_google_email_aliases_are_accepted(self):
        self.assertTrue(identity.has_scopes('openid email',identity.SCOPES))
        self.assertTrue(identity.has_scopes(identity.SCOPES,['openid','email']))
        self.assertFalse(identity.has_scopes(['openid'],identity.SCOPES))

    def test_old_grant_missing_email_does_not_invalidate_channel(self):
        row=dict(status='Connected',label='Channel')
        with patch.object(identity,'fetch',side_effect=ValueError('Permission unavailable')):
            self.assertFalse(identity.update(row,SimpleNamespace(token='token'),['youtube.upload'],'now'))
        self.assertEqual(row['status'],'Connected')
        self.assertEqual(row['google_email_status'],'Needs permission')
        self.assertNotIn('google_email',row)

    def test_transient_check_retains_previous_verified_email_and_timestamp(self):
        row=dict(google_email='manager@example.com',google_email_verified=True,google_email_checked_at='old')
        with patch.object(identity,'fetch',side_effect=ValueError('Unavailable')):
            identity.update(row,SimpleNamespace(token='token'),identity.SCOPES,'new')
        self.assertEqual(row['google_email_checked_at'],'old')
        self.assertEqual(row['google_email'],'manager@example.com')
        self.assertEqual(row['google_email_status'],'Check needed')


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.h=base.ControlTests();self.h.setUp()
        self.h.vault.put('google-desktop',{'installed':{'client_id':'test-id','client_secret':'test-secret'}})
    def tearDown(self):self.h.tearDown()

    def existing(self):
        self.h.state['connections']['yt']=dict(id='yt',kind='youtube',label='Channel',channel_id=base.CHANNEL,
            google_email='old@example.com',google_email_verified=True,google_subject='old-subject')
        self.h.vault.put('yt',dict(REFRESH_TOKEN='old-refresh'))

    def test_wrong_channel_restores_previous_email_and_credentials(self):
        self.existing()
        def check(cid):
            self.assertNotIn('google_email',self.h.state['connections'][cid])
            raise ValueError('Wrong channel')
        with patch.object(app,'authorize',return_value=dict(REFRESH_TOKEN='new-refresh')),patch.object(app,'check_google',side_effect=check):
            with self.assertRaisesRegex(ValueError,'Wrong channel'):
                app.connect_google(dict(kind='youtube',id='yt'))
        self.assertEqual(self.h.state['connections']['yt']['google_email'],'old@example.com')
        self.assertEqual(self.h.vault.get('yt')['REFRESH_TOKEN'],'old-refresh')

    def test_reconnect_same_channel_replaces_authorizing_email(self):
        self.existing()
        def check(cid):
            with patch.object(identity,'fetch',return_value=dict(email='new@example.com',subject='new-subject')):
                identity.update(self.h.state['connections'][cid],SimpleNamespace(token='token'),identity.SCOPES,'new')
        with patch.object(app,'authorize',return_value=dict(REFRESH_TOKEN='new-refresh')) as consent,patch.object(app,'check_google',side_effect=check):
            app.connect_google(dict(kind='youtube',id='yt'))
        self.assertTrue(identity.has_scopes(consent.call_args.kwargs['scopes'],identity.SCOPES))
        self.assertEqual(self.h.state['connections']['yt']['google_email'],'new@example.com')

    def test_new_grant_does_not_reuse_previous_managers_email_on_lookup_failure(self):
        self.existing()
        def check(cid):
            with patch.object(identity,'fetch',side_effect=ValueError('Unavailable')):
                identity.update(self.h.state['connections'][cid],SimpleNamespace(token='token'),identity.SCOPES,'new')
        with patch.object(app,'authorize',return_value=dict(REFRESH_TOKEN='new-refresh')),patch.object(app,'check_google',side_effect=check):
            app.connect_google(dict(kind='youtube',id='yt'))
        self.assertNotIn('google_email',self.h.state['connections']['yt'])

    def test_drive_grants_remain_drive_only(self):
        with patch.object(app,'authorize',return_value=dict(REFRESH_TOKEN='new-refresh')) as consent,patch.object(app,'check_google'):
            app.connect_google(dict(kind='drive',label='Drive'))
        self.assertEqual(consent.call_args.kwargs['scopes'],[app.helpers.SCOPES[0]])

    def test_email_refresh_only_updates_youtube_metadata_without_worker_changes(self):
        self.existing()
        self.h.state['connections']['gh']=dict(id='gh',kind='github',label='Operator')
        self.h.state['clients']['client']=dict(id='client',enabled=True,youtube_connection='yt')
        with patch.object(app,'google_credentials',return_value=SimpleNamespace(token='token')),patch.object(identity,'fetch',return_value=dict(email='manager@example.com',subject='123')),patch.object(app,'provision_google_secrets') as worker:
            result=upgrade.google_emails(app)
        self.assertEqual(result['updated'],1)
        self.assertEqual(len(result['rows']),1)
        self.assertTrue(self.h.state['clients']['client']['enabled'])
        worker.assert_not_called()
        self.assertEqual(self.h.state['connections']['yt']['google_email'],'manager@example.com')


if __name__=='__main__':unittest.main()
