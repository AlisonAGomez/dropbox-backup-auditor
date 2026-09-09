# Arquitetura e organização

## Camadas

### Entrada operacional

`auditoria.py` contém menu, rotina semanal, consolidação e geração de pacote de suporte. Ele não contém a lógica de descoberta de periodicidade ou de leitura dos eventos do Dropbox.

`executar.bat` é apenas um facilitador para Windows.

### Núcleo `auditor_bkp`

- `auditor.py`: funções comuns de configuração, Dropbox, parsing de `vzdump`, periodicidade, relatórios genéricos e utilidades.
- `auditor_arquivos.py`: auditoria incremental/completa da área Arquivos e gerenciamento do cursor correspondente.
- `auditor_vms.py`: fluxo de auditoria de VMs e busca opcional fora da árvore VMS.
- `vms_history.py`: histórico persistente de uploads/exclusões e detecção de periodicidade.
- `pdf_reports.py`: renderização dos PDFs.
- `config_validation.py`: validação estrutural e de segurança do YAML.
- `security.py`: mascaramento de segredos, proteção de CSV e utilidades de segurança.
- `status_catalog.py`: catálogo central de códigos e textos para humanos.
- `managerial_policy.py`: interpreta impacto gerencial sem alterar os códigos técnicos, reduzindo falsos positivos no consolidado.
- `integration.py`: contrato estável para o sistema integrador.
- `version.py`: fonte única da versão do código.

### Persistência local

A pasta `cache` é estado funcional do auditor. Ela não é saída descartável. `logs`, `relatorios` e `suporte` são saídas de execução.

## Dependências

O projeto usa o SDK oficial do Dropbox, PyYAML, Requests, python-dateutil e ReportLab. `requirements.txt` contém limites de versão e `pyproject.toml` permite instalar o núcleo como pacote Python.

## Princípio para integração

O novo sistema deve chamar `auditor_bkp.integration.executar_auditoria()` ou `integracao.py`. Não deve importar funções internas de `auditor_arquivos.py`/`vms_history.py` como contrato de produto, pois essas funções podem evoluir em versões futuras.

## Contratos que devem permanecer estáveis

- versão do contrato JSON (`schema_version`);
- códigos de saída 0/2/3;
- códigos técnicos de status;
- `versao` do auditor;
- significado do cache;
- nomes das variáveis de ambiente de autenticação.

Rótulos, descrições humanas e a interpretação gerencial podem evoluir sem reescrever os códigos técnicos. O template visual dos relatórios é independente dessa camada.
