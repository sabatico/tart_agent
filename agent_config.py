import os

AGENT_PORT = int(os.environ.get('AGENT_PORT', 7000))
AGENT_TOKEN = os.environ.get('AGENT_TOKEN', '')  # must match Flask server config
REGISTRY_URL = os.environ.get('REGISTRY_URL', 'registry.local:5001')
TART_BIN = os.environ.get('TART_BIN', 'tart')
WEBSOCKIFY_BIN = os.environ.get('WEBSOCKIFY_BIN', 'websockify')
VNC_PORT = int(os.environ.get('VNC_PORT', 5900))
WEBSOCKIFY_PORT_MIN = int(os.environ.get('WEBSOCKIFY_PORT_MIN', 6900))
WEBSOCKIFY_PORT_MAX = int(os.environ.get('WEBSOCKIFY_PORT_MAX', 6999))
MAX_VMS = int(os.environ.get('MAX_VMS', 2))
