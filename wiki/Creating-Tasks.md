# Creating Tasks

A task is one exercise: instructions, starter code, optional hints and solution, and the tests that decide when it is solved. You create it with an interactive command:

```sh
pyics create_task [--fp path/to/task]
```

Without `--fp`, the task file is written to the working directory, named after the task. The `.pyicstask.json` suffix is added if it is missing.

## Before you start

Have these files ready. `create_task` asks for paths to **existing** files; it doesn't create them for you.

1. **The exercise file**: the starter code students get, e.g. `.exercises/dfs.py` with a function stub and a `# TODO`. At least one is required. It can also be a Jupyter notebook.
2. **The solution file** (optional, recommended): e.g. `.solutions/dfs.py`. You need it to check `GeneralTest`s while you create them, and it is useful as the test information file.
3. **A test information file** (optional, recommended): a Python file (or notebook) that defines the values your tests need, such as arguments, expected results and rule lists. See [below](#the-test-information-file).

Work out what to check before you start. [Choosing Tests](Choosing-Tests) helps you pick the right test for each check.

## How the prompts work

| Key | Does |
|---|---|
| <kbd>enter</kbd> | submit an answer |
| <kbd>ctrl</kbd>+<kbd>d</kbd> | submit a multi-line answer (instructions, hints, expected output) |
| <kbd>esc</kbd> | go back to the previous prompt, keeping its answer for you to change |
| <kbd>ctrl</kbd>+<kbd>c</kbd> | cancel; nothing is written |
| <kbd>tab</kbd> | complete a file path |

Invalid answers aren't accepted: the error is shown under the prompt, and you can correct it. Nothing is written until you finish the last prompt.

## Step 1: Name

The task's name identifies it in the course. It must be unique among the tasks of a course.

## Step 2: Instructions

Shown in the app's header for as long as the task is open. Keep them short and concrete: what to implement, and any rules (e.g. "without recursion"). Longer explanations belong in the exercise file, as comments or docstrings, or in a notebook's markdown cells.

## Step 3: Hints

Add as many hints as you like, in order. Students reveal them one at a time with <kbd>h</kbd>; revealed hints stay visible until the app is closed. A good sequence goes from a nudge to an almost-solution:

1. "Keep the paths still to be explored in a stack: `pop()` from the top, `append` new ones."
2. "Start with the stack `[(start,)]`. Take the top path; if it ends at the goal, return it…"

## Step 4: Exercise files

Give the path of each exercise file. Paths are stored relative to the task file. The file's current contents are stored as the starter code.

If an exercise file is a notebook with [sections](Jupyter-Notebooks#several-exercises-in-one-notebook-sections), you are asked which section the task is about.

## Step 5: Solution files

Optional. Their contents are stored too. Solution files are written to students' machines when the task opens, unless you pack the course with `--no_solutions` (see [How Courses Work](How-Courses-Work#what-happens-on-a-students-machine)).

## Step 6: Tests

Choose a kind of test, fill in its parameters, and repeat until you choose **Done**. A task with several tests is solved once all of them pass (soft tests aside).

For each test, you are first asked: **"Reference a file with test information?"** If you do, the values that file defines are offered for the test's parameters.

Then each parameter of the test is asked for:

- **Text parameters** (`name`, a `PrintTest`'s `expected` output, …) are typed as plain text.
- **`file_path`**: choose one of the task's exercise files.
- **Everything else is a Python expression**, e.g. `[("A", "B"), ("B", "C")]`, `("A", "B", "D")` or `{"concept": "loop", "max": 0}`. The variables of the test information file are in scope, and listed under the prompt.
- For a `SimpleTest`, `Raises(...)` and `Make(...)` are in scope as well (see the [SimpleTest Reference](SimpleTest-Reference)).
- For a `StructureTest`, the available concepts are listed under the prompt.

A `GeneralTest` is run against the solution file right away: you see how many of its tests pass, and can keep it, edit it again, or discard it.

## The test information file

Typing long values into a prompt is error-prone. Instead, write them in a Python file:

```python
# dfs_info.py
graph = [("A", "B", 1), ("A", "C", 1), ("B", "D", 1), ("D", "E", 1), ("E", "G", 1)]

function_name = "depth_first_search"
function_args = [graph, "A", "G"]
expected = ("A", "B", "D", "E", "G")

rules = [{"concept": "recursion", "scope": "depth_first_search", "max": 0,
          "message": "Use a stack instead of recursion."}]
```

- **Names that match a parameter are selected automatically.** Above, `function_name`, `function_args`, `expected` and `rules` fill the parameters of the same name. Other values (like `graph`) can be used in the expressions you type.
- **Functions and classes work too.** For a `SpecTest`, the expected signature can be a function or class defined in the file. Using your **solution file** as the test information file is often the quickest way to do this.
- The file runs in its own directory, like the tests, so it can read data files and import modules next to it.
- A **notebook** can be the test information file, e.g. your solution notebook. Its code cells run as they do in tests.
- **Objects of classes the file defines can't be stored** in a task, because the file isn't there when the course runs. Use `Make("Graph", edges)` instead, which creates the object from the student's class during the test (see [SimpleTest Reference: objects as arguments](SimpleTest-Reference#objects-as-arguments)).

## Editing a task

```sh
pyics edit_task path/to/task.pyicstask.json
```

This goes through the same prompts, prefilled with the task's current values: confirming each one leaves the task as it was. Along the way you can:

- **Update a file**: if an exercise or solution file on disk differs from the stored version, you see the difference and can choose _Update_ to store the version on disk. **Do this after every change to starter code or solutions**: the task only knows what it stored.
- **Replace or remove files**: tests of a replaced file then test the new one.
- **Keep, edit or remove tests and hints**, one by one, and add new ones.

If you rename the task, the course manifests below the working directory that include it are updated too. `--max_depth` sets how many directory levels are searched (3 by default).

## Tips

- **Write the solution first**, then derive the starter code from it. Every test should pass on the solution.
- **One concept per task.** Several small tasks give better feedback than one large one.
- **Give tests clear names.** The name is what students see when a test fails, e.g. "Finds a path from A to G", not "test 1".
- **Use soft tests for style.** A `StructureTest` with `soft=True` can point out a nested loop without blocking progress.
- **Turn off tracing sparingly.** `traceable=False` hides a test's arguments from the trace (<kbd>t</kbd>), e.g. when they would reveal the solution. Tracing is one of the most useful tools students have.

**Next:** [Choosing Tests](Choosing-Tests), or [Creating Courses](Creating-Courses) once your tasks are ready.
