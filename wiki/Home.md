# PyICs for Instructors

PyICs (_Python Interactive Courses_) lets you build interactive Python exercises that students solve on their own computer, in their own editor. Students write code in an exercise file; each time they save, PyICs runs your tests on it and shows what still fails. When every test passes, they move on to the next task. It is inspired by [rustlings](https://github.com/rust-lang/rustlings).

This wiki is the instructor's guide: how to build a course, which tests to use, and how to get it to your students.

## Start here

If you are new to PyICs, read these in order. Together they take you from installing PyICs to students working on your course.

1. **[Getting Started](Getting-Started)**: install PyICs and try the demo course as a student would.
2. **[How Courses Work](How-Courses-Work)**: courses, tasks, and the files behind them.
3. **[Creating Tasks](Creating-Tasks)**: write a task with `pyics create_task`.
4. **[Choosing Tests](Choosing-Tests)**: which test checks what, with examples.
5. **[Creating Courses](Creating-Courses)**: put tasks together into a course with `pyics create_course`.
6. **[Distributing Courses](Distributing-Courses)**: pack the course and share it with students.

## I want to…

| I want to… | Go to |
|---|---|
| check the value a function returns | [SimpleTest Reference](SimpleTest-Reference) |
| check that a class or function has the right signature | [Choosing Tests: SpecTest](Choosing-Tests#spectest-signatures) |
| check _how_ students solve it (no loops, recursion, no `sorted()`…) | [StructureTest Reference](StructureTest-Reference) |
| check what a script prints, including `input()` | [Choosing Tests: PrintTest](Choosing-Tests#printtest-console-output) |
| write my own pytest tests | [GeneralTest Reference](GeneralTest-Reference) |
| give hints, or a warning that doesn't block progress | [Creating Tasks: hints](Creating-Tasks#step-3-hints), [Choosing Tests: soft tests](Choosing-Tests#options-every-test-has) |
| use Jupyter notebooks as exercises | [Jupyter Notebooks](Jupyter-Notebooks) |
| put several exercises in one notebook | [Jupyter Notebooks: sections](Jupyter-Notebooks#several-exercises-in-one-notebook-sections) |
| let students skip ahead | [Creating Courses: free navigation](Creating-Courses#free-navigation) |
| ship data files with a course | [Distributing Courses: pack](Distributing-Courses#1-pack-the-course) |
| publish a course library students can pull from | [Distributing Courses](Distributing-Courses) |
| change a task after publishing it | [Creating Tasks: editing](Creating-Tasks#editing-a-task), [Distributing Courses: updates](Distributing-Courses#publishing-a-new-version) |
| send students instructions | [Student Quick Guide](Student-Quick-Guide) |
| look up a command or option | [Command Reference](Command-Reference) |
| fix something that doesn't work | [Troubleshooting & FAQ](Troubleshooting-and-FAQ) |

## Reference

- **[Command Reference](Command-Reference)**: every `pyics` command and option.
- **[SimpleTest Reference](SimpleTest-Reference)**: call sequences, objects, `Make`, `Raises`, working directories.
- **[StructureTest Reference](StructureTest-Reference)**: rules, concepts, AST nodes, code patterns.
- **[GeneralTest Reference](GeneralTest-Reference)**: pytest code, the PyICs fixtures, `pyics.testing`.
- **[Student Quick Guide](Student-Quick-Guide)**: what students do, to copy into your course page or email.

## Requirements at a glance

- Python **3.14** or newer, on Windows 10+, macOS or Linux.
- Install with `pip install python-interactive-courses`. The command is `pyics`.
- Students need the same: PyICs and any packages your course uses, in one environment.

## Getting help

- Something missing or wrong? [Open an issue](https://github.com/johannes-langer/PyICs/issues). That includes tests that can't express what you need to check.
- The [README](https://github.com/johannes-langer/PyICs#readme) covers the same ground in one page, including the technical structure for contributors.
