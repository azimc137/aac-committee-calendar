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
    'Fall Events': ('fall events', 'fall event', 'fall events committee'),
    'Fishing': ('fishing', 'angling'),
    'International': ('international','International Scouting Committee Meeting'),
    'Radio': ('radio', 'jota', 'joti', 'jamboree on the air'),
    'RATA / Shooting Sports': ('rata', 'shooting sports', 'shooting sport', 'range and target', 'RATA committee meeting'),
    'Cub Scout Day Camp': ('Cub Scout Day Camp Planning Committee Meeting', 'CSDC', 'Cub Scout Day Camp'
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


def with_derived_fall_events(events):
    """Create 8–9 PM Fall Events meeting on each published 7–8 PM RATA meeting day.

    Do not fabricate a location; do not duplicate an independently published Fall Events
    meeting on the same date. Only derive dates from active, future RATA meetings.
    """
    now = datetime.now(TZ)
    existing_fall_dates = set()
    for rec in events:
        if classify(rec.get('title') or rec.get('fullName')) == 'Fall Events' and rec.get('start'):
            try:
                existing_fall_dates.add(localtime(rec['start']).date())
            except (ValueError, TypeError):
                pass
    derived = []
    seen_dates = set()
    for rec in events:
        title = rec.get('title') or rec.get('fullName') or ''
        if classify(title) != 'RATA / Shooting Sports':
            continue
        if str(rec.get('isCancelled', False)).lower() == 'true':
            continue
        try:
            start, end = localtime(rec['start']), localtime(rec['end'])
        except (ValueError, TypeError, KeyError):
            continue
        if end <= now:
            continue
        # Only derive from the actual 7–8 PM RATA committee meeting, not a
        # training session or a different RATA event.
        if start.hour != 19 or start.minute != 0 or end.hour != 20 or end.minute != 0:
            print(f'NOT DERIVING Fall Events (RATA time differs from 7–8 PM): {title} at {start}', flush=True)
            continue
        day = start.date()
        if day in existing_fall_dates or day in seen_dates:
            print(f'NOT DERIVING Fall Events (already published or derived): {day}', flush=True)
            continue
        seen_dates.add(day)
        source_id = str(rec['eventId'])
        derived.append({
            'eventId': f'fall-events-from-rata-{source_id}',
            'title': 'Fall Events Committee Meeting (schedule inferred from RATA)',
            'fullName': 'Fall Events Committee Meeting (schedule inferred from RATA)',
            'start': (start + timedelta(hours=1)).isoformat(),
            'end': (end + timedelta(hours=1)).isoformat(),
            'location': '',
            'eventPageURL': rec.get('eventPageURL') or f'https://mycouncil.alamoareascouting.org/Event/{source_id}',
            'isCancelled': 'false',
            '_derived': True,
        })
        print(f'DERIVED: {day} | Fall Events | 8:00–9:00 PM from RATA event {source_id}', flush=True)
        print(f'::warning title=Fall Events listing missing::{day}: RATA is published but Fall Events has no matching official CouncilWare record. Check event category/tagging in MyCouncil.', flush=True)
    return events + derived


def generate(events):
    now = datetime.now(TZ)
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//AAC Committee Calendar//EN',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:AAC Committee Meetings',
             'X-WR-TIMEZONE:America/Chicago']
    meeting_count = prep_count = 0
    committee_counts = {}
    other_meeting_titles = []
    for rec in sorted(with_derived_fall_events(events), key=lambda x: x.get('start', '')):
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
        description = (f'{label} meeting time inferred from a published 7–8 PM RATA meeting. '
                       f'Please confirm this 8–9 PM Fall Events meeting and its location. RATA source: {url}'
                       if rec.get('_derived') else f'{label} committee meeting. Source: {url}')
        add_event(lines, f'aac-{event_id}@alamoareascouting.org', title, start, end,
                  rec.get('location') or '', url, description, cancelled)
        meeting_count += 1
        committee_counts[label] = committee_counts.get(label, 0) + 1
        print(f'INCLUDED: {start:%Y-%m-%d} | {label} | {title}', flush=True)
        if cancelled:
            continue
        # An inferred Fall Events meeting means MyCouncil did not return an
        # official Fall Events record for this RATA meeting date.
        # Display a one-time review appointment seven days beforehand.
        # When the official record appears, this warning is no longer generated.
        if rec.get('_derived'):
            review_day = start.date() - timedelta(days=7)
            review_start = datetime.combine(review_day, time(9, 30), tzinfo=TZ)
            review_end = review_start + timedelta(minutes=15)
            if review_end > now:
                add_event(
                    lines,
                    f'aac-check-fall-events-{start:%Y%m%d}@alamoareascouting.org',
                    '[Check MyCouncil] Fall Events meeting not listed',
                    review_start, review_end, '', url,
                    f'RATA is listed for {start:%B %d, %Y} at 7:00 PM, but no official '
                    'Fall Events Committee listing was returned by CouncilWare. '
                    'Check the Fall Events event category/tagging and confirm its '
                    '8:00-9:00 PM schedule. This is a review task, not proof the '
                    'meeting was cancelled.'
                )
                print(f'WARNING ENTRY: {review_day} 9:30 AM | Check official Fall Events listing for {start.date()}', flush=True)
        for days, action in PREP:
            day = start.date() - timedelta(days=days)
            prep_start = datetime.combine(day, time(9, 0), tzinfo=TZ)
            prep_end = prep_start + timedelta(minutes=15)
            if prep_end <= now:
                continue
            prep_note = 'Time inferred from RATA; verify details. ' if rec.get('_derived') else ''
            add_event(lines, f'aac-prep-{event_id}-{days}d@alamoareascouting.org',
                      f'[Prep] {action} — {title}', prep_start, prep_end, '', url,
                      f'{label}: {action} for {title}. {prep_note}{url}')
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
