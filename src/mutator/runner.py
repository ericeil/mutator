"""Run a project's tests, and decide the command for each language."""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

Command = str | list[str]


@dataclass(frozen=True)
class CommandResult:
    code: int
    timed_out: bool
    seconds: float
    output: str


def _worker_home(cwd: Path) -> Path | None:
    """The overlay root when cwd is inside target/mutation-workers."""

    for candidate in [cwd, *cwd.parents]:
        parent = candidate.parent
        if not candidate.name.startswith("worker-"):
            continue
        if not parent.name.startswith("run-"):
            continue
        if parent.parent.name == "mutation-workers":
            return candidate
    return None


def _source_entries(cwd: Path, worker: Path) -> list[str]:
    found = []
    for directory in (cwd / "src", cwd, worker / "src", worker):
        text = str(directory)
        if text in found:
            continue
        if directory.is_dir():
            found.append(text)
    return found


def _prefer_worker_sources(cwd: Path, environment: dict) -> None:
    """An editable install would otherwise import the unmutated tree."""

    worker = _worker_home(cwd)
    if worker is None:
        return
    entries = _source_entries(cwd, worker)
    current = environment.get("PYTHONPATH")
    if current:
        entries.append(current)
    if not entries:
        return
    environment["PYTHONPATH"] = os.pathsep.join(entries)


def display_command(command: Command) -> str:
    """A shell-looking rendering. A list is quoted so it can be pasted."""

    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


class CommandRunner:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def run(self, command: Command, cwd: Path, timeout: float | None) -> CommandResult:
        """Run `command` in `cwd`.

        A list is an argument vector and does not go through a shell, so a
        path with spaces or metacharacters stays one argument. A string is a
        command the user typed in `--test-command` or `--coverage-command`.
        """

        if self.verbose:
            print(f"+ ({cwd}) {display_command(command)}", file=sys.stderr)
        started = time.monotonic()
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        _prefer_worker_sources(cwd, environment)
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                shell=isinstance(command, str),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
                env=environment,
            )
        except OSError as exc:
            seconds = time.monotonic() - started
            return CommandResult(code=127, timed_out=False, seconds=seconds, output=str(exc))
        try:
            output, _err = process.communicate(timeout=timeout)
            code = process.returncode if process.returncode is not None else 1
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _err = process.communicate()
            code = 124
            timed_out = True
        seconds = time.monotonic() - started
        return CommandResult(code=code, timed_out=timed_out, seconds=seconds, output=output or "")


def nearest(start: Path, marker: str, stop: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    stop = stop.resolve()
    current = current.resolve()
    while True:
        if (current / marker).is_file():
            return current
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _clojure_command(directory: Path) -> list[str]:
    deps = directory / "deps.edn"
    bb = directory / "bb.edn"
    if bb.is_file() and not deps.is_file():
        text = _read(bb)
        if "spec" in text:
            return ["bb", "spec", "--tag", "~no-mutate"]
        return ["bb", "test"]
    text = _read(deps)
    if ":spec" in text and "speclj" in text:
        return ["clj", "-M:spec", "--tag", "~no-mutate"]
    if ":spec" in text:
        return ["clj", "-M:spec"]
    return ["clj", "-M:test"]


def _project_interpreter(directory: Path) -> str:
    """The project's virtualenv, or this process when the project has none.

    The path stays absolute and the symlink is left in place. A worker does
    not link ``.venv``, and resolving ``bin/python`` would leave the virtualenv.
    """

    for name in (".venv", "venv"):
        candidate = (directory / name / "bin" / "python").absolute()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return sys.executable


def _python_command(directory: Path) -> list[str]:
    interpreter = _project_interpreter(directory)
    pyproject = _read(directory / "pyproject.toml")
    if (
        (directory / "pytest.ini").is_file()
        or (directory / "conftest.py").is_file()
        or "pytest" in pyproject
        or (directory / "setup.cfg").is_file() and "pytest" in _read(directory / "setup.cfg")
    ):
        return [interpreter, "-m", "pytest"]
    return [interpreter, "-m", "unittest", "discover"]


def _nearest_marker(source: Path, root: Path, markers: tuple[str, ...]) -> Path:
    for marker in markers:
        found = nearest(source, marker, root)
        if found is not None:
            return found
    return root


def _go_package(directory: Path, source: Path) -> str:
    relative = source.resolve().parent.relative_to(directory.resolve())
    if relative == Path("."):
        return "."
    return "./" + relative.as_posix()


def _clojure_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("deps.edn", "bb.edn"))
    return _clojure_command(directory), directory


