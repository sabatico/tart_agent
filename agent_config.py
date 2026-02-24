import os


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')

AGENT_PORT = int(os.environ.get('AGENT_PORT', 7000))
AGENT_TOKEN = os.environ.get('AGENT_TOKEN', '')  # must match Flask server config
REGISTRY_URL = os.environ.get('REGISTRY_URL', 'registry.local:5001')
# Set true for plain-HTTP LAN registry endpoints.
REGISTRY_INSECURE = _env_bool('REGISTRY_INSECURE', True)
# Optional explicit host path used by Docker registry data volume.
# Example: /Users/Shared/tart-registry
REGISTRY_DATA_DIR = os.environ.get('REGISTRY_DATA_DIR', '').strip()
# Docker registry container name used for auto mount-source discovery.
REGISTRY_CONTAINER_NAME = os.environ.get('REGISTRY_CONTAINER_NAME', 'tart-registry').strip()
TART_BIN = os.environ.get('TART_BIN', 'tart')
# Default: use same Python as agent (python -m websockify), no PATH needed.
# Override with full path, e.g. WEBSOCKIFY_BIN=/path/to/websockify
WEBSOCKIFY_BIN = os.environ.get('WEBSOCKIFY_BIN', 'websockify')
VNC_PORT = int(os.environ.get('VNC_PORT', 5900))
# VNC target strategy:
# - vm_ip_first (default): use VM IP endpoint first, then localhost fallback
# - localhost_first: use localhost endpoint first, then VM IP fallback
VNC_TARGET_PREFERENCE = os.environ.get('VNC_TARGET_PREFERENCE', 'vm_ip_first').strip().lower()
WEBSOCKIFY_PORT_MIN = int(os.environ.get('WEBSOCKIFY_PORT_MIN', 6900))
WEBSOCKIFY_PORT_MAX = int(os.environ.get('WEBSOCKIFY_PORT_MAX', 6999))
MAX_VMS = int(os.environ.get('MAX_VMS', 2))
