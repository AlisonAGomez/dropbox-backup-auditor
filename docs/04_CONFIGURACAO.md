# Configuração operacional

`config.yaml` contém somente parâmetros operacionais. Credenciais são proibidas.

## Principais blocos

### `raiz_dropbox`
Raiz onde as empresas são descobertas. Padrão atual: `/Aplicativos`.

### `timezone`
Fuso usado para datas e previsões. Atual: `America/Sao_Paulo`.

### `estrutura`
Nomes das áreas `arquivos`, `VMS/pve` e prefixo de VM.

### `analise` e `tolerancias`
Quantidade mínima de eventos, tamanho de histórico exibido e tolerâncias adicionais para atraso.

### `dropbox.timeout_segundos`
Tempo máximo de espera por resposta/pacote da API antes de considerar falha transitória e aplicar retry/checkpoint.

### `auditoria_arquivos`
Controla modo incremental, tentativas, limite de páginas, tempo máximo, checkpoint, cache, heartbeat e filtros de temporários.

### `auditoria_vms`
Controla quantidade de eventos necessária para detectar periodicidade, idade máxima sem histórico suficiente e limites de buscas adicionais.

### `empresas`
Define escopo e exceções por empresa. Exemplo:

```yaml
empresas:
  EMPRESA-BACKUP:
    auditar_arquivos: true
    auditar_vms: true
    observacao: "Arquivos e VMs ativos no Dropbox."
```

Empresas migradas podem usar `estado: migrado_drive`; empresas encerradas/fora do contrato podem usar `estado: fora_escopo`. Nos dois casos `auditar_arquivos` e `auditar_vms` devem ser `false`, e a validação rejeita configuração contraditória.

Quando a ausência de alterações em Arquivos realmente precisar ser cobrada como alerta, configure explicitamente `exigir_atividade_arquivos: true`. Sem essa política, `SEM_MUDANCAS`/`SOMENTE_EXCLUSOES` são informativos. Com a política, o auditor emite `ATIVIDADE_ESPERADA_AUSENTE`.

### `politicas_vms`
Permite substituir a detecção automática por uma política explícita:

```yaml
politicas_vms:
  EMPRESA-BACKUP:
    "100":
      frequencia: semanal
      tolerancia_horas: 36
    "101":
      isento: true
      motivo: "CT fora da política de backup por decisão documentada."
```

Para uma exceção confirmada também são aceitos `obrigatorio: false`, `auditar: false` ou `estado: isento`. A exceção fica no YAML, não em regra oculta no código.

## Validação de segurança

A v2.5 recusa:

- YAML que não tenha objeto na raiz;
- campos de empresa indevidamente posicionados na raiz (erro típico de indentação);
- App Secret/Refresh Token/Access Token no YAML;
- timezone inválido;
- caminhos locais de cache absolutos ou contendo `..`;
- nome do arquivo de cache contendo subpastas;
- valores numéricos inválidos nos limites principais;
- `auditar_arquivos`/`auditar_vms` e flags gerenciais que não sejam booleanos;
- políticas de VM/CT com flags inválidas, frequência desconhecida, tolerância numérica inválida ou combinação contraditória de `isento: true` com frequência;
- estados `migrado_drive`/`fora_escopo` que ainda deixem algum módulo de auditoria habilitado.

A v2.5 detecta campos de empresa indevidamente posicionados na raiz, evitando que erros de indentação do YAML sejam aceitos silenciosamente.


## Periodicidade confirmada por VM/CT

Quando o histórico do Dropbox é irregular, o consolidado não inventa um prazo. Se a rotina real for conhecida, declare-a explicitamente:

```yaml
politicas_vms:
  EMPRESA-BACKUP:
    "100":
      frequencia: diario
      tolerancia_horas: 36
      motivo: "Rotina diária confirmada no Proxmox."
```

Valores aceitos para `frequencia`: `diario`, `semanal`, `quinzenal` e `mensal`. A política explícita transforma o acompanhamento em cálculo objetivo de `OK` ou `ATRASADO`.
