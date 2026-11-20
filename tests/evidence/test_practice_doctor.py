from pathlib import Path

import pytest

from scripts import practice_setup as doctor


def test_inspect_missing_environment_leaves_folder_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "ENV", tmp_path / "missing")
    assert not doctor.check_environment("uv", repair=False)
    assert not list(tmp_path.iterdir())


def test_repair_preserves_unrecognized_folder(tmp_path, monkeypatch):
    env = tmp_path / "practice"
    env.mkdir()
    sentinel = env / "keep.txt"
    sentinel.write_text("existing files")
    monkeypatch.setattr(doctor, "ENV", env)
    with pytest.raises(RuntimeError, match="Unrecognized folder"):
        doctor.check_environment("uv", repair=True)
    assert sentinel.read_text() == "existing files"


def test_repair_rejects_linked_environment(tmp_path, monkeypatch):
    target = tmp_path / "existing"
    target.mkdir()
    env = tmp_path / "practice"
    try:
        env.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Host prevents creating test directory symlinks")
    monkeypatch.setattr(doctor, "ENV", env)
    with pytest.raises(RuntimeError, match="points to another folder"):
        doctor.check_environment("uv", repair=True)
    assert not list(target.iterdir())


def test_missing_release_names_the_file_before_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "ROOT", Path(tmp_path))
    with pytest.raises(RuntimeError, match=r"Missing release files: .*uv\.lock"):
        doctor.host_checks()
    assert not list(tmp_path.iterdir())
