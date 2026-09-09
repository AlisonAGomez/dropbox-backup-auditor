# Status e códigos

A v2.5 mantém duas camadas diferentes para evitar falsos positivos sem esconder informação técnica.

## 1. Status técnico

O status técnico descreve exatamente o que o coletor observou. Ele permanece estável para diagnóstico e integração e **não é reescrito** pela camada gerencial.

| Código | Rótulo técnico | Nível técnico |
|---|---|---|
| `OK` | Sem pendências | OK |
| `ATIVIDADE_CONFIRMADA` | Atividade confirmada | OK |
| `SEM_BACKUP` | Sem backup válido | Erro |
| `ATRASADO` | Backup atrasado | Atenção |
| `ATIVIDADE_ESPERADA_AUSENTE` | Atividade esperada não observada | Atenção |
| `SEM_UPLOAD_RECENTE` | Sem upload recente | Atenção |
| `EM_APRENDIZADO` | Coletando histórico de periodicidade | Informativo |
| `IRREGULAR` | Periodicidade irregular | Informativo |
| `SEM_MUDANCAS` | Sem alterações no período | Informativo |
| `SOMENTE_EXCLUSOES` | Somente exclusões no período | Informativo |
| `INCOMPLETO` | Execução incompleta | Erro técnico da auditoria |
| `NAO_EXISTE` | Pasta não encontrada | Erro |
| `SEM_PASTA_VMS` | Pasta de VMs não encontrada | Erro técnico |
| `VM_NAO_ENCONTRADA` | VM não encontrada | Erro técnico |
| `BASELINE_CRIADA` | Referência inicial criada | Informativo |
| `BASELINE_RECRIADA_POLITICA` | Referência recriada por política | Informativo |
| `MIGRADO_PARA_DRIVE` | Fora do escopo — migrado para Google Drive | Informativo |
| `NAO_APLICAVEL` | Não aplicável / isento | Informativo |

O catálogo completo está em `auditor_bkp/status_catalog.py` e é exportável por `catalogo()`.

## 2. Status gerencial do consolidado

O relatório semanal consolidado usa `auditor_bkp/managerial_policy.py` para decidir se o estado técnico deve realmente virar `OK`, `INFORMATIVO`, `ATENCAO` ou `ERRO` para a empresa.

Regras principais:

- `EM_APRENDIZADO` não gera atenção por si só;
- `IRREGULAR` é informativo porque não há periodicidade confiável para cobrar atraso automaticamente; quando a periodicidade real for conhecida, configure-a em `politicas_vms`, fazendo o auditor calcular `OK`/`ATRASADO` objetivamente;
- `SEM_MUDANCAS` e `SOMENTE_EXCLUSOES` são informativos por padrão, porque ausência de mudança não prova falha de backup; se `exigir_atividade_arquivos: true` estiver definido para a empresa, o auditor emite `ATIVIDADE_ESPERADA_AUSENTE` e gera atenção;
- `INCOMPLETO` não é tratado como falha de backup: com evidência positiva vira informativo; sem evidência suficiente vira atenção para continuar/repetir a auditoria;
- falhas de cursor/histórico/API da auditoria viram atenção, não erro de backup, salvo quando há evidência objetiva de ausência do backup esperado;
- `SEM_PASTA_VMS` só vira erro quando a empresa está explicitamente configurada para possuir VMs no escopo; caso contrário fica atenção para confirmação;
- `VM_NAO_ENCONTRADA` vira erro quando a VM/CT é explicitamente obrigatória na política; sem política obrigatória fica atenção para confirmar remoção/renomeação;
- `SEM_BACKUP` e pasta obrigatória inexistente continuam sendo erro real;
- VM/CT marcada como isenta na política fica `NAO_APLICAVEL` e não gera falso positivo.

O template visual do relatório não depende desta lógica e permanece inalterado. O `pdf_reports.py` é validado por SHA-256 no `validar.py`; mudam apenas classificação, dados e texto da ação.

## Política de exceção por VM/CT

Exemplo:

```yaml
politicas_vms:
  EMPRESA-BACKUP:
    "101":
      isento: true
      motivo: "CT fora da política de backup por decisão documentada."
```

Também são aceitos `obrigatorio: false`, `auditar: false` ou `estado: isento`.

## Código técnico x código gerencial

No JSON consolidado:

- `codigo_arquivos`, `codigo_vms` e `codigo_final` continuam representando o resultado técnico dos módulos, para compatibilidade;
- `codigo_gerencial` representa somente o maior impacto do relatório consolidado após a política anti-falso-positivo;
- cada empresa continua expondo os códigos técnicos em `status_arquivos` e `status_vms`, além de `status_geral` para dashboard/gestão.

Status não catalogado nunca vira sucesso silencioso; a camada gerencial o mantém como atenção para revisão de compatibilidade.
