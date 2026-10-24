"""Drive-only safety, complete discovery, selection and history compatibility."""
import copy
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,drive_sources,workspace_schema,worker,control_services
import test_control_v2 as base


class DiscoveryTests(unittest.TestCase):
    def drive(self,responses):
        drive=MagicMock()
        drive.files.return_value.list.return_value.execute.side_effect=responses
        return drive

    def test_all_pages_and_nested_paths_and_duplicates_are_handled(self):
        drive=self.drive([
            dict(files=[dict(id='a',name='Same name',parents=['root'])],nextPageToken='two'),
            dict(files=[dict(id='b',name='Same name',parents=['root']),dict(id='done',name='Done',parents=['root'])]),
            dict(files=[dict(id='nested',name='Clips',parents=['a'])]),
            dict(files=[]),
        ])
        rows=drive_sources.descendants(drive,'root')
        self.assertEqual({r['id'] for r in rows},{'a','b','nested'})
        self.assertEqual(next(r for r in rows if r['id']=='nested')['path'],'Same name/Clips')
        calls=drive.files.return_value.list.call_args_list
        self.assertEqual(calls[1].kwargs['pageToken'],'two')
        self.assertNotIn('done',calls[-1].kwargs['q'])

    def test_empty_first_page_with_token_does_not_truncate_scan(self):
        drive=self.drive([dict(files=[],nextPageToken='two'),dict(files=[dict(id='a',name='A',parents=['root'])]),dict(files=[])])
        self.assertEqual(len(drive_sources.descendants(drive,'root')),1)

    def test_incomplete_or_repeated_page_stops(self):
        for responses in ([dict(files=[],incompleteSearch=True)],
                          [dict(files=[],nextPageToken='x'),dict(files=[],nextPageToken='x')]):
            with self.assertRaises(ValueError):drive_sources.descendants(self.drive(responses),'root')

    def test_folder_limit_never_returns_a_fake_complete_tree(self):
        drive=self.drive([dict(files=[dict(id=str(i),name=str(i),parents=['root']) for i in range(3)])])
        with patch.object(drive_sources,'MAX_FOLDERS',2),self.assertRaises(ValueError):drive_sources.descendants(drive,'root')

    def test_foreign_parent_is_rejected(self):
        drive=self.drive([dict(files=[dict(id='x',name='X',parents=['outside'])])])
        with self.assertRaisesRegex(ValueError,'ancestry'):drive_sources.descendants(drive,'root')

    def test_live_parent_walk_detects_a_folder_moved_outside_root(self):
        drive=MagicMock()
        drive.files.return_value.get.return_value.execute.side_effect=[dict(id='child',mimeType=drive_sources.FOLDER_MIME,parents=['outside']),dict(id='outside',mimeType=drive_sources.FOLDER_MIME,parents=[])]
        with self.assertRaisesRegex(ValueError,'no longer inside'):drive_sources.verify_ancestry(drive,'child','root')

    def test_live_nested_parent_walk_accepts_root(self):
        drive=MagicMock()
        drive.files.return_value.get.return_value.execute.side_effect=[dict(mimeType=drive_sources.FOLDER_MIME,parents=['parent']),dict(mimeType=drive_sources.FOLDER_MIME,parents=['root'])]
        drive_sources.verify_ancestry(drive,'child','root')
        self.assertEqual(drive.files.return_value.get.call_count,2)

    def test_trashed_or_cyclic_ancestry_is_rejected(self):
        for row in (dict(mimeType=drive_sources.FOLDER_MIME,parents=['root'],trashed=True),dict(mimeType=drive_sources.FOLDER_MIME,parents=['child'])):
            drive=MagicMock();drive.files.return_value.get.return_value.execute.return_value=row
            with self.assertRaises(ValueError):drive_sources.verify_ancestry(drive,'child','root')


