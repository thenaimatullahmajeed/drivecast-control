"""Deterministic, varied publication windows shared by the panel and workers."""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_ZONE = 'America/Los_Angeles'
DEFAULT_WINDOWS = '12:00-14:00,18:00-21:00'


def options(value=None):
    value = value or {}
    mode = value.get('schedule_mode', 'smart')
    if mode not in ('smart', 'interval'):
        raise ValueError('Choose preferred windows or regular checks.')
    zone = value.get('schedule_timezone', DEFAULT_ZONE)
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('Use a valid audience time zone, for example America/Los_Angeles.') from None
    text = str(value.get('schedule_windows', DEFAULT_WINDOWS)).strip()
    windows = []
    try:
        for span in text.split(','):
            start, end = span.strip().split('-')
            def minute(s):
                h, m = map(int, s.split(':'))
                if not 0 <= h <= 23 or not 0 <= m < 60:
                    raise ValueError()
                return h * 60 + m
            a, b = minute(start), minute(end)
            if b - a < 20:
                raise ValueError()
            windows.append((a, b))
        windows.sort()
        if not 1 <= len(windows) <= 4 or any(a[1] > b[0] for a,b in zip(windows, windows[1:])):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Use 1–4 separate windows of at least 20 minutes, e.g. 12:00-14:00,18:00-21:00.') from None
    return dict(schedule_mode=mode, schedule_timezone=zone, schedule_windows=text), windows


def slots(client, day):
    config, windows = options(client)
    count = int(client.get('max_uploads_per_day', 2))
    if not 1 <= count <= 100:
        raise ValueError('Daily maximum must be between 1 and 100.')
    seed = client.get('channel_id') or client.get('id') or 'channel'
    zone = ZoneInfo(config['schedule_timezone'])
    out = []
    # Distribute attempts across windows, then vary each slot within its own band.
    for index in range(count):
        w = index % len(windows)
        a, b = windows[w]
        b-=12  # Leave time for the next ten-minute worker tick before the window ends.
        repetitions = len(range(w, count, len(windows)))
        band = index // len(windows)
        lower = a + (b-a) * band // repetitions
        upper = a + (b-a) * (band+1) // repetitions
        digest = hashlib.sha256(f'{seed}:{day}:{index}'.encode()).digest()
        minute = lower + int.from_bytes(digest[:4], 'big') % max(1, upper-lower)
        moment = datetime.combine(day, datetime.min.time(), tzinfo=zone) + timedelta(minutes=minute)
        out.append(moment.astimezone(timezone.utc))
    return sorted(out)


def next_slot(client, now=None, used=0):
    now = now or datetime.now(timezone.utc)
    config, windows = options(client)
    zone=ZoneInfo(config['schedule_timezone'])
    day = now.astimezone(ZoneInfo(config['schedule_timezone'])).date()
    for offset in range(3):
        remaining = slots(client, day+timedelta(days=offset))
        if offset == 0:
            if used >= int(client.get('max_uploads_per_day',2)):
                continue
            reservations=client.get('schedule_usage',{}).get('reservations')
            if reservations is None:
                remaining=remaining[int(used):]
            else:
                kept=[]
                for a,b in windows:
                    count=0
                    for stamp in reservations:
                        t=datetime.fromisoformat(stamp).astimezone(zone)
                        if t.date()==day and a<=t.hour*60+t.minute<b:count+=1
                    group=[s for s in remaining if a<=s.astimezone(zone).hour*60+s.astimezone(zone).minute<b]
                    kept.extend(group[count:])
                remaining=sorted(kept)
        for moment in remaining:
            if moment > now:
                return moment.isoformat()
            local=now.astimezone(zone);planned=moment.astimezone(zone)
            if offset==0 and any(a<=local.hour*60+local.minute<b and a<=planned.hour*60+planned.minute<b for a,b in windows):
                return moment.isoformat()
    return ''


def smart_cron(client):
    """Wake only around the windows, including both standard/DST UTC hours."""
    config, windows=options(client)
    zone=ZoneInfo(config['schedule_timezone'])
    year=datetime.now(timezone.utc).year
    hours=set()
    for month in (1,4,7,10):
        day=datetime(year,month,15,tzinfo=zone)
        for start,end in windows:
            for minute in range(start,end,5):
                hours.add((day+timedelta(minutes=minute)).astimezone(timezone.utc).hour)
    return '7-59/10 '+','.join(str(h) for h in sorted(hours))+' * * *'


