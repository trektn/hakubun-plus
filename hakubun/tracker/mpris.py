# This file is part of Hakubun.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
from __future__ import annotations

from enum import Enum
import os
import re
import sys
import threading
import time
import urllib.parse
import asyncio
from typing import Any
from dataclasses import dataclass
from collections import deque

from jeepney import HeaderFields, Properties, DBusAddress
from jeepney.io.asyncio import open_dbus_router, DBusRouter, Proxy
from jeepney.bus_messages import message_bus, MatchRule

from hakubun import utils
from hakubun.tracker import tracker

__all__ = [
    'MprisTracker',
]


BUS_NAMESPACE = 'org.mpris.MediaPlayer2'
PATH_MPRIS = '/org/mpris/MediaPlayer2'
IFACE_PROPERTIES = 'org.freedesktop.DBus.Properties'
IFACE_MPRIS = 'org.mpris.MediaPlayer2'
IFACE_MPRIS_PLAYER = IFACE_MPRIS + '.Player'


class PlaybackStatus(str, Enum):
    PLAYING = 'Playing'
    PAUSED = 'Paused'
    STOPPED = 'Stopped'


def safe_get_dbus_value(pair, type_):
    """Assert the value's type before returning it None-safely."""
    if pair is None:
        return None
    if pair[0] != type_:
        msg = f"Expected type {type_!r} but found {pair[0]!r}; value: {pair[1]!r}"
        raise TypeError(msg)
    return pair[1]


async def collect_names(router) -> dict[str, str]:
    """Find all active buses in the known namespace mapped by their unique name."""
    message_proxy = Proxy(message_bus, router)
    resp = await message_proxy.ListNames()
    names = [name for name in resp[0] if name.startswith(BUS_NAMESPACE)]
    unique_names = await asyncio.gather(*(message_proxy.GetNameOwner(name) for name in names))
    name_map = {un[0]: n for un, n in zip(unique_names, names)}
    return name_map


@dataclass
class Player:
    config: dict[str, Any]
    router: DBusRouter
    wellknown_name: str
    unique_name: str
    playback_status: str | None = None
    title: str | None = None
    url: str | None = None
    length: int | None = None  # Milliseconds; from mpris:length metadata.

    @classmethod
    async def new(cls, config, router, wellknown_name, unique_name):
        player = Player(config, router, wellknown_name, unique_name)
        await asyncio.gather(
            player.update_filename(),
            player.update_playback_status(),
        )
        return player

    @property
    def filename(self):
        if self.url:
            unquoted_url = urllib.parse.unquote_plus(self.url)
            # We only trust URLs using the file protocol to be full paths.
            if self.config['library_full_path'] and unquoted_url.startswith('file://'):
                return unquoted_url.removeprefix('file://')
            return os.path.basename(unquoted_url)
        else:
            return self.title

    async def update_filename(self):
        msg = self._player_properties.get('Metadata')
        reply = await self.router.send_and_get_reply(msg)
        metadata = safe_get_dbus_value(reply.body[0], 'a{sv}')
        if metadata:
            self.title = safe_get_dbus_value(metadata.get('xesam:title'), 's')
            self.url = safe_get_dbus_value(metadata.get('xesam:url'), 's')
            length_us = safe_get_dbus_value(metadata.get('mpris:length'), 'x')
            self.length = int(length_us) // 1000 if length_us else None
        else:
            self.title = None
            self.url = None
            self.length = None

    async def update_playback_status(self):
        msg = self._player_properties.get('PlaybackStatus')
        reply = await self.router.send_and_get_reply(msg)
        self.playback_status = safe_get_dbus_value(reply.body[0], 's')

    async def get_position(self):
        msg = self._player_properties.get('Position')
        reply = await self.router.send_and_get_reply(msg)
        return safe_get_dbus_value(reply.body[0], 'x')

    @property
    def _player_properties(self):
        address = DBusAddress(PATH_MPRIS, bus_name=self.unique_name, interface=IFACE_MPRIS_PLAYER)
        return Properties(address)


