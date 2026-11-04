# Jupyter Notebooks

Exercise and solution files can be Jupyter notebooks (`.ipynb`), and every kind of test works with them. Students work in JupyterLab, Notebook 7 or VS Code, ideally with the PyICs app in a terminal next to it. Each time they **save** the notebook (<kbd>ctrl</kbd>+<kbd>s</kbd>, or when Jupyter autosaves), the tests of the current task run.

The repository has a worked example in [`examples/search_notebook`](https://github.com/johannes-langer/PyICs/tree/main/examples/search_notebook): the demo course with both tasks in one notebook.

## Creating notebook tasks

`create_task` and `edit_task` accept notebooks as exercise and solution files like Python files.

- **Keep exercise notebooks in a visible directory**, e.g. next to the course manifest, not in `.exercises/`. JupyterLab only opens notebooks in hidden directories when started with `--ContentsManager.allow_hidden=True`.
- **Notebooks are stored normalized**: without outputs and execution counts, so that your solution's outputs don't reach students and task files stay small. The kernel (`kernelspec`) isn't stored either, since it names an environment on your machine.
- A notebook you merely ran, or ran with another kernel, doesn't count as changed when you edit the task. If it did change, `edit_task` shows the difference as the text of its cells (`# %% cell 3` blocks). If only its metadata changed (e.g. cell tags), it says so.
- **A notebook can be the [test information file](Creating-Tasks#the-test-information-file)**, e.g. your solution notebook. Its code cells run as they do in tests, and its variables are offered for the tests' parameters.
- `pack` finds the packages imported in code cells too, and shows the notebooks' `%pip install` lines as hints for what to declare. Those lines do nothing in tests, so **declare the packages** (see [Distributing Courses](Distributing-Courses#packages-your-course-needs)).

## How tests behave on notebooks

Tests run the code cells of the **saved** notebook from the top, like _Restart Kernel and Run All Cells_:

- **An error in a cell doesn't stop the run.** An unfinished exercise further down doesn't fail the tests of the exercises above it. A failed test names the cells that raised, e.g. `cell 7 raised NameError: name 'qeue' is not defined`. Cells are counted from the top, markdown cells included.
- **`SimpleTest`s and `SpecTest`s** find functions and classes by name, in whichever cell they are defined.
- **`StructureTest`s** check the code of all code cells (or of the task's section). Cells that can't be parsed are left out, and named in the message if the test fails.
- **`PrintTest`s** compare the output of all cells (or of the section's cells): what they print, plus what Jupyter shows as text, i.e. displayed objects and the value of a cell's last expression (unless it is `None` or the line ends with `;`).
  - The output only has to **contain** the expected output, since students output other things while they work. Short expected outputs are found too easily (`3` is found in `13`), so **expect enough to tell right from wrong**, e.g. `Sum: 3`.
  - A test that expects no output still fails if there is any.
- **Working directory**: tests run in the exercise file's directory, like Jupyter, so relative paths behave the same in both.

### Notebook-only syntax in tests

| In the notebook | In a test run |
|---|---|
| `%%time`, `%%timeit`, `%%capture`, `%time`, `%timeit` | the code runs once, untimed; what `%%capture` captures isn't part of the output |
| other cell magics (`%%writefile`, `%%bash`, …) | the cell is skipped |
| line magics (`%matplotlib inline`, `%pip install`, …) and shell commands (`!ls`) | do nothing; `files = !ls` gives an empty list |
| `display(x)` | prints `x` as text (its `repr`), with or without importing it |
| `plt.show()` | doesn't open a window or wait |
| cells tagged `skip-execution` | don't run, e.g. a long download or training run |

Tags are edited in JupyterLab's property inspector, or with _Add Cell Tag_ in VS Code.

## Several exercises in one notebook: sections

A lecture notebook often holds several exercises. **Sections** link each task to its part of the notebook, so each task is solved on its own.

### Marking a section

A section starts at a cell that is either:

- **tagged** `pyics:<name>`, e.g. `pyics:ex2` (Jupyter's own cell tags, hidden in most frontends), or
- **starts with a marker** on its first line: `<!-- pyics: ex2 -->` in a markdown cell, or `# pyics: ex2` in a code cell. Markers are visible everywhere, and students can restore one they deleted.

Names consist of letters, digits, `_`, `.` and `-`. Using both a tag and a marker, as the example course does, is the most robust.

A section ends just before the next section starts, or at the end of the notebook. Cells above the first section belong to none.

**Put the start on the exercise's description**, a markdown cell students have no reason to change. Cells they add, split or move below it belong to the section, as long as they stay above the next one.

### Linking tasks to sections

- `create_task` and `edit_task` ask which section of each notebook exercise file the task is about. It is stored in the task file (`"sections"`).
- **`StructureTest`s** check only the section's code cells.
- **`PrintTest`s** compare only the section's output, while the whole notebook still runs (a section usually needs the imports and data above it).
- **`SimpleTest`s and `SpecTest`s** aren't affected: they find functions and classes by name.
- A `StructureTest` or `PrintTest` uses the task's section unless you give it another one: `create_task` offers _The task's section_, _Whole notebook_, and the notebook's other sections. A test that uses the task's section follows when you change the task's section.

### One notebook, several tasks

Each task lists **the same notebook** among its exercise files, with its own section. The first task a student opens creates the notebook (an existing exercise file is never overwritten), so **all tasks must store the same contents for it**. `pack` and `edit_task` warn when they don't; in `edit_task`, choose _Update_ for the notebook to store it as it is on disk.

### What students see

The app's title names the section after the notebook's path, e.g. `lecture.ipynb › Exercise 2: Breadth-First Search` (the first heading of the cell that starts it). JupyterLab's _Table of Contents_ and VS Code's _Outline_ list the notebook's headings, so students find the exercise quickly.

If a student deletes the cell that starts a section, the section's tests fail with an explanation, e.g. `Section 'ex2' not found: the cell that starts it … was removed or its tag changed.` Tests never check another part of the notebook instead.

## Tell your students

- **Select the kernel** of the environment where PyICs is installed when Jupyter or VS Code asks: that is where the tests run. Notebooks come without a kernel.
- **Save to run the tests.** Running a cell isn't enough.
- Traces (<kbd>t</kbd>) and messages name cells and lines, e.g. `bfs.ipynb:cell 3` or `cell 3, line 2`.

These points are also in the [Student Quick Guide](Student-Quick-Guide#jupyter-notebooks).

## Alternative: Jupytext

Instead of using notebooks directly, you can pair a notebook with a Python file using [Jupytext](https://jupytext.readthedocs.io) and use the `.py` file as the exercise file. Students then need Jupytext in their Jupyter, and pairing tends to confuse beginners, so native notebook support is usually the better choice.