def worker_allowance(ledger, maximum, now=None):
    """Never consume tomorrow's slot or batch a missed day's uploads."""
    if os.environ.get('SCHEDULE_MODE', 'interval') != 'smart' or os.environ.get('FORCE_PRIVATE') == 'true' or os.environ.get('DRY_RUN','true') == 'true':
        return maximum
    now = now or datetime.now(timezone.utc)
    client = dict(schedule_timezone=os.environ.get('SCHEDULE_TIMEZONE', DEFAULT_ZONE),
        schedule_windows=os.environ.get('SCHEDULE_WINDOWS', DEFAULT_WINDOWS),
        channel_id=os.environ.get('EXPECTED_CHANNEL_ID'), max_uploads_per_day=maximum)
    config, windows = options(client)
    local = now.astimezone(ZoneInfo(config['schedule_timezone']))
    minute = local.hour*60+local.minute
    if not any(a <= minute < b for a,b in windows):
        return 0
    window=next((span for span in windows if span[0]<=minute<span[1]))
    due = sum(s <= now and window[0]<=s.astimezone(local.tzinfo).hour*60+s.astimezone(local.tzinfo).minute<window[1] for s in slots(client, local.date()))
    # Existing reservation timestamps are authoritative across New York daily-cap
    # boundaries, retries and account migration. Failed attempts still consume a slot.
    used = 0;used_window=0
    for row in ledger.get('items', {}).values():
        try:
            moment = datetime.fromisoformat(row.get('reserved_at') or row['recorded_at'].replace('Z','+00:00'))
            if moment.astimezone(ZoneInfo(config['schedule_timezone'])).date() == local.date():
                used += 1
                when=moment.astimezone(local.tzinfo)
                if window[0]<=when.hour*60+when.minute<window[1]:used_window+=1
        except (KeyError, TypeError, ValueError):
            continue
    return min(1, max(0, due-used_window), max(0,maximum-used))


def usage(ledger,client,now=None):
    config,_=options(client)
    zone=ZoneInfo(config['schedule_timezone'])
    day=(now or datetime.now(timezone.utc)).astimezone(zone).date()
    count=0;reservations=[]
    for row in ledger.get('items',{}).values():
        try:
            stamp=row.get('reserved_at') or row['recorded_at']
            if datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(zone).date()==day:
                count+=1
                reservations.append(stamp.replace('Z','+00:00'))
        except (KeyError,TypeError,ValueError):pass
    return dict(date=day.isoformat(),count=count,reservations=reservations)


def public_schedule(client, now=None):
    now = now or datetime.now(timezone.utc)
    config, _ = options(client)
    active = client.get('schedule_deployed') is True
    mode = config['schedule_mode'] if active else 'interval'
    if not client.get('enabled'):
        return dict(mode=mode, next_at='', label='Stopped', **{k:v for k,v in config.items() if k!='schedule_mode'})
    if mode == 'smart':
        day=now.astimezone(ZoneInfo(config['schedule_timezone'])).date().isoformat()
        usage=client.get('schedule_usage',{})
        used=usage.get('count',0) if usage.get('date')==day else 0
        return dict(mode=mode, next_at=next_slot(client, now, used), label='Next posting window',
            basis='Preferred audience windows; times vary each day. Audience peak analytics are not connected.',
            **{k:v for k,v in config.items() if k!='schedule_mode'})
    minutes = int(client.get('check_minutes', 60))
    # Mirror the existing workflow's UTC cron, rather than reset a timer on refresh.
    start = now.replace(second=0, microsecond=0)
    for offset in range(1, 2882):
        t = start + timedelta(minutes=offset)
        match = t.minute >= 3 and (t.minute-3)%minutes == 0 if minutes < 60 else t.minute == 17 and (minutes == 60 or t.hour%(minutes//60) == 0)
        if match:
            return dict(mode='interval',next_at=t.isoformat(),label='Next source check',basis='A check can upload only when new permitted videos and daily allowance are available.')
    return dict(mode='interval',next_at='',label='Schedule unavailable')
