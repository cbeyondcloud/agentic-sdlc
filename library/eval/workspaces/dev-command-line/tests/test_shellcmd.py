from shellcmd import command_line


def test_simple_arguments_are_unchanged():
    assert command_line(["git", "status"], windows=False) == "git status"
    assert command_line(["git", "status"], windows=True) == "git status"


def test_posix_quoting():
    assert command_line(["cp", "my file.txt", "it's"], windows=False) == "cp 'my file.txt' 'it'\"'\"'s'"


def test_windows_quoting():
    assert command_line(["copy", "my file.txt", 'say "hi"'], windows=True) == 'copy "my file.txt" "say \\"hi\\""'
