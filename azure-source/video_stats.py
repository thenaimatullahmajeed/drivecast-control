"""Read-only per-video counters; missing/unsupported values stay unavailable."""
import json
import os
from datetime import datetime,timezone
from googleapiclient.discovery import build
from github_api import GitHub,Ledger


def stamp():return datetime.now(timezone.utc).isoformat()


def youtube_stats(youtube,ids):
    ids=list(dict.fromkeys(x for x in ids if x));out={}
    for offset in range(0,len(ids),50):
        batch=ids[offset:offset+50]
        result=youtube.videos().list(part='statistics,snippet,status',id=','.join(batch),maxResults=50).execute(num_retries=2)
        for video in result.get('items',[]):
            counts=video.get('statistics',{})
            out[video['id']]=dict(views=counts.get('viewCount'),likes=counts.get('likeCount'),comments=counts.get('commentCount'),
                title=video.get('snippet',{}).get('title',''),privacy=video.get('status',{}).get('privacyStatus',''),checked_at=stamp(),notice='')
        for video_id in batch:
            out.setdefault(video_id,dict(checked_at=stamp(),notice='Video unavailable to this connection; last successful counters retained.'))
    return out


def worker_sync(credentials):
    owner,repo=os.environ['GITHUB_REPOSITORY'].split('/')
    github=GitHub(os.environ['GITHUB_TOKEN'],owner)
    expected=os.environ['EXPECTED_CHANNEL_ID']
    ledger=Ledger(github,repo,expected,path=os.environ.get('YOUTUBE_LEDGER_PATH','records/ledger.json'))
    data=ledger.load(required=True)
    from main import verify_channel,SCOPES,CHANNEL_SCOPE
    auth=credentials(os.environ.get('YOUTUBE_REFRESH_TOKEN') or os.environ['REFRESH_TOKEN'],[SCOPES[1],CHANNEL_SCOPE])
    result=dict(version=1,checked_at=stamp(),youtube={})
    with build('youtube','v3',credentials=auth,cache_discovery=False) as yt:
        verify_channel(yt,expected)
        result['youtube']=youtube_stats(yt,[r.get('youtube_id') for r in data['items'].values()])
        channels=yt.channels().list(part='statistics',id=expected).execute(num_retries=2).get('items',[])
        result['channel']=channels[0].get('statistics',{}) if channels else {}
    raw,_=github.read_file(repo,'records/stats.json','drivecast-state',missing=True)
    previous=json.loads(raw) if raw else {}
    for platform in ('youtube',):
        for video_id,row in result[platform].items():
            if row.get('notice') and video_id in previous.get(platform,{}):
                row.update({k:v for k,v in previous[platform][video_id].items() if k in ('views','likes','comments','last_success')})
            elif not row.get('notice'):row['last_success']=row['checked_at']
    _,sha=github.read_file(repo,'records/stats.json','drivecast-state',missing=True)
    github.write_file(repo,'records/stats.json',json.dumps(result).encode(),'drivecast-state',sha,message='Refresh read-only video counters')
    print('Video/channel statistics refreshed. No upload or source mutation requested.',flush=True)
    return 0
