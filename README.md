## 1. Install Simstack II — 3-step lightning setup ⚡️

> **Goal:** create an isolated **`simstack`** environment, pull the model code, and satisfy every dependency in a single
> coffee break.

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

Simstack II reads a single **TOML** file (`simstack.toml`) to learn

* which **resources** (local & remote) exist,
* how to reach your **MongoDB** backend,
* and where each host should place logs / artifacts.

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
database         = "celso_data"                    # default DB
test_database    = "celso_test_data"               # used by `simstack selftest`
connection_string = "mongodb://<user>:<pass>@<host>:27017/"  # ⬚ change!

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
python src/simstack/core/runner.py --resource int-nano  # 3️⃣ start runner
```

You should see something like:
```bash!
2025-04-24 11:26:58 - ConfigReader - INFO - Initializing ConfigReader with resource: local on database celso_data
2025-04-24 11:26:58 - ConfigReader - INFO - workdir: /home/celso/Desktop/Project/KIT/simstack/Files/simstack_workflows
```

The runner now listens for jobs assigned to the int-nano resource and inherits the correct PYTHONPATH so your code imports flawlessly.

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
*Under the hood it uses the FloatData ODM model so the result is automatically stored in MongoDB with an ObjectId.*

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

 **3.** Persisted the final result in your configured MongoDB instance

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
   python src/simstack/core/runner.py --resource int-nano

Leave this terminal open—your runner will watch the message broker for tasks targeting int-nano.

`node_example.py` available on your workstation (it lives in simstack-model/examples).

Slurm access on int-nano (the runner will create and submit the sbatch scripts for you).

### 🚀 Launch the workflow from your local machine

```python!
# still in (simstack) and inside simstack-model/examples on the local machine
python node_example.py
```

That single command does three things behind the scenes:

1. **Creates a task document** in MongoDB with resource="int-nano", queue="slurm".

2. **Signals the int-nano runner**, which in turn

* auto-generates an `id_num.err`, `id_num.out`, and `slurm_script.sh` file inside <workdir>/adder/ (see your simstack.toml)
* submits it with sbatch.

3. **Streams status** back to your local terminal until completion.

### 🖥️ Expected local console output
    
```bash!
task_id: 680f4ac265bb513834eeb92a created in read_db Task adder with 680f4ac265bb513834eeb92a is waiting for results
2025-04-28 11:30:47 - simstack.core.node - INFO - Task adder with task_id: 680f4ac265bb513834eeb92a completed remotely
2025-04-28 11:30:47 - simstack.core.node - INFO - Task adder with task_id: 680f4ac265bb513834eeb92a found with status TaskStatus.COMPLETED
2025-04-28 11:30:47 - simstack.core.node - INFO - Task adder with task_id: 680f4ac265bb513834eeb92a loaded outputs
```

Once you see TaskStatus.COMPLETED, the Slurm job finished on int-nano and the result document was synced back to your MongoDB.

Now you’ve successfully:

* Spun up a remote runner on int-nano.
* Queued a Slurm job without writing a single sbatch file yourself.
* Retrieved the output transparently through Simstack II’s data layer.

Tweak the numbers in the script, re-run, and watch new adder.sbatch files—and fresh Slurm job-IDs—appear in your adder/ folder. Enjoy the speed-up! ⚡️