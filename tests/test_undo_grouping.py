from collections import deque

from hakubun.engine import Engine
from hakubun.messenger import Messenger


def _bare_engine():
    """An Engine with just the undo/redo machinery initialized, bypassing
    the real __init__ (which needs accounts/API/data-handler setup)."""
    e = Engine.__new__(Engine)
    e.msg = Messenger(None, 'Test')
    e._undo_stack = deque(maxlen=Engine.UNDO_LIMIT)
    e._redo_stack = deque(maxlen=Engine.UNDO_LIMIT)
    e._replaying_undo = False
    e._undo_group = None
    e.applied = {}
    return e


def _install_setter(e, name):
    """Attach a fake set_<field> method that records its calls in
    e.applied and goes through the same _record_undo() path the real
    engine setters use."""
    def setter(showid, value):
        old_value = e.applied.setdefault(showid, {}).get(name)
        e.applied[showid][name] = value
        e._record_undo(
            'set_%s' % name, showid, 'Show', old_value, value,
            "%s change (-> %s)" % (name, value))
    setattr(e, 'set_%s' % name, setter)
    return setter


def test_ungrouped_actions_undo_one_at_a_time():
    e = _bare_engine()
    _install_setter(e, 'a')
    e.set_a(1, 'x')
    e.set_a(1, 'y')
    assert len(e._undo_stack) == 2


def test_grouped_actions_form_a_single_undo_entry():
    e = _bare_engine()
    _install_setter(e, 'episode')
    _install_setter(e, 'status')

    with e._grouped_undo():
        e.set_episode(1, 12)
        e.set_status(1, 'finished')

    assert len(e._undo_stack) == 1


def test_undo_of_a_grouped_entry_reverts_all_fields_in_one_call():
    e = _bare_engine()
    _install_setter(e, 'episode')
    _install_setter(e, 'status')
    e.applied[1] = {'episode': 5, 'status': 'watching'}

    with e._grouped_undo():
        e.set_episode(1, 12)
        e.set_status(1, 'finished')

    assert e.applied[1] == {'episode': 12, 'status': 'finished'}

    description = e.undo()
    assert e.applied[1] == {'episode': 5, 'status': 'watching'}
    assert 'episode' in description
    assert len(e._undo_stack) == 0
    assert len(e._redo_stack) == 1


def test_redo_of_a_grouped_entry_reapplies_all_fields():
    e = _bare_engine()
    _install_setter(e, 'episode')
    _install_setter(e, 'status')
    e.applied[1] = {'episode': 5, 'status': 'watching'}

    with e._grouped_undo():
        e.set_episode(1, 12)
        e.set_status(1, 'finished')

    e.undo()
    description = e.redo()
    assert e.applied[1] == {'episode': 12, 'status': 'finished'}
    assert 'episode' in description
    assert len(e._undo_stack) == 1
    assert len(e._redo_stack) == 0


def test_nested_grouped_undo_joins_the_outer_group():
    e = _bare_engine()
    _install_setter(e, 'a')
    _install_setter(e, 'b')

    with e._grouped_undo():
        e.set_a(1, 'x')
        with e._grouped_undo():
            e.set_b(1, 'y')

    assert len(e._undo_stack) == 1


def test_empty_group_pushes_nothing():
    e = _bare_engine()
    with e._grouped_undo():
        pass
    assert len(e._undo_stack) == 0
