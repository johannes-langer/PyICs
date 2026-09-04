import logging
from os import PathLike
from pathlib import Path

_setup_done: bool = False


def setup_logging(
    level: logging._Level = logging.INFO, fp: PathLike = Path("./.pyics.log")
):
    """
    Perform logging setup
    """
    global _setup_done

    logging.root.setLevel(level)
    logging.root.handlers.clear()

    fp = fp if isinstance(fp, Path) else Path(fp)
    fp.parent.mkdir(exist_ok=True)

    file_handler = logging.FileHandler(fp, delay=True)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s (%(lineno)d)",
            datefmt="%H:%M:%S",
        )
    )

    logging.root.addHandler(file_handler)

    _setup_done = True


def get_logger(name: str) -> logging.Logger:
    if not _setup_done:
        setup_logging()

    return logging.getLogger(name)
