"""
Copyright (C) 2020-2020 Kristian Tashkov <kristian.tashkov@gmail.com>

This file is part of "Tools for Todoist".

"Tools for Todoist" is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the
Free Software Foundation, either version 3 of the License, or (at your
option) any later version.

"Tools for Todoist" is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
more details.

You should have received a copy of the GNU General Public License along
with this program. If not, see <http://www.gnu.org/licenses/>.
"""

import copy
import re

from dateutil.parser import parse
from dateutil.rrule import rrulestr
from dateutil.tz import gettz

from tools_for_todoist.models.rrule import rrule_to_string
from tools_for_todoist.utils import datetime_as, ensure_datetime, is_allday


class CalendarEvent:
    def __init__(self, google_calendar):
        self.google_calendar = google_calendar
        self.exceptions = {}
        self.recurring_event = None
        self._raw = None
        self._id = -1
        self.summary = None
        self._extended_properties = None

    def id(self):
        return self._id

    def raw(self):
        return self._raw

    def _get_recurrence(self):
        recurrence = self._raw.get('recurrence')
        if recurrence is None:
            return None
        if is_allday(self.start()):

            def fix_utc(recurrence_line):
                if 'RRULE' not in recurrence_line or 'UNTIL' not in recurrence_line:
                    return recurrence_line
                until_matcher = r'UNTIL=(\d{8}T\d{6}Z)'
                match = re.search(until_matcher, recurrence_line)
                if match:
                    new_end = (
                        parse(match[1])
                        .astimezone(gettz(self.google_calendar.default_timezone))
                        .date()
                    )
                    recurrence_line = re.sub(
                        until_matcher,
                        f"UNTIL={new_end.year}{new_end.month:02}{new_end.day:02}",
                        recurrence_line,
                    )
                return recurrence_line

            recurrence = [fix_utc(x) for x in recurrence]

        return '\n'.join(recurrence)

    def _get_rrule(self):
        rrule = self._get_recurrence()
        if rrule is None:
            return None

        start_date = ensure_datetime(self.start())
        return rrulestr(rrule, dtstart=start_date, unfold=True)

    @staticmethod
    def from_raw(google_calendar, raw):
        event = CalendarEvent(google_calendar)
        event._id = raw['id']
        event.update_from_raw(raw)
        return event

    def deep_copy(self):
        event = CalendarEvent.from_raw(self.google_calendar, self._raw)
        for event_instance in self.exceptions.values():
            event.update_exception(event_instance.raw())
        return event

    def update_from_raw(self, raw):
        self._raw = copy.deepcopy(raw)
        self._extended_properties = self._raw.get('extendedProperties')
        self.summary = self._raw.get('summary')

    def update_exception(self, exception):
        if exception['id'] not in self.exceptions:
            event = CalendarEvent.from_raw(self.google_calendar, exception)
            event.recurring_event = self
            self.exceptions[exception['id']] = event
        else:
            self.exceptions[exception['id']].update_from_raw(exception)

    def save_private_info(self, key, value):
        assert value is not None
        value = str(value)
        if self._extended_properties is None:
            self._extended_properties = {}
        else:
            self._extended_properties = copy.deepcopy(self._extended_properties)
        if 'private' not in self._extended_properties:
            self._extended_properties['private'] = {}
        self._extended_properties['private'][key] = value

    def get_private_info(self, key):
        if self._extended_properties is None:
            return None
        return self._extended_properties.get('private', {}).get(key)

    def _get_timezone(self, raw_start):
        raw_timezone = raw_start.get('timeZone', self.google_calendar.default_timezone)
        if raw_timezone == 'UTC':
            raw_timezone = 'Europe/London'
        return gettz(raw_timezone)

    def _parse_start(self, raw_start):
        if 'date' in raw_start:
            return parse(raw_start['date']).date()
        dt = parse(raw_start['dateTime'])
        return dt.astimezone(self._get_timezone(raw_start))

    def start(self):
        return self._parse_start(self._raw['start'])

    def end(self):
        return self._parse_start(self._raw['end'])

    def _get_original_start(self):
        return self._parse_start(self._raw['originalStartTime'])

    def _last_occurrence(self):
        instances = self._get_rrule()
        start = self.start()

        if instances.count() == 0:
            return None
        last_occurrence = instances[-1]
        if not is_allday(start):
            last_occurrence = last_occurrence.astimezone(start.tzinfo)
        return last_occurrence.date() if is_allday(start) else last_occurrence

    def _find_next_occurrence(self, rrule_instances, after_dt):
        non_cancelled_exception_starts = [
            (x.start(), x)
            for x in self.exceptions.values()
            if not (x._is_cancelled() or x.is_declined_by_me() or x.is_declined_by_others())
        ]
        future_exception_starts = (
            (start, event) for start, event in non_cancelled_exception_starts if start > after_dt
        )
        first_exception_start = min(
            future_exception_starts, default=(None, None), key=lambda x: x[0]
        )

        exception_original_starts = {x._get_original_start() for x in self.exceptions.values()}
        if self.is_declined_by_me() or self.is_declined_by_others():
            return first_exception_start

        for next_regular_occurrence in rrule_instances.xafter(ensure_datetime(after_dt)):
            if (
                first_exception_start[0] is not None
                and first_exception_start[0] <= next_regular_occurrence
            ):
                return first_exception_start
            if next_regular_occurrence not in exception_original_starts:
                return next_regular_occurrence, self
        return first_exception_start

    def next_occurrence(self, after_dt):
        start = self.start()
        after_dt = datetime_as(after_dt, start)
        instances = self._get_rrule()

        if instances is None:
            is_declined = self.is_declined_by_me() or self.is_declined_by_others()
            return (start, self) if after_dt < start and not is_declined else (None, None)

        next_occurrence, source_event = self._find_next_occurrence(instances, after_dt)
        if next_occurrence is not None:
            next_occurrence = (
                next_occurrence.date()
                if is_allday(start)
                else next_occurrence.astimezone(start.tzinfo)
            )
        return next_occurrence, source_event

    def recurrence_string(self):
        rrule = self._get_recurrence()
        if rrule is None:
            return None
        rrule = [x for x in rrule.split('\n') if 'RRULE' in x][0]

        start = self.start()
        if not is_allday(start):
            start_time = f'at {start.time().hour:02}:{start.time().minute:02} '
        else:
            start_time = ''

        formatted = rrule_to_string(rrule)
        match = re.search(r'until (.*Z|[\d]{8})', formatted)
        if match is not None:
            until_date = parse(match[1])
            if 'Z' in match[0]:
                until_date = until_date.astimezone(self._get_timezone(self._raw['start']))

            formatted = (
                f'{formatted[:match.span()[0]]}'
                f'{start_time}until {until_date.date().isoformat()}'
                f'{formatted[match.span()[1]:]}'
            )
            start_time = None
        match = re.search(r'for ([\d]*) times', formatted)
        if match:
            last_instance = self._last_occurrence()
            last_instance = f'{last_instance.year}-{last_instance.month:02}-{last_instance.day:02}'
            formatted = (
                f'{formatted[:match.span()[0]]}'
                f'{start_time}until {last_instance}'
                f'{formatted[match.span()[1]:]}'
            )
            start_time = None
        if start_time:
            formatted += f' {start_time.strip()}'
        return formatted

    def html_link(self):
        return self._raw['htmlLink']

    def save(self):
        updated_fields = {}
        if self.summary != self._raw.get('summary'):
            updated_fields['summary'] = self.summary
        if self._extended_properties != self._raw.get('extendedProperties'):
            updated_fields['extendedProperties'] = self._extended_properties
        if updated_fields:
            self.google_calendar.update_event(self._id, updated_fields)

    def _is_cancelled(self):
        return self._raw['status'] == 'cancelled'

    def is_declined_by_me(self):
        return self.response_status() == 'declined'

    def is_declined_by_others(self):
        other_attendees = [
            x for x in self.attendees() if not x.get('self', False) and not x.get('resource', False)
        ]
        all_declined = all([x['responseStatus'] == 'declined' for x in other_attendees])
        return len(other_attendees) > 0 and all_declined

    def response_status(self):
        self_attendee = next((x for x in self.attendees() if x.get('self', False)), None)
        return self_attendee['responseStatus'] if self_attendee is not None else None

    def todoist_duration(self):
        duration = self.duration()
        if is_allday(self.start()):
            return {'amount': int(duration / 60 / 24), 'unit': 'day'}
        return {'amount': int(duration), 'unit': 'minute'}

    def duration(self):
        return (self.end() - self.start()).total_seconds() / 60

    def attendees(self):
        return self._raw.get('attendees', [])

    def description(self):
        return self._raw.get('description', '')

    def conference_link(self):
        conference_data = self._raw.get('conferenceData')
        if conference_data is None:
            return None
        video_entrypoints = [
            entry['uri']
            for entry in conference_data['entryPoints']
            if entry.get('entryPointType') == 'video'
        ]
        return video_entrypoints[0] if video_entrypoints else None

    def __repr__(self):
        cancelled_tag = 'cancelled|' if self._is_cancelled() else ''
        return (
            f"{self._id}: {cancelled_tag}{self.summary}, {self.start()}, "
            f"{self._raw.get('recurrence')}"
        )
