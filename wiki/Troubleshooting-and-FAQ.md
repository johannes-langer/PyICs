# Troubleshooting & FAQ

## Authoring

**I changed the starter code (or solution), but students still get the old version.**
The task file stores a copy of each file. Run `pyics edit_task <task file>` and choose _Update_ for the changed file, then pack and publish again. Students who already opened the task keep their exercise file. See [Publishing a new version](Distributing-Courses#publishing-a-new-version).

**My edits to a solution file disappeared after I ran the course.**
Opening a task rewrites its solution files from the task file. Store your edits with `pyics edit_task` (choose _Update_) before you run the course, or test from a copy (see [Trying the course as a student](Creating-Courses#trying-the-course-as-a-student)).

**`create_task` rejects a value: "can't be stored".**
Objects of classes your test information file defines can't be stored, since that file isn't there when the course runs. Use `Make("ClassName", args…)`, which creates the object from the student's class while the test runs. See [SimpleTest Reference: objects as arguments](SimpleTest-Reference#objects-as-arguments).

**How do I test that a function raises an exception?**
Use `Raises(ValueError)` (or `Raises("MyError")` for an exception the exercise file defines) as the `expected` value of a `SimpleTest`. See [SimpleTest Reference: expected exceptions](SimpleTest-Reference#expected-exceptions).

**How do I test a class?**
`SpecTest` checks its attributes, methods and their signatures; a `SimpleTest` with `constructor="ClassName"` creates an object and calls its methods in sequence. See [Choosing Tests](Choosing-Tests).

**How do I compare floats with a tolerance, or check that an argument was mutated?**
Write a [`GeneralTest`](GeneralTest-Reference) with `pytest.approx`, or with assertions on the argument after the call.

**My `StructureTest` rule raises a `ValueError` in `create_task`.**
Rules are checked when the test is created. The message names the problem, e.g. an unknown concept or node name, or a pattern that isn't valid Python. Try rules in Python first:

```python
from pyics.tests.check_structure import check_structure
check_structure([{"concept": "loop", "max": 0}], open("solution.py").read())   # [] means it holds
```

**Recursion isn't detected (or is detected where there is none).**
Detection is static. It misses calls through aliases or `getattr`, and can be fooled by methods of the same name in other classes. See [How recursion is detected](StructureTest-Reference#how-recursion-is-detected).

**Can I hide some tests from students?**
No. A test's name is shown when it fails, and task files are readable JSON. Use `traceable=False` to keep a test's arguments out of the trace. For graded work, use `pack --no_solutions` and keep your grading tests separate.

**Can I write task files by hand?**
They are JSON, but the tests are stored in a serialized form. Use `create_task` and `edit_task`; they validate everything.

**A test I need can't be expressed.**
Try a [`GeneralTest`](GeneralTest-Reference) first. If that doesn't fit either, please [open an issue](https://github.com/johannes-langer/PyICs/issues).

## Data files and paths

**Student code reads a data file. Where do I put it?**
Next to the exercise file. Tests run in the exercise file's directory, so `open("edges.csv")` works in tests just as it does when students run the file. Ship the file with `pack --include`.

**A test with `isolated=True` can't find a data file.**
Isolated tests start in an empty temporary directory. Don't isolate tests whose code reads files next to it.

## Packages

**Students get `ModuleNotFoundError: numpy`.**
Packages must be installed in the environment where PyICs runs. Declare them in the course (`pack` offers to); then `pyics pull` tells students what to install, or installs it with `--install_deps`. See [Packages your course needs](Distributing-Courses#packages-your-course-needs).

**A task is refused because it imports a module.**
A task file may only import modules of the packages its course declares. Declare the package in the course's `requires`: run `pack` again and accept the suggestion.

## Notebooks

**Tests don't run when students run a cell.**
Tests run when the notebook is **saved**, on the saved notebook.

**The tests use the wrong Python, or can't import packages, in a notebook.**
Students must select the kernel of the environment where PyICs is installed. Notebooks come without a kernel.

**JupyterLab won't open the exercise notebook.**
It is probably in a hidden directory like `.exercises/`. Move exercise notebooks to a visible directory, or start JupyterLab with `--ContentsManager.allow_hidden=True`.

**A `PrintTest` passes on a wrong answer in a notebook.**
In notebooks, the output only has to contain the expected output. Expect more context, e.g. `Sum: 3` instead of `3`.

**"Section 'ex2' not found".**
The cell that starts the section was deleted or its tag changed. Students can restore the marker line (e.g. `<!-- pyics: ex2 -->`) at the top of the exercise's description cell. See [Sections](Jupyter-Notebooks#several-exercises-in-one-notebook-sections).

## Distribution

**`pyics add_source` refuses my URL.**
Plain `http` isn't allowed by default. Serve the index over https, or have students pass `--allow_http`.

**Students don't see the new version.**
Did you give it a new `--version`, run `pyics build_index` again, and upload both the archive and `index.json`? Students then run `pyics update` in the course directory.

**Sources disappeared after switching virtual environments.**
Sources are stored per environment. Add them again in the new environment, or let an administrator add them for all users. See [Student Quick Guide](Student-Quick-Guide#course-libraries-sources).

## Reporting a bug

Run the app with logging turned on, `pyics --log_level DEBUG`, reproduce the problem, and attach `.pyics.log` to an [issue](https://github.com/johannes-langer/PyICs/issues).
