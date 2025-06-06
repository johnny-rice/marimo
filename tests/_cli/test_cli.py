# Copyright 2024 Marimo. All rights reserved.
"""
Crude CLI tests

Requires frontend to be built
"""

from __future__ import annotations

import contextlib
import inspect
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import pytest

from marimo._ast import codegen
from marimo._ast.cell import CellConfig
from marimo._dependencies.dependencies import DependencyManager
from marimo._server.templates.templates import get_version
from marimo._utils.config.config import ROOT_DIR as CONFIG_ROOT_DIR
from marimo._utils.platform import is_windows
from marimo._utils.toml import read_toml

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

HAS_UV = DependencyManager.which("uv")

DIR_PATH = os.path.dirname(os.path.realpath(__file__))


def _is_win32() -> bool:
    return sys.platform == "win32"


def _can_access_pypi() -> bool:
    try:
        pypi_url = "https://pypi.org/pypi/marimo/json"
        with urllib.request.urlopen(pypi_url, timeout=5):
            return True
    except urllib.error.URLError:
        return False


@contextlib.contextmanager
def _patch_signals_win32() -> Iterator[None]:
    old_handler: Any = None
    try:
        if _is_win32():
            old_handler = signal.signal(signal.SIGINT, lambda *_: ...)
        yield
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)


def _interrupt(process: subprocess.Popen[Any]) -> None:
    if _is_win32():
        os.kill(process.pid, signal.CTRL_C_EVENT)
    else:
        os.kill(process.pid, signal.SIGINT)


def _confirm_shutdown(process: subprocess.Popen[Any]) -> None:
    if _is_win32():
        process.stdin.write(b"y\r\n")
    else:
        process.stdin.write(b"y\n")
    process.stdin.flush()


def _check_shutdown(
    process: subprocess.Popen[Any],
    check_fn: Optional[Callable[[int], bool]] = None,
) -> None:
    max_tries = 3
    tries = 0
    while process.poll() is None and tries < max_tries:
        time.sleep(1)
        tries += 1
    if check_fn is None:
        assert process.poll() == 0
    else:
        assert check_fn(process.poll())


def _try_fetch(
    port: int, host: str = "localhost", token: Optional[str] = None
) -> Optional[bytes]:
    for _ in range(20):
        try:
            url = f"http://{host}:{port}"
            if token is not None:
                url = f"{url}?access_token={token}"
            return urllib.request.urlopen(url).read()
        except Exception:
            time.sleep(0.6)
    print("Failed to fetch contents")
    return None


def _check_started(port: int, host: str = "localhost") -> Optional[bytes]:
    assert _try_fetch(port, host) is not None


def _temp_run_file(directory: tempfile.TemporaryDirectory[str]) -> str:
    filecontents = codegen.generate_filecontents(
        codes=["import marimo as mo"],
        names=["one"],
        cell_configs=[CellConfig()],
    )
    path = Path(directory.name) / "run.py"
    path.write_text(filecontents, encoding="utf-8")
    return str(path)


def _check_contents(
    p: subprocess.Popen[Any],  # type: ignore
    phrase: bytes,
    contents: Optional[bytes],
) -> None:
    try:
        assert contents is not None, contents
        assert phrase in contents, contents
    finally:
        p.kill()


