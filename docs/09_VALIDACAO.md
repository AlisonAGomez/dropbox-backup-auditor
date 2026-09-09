# Validação e homologação

## Validação local

Após instalar as dependências:

```bat
.venv\Scripts\python.exe validar.py
```

ou:

```bat
validar.bat
```

Se `config.yaml` ainda não existir, use o modelo público:

```bat
copy config.example.yaml config.yaml
```

`config.yaml` é ignorado pelo Git e deve conter somente a configuração operacional local, nunca segredos.

A validação verifica:

- Python mínimo e versão do pacote;
- compilação dos módulos;
- configuração e ausência de segredos no YAML;
- caches JSON, quando existirem localmente;
- dependências importáveis;
- `pip check`;
- suíte de testes automatizados;
- self-test de parsing/periodicidade;
- smoke test do lançador e da interface de integração;
- construções inseguras conhecidas no código de produção.

## Homologação com Dropbox real

Os testes automatizados não utilizam credenciais reais. Depois de configurar as variáveis de ambiente, execute:

```bat
validar_dropbox.bat
```

Esse comando faz uma consulta de metadados não destrutiva e não altera cache nem conteúdo remoto.

Para homologação funcional completa:

1. confirme o sucesso do `validar_dropbox.bat`;
2. execute uma empresa de teste ou escopo reduzido;
3. confirme que nenhum conteúdo é baixado/modificado;
4. compare o resultado com os metadados do Dropbox;
5. confirme a criação/atualização local do cache;
6. execute novamente para verificar o comportamento incremental;
7. valide a rotina semanal completa;
8. só então conecte o consumidor da interface JSON.

## Critérios para publicação no GitHub

- nenhum segredo ou configuração operacional real;
- nenhuma pasta/cache de runtime;
- nenhum log ou relatório;
- nenhum pacote de suporte;
- nenhuma `.venv` ou bytecode Python;
- somente exemplos genéricos em arquivos versionados;
- testes aprovados;
- `.gitignore` cobrindo todos os artefatos locais.
