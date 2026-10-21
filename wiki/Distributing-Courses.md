# Distributing Courses

There are three ways to get a course to students, from simplest to most convenient for them:

| Way | You do | Students do |
|---|---|---|
| **Share the files** | Give them the course directory (manifest and task files), e.g. as a git repository or a shared folder | Run `pyics` in it |
| **Share an archive** | `pyics pack`, then put the `.zip` on your course page | `pyics pull <url or path of the zip>` |
| **Publish a course library** (a _source_) | `pyics pack` each course, `pyics build_index`, upload the directory | `pyics add_source <index url>` once, then `pyics pull <course>` and `pyics update` |

A course library is worth it as soon as you have more than one course, or plan to fix or extend a course during the semester: students get updates with one command and keep their work.

## 1. Pack the course

```sh
pyics pack my_course.pyicscourse.json --version 1.0.0 --description "DFS and BFS" --tags graphs search
```

This writes `./<slug>-<version>.zip`, e.g. `search-algorithms-1.0.0.zip`. The archive holds the **course manifest** (with the progress reset) and the **task files**. Exercise and solution files aren't included: they are created from the task files when students start the course.

| Option | Meaning |
|---|---|
| `--version` | The course's version, e.g. `1.0.0`. Give every version you publish a new one, so that students can update. |
| `--description` | A short description, shown by `pyics search`. |
| `--tags` | Tags to find the course by, e.g. `--tags graphs search`. |
| `--slug` | The name students pull the course by, e.g. `search-algorithms`. By default derived from the course's name the first time you pack it, then kept, so renaming the course doesn't change it. Lowercase letters, digits, `.`, `_` and `-`. |
| `--include` | Other files or directories the course needs, e.g. `--include data/` for datasets the exercises read. |
| `--no_solutions` | Leave the solutions out of the task files, e.g. for a graded course. |
| `--out` | Where to write the archive, or a directory to write it to. |
| `--yes` | Don't ask for confirmation. |

`--version`, `--description`, `--tags` and `--slug` are **stored in the manifest**, so the next `pack` reuses them: you only need to pass what changes.

`pack` also warns about tasks that share a notebook but store different contents for it (see [Jupyter Notebooks](Jupyter-Notebooks#several-exercises-in-one-notebook-sections)).

### Packages your course needs

Tests run in the same Python environment as PyICs, so students must install any package your course uses, such as `numpy` or `pandas`. `pack` finds them: it looks at the imports of your exercise and solution code (including notebook cells and `GeneralTest` sources) and at the values your tests store (e.g. numpy arrays). It then offers to declare the ones that aren't declared yet in the manifest's `requires`.

Declared packages pay off on the student side:

- `pyics search` and the course selector show which ones are missing.
- `pyics pull` prints the command that installs them, or runs it with `--install_deps` (after asking). Nothing is ever installed without asking.

Declaring them also matters for safety: **a task file may only make PyICs import modules of the packages its course declares.** Any other module is refused before the task is loaded.

`%pip install` lines in notebooks do nothing in tests. `pack` shows them as hints for what to declare.

The manifest also gets `requires_pyics`, the minimum PyICs version (by default, the version you packed with).

## 2. Build an index

Put the archives of all your courses in one directory (subdirectories are fine) and run:

```sh
pyics build_index courses/ --name "Uni Bamberg AI courses"
```

This writes `courses/index.json`, listing the **newest version** of each course, with each archive's URL relative to the index and its SHA-256 hash:

```json
{
    "pyics_index": 1,
    "name": "Uni Bamberg AI courses",
    "courses": {
        "search-algorithms": {
            "title": "Search Algorithms",
            "description": "DFS and BFS",
            "version": "1.0.0",
            "requires_pyics": ">=1.0.3",
            "requires": ["numpy>=1.26"],
            "url": "search-algorithms-1.0.0.zip",
            "sha256": "…",
            "tags": ["graphs", "search"]
        }
    }
}
```

| Option | Meaning |
|---|---|
| `directory` | The directory of the archives. The working directory by default. |
| `--out` | Where to write the index. By default `index.json` in `directory`. |
| `--name` | The library's name. By default the name of the index it replaces, or the directory's name. |

## 3. Upload it

Upload the whole directory (index and archives) to any web server that serves static files over **https**: your university web space, GitHub Pages, an object storage bucket, and so on. A path on a network share works too.

Then give students the index's URL:

```sh
pyics add_source https://example.org/pyics-courses/index.json
pyics search
pyics pull search-algorithms
```

Plain `http` is refused unless students pass `--allow_http`; use https.

Administrators can add a source for all users of a computer, e.g. in a lab, in the site configuration directory (e.g. `/etc/xdg/pyics/sources.json` on Linux). See the [Student Quick Guide](Student-Quick-Guide#course-libraries-sources) for where sources are stored.

## Publishing a new version

1. Fix the task with `pyics edit_task` (or the course with `pyics edit_course`).
2. Pack it with a **new version**: `pyics pack my_course.pyicscourse.json --version 1.0.1`.
3. Put the archive next to the old ones, run `pyics build_index` again, and upload.

Students run `pyics update` in the course's directory. An update replaces the files that were pulled (manifest, task files, included files) but **never the exercise files** holding their work, nor any other file they created. Their progress is kept.

Keep in mind:

- **Changed starter code doesn't reach students who already started a task**, since their exercise file is kept. Put fixes students need into instructions, hints or tests, or tell them to delete the exercise file to get the new starter code (they lose their work on it).
- **Changed tests and hints take effect** the next time students open the task after updating.

## Security: what students should know

Courses run code on students' computers: the tests run their code together with yours. PyICs tells students to only add sources and pull courses from people they trust. To earn that trust, PyICs checks archives before using anything in them:

- An archive can't write files outside the directory it is pulled into.
- Its hash has to match the one in the index.
- Its task files can only import modules of the packages the course declares.

**Next:** send students the [Student Quick Guide](Student-Quick-Guide).