class ProjectTests(unittest.TestCase):
    setUp=base.ControlTests.setUp
    tearDown=base.ControlTests.tearDown

    def root(self):
        self.state['connections']['drive']=dict(id='drive',kind='drive',label='Project Drive')
        self.state['drive_root']=dict(folder_id='root',connection_id='drive',name='Main',folders=[dict(id='child',name='Child',path='Child')])

    def test_unknown_folder_wrong_account_and_main_root_are_rejected(self):
        self.root()
        for fid,cid in (('outside','drive'),('root','drive'),('child','other')):
            with self.assertRaises(ValueError):drive_sources.selected(app,fid,cid)

    def test_one_connection_reused_without_reauthorization(self):
        self.root()
        root,row=drive_sources.selected(app,'child')
        self.assertEqual((root['connection_id'],row['id']),('drive','child'))

    def test_refresh_failure_preserves_previous_root_and_assignments(self):
        self.root();before=copy.deepcopy(self.state['drive_root'])
        drive=MagicMock();drive.__enter__.return_value=drive
        drive.files.return_value.get.return_value.execute.return_value=dict(id='root',name='Main',mimeType=drive_sources.FOLDER_MIME,capabilities={'canAddChildren':True})
        with patch.object(app,'google_credentials'),patch.object(app,'build',return_value=drive),patch.object(drive_sources,'descendants',side_effect=ValueError('incomplete')):
            with self.assertRaises(ValueError):drive_sources.discover(app)
        self.assertEqual(self.state['drive_root'],before)

    def test_discovery_associates_by_id_and_never_assigns_or_starts_channels(self):
        self.root();self.state['clients']['c']=dict(id='c',name='Channel',folder_id='child',enabled=False)
        before=copy.deepcopy(self.state['clients'])
        drive=MagicMock();drive.__enter__.return_value=drive
        drive.files.return_value.get.return_value.execute.return_value=dict(id='root',name='Main',mimeType=drive_sources.FOLDER_MIME,capabilities={'canAddChildren':True})
        with patch.object(app,'google_credentials'),patch.object(app,'build',return_value=drive),patch.object(drive_sources,'descendants',return_value=[dict(id='child',name='Child',path='Child'),dict(id='new',name='Channel',path='Channel')]):
            drive_sources.discover(app)
        self.assertEqual(self.state['clients'],before)
        self.assertEqual(self.state['drive_root']['folders'][0]['client_id'],'c')
        self.assertEqual(self.state['drive_root']['folders'][1]['client_id'],'')

    def test_removed_sources_are_rejected_before_credentials_or_network(self):
        with patch.object(app,'connection') as connection,patch.object(app,'build') as build:
            with self.assertRaises(ValueError):app.add_client({'source_type':'instagram'})
            with self.assertRaises(ValueError):control_services.validate_source(app,{'source_type':'instagram'})
        connection.assert_not_called();build.assert_not_called()

    def test_draft_can_never_restore_removed_source_settings(self):
        control_services.draft(app,dict(name='Channel',source_type='instagram',instagram_username='old',meta_connection='old'))
        self.assertEqual(self.state['draft']['source_type'],'drive')
        self.assertNotIn('meta_connection',self.state['draft'])

    def test_schema_migration_preserves_youtube_tokens_and_history(self):
        self.vault.put('yt',dict(REFRESH_TOKEN='keep',CLIENT_ID='client',CLIENT_SECRET='secret'))
        self.vault.put('meta:old',dict(token='remove'))
        self.state['connections'].update({'yt':dict(id='yt',kind='youtube'),'meta:old':dict(id='meta:old',kind='meta')})
        self.state['clients']['c']=dict(id='c',name='Channel',source_type='instagram',instagram_username='old',enabled=True,deployed=True,youtube_connection='yt',channel_id=base.CHANNEL,repo_name='repo')
        app.store().set_stats('c','youtube','accepted',dict(views=17))
        before=app.store().data()
        workspace_schema.drive_only(self.state,self.vault)
        client=self.state['clients']['c']
        self.assertEqual((client['source_type'],client['enabled'],client['needs_drive_folder']),('drive',False,True))
        self.assertEqual((client['youtube_connection'],client['channel_id'],client['repo_name']),('yt',base.CHANNEL,'repo'))
        self.assertEqual(self.vault.get('yt')['REFRESH_TOKEN'],'keep')
        self.assertNotIn('meta:old',self.vault.values)
        self.assertEqual(app.store().data(),before)

    def test_existing_drive_channel_is_not_paused_by_migration(self):
        self.state['clients']['c']=dict(id='c',name='Drive channel',source_type='drive',folder_id='child',enabled=True)
        workspace_schema.drive_only(self.state)
        self.assertTrue(self.state['clients']['c']['enabled'])
        self.assertEqual(self.state['clients']['c']['folder_id'],'child')

    def test_channel_without_folder_cannot_start_worker(self):
        self.state['clients']['c']=dict(id='c',needs_drive_folder=True)
        with patch.object(app,'github_for_client') as github:
            with self.assertRaisesRegex(ValueError,'subfolder'):app.client_action(dict(id='c',action='enable',confirmed=True))
        github.assert_not_called()

    def test_drive_authorization_reuses_the_main_folder_already_provided(self):
        self.state['drive_project']={'folder_id':'provided-main'}
        config={'installed':{'client_id':'client','client_secret':'secret'}}
        grant=dict(CLIENT_ID='client',CLIENT_SECRET='secret',REFRESH_TOKEN='refresh')
        with patch.object(app,'authorize',return_value=grant),patch.object(app,'check_google'), \
             patch.object(drive_sources,'discover',return_value={'updated':2}) as discover:
            result=app.connect_google(dict(kind='drive',oauth=config,label='Project Drive'))
        self.assertEqual(discover.call_args.args[1],dict(connection_id=result['connection_id'],folder_id='provided-main'))
        self.assertIn(result['connection_id'],self.state['connections'])

    def test_folder_permission_error_does_not_erase_successful_google_consent(self):
        self.state['drive_project']={'folder_id':'provided-main'}
        config={'installed':{'client_id':'client','client_secret':'secret'}}
        grant=dict(CLIENT_ID='client',CLIENT_SECRET='secret',REFRESH_TOKEN='refresh')
        with patch.object(app,'authorize',return_value=grant),patch.object(app,'check_google'), \
             patch.object(drive_sources,'discover',side_effect=ValueError('The Drive account needs Editor access.')):
            result=app.connect_google(dict(kind='drive',oauth=config,label='Project Drive'))
        self.assertEqual(self.vault.get(result['connection_id'])['REFRESH_TOKEN'],'refresh')
        self.assertIn('Editor access',self.state['drive_project']['notice'])


