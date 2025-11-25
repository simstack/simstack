import logging
import os
import subprocess
import time
from datetime import datetime

logger = logging.getLogger("git_pull")


def git_pull_periodically(repo_path, interval_minutes=1, log_file=None):
    """
    Performs git pull in the specified repository at regular intervals.

    Args:
        repo_path (str): Path to the Git repository
        interval_minutes (int): Time interval between pulls in minutes
        log_file (str, optional): Path to log file. If None, logs to console.

    Returns:
        None
    """

    # Convert minutes to seconds
    interval_seconds = interval_minutes * 60
    logger.info("git_pull " + repo_path)
    # Check if the path is a valid git repository
    if not os.path.exists(os.path.join(repo_path, ".git")):
        logging.error(f"The directory {repo_path} is not a valid Git repository.")
        return

    logging.info(
        f"Starting periodic git pull every {interval_minutes} minutes for {repo_path}"
    )

    try:
        while True:
            try:
                # Change to the repository directory
                os.chdir(repo_path)

                # Log the start time
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logging.info(f"Executing git pull at {now}")

                # Execute git pull
                result = subprocess.run(
                    ["git", "pull"], capture_output=True, text=True, check=True
                )

                # Log the output
                logging.info(f"Git pull output: {result.stdout.strip()}")
                if result.stderr:
                    logging.warning(f"Git pull stderr: {result.stderr.strip()}")

            except subprocess.CalledProcessError as e:
                logging.error(f"Git pull failed: {e.stderr.strip()}")
            except Exception as e:
                logging.error(f"Error during git pull: {str(e)}")

            # Wait for the next interval
            logging.info(f"Waiting {interval_minutes} minutes until next pull...")
            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        logging.info("Git pull process stopped by user.")
