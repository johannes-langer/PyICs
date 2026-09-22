import jsonargparse


def _create_course() -> None:
    """
    Console script to collect task files and construct a course manifest form them.
    """
    ...


def create_course() -> None:
    jsonargparse.auto_cli(_create_course)
