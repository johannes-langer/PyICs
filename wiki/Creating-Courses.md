# Creating Courses

A course puts tasks in order. Create one with:

```sh
pyics create_course [--fp path/to/course] [--max_depth 3]
```

Run it in your course directory. Without `--fp`, the manifest is written to the working directory, named after the course, with the `.pyicscourse.json` suffix.

## The prompts

1. **Course name**: shown in the course selector, and in course libraries when you publish it.
2. **Add tasks**, one at a time:
   - _Add an existing task_: pick one of the task files found below the working directory (up to `--max_depth` levels, 3 by default).
   - _Create a new task_: goes through the whole [task creation flow](Creating-Tasks), then asks where to write the task file (e.g. `.tasks/dfs.pyicstask.json`). New task files are only written once the course is finished.
   - _Done_ once all tasks are added. A course needs at least one task.
3. **Task order**: with the arrow keys, <kbd>space</kbd> picks up a task, <kbd>↑</kbd>/<kbd>↓</kbd> move it, <kbd>space</kbd> drops it.
4. **Free navigation**: "Let learners move on to the next task before solving the current one?" See [below](#free-navigation).

The keys are the same as in `create_task`: <kbd>esc</kbd> goes back one prompt, <kbd>ctrl</kbd>+<kbd>c</kbd> cancels without writing anything.

A task file can belong to several courses, e.g. a shared warm-up task.

## Free navigation

By default, students have to solve a task before <kbd>n</kbd> moves on. With free navigation (`"free_navigation": true` in the manifest), <kbd>n</kbd> always moves on.

- **Leave it off** for courses that build up step by step, where each task needs the one before.
- **Turn it on** for collections of independent exercises, practice sheets, or courses where students should be able to skip a task they are stuck on.

## Editing a course

```sh
pyics edit_course path/to/course.pyicscourse.json [--max_depth 3]
```

This goes through the same prompts, prefilled. For each task of the course you can **keep** it, **edit** it (the task creation flow, prefilled; the task file is rewritten once you finish), or **remove** it from the course (its task file is kept). Then you can add more tasks, reorder them, and change free navigation.

The manifest is rewritten in place, and each task keeps its progress. If you rename a task, the other course manifests below the working directory that include it are updated too.

To change a single task, `pyics edit_task` is quicker (see [Creating Tasks](Creating-Tasks#editing-a-task)).

## Trying the course as a student

Before you hand a course out, work through it the way a student will.

**From a copy (quick):** copy the course directory somewhere else, delete the copy's exercise files (e.g. `.exercises/`), and run `pyics` in it. The exercise files are recreated from the starter code stored in the tasks, exactly as students get them.

**From an archive (closest to the real thing):** pack the course and pull it into a new directory:

```sh
pyics pack my_course.pyicscourse.json --version 1.0.0 --out /tmp/my-course.zip
pyics pull /tmp/my-course.zip --dest /tmp/my-course-test
cd /tmp/my-course-test
pyics
```

This also checks that every data file the course needs is in the archive (see [Distributing Courses](Distributing-Courses)).

Don't test in your authoring directory if you have unsaved changes to solution files: opening a task rewrites its solution files from the task file (see [How Courses Work](How-Courses-Work#what-happens-on-a-students-machine)).

Things to check:

- Every task fails on the starter code and passes on the solution.
- Failure messages make sense without knowing the solution.
- Hints come in a sensible order.
- Instructions fit in the header.
- The trace (<kbd>t</kbd>) is helpful and doesn't give the solution away.

**Next:** [Distributing Courses](Distributing-Courses).
