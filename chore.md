# Chores — blocked on the AUR coming back

The AUR refused SSH on 2026-08-07 ("The AUR is down due to maintenance"). Everything
below is queued behind that. Nothing here affects the GitHub repos, which are already
pushed: `hakubun-plus` master `d3aecff`, `hakubun` master `f5a2318`.

## 1. Push the anitomy-ng bump (done locally, just needs the push)

Commit `c8e79c2 upgpkg: 1.0.9` is sitting in a durable clone:

```
git -C /home/makkii/git/python-anitomy-ng-bin push origin master
```

Already verified: `makepkg -f` builds clean, both checksums validate, and the packaged
module has `parse_path` plus the three 1.0.9 fixes. `.SRCINFO` regenerated with
`makepkg --printsrcinfo`. Push remote is already set to
`ssh://aur@aur.archlinux.org/python-anitomy-ng-bin.git` (the fetch URL is https and
read-only, which is why the push URL is set separately).

Until this lands, anyone installing `python-anitomy-ng-bin` from the AUR gets 1.0.7,
which is below the pin both front-ends now declare.

## 2. Declare the anitomy-ng dependency in both front-end PKGBUILDs

This is the real hole. `pyproject.toml` now requires `anitomy-ng>=1.0.9`, but neither
AUR package tells pacman anything useful about it:

| package | anitomy-ng declared as | versioned? |
|---|---|---|
| `hakubun-git` | not listed at all | — |
| `hakubun-plus-git` | `optdepends` (line 33) | no |
| `python-anitomy-ng-bin` | (is the package) | bumped to 1.0.9, unpushed |

Failure mode if left alone: someone on `python-anitomy-ng-bin` 1.0.7 updates either
front-end, gets the new wrapper against the old library, and `parse_path` raises
`AttributeError` into the wrapper's `except Exception: elements = []`. Every filename
parses to nothing, with no error and no pacman warning. Same silent failure called out
in the PR #29 review, now reachable through the package manager.

**Decision needed before editing:** versioned `optdepends`, or promote to `depends`?

- Versioned `optdepends` — `'python-anitomy-ng-bin>=1.0.9: filename parsing via
  anitomy-ng (AUR)'`. Honest documentation, but pacman does not *enforce* optdepend
  versions, so a stale 1.0.7 still slips through silently. Fixes nothing mechanically.
- `depends=('python-anitomy-ng-bin>=1.0.9')` — pacman refuses the mismatch, which is
  the only option that actually prevents the silent-empty-parse. Cost: makes
  anitomy-ng mandatory for people who use the anitopy parser instead.

`hakubun-git` needs the line added either way — it currently lists only
`python-anitopy-git` for filename parsing.

Clones for editing (throwaway, re-clone if the job scratch is gone):

```
git clone https://aur.archlinux.org/hakubun-git.git
git clone https://aur.archlinux.org/hakubun-plus-git.git
```

Remember `makepkg --printsrcinfo > .SRCINFO` after any PKGBUILD edit.

## Not blocked on the AUR

### ~~Credit anitomy-ng in `hakubun`'s About dialogs~~ — done 2026-08-07

Both dialogs now credit it (`hakubun/ui/qt/mainwindow.py`, `hakubun/ui/gtk/window.py`).
Uncommitted in `/home/makkii/git/hakubun` alongside the MPRIS progress-bar and
update-percentage fixes.

### anitomy-ng 1.0.10 follow-ups

Three follow-ups reported to tylergibbs2 on PR #29 for anitomy-ng 1.0.10, all
upstream, none hakubun-side:

1. A bare `Title - NN` basename under an *unrelated* directory prefix still keeps the
   whole path in the title — `/mnt/nfs/Downloads/torrents/anime/Mobile Suit Gundam THE
   WITCH FROM MERCURY - 18.mp4`. 1 of 959 root-level files in the torrent library.
2. A grandparent folder can become the title and `NCED` in the path is not read as a
   type — `.../NC/NCED/02 - Harmonia.mkv` parses as `'NC'` ep 2. 25 files.
3. The `3` in the `EAC-3` audio term is taken as the episode, real episode folded into
   the title — `[sam] Dr. STONE - New World - 14 [WEB 1080p EAC-3]` gives ep 3. Not a
   regression (1.0.7 does it too), but `EAC-3` is a common modern release tag. 6 of the
   7 `EAC-3` files here.
