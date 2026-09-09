# Changelog

## v2.5 — 09/09/2026

### Organização e integração
- Núcleo reorganizado no pacote importável `auditor_bkp`.
- `auditoria.py` mantido como entrada operacional para técnicos.
- Criada fachada JSON em `auditor_bkp.integration` e `integracao.py`.
- Adicionado `schema_version: 1.0` aos contratos JSON.
- Códigos técnicos permanecem estáveis e são acompanhados por rótulo, nível e descrição humana.
- Documentação separada por funcionamento, arquitetura, autenticação, configuração, integração, segurança, cache, status e validação.

### Segurança
- Credenciais no `config.yaml` são rejeitadas.
- App Secret, Refresh Token e Access Token devem ser fornecidos por ambiente/cofre de segredos.
- Logs usam mascaramento centralizado de padrões de credencial.
- Subprocessos são executados com `shell=False`.
- Caminhos locais configuráveis são validados contra path traversal.
- CSVs neutralizam células potencialmente interpretadas como fórmulas.
- Pacotes de suporte são sanitizados e não incluem cache por padrão.
- YAML é carregado com `safe_load`.
- O fluxo OAuth solicita somente `files.metadata.read` para a operação normal.

### Robustez operacional
- Validação preventiva de erros de estrutura/indentação do YAML.
- Lançadores ajustados para o pacote `auditor_bkp`.
- Requisito atualizado para Python 3.11+ e SDK Dropbox 12.x.
- `tzdata` incluído para suporte de `zoneinfo` no Windows.
- Ambiente virtual `.venv` local usado pelos launchers Windows.
- Estados técnicos informativos foram separados de alertas gerenciais para reduzir falsos positivos.
- Periodicidade explícita por VM/CT pode substituir inferência histórica.
- Exceções por ambiente pertencem exclusivamente ao `config.yaml` local; não existem regras de clientes embutidas no código público.

### Linguagem operacional
- Textos foram ajustados para explicar “coleta de histórico” e “periodicidade detectada automaticamente”, evitando sugerir uso de IA generativa.

### Distribuição pública
- `config.yaml` real, cache, logs, relatórios, suporte, `.venv` e dados de clientes não fazem parte do repositório.
- `config.example.yaml` contém somente exemplos genéricos.
