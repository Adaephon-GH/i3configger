from pathlib import Path

import pytest

from i3configger import build, ipc, paths

HERE = Path(__file__).parent
EXAMPLES = HERE.parent / 'examples'
REFERENCE = HERE / 'examples'
FAKE_HEADER = """\
###############################################################################
# Built by i3configger /from/some/directory/who/cares  (some time after 1972) #
###############################################################################
"""
TEST_FOLDER_NAMES = list(
    [d.name for d in EXAMPLES.iterdir()
     if d.is_dir() and not str(d.name).startswith('_')])


@pytest.mark.parametrize("container", TEST_FOLDER_NAMES)
def test_no_build(container, monkeypatch):
    ipc.Notify.set_notify_command(True)
    monkeypatch.setattr(
        paths, 'get_i3_config_path', lambda: EXAMPLES / container)
    configPath = paths.get_my_config_path()
    assert configPath.exists() and configPath.is_file()
    builder = build.Builder(configPath)
    monkeypatch.setattr(builder, 'make_header', lambda: FAKE_HEADER)
    builder.build()
    buildPath = configPath.parents[1]
    referencePath = REFERENCE / container
    for path in referencePath.iterdir():
        resultFilePath = buildPath / path.name
        referenceFilePath = (REFERENCE / path.name)
        assert resultFilePath != referenceFilePath
        result = resultFilePath.read_text()
        reference = resultFilePath.read_text()
        assert result == reference
