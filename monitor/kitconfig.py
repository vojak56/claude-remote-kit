"""Settings shared by the monitor, the web monitor and push notifications.

Values come from (later wins):
  1. the defaults below,
  2. config.json in the kit folder (or the file named by CRK_CONFIG),
  3. environment variables CRK_WEB_PORT, CRK_ALLOWED_USERS, CRK_STATE_DIR.
"""
import json
import os
import shutil
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    'wezterm': 'wezterm',
    'state_dir': 'state',
    'projects_file': 'projects.txt',
    'monitor': {
        'web_port': 8790,
        'allowed_users': [],
        'push_contact': '',
        'services': [],
        'your_turn_minutes': 60,
        'context_tokens': 1_000_000,
    },
}


def _load():
    cfg = json.loads(json.dumps(DEFAULTS))
    path = Path(os.environ.get('CRK_CONFIG') or KIT_ROOT / 'config.json')
    if path.exists():
        user = json.loads(path.read_text(encoding='utf-8-sig'))
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            elif not key.startswith('_'):
                cfg[key] = value
    mon = cfg['monitor']
    if os.environ.get('CRK_WEB_PORT'):
        mon['web_port'] = int(os.environ['CRK_WEB_PORT'])
    if os.environ.get('CRK_ALLOWED_USERS'):
        mon['allowed_users'] = os.environ['CRK_ALLOWED_USERS'].split(',')
    if os.environ.get('CRK_STATE_DIR'):
        cfg['state_dir'] = os.environ['CRK_STATE_DIR']
    return cfg


def _path(value):
    p = Path(os.path.expandvars(value)).expanduser()
    return p if p.is_absolute() else KIT_ROOT / p


def _exe(value):
    """A command name is looked up on PATH; a path is used as is."""
    value = os.path.expandvars(value)
    if any(sep in value for sep in '/\\'):
        return value
    return shutil.which(value) or value


CONFIG = _load()
MONITOR = CONFIG['monitor']
STATE_DIR = _path(CONFIG['state_dir'])
PROJECTS_FILE = _path(CONFIG['projects_file'])
WEZTERM = _exe(CONFIG['wezterm'])
WEB_PORT = int(MONITOR['web_port'])
ALLOWED_USERS = {u.strip().lower() for u in MONITOR['allowed_users'] if u.strip()}
PUSH_CONTACT = MONITOR['push_contact']
SERVICES = [(s['name'], int(s['port'])) for s in MONITOR['services']]
YOUR_TURN_MINUTES = int(MONITOR['your_turn_minutes'])
CONTEXT_TOKENS = int(MONITOR['context_tokens'])
