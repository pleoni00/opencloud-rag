import os
from pathlib import Path
import secrets
path = Path(__file__).resolve().parent.parent / '.env'
if not path.exists():
    values = {'OC_ADMIN_PASSWORD': secrets.token_urlsafe(24),
              'MINIO_ROOT_USER': 'opencloud-storage', 'MINIO_ROOT_PASSWORD': secrets.token_hex(24)}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        stream.write(''.join(f'{k}={v}\n' for k, v in values.items()))
print('Credenziali locali in opencloud/.env (0600).')
