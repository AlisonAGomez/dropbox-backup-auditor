# Funcionamento do Auditor Dropbox v2.5

## Objetivo

O Auditor Dropbox verifica se as rotinas de backup que deveriam chegar ao Dropbox apresentam evidências coerentes de execução. A análise é baseada em metadados disponibilizados pela API do Dropbox e em histórico local de eventos já observados.

Ele não comprova a integridade interna do conteúdo de um arquivo de backup e não substitui teste de restauração. Um arquivo presente e recente pode ser considerado evidência de execução, mas a recuperabilidade deve ser validada por política própria de restore/teste.

## 1. Descoberta das empresas

A raiz padrão é `/Aplicativos`. O auditor lista as pastas diretamente abaixo da raiz, aplica filtros informados pelo operador e consulta a seção `empresas` do `config.yaml` para saber se Arquivos e/ou VMs fazem parte do escopo.

Nomes são comparados sem diferenciar maiúsculas e minúsculas.

## 2. Auditoria de Arquivos

O modo semanal usa cursor incremental do Dropbox. O fluxo é:

1. identifica a pasta `arquivos` da empresa;
2. usa o cursor confirmado salvo em `cache/auditoria_arquivos_cursor_cache.json`;
3. solicita somente eventos posteriores ao cursor;
4. processa páginas em streaming;
5. contabiliza arquivos criados/alterados, excluídos, pastas e itens ignorados;
6. salva checkpoint durante a execução;
7. quando conclui com segurança, confirma a nova referência para a próxima rotina;
8. gera status, relatório e log.

A primeira execução de uma nova empresa pode criar apenas uma referência inicial. Nesse caso ainda não existe período anterior para comparação.

### Diagnóstico e inventário completo

O modo `diagnostico` faz uma verificação leve sem avançar a referência incremental. O modo `completo` percorre a árvore e deve ser usado de forma controlada, principalmente em ambientes muito grandes.

### Ambiente de alto volume

Uma empresa pode usar `atividade_com_rebaseline`. O objetivo é confirmar atividade recente lendo poucas páginas e recriar uma referência atual, evitando consumir um backlog de milhões de eventos. Essa política é apropriada quando a pergunta operacional é “houve atividade de backup?” e não “quantos eventos históricos existem?”.

## 3. Auditoria de VMs

O auditor procura estruturas como:

```text
VMS/pve/vm-100
VMS/pve/100
VMS/vm-100
VMS/100
```

A identificação é reforçada pelo nome padrão `vzdump`, por exemplo:

```text
vzdump-qemu-100-2026_09_08-23_00_00.vma.zst
vzdump-lxc-101-2026_09_08-23_00_00.tar.zst
```

São usados metadados como nome, tamanho, caminho e `server_modified`. O auditor não baixa o arquivo.

## 4. Arquivos divididos em chunks

Quando um backup foi dividido pelo rclone:

```text
backup.tar.zst
backup.tar.zst.rclone_chunk.001
backup.tar.zst.rclone_chunk.002
```

as partes são consolidadas como um único backup lógico. O tamanho é somado e a atividade mais recente das partes representa a conclusão observada do envio.

## 5. Periodicidade

A periodicidade pode vir de uma política manual ou ser **detectada automaticamente a partir do histórico**.

Não existe modelo de IA. O algoritmo calcula intervalos entre eventos observados, mediana, tolerâncias e consistência do ciclo. Com poucos eventos o auditor não inventa uma frequência: exibe “Coletando histórico de periodicidade”.

O histórico também aceita lacunas múltiplas do ciclo — por exemplo, 7, 14 e 7 dias podem continuar indicando comportamento semanal, com observação de possível ciclo ausente.

## 6. Exclusões e retenção

O histórico local preserva uploads observados mesmo após a rotação remover arquivos antigos do Dropbox. Exclusões podem ser detectadas por comparação de inventário ou pelos eventos incrementais e são deduplicadas para não contar a mesma remoção duas vezes.

## 7. Backups fora da árvore esperada

Sob solicitação explícita, o auditor pode procurar nomes de backup de VM fora da árvore `VMS`. A busca é feita em streaming e pode ser limitada por tempo/páginas.

## 8. Saídas

A auditoria produz:

- PDF para leitura humana;
- CSV para filtros e planilhas;
- JSON para integração e investigação;
- log técnico;
- consolidado semanal quando Arquivos + VMs são executados juntos.

Os códigos técnicos permanecem no JSON/CSV. Os PDFs e campos adicionais do JSON usam rótulos humanos.

## 9. O que o auditor não faz

- não baixa o conteúdo dos backups;
- não abre arquivos `.zst`, `.vma`, `.tar` ou similares;
- não restaura VM;
- não acessa Proxmox por SSH/API;
- não apaga ou renomeia itens no Dropbox;
- não valida conteúdo contra ransomware;
- não substitui teste de restauração;
- não utiliza IA generativa.
