#!/usr/bin/env python3
"""CouncilWare committee calendar + preparation dates. Public, read-only source."""
import json
import re
import sys
import time as timelib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone, time
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = 'https://www.alamoareascouting.org/wp-content/plugins/councilware/calendar/render.php'
TZ = ZoneInfo('America/Chicago')
CALENDAR_ID = '27'
LOOKAHEAD_DAYS = 180
OUTPUT = Path('site/aac-committees.ics')
COMMITTEES = {
    'Aquatics': ('aquatics', 'aquatic'),
    'Climbing': ('climbing',),
    'COPE': ('cope', 'challenging outdoor personal experience'),
    'Fall Events': ('fall events', 'fall event'),
    'Fishing': ('fishing', 'angling'),
    'International': ('international',),
    'Radio': ('radio', 'jota', 'joti', 'jamboree on the air'),
    'RATA / Shooting Sports': ('rata', 'shooting sports', 'shooting sport', 'range and target'),
}
MEETING_TERMS = ('committee', 'meeting', 'meetings', 'planning session', 'planning call',
                 'workgroup', 'working group', 'staff call', 'staff meeting', 'roundtable')
PREP = ((7, 'Prepare agenda'), (3, 'Confirm details'), (1, 'Final preparation'))


def norm(s):
    return re.sub(r'\s+', ' ', str(s or '').casefold().replace('-', ' ')).strip()


def contains(text, word):
    return re.search(r'(?<!\w)' + re.escape(norm(word)) + r'(?!\w)', text) is not None


def classify(title):
    t = norm(title)
    if not any(contains(t, x) for x in MEETING_TERMS):
        return None
    for label, aliases in COMMITTEES.items():
        if any(contains(t, a) for a in aliases):
            return label
    return None


def localtime(s):
    d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)


def stamp(d):
    return d.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def escape(s):
    return str(s or '').replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(',', '\\,').replace(';', '\\;')


def fold(line):
    parts, current = [], ''
    for ch in line:
        if len((current + ch).encode('utf-8')) > 75:
            parts.append(current)
            current = ' ' + ch
        else:
            current += ch
    parts.append(current)
    return '\r\n'.join(parts)


def request_window(start, end):
    params = urllib.parse.urlencode({'calendarID': CALENDAR_ID, 'start': start.isoformat(), 'end': end.isoformat()})
    req = urllib.request.Request(BASE + '?' + params, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; AAC-CommitteeCalendar/1.2)',
        'Accept': 'application/json',
    })
    # Retry transient network or certificate-chain errors; NEVER disable TLS verification.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                data = json.loads(response.read().decode('utf-8-sig'))
            if not isinstance(data, list):
                raise ValueError('CouncilWare returned an unexpected non-list response')
            return data
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f'Could not fetch {start} to {end}: {exc}') from exc
            print(f'Retrying {start} to {end} following {type(exc).__name__}', flush=True)
            timelib.sleep(3 * (attempt + 1))


def fetch_all():
    today = datetime.now(TZ).date()
    stop = today + timedelta(days=LOOKAHEAD_DAYS)
    start = today - timedelta(days=1)
    found = {}
    count = 0
    print('Calendar ID:', CALENDAR_ID, '| Today:', today, '| End:', stop, flush=True)
    # Small date windows mirror the date-scoped FullCalendar requests.
    while start < stop:
        end = min(start + timedelta(days=28), stop)
        records = request_window(start, end)
        count += len(records)
        print(f'SOURCE WINDOW {start} to {end}: {len(records)} events', flush=True)
        for rec in records:
            event_id = rec.get('eventId')
            if event_id is not None:
                found[str(event_id)] = rec
        start = end
    if not found:
        raise RuntimeError('CouncilWare returned no records; keeping prior feed')
    print(f'SOURCE TOTAL: {count} rows / {len(found)} unique event IDs', flush=True)
    return list(found.values())


def add_event(lines, uid, title, start, end, location, url, description, cancelled=False):
    now = stamp(datetime.now(timezone.utc))
    lines.extend([
        'BEGIN:VEVENT', f'UID:{uid}', f'DTSTAMP:{now}',
        f'DTSTART:{stamp(start)}', f'DTEND:{stamp(end)}',
        f'SUMMARY:{escape(title)}', f'LOCATION:{escape(location)}',
        f'DESCRIPTION:{escape(description)}', f'URL:{url}',
        'STATUS:CANCELLED' if cancelled else 'STATUS:CONFIRMED',
        'TRANSP:TRANSPARENT', 'END:VEVENT',
    ])


def generate(events):
    now = datetime.now(TZ)
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//AAC Committee Calendar//EN',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:AAC Committee Meetings',
             'X-WR-TIMEZONE:America/Chicago']
    meeting_count = prep_count = 0
    committee_counts = {}
    other_meeting_titles = []
    for rec in sorted(events, key=lambda x: x.get('start', '')):
        title = str(rec.get('title') or rec.get('fullName') or '')
        label = classify(title)
        if not label:
            if any(contains(norm(title), w) for w in MEETING_TERMS):
                other_meeting_titles.append(title)
            continue
        if not rec.get('start') or not rec.get('end'):
            print('SKIPPED (missing time):', title, flush=True)
            continue
        try:
            start, end = localtime(rec['start']), localtime(rec['end'])
        except ValueError:
            print('SKIPPED (bad time):', title, flush=True)
            continue
        if end <= start or end < now:
            continue
        event_id = str(rec['eventId'])
        url = rec.get('eventPageURL') or f'https://mycouncil.alamoareascouting.org/Event/{event_id}'
        cancelled = str(rec.get('isCancelled', False)).lower() == 'true'
        add_event(lines, f'aac-{event_id}@alamoareascouting.org', title, start, end,
                  rec.get('location') or '', url, f'{label} committee meeting. Source: {url}', cancelled)
        meeting_count += 1
        committee_counts[label] = committee_counts.get(label, 0) + 1
        print(f'INCLUDED: {start:%Y-%m-%d} | {label} | {title}', flush=True)
        if cancelled:
            continue
        for days, action in PREP:
            day = start.date() - timedelta(days=days)
            prep_start = datetime.combine(day, time(9, 0), tzinfo=TZ)
            prep_end = prep_start + timedelta(minutes=15)
            if prep_end <= now:
                continue
            add_event(lines, f'aac-prep-{event_id}-{days}d@alamoareascouting.org',
                      f'[Prep] {action} — {title}', prep_start, prep_end, '', url,
                      f'{label}: {action} for {title}. {url}')
            prep_count += 1
    print('\nCOMMITTEE BREAKDOWN:', committee_counts, flush=True)
    print('OTHER MEETING TITLES (not matched):', flush=True)
    for title in sorted(set(other_meeting_titles))[:100]:
        print('  -', title, flush=True)
    if not meeting_count:
        raise RuntimeError('No relevant committee meetings found; preserving existing calendar')
    lines.append('END:VCALENDAR')
    return '\r\n'.join(fold(line) for line in lines) + '\r\n', meeting_count, prep_count


def main():
    events = fetch_all()
    content, meetings, preps = generate(events)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(content.encode('utf-8'))
    (OUTPUT.parent / 'index.html').write_text('<!doctype html><title>AAC Committee Meetings</title><a href="aac-committees.ics">Calendar feed</a>', encoding='utf-8')
    print(f'PUBLISHED {meetings} meetings and {preps} preparation entries from {len(events)} source events.', flush=True)


if __name__ == '__main__':
    main()
