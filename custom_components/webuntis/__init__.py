"""The Web Untis integration."""
from __future__ import annotations

import logging
from asyncio.log import logger
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any
import json

# pylint: disable=import-self
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.event import async_track_time_interval

from homeassistant.components.calendar import CalendarEvent
import homeassistant.util.dt as dt_util

import webuntis

from .const import (
    DOMAIN,
    SCAN_INTERVAL,
    SIGNAL_NAME_PREFIX,
    DAYS_TO_FUTURE,
)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.CALENDAR]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WebUntis from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})

    # Create and store server instance.
    assert entry.unique_id
    unique_id = entry.unique_id
    _LOGGER.debug(
        "Creating server instance for '%s' (%s)",
        entry.data["username"],
        entry.data["school"],
    )

    server = WebUntis(hass, unique_id, entry.data)
    domain_data[unique_id] = server
    await server.async_update()
    server.start_periodic_update()

    # Set up platforms.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:

        new = {**config_entry.data}

        new["calendar_long_name"] = True

        config_entry.version = 2
        hass.config_entries.async_update_entry(config_entry, data=new)

    _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unique_id = config_entry.unique_id
    server = hass.data[DOMAIN][unique_id]

    # Unload platforms.
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    # Clean up.
    server.stop_periodic_update()
    hass.data[DOMAIN].pop(unique_id)

    return unload_ok


