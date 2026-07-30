"""Divergence resolution via field-ownership policies.

Policies decide *divergence* (both sides changed since the merge base).
Single-sided changes propagate normally whatever the owner is -- like
git, ownership answers "who wins a merge conflict", not "who may
commit". `individual` is the exception: the field never syncs at all.

Decisions returned by resolve():
  ('local', value)   local side wins -> push value to the provider
  ('remote', value)  remote side wins -> pull value into local state
  ('conflict', None) the user decides
  (None, None)       nothing to do
"""

from hakubun.sync.diff import eq
from hakubun.sync.models import PolicyKind, SET_FIELDS


def resolve(policy, field, local, remote, provider,
            local_ts=0, remote_ts=0):
    """Resolve a diverged field between local and one provider."""
    kind = policy.kind
    if kind is PolicyKind.INDIVIDUAL:
        return (None, None)
    if kind is PolicyKind.LOCAL:
        return ('local', local)
    if kind is PolicyKind.PROVIDER:
        if policy.provider == provider:
            return ('remote', remote)
        # The owner is some other provider; the owner's value reaches
        # this provider as a push once it lands in local state. Against
        # a non-owner, the local (owner-fed) value wins.
        return ('local', local)
    if kind is PolicyKind.MERGE:
        if field in SET_FIELDS or (isinstance(local, list)
                                   and isinstance(remote, list)):
            union = sorted({*map(str, local or []), *map(str, remote or [])})
            if eq(union, local):
                return ('local', local)
            return ('merged', union)
        # Scalars: newest known change wins; a tie is a real conflict.
        if local_ts > remote_ts:
            return ('local', local)
        if remote_ts > local_ts:
            return ('remote', remote)
        return ('conflict', None)
    if kind is PolicyKind.ASK:
        return ('conflict', None)
    raise ValueError('unhandled policy: %s' % policy)
