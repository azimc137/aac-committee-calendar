#!/usr/bin/env python3
"""Build a public committee meeting ICS from Alamo Area Council's public calendar."""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = 'https://www.alamoareascouting.org/wp-content/plugins/councilware/calendar/render.php'
TZ = ZoneInfo('America/Chicago')
OUTPUT = Path('site/aac-committees.ics')
PREP_OUTPUT = Path('site/aac-preparation.ics')
WINDOW_DAYS = 180


def ical_escape(value):
    return (str(value or '').replace('\\', '\\\\').replace('\r\n', '\n')
            .replace('\r', '\n').replace('\n', '\\n')
            .replace(',', '\\,').replace(';', '\\;'))


def fold(line):
    """Fold iCalendar content lines at 75 octets, preserving UTF-8 chars."""
    parts = []
    current = ''
    limit = 75
    for char in line:
        encoded = len(char.encode('utf-8'))
        if len(current.encode('utf-8')) + encoded > limit:
            parts.append(current)
            current = ' ' + char
            limit = 75
        else:
            current += char
    parts.append(current)
    return '\r\n'.join(parts)


def matches(title):
    name = str(title or '').casefold()
    # Start narrow: these are obvious committee meetings, not every event.
    return ('committee' in name or bool(re.search(r'\bcwc\s+meeting\b', name)))


def managed_committee(title):
    """Only our selected committee meeting types generate prep appointments."""
    name = str(title or '').casefold()
    if 'fishing committee' in name:
        return 'Fishing'
    if 'climbing committee' in name or 'cope & climbing committee' in name or 'cope and climbing committee' in name:
        return 'Climbing'
    if 'cwc' in name and ('meeting' in name or 'committee' in name):
        return 'CWC'
    return None


def make_prep_calendar(events):
    """Generate distinct prep appointments; does not modify the council calendar."""
    now = datetime.now(TZ)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0',
             'PRODID:-//AAC Committee Preparation Calendar//EN',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
             'X-WR-CALNAME:AAC Committee Preparation',
             'X-WR-TIMEZONE:America/Chicago',
             'REFRESH-INTERVAL;VALUE=DURATION:PT24H']
    count = 0
    seen = set()
    stages = ((7, 'Prepare agenda', 'Review previous minutes and outstanding action items.'),
              (3, 'Confirm meeting details', 'Confirm agenda, meeting time/location, and attendance.'),
              (1, 'Final preparation', 'Review materials and prepare for tomorrow’s meeting.'))
    for event in events:
        title = event.get('title') or event.get('fullName') or ''
        committee = managed_committee(title)
        event_id = event.get('eventId')
        if not committee or event_id is None or not event.get('start') or str(event_id) in seen:
            continue
        seen.add(str(event_id))
        if str(event.get('isCancelled', 'false')).lower() == 'true':
            continue
        start = parse_local_date(event['start'])
        if start <= now:
            continue
        url = str(event.get('eventPageURL') or '')
        if not url.startswith('https://mycouncil.alamoareascouting.org/Event/'):
            url = f'https://mycouncil.alamoareascouting.org/Event/{event_id}'
        for days, label, details in stages:
            prep_day = start.date() - timedelta(days=days)
            # 9 AM on reminder day, displayed in San Antonio local time.
            prep_start = datetime.combine(prep_day, datetime.min.time().replace(hour=9), tzinfo=TZ)
            prep_end = prep_start + timedelta(minutes=15)
            if prep_end <= now:
                continue
            lines.extend([
                'BEGIN:VEVENT',
                f'UID:aac-prep-{event_id}-{days}d@alamoareascouting.org',
                f'DTSTAMP:{stamp}',
                f'DTSTART:{prep_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}',
                f'DTEND:{prep_end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}',
                f'SUMMARY:{ical_escape(f"{committee}: {label} — {title}")}',
                f'DESCRIPTION:{ical_escape(details + " Meeting: " + title + ". MyCouncil: " + url)}',
                f'URL:{url}',
                'STATUS:CONFIRMED',
                'TRANSP:TRANSPARENT',
                'BEGIN:VALARM',
                'ACTION:DISPLAY',
                'DESCRIPTION:Committee preparation',
                'TRIGGER:PT0S',
                'END:VALARM',
                'END:VEVENT',
            ])
            count += 1
    lines.append('END:VCALENDAR')
    return '\r\n'.join(fold(line) for line in lines) + '\r\n', count