def _get_port() -> int:
    port = 2718
    max_tries = 25
    for _ in range(max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            in_use = s.connect_ex(("localhost", port)) == 0
        if in_use:
            port += 1
        else:
            return port
    raise OSError("Could not find an unused port.")


def _read_toml(filepath: str) -> Optional[dict[str, Any]]:
    if not os.path.exists(filepath):
        return None
    return read_toml(filepath)


@pytest.fixture
def temp_marimo_file_with_inline_metadata() -> Generator[str, None, None]:
    tmp_dir = tempfile.TemporaryDirectory()
    tmp_file = os.path.join(tmp_dir.name, "notebook.py")
    content = inspect.cleandoc(
        """
        # /// script
        # requires-python = ">=3.11"
        # dependencies = ["polars"]
        # ///

        import marimo
        app = marimo.App()

        @app.cell
        def __():
            import marimo as mo
            return mo,

        @app.cell
        def __(mo):
            slider = mo.ui.slider(0, 10)
            return slider,

        if __name__ == "__main__":
            app.run()
        """
    )

    try:
        with open(tmp_file, "w") as f:
            f.write(content)
        yield tmp_file
    finally:
        tmp_dir.cleanup()


def test_cli_help_exit_code() -> None:
    # smoke test: makes sure CLI starts
    # helpful for catching issues related to
    p = subprocess.run(["marimo", "--help"])
    assert p.returncode == 0


def test_cli_edit_none() -> None:
    # smoke test: makes sure CLI starts and has basic things we expect
    # helpful for catching issues related to
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--skip-update-check",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "home"', contents)
    _check_contents(
        p,
        f'"version": "{get_version()}"'.encode(),
        contents,
    )
    _check_contents(p, b'"serverToken": ', contents)


def test_cli_edit_token() -> None:
    # smoke test: makes sure CLI starts and has basic things we expect
    # helpful for catching issues related to
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            "-p",
            str(port),
            "--headless",
            "--token-password",
            "secret",
            "--skip-update-check",
        ]
    )
    contents = _try_fetch(port, "localhost", "secret")
    _check_contents(p, b'"mode": "home"', contents)
    _check_contents(
        p,
        f'"version": "{get_version()}"'.encode(),
        contents,
    )
    _check_contents(p, b'"serverToken": ', contents)


def test_cli_edit_directory() -> None:
    d = tempfile.TemporaryDirectory()
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            d.name,
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--skip-update-check",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "home"', contents)
    _check_contents(
        p,
        f'"version": "{get_version()}"'.encode(),
        contents,
    )
    _check_contents(p, b'"serverToken": ', contents)


def test_cli_edit_new_file() -> None:
    d = tempfile.TemporaryDirectory()
    path = os.path.join(d.name, "new.py")
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            path,
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--skip-update-check",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "edit"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)
    _check_contents(p, b'"serverToken": ', contents)


def test_cli_edit_with_additional_args(temp_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            temp_marimo_file,
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--skip-update-check",
            "--",
            "-a=foo",
            "--b=bar",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "edit"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)


@pytest.mark.skipif(
    condition=not _can_access_pypi(),
    reason="update check won't work without access to pypi",
)
def test_cli_edit_update_check() -> None:
    with tempfile.TemporaryDirectory() as tempdir:
        port = _get_port()
        env = {**os.environ, "MARIMO_PYTEST_HOME_DIR": tempdir}
        # pop off MARIMO_SKIP_UPDATE_CHECK
        env.pop("MARIMO_SKIP_UPDATE_CHECK", None)
        p = subprocess.Popen(
            ["marimo", "edit", "-p", str(port), "--headless", "--no-token"],
            env=env,
        )
        contents = _try_fetch(port)
        _check_contents(p, b'"mode": "home"', contents)

        state_contents = _read_toml(
            os.path.join(tempdir, CONFIG_ROOT_DIR, "state.toml")
        )
        assert state_contents is not None
        assert state_contents.get("last_checked_at") is not None


@pytest.mark.skipif(
    condition=not _can_access_pypi(),
    reason="update check skip is only detectable if pypi is accessible",
)
def test_cli_edit_skip_update_check() -> None:
    with tempfile.TemporaryDirectory() as tempdir:
        port = _get_port()
        p = subprocess.Popen(
            [
                "marimo",
                "edit",
                "-p",
                str(port),
                "--headless",
                "--no-token",
                "--skip-update-check",
            ],
            env={**os.environ, "MARIMO_PYTEST_HOME_DIR": tempdir},
        )
        contents = _try_fetch(port)
        _check_contents(p, b'"mode": "home"', contents)

        state_contents = _read_toml(
            os.path.join(tempdir, CONFIG_ROOT_DIR, "state.toml")
        )
        assert (
            state_contents is None
            or state_contents.get("last_checked_at") is None
        )


