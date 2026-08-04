"""
Central logging setup.

Two things write logs in this app: our own code (loggers named
"perennia" / "perennia.<module>") and uvicorn itself (loggers named
"uvicorn" / "uvicorn.error" / "uvicorn.access"). Unhandled exceptions —
the source of any 500 response — are logged by uvicorn.error with a full
traceback, not by our own "perennia" logger. A file handler attached
only to "perennia" would miss those entirely, so this module builds one
shared rotating-file config and applies it to both.
"""
import copy
import logging
import logging.config
from logging.handlers import RotatingFileHandler

from app.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_app_logging() -> None:
    """Attach a console handler + rotating file handler to the 'perennia'
    logger tree. Safe to call multiple times (e.g. under a reloader) —
    clears any handlers it previously added first."""
    logger = logging.getLogger("perennia")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        settings.LOG_DIR / settings.LOG_FILE,
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def uvicorn_log_config() -> dict:
    """Returns a logging config for uvicorn.run(log_config=...) that keeps
    uvicorn's normal console output but adds the same rotating file so
    request errors and unhandled-exception tracebacks are captured too."""
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg["formatters"]["file"] = {"format": LOG_FORMAT}
    cfg["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(settings.LOG_DIR / settings.LOG_FILE),
        "maxBytes": settings.LOG_MAX_BYTES,
        "backupCount": settings.LOG_BACKUP_COUNT,
        "encoding": "utf-8",
        "formatter": "file",
    }
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        cfg["loggers"].setdefault(logger_name, {})
        handlers = list(cfg["loggers"][logger_name].get("handlers", []))
        # uvicorn.error ships with no handlers of its own in uvicorn's
        # default config — it only ever printed to console by propagating
        # up to the 'uvicorn' logger. We're about to turn propagation off
        # (below) to stop double file-writes, so give it the console
        # handler explicitly or it goes silent on the terminal.
        if not handlers:
            handlers = ["default"]
        if "file" not in handlers:
            handlers.append("file")
        cfg["loggers"][logger_name]["handlers"] = handlers
        cfg["loggers"][logger_name].setdefault("level", "INFO")
        # Each of these three loggers now has its own handlers (console +
        # file) directly attached, so none of them should also hand its
        # records up to a parent logger that would process them again —
        # otherwise every line gets written twice.
        cfg["loggers"][logger_name]["propagate"] = False
    return cfg