class WebUntis:
    """Representation of a WebUntis client."""

    def __init__(
        self, hass: HomeAssistant, unique_id: str, config_data: Mapping[str, Any]
    ) -> None:
        """Initialize client instance."""
        self._hass = hass

        # Server data
        self.unique_id = unique_id
        self.server = config_data["server"]
        self.school = config_data["school"]
        self.username = config_data["username"]
        self.password = config_data["password"]
        self.timetable_source = config_data["timetable_source"]
        self.timetable_source_id = config_data["timetable_source_id"]

        self.calendar_long_name = config_data["calendar_long_name"]

        # pylint: disable=maybe-no-member
        self.session = webuntis.Session(
            username=self.username,
            password=self.password,
            server=self.server,
            useragent="foo",
            school=self.school,
        )

        self._last_status_request_failed = False

        # Data provided by 3rd party library
        self.is_class = None
        self.next_class = None
        self.next_class_json = None
        self.next_lesson_to_wake_up = None
        self.calendar_events = None
        self.next_day_json = None

        # Dispatcher signal name
        self.signal_name = f"{SIGNAL_NAME_PREFIX}_{self.unique_id}"

        # Callback for stopping periodic update.
        self._stop_periodic_update: CALLBACK_TYPE | None = None

    def start_periodic_update(self) -> None:
        """Start periodic execution of update method."""
        self._stop_periodic_update = async_track_time_interval(
            self._hass, self.async_update, timedelta(seconds=SCAN_INTERVAL)
        )

    def stop_periodic_update(self) -> None:
        """Stop periodic execution of update method."""
        if self._stop_periodic_update:
            self._stop_periodic_update()

    # pylint: disable=unused-argument
    async def async_update(self, now: datetime | None = None) -> None:
        """Get server data from 3rd party library and update properties."""

        await self._async_status_request()

        # Notify sensors about new data.
        async_dispatcher_send(self._hass, self.signal_name)

    async def _async_status_request(self) -> None:
        """Request status and update properties."""
        self.is_class = False

        try:
            await self._hass.async_add_executor_job(self.session.login)
        except OSError as error:
            # Login error, set all properties to unknown.
            self.is_class = None
            self.next_class = None
            self.next_lesson_to_wake_up = None

            # pylint: disable=maybe-no-member
            self.session = webuntis.Session(
                username=self.username,
                password=self.password,
                server=self.server,
                useragent="foo",
                school=self.school,
            )

            # Inform user once about failed update if necessary.
            if not self._last_status_request_failed:
                _LOGGER.warning(
                    "Login to WebUntis '%s@%s' failed - OSError: %s",
                    self.school,
                    self.username,
                    error,
                )
            self._last_status_request_failed = True
            return

        try:
            self.is_class = await self._hass.async_add_executor_job(self._is_class)
        except OSError as error:
            self.is_class = None

            _LOGGER.warning(
                "Updating the propertie is_class of '%s@%s' failed - OSError: %s",
                self.school,
                self.username,
                error,
            )

        try:
            self.next_class = await self._hass.async_add_executor_job(self._next_class)
        except OSError as error:
            self.next_class = None

            _LOGGER.warning(
                "Updating the propertie next_class of '%s@%s' failed - OSError: %s",
                self.school,
                self.username,
                error,
            )

        try:
            self.next_lesson_to_wake_up = await self._hass.async_add_executor_job(
                self._next_lesson_to_wake_up
            )
        except OSError as error:
            self.next_lesson_to_wake_up = None

            _LOGGER.warning(
                "Updating the propertie next_lesson_to_wake_up of '%s@%s' failed - OSError: %s",
                self.school,
                self.username,
                error,
            )

        try:
            self.next_day_json = await self._hass.async_add_executor_job(
                self._next_day_json
            )
        except OSError as error:
            self.next_day_json = None

            _LOGGER.warning(
                "Updating the propertie next_day_json of '%s@%s' failed - OSError: %s",
                self.school,
                self.username,
                error,
            )

        try:
            self.calendar_events = await self._hass.async_add_executor_job(
                self._get_events
            )
        except OSError as error:
            self.calendar_events = None

            _LOGGER.warning(
                "Updating the propertie calendar_events of '%s@%s' failed - OSError: %s",
                self.school,
                self.username,
                error,
            )

        await self._hass.async_add_executor_job(self.session.logout)

    def get_timetable_object(self):
        """return the object to request the timetable"""
        if self.timetable_source == "student":
            source = self.session.get_student(
                self.timetable_source_id[1], self.timetable_source_id[0]
            )
        elif self.timetable_source == "klasse":
            klassen = self.session.klassen()
            # pylint: disable=maybe-no-member
            source = klassen.filter(name=self.timetable_source_id)[0]
        elif self.timetable_source == "teacher":
            source = self.session.get_teacher(
                self.timetable_source_id[1], self.timetable_source_id[0]
            )
        elif self.timetable_source == "subject":
            pass
        elif self.timetable_source == "room":
            pass

        return {self.timetable_source: source}

    def _is_class(self):
        """return if is class"""
        today = date.today()
        timetable_object = self.get_timetable_object()

        table = self.session.timetable(start=today, end=today, **timetable_object)

        now = datetime.now()

        for lesson in table:
            # pylint: disable=maybe-no-member
            if lesson.start < now < lesson.end and self.check_lesson(lesson):
                return True
        return False

    def _next_class(self):
        """returns time of next class."""
        today = date.today()
        in_x_days = today + timedelta(days=DAYS_TO_FUTURE)
        timetable_object = self.get_timetable_object()

        # pylint: disable=maybe-no-member
        table = self.session.timetable(start=today, end=in_x_days, **timetable_object)

        now = datetime.now()

        lesson_list = []
        for lesson in table:
            if lesson.start > now and self.check_lesson(lesson):
                lesson_list.append(lesson)

        lesson_list.sort(key=lambda e: (e.start))

        try:
            lesson = lesson_list[0]
        except IndexError:
            _LOGGER.warning(
                "Updating the propertie _next_class of '%s@%s' failed - No lesson in the next %s days",
                self.school,
                self.username,
                DAYS_TO_FUTURE,
            )
            return None

        self.next_class_json = self.get_lesson_json(lesson)

        return lesson.start.astimezone()

    """def _first_class(self):
        ""returns time of first class.""
        today = date.today()
        timetable_object = self.get_timetable_object()

        # pylint: disable=maybe-no-member
        table = self.session.timetable(start=today, end=today, **timetable_object)

        time_list = []
        for lesson in table:
            if self.check_lesson(lesson):
                time_list.append(lesson.start)

        if len(time_list) > 1:
            return sorted(time_list)[0].astimezone()
        else:
            return None"""

    def _next_lesson_to_wake_up(self):
        """returns time of the next lesson to weak up."""
        today = date.today()
        now = datetime.now()
        in_x_days = today + timedelta(days=DAYS_TO_FUTURE)
        timetable_object = self.get_timetable_object()

        # pylint: disable=maybe-no-member
        table = self.session.timetable(start=today, end=in_x_days, **timetable_object)

        time_list = []
        for lesson in table:
            if self.check_lesson(lesson):
                time_list.append(lesson.start)

        day = now
        time_list_new = []
        for time in sorted(time_list):
            if time < day:
                day = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
                continue
            else:
                time_list_new.append(time)

        try:
            return sorted(time_list_new)[0].astimezone()
        except IndexError:
            _LOGGER.warning(
                "Updating the propertie _next_lesson_to_wake_up of '%s@%s' failed - No lesson in the next %s days",
                self.school,
                self.username,
                DAYS_TO_FUTURE,
            )
            return None

    def _next_day_json(self):
        if self.next_lesson_to_wake_up is None:
            return None
        day = self.next_lesson_to_wake_up
        timetable_object = self.get_timetable_object()

        table = self.session.timetable(start=day, end=day, **timetable_object)

        json_str = "["
        for lesson in table:
            json_str += str(self.get_lesson_json(lesson)) + ","
        json_str = json_str[:-1] + "]"

        return json_str

    def _get_events(self):
        today = date.today()
        in_x_days = today + timedelta(days=DAYS_TO_FUTURE)
        timetable_object = self.get_timetable_object()

        table = self.session.timetable(start=today, end=in_x_days, **timetable_object)

        event_list = []

        for lesson in table:
            if self.check_lesson(lesson):
                try:
                    event_list.append(
                        CalendarEvent(
                            start=lesson.start.astimezone(),
                            end=lesson.end.astimezone(),
                            summary=lesson.subjects[0].long_name
                            if self.calendar_long_name
                            else lesson.subjects[0].name,
                            location=lesson.rooms[0].long_name,  # add Room as location
                            description=self.get_lesson_json(lesson),
                        )
                    )
                except OSError as error:
                    _LOGGER.warning(
                        "Updating of a calendar_event of '%s@%s' failed - OSError: %s",
                        self.school,
                        self.username,
                        error,
                    )
        return event_list

    def check_lesson(self, lesson) -> bool:
        """Checks if a lesson is taking place"""
        return lesson.code != "cancelled" and lesson.subjects

    # pylint: disable=bare-except
    def get_lesson_json(self, lesson) -> str:
        """returns info about lesson in json"""
        dic = {}
        dic["start"] = str(lesson.start.astimezone())
        dic["end"] = str(lesson.end.astimezone())
        try:
            dic["id"] = int(lesson.id)
        except:
            pass
        try:
            dic["code"] = str(lesson.code)
        except:
            pass
        try:
            dic["type"] = str(lesson.type)
        except:
            pass
        try:
            dic["subjects"] = [
                {"name": str(subject.name), "long_name": str(subject.long_name)}
                for subject in lesson.subjects
            ]
        except:
            pass
        try:
            dic["rooms"] = [
                {"name": str(room.name), "long_name": str(room.long_name)}
                for room in lesson.rooms
            ]
        except:
            pass
        try:
            dic["klassen"] = [
                {"name": str(klasse.name), "long_name": str(klasse.long_name)}
                for klasse in lesson.klassen
            ]
        except:
            pass
        try:
            dic["original_rooms"] = [
                {"name": str(room.name), "long_name": str(room.long_name)}
                for room in lesson.original_rooms
            ]
        except:
            pass
        try:
            dic["teachers"] = [
                {"name": str(teacher.name), "long_name": str(teacher.long_name)}
                for teacher in lesson.teachers
            ]
        except:
            pass
        try:
            dic["original_teachers"] = [
                {"name": str(teacher.name), "long_name": str(teacher.long_name)}
                for teacher in lesson.original_teachers
            ]
        except:
            pass

        return str(json.dumps(dic))