class MprisTracker(tracker.TrackerBase):

    name = 'Tracker (MPRIS)'

    def __init__(self, *args, **kwargs):
        # `TrackerBase.__init__` spawns a new thread for `observe`
        # where we wait for this event.
        self.initialized = threading.Event()
        super().__init__(*args, **kwargs)

        self.re_players = re.compile(self.config['tracker_process'])
        # Map "OwnerName"s to Player instances
        # for monitoring when one appears or disappears
        # via the `NameOwnerChanged` signal.
        self.players = {}
        self.timing = False
        self.active_player = None
        self.initialized.set()

    def update_list(self, *args, **kwargs):
        super().update_list(*args, **kwargs)
        if self.last_state != utils.Tracker.PLAYING:
            # Re-check if we have any player with a valid show running after a list update
            self.last_filename = None
            self.find_playing_player()

    def observe(self, config, watch_dirs):
        self.msg.info("Using MPRIS.")
        self.initialized.wait()

        # Permit 5 starts within 60 seconds,
        # waiting for 5s between each retry.
        # If it fails more frequently,
        # we consider the tracker to be broken and let it die.
        start_times = deque(maxlen=5)
        while len(start_times) < 5 or start_times[0] + 60 < time.time():
            start_times.append(time.time())
            asyncio.run(self.observe_async())
            time.sleep(5)

        self.msg.warn("Reached restart limit for MPRIS tracker.")

    async def observe_async(self):
        async with open_dbus_router() as router:
            name_owner_watcher_task = asyncio.create_task(self.name_owner_watcher(router))
            properties_watcher_task = asyncio.create_task(self.properties_watcher(router))
            timer_task = asyncio.create_task(self._timer())
            tasks = [name_owner_watcher_task, properties_watcher_task, timer_task]

            name_map = await collect_names(router)
            self.players = {
                unique_name: await Player.new(self.config, router, wellknown_name, unique_name)
                for unique_name, wellknown_name in name_map.items()
                if self.valid_player(wellknown_name)
            }
            ignored_players = [name for name in name_map.values() if not self.valid_player(name)]
            self.msg.debug(f"Ignoring players: {ignored_players}")
            for player in self.players.values():
                self.msg.debug(f"Player connected: {player.wellknown_name}")
            self.find_playing_player()

            try:
                await asyncio.gather(*tasks)
            except Exception:
                self.msg.exception("Error in dbus watchers; cleaning up", sys.exc_info())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks)

    async def name_owner_watcher(self, router):
        # Select name change signals for the well-known mpris service name.
        # We only consider players with such a well-known name
        # and thus treat any changes to the owned name
        # as a creation and a disappearance of a service.
        # Signals will be published by the service's unique name,
        # which is why we need to track it.
        match_rule = MatchRule(
            type='signal',
            sender=message_bus.bus_name,
            interface=message_bus.interface,
            member='NameOwnerChanged',
            path=message_bus.object_path,
        )
        match_rule.add_arg_condition(0, BUS_NAMESPACE, kind='namespace')

        message_proxy = Proxy(message_bus, router)
        await message_proxy.AddMatch(match_rule)

        with router.filter(match_rule, bufsize=20) as queue:
            while True:
                # https://dbus.freedesktop.org/doc/dbus-specification.html#bus-messages-name-owner-changed
                msg = await queue.get()
                (wellknown_name, old_name, new_name) = msg.body
                if old_name:
                    self.on_bus_removed(wellknown_name, old_name)
                if new_name:
                    await self.on_bus_added(router, wellknown_name, new_name)

    async def on_bus_added(self, router, wellknown_name, unique_name):
        if not self.valid_player(wellknown_name):
            self.msg.debug("Ignoring new bus that does not match configured player:"
                           f" {wellknown_name}")
            return
        player = await Player.new(self.config, router, wellknown_name, unique_name)
        self.msg.debug(f"Player connected: {player.wellknown_name}")
        self.players[unique_name] = player
        self._handle_player_update(player)

    def on_bus_removed(self, wellknown_name, unique_name):
        player = self.players.pop(unique_name, None)
        if not player:
            return
        self.msg.debug(f"Player disconnected: {player.wellknown_name}")
        if player == self.active_player:
            self._handle_player_stopped()
            self.active_player = None

    async def properties_watcher(self, router):
        # Select PropertiesChanged signals for the mpris-player subinterface
        # for all senders.
        match_rule = MatchRule(
            type='signal',
            sender=None,
            interface=IFACE_PROPERTIES,
            member='PropertiesChanged',
            path=PATH_MPRIS,
        )
        match_rule.add_arg_condition(0, IFACE_MPRIS_PLAYER)

        message_proxy = Proxy(message_bus, router)
        await message_proxy.AddMatch(match_rule)

        with router.filter(match_rule, bufsize=10) as queue:
            while True:
                msg = await queue.get()
                self.handle_properties_changed(msg)

    def handle_properties_changed(self, msg):
        # https://dbus.freedesktop.org/doc/dbus-specification.html#standard-interfaces-properties
        (_, changed_properties, _) = msg.body
        sender = msg.header.fields[HeaderFields.sender]
        try:
            playback_status = safe_get_dbus_value(changed_properties.get('PlaybackStatus'), 's')
            if playback_status:
                self.on_playback_status_change(sender, playback_status)

            metadata = safe_get_dbus_value(changed_properties.get('Metadata'), 'a{sv}')
            if metadata and ('xesam:title' in metadata or 'xesam:url' in metadata):
                title = safe_get_dbus_value(metadata.get('xesam:title'), 's')
                url = safe_get_dbus_value(metadata.get('xesam:url'), 's')
                length_us = safe_get_dbus_value(metadata.get('mpris:length'), 'x')
                length = int(length_us) // 1000 if length_us else None
                self.on_filename_change(sender, title, url, length)
        except TypeError as e:
            self.msg.warn(f"Failed to read properties for {sender}: {e}")

    def valid_player(self, wellknown_name):
        return self.re_players.search(wellknown_name)

    def find_playing_player(self) -> bool:
        # Go through all connected players in random order
        # (or not since dicts are ordered now).
        return any(
            self._update_active_player(player, probing=True)
            for player in self.players.values()
            if player.playback_status == PlaybackStatus.PLAYING
        )

    def on_playback_status_change(self, sender, playback_status):
        player = self.players.get(sender)
        if not player:
            return
        player.playback_status = playback_status
        self._handle_player_update(player)

    def on_filename_change(self, sender, title, url, length=None):
        player = self.players.get(sender)
        if not player:
            return
        player.title = title
        player.url = url
        # `length` is None both when the metadata genuinely doesn't carry
        # a duration and when this is called without one (e.g. tests) --
        # in the latter case keep whatever the player already reported
        # rather than clobbering a known-good value.
        if length is not None:
            player.length = length
        self._handle_player_update(player)

    def _handle_player_update(self, player):
        if player == self.active_player:
            if player.playback_status == PlaybackStatus.STOPPED:
                self._handle_player_stopped()
            else:
                self._update_active_player(player)

        elif self.active_player and self.active_player.playback_status == PlaybackStatus.PLAYING:
            self.msg.debug("Still playing on active player; ignoring update")
            return

        elif player.playback_status == PlaybackStatus.PLAYING:
            self._update_active_player(player)

    def _update_active_player(self, player: Player, probing=False) -> bool:
        is_new_player = player != self.active_player

        (state, show_tuple) = (None, None)
        previous_last_filename = self.last_filename
        new_show = False
        if player.filename != self.last_filename:
            (state, show_tuple) = self._get_playing_show(player.filename)
            if state in [utils.Tracker.UNRECOGNIZED, utils.Tracker.NOT_FOUND]:
                self.msg.debug("Video not recognized")
            elif state == utils.Tracker.NOVIDEO:
                self.msg.debug("No video loaded")
            else:
                new_show = True
                self.wait_s = self._percentage_wait_s(player)

        if is_new_player and not new_show:
            if probing:
                # Ignore this 'new' player & restore `last_filename`
                # since we're just looking for a new player candidate.
                # (This is a hack but a proper fix needs larger refactoring
                # involving the parent class.)
                self.last_filename = previous_last_filename
            return False

        if is_new_player and new_show:
            self.msg.debug(f"Setting active player: {player.wellknown_name}")
            self.active_player = player

        if state:
            self.msg.debug(f"New tracker status: {state} (previously: {self.last_state})")
            self.update_show_if_needed(state, show_tuple)

        if player.playback_status == PlaybackStatus.PLAYING:
            self._start_timer()
        else:
            self._stop_timer()

        return True

    def _handle_player_stopped(self):
        # Active player got closed!
        if self.active_player:
            self.msg.debug(f"Clearing active player: {self.active_player.wellknown_name}")
        self.active_player = None
        self.view_offset = None
        self.length = None

        if not self.find_playing_player():
            (state, show_tuple) = self._get_playing_show(None)
            self.update_show_if_needed(state, show_tuple)
            self._stop_timer()

    def _percentage_wait_s(self, player):
        # tracker_update_percentage% of the episode's actual duration,
        # like Plex/Kodi's own obey_update_wait_s toggles -- roughly the
        # runtime minus OP/ED at the default 80%. Falls back to the fixed
        # tracker_update_wait_s (returning None, which update_timer()
        # treats the same way) if the player hasn't reported a duration
        # yet, e.g. right when playback starts.
        if self.config['mpris_obey_update_wait_s'] or not player.length:
            return None
        fraction = self.config['tracker_update_percentage'] / 100.0
        target_s = (player.length / 1000) * fraction
        if self.view_offset is not None:
            # Anchor to the actual playback position (view_offset, ms):
            # resuming an episode mid-way or seeking shouldn't restart
            # the whole percentage wait from zero. update_timer()
            # measures wait_s against _effective_elapsed_s(), so re-add
            # that same elapsed time on top of the playback remaining.
            remaining_s = max(0.0, target_s - self.view_offset / 1000)
            return round(remaining_s + self._effective_elapsed_s())
        return round(target_s)

    def _start_timer(self):
        self.resume_timer()
        if not self.timing:
            self.timing = True
            self.msg.debug("MPRIS timer started.")

    def _stop_timer(self):
        self.pause_timer()
        if self.timing:
            self.timing = False
            self.msg.debug("MPRIS timer paused.")

    async def _timer(self):
        while True:
            if self.timing:
                await self._on_tick()
            await asyncio.sleep(1, True)

    async def _on_tick(self):
        if self.active_player:
            try:
                self.view_offset = int(await self.active_player.get_position()) / 1000
            except TypeError:
                self.view_offset = None
                # The view_offset is not important, so we ignore errors.
                pass

            self.length = self.active_player.length

            # Recompute every tick: the player may not have reported a
            # duration yet right when playback started (mpris:length
            # arriving a beat after xesam:title/url is common), and the
            # percentage wait tracks the live playback position, which
            # moves under seeks and pauses.
            new_wait = self._percentage_wait_s(self.active_player)
            if new_wait is not None:
                self.wait_s = new_wait

        if self.last_show_tuple:
            self.update_timer(self.last_state, self.last_show_tuple)

        if self.last_updated:
            self._stop_timer()
