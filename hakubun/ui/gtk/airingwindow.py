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

import datetime
import os
import threading

from gi.repository import GdkPixbuf, GLib, Gtk

from hakubun import utils
from hakubun.ui.gtk.imagebox import ImageThread, scale

_CARD_WIDTH = 140
_IMAGE_SIZE = (110, 155)


class AiringScheduleWindow(Gtk.Window):
    """
    A week-view (today + the next 6 days) of the airing shows in the
    user's list, cross-referenced from AniList's public API -- see
    Engine.get_airing_schedule(). Each show gets a card (poster, title,
    relative air time, caught-up/behind status) under the day it airs.
    """

    def __init__(self, engine, transient_for=None):
        Gtk.Window.__init__(self, title='Airing Schedule',
                            transient_for=transient_for)
        self.set_default_size(1100, 420)
        self._engine = engine
        # Kept here (rather than letting each card own its thread) so
        # they have an unambiguous, stable-lifetime owner for as long as
        # this window is open.
        self._image_threads = []

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_border_width(8)

        self._status_label = Gtk.Label(
            label='Loading airing schedule...', xalign=0)
        outer.pack_start(self._status_label, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self._week_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        scroller.add(self._week_box)
        outer.pack_start(scroller, True, True, 0)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        close_btn = Gtk.Button(label='Close')
        close_btn.connect('clicked', lambda *_a: self.destroy())
        button_row.pack_end(close_btn, False, False, 0)
        outer.pack_start(button_row, False, False, 0)

        self.add(outer)
        self.show_all()

        threading.Thread(target=self._fetch_task, daemon=True).start()

    def _fetch_task(self):
        try:
            schedule = self._engine.get_airing_schedule()
        except Exception as e:
            # Broad on purpose: any stray exception would otherwise kill
            # this daemon thread and leave the window on 'Loading...'.
            GLib.idle_add(self._status_label.set_text,
                          'Could not load airing schedule: %s' % e)
            return
        GLib.idle_add(self._populate, schedule)

    def _populate(self, schedule):
        today = datetime.date.today()
        days = [today + datetime.timedelta(days=i) for i in range(7)]
        by_day = {day: [] for day in days}
        for entry in schedule:
            day = entry['airing_at'].astimezone().date()
            if day in by_day:
                by_day[day].append(entry)

        total_this_week = sum(len(entries) for entries in by_day.values())
        if not schedule:
            self._status_label.set_text(
                'No airing shows with a known schedule in your list.')
        elif not total_this_week:
            self._status_label.set_text(
                'Nothing airing in the next 7 days -- %d upcoming episode(s) further out.'
                % len(schedule))
        else:
            self._status_label.set_text(
                '%d episode(s) airing this week.' % total_this_week)

        api_info = self._engine.api_info
        for day in days:
            entries = sorted(by_day[day], key=lambda e: e['airing_at'])
            self._week_box.pack_start(
                self._build_day_column(day, entries, api_info),
                False, False, 0)

        self._week_box.show_all()

    def _set_image_pixbuf(self, image, filename):
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(filename)
        width, height = scale(
            pixbuf.get_width(), pixbuf.get_height(), *_IMAGE_SIZE)
        image.set_from_pixbuf(pixbuf.scale_simple(
            width, height, GdkPixbuf.InterpType.BILINEAR))

    def _build_day_column(self, day, entries, api_info):
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        column.set_size_request(_CARD_WIDTH, -1)

        header_text = day.strftime('%A\n%b %d')
        header = Gtk.Label(justify=Gtk.Justification.CENTER)
        escaped = GLib.markup_escape_text(header_text)
        if day == datetime.date.today():
            header.set_markup('<span foreground="#74C0FA"><b>%s</b></span>' % escaped)
        else:
            header.set_markup('<b>%s</b>' % escaped)
        column.pack_start(header, False, False, 0)

        for entry in entries:
            column.pack_start(
                self._build_card(entry, api_info), False, False, 0)

        return column

    def _build_card(self, entry, api_info):
        show = entry['show']
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.get_style_context().add_class('frame')
        # Forces the title label below to actually wrap at this width
        # instead of stretching the card to fit its unwrapped text.
        card.set_size_request(_CARD_WIDTH, -1)

        image = Gtk.Image()
        image.set_size_request(*_IMAGE_SIZE)
        image.set_halign(Gtk.Align.CENTER)
        if show.get('image_thumb') or show.get('image'):
            utils.make_dir(utils.to_cache_path())
            filename = utils.to_cache_path("%s_%s_f_%s.jpg" % (
                api_info['shortname'], api_info['mediatype'], show['id']))
            if os.path.isfile(filename):
                self._set_image_pixbuf(image, filename)
            else:
                thread = ImageThread(
                    show.get('image_thumb') or show['image'], filename,
                    *_IMAGE_SIZE, lambda f: self._set_image_pixbuf(image, f))
                self._image_threads.append(thread)
                thread.start()
        card.pack_start(image, False, False, 0)

        title = Gtk.Label(label=show['title'], wrap=True,
                          justify=Gtk.Justification.CENTER, max_width_chars=1)
        card.pack_start(title, False, False, 0)

        when_label = Gtk.Label(
            label='Ep %d — %s' % (
                entry['episode'], utils.format_relative_airtime(entry['airing_at'])),
            wrap=True, justify=Gtk.Justification.CENTER)
        when_label.set_tooltip_text(utils.format_local_time(entry['airing_at']))
        card.pack_start(when_label, False, False, 0)

        behind_by = (entry['episode'] - 1) - (show.get('my_progress') or 0)
        status = Gtk.Label()
        if behind_by <= 0:
            status.set_markup('<span foreground="#4CAF50"><b>Up to date</b></span>')
        else:
            status.set_markup(
                '<span foreground="#E5A400"><b>Behind by %d</b></span>' % behind_by)
        card.pack_start(status, False, False, 0)

        return card
