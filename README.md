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

# 6. Start the agent
# Using 'python' here will automatically use the venv's python 3.14 [5]
python agent.py
# or: helper script that prepares venv + deps automatically
./run.sh
```

## Deploy helper

Use root `deploy.sh` on each node to update code and dependencies:

```bash
cd /Users/Shared/TART_Agent
chmod +x deploy.sh
./deploy.sh
```

Optional service restart:

```bash
RESTART_CMD='sudo launchctl kickstart -k system/com.tart-agent' ./deploy.sh
```

## Run helper

Use root `run.sh` for quick startup:

```bash
cd /Users/Shared/TART_Agent
chmod +x run.sh
./run.sh
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
