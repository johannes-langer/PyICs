# Command Reference

Every command is a subcommand of `pyics`. Run `pyics <command> --help` for its options. Boolean options are flags, e.g. `--yes` (and `--no_yes`). Options that take a list accept several values, e.g. `--tags a b`.

## Global options

These configure the app's log (`pyics` without a command), e.g. `pyics --log_level DEBUG`.

| Option | Default | Meaning |
|---|---|---|
| `--log_level` | `50` (critical only) | Logging level, as a number or a name, e.g. `10` or `DEBUG`. Useful when reporting a bug. |
| `--log_file` | `./.pyics.log` | Where the log is written. |

## Running courses

| Command | Does |
|---|---|
| `pyics` | Opens the app: the course selector, then the course. Courses are found in the working directory and up to two levels below it. |

## Authoring

| Command | Does |
|---|---|
| `pyics create_task [--fp FP]` | Creates a task interactively. `--fp`: where to write the task file (default: the working directory, named after the task). See [Creating Tasks](Creating-Tasks). |
| `pyics edit_task [--max_depth N] task_file` | Edits a task file in place. Course manifests up to `N` levels below the working directory (default 3) are kept consistent with it. |
| `pyics create_course [--fp FP] [--max_depth N]` | Creates a course interactively from existing task files (searched `N` levels deep, default 3) or new ones. See [Creating Courses](Creating-Courses). |
| `pyics edit_course [--max_depth N] course_file` | Edits a course manifest in place. |

Keys in the interactive commands: <kbd>enter</kbd> submits, <kbd>ctrl</kbd>+<kbd>d</kbd> submits multi-line text, <kbd>esc</kbd> goes back one prompt, <kbd>tab</kbd> completes paths, <kbd>ctrl</kbd>+<kbd>c</kbd> cancels without writing.

## Distributing

| Command | Does |
|---|---|
| `pyics pack [options] manifest` | Packs a course into a `.zip` archive. See [Distributing Courses](Distributing-Courses#1-pack-the-course). |
| `pyics build_index [--out OUT] [--name NAME] [directory]` | Writes `index.json` for the archives in `directory` (default: the working directory) and below. |

`pack` options:

| Option | Meaning |
|---|---|
| `--out OUT` | Archive path, or directory for it. Default `./<slug>-<version>.zip`. |
| `--version VERSION` | Course version. Stored in the manifest. |
| `--description TEXT` | Short description. Stored in the manifest. |
| `--slug SLUG` | Name the course is pulled by. Stored in the manifest. |
| `--tags TAG [TAG …]` | Tags. Stored in the manifest. |
| `--include PATH [PATH …]` | Extra files or directories, e.g. data. |
| `--no_solutions` | Leave solutions out. |
| `--yes` | Don't ask for confirmation, e.g. to declare packages. |

## Getting courses (students)

| Command | Does |
|---|---|
| `pyics add_source [--name NAME] [--allow_http] [--yes] url` | Adds a course library by the URL (or path) of its index. |
| `pyics sources` | Lists the course libraries. |
| `pyics remove_source name` | Removes a course library. Pulled courses stay. |
| `pyics remove_legacy_sources [--yes]` | Deletes the user-wide sources that older versions of PyICs stored for all environments. |
| `pyics search [--offline] [term]` | Lists the courses in the libraries, or those matching `term` in their name, title, description or tags. `--offline` uses the indexes fetched last. |
| `pyics pull [--dest DIR] [--install_deps] [--allow_http] [--yes] course` | Pulls a course by name (`<source>/<name>` if several libraries have one of that name), or an archive by URL or path, into `./<name>` or `--dest` (which must be new or empty). |
| `pyics update [--install_deps] [--allow_http] [--force] [--yes] [directory]` | Updates a pulled course to its latest version, keeping exercise files and progress. `--force` updates even if it is up to date, e.g. to restore its files. |

## Python API

For [test information files](Creating-Tasks#the-test-information-file) and `GeneralTest` sources:

| Import | Contents |
|---|---|
| `from pyics.tests import SimpleTest, SpecTest, StructureTest, PrintTest, GeneralTest` | The test classes |
| `from pyics.tests import Make, Raises` | Markers for `SimpleTest` values (see [SimpleTest Reference](SimpleTest-Reference)) |
| `from pyics.tests.check_structure import check_structure` | Try `StructureTest` rules on source code without creating a test (see [StructureTest Reference](StructureTest-Reference)) |
| `from pyics.testing import capture_stdout, provide_input` | Helpers for `GeneralTest` code (see [GeneralTest Reference](GeneralTest-Reference#pyicstesting)) |
