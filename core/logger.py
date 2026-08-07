"""
Central logging setup.

Console output stays exactly as clean/minimal as before (plain prints in
the CLI); this module additionally mirrors everything important to
logs/download.log so a failed overnight run can be diagnosed afterwards
(request #25 in the improvement list).
"""

import logging

from config import LOG_DIR

LOG_FILE = LOG_DIR / "download.log"


def get_logger(name: str = "noveldownloader") -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        # Already configured (module can be imported from many places).
        return logger

    logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger.addHandler(file_handler)
    logger.propagate = False

    return logger
