import logging
import logging.handlers
import sys
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup(log_dir: str = "logs", level: int = logging.INFO) -> None:
    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)
    root = logging.getLogger()
    root.setLevel(level)

    # Console (stdout)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        Path(log_dir).mkdir(exist_ok=True)

        # Daily rotation, keep 14 days
        file_h = logging.handlers.TimedRotatingFileHandler(
            f"{log_dir}/bot.log",
            when="midnight",
            backupCount=14,
            encoding="utf-8",
        )
        file_h.setFormatter(fmt)
        root.addHandler(file_h)

        # Errors only → easier post-mortem scanning
        err_h = logging.handlers.TimedRotatingFileHandler(
            f"{log_dir}/errors.log",
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        err_h.setLevel(logging.ERROR)
        err_h.setFormatter(fmt)
        root.addHandler(err_h)

    except OSError as e:
        logging.getLogger("log_setup").warning("Could not create log files: %s", e)

    # Log unhandled sync exceptions
    def _exc_handler(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("main").critical(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = _exc_handler

    # Suppress httpx/httpcore request-level logs to avoid leaking Telegram token URLs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