class WebUntisEntity(Entity):
    """Representation of a Web Untis base entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        server: WebUntis,
        type_name: str,
        icon: str,
        device_class: str | None,
    ) -> None:
        """Initialize base entity."""
        self._server = server
        self._attr_name = type_name
        self._attr_icon = icon
        self._attr_unique_id = f"{self._server.unique_id}-{type_name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._server.unique_id)},
            manufacturer="Web Untis",
            model=f"{self._server.username}@{self._server.school}",
            name=self._server.username,
        )
        self._attr_device_class = device_class
        self._extra_state_attributes = None
        self._disconnect_dispatcher: CALLBACK_TYPE | None = None

    async def async_update(self) -> None:
        """Fetch data from the server."""
        raise NotImplementedError()

    async def async_added_to_hass(self) -> None:
        """Connect dispatcher to signal from server."""
        self._disconnect_dispatcher = async_dispatcher_connect(
            self.hass, self._server.signal_name, self._update_callback
        )

    async def async_will_remove_from_hass(self) -> None:
        """Disconnect dispatcher before removal."""
        if self._disconnect_dispatcher:
            self._disconnect_dispatcher()

    @callback
    def _update_callback(self) -> None:
        """Triggers update of properties after receiving signal from server."""
        self.async_schedule_update_ha_state(force_refresh=True)
