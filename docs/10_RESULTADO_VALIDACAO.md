# Resultado da validação da versão 2.5

Data da revisão pública: **09/09/2026**.

Esta distribuição foi preparada especificamente para publicação de código-fonte. Configurações operacionais, cache, logs, relatórios, pacotes de suporte e dados de clientes foram excluídos.

## Controles executados

- versão 2.5 consistente em código e metadados;
- módulos Python compiláveis;
- **60 testes automatizados aprovados** na revisão pública;
- `config.example.yaml` validado e sem segredos;
- busca por identificadores operacionais conhecidos e padrões comuns de segredo;
- ausência de `.venv`, `__pycache__`, `.pyc`, cache, logs, relatórios e pacotes de suporte no pacote público;
- subprocessos de produção sem `shell=True`;
- ausência de `os.system`, `eval`, `exec`, desserialização `pickle`, `yaml.load` inseguro e `verify=False` no código de produção;
- proteção contra formula injection em CSV;
- mascaramento de credenciais;
- interface JSON e lançador cobertos por testes;
- autenticação real não é executada durante os testes públicos, pois exige credenciais externas.

## Homologação de produção

Depois de clonar o repositório, copie `config.example.yaml` para `config.yaml`, configure somente os dados do ambiente local e forneça as credenciais por variáveis de ambiente ou cofre de segredos. Em seguida execute `validar.bat` e `validar_dropbox.bat` antes da primeira rotina real.

O repositório público nunca deve receber o `config.yaml` operacional nem o conteúdo das pastas de runtime.
