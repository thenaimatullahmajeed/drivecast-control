import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import operations
import upgrade
import test_control_v2 as base
import test_console_backend as prior


class CapacityTests(unittest.TestCase):
    def setUp(self):self.h=base.ControlTests();self.h.setUp()
    def tearDown(self):self.h.tearDown()
    def account(self,name):
        cid=self.h.github(name)
        self.h.state['connections'][cid].update(capacity=50,health=dict(checked_at=app.now(),token_valid=True,rate_remaining=4000))
        return cid

    def test_old_capacity_cannot_allow_more_than_two_channels(self):
        a=self.account('a');b=self.account('b')
        for i in range(2):self.h.state['clients'][str(i)]=dict(id=str(i),github_connection=a)
        self.assertEqual(upgrade.pool(app)[0]['capacity'],2)
        self.assertEqual(upgrade.assign(app),b)
        with self.assertRaisesRegex(ValueError,'Maximum 2'):upgrade.ensure_slot(app,a)
        with self.assertRaisesRegex(ValueError,'Maximum 2'):upgrade.pool_settings(app,dict(id=a,capacity=3))

    def test_existing_over_capacity_routes_are_preserved_but_not_extended(self):
        a=self.account('a')
        for i in range(3):self.h.state['clients'][str(i)]=dict(id=str(i),github_connection=a,enabled=True)
        self.assertEqual(upgrade.pool(app)[0]['free'],0)
        self.assertTrue(all(c['enabled'] for c in self.h.state['clients'].values()))
        with self.assertRaises(ValueError):upgrade.assign(app)

    def test_suspension_and_provider_rate_limits_never_trigger_account_switch(self):
        a=self.account('a');self.account('b')
        self.h.state['automation']=dict(armed=True,auto_failover=True)
        self.h.state['clients']['client']=dict(id='client',name='Channel',github_connection=a,deployed=True,enabled=True)
        for issue in ('suspended','rate_limit'):
            self.h.state['connections'][a].update(status='Account suspended' if issue=='suspended' else 'Check connection',
                health=dict(checked_at=app.now(),token_valid=False,issue=issue,quota_issue=issue=='rate_limit'))
            with self.subTest(issue=issue),patch.object(operations,'migration_preview') as preview,patch.object(operations,'migrate') as move:
                result=upgrade.failover(app,execute=True)
                self.assertEqual(result['rows'][0]['status'],'Blocked')
                self.assertNotIn('target',result['rows'][0]);preview.assert_not_called();move.assert_not_called()
                self.assertTrue(self.h.state['clients']['client']['enabled'])

    def test_suspended_source_cannot_use_manual_migration_api(self):
        f=prior.MigrationTests();f.setUp()
        try:
            f.s['connections']['gh:operator'].update(status='Account suspended',health=dict(issue='suspended'))
            result=operations.migration_preview(app,dict(source='gh:operator',target='gh:target'))
            self.assertFalse(result['ready']);self.assertIn('suspended',' '.join(result['rows'][0]['blockers']))
            with self.assertRaises(ValueError):operations.migrate(app,dict(source='gh:operator',target='gh:target',confirmed=True))
            self.assertEqual(f.events,[]);self.assertTrue(f.client['enabled'])
        finally:f.tearDown()

    def test_migration_api_enforces_destination_slots_before_mutation(self):
        f=prior.MigrationTests();f.setUp()
        try:
            for i in range(2):f.s['clients']['other'+str(i)]=dict(id='other'+str(i),github_connection='gh:target')
            result=operations.migration_preview(app,dict(ids=['one'],target='gh:target'))
            self.assertFalse(result['ready']);self.assertIn('Maximum 2',' '.join(result['rows'][0]['blockers']))
            self.assertEqual(f.events,[])
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