def _java_plan(
    root: Path, source: Path, maven_args: tuple[str, ...] | list[str] = ()
) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("pom.xml",))
    return ["mvn", "-q", "test", "-DexcludeTags=no-mutate", *maven_args], directory


_GRADLE_BUILD_FILES = ("build.gradle", "build.gradle.kts")
_GRADLE_SETTINGS_FILES = ("settings.gradle", "settings.gradle.kts")


def nearest_any(start: Path, markers: tuple[str, ...], stop: Path) -> Path | None:
    """The nearest directory at or above `start` that contains one of `markers`."""

    current = start if start.is_dir() else start.parent
    stop = stop.resolve()
    current = current.resolve()
    while True:
        if any((current / marker).is_file() for marker in markers):
            return current
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _gradle_program(module: Path, root: Path) -> str:
    """A wrapper path relative to `module`, so a worker runs its own copy.

    An absolute path would point at the real tree. Gradle then resolves the
    script's directory and compiles the unmutated sources. `gradlew` is copied
    into the worker, and this relative path finds that copy.
    """

    wrapper = nearest(module, "gradlew", root)
    if wrapper is None:
        return "gradle"
    relative = Path(os.path.relpath(wrapper / "gradlew", module)).as_posix()
    # `gradlew` with no slash is looked up on PATH. The wrapper in this
    # directory has to be `./gradlew` so the worker's copy is the one that runs.
    if "/" not in relative:
        return "./" + relative
    return relative


def _kotlin_tool(root: Path, source: Path) -> tuple[str, Path]:
    """Gradle when its build file is strictly nearer than pom.xml.

    A directory that has both is a Maven module, matching crapper.
    """

    gradle = nearest_any(source, _GRADLE_BUILD_FILES, root)
    maven = nearest(source, "pom.xml", root)
    if gradle is not None and (maven is None or len(gradle.parts) > len(maven.parts)):
        return "gradle", gradle
    if maven is not None:
        return "maven", maven
    if gradle is not None:
        return "gradle", gradle
    return "gradle", root


def _settings_dir(start: Path, stop: Path) -> Path | None:
    """The nearest Gradle settings directory at or above `start`, not above `stop`."""

    current = start if start.is_dir() else start.parent
    current = current.resolve()
    stop = stop.resolve()
    while True:
        if any((current / name).is_file() for name in _GRADLE_SETTINGS_FILES):
            return current
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _settings_text(settings: Path) -> str:
    for name in _GRADLE_SETTINGS_FILES:
        path = settings / name
        if path.is_file():
            return _read(path)
    return ""


def _strip_gradle_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)//.*?$", "", text)


def _include_body(text: str) -> str:
    """Arguments of one `include` call, either `(...)` or the rest of the line."""

    stripped = text.lstrip()
    if not stripped.startswith("("):
        return stripped.split("\n", 1)[0]
    depth = 0
    for index, char in enumerate(stripped):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return stripped[1:index]
    return stripped


def _included_projects(text: str) -> list[str]:
    """Project paths named by `include` in a settings script.

    `includeBuild` is a different call and is ignored. Comments are ignored.
    """

    cleaned = _strip_gradle_comments(text)
    names: list[str] = []
    for match in re.finditer(r"\binclude\b", cleaned):
        body = _include_body(cleaned[match.end() :])
        names.extend(re.findall(r"""["']([^"']+)["']""", body))
    return names


