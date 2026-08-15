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


def _(msgid):
    """Live-redirecting translation lookup for code that can't rely on
    the ambient builtin `_()` -- notably hakubun/lib/*.py, which is also
    imported by the (gettext-less) CLI/curses front-end. Unlike the
    builtin, which install() rebinds by name, this indirects through the
    module-level _translation on every call, so it picks up whatever
    language (or NullTranslations identity, if install() was never
    called) is current at call time regardless of import order."""
    return _translation.gettext(msgid)


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
    _('Aliases'), _('Length'), _('Links'),
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
    global _translation

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
