import os
import stat
from pathlib import Path

from mutator.cli import _build_args, parse_args, run
from mutator.coverage import covered_lines
from mutator.functions import sites_in_file
from mutator.kotlin_source import blank_context_receivers, prepare_kotlin
from mutator.runner import test_plan as plan_command
from mutator.sites import apply_site
from mutator.workers import create_workers, delete_tree, new_run_dir


def _sites(tmp_path: Path, relative: str, source: str):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return sites_in_file(source, path, tmp_path, relative)


BOARD = """package demo

class Board {
    fun place(x: Int, ready: Boolean, name: String?, items: List<Int>): Int {
        if (x > 0 && ready || x == 1 && x != 2) {
            val n = name?.length ?: 0
            val forced = name!!
            if (name === "a" && name !== "b") return x + 1
            if (x in items && name !in items) return -x
            if (name is String && name !is CharSequence) return x * 2 / 1
            var i = 0
            while (i < items.size) { i++ }
            return if (!ready) 1 else 0
        }
        return 0
    }

    private fun hide() = false
}

fun top(x: Int) = x >= 0
"""


def _pairs(sites):
    return {(site.original, site.mutant) for site in sites}


def test_kotlin_operators_follow_java_and_the_kotlin_forms(tmp_path):
    sites = _sites(tmp_path, "src/demo/Board.kt", BOARD)
    place = [site for site in sites if site.name == "place"]
    pairs = _pairs(place)
    assert (">", ">=") in pairs
    assert ("&&", "||") in pairs
    assert ("||", "&&") in pairs
    assert ("==", "!=") in pairs
    assert ("!=", "==") in pairs
    assert ("===", "!==") in pairs
    assert ("!==", "===") in pairs
    assert ("+", "-") in pairs
    assert ("-", "") in pairs
    assert ("*", "/") in pairs
    assert ("/", "*") in pairs
    assert ("!", "") in pairs
    assert ("?.", ".") in pairs
    assert ("!!", "") in pairs
    assert ("++", "--") in pairs
    assert ("<", "<=") in pairs
    assert ("in", "!in") in pairs
    assert ("is", "!is") in pairs
    assert ("0", "1") in pairs
    assert ("1", "0") in pairs
    assert ("true", "false") not in pairs
    hide = [site for site in sites if site.name == "hide"]
    assert ("false", "true") in _pairs(hide)
    elvis = [site for site in place if site.category == "elvis"]
    assert {site.mutant for site in elvis} == {"0", "(name?.length)!!"}
    assert {site.namespace for site in place} == {"demo.Board"}
    assert {site.form_id for site in sites if site.name == "hide"} == {"defn-/hide"}
    assert {site.namespace for site in sites if site.name == "top"} == {"demo.BoardKt"}
    assert (">=", ">") in _pairs(site for site in sites if site.name == "top")


def test_type_arguments_and_strings_are_not_sites(tmp_path):
    source = """package demo
fun size(items: List<Int>): Int {
    val label = "x > 0 && true ?: 1"
    return items.size
}
fun sum(x: Int) = "n=${x + 1}"
"""
    sites = _sites(tmp_path, "src/demo/Sizes.kt", source)
    assert ("<", "<=") not in _pairs(sites)
    assert (">", ">=") not in _pairs(sites)
    assert {site.original for site in sites} == {"+", "1"}


def test_ranges_and_a_private_top_level_function(tmp_path):
    source = """package demo
fun span(a: Int, b: Int) = a..b
fun open(a: Int, b: Int) = a..<b
fun untilEnd(a: Int, b: Int) = a until b
private fun hide(a: Int) = a > 0
"""
    sites = _sites(tmp_path, "src/demo/Range.kt", source)
    assert ("..", "..<") in _pairs(sites)
    assert ("..<", "..") in _pairs(sites)
    assert ("until", "..") in _pairs(sites)
    assert {site.form_id for site in sites if site.name == "hide"} == {"defn-/hide"}


def test_context_receivers_keep_the_object_and_a_call_named_context(tmp_path, capsys):
    source = """package demo

context(Foo)
fun choose(a: Boolean) = if (a && true) 1 else 0

object Box {
    fun use(f: context(Foo) () -> Int): Int {
        if (f == null) return 0
        return 1
    }

    fun other(a: Boolean) = if (a) 1 else 0
}

fun plain() = context(1) + 1
"""
    sites = _sites(tmp_path, "src/demo/Receivers.kt", source)
    assert "parse error" not in capsys.readouterr().err
    assert {site.name for site in sites} >= {"choose", "use", "other", "plain"}
    assert {site.namespace for site in sites if site.name == "other"} == {"demo.Box"}
    assert ("+", "-") in _pairs(site for site in sites if site.name == "plain")
    assert "context(1)" in prepare_kotlin(source)
    dotted = "fun exp() = list.context(ctx)\n"
    assert blank_context_receivers(dotted) == dotted


