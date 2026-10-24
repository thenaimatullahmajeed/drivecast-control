import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import github_api
import upgrade
import test_control_v2 as base


class DiagnosticsTests(unittest.TestCase):
    def error(self,message,status=403,headers=None):
        return HTTPError('https://api.github.com/user',status,'Denied',headers,io.BytesIO(json.dumps({'message':message}).encode()))

    def test_suspension_has_specific_safe_reason_and_is_not_quota(self):
        with patch.object(github_api,'urlopen',side_effect=self.error('Sorry. Your account was suspended')):
            with self.assertRaises(github_api.GitHubError) as result:github_api.GitHub('private-secret').request('/user')
        self.assertEqual(result.exception.reason,'suspended')
        self.assertIn('account suspended',str(result.exception))
        self.assertNotIn('private-secret',str(result.exception))

    def test_rate_limit_and_permissions_are_separate_from_suspension(self):
        for message,status,headers,expected in [('API rate limit exceeded',403,{},'rate_limit'),('Denied',429,{},'rate_limit'),
            ('Resource not accessible by personal access token',403,{},'permission'),('Denied',403,{'X-GitHub-SSO':'required; url=private-url'},'sso'),('Bad credentials',401,{},'authentication')]:
            with self.subTest(expected=expected),patch.object(github_api,'urlopen',side_effect=self.error(message,status,headers)):
                with self.assertRaises(github_api.GitHubError) as result:github_api.GitHub('private-secret').request('/user')
                self.assertEqual(result.exception.reason,expected)

    def test_unrecognized_provider_content_is_never_echoed(self):
        with patch.object(github_api,'urlopen',side_effect=self.error('secret-token and private-url in provider response')):
            with self.assertRaises(github_api.GitHubError) as result:github_api.GitHub('secret-token').request('/user')
        self.assertEqual(result.exception.reason,'unknown')
        self.assertNotIn('secret-token',str(result.exception));self.assertNotIn('private-url',str(result.exception))

    def test_health_marks_suspension_without_touching_workers_or_routes(self):
        h=base.ControlTests();h.setUp()
        try:
            cid=h.github('operator')
            h.state['clients']['client']=dict(id='client',github_connection=cid,enabled=True,deployed=True,repo_name='pipeline')
            with patch.object(app,'gh_for') as factory:
                factory.return_value.request.side_effect=github_api.GitHubError(403,'GitHub account suspended','suspended')
                upgrade.health(app,dict(id=cid))
            row=h.state['connections'][cid]
            self.assertEqual(row['status'],'Account suspended')
            self.assertEqual(row['health']['issue'],'suspended')
            self.assertFalse(row['health']['quota_issue']);self.assertFalse(row['health']['token_valid'])
            self.assertTrue(h.state['clients']['client']['enabled'])
            self.assertFalse(upgrade.pool(app)[0]['healthy'])
            self.assertEqual(factory.return_value.request.call_count,1)
        finally:h.tearDown()

    def test_stale_successful_health_cannot_hide_new_connection_failure(self):
        h=base.ControlTests();h.setUp()
        try:
            cid=h.github('operator')
            h.state['connections'][cid].update(status='Account suspended',health=dict(checked_at=app.now(),token_valid=True,rate_remaining=4000))
            self.assertFalse(upgrade.pool(app)[0]['healthy'])
        finally:h.tearDown()


if __name__=='__main__':unittest.main()
