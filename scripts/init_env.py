#!/usr/bin/env python3
"""Create local credentials without overwriting an existing configuration."""
import os
from pathlib import Path
import secrets

path = Path('.env')
if path.exists():
    print('.env já existe; configuração preservada.')
else:
    content = Path('.env.example').read_text()
    content = content.replace('SUBSTITUA_POR_UMA_SENHA_FORTE', secrets.token_urlsafe(24))
    content = content.replace('SUBSTITUA_POR_UMA_CHAVE_ALEATORIA_COM_64_CARACTERES', secrets.token_hex(32))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(content)
    print('.env criado com senha e chave aleatórias. Consulte o arquivo para fazer login.')
