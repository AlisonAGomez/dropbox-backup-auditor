"""CLI JSON para integração do Auditor Dropbox v2.5."""
from __future__ import annotations

import argparse
import sys

from auditor_bkp.integration import executar_auditoria


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Executa o Auditor Dropbox e devolve um JSON de integração.")
    parser.add_argument("operacao", choices=["rotina-semanal", "arquivos", "vms"], nargs="?", default="rotina-semanal")
    parser.add_argument("--empresa", default="")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--modo-arquivos", choices=["semanal", "cursor", "diagnostico", "completo"], default="semanal")
    parser.add_argument("--timeout-segundos", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        resultado = executar_auditoria(
            args.operacao,
            empresa=args.empresa,
            config_path=args.config,
            modo_arquivos=args.modo_arquivos,
            timeout_segundos=args.timeout_segundos or None,
        )
    except Exception as exc:  # erro de contrato/configuração antes da execução
        print(f'{{"schema_version":"1.0","codigo_saida":3,"resultado":"erro","mensagem":{__import__("json").dumps(str(exc), ensure_ascii=False)}}}')
        return 3
    print(resultado.to_json())
    return resultado.codigo_saida


if __name__ == "__main__":
    sys.exit(main())
