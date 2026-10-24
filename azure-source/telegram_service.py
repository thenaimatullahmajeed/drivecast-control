"""Telegram integration. Bot credentials never enter public state or error text."""
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

DEFAULTS = dict(uploads=True,failures=True,reconnect=True,failover=True,daily=True,weekly=True,
                daily_time='20:00',timezone='America/Los_Angeles',commands=False)


def call(token, method, body=None):
    if not re.fullmatch(r'\d{5,16}:[A-Za-z0-9_-]{25,100}',token):
        raise ValueError('Enter the bot token from BotFather.')
    try:
        request=Request('https://api.telegram.org/bot'+token+'/'+method,
                        data=json.dumps(body or {}).encode(),headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=20) as response: result=json.load(response)
        if not result.get('ok'): raise ValueError()
        return result['result']
    except Exception:
        raise ValueError('Telegram request was not confirmed. Check bot token, chat permission and connection.') from None


def chunks(text, limit=3500):
    # UTF-16 units also fit the Bot API 4096-character limit, including emoji.
    part=[];units=0
    for char in str(text):
        size=2 if ord(char)>65535 else 1
        if units+size>limit:
            yield ''.join(part);part=[];units=0
        part.append(char);units+=size
    if part: yield ''.join(part)


def send(config, text):
    for part in chunks(text):
        call(config['token'],'sendMessage',dict(chat_id=config['chat_id'],text=part,link_preview_options={'is_disabled':True}))


def number(value): return str(value) if value is not None and value!='' else 'Unavailable'


def report(state, records, selected=None):
    lines=['DriveCast · last-known statistics', 'Workspace sync: '+(state.get('last_sync') or 'Unavailable')]
    for c in state.get('clients',{}).values():
        if selected and c['id']!=selected: continue
        stats=c.get('stats',{})
        lines.extend(['',c.get('channel_title') or c['name'],
            'Status: '+('Running' if c.get('enabled') else c.get('status','Paused')),
            'Source: Drive '+c.get('folder_name',c.get('folder_id','')),
            'YouTube: '+c.get('channel_id','Unavailable'),
            'Subscribers: '+number(stats.get('subscriberCount'))+' · channel views: '+number(stats.get('viewCount'))+' · channel videos: '+number(stats.get('videoCount')),
            'Channel stats checked: '+(stats.get('checked_at') or 'Unavailable'),
            '30-day views: '+number(c.get('analytics',{}).get('views_30d')),
            'Source available / blocked: '+number(c.get('source_stats',{}).get('available'))+' / '+number(c.get('source_stats',{}).get('blocked')),
            'Source checked: '+(c.get('source_stats',{}).get('checked_at') or 'Unavailable')])
        seen=set()
        for v in records.get('uploads',[]):
            if v['client_id']!=c['id'] or v['file_id'] in seen: continue
            seen.add(v['file_id'])
            lines.extend(['  '+(v.get('title') or v.get('filename') or 'Video'),
                '  YouTube: '+(v.get('youtube_url') or 'Unavailable')+' · requested '+v.get('requested_privacy','unknown')+' · actual '+v.get('actual_privacy','unknown'),
                '  Views / likes / comments: '+number(v.get('youtube_views'))+' / '+number(v.get('youtube_likes'))+' / '+number(v.get('youtube_comments')),
                '  Checked: '+(v.get('stats_checked_at') or 'Unavailable')])
    return '\n'.join(lines)


def connect(app, body):
    token=str(body.get('token','')).strip()
    me=call(token,'getMe')
    config=dict(token=token,chat_id='',pairing=secrets.token_urlsafe(24),offset=0)
    app.VAULT.put('telegram-bot',config)
    app.STATE['telegram']=dict(username=me['username'],linked=False,settings=dict(DEFAULTS),connected_at=app.now(),
                               start_link='https://t.me/'+me['username']+'?start='+config['pairing'])
    app.save_local()
    return dict(updated=True)


