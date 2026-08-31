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

"""Shared UI translation setup for the Qt and GTK front-ends.

Both interfaces mark user-facing strings with gettext -- ``_()`` in Python
code, ``translatable="yes"`` in GtkBuilder XML -- against the single
``hakubun-plus`` domain, whose catalogs live under ``hakubun/locale/``.
Marking every string is a separate, larger pass; this module is the
plumbing that pass will rely on, wired up now so the Interface language
dropdown in each settings window is functional end to end.

Qt has no gettext integration of its own, so ``install()`` also loads the
matching Qt-bundled ``qtbase_<lang>.qm`` translation (native dialog button
labels, standard shortcuts, etc.) via QTranslator when PyQt6 is available.
GTK reads the process-wide gettext catalog directly through GtkBuilder's
``translatable="yes"`` strings, so no extra wiring is needed there beyond
calling ``bindtextdomain``/``textdomain``, which ``install()`` also does.
"""

import gettext
import locale
import os

DOMAIN = 'hakubun-plus'
LOCALE_DIR = os.path.join(os.path.dirname(__file__), 'locale')

# (code, display name) -- code is either 'auto' or a gettext language code
# matching a hakubun/locale/<code>/LC_MESSAGES/hakubun-plus.mo catalog.
SUPPORTED_LANGUAGES = [
    ('auto',  'System Default'),
    ('en',    'English'),
    ('ja',    '日本語 (Japanese)'),
    ('zh_CN', '简体中文 (Chinese, Simplified)'),
    ('zh_TW', '繁體中文 (Chinese, Traditional)'),
    ('es',    'Español (Spanish)'),
]

_translation = gettext.NullTranslations()
_active_language = 'en'


# Primary-title choices are intentionally tied to the interface language.
# The values are stable config/API identifiers; labels are resolved live so
# the Settings window itself follows the selected UI catalog.
_TITLE_MODES = {
    'en': (
        ('english', 'English'),
        ('romaji', 'Romaji'),
        ('native', 'Native'),
    ),
    'ja': (
        ('native', '日本語'),
    ),
    'zh_CN': (
        ('zh-Hans', '简体中文'),
        ('zh-Hant', '繁體中文'),
        ('native', 'Native'),
    ),
    'zh_TW': (
        ('zh-Hant', '繁體中文'),
        ('zh-Hans', '简体中文'),
        ('native', 'Native'),
    ),
    'es': (
        ('spanish', 'Español'),
        ('romaji', 'Romaji'),
        ('native', 'Native'),
    ),
}


def effective_language(language='auto'):
    """Return one supported language code for a UI config value.

    ``auto`` follows the same environment/locale inputs gettext uses, but
    collapses regional variants (``es_MX`` -> ``es``) to the catalogs and
    title-mode matrices Hakubun+ actually supports.
    """
    candidates = []
    if language and language != 'auto':
        candidates.append(language)
    else:
        for name in ('LANGUAGE', 'LC_ALL', 'LC_MESSAGES', 'LANG'):
            value = os.environ.get(name)
            if value:
                candidates.extend(value.split(':'))
        try:
            loc = locale.getlocale(locale.LC_MESSAGES)[0]
        except (AttributeError, TypeError, ValueError):
            loc = None
        if loc:
            candidates.append(loc)

    for candidate in candidates:
        code = candidate.split('.', 1)[0].replace('-', '_')
        if code in _TITLE_MODES:
            return code
        if code.startswith('zh_'):
            territory = code.split('_', 1)[1].upper()
            return 'zh_TW' if territory in ('TW', 'HK', 'MO', 'HANT') \
                else 'zh_CN'
        base = code.split('_', 1)[0].lower()
        if base in ('en', 'ja', 'es'):
            return base
    return 'en'


def active_language():
    """Language selected by the most recent :func:`install` call."""
    return _active_language


def title_mode_options(language='auto'):
    """Available ``(mode, label)`` primary-title choices for a UI language."""
    return [(mode, _(label))
            for mode, label in _TITLE_MODES[effective_language(language)]]


def default_title_mode(language='auto'):
    """Default primary-title mode for a UI language."""
    return _TITLE_MODES[effective_language(language)][0][0]


def _(msgid):
    """Live-redirecting translation lookup for code that can't rely on
    the ambient builtin `_()` -- notably hakubun/lib/*.py, which is also
    imported by the (gettext-less) CLI/curses front-end. Unlike the
    builtin, which install() rebinds by name, this indirects through the
    module-level _translation on every call, so it picks up whatever
    language (or NullTranslations identity, if install() was never
    called) is current at call time regardless of import order."""
    return _translation.gettext(msgid)


