# hakubun-plus, packaged from this repository.
#
# ## uv builds it; uv does not install its dependencies
#
# Nix builds run sandboxed with no network, so `uv sync` / `uv pip install`
# cannot fetch anything at build time. uv stays the development tool -- see the
# devShell in flake.nix -- and Nix resolves dependencies from nixpkgs instead.
#
# That is not merely a workaround here, it is the better option: every
# dependency this project has is already in nixpkgs, built natively against the
# right GTK and Qt. uv2nix would instead build them from PyPI wheels, and
# PyGObject and PyQt6 are exactly the case where wheels fight Nix hardest.
# uv.lock is therefore not consulted -- revisit that only if a dependency
# appears that nixpkgs lacks and the exact pin starts to matter.
#
# `build-backend = "uv_build"` is fine as a *build* backend: nixpkgs carries
# uv-build, and it is listed in `build-system` below.
#
# ## Why both frontends are wrapped by the Qt hook
#
# hakubun-plus-gtk needs wrapGAppsHook4 (GI_TYPELIB_PATH, GSettings schemas,
# pixbuf loaders) and hakubun-plus-qt needs wrapQtAppsHook (QT_PLUGIN_PATH).
# Both hooks wrap every executable in $out/bin, so left alone they wrap each
# other and the inner environment is lost. The standard resolution is to stop
# wrapGAppsHook from wrapping anything and hand its arguments to the Qt hook,
# which then does the single wrap for all three entry points.
{ lib
, python3Packages
, src
, version
, gobject-introspection
, wrapGAppsHook3
, qt6
, gtk3
, pango
, gdk-pixbuf
, librsvg
, glib
, atk
, harfbuzz
}:

let
  # Computed here rather than read from $GI_TYPELIB_PATH at fixup time.
  # gobject-introspection's setup hook does populate that variable, but with
  # `dontWrapGApps` in play it was empty by the time preFixup ran, and the
  # resulting package built cleanly and then failed at startup with
  # "ValueError: Namespace Gtk not available". Deriving the path in Nix removes
  # the dependency on hook ordering entirely.
  # Every one of these keeps its typelibs in the `out` output specifically, and
  # for several the *default* output is something else (pango, glib, atk), so
  # naming the package alone silently contributes a path that does not exist.
  # That fails one namespace at a time at startup, which is a slow way to find
  # out -- hence the explicit `.out` on all of them.
  giTypelibPath = lib.makeSearchPath "lib/girepository-1.0" [
    glib.out
    gtk3.out
    gdk-pixbuf.out
    pango.out
    atk.out
    harfbuzz.out
    librsvg.out
    # pygobject pulls in the cairo typelib as soon as anything draws
    # ("ImportError: Typelib file for namespace 'cairo', version '1.0' not
    # found"). Note it ships with gobject-introspection, *not* with the cairo
    # package -- adding `cairo` here looks right and changes nothing.
    gobject-introspection
  ];
in

python3Packages.buildPythonApplication {
  pname = "hakubun-plus";
  inherit src version;
  pyproject = true;

  build-system = [ python3Packages.uv-build ];

  # A development checkout carries a dist/ from previous `uv build` runs. If
  # the source reaches Nix unfiltered (a `path:` flake input does exactly that,
  # ignoring git), pypaInstallPhase finds both the stale wheel and the one just
  # built and dies with "FileExistsError: File already exists: .../bin/
  # hakubun-plus". Clearing it makes the build independent of how src arrived.
  postPatch = ''
    rm -rf dist build
  '';

  dependencies = with python3Packages; [
    # [project.dependencies]
    anitomy-ng
    # [project.optional-dependencies] -- all of them, since the package ships
    # both frontends and the trackers/rpc features are what it is for.
    pygobject3
    pycairo
    pillow
    pyqt6
    inotify
    pyinotify
    jeepney
    pypresence
    anitopy
    rapidfuzz
  ];

  nativeBuildInputs = [
    gobject-introspection
    # GTK *3*, not 4: the frontend does `require_version('Gtk', '3.0')` and
    # `require_version('Gdk', '3.0')`. Building it against gtk4 produces a
    # package that installs fine and then dies at startup on the version
    # requirement, so this has to match the source.
    wrapGAppsHook3
    qt6.wrapQtAppsHook
  ];

  buildInputs = [
    gtk3
    pango
    gdk-pixbuf
    librsvg
    # wrapQtAppsHook reads qtPluginPrefix off qtbase and fails outright without
    # it: "qtPluginPrefix is unset. hint: add qt6.qtbase to buildInputs".
    qt6.qtbase
  ];

  # Stop wrapGAppsHook3 wrapping anything, so it cannot double-wrap on top of
  # wrapQtAppsHook; the environment both frontends need is supplied through
  # buildPythonApplication's own makeWrapperArgs instead.
  #
  # Going through qtWrapperArgs from preFixup -- the usual GTK+Qt idiom -- does
  # *not* work here: the resulting wrappers ended up setting only PATH, and the
  # package built cleanly and then died at startup with "ValueError: Namespace
  # Gtk not available". makeWrapperArgs is the mechanism buildPythonApplication
  # actually applies to its console-script entry points.
  dontWrapGApps = true;
  makeWrapperArgs = [
    "--prefix" "GI_TYPELIB_PATH" ":" giTypelibPath
  ];

  # The suite talks to the real tracking sites.
  doCheck = false;

  pythonImportsCheck = [ "hakubun" ];

  meta = {
    description = "Open multi-site list manager for Unix-like systems (independent fork of Trackma, with Taiga mode and an airing schedule window)";
    homepage = "https://github.com/trektn/hakubun-plus";
    license = lib.licenses.gpl3Plus;
    mainProgram = "hakubun-plus";
    platforms = lib.platforms.linux;
  };
}
