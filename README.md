# Webhook Console

Plataforma em Python/Flask para cadastrar URLs de webhook, gerar credenciais e inspecionar requisições. Interface em português, sem dependências de CDN.

## Funcionalidades

- Login administrativo configurado por ambiente, sessão de 8 horas, CSRF e bloqueio por IP após 10 falhas em 15 minutos.
- Cadastro, edição, ativação e desativação de caminhos como `/teste` ou `/integracoes/pedidos`.
- Endpoints públicos ou com Bearer token ou Basic. Usuário, senha e token gerados com aleatoriedade criptográfica; segredos armazenados com hash scrypt e exibidos somente na criação/renovação.
- Renovação de credenciais e alteração do tipo de autenticação invalidam as anteriores.
- Histórico paginado com filtros por data UTC, método e autenticação; visualização de JSON/texto e download de corpos binários.
- Exclusão individual, em lote e de todo o histórico de um endpoint, com confirmação.
- Exclusão de endpoint preservando o histórico como arquivo, ou removendo tudo e liberando o caminho.
- SQLite com WAL em volume persistente, migrações Alembic e healthcheck.
- Docker Compose com Gunicorn e Caddy, HTTPS automático no domínio público.

## Iniciar localmente

Requisitos: Docker e Docker Compose 2.24.4 ou superior (`!override`). Python 3 é opcional e utilizado apenas para gerar o `.env` fora do container.

```bash
python3 scripts/init_env.py
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Sem Python instalado, gere o arquivo usando Docker:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work python:3.12-slim python scripts/init_env.py
```

Acesse **http://localhost:8090**. Consulte `ADMIN_USERNAME` e `ADMIN_PASSWORD` no `.env` para entrar. O gerador preserva configurações existentes e cria o arquivo com permissão `0600`. O `.env` nunca entra no Git nem na imagem Docker.

A porta local pode ser alterada com `LOCAL_PORT` no `.env` (padrão 8090). A configuração local expõe somente essa porta no loopback e permite cookies HTTP. A aplicação fica acessível apenas através do Caddy. O override local substitui a URL pública por `http://localhost:8090`.

Para parar:

```bash
docker compose -f compose.yaml -f compose.local.yaml down
```

## Publicar em webhook.krischanski.dev

1. No servidor, gere o `.env` e revise os valores abaixo.
2. Aponte o DNS A (e AAAA, se usar IPv6) de `webhook.krischanski.dev` para o servidor.
3. Libere as portas TCP 80 e 443 para o Caddy emitir e renovar o certificado HTTPS.
4. Suba a configuração de produção:

```bash
docker compose up -d --build
```

Acesse **https://webhook.krischanski.dev**. O Caddy redireciona HTTP para HTTPS e mantém os certificados em volume persistente. Essa publicação depende do DNS e da conectividade do servidor; iniciar em modo local não publica o domínio.

Se estiver migrando da configuração local, pare-a antes de iniciar a configuração pública para recriar o proxy com as portas corretas.

### Variáveis de ambiente

| Variável | Uso |
| --- | --- |
| `ADMIN_USERNAME` | Usuário administrativo obrigatório |
| `ADMIN_PASSWORD` | Senha administrativa obrigatória |
| `SECRET_KEY` | Chave das sessões; use 64 caracteres aleatórios |
| `PUBLIC_BASE_URL` | URL exibida nos endpoints, sem barra final |
| `SITE_ADDRESS` | Domínio usado pelo Caddy |
| `COOKIE_SECURE` | `true` em produção HTTPS |
| `MAX_BODY_BYTES` | Limite do corpo por chamada, padrão 1048576 (1 MiB) |
| `HTTP_PORT` / `HTTPS_PORT` | Portas do host, padrão 80 e 443 |

O Compose define `DATABASE_URL=sqlite:////data/webhooks.db` e `TRUST_PROXY=true` para confiar no único proxy Caddy. O Gunicorn não é exposto diretamente. Fora do Compose, só habilite `TRUST_PROXY` quando houver um proxy confiável imediatamente à frente da aplicação; por padrão ele é desabilitado.

Alterar usuário ou senha administrativa invalida as sessões existentes após recriar a aplicação:

```bash
docker compose up -d --force-recreate app
```

## Receber um webhook

No painel, clique em **Novo endpoint**, preencha nome, caminho e autenticação e salve. Copie os dados da tela de conexão; segredos não poderão ser consultados novamente. O painel permite gerar novos segredos quando necessário.

Exemplo para `/teste` com autenticação desativada:

```bash
curl -X POST https://webhook.krischanski.dev/teste \
  -H 'Content-Type: application/json' \
  -d '{"evento":"teste"}'
```

Para Bearer, adicione `-H 'Authorization: Bearer SEU_TOKEN'`. Para Basic, adicione `-u 'USUARIO:SENHA'`. O painel já apresenta o comando completo após gerar credenciais.

São aceitos GET, POST, PUT, PATCH, DELETE, OPTIONS e HEAD. A plataforma recebe e registra; não encaminha nem executa o conteúdo recebido. Os caminhos são exatos e não terminam com barra. `/admin`, `/login`, `/logout`, `/static` e `/healthz` e seus subcaminhos são reservados.

