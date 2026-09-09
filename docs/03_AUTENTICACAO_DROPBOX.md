# Autenticação no Dropbox — App Key, App Secret e Refresh Token

## Resumo

A v2.5 usa OAuth 2.0 com acesso offline por meio do SDK oficial do Dropbox.

As credenciais são fornecidas ao processo pelas variáveis:

```text
DROPBOX_APP_KEY
DROPBOX_APP_SECRET
DROPBOX_REFRESH_TOKEN
```

Para teste temporário também existe suporte a:

```text
DROPBOX_ACCESS_TOKEN
```

O `config.yaml` **não aceita** App Secret, Refresh Token ou Access Token.

## O papel de cada valor

**App Key** identifica o aplicativo criado no Dropbox Developer Console. É o identificador do cliente OAuth.

**App Secret** autentica o aplicativo durante o fluxo OAuth/renovação quando o fluxo foi criado com segredo. É confidencial.

**Refresh Token** representa uma autorização duradoura do usuário para aquele aplicativo. É confidencial, específico do app/usuário e pode ser revogado.

**Access Token** é o token de curta duração efetivamente usado nas chamadas à API. A v2.5 não precisa gravá-lo: o SDK o obtém e renova durante a execução.

## Fluxo exato usado pelo auditor

O código cria o cliente assim:

```python
dropbox.Dropbox(
    oauth2_refresh_token=refresh_token,
    app_key=app_key,
    app_secret=app_secret,
    timeout=timeout_segundos,
    user_agent="DropboxBackupAuditor/2.5",
)
```

A partir daí o SDK gerencia o access token curto. Na linha atual do SDK Python, a renovação é feita por HTTPS com um `POST` de formulário para:

```text
https://api.dropboxapi.com/oauth2/token
```

O corpo enviado pelo próprio SDK contém:

```text
grant_type=refresh_token
refresh_token=<DROPBOX_REFRESH_TOKEN>
client_id=<DROPBOX_APP_KEY>
client_secret=<DROPBOX_APP_SECRET>
```

O Dropbox devolve `access_token` e `expires_in`. O SDK mantém esse access token apenas em memória, calcula a expiração e o usa como `Authorization: Bearer ...` nas chamadas seguintes. Antes de uma chamada, se o token estiver ausente ou próximo da expiração, `check_and_refresh_access_token()` solicita outro automaticamente.

A sessão HTTP do SDK mantém verificação de certificado TLS habilitada (`verify=True`/`CERT_REQUIRED`). A v2.5 não substitui o bundle de CAs e não desativa essa validação.

Referências oficiais:

- https://developers.dropbox.com/oauth-guide
- https://dropbox.tech/developers/using-oauth-2-0-with-offline-access
- https://dropbox-sdk-python.readthedocs.io/en/latest/api/dropbox.html

## Escopo solicitado

O gerador interativo usa:

```python
DropboxOAuth2FlowNoRedirect(
    consumer_key=app_key,
    consumer_secret=app_secret,
    token_access_type="offline",
    scope=["files.metadata.read"],
)
```

Portanto, o app deve ter `files.metadata.read` habilitado no Dropbox Developer Console. O auditor usa operações de listagem, continuidade de cursor, obtenção do cursor atual e leitura de metadados. Não solicita permissão de escrita para a operação normal.


## Validar as credenciais sem executar a auditoria

Depois de instalar as dependências e configurar as três variáveis, execute:

```bat
validar_dropbox.bat
```

ou:

```bat
.venv\Scripts\python.exe validar_dropbox.py --config config.yaml
```

Esse teste força a autenticação/renovação quando necessária e solicita somente o cursor mais recente da raiz configurada usando `files.metadata.read`. Ele não baixa, envia, altera ou exclui arquivos e não modifica o cache local. O comando retorna `0` em sucesso e `3` em falha, sem imprimir as credenciais.

## Como gerar um novo Refresh Token

No menu, escolha **9 - Gerar refresh token do Dropbox**, ou execute:

```bat
py -m auditor_bkp.auditor --gerar-refresh-token
```

O fluxo:

1. solicita App Key e App Secret;
2. gera a URL de autorização;
3. o operador abre a URL e autoriza o aplicativo;
4. cola o código de autorização no terminal;
5. o SDK troca o código por access token + refresh token;
6. o refresh token é mostrado uma única vez no terminal para armazenamento seguro.

O código de autorização é descartável. Refresh Token e App Secret não devem ser colados em tickets, e-mail, relatório ou arquivo de configuração.

## Como fornecer as credenciais ao auditor

### Sessão atual do PowerShell

```powershell
$env:DROPBOX_APP_KEY = "APP_KEY"
$env:DROPBOX_APP_SECRET = "APP_SECRET"
$env:DROPBOX_REFRESH_TOKEN = "REFRESH_TOKEN"
```

Esse método vale somente para o processo atual e filhos.

### Sistema integrado — recomendado

O sistema novo deve buscar App Secret/Refresh Token no seu cofre de segredos e injetar as três variáveis no ambiente do processo do auditor. Não grave o valor no banco em texto claro nem no YAML do projeto.

### Serviço/Agendador no Windows

Use uma conta de serviço dedicada. O mecanismo que inicia o processo deve fornecer as variáveis no contexto dessa conta. Se optar por variáveis persistentes do Windows, restrinja o acesso à conta e entenda que variáveis persistentes não substituem um cofre de segredos.

## Como localizar credenciais já configuradas no Windows

Para verificar as credenciais já configuradas no ambiente de execução, use o PowerShell da **mesma conta** que executa o auditor:

```powershell
$env:DROPBOX_APP_KEY
$env:DROPBOX_APP_SECRET
$env:DROPBOX_REFRESH_TOKEN
```

Também verifique valores persistentes por usuário:

```powershell
[Environment]::GetEnvironmentVariable("DROPBOX_APP_KEY", "User")
[Environment]::GetEnvironmentVariable("DROPBOX_APP_SECRET", "User")
[Environment]::GetEnvironmentVariable("DROPBOX_REFRESH_TOKEN", "User")
```

E, se o processo era executado como serviço/agendador com configuração de máquina:

```powershell
[Environment]::GetEnvironmentVariable("DROPBOX_APP_KEY", "Machine")
[Environment]::GetEnvironmentVariable("DROPBOX_APP_SECRET", "Machine")
[Environment]::GetEnvironmentVariable("DROPBOX_REFRESH_TOKEN", "Machine")
```

Esses comandos **mostram os segredos em texto claro no console**. Use somente em uma sessão administrativa confiável e não envie captura de tela contendo os valores.

## Rotação/revogação

Se houver suspeita de exposição, revogue a autorização no Dropbox e gere um novo refresh token. Como o refresh token é específico do app, usar App Key/App Secret de outro aplicativo com o token antigo falhará.