def parse_local_date(value):
    # CouncilWare timestamps in observed response contain no time-zone offset.
    # Treat them as local wall-clock time in San Antonio.
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def fetch_events():
    now = datetime.now(TZ)
    start = (now - timedelta(days=7)).date().isoformat()
    end = (now + timedelta(days=WINDOW_DAYS)).date().isoformat()
    url = BASE + '?' + urllib.parse.urlencode({
        'calendarID': '27', 'start': start, 'end': end,
    })
    req = urllib.request.Request(url, headers={
        'User-Agent': 'AAC-CommitteeCalendar/1.0 (public calendar feed converter)',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode('utf-8-sig'))
    if not isinstance(data, list):
        raise ValueError('Expected an array of events from CouncilWare')
    if not data:
        raise ValueError('CouncilWare returned zero events. Refusing to publish an empty calendar.')
    return data


def make_calendar(events):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0',
             'PRODID:-//AAC Public Committee Calendar Converter//EN',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
             'X-WR-CALNAME:AAC Committee Meetings',
             'X-WR-TIMEZONE:America/Chicago',
             'REFRESH-INTERVAL;VALUE=DURATION:PT24H']
    matched = 0
    seen = set()
    for event in events:
        title = event.get('title') or event.get('fullName') or ''
        if not matches(title):
            continue
        event_id = event.get('eventId')
        if event_id is None or not event.get('start') or not event.get('end'):
            print('Skipping malformed meeting:', repr(title), file=sys.stderr)
            continue
        if str(event_id) in seen:
            continue
        seen.add(str(event_id))
        start = parse_local_date(event['start']).astimezone(timezone.utc)
        end = parse_local_date(event['end']).astimezone(timezone.utc)
        if end <= start:
            print('Skipping meeting with invalid duration:', repr(title), file=sys.stderr)
            continue
        is_cancelled = str(event.get('isCancelled', 'false')).lower() == 'true'
        url = str(event.get('eventPageURL') or '')
        if not url.startswith('https://mycouncil.alamoareascouting.org/Event/'):
            url = f'https://mycouncil.alamoareascouting.org/Event/{event_id}'
        lines.extend([
            'BEGIN:VEVENT',
            f'UID:aac-{event_id}@alamoareascouting.org',
            f'DTSTAMP:{stamp}',
            f'DTSTART:{start.strftime("%Y%m%dT%H%M%SZ")}',
            f'DTEND:{end.strftime("%Y%m%dT%H%M%SZ")}',
            f'SUMMARY:{ical_escape(title)}',
            f'LOCATION:{ical_escape(event.get("location") or "")}',
            f'DESCRIPTION:{ical_escape("Public MyCouncil meeting listing: " + url)}',
            f'URL:{url}',
            f'STATUS:{"CANCELLED" if is_cancelled else "CONFIRMED"}',
            'TRANSP:TRANSPARENT',
            'END:VEVENT',
        ])
        matched += 1
    if matched == 0:
        raise ValueError('No committee meetings matched. Refusing to replace existing calendar.')
    lines.append('END:VCALENDAR')
    return '\r\n'.join(fold(line) for line in lines) + '\r\n', matched


def main():
    events = fetch_events()
    content, matched = make_calendar(events)
    prep_content, prep_count = make_prep_calendar(events)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(content.encode('utf-8'))
    PREP_OUTPUT.write_bytes(prep_content.encode('utf-8'))
    (OUTPUT.parent / 'index.html').write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<title>AAC Committee Calendar</title><body>'
        '<h1>AAC Committee Meetings</h1>'
        '<p><a href="aac-committees.ics">Committee calendar</a></p>'
        '<p><a href="aac-preparation.ics">Committee preparation calendar</a></p>'
        '<p>Unofficial public calendar conversion. Confirm details on MyCouncil.</p>'
        '</body></html>\n', encoding='utf-8')
    print(f'Converted {matched} committee meetings and {prep_count} preparation entries from {len(events)} events.')


if __name__ == '__main__':
    main()
