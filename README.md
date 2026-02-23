# TART Agent

Small Flask HTTP service that runs on each Mac node.
Wraps `tart` CLI commands in a REST API and manages local `websockify` processes for VNC.

## Setup

```bash
# 1. Navigate to your agent directory (sibling to the main project)
# cd path/to/orchard_ui/tart_agent

# 2. Create the virtual environment
python3 -m venv venv

# 3. Activate the environment
source venv/bin/activate

# 4. Install dependencies within the venv
# This ensures pip does not attempt to modify the system Python
pip install --upgrade pip
pip install -r requirements.txt

# 5. Set mandatory environment variables
# AGENT_TOKEN must match the value in your Flask manager's .env [3, 4]
export AGENT_TOKEN="your-shared-secret"
export REGISTRY_URL="YOUR URL TO DOCKER REGISTRY"
# For plain HTTP local registries (default: true)
export REGISTRY_INSECURE="true"
# Optional: set registry storage path for accurate free-space reporting in /health
export REGISTRY_DATA_DIR="/Users/Shared/tart-registry"
# Optional: docker container name used for auto-discovery when REGISTRY_DATA_DIR is unset
export REGISTRY_CONTAINER_NAME="tart-registry"

# 6. Start the agent
# Using 'python' here will automatically use the venv's python 3.14 [5]
python agent.py
# or: helper script that prepares venv + deps automatically
./run.sh
```

## Operational git workflow (agent node)

Use once per node to connect an existing operational folder to Git while preserving local runtime data:

```bash
cd /Users/Shared
tar -czf TART_Agent_backup_$(date +%Y%m%d_%H%M%S).tar.gz TART_Agent

cd /Users/Shared/TART_Agent
mkdir -p /tmp/tart_agent_keep
cp -a .env logs /tmp/tart_agent_keep/ 2>/dev/null || true

git init
git remote add origin https://github.com/sabatico/tart_agent.git
git fetch origin
git reset --hard origin/main
git branch -M main
git branch --set-upstream-to=origin/main main

cp -a /tmp/tart_agent_keep/.env /Users/Shared/TART_Agent/ 2>/dev/null || true
cp -a /tmp/tart_agent_keep/logs /Users/Shared/TART_Agent/ 2>/dev/null || true
```

Daily update command:

```bash
cd /Users/Shared/TART_Agent
./deploy.sh
```

Optional service restart during deploy:

```bash
RESTART_CMD='sudo launchctl kickstart -k system/com.tart-agent' ./deploy.sh
```

Quick manual start:

```bash
cd /Users/Shared/TART_Agent
./run.sh
```

`run.sh` now auto-recovers stale previous agent processes on the same port (`7000` by default).
If you prefer strict behavior (do not auto-stop existing process), run with:

```bash
AUTO_STOP_EXISTING=false ./run.sh
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Node status, running VMs, free slots, disk |
| GET | `/vms` | List all local VMs |
| POST | `/vms/create` | Clone from base image |
| POST | `/vms/<name>/start` | Start VM (background) |
| POST | `/vms/<name>/stop` | Graceful shutdown |
| POST | `/vms/<name>/save` | Shutdown + push to registry (async) |
| POST | `/vms/<name>/restore` | Pull from registry + start (async) |
| GET | `/vms/<name>/op` | Poll async operation progress |
| GET | `/vms/<name>/ip` | Get VM IP address |
| DELETE | `/vms/<name>` | Delete local VM |
| POST | `/vnc/<name>/start` | Start websockify, returns port |
| POST | `/vnc/<name>/stop` | Stop websockify |

## Auth

All endpoints (except `/health`) require `Authorization: Bearer <AGENT_TOKEN>` header.
Set `AGENT_TOKEN` to the same value on both the Flask UI server and each agent node.
