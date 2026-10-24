import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'work' / 'qa-deps'))
sys.path.insert(0, os.environ.get('TOOL_DIRECTORY', str(ROOT)))
import main as tool
import get_token
from googleapiclient.errors import HttpError
from googleapiclient.discovery import build
from google.auth.exceptions import RefreshError
import httplib2


def http_error(reason, status=403):
    return HttpError(httplib2.Response({'status': str(status)}),
                     json.dumps({'error': {'message': reason, 'errors': [{'reason': reason}]}}).encode())


class Operation:
    def __init__(self, call): self.call = call
    def execute(self, num_retries=0): return self.call()


class Drive:
    def __init__(self, files=(), fault=None):
        self.data = {f['id']: copy.deepcopy(f) for f in files}
        self.data.setdefault('root', {'id':'root', 'name':'Source', 'mimeType':'application/vnd.google-apps.folder', 'properties':{}})
        self.events = []
        self.fault = fault
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def files(self): return self
    def list(self, **kw):
        folder = kw['q'].split("'")[1]
        def call():
            self.events.append(('list', folder))
            files = [copy.deepcopy(f) for f in self.data.values() if folder in f.get('parents', [])]
            return {'files': sorted(files, key=lambda f: (f.get('createdTime', ''), f['name']))}
        return Operation(call)
    def get(self, fileId, **kw): return Operation(lambda: copy.deepcopy(self.data[fileId]))
    def create(self, body, **kw):
        def call():
            item = dict(body, id=body['name'], createdTime='2020')
            self.data[item['id']] = item
            self.events.append(('create', item['id']))
            return item
        return Operation(call)
    def update(self, fileId, body=None, addParents=None, removeParents=None, **kw):
        def call():
            self.events.append(('update', fileId, addParents, copy.deepcopy(body)))
            item = self.data[fileId]
            if body: item.update(body)
            if addParents:
                item['parents'] = [p for p in item.get('parents', []) if p != removeParents]
                if addParents not in item['parents']: item['parents'].append(addParents)
            if self.fault: self.fault(fileId, body, addParents)
            return copy.deepcopy(item)
        return Operation(call)


def video(id='v1', name='first.mp4', date='2020-01-01', parent='root'):
    return {'id': id, 'name': name, 'createdTime': date, 'mimeType': 'video/mp4', 'parents': [parent]}


ENV = {'CLIENT_ID': 'client', 'CLIENT_SECRET': 'secret', 'REFRESH_TOKEN': 'refresh', 'FOLDER_ID': 'root'}


