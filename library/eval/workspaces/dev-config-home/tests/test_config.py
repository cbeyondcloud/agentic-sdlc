from pathlib import PurePosixPath, PureWindowsPath

from config import config_dir


def test_linux_uses_xdg_or_home():
    assert config_dir({"HOME": "/home/a"}, "linux") == PurePosixPath("/home/a/.config/acme")
    env = {"HOME": "/home/a", "XDG_CONFIG_HOME": "/cfg"}
    assert config_dir(env, "linux") == PurePosixPath("/cfg/acme")


def test_windows_uses_appdata_without_home():
    env = {"APPDATA": "C:\\Users\\a\\AppData\\Roaming", "USERPROFILE": "C:\\Users\\a"}
    assert config_dir(env, "win32") == PureWindowsPath("C:/Users/a/AppData/Roaming/acme")
    assert config_dir({"USERPROFILE": "C:\\Users\\b"}, "win32") == PureWindowsPath(
        "C:/Users/b/AppData/Roaming/acme"
    )


def test_macos_uses_application_support():
    assert config_dir({"HOME": "/Users/a"}, "darwin") == PurePosixPath(
        "/Users/a/Library/Application Support/acme"
    )
