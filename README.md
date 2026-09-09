# Auditor Dropbox v2.5

Auditor operacional de backups armazenados no Dropbox, desenvolvido em Python e preparado para uso manual por técnicos e para integração com outros sistemas.

A v2.5 organiza o núcleo como pacote importável (`auditor_bkp`), mantém uma entrada simples para operação (`auditoria.py` / `executar.bat`) e fornece uma fachada JSON de integração (`integracao.py` / `auditor_bkp.integration`).

> O auditor não usa inteligência artificial generativa. A periodicidade é detectada por análise determinística das datas e dos intervalos observados no histórico de backups.

## O que o auditor verifica

- atividade da área de arquivos por eventos incrementais do Dropbox;
- backups de VMs/CTs Proxmox armazenados no Dropbox;
- presença, data, tamanho e atividade mais recente dos backups;
- periodicidade configurada explicitamente ou detectada pelo histórico;
- atraso em relação à periodicidade e à tolerância configurada;
- exclusões observadas no histórico incremental;
- arquivos `vzdump` divididos em `rclone_chunk` como um único backup lógico;
- backups encontrados fora da árvore esperada, quando essa verificação é habilitada;
- ambientes fora de escopo ou migrados para outro destino;
- ambientes de alto volume por amostragem e recriação controlada de referência.

O auditor consulta metadados. Ele não baixa o conteúdo dos backups, não restaura VMs, não acessa o Proxmox por SSH e não exclui arquivos no Dropbox.

## Repositório público e dados locais

Este repositório contém somente código-fonte, documentação, testes e exemplos genéricos. Não devem ser versionados:

- `config.yaml` de produção;
- App Key, App Secret, Refresh Token ou Access Token;
- cache e cursores de auditoria;
- logs;
- relatórios PDF/CSV/JSON;
- pacotes de suporte;
- ambiente virtual `.venv`;
- arquivos de clientes ou inventários reais.

Esses itens estão cobertos pelo `.gitignore`.

## Estrutura

```text
Auditor Dropbox/
├── auditoria.py                  # entrada operacional para técnicos
├── integracao.py                 # entrada JSON para integração
├── auditor_bkp/                  # núcleo Python importável
├── docs/                         # documentação técnica e operacional
├── tests/                        # testes automatizados
├── config.example.yaml           # modelo sanitizado de configuração
├── config_politicas_vms.example.yaml
├── requirements.txt
├── pyproject.toml
├── executar.bat
├── instalar.bat
├── validar.py                    # validação local, sem API
├── validar_dropbox.py            # validação online não destrutiva
└── VERSAO.txt
```

As pastas `cache`, `logs`, `relatorios` e `suporte` são criadas/ocupadas somente durante a operação local e não fazem parte do código versionado.

## Requisitos

- Python 3.11 ou superior;
- acesso HTTPS à API do Dropbox;
- App Key, App Secret e Refresh Token fornecidos por variáveis de ambiente;
- permissão `files.metadata.read` no aplicativo Dropbox.

## Instalação no Windows

1. Clone ou extraia o repositório.
2. Execute `instalar.bat`.
3. Se `config.yaml` não existir, o instalador cria uma cópia local de `config.example.yaml`.
4. Ajuste o `config.yaml` local para o seu ambiente. Ele é ignorado pelo Git.
5. Configure as credenciais conforme `docs/03_AUTENTICACAO_DROPBOX.md`.
6. Execute `validar.bat`.
7. Execute `validar_dropbox.bat` para testar OAuth/rede sem alterar dados.
8. Execute `executar.bat`.

O `instalar.bat` cria `.venv` local e instala as dependências nesse ambiente isolado.

## Uso

Rotina semanal:

```bat
executar.bat rotina-semanal
```

Uma empresa específica:

```bat
executar.bat rotina-semanal --empresa "EMPRESA-BACKUP"
```

Integração JSON:

```bat
.venv\Scripts\python.exe integracao.py rotina-semanal --config config.yaml
```

O consumidor deve usar os campos estruturados do JSON, não interpretar o texto do terminal. A interface separa código técnico e código gerencial para permitir que o sistema integrador diferencie falhas de execução de prioridades operacionais.

## Códigos de saída

- `0`: execução normal ou somente informativa;
- `2`: existe ponto que exige conferência do técnico;
- `3`: erro, falha comprovada ou auditoria incompleta.

## Segurança

A v2.5 inclui validação de configuração, bloqueio de segredos no YAML, mascaramento de credenciais em logs, execução de subprocessos sem shell, proteção contra formula injection em CSV, validação de caminhos locais e pacote de suporte sanitizado por padrão.

Credenciais devem ser fornecidas por variáveis de ambiente ou por um cofre de segredos do sistema integrador. Nunca publique credenciais no GitHub, mesmo em repositório privado.

## Documentação

- [Funcionamento](docs/01_FUNCIONAMENTO.md)
- [Arquitetura](docs/02_ARQUITETURA.md)
- [Autenticação Dropbox](docs/03_AUTENTICACAO_DROPBOX.md)
- [Configuração](docs/04_CONFIGURACAO.md)
- [Integração](docs/05_INTEGRACAO.md)
- [Segurança](docs/06_SEGURANCA.md)
- [Cache e periodicidade](docs/07_CACHE_E_PERIODICIDADE.md)
- [Status e códigos](docs/08_STATUS_E_CODIGOS.md)
- [Validação](docs/09_VALIDACAO.md)
- [Resultado da revisão](docs/10_RESULTADO_VALIDACAO.md)

## Versão

Versão pública atual: **2.5**.
