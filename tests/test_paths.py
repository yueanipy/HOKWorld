import paths


def test_user_data_dir_is_localappdata_hokworldscript():
    d = paths.user_data_dir()
    assert d.name == "HOKWorldScript"
    assert d.exists()


def test_user_data_subdirs_created():
    for fn in (paths.logs_dir, paths.cache_dir, paths.updates_dir,
               paths.sessions_dir, paths.screenshots_dir):
        p = fn()
        assert p.exists() and p.is_dir()
        # 用户数据必须在用户数据根下,绝不在安装目录里
        assert paths.user_data_dir() in p.parents or p == paths.user_data_dir()


def test_resource_path_resolves_bundled_assets():
    # 源码模式:资源根=项目目录,模板与图标可定位(冻结后走 sys._MEIPASS)
    assert paths.resource_path("fishing", "templates", "raw").exists()
    assert paths.resource_path("assets", "app.ico").exists()


def test_is_dev_true_when_not_frozen():
    assert paths.is_dev() is True
    assert paths.is_frozen() is False


def test_config_path_in_user_data():
    cp = paths.config_path()
    assert cp.name == "config.json"
    assert cp.parent == paths.user_data_dir()
