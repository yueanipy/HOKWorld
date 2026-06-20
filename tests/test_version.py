import pytest

import version


def test_version_is_semver():
    parts = version.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts), version.__version__


def test_version_tuple_parsing():
    assert version.version_tuple("0.0.1") == (0, 0, 1)
    assert version.version_tuple("v0.0.1") == (0, 0, 1)
    assert version.version_tuple("1.2.3") == (1, 2, 3)
    assert version.version_tuple("2.0") == (2, 0, 0)        # 补零到三段
    assert version.version_tuple("0.0.1") < version.version_tuple("0.0.2")


def test_gui_version_single_source():
    """GUI 显示的版本必须取自 version.__version__(统一来源,不得各自维护)。"""
    pytest.importorskip("PySide6")
    pytest.importorskip("win32gui")
    import app
    assert app.APP_VERSION == f"v{version.__version__}"
