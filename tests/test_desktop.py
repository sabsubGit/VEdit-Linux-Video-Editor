"""The launcher entry, the icons and the `.vedit` file type."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from vedit import desktop


@pytest.fixture
def root(tmp_path):
    return tmp_path / "share"


class TestTheIcon:
    def test_it_ships_inside_the_package_and_renders(self, qt_app):
        from PySide6.QtSvg import QSvgRenderer

        assert desktop.icon_file().is_file()
        assert QSvgRenderer(str(desktop.icon_file())).isValid()

    def test_it_uses_nothing_a_small_svg_renderer_would_skip(self):
        """Clip paths and filters are where launcher renderers disagree; the
        first draft used one and Qt drew the clips spilling out of the tile."""
        text = desktop.icon_file().read_text()
        for unsupported in ("clipPath", "clip-path", "<filter", "<mask", "<image"):
            assert unsupported not in text


class TestInstall:
    def test_it_writes_the_entry_the_file_type_and_every_icon_size(self, qt_app, root):
        written = desktop.install(root, python="/opt/venv/bin/python")
        paths = desktop.targets(root)
        assert set(written) == set(paths.values())
        for path in paths.values():
            assert path.is_file() and path.stat().st_size > 0

        from PySide6.QtGui import QImage

        for size in desktop.PNG_SIZES:
            image = QImage(str(paths[f"png{size}"]))
            assert (image.width(), image.height()) == (size, size)
            assert image.pixelColor(0, 0).alpha() == 0, "the corners stay transparent"
            assert image.pixelColor(size // 2, size // 2).alpha() == 255

    def test_the_three_names_that_have_to_agree_do(self, qt_app, root):
        desktop.install(root, python="/opt/venv/bin/python")
        paths = desktop.targets(root)
        entry = paths["desktop"].read_text()
        assert paths["desktop"].name == f"{desktop.APP_ID}.desktop"
        assert f"Icon={desktop.APP_ID}" in entry
        assert f"StartupWMClass={desktop.APP_ID}" in entry
        assert paths["svg"].stem == desktop.APP_ID

    def test_the_entry_runs_the_python_that_installed_it_and_takes_a_file(self, root):
        entry = desktop.desktop_entry("/home/some one/.venv/bin/python")
        assert 'Exec="/home/some one/.venv/bin/python" -m vedit %f' in entry
        assert f"MimeType={desktop.MIME_TYPE};" in entry

    def test_awkward_paths_are_escaped(self):
        entry = desktop.desktop_entry('/odd/$HOME/100%/py"thon')
        assert r'Exec="/odd/\$HOME/100%%/py\"thon" -m vedit %f' in entry

    @pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="no validator")
    def test_the_entry_passes_the_freedesktop_validator(self, qt_app, root):
        desktop.install(root, python="/opt/venv/bin/python")
        result = subprocess.run(
            ["desktop-file-validate", str(desktop.targets(root)["desktop"])],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_project_files_are_given_a_type(self, qt_app, root):
        desktop.install(root)
        definition = desktop.targets(root)["mime"].read_text()
        assert desktop.MIME_TYPE in definition and '*.vedit' in definition


class TestUninstall:
    def test_it_removes_exactly_what_was_written(self, qt_app, root):
        other = root / "applications" / "someone-else.desktop"
        other.parent.mkdir(parents=True)
        other.write_text("[Desktop Entry]\n")

        written = desktop.install(root)
        removed = desktop.uninstall(root)
        assert set(removed) == set(written)
        assert not any(path.exists() for path in desktop.targets(root).values())
        assert other.exists(), "nobody else's entry is touched"
        assert desktop.uninstall(root) == []


class TestTheCommand:
    def test_ordinary_arguments_start_the_editor(self):
        assert desktop.run_command([]) is None
        assert desktop.run_command(["/some/project.vedit"]) is None

    def test_install_and_uninstall_exit_instead(self, qt_app, root, monkeypatch, capsys):
        monkeypatch.setenv("XDG_DATA_HOME", str(root))
        assert desktop.run_command(["--install-desktop"]) == 0
        assert desktop.targets(root)["desktop"].is_file()
        assert "application launcher" in capsys.readouterr().out
        assert desktop.run_command(["--uninstall-desktop"]) == 0
        assert not desktop.targets(root)["desktop"].exists()