def test_repairs_keep_lines_and_the_while_comparison_is_a_real_site(tmp_path, capsys):
    source = r"""package demo

fun <T> keep(x: T): T where T : Any = x

fun rewrite(where: List<Int>): Int {
    if (where.none { it > 0 }) {
        return@flatMap Stream.empty<Pair<A, B>>()
    }
    where.mapIndexedNotNull { index, simple ->
        if (simple > 0) index else null
    }
    return where.size
}

fun sym(name: String) = "echo \$name"

class Foo {
    fun before(a: Boolean) = if (a) 1 else 0
    val s = "pattern:\$"
    fun after(a: Boolean) = if (a) 1 else 2
}

fun merge(out: MutableList<Int>): Int {
    var i = 0
    while (i < out.size - 1) {
        if (i > 0) return i
        i++
    }
    return i
}

fun shift(by: Int) = when {
    by < 0 -> 1
    else -> 0
}
"""
    prepared = prepare_kotlin(source)
    assert len(prepared) == len(source)
    assert prepared.count("\n") == source.count("\n")
    assert "where T : Any" in prepared
    assert ";where.mapIndexedNotNull" in prepared
    assert "i<(out).size" in prepared
    assert "by < 0 ->" in prepared
    assert "\\$" not in prepared
    sites = _sites(tmp_path, "src/demo/Repairs.kt", source)
    assert "parse error" not in capsys.readouterr().err
    assert {site.name for site in sites} >= {"rewrite", "before", "after", "merge", "shift"}
    compared = [site for site in sites if site.name == "merge" and site.original == "<"]
    assert len(compared) == 1
    mutated = apply_site(source, compared[0].start, compared[0].end, compared[0].mutant)
    assert "while (i <= out.size - 1)" in mutated
    assert "i<(out)" not in mutated


