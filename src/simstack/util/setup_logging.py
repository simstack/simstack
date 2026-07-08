import logging

from simstack.util.db_logger import DBLogHandler


def setup_logging(
    connection_string: str,
    db_name: str,
    log_level=logging.INFO,
    console=True,
    log_format: str = "%(asctime)s - %(name)-15s - %(levelname)-10s - %(filename)-20s:%(lineno)4d - %(message)s",
):
    """
    Configures the root logger with specified logging level, handlers for both database
    and optionally the console.

    :param log_format:
    :param connection_string: A string representing the database connection.
    :param db_name: A name of the database to log into.
    :param log_level: Logging verbosity level, defaulting to logging.INFO.
    :param console: A boolean indicating whether to log messages to the console.
    :return: Configured root logger instance.
    """
    # Configure the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove any existing handlers to avoid duplicates during reloads
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create formatter
    formatter = logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    db_handler = DBLogHandler(connection_string, db_name, "logs")

    # Add database handler
    db_handler.setLevel(log_level)
    db_handler.setFormatter(formatter)
    root_logger.addHandler(db_handler)

    # Optionally add console handler
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(log_level)
        root_logger.addHandler(console_handler)

    return root_logger