def _p(context, msgid):
    """Context-qualified counterpart to _() -- same live redirection, but
    for words that are ambiguous stripped of context ('Completed' the
    watch status vs. 'Completed' the progress-bar-color swatch label in
    Settings > Theme -- both plain English "Completed"). Without a
    context, translating one would silently retranslate the other to
    the same text, which is wrong for languages where the two don't
    share a word."""
    return _translation.pgettext(context, msgid)


# The Details view's facts table is built from (label, value) tuples that
# hakubun/lib/*.py adapters return per-provider (see e.g. libanilist.py's
# 'extra' list). Those labels double as identifiers -- hakubun/data.py and
# DetailsWidget.PROSE_KEYS both compare against the literal English string
# (key == 'Synopsis') to special-case that field's rendering -- so the
# adapters themselves must keep returning untranslated English keys, or
# every non-English locale would silently break that comparison.
#
# Translation therefore happens display-side only: DetailsWidget and
# ShowInfoBox look up _(key) when building each row's label, after any
# identity comparison against the raw key has already happened. Because
# that lookup passes a runtime variable, xgettext can't discover the
# messages by scanning the call site -- this literal list exists purely
# so extraction finds them; it is never iterated at runtime. Keep it in
# sync with the labels the adapters actually emit.
_DETAILS_FIELD_LABELS = (
    _('English'), _('Romaji'), _('Japanese'), _('Synonyms'), _('Season'),
    _('Genres'), _('Studios'), _('Synopsis'), _('Type'), _('Average score'),
    _('Mean score'), _('Status'), _('Description'), _('Russian title'),
    _('Japanese title'), _('English title'), _('Score'), _('Age Rating'),
    _('Titles'), _('Average Rating'), _('Rank'), _('NSFW'),
    _('Serialization'), _('Expected Release'), _('Original Name'),
    _('Released'), _('Languages'), _('Original Language'), _('Platforms'),
    _('Aliases'), _('Length'), _('Links'), _('Next episode will air in'),
    _('Next episode'), _('Tracker title'),
)

# Same reasoning as _DETAILS_FIELD_LABELS above, but for the watch/read
# status names each hakubun/lib/*.py adapter's statuses_dict returns
# (e.g. libanilist.py's mediatypes['anime']['statuses_dict']) -- covers
# every mediatype across every adapter, including VNDB's vnlist
# (Playing/Finished/Stalled/...) and wishlist (High/Medium/Low/...),
# since engine._localize_mediainfo() translates whichever
# mediainfo['statuses_dict'] it's handed the same way regardless of
# provider or mediatype. Those dicts key on the *provider's own status
# code* ('CURRENT', 'watching', 1, ...), never on the English display
# string, so translating the values at the source is safe -- nothing
# compares against them by identity. Spelling variants ('On Hold' /
# 'On hold' / 'On-Hold') are each their own adapter's exact string and
# need their own catalog entry.
#
# Uses the 'status' msgctxt (_p, not _) since some of these words --
# "Completed" above all -- collide with unrelated same-spelled UI
# strings elsewhere (the Settings > Theme progress-bar-color swatch
# label) that must not share a translation.
_STATUS_LABELS = (
    _p('status', 'Watching'), _p('status', 'Completed'),
    _p('status', 'Rewatching'), _p('status', 'Paused'),
    _p('status', 'Dropped'), _p('status', 'Plan to Watch'),
    _p('status', 'On Hold'), _p('status', 'On hold'),
    _p('status', 'On-Hold'), _p('status', 'Reading'),
    _p('status', 'Rereading'), _p('status', 'Re-reading'),
    _p('status', 'Plan to Read'),
    # VNDB vnlist (play/read progress) and wishlist (priority).
    _p('status', 'Playing'), _p('status', 'Finished'),
    _p('status', 'Stalled'), _p('status', 'Unknown'),
    _p('status', 'High'), _p('status', 'Medium'),
    _p('status', 'Low'), _p('status', 'Blacklist'),
)

