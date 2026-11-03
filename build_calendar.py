#!/usr/bin/env python3
"""CouncilWare committee meetings + preparation checkpoints in ONE ICS calendar.

No external Python packages required. The GitHub Actions workflow already in your
repository can run this unchanged. Only public calendar data is published.
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone, time
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = 'https://www.alamoareascouting.org/wp-content/plugins/councilware/calendar/render.php'
TIMEZONE = ZoneInfo('America/Chicago')
CALENDAR_ID = '27'
LOOKAHEAD_DAYS = 180
OUTPUT = Path('site/aac-committees.ics')  # same URL as your existing Outlook subscription!

# Only these 8 committee areas are included; no general camps or big events.
# Add additional distinctive aliases to the relevant list if CouncilWare uses
# another committee name. These are matched to public event TITLES.
COMMITTEES = {
    'Aquatics Committee': ('aquatics', 'aquatic'),
    'Climbing Committee': ('climbing',),
    'COPE Committee': ('cope', 'challenging outdoor personal experience'),
    'Fall Events Committee': ('fall events', 'fall event'),
    'Fishing Committee': ('fishing', 'angling'),
    'International Committee': ('international',),
    'Radio Committee': ('radio', 'jota', 'joti', 'jamboree on the air'),
    'RATA / Shooting Sports Committee': ('rata', 'shooting sports', 'shooting sport', 'range and target'),
}

# Titles must indicate a meeting, committee, or actual planning session.
# This avoids including Fishing Adventures, Shooting Sports activities, etc.
MEETING_INDICATORS = (
    'committee', 'meeting', 'meetings', 'planning session',
    'planning call', 'workgroup', 'working group',
    'staff call', 'staff meeting', 'roundtable',
)
PREP_STAGES = (
    (7, 'Prepare agenda', 'Review previous minutes and unresolved action items.'),
    (3, 'Confirm details', 'Confirm agenda, meeting time, location, and participants.'),
    (1, 'Final preparation', 'Review documents and notes for tomorrow’s meeting.'),
)


def normalize(s):
    return re.sub(r'\s+', ' ', str(s or '').casefold().replace('-', ' ')).strip()


def has_phrase(text, phrase):
    # Word boundaries prevent "radio" matching words like "radiology".
    return bool(re.search(r'(?<!\w)' + re.escape(normalize(phrase)) + r'(?!\w)', text))


def classify(title):
    text = normalize(title)
    if not any(has_phrase(text, word) for word in MEETING_INDICATORS):
        return None
    for label, aliases in COMMITTEES.items():
        if any(has_phrase(text, alias) for alias in aliases):
            return label
    return None


def ics_escape(text):
    return (str(text or '').replace('\\', '\\\\')
            .replace('\r\n', '\n').replace('\r', '\n')
            .replace('\n', '\\n').replace(',', '\\,').replace(';', '\\;'))


def fold(line):
    """RFC 5545 content lines, folded at <=75 UTF-8 octets."""
    out, current = [], ''
    for ch in line:
        if len((current + ch).encode('utf-8')) > 75:
            out.append(current)
            current = ' ' + ch
        else:
            current += ch
    out.append(current)
    return '\r\n'.join(out)


def local_datetime(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt.replace(tzinfo=TIMEZONE) if dt.tzinfo is None else dt.astimezone(TIMEZONE)


def utcstamp(dt):
    return dt.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def fetch_events():
    now = datetime.now(TIMEZONE)
    # Start slightly in the past so prep events are included for near-term meetings.
    parameters = urllib.parse.urlencode({
        'calendarID': CALENDAR_ID,
        'start': (now - timedelta(days=1)).date().isoformat(),
        'end': (now + timedelta(days=LOOKAHEAD_DAYS)).date().isoformat(),
    })
    request = urllib.request.Request(BASE + '?' + parameters, headers={
        'User-Agent': 'AAC-PublicCommitteeCalendar/1.0',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(request, timeout=35) as response:
        events = json.loads(response.read().decode('utf-8-sig'))
    if not isinstance(events, list) or not events:
        raise RuntimeError('Calendar returned no event records; refusing to replace the published feed.')
    return events


def event_lines(uid, summary, start, end, stamp, url, details, location='', cancelled=False):
    return [
        'BEGIN:VEVENT', f'UID:{uid}', f'DTSTAMP:{stamp}',
        f'DTSTART:{utcstamp(start)}', f'DTEND:{utcstamp(end)}',
        f'SUMMARY:{ics_escape(summary)}',
        f'LOCATION:{ics_escape(location)}',
        f'DESCRIPTION:{ics_escape(details)}',
        f'URL:{url}',
        'STATUS:CANCELLED' if cancelled else 'STATUS:CONFIRMED',
        'TRANSP:TRANSPARENT',
        'END:VEVENT',
    ]


def generate(events, now=None):
    now = now or datetime.now(TIMEZONE)
    stamp = utcstamp(datetime.now(timezone.utc))
    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0',
        'PRODID:-//AAC Public Committee Meetings and Prep//EN',
        'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
        'X-WR-CALNAME:AAC Committee Meetings',
        'X-WR-TIMEZONE:America/Chicago',
        'REFRESH-INTERVAL;VALUE=DURATION:PT24H',
    ]
    count = prep_count = 0
    seen = set()
    for record in events:
        title = str(record.get('title') or record.get('fullName') or '')
        committee = classify(title)
        event_id = record.get('eventId')
        if not committee or event_id is None or not record.get('start') or not record.get('end'):
            continue
        if str(event_id) in seen:
            continue
        seen.add(str(event_id))
        try:
            start = local_datetime(record['start'])
            end = local_datetime(record['end'])
        except ValueError:
            print('Skipping invalid date:', title, file=sys.stderr)
            continue
        if end <= start or end < now:
            continue
        cancelled = str(record.get('isCancelled', 'false')).casefold() == 'true'
        url = f'https://mycouncil.alamoareascouting.org/Event/{event_id}'
        lines.extend(event_lines(
            f'aac-{event_id}@alamoareascouting.org', title, start, end, stamp,
            url, f'{committee}. Public MyCouncil event: {url}',
            location=record.get('location') or '', cancelled=cancelled,
        ))
        count += 1
        if cancelled:
            continue  # don't send prep reminders for canceled meetings
        for days, action, note in PREP_STAGES:
            when = start.date() - timedelta(days=days)
            prep_start = datetime.combine(when, time(9, 0), tzinfo=TIMEZONE)
            prep_end = prep_start + timedelta(minutes=15)
            if prep_end <= now:
                continue
            lines.extend(event_lines(
                f'aac-prep-{event_id}-{days}d@alamoareascouting.org',
                f'[Prep] {action} — {title}', prep_start, prep_end,
                stamp, url, f'{committee}: {note} Meeting: {title}. {url}',
            ))
            prep_count += 1
    if count == 0:
        raise RuntimeError('No committee meetings matched; preserving the previous published calendar.')
    lines.append('END:VCALENDAR')
    content = '\r\n'.join(fold(line) for line in lines) + '\r\n'
    return content, count, prep_count


def main():
    events = fetch_events()
    ics, meetings, preparations = generate(events)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(ics.encode('utf-8'))
    # Keep the legacy URL usable but empty to avoid duplicate old reminders.
    # Users should unsubscribe from the old AAC Committee Preparation calendar.
    legacy = Path('site/aac-preparation.ics')
    legacy.write_text('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//AAC Legacy Preparation Calendar//EN\r\nEND:VCALENDAR\r\n', encoding='utf-8')
    (OUTPUT.parent / 'index.html').write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<title>AAC Committee Calendar</title><body><h1>AAC Committee Calendar</h1>'
        '<p><a href="aac-committees.ics">Committee meetings and preparation dates</a></p>'
        '<p>Unofficial public calendar conversion; verify changes on MyCouncil.</p>'
        '</body></html>\n', encoding='utf-8')
    print(f'Published {meetings} committee meetings and {preparations} preparation dates from {len(events)} CouncilWare events.')


if __name__ == '__main__':
    main()
