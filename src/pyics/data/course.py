from os import PathLike
import os
from pathlib import Path
from typing import Optional

from pyics.util import json_handling as json
from pyics.util.logging import get_logger


class Course:
    """
    Course class. Handles creation and storage of courses, as well as safe task loading
    and keeping track of course progress.
    """

    @staticmethod
    def create_course_manifest(
        fp: PathLike,
        task_files: list[PathLike],
        *,
        name: Optional[str] = None,
        use_task_names: bool = True,
    ) -> None:
        """
        Compose a course manifest from the provided ``task_files``. A course manifest is
        a json file containing the course name, current progress, and an ordered mapping
        of task names and their file paths.

        Parameters
        ----------
        fp: PathLike
            Path to the course manifest. Should have the suffix '.pyicscourse.json'. If
            it doesn't, makes changes to the suffix and logs (if logging is active).
        task_files: list[PathLike]
            List of task files to compose the course. Order is relevant.
        name: str | None (optional, keyword only)
            Name of the course. If None, use ``fp``'s stem.
        use_task_names: bool (optional, keyword only)
            Use tasks' names for the mapping in the course manifest. Default is True.
            Setting this to False increases performance (no more json loading of tasks),
            but loses information (only enumerates tasks).
        """
        fp = Path(fp)

        match fp.suffixes:
            case []:
                fp = fp.with_suffix(".pyicscourse.json")
            case [_]:
                get_logger(__name__).warning(
                    f"Replacing suffix '{fp.suffix}' with '.pyicscourse.json'."
                )
                fp = fp.with_suffix(".pyicscourse.json")
            case [".pyicscourse", ".json"]:
                pass
            case _:
                get_logger(__name__).warning(
                    f"File suffixes don't match expected file path. Adding '.pyicscourse.json'."
                )
                fp = fp.with_suffix(fp.suffix + ".pyicscourse.json")

        manifest = {}

        manifest["name"] = name or fp.stem

        if use_task_names:

            def get_task_name(fp: PathLike) -> str:
                with open(fp, "r") as f:
                    name = json.load(f)["name"]
                return name

            keys = map(get_task_name, task_files)

        else:
            width = len(str(len(task_files)))
            keys = [
                f"{manifest['name']}_{str(n).zfill(width)}" for n in range(len(task_files))
            ]

        manifest["tasks"] = {k: v for k, v in zip(keys, task_files)}

        manifest["progress"] = 0.0

        os.makedirs(fp.parent, exist_ok=True)
        with open(fp, "w") as f:
            json.dump(manifest, f, indent=4)
