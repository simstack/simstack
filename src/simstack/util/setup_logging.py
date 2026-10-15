import logging


def setup_logging(
    log_level=logging.INFO,
    console=True,
    runner_api=None,
    runner_task_id: str | None = None,
    log_format: str = "%(asctime)s - %(name)-15s - %(levelname)-10s - %(filename)-20s:%(lineno)4d - %(message)s",
):
    """
    Configure standard Python logging without opening a database connection.

    Runner log persistence is handled by the execution API, not by this
    process-local console configuration.

    :param log_format:
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

    # Optionally add console handler
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(log_level)
        root_logger.addHandler(console_handler)

    if runner_api is not None:
        from simstack.runner_runtime.logging import RunnerApiLogHandler

        api_handler = RunnerApiLogHandler(runner_api, task_id=runner_task_id)
        api_handler.setLevel(log_level)
        root_logger.addHandler(api_handler)

    return root_logger
