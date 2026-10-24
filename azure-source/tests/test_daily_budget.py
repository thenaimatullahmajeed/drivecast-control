import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from drive_fixtures import Drive, tool, video
import drive_fixtures as helpers


class DailyBudgetTests(unittest.TestCase):
    run_main = helpers.LifecycleTests.run_main

    def test_split_runtime_never_sends_drive_credential_to_youtube(self):
        drive=Drive([video()]); youtube=MagicMock()
        youtube.__enter__.return_value=youtube
        drive_credential,youtube_credential=MagicMock(),MagicMock()
        with patch.dict(helpers.os.environ,dict(helpers.ENV,YOUTUBE_REFRESH_TOKEN='youtube-fixture'),clear=True), \
             patch.object(tool,'Credentials',side_effect=[drive_credential,youtube_credential]) as credentials, \
             patch.object(tool,'Request'),patch.object(tool,'upload',return_value='yt'), \
             patch.object(tool,'build',side_effect=[drive,youtube]) as build:
            self.assertEqual(tool.main(),0)
        self.assertEqual(credentials.call_args_list[0].kwargs['scopes'],[tool.SCOPES[0]])
        self.assertEqual(credentials.call_args_list[1].kwargs['refresh_token'],'youtube-fixture')
        self.assertNotIn(tool.SCOPES[0],credentials.call_args_list[1].kwargs['scopes'])
        self.assertIs(build.call_args_list[0].kwargs['credentials'],drive_credential)
        self.assertIs(build.call_args_list[1].kwargs['credentials'],youtube_credential)

    def test_split_expired_youtube_token_fails_before_drive_access(self):
        drive_credential,youtube_credential=MagicMock(),MagicMock()
        youtube_credential.refresh.side_effect=helpers.RefreshError('invalid_grant')
        with patch.dict(helpers.os.environ,dict(helpers.ENV,YOUTUBE_REFRESH_TOKEN='youtube-fixture'),clear=True), \
             patch.object(tool,'Credentials',side_effect=[drive_credential,youtube_credential]), \
             patch.object(tool,'Request'),patch.object(tool,'build') as build, self.assertRaises(helpers.RefreshError):
            tool.main()
        build.assert_not_called()

    def test_split_dry_run_refreshes_only_drive_token(self):
        drive=Drive([video()])
        with patch.dict(helpers.os.environ,dict(helpers.ENV,YOUTUBE_REFRESH_TOKEN='youtube-fixture',DRY_RUN='true'),clear=True), \
             patch.object(tool,'Credentials') as credentials,patch.object(tool,'Request'), \
             patch.object(tool,'build',return_value=drive) as build:
            self.assertEqual(tool.main(),0)
        self.assertEqual(credentials.call_count,1)
        self.assertEqual(build.call_args.args[0],'drive')

    def test_manual_reruns_and_fresh_workers_stop_at_two(self):
        drive = Drive([video(str(i), f'{i}.mp4') for i in range(4)])
        with patch.object(tool, 'upload', return_value='yt') as upload:
            for _ in range(4):
                self.assertEqual(self.run_main(drive), 0)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(drive.data['2']['parents'], ['root'])
        self.assertEqual(drive.data['3']['parents'], ['root'])

    def test_failure_consumes_slot_and_large_run_cannot_bypass_cap(self):
        drive = Drive([video(str(i), f'{i}.mp4') for i in range(4)])
        with patch.object(tool, 'upload', side_effect=TimeoutError('uncertain')) as upload:
            self.assertEqual(self.run_main(drive, UPLOAD_LIMIT='50'), 1)
            self.assertEqual(self.run_main(drive, UPLOAD_LIMIT='50'), 0)
        self.assertEqual(upload.call_count, 2)

    def test_dry_run_does_not_use_daily_slots(self):
        drive = Drive([video()])
        self.run_main(drive, DRY_RUN='true')
        self.assertNotIn(tool.DAILY_KEY, drive.data['root']['properties'])

    def test_lost_reservation_ack_prevents_any_upload(self):
        def fault(id, body, destination):
            if id == 'root':
                raise TimeoutError('reservation response lost')
        drive = Drive([video()], fault=fault)
        with patch.object(tool, 'upload') as upload, self.assertRaises(TimeoutError):
            self.run_main(drive)
        upload.assert_not_called()
        self.assertEqual(drive.data['v1']['parents'], ['root'])
        self.assertEqual(json.loads(drive.data['root']['properties'][tool.DAILY_KEY])['count'], 1)

    def test_new_york_midnight_and_dst_use_local_date(self):
        drive = Drive()
        points = [
            ('2026-01-02T02:00:00+00:00', '2026-01-01'),
            ('2026-07-02T02:00:00+00:00', '2026-07-01'),
        ]
        for stamp, expected in points:
            drive.data['root']['properties'] = {}
            now = datetime.fromisoformat(stamp)
            with patch.object(tool, 'datetime') as clock:
                clock.now.side_effect = lambda tz: now.astimezone(tz)
                tool.daily_budget(drive, 'root', reserve=True)
            ledger = json.loads(drive.data['root']['properties'][tool.DAILY_KEY])
            self.assertEqual(ledger, {'date': expected, 'count': 1})

    def test_next_day_resets_but_future_or_corrupt_ledger_stops(self):
        drive = Drive()
        with patch.object(tool, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
            for ledger in [{'date':'2026-10-15','count':2}, {'date':'2026-10-16','count':2}]:
                drive.data['root']['properties'][tool.DAILY_KEY] = json.dumps(ledger)
                expected = 2 if ledger['date'].endswith('15') else 0
                self.assertEqual(tool.daily_budget(drive, 'root'), expected)
            for raw in ['broken', '{"date":"2099-01-01","count":0}', '{"date":"2026-10-16","count":-1}']:
                drive.data['root']['properties'][tool.DAILY_KEY] = raw
                with self.assertRaises(ValueError): tool.daily_budget(drive, 'root', reserve=True)


if __name__ == '__main__':
    unittest.main()
