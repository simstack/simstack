#!/bin/bash
source ~/.bashrc
# Path to the Python script

# Path to the Git repository
USER=$(whoami)
REPO_PATH="$HOME/projects/simstack-model"
SCRIPT_PATH="src/simstack/core/runner.py"
RESOURCE=justus
# Navigate to the repository

# Path to existing micromamba installation
MAMBA_ROOT="$HOME/mambaforge"
export PATH="$MAMBA_ROOT/bin:$PATH"

# Initialize shell environment (only needed once per shell session)
#eval "$($MAMBA_ROOT/bin/conda shell hook -s bash)"

cd "$REPO_PATH" || exit
micromamba activate simstack-env

export PYTHONPATH="$REPO_PATH:$REPO_PATH/src"

# Better process checking with more details
echo "$(date +%Y-%m-%d\ %H:%M:%S) Checking for running processes..."
RUNNING_PIDS=$(pgrep -u "$USER" -f "python.*$SCRIPT_PATH")
if [ ! -z "$RUNNING_PIDS" ]; then
    echo "Found running processes with PIDs: $RUNNING_PIDS"
    # Show detailed process info
    ps -u "$USER" -p "$RUNNING_PIDS" -o pid,ppid,etime,cmd
fi

# Check for updates using git pull
if git pull | grep -q "Already up to date"; then
    echo "$(date +%Y-%m-%d\ %H:%M:%S) No updates found."
    if ! pgrep -u "$USER" -f "python.*$SCRIPT_PATH" > /dev/null; then
        echo "$(date +%Y-%m-%d\ %H:%M:%S) Script is not running. Starting it..."
        nohup python $SCRIPT_PATH --resource $RESOURCE  > script_runner.out 2>&1 &
        echo "Started new process with PID: $!"
    else
        echo "$(date +%Y-%m-%d\ %H:%M:%S) Script is already running."
    fi
else
    echo "$(date +%Y-%m-%d\ %H:%M:%S) New version detected. Restarting the script..."
    # Kill the running script if it exists
    if [ ! -z "$RUNNING_PIDS" ]; then
        echo "Killing processes: $RUNNING_PIDS"
        pkill -u "$USER" -f "python.*$SCRIPT_PATH"
        sleep 2
        # Force kill if still running
        if pgrep -u "$USER" -f "python.*$SCRIPT_PATH" > /dev/null; then
            echo "Processes still running, force killing..."
            pkill -9 -u "$USER" -f "python.*$SCRIPT_PATH"
        fi
    fi

    # Start the updated script
    nohup python $SCRIPT_PATH --resource $RESOURCE   > script_runner.out  2>&1 &
    echo "Started new process with PID: $!"
fi
