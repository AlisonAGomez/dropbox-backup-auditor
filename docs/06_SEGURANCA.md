# Segurança

## Escopo da revisão v2.5

A v2.5 recebeu endurecimento voltado a execução local/agendada e integração. Nenhum software pode ser declarado “invulnerável”; os controles abaixo reduzem riscos conhecidos e a validação deve continuar fazendo parte do ciclo de atualização.

## Credenciais

- App Secret, Refresh Token e Access Token são segredos.
- O YAML rejeita esses valores.
- Logs passam por `RedactingFormatter`.
- Pacotes de suporte mascaram padrões conhecidos.
- A autenticação normal usa refresh token, evitando armazenar access token de curta duração.
- O aplicativo solicita `files.metadata.read`, reduzindo privilégio.

## Execução de processos

Chamadas aos módulos usam lista de argumentos e `shell=False`. Empresa, caminho de configuração e demais entradas não são interpolados em comando de shell.

O menu limpa a tela por sequência de terminal e não invoca `os.system`/shell para essa operação.

## YAML

A configuração é lida com `yaml.safe_load`. O validador impede segredos, erros comuns de indentação e caminhos locais capazes de escapar da pasta prevista por `..` ou caminho absoluto.

## CSV Formula Injection

Nomes de empresa, arquivos e caminhos vêm de dados externos. Uma célula iniciada por `=`, `+`, `-` ou `@` pode ser tratada como fórmula quando o CSV é aberto em uma planilha. A v2.5 neutraliza esses prefixos antes da gravação do CSV.

## Logs e relatórios

Logs não devem conter tokens. Exceções passam pelo mascaramento de padrões sensíveis. Relatórios contêm nomes de empresas, caminhos e metadados e, portanto, devem ser classificados como informação operacional interna.

## Pacote de suporte

Por padrão inclui configuração sanitizada, logs e relatórios. O cache não é incluído automaticamente porque pode revelar histórico e nomes de arquivos. `--incluir-cache` deve ser usado somente quando necessário.

## Cache

O cache não contém credenciais por projeto, mas contém estado operacional. Restrinja a ACL da pasta de instalação. Em POSIX, arquivos sensíveis criados diretamente pelo núcleo recebem tentativa de `0600`; no Windows, a proteção depende da ACL NTFS da pasta/conta de serviço.

## Transporte

A comunicação é realizada pelo SDK oficial do Dropbox/Requests via HTTPS. A v2.5 não desativa verificação TLS e não usa `verify=False`.

## Dependências

- Python mínimo: 3.11.
- Dropbox SDK: 12.x, mínimo 12.0.2.
- Pisos de dependências auxiliares foram elevados para as linhas verificadas na preparação da v2.5 (`PyYAML 6.0.3`, `Requests 2.32.5`, `python-dateutil 2.9.0.post0`, `urllib3 2.7.0` e `ReportLab 4.4.9`).
- Dependências têm limite de próxima major compatível em `requirements.txt`; upgrades de major devem passar por homologação antes de produção.

Antes de publicar atualização, execute `validar.py`, `pip check` e, quando disponível no pipeline, uma ferramenta de auditoria de dependências como `pip-audit`.

## Privilégio e isolamento recomendados

- conta de serviço dedicada;
- sem privilégios administrativos desnecessários;
- acesso de leitura somente ao escopo Dropbox necessário;
- pasta de instalação/cache restrita;
- segredos em cofre de credenciais do sistema novo;
- rotação imediata se token for exposto;
- retenção controlada para relatórios/logs.

## Riscos residuais

- um processo comprometido na mesma conta pode ler variáveis de ambiente/arquivos acessíveis à conta;
- metadados de backup podem ser sensíveis;
- presença de arquivo não garante que o conteúdo seja restaurável;
- interrupção abrupta pode deixar execução incompleta, embora os checkpoints reduzam perda de progresso;
- alterações futuras no Dropbox SDK/API exigem nova homologação.