def test_cli_new() -> None:
    port = _get_port()
    p = subprocess.Popen(
        ["marimo", "new", "-p", str(port), "--headless", "--no-token"]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "edit"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)
    _check_contents(p, b'"serverToken": ', contents)


def test_cli_run(temp_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        ["marimo", "run", temp_marimo_file, "-p", str(port), "--headless"]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "read"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)


def test_cli_run_with_show_code(temp_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "run",
            temp_marimo_file,
            "-p",
            str(port),
            "--headless",
            "--include-code",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "read"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)


def test_cli_run_with_additional_args(temp_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "run",
            temp_marimo_file,
            "-p",
            str(port),
            "--headless",
            "--",
            "-a=foo",
            "--b=bar",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "read"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)


def test_cli_tutorial() -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "tutorial",
            "intro",
            "-p",
            str(port),
            "--headless",
            "--no-token",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "edit"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)
    _check_contents(p, b"intro.py", contents)


def test_cli_md_tutorial() -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "tutorial",
            "markdown-format",
            "-p",
            str(port),
            "--headless",
            "--no-token",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "edit"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)
    _check_contents(p, b"markdown-format.md", contents)


def test_cli_md_run(temp_md_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        ["marimo", "run", temp_md_marimo_file, "-p", str(port), "--headless"]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "read"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)


def test_cli_md_edit(temp_md_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            temp_md_marimo_file,
            "-p",
            str(port),
            "--no-token",
            "--headless",
            "--skip-update-check",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "edit"', contents)
    _check_contents(p, f'"version": "{get_version()}"'.encode(), contents)


def test_cli_custom_host() -> None:
    port = _get_port()
    host = "localhost"
    p = subprocess.Popen(
        [
            "marimo",
            "tutorial",
            "intro",
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--host",
            host,
        ]
    )
    contents = _try_fetch(port, host)
    _check_contents(p, b'"mode": "edit"', contents)


@pytest.mark.skipif(not HAS_UV, reason="uv is required for sandbox tests")
def test_cli_sandbox_edit(temp_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            temp_marimo_file,
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--sandbox",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b"edit", contents)


@pytest.mark.skipif(not HAS_UV, reason="uv is required for sandbox tests")
def test_cli_sandbox_edit_no_prompt(temp_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            temp_marimo_file,
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--no-sandbox",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b"edit", contents)


@pytest.mark.skipif(not HAS_UV, reason="uv is required for sandbox tests")
def test_cli_sandbox_edit_new_file() -> None:
    port = _get_port()
    d = tempfile.TemporaryDirectory()
    path = os.path.join(d.name, "new_sandbox_file.py")
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            path,
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--sandbox",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b"edit", contents)


@pytest.mark.skipif(is_windows(), reason="Windows will prompt for Docker")
def test_cli_edit_by_url() -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            "https://github.com/marimo-team/marimo/blob/main/examples/ui/button.py",
            "-p",
            str(port),
            "--headless",
            "--no-token",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "edit"', contents)


@pytest.mark.skipif(not HAS_UV, reason="uv is required for sandbox tests")
def test_cli_sandbox_run(temp_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "run",
            temp_marimo_file,
            "-p",
            str(port),
            "--headless",
            "--sandbox",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b"read", contents)


@pytest.mark.skipif(not HAS_UV, reason="uv is required for sandbox tests")
def test_cli_sandbox_run_no_prompt(temp_marimo_file: str) -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "run",
            temp_marimo_file,
            "-p",
            str(port),
            "--headless",
            "--no-sandbox",
        ]
    )
    contents = _try_fetch(port)
    _check_contents(p, b'"mode": "read"', contents)


@pytest.mark.skipif(not HAS_UV, reason="uv is required for sandbox tests")
def test_cli_sandbox_run_with_python_version(
    temp_marimo_file_with_inline_metadata: str,
) -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "run",
            temp_marimo_file_with_inline_metadata,
            "-p",
            str(port),
            "--headless",
            "--sandbox",
        ],
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    contents = _try_fetch(port)

    # If fetch fails, capture and print server output for debugging
    if contents is None:
        stdout, stderr = p.communicate(timeout=5)
        raise AssertionError(
            f"Server failed to start. stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    _check_contents(p, b"read", contents)

    p.terminate()
    p.wait(timeout=5)


@pytest.mark.xfail(condition=_is_win32(), reason="flaky on Windows")
def test_cli_edit_interrupt_twice() -> None:
    # two SIGINTs should kill the CLI
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--skip-update-check",
        ]
    )
    _check_started(port)
    with _patch_signals_win32():
        _interrupt(p)
        assert p.poll() is None
        _interrupt(p)
        # exit code is system dependent when killed by signal
        _check_shutdown(p, check_fn=lambda code: code is not None)


