"""pytest 公共夹具：让场景接口使用临时数据库，互不污染工作区。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _isolated_scene_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_scenes.db")
    monkeypatch.setenv("SCENES_DB_PATH", db_path)

    # storage 在导入时读取环境变量作为默认值，main 持有惰性单例，
    # 两者都需要重置才能让新路径生效。
    from app import storage as storage_module
    from app import main as main_module

    monkeypatch.setattr(storage_module, "DEFAULT_DB_PATH", db_path)
    main_module.reset_store()
    main_module.reset_playback()
    yield
    main_module.reset_store()
    main_module.reset_playback()
