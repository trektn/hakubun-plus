import json
import os

import pytest

from hakubun import utils


def test_save_config_writes_and_reads_back(tmp_path):
    f = str(tmp_path / "config.json")
    utils.save_config({"a": 1}, f)
    assert json.load(open(f)) == {"a": 1}


def test_save_data_writes_and_reads_back(tmp_path):
    f = str(tmp_path / "data.pickle")
    utils.save_data({"a": 1}, f)
    assert utils.load_data(f) == {"a": 1}


def test_save_config_failure_does_not_corrupt_existing_file(tmp_path, monkeypatch):
    f = str(tmp_path / "config.json")
    utils.save_config({"a": 1}, f)

    def boom(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(json, "dumps", boom)
    with pytest.raises(ValueError):
        utils.save_config({"a": 2}, f)

    assert json.load(open(f)) == {"a": 1}
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".tmp-")]


def test_save_data_failure_does_not_corrupt_existing_file(tmp_path, monkeypatch):
    f = str(tmp_path / "data.pickle")
    utils.save_data({"a": 1}, f)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        utils.save_data({"a": 2}, f)

    assert utils.load_data(f) == {"a": 1}
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".tmp-")]