def _project_dirs(settings: Path) -> list[Path]:
    """The root project and each included project, using Gradle's default directory."""

    settings = settings.resolve()
    directories = [settings]
    for name in _included_projects(_settings_text(settings)):
        parts = [part for part in name.split(":") if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts):
            continue
        candidate = (settings / Path(*parts)).resolve()
        try:
            candidate.relative_to(settings)
        except ValueError:
            continue
        if candidate not in directories:
            directories.append(candidate)
    return directories


def _owning_project(source: Path, projects: list[Path]) -> Path | None:
    """The longest project directory that contains `source`."""

    current = source if source.is_dir() else source.parent
    current = current.resolve()
    best: Path | None = None
    best_len = -1
    for project in projects:
        try:
            current.relative_to(project)
        except ValueError:
            continue
        if len(project.parts) > best_len:
            best = project
            best_len = len(project.parts)
    return best


def _gradle_test_task(source: Path, module: Path, root: Path) -> str:
    """The test task of the Gradle project that contains `source`.

    Unqualified `test` runs in the current project and every project under it.
    From the root of a multi-project build that is every module, and a
    `--tests` filter then fails a module with no match ("No tests found for
    given includes"). The task path names only the project that contains the
    file. A single-project build stays `test`. A directory that has a build
    file but is not included stays `test` too.
    """

    settings = _settings_dir(source, root)
    if settings is None:
        return "test"
    settings = settings.resolve()
    projects = _project_dirs(settings)
    owning = _owning_project(source, projects)
    if owning is None:
        return "test"
    module = module.resolve()
    # A build file that the settings script does not include is not that
    # project. Keep the unqualified task, which is what this directory ran before.
    if module != owning and module != settings:
        return "test"
    if owning == settings:
        if len(projects) > 1:
            return ":test"
        return "test"
    relative = owning.relative_to(settings)
    return ":" + ":".join(relative.parts) + ":test"


def _kotlin_plan(
    root: Path,
    source: Path,
    gradle_args: tuple[str, ...] | list[str] = (),
    maven_args: tuple[str, ...] | list[str] = (),
) -> tuple[Command, Path]:
    tool, directory = _kotlin_tool(root, source)
    if tool == "maven":
        return ["mvn", "-q", "test", "-DexcludeTags=no-mutate", *maven_args], directory
    # Args sit after the task, so `--tests` is a task option. `-D` and `-P`
    # apply from either position.
    task = _gradle_test_task(source, directory, root)
    return [_gradle_program(directory, root), task, *gradle_args], directory


def _go_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("go.mod",))
    return ["go", "test", "-count=1", _go_package(directory, source)], directory


def _typescript_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("package.json",))
    return ["npm", "test"], directory


def _rust_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("Cargo.toml",))
    return ["cargo", "test"], directory


def _python_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("pyproject.toml", "pytest.ini", "setup.cfg"))
    return _python_command(directory), directory


_PLANS = {
    "clojure": _clojure_plan,
    "go": _go_plan,
    "typescript": _typescript_plan,
    "rust": _rust_plan,
    "python": _python_plan,
}


def test_plan(
    root: Path,
    source: Path,
    language: str,
    override: str | None,
    gradle_args: tuple[str, ...] | list[str] = (),
    maven_args: tuple[str, ...] | list[str] = (),
) -> tuple[Command, Path]:
    """The test command and the directory it runs in.

    A user override stays a shell string. Every generated command is an
    argument vector, so a package path is one argument. Gradle and Maven
    arguments are added for Kotlin, and Maven arguments for Java. They are
    not added to an override.
    """

    root = root.resolve()
    if override:
        return override, root
    if language == "kotlin":
        return _kotlin_plan(root, source, gradle_args, maven_args)
    if language == "java":
        return _java_plan(root, source, maven_args)
    plan = _PLANS.get(language)
    if plan is None:
        return ["false"], root
    return plan(root, source)