# Same reasoning again, for the list/table column headers -- Qt's
# ShowListModel.columns and AddTableModel.columns, and GTK's
# ShowTreeView.available_columns. All three are consulted by identity
# (config's 'visible_columns', the column-toggle menus, the Qt sort
# column keys) so they stay English at the source; headerData() and
# the column-header Gtk.Label/menu items call _(name) for display only.
_COLUMN_LABELS = (
    _('ID'), _('Title'), _('Progress'), _('Score'), _('Percent'),
    _('Next Episode'), _('Start date'), _('End date'), _('My start'),
    _('My finish'), _('Tags'), _('Status'), _('Last updated'),
    _('Season'), _('Type'), _('Platform Score'), _('MAL Score'),
    _('Synced Score'), _('Name'), _('Total'), _('In Your List'),
    _('Start'), _('End'), _('My end'), _('Airing Status'),
)

# Date names are looked up dynamically after strftime produces the system
# locale's English token, so keep literals here for gettext extraction.
_DATE_LABELS = (
    _('Monday'), _('Tuesday'), _('Wednesday'), _('Thursday'),
    _('Friday'), _('Saturday'), _('Sunday'),
    _('Mon'), _('Tue'), _('Wed'), _('Thu'), _('Fri'), _('Sat'), _('Sun'),
    _('Jan'), _('Feb'), _('Mar'), _('Apr'), _('May'), _('Jun'),
    _('Jul'), _('Aug'), _('Sep'), _('Oct'), _('Nov'), _('Dec'),
)

# title_mode_options() looks labels up dynamically from _TITLE_MODES, so these
# literals keep the setting choices visible to xgettext.
_TITLE_MODE_LABELS = (
    _('Romaji'), _('Native'), _('日本語'), _('简体中文'),
    _('繁體中文'), _('Español'),
)


def _languages_for(language):
    """Resolves a config 'language' value to the list gettext.translation's
    `languages` argument expects, or None to let gettext fall back to the
    process locale/environment (LANGUAGE, LC_ALL, LC_MESSAGES, LANG)."""
    if not language or language == 'auto':
        return None
    return [language]


def install(language='auto'):
    """Loads the hakubun-plus gettext catalog for `language` and installs
    it as the global `_()` (and `ngettext()`), and points GtkBuilder's
    translation lookups at the same catalog. Must be called before any
    translatable string is built -- for Qt that means before constructing
    any widgets; for GTK it means before importing any module that defines
    a Gtk.Template class, since the template XML is parsed at import time.

    Falls back to untranslated (identity) strings if no catalog exists
    for the requested language, rather than raising -- a missing .mo is
    expected for a language nobody has translated yet.
    """
    global _translation, _active_language
    _active_language = effective_language(language)

    # GtkBuilder resolves translatable="yes" strings through the C
    # library's dgettext() at parse time, which -- unlike Python's
    # gettext.translation(languages=...) below -- picks its language from
    # the environment (LANGUAGE, then LC_ALL/LC_MESSAGES/LANG), not from
    # any in-process state. Setting LANGUAGE is the standard way to force
    # a language independent of the system locale; for 'auto' we leave
    # whatever the user/system already has alone.
    if language and language != 'auto':
        os.environ['LANGUAGE'] = language

    try:
        _translation = gettext.translation(
            DOMAIN, localedir=LOCALE_DIR,
            languages=_languages_for(language), fallback=True)
    except OSError:
        _translation = gettext.NullTranslations()

    _translation.install(names=('gettext', 'ngettext'))

    try:
        locale.bindtextdomain(DOMAIN, LOCALE_DIR)
        locale.textdomain(DOMAIN)
    except AttributeError:
        # locale.bindtextdomain/textdomain aren't available on Windows'
        # msvcrt-based locale module -- GtkBuilder translation lookups
        # simply won't pick up our catalog there, which only matters for
        # the (Linux-only) GTK front-end.
        pass

    _install_qt_translator(language)


def _install_qt_translator(language):
    """Best-effort: loads Qt's own qtbase_<lang>.qm (native dialog button
    labels, standard shortcuts) if PyQt6 and a matching catalog are both
    available. No-op otherwise -- this only covers Qt's own strings, not
    hakubun-plus's, so its absence shouldn't block startup."""
    try:
        from PyQt6.QtCore import QLibraryInfo, QLocale, QTranslator
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return

    app = QApplication.instance()
    if app is None:
        return

    for translator in getattr(app, '_hakubun_qt_translators', []):
        app.removeTranslator(translator)
    app._hakubun_qt_translators = []

    qt_locale = QLocale() if (not language or language == 'auto') else QLocale(language)
    translations_path = QLibraryInfo.path(
        QLibraryInfo.LibraryPath.TranslationsPath)

    translator = QTranslator()
    if translator.load(qt_locale, 'qtbase', '_', translations_path):
        app.installTranslator(translator)
        app._hakubun_qt_translators.append(translator)
