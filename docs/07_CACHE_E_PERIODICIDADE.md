# Cache e detecção de periodicidade

## Por que o cache deve ser preservado

O cache é memória operacional do auditor. Ele permite comparar a execução atual com o que já foi observado e evita concluir frequência apenas pelo número de arquivos ainda retidos no Dropbox.

## Arquivos principais

### `cache/auditoria_arquivos_cursor_cache.json`

Mantém referências incrementais, checkpoints e resumos necessários para continuar a auditoria de Arquivos sem reler toda a árvore.

### `cache/auditoria_vms_historico.json`

Mantém uploads/exclusões observados por VM, cursores de eventos e o histórico utilizado para detectar periodicidade.

### Backups do próprio cache

Arquivos `backup_*` foram preservados porque permitem retorno/manual recovery em caso de migração ou corrupção.

### `cache/backlogs_arquivados`

Registra backlogs descartados/referências arquivadas em políticas de alto volume. Eles são evidência técnica, não relatórios de usuário.

## Detecção de periodicidade — sem IA

O algoritmo usa datas do histórico e calcula os intervalos entre backups observados. Ele compara o padrão resultante com ciclos conhecidos (diário, semanal, quinzenal e mensal) e atribui confiança de acordo com quantidade/consistência dos eventos.

Com amostra insuficiente, o status técnico legado continua `EM_APRENDIZADO` por compatibilidade, mas a interface exibe **“Coletando histórico de periodicidade”**.

A origem `historico_detectado` significa que a periodicidade foi inferida deterministicamente pelos intervalos observados. `coleta_historico` significa que ainda não há eventos suficientes.

## Reset de cache

Resetar cursor de Arquivos cria uma nova linha de base e perde a comparação incremental anterior. Faça isso somente quando:

- o cursor se tornou inválido;
- a estrutura foi deliberadamente migrada;
- o backlog anterior foi revisado e a recriação foi autorizada;
- a documentação do incidente registrar o motivo.

Não apague o histórico de VMs como rotina de manutenção. Isso reiniciaria a capacidade de detectar periodicidade.
