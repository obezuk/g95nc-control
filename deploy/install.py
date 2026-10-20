#!/usr/bin/env python3
"""Install/update the service for the current user. Run without sudo."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--host', help='listen address for a new install; existing config changes only if supplied')
p.add_argument('--port', type=int, help='HTTP port, default 8765')
p.add_argument('--serial', help='monitor serial from ddcutil detect; required for a new install')
p.add_argument('--start', action='store_true', help='enable and start/restart the user service')
a = p.parse_args()
if os.getuid() == 0:
    p.error('Run as the desktop user, without sudo')
source = Path(__file__).resolve().parents[1]
home = Path.home()
app = home / '.local/share/g95nc'
config_dir = home / '.config/g95nc'
unit_dir = home / '.config/systemd/user'
config_file = config_dir / 'config.json'
config = json.loads(config_file.read_text()) if config_file.exists() else {
    'host': '127.0.0.1', 'port': 8765,
    'poll_interval': 30, 'settle': 3, 'token_file': str(config_dir / 'token'),
}
for field in ('host', 'port', 'serial'):
    if getattr(a, field) is not None:
        config[field] = getattr(a, field)
if not isinstance(config.get('serial'), str) or not config['serial'].strip():
    p.error('--serial is required for a new install; find it with ddcutil detect')
for directory in (app, config_dir, unit_dir):
    directory.mkdir(parents=True, exist_ok=True)
config_dir.chmod(0o700)
for name in ('monitor.py', 'service.py'):
    shutil.copy2(source / name, app / name)
shutil.copytree(source / 'web', app / 'web', dirs_exist_ok=True)
token_file = config_dir / 'token'
if not token_file.exists():
    with open(token_file, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        f.write(secrets.token_urlsafe(36) + '\n')
token_file.chmod(0o600)
config_file.write_text(json.dumps(config, indent=2) + '\n')
config_file.chmod(0o600)
# %h is expanded by systemd. No root service or privileged hardware writes.
unit = '''[Unit]
Description=Samsung G95NC monitor API and control panel
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/.local/share/g95nc/service.py --config %h/.config/g95nc/config.json
WorkingDirectory=%h/.local/share/g95nc
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=190
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
'''
(unit_dir / 'g95nc.service').write_text(unit)
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
if a.start:
    subprocess.run(['systemctl', '--user', 'enable', 'g95nc.service'], check=True)
    subprocess.run(['systemctl', '--user', 'restart', 'g95nc.service'], check=True)
print(f'Installed: {app}')
print(f'Configuration: {config_file}')
print(f'API token file (not printed): {token_file}')
print(f'Listen address: {config["host"]}:{config["port"]}')