| HTTP | Resultado |
| --- | --- |
| 200 | Chamada recebida e persistida |
| 401 | Autenticação inválida, tentativa registrada |
| 404 | Endpoint inexistente, excluído ou desativado |
| 413 | Corpo maior que o limite; metadados registrados, corpo descartado |

As respostas de webhook incluem `status` e `request_id`. HEAD registra a chamada e retorna os mesmos cabeçalhos de uma resposta normal, sem corpo, conforme HTTP. Requisições para caminhos desconhecidos/desativados não entram no histórico. Falhas de armazenamento resultam em erro, sem confirmar recebimento.

Credenciais nos cabeçalhos `Authorization`, `Cookie`, `X-API-Key` e outros campos conhecidos são ocultadas no histórico, assim como parâmetros sensíveis conhecidos na query string. **O corpo original é armazenado integralmente**, podendo conter dados pessoais e segredos enviados pelo serviço de origem. O acesso ao histórico e ao download exige login. Não coloque credenciais na URL: servidores e proxies podem registrá-la em logs.

Não há expiração automática dos registros. Monitore o volume de disco e use a exclusão do painel quando necessário. Uma instalação SQLite é adequada para um único servidor e cargas pequenas/moderadas.

## Operação e backup

```bash
docker compose ps
docker compose logs --tail=100 app caddy
docker compose exec app flask --app wsgi db current
```

As migrações são executadas automaticamente antes de iniciar o Gunicorn. Para criar uma migração em desenvolvimento, utilize um checkout gravável e `flask --app wsgi db migrate -m "descricao"` após configurar o ambiente.

Backup consistente do SQLite usando a API de backup (inclui registros presentes no WAL):

```bash
docker compose exec app python -c "import sqlite3; source=sqlite3.connect('/data/webhooks.db'); target=sqlite3.connect('/data/backup.db'); source.backup(target); target.close(); source.close()"
docker compose cp app:/data/backup.db ./webhooks-backup.db
```

Para restaurar, pare app e Caddy, use um container auxiliar com o volume `webhook_data` para substituir o banco e remover seus arquivos `-wal` e `-shm`, preservando o proprietário UID/GID `10001`. Reinicie os serviços. Faça backup também do `.env` em um local protegido.

`docker compose down` preserva os volumes. **`docker compose down -v` remove os bancos e certificados permanentemente.**

## Testes

A suíte usa bancos temporários, executa as migrações reais e cobre autenticação, CSRF, limitação de login, rotação, registro, filtros, download, limites, exclusão e proteção das rotas administrativas.

```bash
docker compose run --rm --no-deps app python -m pytest -q
```

Ou fora do Docker:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

A implantação utiliza [Gunicorn](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/) e configura o [proxy confiável](https://flask.palletsprojects.com/en/stable/deploying/proxy_fix/) conforme a documentação oficial do Flask.

## Deploy pelo Jenkins com proxy existente

O [Jenkinsfile](Jenkinsfile) usa `scripts/deploy.sh` para publicar o backend no Docker do agente Jenkins. O agente precisa estar no servidor de destino, ter Docker e curl e conseguir ler `/var/env/webhook.env`. Se houver vários agentes, substitua `agent any` por um label exclusivo desse servidor. O acesso SSH ao repositório deve estar configurado no agente ou no `credentialsId` do checkout.

Neste modo, o proxy HTTPS existente deve encaminhar `webhook.krischanski.dev` para **127.0.0.1:8089**, que mapeia a porta **8000** do Gunicorn. O pipeline não inicia o Caddy do Compose. Se o proxy estiver em outro container, conecte-o à rede `webhook-network` e use `webhook-backend-container:8000` como upstream; o loopback do container do proxy não é o loopback do host.

O arquivo `/var/env/webhook.env` deve conter pelo menos `ADMIN_USERNAME`, `ADMIN_PASSWORD` e `SECRET_KEY` (32 caracteres ou mais). O deploy força a URL pública HTTPS, cookies seguros, confiança no proxy e o SQLite no volume `webhook-data` em `/data`. Não exponha a porta 8000 diretamente nem conecte containers não confiáveis à rede do proxy.

O pipeline constrói uma imagem identificada por commit/build, executa os testes sem credenciais de produção e só então troca o container. Desabilita execuções simultâneas do mesmo job, conforme as [opções oficiais do Jenkins](https://www.jenkins.io/doc/book/pipeline/syntax/). Esse job deve ser o único responsável pelo deploy desse container.

Durante a troca existe uma breve indisponibilidade. O script para o container anterior, faz backup SQLite antes das migrações e mantém o container anterior até validar o healthcheck interno e `/healthz` pela porta publicada. Se a nova versão falhar, remove o candidato, restaura o banco e reinicia o anterior. Chamadas recebidas pela versão candidata antes de uma reversão são descartadas com a restauração do backup. Interrupções abruptas do agente/host podem exigir recuperação manual; nesses casos, inspecione o container `webhook-backend-container-previous` e `/data/.deploy-backup.sqlite` antes de repetir.

Se o container atual não usar o volume esperado em `/data`, o deploy é interrompido para evitar perda silenciosa de dados. Para reutilizar os dados de uma instalação Compose, altere `DATA_VOLUME` no Jenkinsfile para o nome real do volume existente e pare a instalação anterior antes da migração. Para dados presentes apenas na camada gravável de um container antigo, faça backup e migre-os para o volume antes do primeiro deploy.