@pytest.mark.xfail(condition=_is_win32(), reason="flaky on Windows")
def test_cli_run_interrupt_twice() -> None:
    # two SIGINTs should kill the CLI
    d = tempfile.TemporaryDirectory()
    path = _temp_run_file(d)
    port = _get_port()
    p = subprocess.Popen(
        ["marimo", "run", path, "-p", str(port), "--headless"]
    )
    _check_started(port)
    with _patch_signals_win32():
        _interrupt(p)
        assert p.poll() is None
        _interrupt(p)
        # exit code is system dependent when killed by signal
        _check_shutdown(p, check_fn=lambda code: code is not None)


@pytest.mark.xfail(condition=_is_win32(), reason="flaky on Windows")
def test_cli_edit_shutdown() -> None:
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            "-p",
            str(port),
            "--headless",
            "--no-token",
            "--skip-update-check",
        ],
        stdin=subprocess.PIPE,
    )
    _check_started(port)
    with _patch_signals_win32():
        _interrupt(p)
        assert p.poll() is None

        assert p.stdin is not None
        _confirm_shutdown(p)
        _check_shutdown(p)


@pytest.mark.xfail(condition=_is_win32(), reason="flaky on Windows")
def test_cli_run_shutdown() -> None:
    d = tempfile.TemporaryDirectory()
    path = _temp_run_file(d)
    port = _get_port()
    p = subprocess.Popen(
        ["marimo", "run", path, "-p", str(port), "--headless", "--no-token"],
        stdin=subprocess.PIPE,
    )
    _check_started(port)
    with _patch_signals_win32():
        _interrupt(p)
        assert p.poll() is None
        assert p.stdin is not None
        p.stdin.write(b"y\n")
        p.stdin.flush()
        time.sleep(3)
        assert p.poll() == 0


def test_cli_edit_sandbox_prompt() -> None:
    port = _get_port()
    path = os.path.join(DIR_PATH, "cli_data", "sandbox.py")
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            path,
            "--headless",
            "--no-token",
            "--skip-update-check",
            "-p",
            str(port),
        ],
        stdin=subprocess.PIPE,
    )
    assert p.poll() is None
    assert p.stdin is not None
    p.stdin.write(b"y\n")
    p.stdin.flush()
    _check_started(port)
    p.kill()


def test_cli_run_sandbox_prompt() -> None:
    port = _get_port()
    path = os.path.join(DIR_PATH, "cli_data", "sandbox.py")
    p = subprocess.Popen(
        [
            "marimo",
            "run",
            path,
            "--headless",
            "--no-token",
            "-p",
            str(port),
        ],
        stdin=subprocess.PIPE,
    )
    assert p.poll() is None
    assert p.stdin is not None
    p.stdin.write(b"y\n")
    p.stdin.flush()
    _check_started(port)
    p.kill()


def test_cli_edit_sandbox_prompt_yes() -> None:
    port = _get_port()
    path = os.path.join(DIR_PATH, "cli_data", "sandbox.py")
    p = subprocess.Popen(
        [
            "marimo",
            "-y",
            "edit",
            path,
            "--headless",
            "--no-token",
            "--skip-update-check",
            "-p",
            str(port),
        ],
    )
    assert p.poll() is None
    _check_started(port)
    p.kill()


