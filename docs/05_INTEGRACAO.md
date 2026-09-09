# Integração com outro sistema

## Opção recomendada: biblioteca Python

```python
from auditor_bkp.integration import executar_auditoria

resultado = executar_auditoria(
    "rotina-semanal",
    config_path=r"C:\Auditor Dropbox\config.yaml",
)

print(resultado.codigo_saida)       # retorno técnico do processo
print(resultado.codigo_tecnico)     # código técnico do consolidado
print(resultado.codigo_gerencial)   # usar no dashboard de backup
print(resultado.relatorios)
print(resultado.to_json())
```

O núcleo é executado em processo separado, sem `shell`, para isolar falhas e preservar o comportamento dos códigos de saída.

## Opção por processo/CLI

```bat
.venv\Scripts\python.exe integracao.py rotina-semanal --config config.yaml
```

Saída:

```json
{
  "schema_version": "1.0",
  "versao": "2.5",
  "operacao": "rotina-semanal",
  "empresa": "",
  "codigo_saida": 0,
  "resultado": "sucesso",
  "codigo_tecnico": 0,
  "codigo_gerencial": 0,
  "resultado_gerencial": "sucesso",
  "relatorios": [],
  "logs": [],
  "mensagem": "Auditoria concluída."
}
```

A lista de artefatos é preenchida com os arquivos criados durante a execução.

## Contrato de código de saída

- `0`: normal/informativo;
- `2`: atenção;
- `3`: erro ou resultado incompleto.

Não trate `2` como falha técnica do processo. Significa que o auditor concluiu, mas encontrou algo que exige avaliação.

## Status

Os relatórios JSON mantêm o campo técnico `status`, por exemplo `SEM_BACKUP`. A v2.5 adiciona campos humanos:

```json
{
  "status": "SEM_BACKUP",
  "status_rotulo": "Sem backup válido",
  "status_nivel": "erro",
  "status_descricao": "Nenhum backup válido foi encontrado para a VM."
}
```

O sistema deve persistir o **código técnico** para diagnóstico. No dashboard gerencial da rotina semanal, use `status_geral` de cada empresa e `codigo_gerencial`. A fachada `executar_auditoria()` também devolve `codigo_tecnico`, `codigo_gerencial` e `resultado_gerencial`, sem remover `codigo_saida`. Isso evita transformar limitações de coleta em falha de backup.


### Separação técnico x gerencial

O JSON consolidado não remove nem reescreve os estados técnicos. Exemplo: uma VM pode continuar com `status_vms: "IRREGULAR, OK"`, enquanto `status_geral` fica `INFORMATIVO` se não existir atraso comprovado. Dessa forma o técnico mantém a evidência completa e o painel não gera um alerta falso.

`codigo_gerencial` usa o mesmo contrato numérico (`0`, `2`, `3`) apenas para o consolidado. Campos anteriores não foram removidos nem renomeados.

## Versionamento do schema

Os novos JSONs incluem `schema_version: "1.0"`. O integrador deve validar esse campo antes de consumir versões futuras incompatíveis.

## Configuração e credenciais

Passe o caminho do YAML por `config_path`/`--config`. Injete as credenciais no ambiente do processo:

```text
DROPBOX_APP_KEY
DROPBOX_APP_SECRET
DROPBOX_REFRESH_TOKEN
```

Nunca monte linha de comando contendo App Secret ou Refresh Token.

## Concorrência

A rotina semanal usa `cache/auditoria_em_execucao.lock` para evitar duas rotinas completas concorrentes sobre o mesmo cache. O sistema integrador deve serializar execuções que compartilhem a mesma instalação/cache.

## Timeout

A fachada aceita `timeout_segundos`, mas o ideal é permitir que os limites internos por empresa/API controlem execuções longas. Se um supervisor externo interromper a execução, consulte logs e checkpoints antes de iniciar novamente.

## Recomendações para o sistema novo

- execute sob conta de serviço dedicada;
- monte a pasta `cache` em armazenamento persistente;
- trate `logs`, `relatorios` e `suporte` como dados de execução, com retenção própria;
- injete segredos por cofre/variável de processo;
- não analise texto do terminal;
- consuma JSON e códigos técnicos;
- guarde a versão do auditor e `schema_version` junto do resultado;
- execute `validar.py` na implantação/atualização antes de liberar a rotina.
