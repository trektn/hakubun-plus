"""Three-way field diff (git: merge-base vs working tree vs remote)."""

# Sentinel: "no merge base recorded" (never synced this field with this
# provider). Distinct from None, which is a real value (e.g. unrated).
NO_BASE = object()

IN_SYNC = 'in_sync'
PULL = 'pull'        # only the remote changed
PUSH = 'push'        # only the local side changed
BOTH = 'both'        # diverged: both changed since the base


def emptyish(v):
    """True for a 'blank/unset/zero' value in any of the forms the
    providers hand back: None (a field a provider left null, e.g. Kitsu
    returning no reconsumeCount), 0/0.0/False, or an empty string/list/
    tuple. These all mean 'nothing here', so one provider's None and
    another's 0 (or '') are the SAME state, not a difference to sync."""
    return v is None or v == 0 or v in ('', [], ())


def eq(a, b):
    """Value equality, with two provider-friendly rules:
    - any two 'emptyish' values (None vs 0 vs '' vs [] ...) are equal,
      so a blank response never diffs against a zero/empty one;
    - lists (tags) compare order-insensitively."""
    if emptyish(a) and emptyish(b):
        return True
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    return a == b


def three_way(base, local, remote):
    """Classify one field for one (entity, provider) pair.

    With no recorded base (first sync against this provider) equal
    values are IN_SYNC and differing values are BOTH -- neither side is
    "the" changed one, so the ownership policy (or the user) decides;
    nothing silently overwrites.
    """
    if base is NO_BASE:
        return IN_SYNC if eq(local, remote) else BOTH
    local_changed = not eq(local, base)
    remote_changed = not eq(remote, base)
    if not local_changed and not remote_changed:
        return IN_SYNC
    if not local_changed:
        return PULL
    if not remote_changed:
        return PUSH
    if eq(local, remote):
        return IN_SYNC   # both changed to the same value: converged
    return BOTH