class WorkerBoundaryTests(unittest.TestCase):
    def test_legacy_source_environment_cannot_run_removed_fetching(self):
        with patch.dict(os.environ,{'SOURCE_TYPE':'instagram'},clear=True),patch.object(worker,'build') as build:
            with self.assertRaisesRegex(ValueError,'Google Drive'):worker.run()
        build.assert_not_called()


class AutomaticDiscoveryTests(unittest.TestCase):
    setUp=base.ControlTests.setUp
    tearDown=base.ControlTests.tearDown
    root=ProjectTests.root
    def test_fresh_inventory_does_not_hit_drive(self):
        self.root();self.state['drive_root']['checked_at']=app.now()
        with patch.object(drive_sources,'discover') as scan:
            self.assertFalse(drive_sources.refresh_if_due(app)['refreshed'])
        scan.assert_not_called()

    def test_new_channel_refreshes_even_a_recent_inventory(self):
        self.root();self.state['drive_root']['checked_at']=app.now()
        with patch.object(drive_sources,'discover',return_value={'updated':2}) as scan:
            self.assertTrue(drive_sources.refresh_if_due(app,force=True)['refreshed'])
            self.assertFalse(drive_sources.refresh_if_due(app,force=True)['refreshed'])
        scan.assert_called_once()

    def test_failure_keeps_folders_and_authorization_and_throttles_retry(self):
        self.root();before=copy.deepcopy(self.state['drive_root']['folders'])
        self.state['connections']['yt']=dict(id='yt',kind='youtube',status='Connected')
        with patch.object(drive_sources,'discover',side_effect=ValueError('Editor access required')) as scan:
            result=drive_sources.refresh_if_due(app)
            self.assertIn('previous list kept',result['notice'])
            self.assertFalse(drive_sources.refresh_if_due(app)['refreshed'])
        scan.assert_called_once();self.assertEqual(self.state['drive_root']['folders'],before)
        self.assertEqual(self.state['connections']['yt']['status'],'Connected')

    def test_due_refresh_is_discovery_only(self):
        self.root();self.state['clients']['existing']=dict(id='existing',enabled=True,folder_id='child')
        before=copy.deepcopy(self.state['clients'])
        with patch.object(drive_sources,'discover',return_value={'updated':2}) as scan:
            self.assertTrue(drive_sources.refresh_if_due(app)['refreshed'])
        scan.assert_called_once();self.assertEqual(before,self.state['clients'])

    def test_no_main_folder_never_creates_an_inventory(self):
        with patch.object(drive_sources,'discover') as scan:
            self.assertFalse(drive_sources.refresh_if_due(app,force=True)['refreshed'])
        scan.assert_not_called();self.assertNotIn('drive_root',self.state)

    def test_youtube_authorization_requests_fresh_inventory(self):
        config={'installed':{'client_id':'client','client_secret':'secret'}}
        grant=dict(CLIENT_ID='client',CLIENT_SECRET='secret',REFRESH_TOKEN='refresh')
        def verify(cid):self.state['connections'][cid].update(channel_id=base.CHANNEL,label='Channel')
        with patch.object(app,'authorize',return_value=grant),patch.object(app,'check_google',side_effect=verify),patch.object(drive_sources,'refresh_if_due') as refresh:
            app.connect_google(dict(kind='youtube',oauth=config,label='Channel'))
        self.assertEqual(refresh.call_args.kwargs,{'force':True})


if __name__=='__main__':unittest.main()
