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

import os

from gi.repository import GObject, Gtk

from hakubun import utils
from hakubun.ui.gtk.imagebox import ImageBox
from hakubun.ui.gtk.showinfobox import ShowInfoBox

_IMAGE_SIZE = (150, 210)


class NowPlayingView(Gtk.Box):
    """
    Shows what the tracker currently sees playing: the recognized show
    (if any), its poster, playback position, and whether the player's
    current file was recognized at all -- plus Last/Next episode
    controls, followed by that show's normal details view (facts +
    synopsis), reused as-is.

    When nothing is recognized as playing, the details view is hidden
    entirely (it used to just keep showing whatever was last loaded)
    and replaced with a "play something at random" recommendation.
    """

    __gsignals__ = {
        'play-episode': (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        'play-random': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, engine=None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_border_width(18)
        self._engine = engine
        self._show = None
        self._episode = None

        self.image_box = ImageBox(*_IMAGE_SIZE)
        self.image_box.set_halign(Gtk.Align.CENTER)
        self.pack_start(self.image_box, False, False, 0)

        self.title_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.title_label.get_style_context().add_class('title-2')
        self.pack_start(self.title_label, False, False, 0)

        self.status_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.pack_start(self.status_label, False, False, 0)

        # A GtkBox's non-packing-direction size (width, here, since this
        # box is VERTICAL) isn't governed by pack_start's expand/fill --
        # those only affect height -- so a bare ProgressBar defaults to
        # halign=FILL and stretches to the box's full width. That's fine
        # in a normal-width window but turns into an absurdly long, thin
        # sliver if the window is very wide, so it's capped and centered
        # instead of left to fill.
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_halign(Gtk.Align.CENTER)
        self.progress_bar.set_size_request(400, -1)
        self.pack_start(self.progress_bar, False, False, 0)

        self.position_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.pack_start(self.position_label, False, False, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.set_halign(Gtk.Align.CENTER)
        self.last_ep_btn = Gtk.Button(label='Last Episode')
        self.last_ep_btn.connect('clicked', self._on_last_episode)
        self.next_ep_btn = Gtk.Button(label='Next Episode')
        self.next_ep_btn.connect('clicked', self._on_next_episode)
        controls.pack_start(self.last_ep_btn, False, False, 0)
        controls.pack_start(self.next_ep_btn, False, False, 0)
        self.pack_start(controls, False, False, 0)

        # The title/poster above already cover this -- showing them
        # again here (this box is normally used standalone in the
        # Details window) would just duplicate what's already on screen,
        # so only its facts+synopsis text is used. ShowInfoBox's own
        # async load callback unconditionally re-.show()s label_title
        # once details finish fetching, which no_show_all alone doesn't
        # stop (that only affects show_all()), so a 'show' handler that
        # immediately re-hides it is needed to make this actually stick.
        self.details_box = ShowInfoBox(engine, orientation=Gtk.Orientation.VERTICAL)
        # Needs the same no_show_all treatment as its children below: the
        # container window calls show_all() on its whole widget tree once
        # at startup, right after show_nothing_playing() below has
        # already hidden this box -- without no_show_all, that show_all()
        # un-hides it again, leaving both this and empty_box visible and
        # fighting for space in an unscrolled box (and pushing the "Watch
        # a Random Episode" button out of reach).
        self.details_box.set_no_show_all(True)
        for widget in (self.details_box.label_title, self.details_box.image_container):
            widget.set_no_show_all(True)
            widget.hide()
            widget.connect('show', lambda w: w.hide())
        # Facts text is a single left-aligned Gtk.Label that otherwise
        # stretches to the full window width -- fine in the narrower
        # sidebar/dialog contexts ShowInfoBox is normally used in, but
        # on this wide, centered page it reads as a giant left-hugging
        # block under the centered poster/title above it. data_container
        # is already halign=center in showinfobox.ui, so it self-centers
        # correctly *once* the label has a real wrap width -- without
        # max-width-chars, a long unbroken line (e.g. the synopsis
        # paragraph) makes the label request an unbounded natural width,
        # which data_container then shrink-wraps to, leaving no slack to
        # center within. width-chars must be set to the same value too:
        # max-width-chars alone still lets a long enough single line push
        # the natural request past it.
        data_label = self.details_box.data_label
        data_label.set_width_chars(72)
        data_label.set_max_width_chars(72)
        self.pack_start(self.details_box, True, True, 0)

        self.empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        empty_label = Gtk.Label(label="Nothing to show details for yet.")
        empty_label.get_style_context().add_class('dim-label')
        self.empty_box.pack_start(empty_label, False, False, 0)
        random_btn = Gtk.Button(label='Watch a Random Episode')
        random_btn.set_halign(Gtk.Align.CENTER)
        random_btn.connect('clicked', lambda *_a: self.emit('play-random'))
        self.empty_box.pack_start(random_btn, False, False, 0)
        self.pack_start(self.empty_box, True, True, 0)

        self.set_engine(engine)
        self.show_nothing_playing()

    def set_engine(self, engine):
        self._engine = engine
        self.details_box._engine = engine

    def _show_empty_state(self):
        self.details_box.hide()
        self.empty_box.show_all()

    def _show_details_state(self):
        self.empty_box.hide()
        # details_box has no_show_all set (see the comment in __init__) so
        # that the container window's startup show_all() can't un-hide it
        # while nothing is playing -- but that same no_show_all also makes
        # GTK ignore a *direct* show_all() call on this widget, silently
        # leaving it (and everything under it) invisible forever. A plain
        # show() isn't subject to that and is all that's needed here: its
        # visible descendants already carry their own visibility (set in
        # showinfobox.ui, or explicitly re-shown once details finish
        # loading in ShowInfoBox._show_load_finish_idle).
        self.details_box.show()

    def show_nothing_playing(self):
        self._show = None
        self._episode = None
        self.image_box.reset()
        self.title_label.set_text('Nothing playing')
        self.status_label.set_text('')
        self.position_label.set_text('')
        self.progress_bar.set_fraction(0)
        self.last_ep_btn.set_sensitive(False)
        self.next_ep_btn.set_sensitive(False)
        self._show_empty_state()

    def update_status(self, status):
        state = status.get('state')

        # IGNORED means the tracker recognized the show/episode fine but
        # isn't updating progress for it (e.g. replaying an
        # already-watched episode from the player's own playlist) -- it
        # should still show details, just labeled differently, rather
        # than falling through to "nothing playing" as if it weren't
        # recognized at all.
        if state in (utils.Tracker.PLAYING, utils.Tracker.IGNORED) and status.get('show'):
            self._update_recognized(status)
        elif state == utils.Tracker.UNRECOGNIZED:
            self._show = None
            self._episode = None
            self.image_box.reset()
            self.title_label.set_text('Unrecognized video')
            self.status_label.set_text(
                "The file that's playing doesn't match anything in your list.")
            self.position_label.set_text('')
            self.progress_bar.set_fraction(0)
            self.last_ep_btn.set_sensitive(False)
            self.next_ep_btn.set_sensitive(False)
            self._show_empty_state()
        elif state == utils.Tracker.NOT_FOUND:
            self._show = None
            self._episode = None
            self.image_box.reset()
            self.title_label.set_text('Not in your list')
            self.status_label.set_text(
                "Playing, but this show isn't in your list.")
            self.position_label.set_text('')
            self.progress_bar.set_fraction(0)
            self.last_ep_btn.set_sensitive(False)
            self.next_ep_btn.set_sensitive(False)
            self._show_empty_state()
        else:
            self.show_nothing_playing()

    def _update_recognized(self, status):
        tracker_show, episode = status['show']
        self._episode = episode

        # The tracker's own show dict is slim (id/title/progress/total) --
        # fetch the full one for the poster and up-to-date progress.
        show = None
        if self._engine:
            try:
                show = self._engine.get_show_info(tracker_show['id'])
            except utils.HakubunError:
                show = None
        show = show or tracker_show

        is_new_show = self._show is None or self._show['id'] != show['id']
        self._show = show

        self.title_label.set_text(show['title'])
        if status.get('state') == utils.Tracker.IGNORED:
            self.status_label.set_text('Already watched -- Episode %d' % episode)
        else:
            self.status_label.set_text('Recognized -- Episode %d' % episode)
        self.position_label.set_text(
            utils.format_playback_position(
                status.get('viewOffset'), status.get('length')))
        length = status.get('length')
        offset = status.get('viewOffset')
        self.progress_bar.set_fraction(
            min(1.0, offset / length) if length and offset is not None else 0)

        if show.get('image_thumb') or show.get('image'):
            api_info = self._engine.api_info if self._engine else None
            if api_info:
                utils.make_dir(utils.to_cache_path())
                filename = utils.to_cache_path("%s_%s_f_%s.jpg" % (
                    api_info['shortname'], api_info['mediatype'], show['id']))
                if os.path.isfile(filename):
                    self.image_box.set_image(filename)
                else:
                    self.image_box.set_image_remote(
                        show.get('image_thumb') or show['image'], filename)
        else:
            self.image_box.reset()

        self.last_ep_btn.set_sensitive(episode > 1)
        self.next_ep_btn.set_sensitive(True)
        self._show_details_state()

        if is_new_show:
            self.details_box.load(show)

    def _on_last_episode(self, _btn):
        if self._show and self._episode and self._episode > 1:
            self.emit('play-episode', self._show['id'], self._episode - 1)

    def _on_next_episode(self, _btn):
        if self._show and self._episode:
            self.emit('play-episode', self._show['id'], self._episode + 1)
