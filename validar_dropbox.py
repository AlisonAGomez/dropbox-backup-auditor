"""Validação online, não destrutiva, das credenciais e do acesso de leitura ao Dropbox."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from auditor_bkp.auditor import carregar_config, criar_cliente_dropbox
from auditor_bkp.security import redact_text
from auditor_bkp.version import BUILD, VERSION
from auditor_bkp.release_guard import validar_politicas_criticas

ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Valida OAuth e acesso de metadados do Auditor Dropbox sem alterar arquivos ou cache."
    )
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Caminho do config.yaml")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = carregar_config(args.config)
        validar_politicas_criticas(config)
    except Exception as exc:  # noqa: BLE001
        print("[FALHA] Configuração/revisão operacional incompatível.", file=sys.stderr)
        print(redact_text(str(exc)), file=sys.stderr)
        return 3

    raiz = str(config.get("raiz_dropbox") or "/Aplicativos")
    timeout = int((config.get("dropbox") or {}).get("timeout_segundos", 60))

    logger = logging.getLogger("auditor_dropbox.validacao_online")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.INFO)

    print(f"Auditor Dropbox v{VERSION} - revisão {BUILD} - validação online não destrutiva")
    print(f"Raiz de leitura: {raiz}")
    print("[OK] Políticas operacionais críticas da revisão validadas.")

    try:
        dbx = criar_cliente_dropbox(logger, timeout_segundos=timeout)
        try:
            # Força a renovação OAuth quando necessário e valida o escopo de leitura
            # sem listar conteúdo, baixar arquivos ou alterar o cache local.
            dbx.files_list_folder_get_latest_cursor(path=raiz, recursive=False)
        finally:
            close = getattr(dbx, "close", None)
            if callable(close):
                close()
    except Exception as exc:  # noqa: BLE001
        print("[FALHA] Não foi possível autenticar/consultar metadados no Dropbox.", file=sys.stderr)
        print(redact_text(str(exc)), file=sys.stderr)
        return 3

    print("[OK] App Key/Secret/Refresh Token, HTTPS e permissão de metadados validados.")
    print("Nenhum arquivo foi baixado, enviado, alterado ou excluído; o cache local não foi modificado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
