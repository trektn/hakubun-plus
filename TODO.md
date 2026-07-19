# TODO

- Make library search faster and more robust (library scanning/matching in
  `hakubun/engine.py`).
- Harden the filename parser (`hakubun/parser/` — `animeinfoextractor.py` and
  the Anitopy wrapper); currently the weakest part.
- Undo stack: `set_episode` with auto-status-change records two undo entries
  (episode + status), so one undo only reverts the status and leaves
  progress at the final episode. Consider grouping both into one entry
  (`hakubun/engine.py`, `_record_undo`).
