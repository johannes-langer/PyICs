# Student Quick Guide

_For instructors: this page is written for your students. Link to it, or copy it into your course page and fill in the parts in angle brackets._

---

## Setup

You need **Python 3.14 or newer**. Create an environment for the course and install PyICs and the packages the course needs:

```sh
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install python-interactive-courses
```

## Getting the course

Your instructor gives you one of these:

- **A course library** (once per environment):

  ```sh
  pyics add_source <index URL>
  pyics search                   # lists the courses; "pyics search graphs" finds matching ones
  pyics pull <course name>       # pulls it into ./<course name>
  cd <course name>
  ```

- **A course archive** (`.zip`): `pyics pull <URL or path of the zip>`, then `cd` into the new directory.
- **The course files**: just `cd` into their directory.

If the course needs other packages, `pyics pull` tells you which, and how to install them (`--install_deps` installs them after asking). Install them in the same environment as PyICs.

> Courses run code on your computer. Only add sources and pull courses from people you trust.

## Working on the course

Start the app in the course's directory:

```sh
pyics
```

Choose the course. It opens at your first unsolved task:

1. The header shows the task's instructions. The main panel shows a link to your **exercise file** and the tests that still fail.
2. Open the exercise file in your editor and work on it.
3. **Save** the file. The tests run again every time you save.
4. When all tests pass, the task is solved. Press <kbd>n</kbd> for the next one.

Red tests have to pass. Yellow tests are warnings: worth fixing, but they don't stop you from moving on.

| Key | Action |
|---|---|
| <kbd>n</kbd> | next task (once this one is solved, unless the course lets you skip) |
| <kbd>p</kbd> | previous task |
| <kbd>h</kbd> | show the next hint |
| <kbd>t</kbd> | trace failed tests: shows every line your code ran, the values of variables, and the calls it made. Press again to turn it off. |
| <kbd>c</kbd> | course selector (<kbd>esc</kbd> returns to the course) |
| <kbd>ctrl</kbd>+<kbd>c</kbd> | quit; your progress is kept |

Your work is never overwritten. To start a task over, delete its exercise file; it is recreated when you open the task again.

## Getting updates

When your instructor publishes a new version of a course you pulled, run this in the course's directory:

```sh
pyics update
```

Your exercise files and your progress are kept.

## Jupyter notebooks

- When Jupyter or VS Code asks for a kernel, **choose the environment where PyICs is installed**.
- **Save** the notebook (<kbd>ctrl</kbd>+<kbd>s</kbd>) to run the tests. Running a cell isn't enough.
- The tests run all cells from the top, like _Restart Kernel and Run All Cells_. An error in one cell doesn't stop the others, and failed tests name the cell that raised, e.g. `cell 7 raised NameError`. Cells are counted from the top, markdown cells included.
- Don't delete the description cells of exercises: they mark where each exercise starts.
- Cells tagged `skip-execution` don't run in tests.

## Course libraries (sources)

| Command | Does |
|---|---|
| `pyics sources` | lists your sources |
| `pyics remove_source <name>` | removes one |
| `pyics search [term] [--offline]` | lists the courses of your sources |

Sources are stored per environment: in a virtual environment, in its `etc/pyics/sources.json`. Outside one, in your PyICs configuration directory (`~/.config/pyics` on Linux, `~/Library/Application Support/pyics` on macOS, `%APPDATA%\pyics` on Windows). The `PYICS_CONFIG_DIR` environment variable sets another one.

The course selector also lists the courses of your sources that you haven't pulled yet. Selecting one pulls it.
