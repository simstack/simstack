## SimStack II Overview ⚡️

Simstack II is a workflow orchestration tool that lets you run your workflows on remote 
resources. 

This repo provides the base package for Simstack II development. To use the graphical user interface,
you need to install the 

- [simstack-server](https://github.com/simstack/simstack-server) project, a Python
  FastAPI app that exclusively owns the SimStack database connection.
- [simstack-ui](https://github.com/simstack/simstack-ui) project, a react-js web frontend. 

You can use the [simstack-example](https://github.com/simstack/simstack-example) project 
to get started. 

## Development workflow and versioning

This repository uses [Gitflow](https://nvie.com/posts/a-successful-git-branching-model/):

- `main` contains production releases.
- `develop` integrates changes for the next release.
- `feature-*`|`fix-*` branches start from and merge back into `develop`.
- `release-*` branches start from `develop`, then merge into both `develop` and `main`.
- `hotfix-*` branches start from `main`, then merge into both `main` and `develop`.
-  each `main` merge commit must have a version tag.

Releases follow [Semantic Versioning 2.0.0](https://semver.org/) using
`MAJOR.MINOR.PATCH`: increment `MAJOR` for incompatible API changes, `MINOR` for
backward-compatible functionality, and `PATCH` for backward-compatible bug fixes.

### Setting the version

The package version is derived from Git tags by `setuptools-scm`, as configured in
`pyproject.toml`; it is not maintained in a source file. Do not edit the generated
`src/simstack/_version.py`.

After the release branch has been merged into `main`, create and push an annotated
semantic-version tag. Pushing it triggers the PyPI release workflow.

```bash
git switch main
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```


## Using SimStack II

### Runner architecture: API only

Runner processes never receive a MongoDB connection string and never import a
MongoDB driver or ODM. The server is the only database owner. A runner claims,
loads, renews, completes, and reports tasks through the authenticated execution
API. There is no database or local fallback.

Issue or rotate a token for the target resource in the server, then start the
runner from a locked ``--no-dev`` environment with only its server URL and
token:

```bash
uv sync --locked --no-dev
export SIMSTACK_SERVER_URL="https://simstack.example"
export SIMSTACK_RUNNER_TOKEN="<resource-scoped-runner-token>"
.venv/bin/simstack_runner \
  --resource int-nano \
  --project-root /path/to/secret-free-source-snapshot
```

Startup fails immediately if either variable is missing, the token resource does
not match, or bootstrap cannot be loaded. Detached, Docker/Apptainer, and Slurm
workers inherit only the API and active-claim values; they do not inherit
database credentials. The execution project root must itself be a secret-free
snapshot because workflow processes can read that tree. Network isolation is a
deployment contract: configure and verify a real guard for each local,
container, Slurm, and watchdog boundary.

The runner never updates its own checkout or environment: it does not run Git,
change branches, stash files, invoke a dependency manager, or inspect a
hardcoded lock/extras file. CI or the deployment supervisor must activate a
validated source snapshot and production environment, then restart the runner.

Runner mode deliberately does not provide a generic HTTP database facade.
Legacy node or helper code that accesses `context.db` fails immediately and
must be migrated to declared task inputs/results, nested-task resolution, file
transfer, or another explicit server endpoint. Registered artifacts are rendered
and persisted by the server during the claim-bound result commit. A per-commit
ledger coordinates concurrent retries and preserves ready artifact snapshots;
parent nodes only consume children linked by the current execution attempt.
Missing server capabilities never reactivate direct MongoDB access.

### Setting Up a MongoDB Instance

SimStack II persists user data via MongoDB, so you need to have a running MongoDB instance.

In a minimal example, you can spin up a MongoDB instance on your local machine with Docker:

```bash
docker run -d \
  --name simstack-mongo \
  -p 27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=admin \
  -e MONGO_INITDB_ROOT_PASSWORD=yourpassword \
  mongo:latest
```

This command:

- Runs MongoDB in detached mode (`-d`)
- Names the container `simstack-mongo`
- Maps port 27017 to your localhost
- Sets up basic authentication with username `admin` and password `yourpassword`

**Important**: Replace `yourpassword` with a secure password and configure this
connection string on `simstack-server` only:

```dotenv
CONNECTION_STRING=mongodb://admin:yourpassword@localhost:27017/
```
You can then create databases and collections in the MongoDB instance with the `mongo` CLI.

### Starting the SimStack II Server

Start the SimStack II server as described in the [simstack-server](https://github.com/simstack/simstack-server) repo.
Set the `CONNECTION_STRING` environment variable to the connection string for the administration
database you just created. You can check localhost:8000 to see if the server is running.

### Starting the SimStack II UI

Start the SimStack II UI as described in the [simstack-ui](https://github.com/simstack/simstack-ui) repo.
You should now be able to access the UI at http://localhost:3000.






| What you’ll do                          | Why it matters                                     |
|-----------------------------------------|----------------------------------------------------|
| 1 Create a project specific environment | Keeps your system Python pristine                  |
| 2 Install simstack                      |
| 3 Clone relevant simstack base packages | Gives you ready-made tasks, sample data, and tests |

---

### Step 1 — Spin up the `simstack` environment 🐍

Choose your favorite Python **env manager**

The most modern managers are **pixi** for conda-style and **uv** for pip-style.

<details open>
<summary><strong>⬤ Recommended – Mamba (10× faster)</strong></summary>

Install **Mamba** if you don’t have it
See mamba [documentation](https://mamba.readthedocs.io/en/latest/) for Windows, Linux and macOS.

```bash
mamba create -n simstack python=3.12 -y
mamba activate simstack
```
</details>

<details>
<summary><strong>⬤ micromamba – single-file binary</strong></summary>

```bash
micromamba create -n simstack python=3.12 -y
micromamba activate simstack
```
</details>

<details>
<summary><strong>⬤ Classic conda</strong></summary>

```bash
conda create -n simstack python=3.12 -y
conda activate simstack
```
</details>

**Heads-up**: Simstack II works with Python ≥ 3.12 (CPython 64-bit).
Older versions (<3.12) may miss tomllib support and fail at runtime.

### Step 2 — Install simstack 📦

```bash
# activate the (simstack) env
python -m pip install --upgrade pip
pip install simstack

Just type `tree` in the terminal, if the **installation** succeeds, you should see a directory structure like the folder tree shown below.

### Step 3 — Clone subrepos for existing simstack packages 📦

```bash
# activate the (simstack) env
python -m pip install --upgrade pip
pip install simstack

Just type `tree` in the terminal, if the **installation** succeeds, you should see a directory structure like the folder tree shown below.



### Step 2 — Install dependencies 📦

```bash
# activate the (simstack) env
python -m pip install --upgrade pip
pip install simstack

Just type `tree` in the terminal, if the **installation** succeeds, you should see a directory structure like the folder tree shown below.





## 2. Configure Simstack II with `simstack.toml` ⚙️

Simstack II reads a **TOML** file (`simstack.toml`) to learn

* which **resources** (local & remote) exist,
* and where each host should place logs / artifacts.

Database credentials belong to the server environment and must not appear in
the runner TOML.

> **Where should the file live?**
> Save it next in the folder simstack-model in both your local and HPC accounts.
> The CLI searches those paths automatically.

### 2.1 Minimal template

```toml
#######################################
# Global / shared parameters
#######################################
[parameters.common]
resources        = ["local", "int-nano", "horeka", "justus", "self", "exchange", "uploads"]

#######################################
# Host-specific overrides
#######################################
# 1) Your own machine --------------------------------
[parameters.local]
ssh-key     = "~/.ssh/id_rsa"                      # private key
resource    = "local"                              # → maps to runners.local
workdir     = "~/simstack/workflows"               # absolute path
python_path = ["~/simstack/simstack-model",
               "~/simstack/simstack-model/src"]

# 2) Remote upload node -----------------------------
[parameters.uploads]
ssh-key     = "~/.ssh/id_rsa"
resource    = "self"
workdir     = "~/simstack/workflows"
python_path = ["~/simstack/simstack-model",
               "~/simstack/simstack-model/src"]

# 3) Example HPC login node -------------------------
[parameters.int-nano]
ssh-key            = "~/.ssh/id_rsa"
workdir            = "/home/<user>/simstack"
python_path        = ["/home/<user>/simstack/simstack-model",
                      "/home/<user>/simstack/simstack-model/src"]
environment_start  = "mamba activate simstack"  # run before each task

#######################################
# Internal web-server (rarely touched)
#######################################
[server]
port        = 8000
SECRET_KEY  = "<32-byte hex or env-var>"           # ⬚ never commit real keys
upload_dir  = "/srv/simstack/uploads"              # Windows paths OK too

#######################################
# Canonical DNS names for hosts
#######################################
[hosts]
local    = "localhost"
int-nano = "int-nano.int.kit.edu"
justus   = "justus.int.kit.edu"
horeka   = "horeka.int.kit.edu"

#######################################
# Directed data routes
#######################################
[[routes]]
source = "local"     # where the artifact lives
target = "int-nano"  # where you want it
host   = "local"     # node that **pushes** the data

[[routes]]
source = "int-nano"
target = "local"
host   = "local"

# …repeat as needed
```

## 3 Prepare *PYTHONPATH* locally & on every HPC account 📂

Add **both** the project root and its `src/` directory to `PYTHONPATH` so every Simstack II task can resolve imports no matter where it runs.

### 3.1 Create a helper script once (call it `set_pythonpath.sh`):

   ```bash
   #!/usr/bin/env bash
   # -----------------------------
   # Adds the current repo + src/ to PYTHONPATH
   # Call with:  source set_pythonpath.sh
   # -----------------------------
   this_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
   export PYTHONPATH="$this_dir:$this_dir/src${PYTHONPATH+:$PYTHONPATH}"
   echo "PYTHONPATH = $PYTHONPATH"
   ```

### 3.2 Make it executable & load it whenever you open a new shell:

```bash
chmod +x set_pythonpath.sh        # one-time
source /path/to/set_pythonpath.sh # every session, or add to ~/.bashrc
```

### 3.3 Copy the script to each HPC account (e.g. int-nano, horeka, …):

```bash
scp set_pythonpath.sh user@int-nano.int.kit.edu:~/simstack/
```

## 4. Ready, Set, Workflow! 🏁

**Mission**: prime Simstack II so your very first workflow launches without a hiccup—DB seeded, nodes known, runner humming.

### 🥇 Step 1 — Initialize the database

Simstack keeps its model and node catalogue in MongoDB. Populate (or refresh) the tables whenever you pull a new commit:

```python!
# from the repo root
cd src/simstack/utils        # ⇢ utility scripts live here
python model_table.py       # 🚀 inserts/updates the “Models” collection
python node_table.py         # 🚀 inserts/updates the “Nodes”  collection
```
### 🥈 Step 2 — Re‑register nodes (WaNos) 🔄

Any time you change a **node definition**—be it locally or on an HPC cluster—you must (re)announce it to the control plane:

```python!
# ▸ Local workstation
python src/simstack/core/node.py  # instantaneous

# ▸ On int‑nano (or another cluster head node)
ssh user@int-nano.int.kit.edu
python ~/simstack/src/simstack/core/node.py
```

Why **WaNos**?  Workflow Aware Nodes—nodes that tell Simstack exactly what they’re capable of.

### 🥉 Step 3 — Fire up the runner on int‑nano 🚀

```python!
ssh user@int-nano.int.kit.edu           # 1️⃣ log in
source ~/simstack/set_pythonpath.sh  # 2️⃣ expose src/ to PYTHONPATH
SIMSTACK_SERVER_URL=https://simstack.example \
SIMSTACK_RUNNER_TOKEN='<resource-scoped-token>' \
.venv/bin/simstack_runner \
  --resource int-nano --project-root "$PWD"  # 3️⃣ start runner
```

You should see something like:
```bash!
Runner API connection to https://simstack.example for resource int-nano
```

The runner now polls the execution API for jobs assigned to the int-nano
resource and inherits the configured Python paths.

```bash!
[Runner‑int‑nano] ⚡️  connected to broker
[Runner‑int‑nano] 💤  waiting for tasks (Ctrl‑C to exit)
```

### 5. Hands‑On: binary_operations.py 🧮

Your environment is up, let's run a real Simstack workflow on your local machine.

### 5.1 What the code does

```python!
# simplified excerpt
a, b, c = 5, 10, 2          # sample inputs
add_result      = a + b      # → 15
multiply_result = add_result * c  # → 30
print(multiply_result)
```
*Under the hood it uses a Pydantic `FloatData` document; the server persists the
result after the worker commits it through the execution API.*

### 5.2 Run it 🚀

```bash!
# stay inside your (simstack) env
cd simstack-model/examples   # 1️⃣ go to examples directory
python binary_operations.py  # 2️⃣ execute workflow script
```
Expected terminal output (the ObjectId will differ):

```bash!
id=ObjectId('680f3c149f39611649075d6a') value=30.0
```

🎉 **Congrats!** You’ve just:

 **1.** Sent inputs through Simstack’s data‑model layer

 **2.** Executed the adder ➜ multiplier chain

 **3.** Committed the final result to the server API

Try changing the numbers in AddMultiplyInput(a, b, c) and re‑running to see different results. Feel free to explore other examples in the same folder or craft your own!

### 5.4 — Run *node_example.py* on **int-nano** via Slurm 🏎️💨

> **Mission:** Run `node_example.py` workflow in **int-nano** HPC cluster and let Simstack II generate & submit the Slurm job for you.
---

#### 🔑 Prerequisites

1. **Runner up & listening on int-nano**
   ```bash
   # on the int-nano login node
   ssh user@int-nano.int.kit.edu
   source ~/simstack/set_pythonpath.sh     # expose src/ to PYTHONPATH
   SIMSTACK_SERVER_URL=https://simstack.example \
   SIMSTACK_RUNNER_TOKEN='<resource-scoped-token>' \
   .venv/bin/simstack_runner \
     --resource int-nano --project-root "$PWD"

Leave this terminal open—your runner will watch the message broker for tasks targeting int-nano.

`node_example.py` available on your workstation (it lives in simstack-model/examples).

Slurm access on int-nano (the runner will create and submit the sbatch scripts for you).

### 🚀 Launch the workflow from your local machine

```python!
# still in (simstack) and inside simstack-model/examples on the local machine
python node_example.py
```

That single command does three things behind the scenes:

1. **Creates a task through the server**, with resource="int-nano" and
   queue="slurm". Only the server owns the database document.

2. **Publishes the task through the server**, where the int-nano runner claims it
   through the execution API and then

* generates Slurm output and ``slurm_script.sh`` inside the bounded
  ``<workdir>/tasks/<task-id>/`` directory
* submits it with sbatch.

3. **Streams status** back to your local terminal until completion.

### 🖥️ Expected local console output

```bash!
task_id: 680f4ac265bb513834eeb92a created in read_db Task adder with 680f4ac265bb513834eeb92a is waiting for results
2025-04-28 11:30:47 - simstack.core.node - INFO - Task adder with task_id: 680f4ac265bb513834eeb92a completed remotely
2025-04-28 11:30:47 - simstack.core.node - INFO - Task adder with task_id: 680f4ac265bb513834eeb92a found with status TaskStatus.COMPLETED
2025-04-28 11:30:47 - simstack.core.node - INFO - Task adder with task_id: 680f4ac265bb513834eeb92a loaded outputs
```

Once you see TaskStatus.COMPLETED, the Slurm job finished on int-nano and the
runner committed the result through the server API.

Now you’ve successfully:

* Spun up a remote runner on int-nano.
* Queued a Slurm job without writing a single sbatch file yourself.
* Retrieved the output transparently through Simstack II’s data layer.

Tweak the numbers in the script, re-run, and watch new adder.sbatch files—and fresh Slurm job-IDs—appear in your adder/ folder. Enjoy the speed-up! ⚡️