def settings(app, body):
    current=app.STATE.setdefault('telegram',{}).setdefault('settings',dict(DEFAULTS))
    for key in ('uploads','failures','reconnect','failover','daily','weekly','commands'):
        if key in body: current[key]=body[key] is True
    if 'daily_time' in body:
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',str(body['daily_time'])): raise ValueError('Use HH:MM.')
        current['daily_time']=body['daily_time']
    if 'timezone' in body:
        ZoneInfo(body['timezone']);current['timezone']=body['timezone']
    app.save_local()
    return dict(updated=True)


def poll(app, link_only=False):
    config=app.VAULT.get('telegram-bot')
    updates=call(config['token'],'getUpdates',dict(offset=config.get('offset',0),timeout=0,allowed_updates=['message']))
    for update in updates:
        config['offset']=max(config.get('offset',0),update['update_id']+1)
        message=update.get('message',{});chat=message.get('chat',{});text=message.get('text','').strip()
        if not config.get('chat_id') and text=='/start '+config.get('pairing','') and chat.get('type')=='private':
            config['chat_id']=str(chat['id']);config.pop('pairing',None)
            app.STATE['telegram'].update(linked=True,chat_label=chat.get('first_name') or chat.get('username') or 'Private chat',linked_at=app.now())
            app.STATE['telegram'].pop('start_link',None)
            send(config,'DriveCast connected. Notifications can run from prepared GitHub workers. Commands require your panel/server to be running.')
        elif str(chat.get('id',''))==config.get('chat_id') and not link_only and app.STATE.get('telegram',{}).get('settings',{}).get('commands'):
            command,_,name=text.partition(' ');command=command.split('@')[0]
            clients=list(app.STATE['clients'].values())
            selected=next((c for c in clients if name.strip().casefold() in (c['name'].casefold(),c.get('channel_title','').strip().casefold())),None)
            if command in ('/pause','/resume'):
                if not selected: send(config,'Use the exact channel name, for example /pause My channel.');continue
                try:
                    app.client_action(dict(id=selected['id'],action='pause' if command=='/pause' else 'enable',confirmed=True))
                    send(config,selected['name']+': '+('paused.' if command=='/pause' else 'start requested; Public results are verified separately.'))
                except Exception: send(config,'Action could not be completed. Open DriveCast to review this channel.')
            elif command in ('/stats','/today','/channel','/status'):
                if command=='/channel' and not selected: send(config,'Channel not found. Use its exact name.');continue
                records=app.store().data()
                if command=='/today':
                    zone=ZoneInfo(app.STATE['telegram'].get('settings',{}).get('timezone','America/Los_Angeles'))
                    today=datetime.now(zone).date()
                    rows=[]
                    for video in records['uploads']:
                        try:
                            if datetime.fromisoformat(video['recorded_at'].replace('Z','+00:00')).astimezone(zone).date()==today:rows.append(video)
                        except (ValueError,KeyError):pass
                    records=dict(records,uploads=rows)
                send(config,report(app.STATE,records,selected['id'] if command=='/channel' else None))
            else: send(config,'Commands: /today, /stats, /channel <name>, /status, /pause <name>, /resume <name>.')
    app.VAULT.put('telegram-bot',config);app.save_local()
    return dict(updated=True)


def notify(app, kind, text):
    config=app.VAULT.values.get('telegram-bot',{})
    if config.get('chat_id') and app.STATE.get('telegram',{}).get('settings',{}).get(kind,True): send(config,text)


def scheduled(app):
    config=app.VAULT.values.get('telegram-bot',{})
    if not config.get('chat_id'): return
    state=app.STATE['telegram'];settings=state.get('settings',DEFAULTS)
    moment=datetime.now(ZoneInfo(settings.get('timezone','America/Los_Angeles')))
    for kind,key,due in [('daily',moment.date().isoformat(),moment.strftime('%H:%M')>=settings.get('daily_time','20:00')),
                         ('weekly',moment.strftime('%G-%V'),moment.weekday()==6 and moment.strftime('%H:%M')>=settings.get('daily_time','20:00'))]:
        if settings.get(kind) and due and state.get('last_'+kind)!=key:
            send(config,kind.title()+' report\n'+report(app.STATE,app.store().data()))
            state['last_'+kind]=key;app.save_local()


