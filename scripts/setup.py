"""Create local secrets once; optionally reuse the existing 911 intake credential without printing it."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parents[1]
target = root / '.env'
if target.exists():
    print('Existing .env preserved.')
    raise SystemExit(0)

dispatch = ''
upstream = Path('/home/vince/canxp-simulator-911/.env')
if upstream.exists():
    for line in upstream.read_text().splitlines():
        if line.startswith('COMMAND_DISPATCH_TOKEN='):
            dispatch = line.split('=', 1)[1].strip().strip('\"\'')
            break

settings = {
    'POSTGRES_PASSWORD': secrets.token_hex(24),
    'TRANSIT_ADMIN_TOKEN': secrets.token_urlsafe(32),
    'TRANSIT_REPORT_TOKEN': secrets.token_urlsafe(32),
    'COMMAND_DISPATCH_TOKEN': dispatch,
    'TRANSIT_PORT': '8825', 'MCP_PORT': '8826', 'SIMULATION_SEED': '42',
    'SIMULATION_START': '2026-07-18T08:00:00-03:00', 'TICK_SECONDS': '1',
}
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as handle:
    handle.write(''.join(f'{key}={value}\n' for key, value in settings.items()))
print('Created private .env. 911 intake credential ' + ('configured.' if dispatch else 'not found; dispatch will remain blocked until configured.'))