class LifecycleTests(unittest.TestCase):
    def run_main(self, drive, **env):
        youtube = MagicMock()
        with patch.dict(os.environ, dict(ENV, **env), clear=True), \
             patch.object(tool, 'Credentials'), patch.object(tool, 'Request'), \
             patch.object(tool, 'build', side_effect=lambda name, *a, **k: drive if name == 'drive' else youtube):
            return tool.main()

    def test_success_claim_before_upload_preserve_description_done(self):
        v = video(); v['description'] = 'Keep this'
        drive = Drive([v])
        def upload(*args):
            self.assertEqual(drive.data['v1']['parents'], ['Processing'])
            return 'yt-id'
        with patch.object(tool, 'upload', side_effect=upload) as up:
            self.assertEqual(self.run_main(drive), 0)
            self.assertEqual(self.run_main(drive), 0)
        self.assertEqual(up.call_count, 1)
        self.assertEqual(drive.data['v1']['parents'], ['Done'])
        self.assertEqual(drive.data['v1']['description'], 'Keep this\nYouTube video ID: yt-id')

    def test_failed_upload_never_retried_next_run(self):
        drive = Drive([video()])
        with patch.object(tool, 'upload', side_effect=TimeoutError('lost final response')) as up:
            self.assertEqual(self.run_main(drive), 1)
            self.assertEqual(self.run_main(drive), 0)
        self.assertEqual(up.call_count, 1)
        self.assertEqual(drive.data['v1']['parents'], ['Failed'])

    def test_existing_processing_warned_and_untouched(self):
        folder = {'id': 'Processing', 'name': 'Processing', 'mimeType': tool.FOLDER_MIME, 'parents': ['root']}
        drive = Drive([folder, video(parent='Processing')])
        with patch.object(tool, 'upload') as up, self.assertLogs(tool.LOG, level='WARNING') as logs:
            self.assertEqual(self.run_main(drive), 0)
        up.assert_not_called()
        self.assertIn('v1', '\n'.join(logs.output))
        self.assertIn('STUCK IN PROCESSING', '\n'.join(logs.output))

    def test_quota_stops_after_quarantine_and_leaves_next_queued(self):
        drive = Drive([video(), video('v2', 'second.mov', '2021')])
        with patch.object(tool, 'upload', side_effect=http_error('quotaExceeded')) as up:
            self.assertEqual(self.run_main(drive), 0)
        self.assertEqual(up.call_count, 1)
        self.assertEqual(drive.data['v1']['parents'], ['Failed'])
        self.assertEqual(drive.data['v2']['parents'], ['root'])

    def test_oldest_first_extension_filter_and_limit(self):
        drive = Drive([video('new', 'new.MKV', '2023'), video('old', 'old.mov', '2021'),
                       video('sub', 'nested.mp4', '2019', 'subfolder'), video('text', 'notes.txt', '2010')])
        with patch.object(tool, 'video_body', return_value={}), patch.object(tool, 'upload', return_value='yt') as up:
            self.assertEqual(self.run_main(drive, UPLOAD_LIMIT='1'), 0)
        self.assertEqual(up.call_args.args[2]['id'], 'old')
        self.assertEqual(drive.data['new']['parents'], ['root'])

    def test_dry_run_no_mutations_sidecar_reads_or_youtube_calls(self):
        drive = Drive([video()])
        with patch.object(tool, 'upload') as up, patch.object(tool, 'video_body') as meta, \
             patch.object(tool, 'build') as build_mock, patch.object(tool, 'Credentials'), patch.object(tool, 'Request'), \
             patch.dict(os.environ, dict(ENV, DRY_RUN='true'), clear=True):
            build_mock.return_value = drive
            self.assertEqual(tool.main(), 0)
        up.assert_not_called(); meta.assert_not_called()
        self.assertEqual([e[0] for e in drive.events], ['list'])
        self.assertEqual(build_mock.call_count, 1)
        self.assertEqual(build_mock.call_args.args[0], 'drive')

    def test_ambiguous_claim_aborts_without_any_upload(self):
        def fault(file_id, body, destination):
            if destination == 'Processing': raise TimeoutError('move response lost')
        drive = Drive([video(), video('v2', 'second.mp4')], fault=fault)
        with patch.object(tool, 'upload') as up, self.assertRaises(tool.ClaimError):
            self.run_main(drive)
        up.assert_not_called()
        self.assertEqual(drive.data['v1']['parents'], ['Processing'])
        self.assertEqual(drive.data['v2']['parents'], ['root'])

    def test_bookkeeping_failure_after_success_does_not_upload_again(self):
        def fault(file_id, body, destination):
            if body and 'description' in body: raise RuntimeError('description response lost')
        drive = Drive([video()], fault=fault)
        with patch.object(tool, 'upload', return_value='known-id') as up:
            self.assertEqual(self.run_main(drive), 1)
            self.assertEqual(self.run_main(drive), 0)
        self.assertEqual(up.call_count, 1)
        self.assertEqual(drive.data['v1']['parents'], ['Failed'])
        self.assertIn('known-id', drive.data['v1']['description'])

    def test_failed_quarantine_stays_in_processing(self):
        def fault(file_id, body, destination):
            if destination == 'Failed':
                drive.data[file_id]['parents'] = ['Processing']
                raise TimeoutError('Drive unavailable')
        drive = Drive([video()], fault=fault)
        with patch.object(tool, 'upload', side_effect=TimeoutError('uncertain upload')) as up:
            self.assertEqual(self.run_main(drive), 1)
            self.assertEqual(self.run_main(drive), 0)
        self.assertEqual(up.call_count, 1)
        self.assertEqual(drive.data['v1']['parents'], ['Processing'])

    def test_marker_prevents_accidental_requeue(self):
        v = video(); v['description'] = tool.MARKER + 'known-id'
        with patch.object(tool, 'upload') as up:
            self.assertEqual(self.run_main(Drive([v])), 0)
        up.assert_not_called()

    def test_invalid_refresh_fails_before_drive(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(tool, 'Credentials') as creds, \
             patch.object(tool, 'Request'), patch.object(tool, 'build') as build_mock:
            creds.return_value.refresh.side_effect = RefreshError('invalid_grant')
            with self.assertLogs(tool.LOG, level='ERROR') as logs:
                self.assertEqual(tool.cli(), 1)
        build_mock.assert_not_called()
        self.assertIn('get_token.py', '\n'.join(logs.output))

    def test_pagination(self):
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.side_effect = [
            {'files': [video('one')], 'nextPageToken': 'next'}, {'files': [video('two')]}]
        self.assertEqual(len(tool.list_files(drive, 'root')), 2)
        calls = drive.files.return_value.list.call_args_list
        self.assertEqual(calls[1].kwargs['pageToken'], 'next')
        self.assertEqual(calls[0].kwargs['orderBy'], 'createdTime,name')

    def test_duplicate_queue_folder_rejected(self):
        files = [dict(id=str(i), name='Done', mimeType=tool.FOLDER_MIME) for i in range(2)]
        with self.assertRaises(ValueError): tool.get_folders(None, 'root', files, False)

    def test_defaults_title_length_and_metadata_override(self):
        with patch.object(tool, 'read_sidecar', return_value={}):
            body = tool.video_body(None, [], video(name='a' * 120 + '.mp4'), 'private', '22')
        self.assertEqual(len(body['snippet']['title']), 100)
        self.assertEqual(body['status'], {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False})
        with patch.object(tool, 'read_sidecar', return_value={'title': 'Override', 'description': 'Body',
                                                             'tags': ['one'], 'privacy': 'unlisted'}):
            body = tool.video_body(None, [], video(), 'private', '22')
        self.assertEqual(body['snippet']['title'], 'Override')
        self.assertEqual(body['status']['privacyStatus'], 'unlisted')

    def test_invalid_metadata_quarantines_before_upload(self):
        drive = Drive([video()])
        with patch.object(tool, 'read_sidecar', return_value={'tags': 'wrong'}), patch.object(tool, 'upload') as up:
            self.assertEqual(self.run_main(drive), 1)
        up.assert_not_called()
        self.assertEqual(drive.data['v1']['parents'], ['Failed'])


class FakeDownload:
    def __init__(self, target, request, chunksize): self.target = target
    def next_chunk(self, num_retries):
        self.target.write(b'video-bytes')
        return None, True


class Transport:
    def __init__(self, failure):
        self.calls = []
        self.failure = failure
        self.failed = False
    def request(self, uri, method='GET', body=None, headers=None, **kw):
        self.calls.append((method, uri))
        if method == 'POST':
            return httplib2.Response({'status': '200', 'location': 'https://upload.example/session-1'}), b''
        if not self.failed:
            self.failed = True
            if isinstance(self.failure, Exception): raise self.failure
            return httplib2.Response({'status': str(self.failure)}), b'{"error":{"message":"temporary"}}'
        return httplib2.Response({'status': '200'}), b'{"id":"same-session-video"}'


class ResumableTests(unittest.TestCase):
    def test_retry_budget_is_bounded(self):
        request = MagicMock()
        request.next_chunk.side_effect = ConnectionResetError('offline')
        with patch.object(tool.time, 'sleep') as sleep, self.assertRaises(ConnectionResetError):
            tool.next_upload_chunk(request)
        self.assertEqual(request.next_chunk.call_count, tool.RETRIES + 1)
        self.assertEqual(sleep.call_count, tool.RETRIES)

    def test_quota_not_retried(self):
        request = MagicMock()
        request.next_chunk.side_effect = http_error('quotaExceeded')
        with patch.object(tool.time, 'sleep') as sleep, self.assertRaises(HttpError):
            tool.next_upload_chunk(request)
        self.assertEqual(request.next_chunk.call_count, 1)
        sleep.assert_not_called()

    def test_network_failure_probes_session_before_resending_bytes(self):
        class PartialTransport:
            def __init__(self): self.calls = []
            def request(self, uri, method='GET', body=None, headers=None, **kw):
                self.calls.append((method, uri, headers))
                count = len(self.calls)
                if count == 1:
                    return httplib2.Response({'status': '200', 'location': 'https://upload.example/session'}), b''
                if count == 2: raise ConnectionResetError('lost response')
                if count == 3:
                    self_outer.assertEqual(headers['Content-Range'], 'bytes */11')
                    self_outer.assertEqual(headers['content-length'], '0')
                    return httplib2.Response({'status': '308', 'range': 'bytes=0-4'}), b''
                self_outer.assertEqual(headers['Content-Range'], 'bytes 5-10/11')
                self_outer.assertEqual(body.read(), b'-bytes')
                return httplib2.Response({'status': '200'}), b'{"id":"resumed"}'
        self_outer = self
        http = PartialTransport()
        youtube = build('youtube', 'v3', http=http, cache_discovery=False, static_discovery=True)
        with patch.object(tool, 'MediaIoBaseDownload', FakeDownload), patch.object(tool.time, 'sleep'):
            self.assertEqual(tool.upload(MagicMock(), youtube, video(), {'snippet': {}, 'status': {}}), 'resumed')
        self.assertEqual(sum(m == 'POST' for m, _, _ in http.calls), 1)

    def test_transient_failure_keeps_one_session(self):
        for failure in (503, ConnectionResetError('connection reset')):
            with self.subTest(failure=failure):
                http = Transport(failure)
                youtube = build('youtube', 'v3', http=http, cache_discovery=False, static_discovery=True)
                with patch.object(tool, 'MediaIoBaseDownload', FakeDownload), patch.object(tool.time, 'sleep'):
                    result = tool.upload(MagicMock(), youtube, video(), {'snippet': {'title': 'x'}, 'status': {}})
                self.assertEqual(result, 'same-session-video')
                self.assertEqual(sum(m == 'POST' for m, u in http.calls), 1)
                self.assertEqual(len({u for m, u in http.calls if m == 'PUT'}), 1)

    def test_expired_session_never_creates_second_upload(self):
        http = Transport(404)
        youtube = build('youtube', 'v3', http=http, cache_discovery=False, static_discovery=True)
        with patch.object(tool, 'MediaIoBaseDownload', FakeDownload), self.assertRaises(HttpError):
            tool.upload(MagicMock(), youtube, video(), {'snippet': {'title': 'x'}, 'status': {}})
        self.assertEqual(sum(m == 'POST' for m, u in http.calls), 1)


class MetadataAndOAuthTests(unittest.TestCase):
    def test_json_precedes_txt_and_utf8_bom_supported(self):
        files = [{'id': 'json', 'name': 'first.json', 'mimeType': 'application/json'},
                 {'id': 'txt', 'name': 'first.txt', 'mimeType': 'text/plain'}]
        drive = MagicMock()
        class Download:
            def __init__(self, target, request, chunksize): self.target = target
            def next_chunk(self, num_retries):
                self.target.write(b'\xef\xbb\xbf{"title":"Override"}')
                return None, True
        with patch.object(tool, 'MediaIoBaseDownload', Download):
            result = tool.read_sidecar(drive, files, video())
        self.assertEqual(result, {'title': 'Override'})
        self.assertEqual(drive.files.return_value.get_media.call_args.kwargs['fileId'], 'json')

    def test_duplicate_sidecar_rejected(self):
        files = [dict(id=str(i), name='first.json', mimeType='application/json') for i in range(2)]
        with self.assertRaises(ValueError): tool.read_sidecar(None, files, video())

    def test_txt_first_line_is_title_remaining_lines_description(self):
        files = [dict(id='txt', name='first.txt', mimeType='text/plain')]
        class Download:
            def __init__(self, target, request, chunksize): self.target = target
            def next_chunk(self, num_retries):
                self.target.write('Real video title\nDescription with unicode: café'.encode())
                return None, True
        with patch.object(tool, 'MediaIoBaseDownload', Download):
            self.assertEqual(tool.read_sidecar(MagicMock(), files, video()),
                             {'title':'Real video title', 'description': 'Description with unicode: café'})

    def test_oauth_loopback_rejects_bad_state_and_uses_pkce_and_both_scopes(self):
        threads, errors, auth_params, exchanges = [], [], [], []
        def browser(url):
            params = parse_qs(urlparse(url).query)
            auth_params.append(params)
            callback = params['redirect_uri'][0]
            def send_callback():
                try:
                    try:
                        urlopen(callback + '?' + urlencode({'state': 'wrong', 'code': 'bad'}), timeout=5)
                        errors.append('Bad state was accepted')
                    except HTTPError as err:
                        if err.code != 400: errors.append(str(err))
                    with urlopen(callback + '?' + urlencode({'state': params['state'][0], 'code': 'test-code'}), timeout=5) as response:
                        if response.status != 200: errors.append('Callback failed')
                except Exception as err: errors.append(str(err))
            thread = threading.Thread(target=send_callback, daemon=True)
            threads.append(thread); thread.start()
        def exchange(request, timeout):
            values = parse_qs(request.data.decode())
            exchanges.append(values)
            challenge = get_token.base64.urlsafe_b64encode(get_token.hashlib.sha256(
                values['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
            self.assertEqual(challenge, auth_params[0]['code_challenge'][0])
            return io.BytesIO(json.dumps({'refresh_token': 'test-refresh', 'scope': ' '.join(tool.SCOPES)}).encode())
        with tempfile.TemporaryDirectory(dir=ROOT / 'work') as directory:
            path = Path(directory) / 'client.json'
            path.write_text(json.dumps({'installed': {'client_id': 'test-client', 'client_secret': 'test-secret'}}))
            with patch.object(get_token, 'open_private', side_effect=browser), \
                 patch.object(get_token, 'urlopen', side_effect=exchange), patch('sys.stdout', new_callable=io.StringIO) as output:
                get_token.get_token(path)
            for thread in threads: thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(set(auth_params[0]['scope'][0].split()), set(tool.SCOPES))
        self.assertEqual(auth_params[0]['access_type'], ['offline'])
        self.assertEqual(exchanges[0]['code'], ['test-code'])
        self.assertIn('REFRESH_TOKEN=test-refresh', output.getvalue())


if __name__ == '__main__': unittest.main(verbosity=2)