def worker_notifications(exit_code=0):
    """Runs after worker results are durably saved. Never affects upload outcomes."""
    if os.getenv('DRY_RUN','true')=='true' or os.getenv('FORCE_PRIVATE')=='true' or not os.getenv('TELEGRAM_BOT_TOKEN') or not os.getenv('TELEGRAM_CHAT_ID'): return
    from github_api import GitHub, Ledger
    owner,repo=os.environ['GITHUB_REPOSITORY'].split('/')
    github=GitHub(os.environ['GITHUB_TOKEN'],owner)
    config=dict(token=os.environ['TELEGRAM_BOT_TOKEN'],chat_id=os.environ['TELEGRAM_CHAT_ID'])
    settings=json.loads(os.getenv('TELEGRAM_SETTINGS','{}'))
    raw,sha=github.read_file(repo,'records/telegram-notifications.json','drivecast-state',missing=True)
    sent=json.loads(raw) if raw else {'sent':{}}
    def once(key,text):
        nonlocal sha
        key=hashlib.sha256(key.encode()).hexdigest()
        if key in sent['sent']: return
        # Reserve before send; uncertain Telegram delivery is never blindly repeated.
        sent['sent'][key]=datetime.now(timezone.utc).isoformat()
        github.write_file(repo,'records/telegram-notifications.json',json.dumps(sent).encode(),'drivecast-state',sha,message='Reserve Telegram report')
        _,sha=github.read_file(repo,'records/telegram-notifications.json','drivecast-state')
        send(config,text)
    ledger=Ledger(github,repo,os.environ['EXPECTED_CHANNEL_ID'],path=os.getenv('YOUTUBE_LEDGER_PATH','records/ledger.json')).load(required=True)
    stats_raw,_=github.read_file(repo,'records/stats.json','drivecast-state',missing=True)
    stats=json.loads(stats_raw) if stats_raw else {}
    for key,row in ledger.get('items',{}).items():
        if row.get('recorded_at','')<settings.get('not_before','9999'): continue
        is_error=row.get('status') in ('Failed','Needs review')
        if not settings.get('failures' if is_error else 'uploads',True): continue
        video_id=row.get('youtube_id');count=stats.get('youtube',{}).get(video_id,{})
        text='DriveCast · '+os.getenv('CLIENT_NAME',repo)+'\n'+row.get('title',row.get('filename','Video'))
        text+='\n'+row.get('status','Unknown')+' · requested Public · actual '+row.get('privacy','unknown')
        text+='\nYouTube: '+('https://youtu.be/'+video_id if video_id else 'Unavailable')
        text+='\nViews / likes / comments: '+' / '.join(number(count.get(k)) for k in ('views','likes','comments'))+'\nChecked: '+(count.get('last_success') or count.get('checked_at') or 'Unavailable')
        channel=stats.get('channel',{})
        text+='\nChannel subscribers / views / videos: '+' / '.join(number(channel.get(k)) for k in ('subscriberCount','viewCount','videoCount'))+' · checked '+(stats.get('checked_at') or 'Unavailable')
        once(key+':'+str(video_id)+':'+row.get('status',''),text)
    kind='reconnect' if os.getenv('RUN_FAILURE_KIND')=='reconnect' else 'failures'
    if exit_code and settings.get(kind,True): once('run:'+os.getenv('GITHUB_RUN_ID','')+':'+os.getenv('GITHUB_RUN_ATTEMPT','1'),
        ('DriveCast reconnect required: ' if kind=='reconnect' else 'DriveCast worker needs attention: ')+os.getenv('CLIENT_NAME',repo)+'\nhttps://github.com/'+owner+'/'+repo+'/actions/runs/'+os.getenv('GITHUB_RUN_ID',''))