def test_cli_run_sandbox_prompt_yes() -> None:
    port = _get_port()
    path = os.path.join(DIR_PATH, "cli_data", "sandbox.py")
    p = subprocess.Popen(
        [
            "marimo",
            "-y",
            "run",
            path,
            "--headless",
            "--no-token",
            "-p",
            str(port),
        ],
    )
    assert p.poll() is None
    _check_started(port)
    p.kill()


@pytest.mark.skipif(not HAS_UV, reason="uv is required for sandbox tests")
def test_cli_with_custom_pyproject_config(tmp_path: Path) -> None:
    # Create a custom pyproject.toml with special marimo config
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_content = """
    [tool.marimo]
    formatting = {line_length = 111}

    [tool.marimo.runtime]
    auto_instantiate = false

    [tool.marimo.package_management]
    manager = "pip"
    """
    pyproject_path.write_text(pyproject_content)

    marimo_file = tmp_path / "tmp.py"

    # marimo edit <marimo_file> --sandbox
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            str(marimo_file),
            "--sandbox",
            "-p",
            str(port),
            "--headless",
            "--no-token",
        ],
    )

    def assert_custom_config(contents: bytes | None) -> None:
        assert contents is not None
        # Verify that the custom config is applied
        assert b'"line_length": 111' in contents
        assert b'"auto_instantiate": false' in contents
        # Verify that the package manager is switch to uv because we are running in a sandbox
        # TODO: fix this, it does not get overridden in tests (maybe it is using a different marimo version that the one in CI)
        # assert b'"manager": "uv"' in contents

    try:
        contents = _try_fetch(port)
        assert_custom_config(contents)
    finally:
        p.kill()

    # marimo edit --sandbox, in the directory with pyproject.toml
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "edit",
            "--sandbox",
            "-p",
            str(port),
            "--headless",
            "--no-token",
        ],
        cwd=tmp_path,
    )

    try:
        contents = _try_fetch(port)
        assert_custom_config(contents)
    finally:
        p.kill()

    # marimo new --sandbox, in the directory with pyproject.toml
    port = _get_port()
    p = subprocess.Popen(
        [
            "marimo",
            "new",
            "--sandbox",
            "-p",
            str(port),
            "--headless",
            "--no-token",
        ],
        cwd=tmp_path,
    )

    try:
        contents = _try_fetch(port)
        assert_custom_config(contents)
    finally:
        p.kill()


# shell-completion has 1 input (value of $SHELL) & 3 outputs (return code, stdout, & stderr)
# parameterize to give coverage. We use a boolean to specify if output on that stream should be present.
@pytest.mark.parametrize(
    "shell,rc,expect_stdout,expect_stderr".split(","),
    [
        # valid shell values, rc of 0, data only on stdout
        ("bash", 0, True, False),
        ("bash.exe", 0, True, False),
        ("/usr/bin/zsh", 0, True, False),
        pytest.param(
            r"c:\spam\eggs\fish.exe",
            0,
            True,
            False,
            marks=pytest.mark.skipif(
                not sys.platform.startswith(("win32", "cygwin")),
                reason="win32 only",
            ),
        ),
        # invalid shell values, rc of 0, data only on stderr
        ("bogus", 2, False, True),
        ("", 2, False, True),  # usage error displayed
    ],
)
def test_shell_completion(
    shell: str, rc: int, expect_stdout: bool, expect_stderr: bool
) -> None:
    test_env = os.environ.copy()
    test_env["SHELL"] = shell
    p = subprocess.run(
        ["marimo", "shell-completion"],
        capture_output=True,
        env=test_env,
    )
    assert p.returncode == rc
    assert bool(len(p.stdout)) == expect_stdout
    assert bool(len(p.stderr)) == expect_stderr


HAS_DOCKER = DependencyManager.which("docker")


@pytest.mark.skipif(
    HAS_DOCKER, reason="docker is required to be not installed"
)
def test_cli_run_docker_remote_url():
    remote_url = "https://example.com/notebook.py"
    p = subprocess.Popen(
        [
            "marimo",
            "-y",
            "edit",
            remote_url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Should fail with missing docker
    assert p.returncode != 0
    assert p.stdout is not None
    assert "Docker is not installed" in p.stdout.read().decode()
