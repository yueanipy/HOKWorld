import config


def test_defaults_are_safe(tmp_path):
    c = config.Config(path=tmp_path / "config.json")
    assert c.get("dry_run") is True            # 演练默认开启
    assert c.get("real_input") is False        # 真实输入默认关闭
    assert c.get("timing_jitter") is False     # 时序抖动默认关闭
    assert c.get("check_update_on_startup") is True
    assert c.inputs_armed() is False           # 默认不发送任何键鼠


def test_armed_requires_real_input_and_not_dry_run(tmp_path):
    c = config.Config(path=tmp_path / "config.json")
    c.set("real_input", True, save=False)
    c.set("dry_run", False)
    assert c.inputs_armed() is True
    # 演练优先:即便真实输入开,演练打开也不发送
    c.set("dry_run", True)
    assert c.inputs_armed() is False


def test_roundtrip_persists(tmp_path):
    p = tmp_path / "config.json"
    c = config.Config(path=p)
    c.set("skip_version", "9.9.9")
    c.set("timing_jitter", True)
    again = config.Config(path=p)
    assert again.get("skip_version") == "9.9.9"
    assert again.get("timing_jitter") is True


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ not json", encoding="utf-8")
    c = config.Config(path=p)
    assert c.get("dry_run") is True            # 坏文件不崩,回退默认