def test_gradle_and_maven_commands(tmp_path):
    root = tmp_path / "proj"
    module = root / "lib"
    source = module / "src" / "main" / "kotlin" / "demo" / "Board.kt"
    source.parent.mkdir(parents=True)
    source.write_text("package demo\nfun place(x: Int) = x > 0\n", encoding="utf-8")
    (module / "build.gradle.kts").write_text("plugins { kotlin(\"jvm\") }\n", encoding="utf-8")
    wrapper = root / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)

    command, directory = plan_command(root, source, "kotlin", None, ["--tests", "demo.BoardTest"])
    assert command == ["../gradlew", "test", "--tests", "demo.BoardTest"]
    assert directory == module

    (root / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    command, directory = plan_command(root, source, "kotlin", None, [], ["-Dtest=BoardTest"])
    assert command[0] == "../gradlew"
    assert directory == module

    (module / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    command, directory = plan_command(root, source, "kotlin", None, [], ["-Dtest=BoardTest"])
    assert command == ["mvn", "-q", "test", "-DexcludeTags=no-mutate", "-Dtest=BoardTest"]
    assert directory == module

    java = root / "src" / "demo" / "Board.java"
    java.parent.mkdir(parents=True)
    java.write_text("package demo;\nclass Board {}\n", encoding="utf-8")
    plain, _java_dir = plan_command(root, java, "java", None)
    assert plain == ["mvn", "-q", "test", "-DexcludeTags=no-mutate"]
    extra, _java_dir = plan_command(root, java, "java", None, maven_args=["-Dtest=BoardTest"])
    assert extra[-1] == "-Dtest=BoardTest"

    loose = tmp_path / "loose"
    loose_source = loose / "App.kt"
    loose.mkdir()
    loose_source.write_text("fun main() {}\n", encoding="utf-8")
    command, directory = plan_command(loose, loose_source, "kotlin", None)
    assert command == ["gradle", "test"]
    assert directory == loose.resolve()


def test_a_worker_copies_the_gradle_wrapper(tmp_path):
    root = tmp_path / "proj"
    source = root / "src" / "main" / "kotlin" / "demo" / "Board.kt"
    source.parent.mkdir(parents=True)
    source.write_text("package demo\nfun place(x: Int) = x > 0\n", encoding="utf-8")
    wrapper = root / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    (root / "build.gradle.kts").write_text("", encoding="utf-8")
    (root / "settings.gradle.kts").write_text("rootProject.name = \"demo\"\n", encoding="utf-8")
    base = new_run_dir(root)
    try:
        workers = create_workers(base, root, "src/main/kotlin/demo/Board.kt", source.read_bytes(), 1)
        copied = workers[0] / "gradlew"
        assert copied.is_file()
        assert not copied.is_symlink()
        assert copied.read_bytes() == wrapper.read_bytes()
        assert copied.stat().st_mode & stat.S_IXUSR
        assert (workers[0] / "settings.gradle.kts").read_text(encoding="utf-8").startswith("rootProject")
    finally:
        delete_tree(base)


def test_gradle_jacoco_lines_and_merged_reports(tmp_path):
    source = tmp_path / "src" / "main" / "kotlin" / "demo" / "Board.kt"
    source.parent.mkdir(parents=True)
    source.write_text("package demo\nclass Board {}\n", encoding="utf-8")
    report = tmp_path / "build" / "reports" / "jacoco" / "test" / "jacocoTestReport.xml"
    report.parent.mkdir(parents=True)
    report.write_text(
        """<?xml version="1.0"?>
<report name="demo">
  <package name="demo">
    <sourcefile name="Board.kt">
      <line nr="4" mi="0" ci="2" mb="0" cb="0"/>
      <line nr="5" mi="1" ci="0" mb="0" cb="0"/>
    </sourcefile>
  </package>
</report>
""",
        encoding="utf-8",
    )
    assert covered_lines(tmp_path, source, "kotlin") == {4}

    merged = tmp_path / "build" / "reports" / "jacoco" / "crapper" / "main.xml"
    merged.parent.mkdir(parents=True)
    merged.write_text(
        """<?xml version="1.0"?>
<report name="crapper">
  <package name="demo">
    <sourcefile name="Board.kt">
      <line nr="9" mi="0" ci="1" mb="0" cb="0"/>
    </sourcefile>
  </package>
</report>
""",
        encoding="utf-8",
    )
    assert covered_lines(tmp_path, source, "kotlin") == {9}


def test_build_args_follow_crapper_toml_and_the_command_line(tmp_path):
    options = parse_args(
        ["--gradle-arg=-DJacoco", "--gradle-arg", "--tests", "demo.BoardTest", "--maven-arg", "-Dtest=BoardTest"]
    )
    assert options.gradle_args == ["-DJacoco", "--tests"]
    assert options.maven_args == ["-Dtest=BoardTest"]
    assert "Kotlin" in parse_args(["--help"]).message

    (tmp_path / "crapper.toml").write_text(
        'gradle-args = ["--tests", "from.file"]\nmaven-args = ["-Dfile=1"]\n',
        encoding="utf-8",
    )
    gradle, maven, error = _build_args(options, tmp_path)
    assert error is None
    assert gradle == ["--tests", "from.file", "-DJacoco", "--tests"]
    assert maven == ["-Dfile=1", "-Dtest=BoardTest"]

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "crapper.toml").write_text("gradle-args = 1\n", encoding="utf-8")
    _gradle, _maven, error = _build_args(parse_args([]), broken)
    assert error
    assert "gradle-args" in error


def test_gradle_mutants_run_through_the_copied_wrapper(tmp_path, capsys):
    root = tmp_path / "proj"
    source = root / "src" / "main" / "kotlin" / "demo" / "Math.kt"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package demo\n"
        "fun add(a: Int, b: Int): Int {\n"
        "    if (a > 0) return a + b\n"
        "    return 0\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "build.gradle.kts").write_text("", encoding="utf-8")
    script = root / "gradlew"
    script.write_text(
        "#!/bin/sh\n"
        "grep -q 'return a + b' src/main/kotlin/demo/Math.kt || exit 1\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    code = run(
        [
            "--no-coverage",
            "--mutate-all",
            "--max-workers",
            "1",
            "--root",
            str(root),
            "--gradle-arg=--tests=demo.MathTest",
            "src/main/kotlin/demo/Math.kt",
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "KILLED" in captured.out
    assert "SURVIVED" in captured.out
    assert source.read_text(encoding="utf-8").startswith("package demo")
    assert os.access(script, os.X_OK)
