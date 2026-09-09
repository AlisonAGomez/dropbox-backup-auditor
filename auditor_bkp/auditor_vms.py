from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FileMetadata

from . import auditor
from . import vms_history
from . import pdf_reports
from .version import VERSION
from .security import sanitize_csv_mapping
from .status_catalog import enriquecer_registro, codigo_saida as codigo_saida_status


CSV_COLUMNS = [
    "empresa",
    "status",
    "vm",
    "vmid",
    "ultimo_backup",
    "ultima_atualizacao_dropbox",
    "data_backup_arquivo",
    "proximo_backup_previsto",
    "atraso_horas",
    "referencia_status",
    "periodicidade_detectada",
    "confianca",
    "quantidade_backups",
    "quantidade_backups_atuais",
    "quantidade_backups_historico",
    "exclusoes_detectadas_ultima_execucao",
    "origem_periodicidade",
    "arquivo_mais_recente",
    "caminho_dropbox",
    "observacao",
]

BACKUP_FORA_LOCAL_COLUMNS = [
    "empresa",
    "vmid",
    "tipo",
    "arquivo",
    "data_backup_arquivo",
    "ultima_atividade_dropbox",
    "tamanho_gb",
    "caminho_dropbox",
    "motivo",
]


@dataclass
class RunArgs:
    config: str
    empresa: list[str]
    listar_empresas: bool
    limite_empresas: int
    limite_vms: int
    quieto: bool
    salvar_parcial_a_cada: int = 0
    incluir_arquivos: bool = False
    buscar_backups_fora_do_lugar: bool = False
    sem_varredura_backups_fora_do_lugar: bool = False  # compatibilidade com a v4
    limite_backups_fora_do_lugar: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auditoria semanal de backups de VMs no Dropbox. Gera relatorios PDF, CSV e JSON."
    )
    parser.add_argument("--config", default="config.yaml", help="Arquivo de configuracao YAML. Padrao: config.yaml")
    parser.add_argument("--empresa", action="append", default=[], help="Processa somente uma empresa. Pode repetir.")
    parser.add_argument("--listar-empresas", action="store_true", help="Lista empresas encontradas e encerra.")
    parser.add_argument("--limite-empresas", type=int, default=0, help="Limita empresas processadas. Uso para teste.")
    parser.add_argument("--limite-vms", type=int, default=0, help="Limita VMs por empresa. Uso para teste.")
    parser.add_argument("--quieto", action="store_true", help="Reduz mensagens no terminal.")
    parser.add_argument(
        "--buscar-backups-fora-do-lugar",
        action="store_true",
        help="Executa a varredura extra procurando arquivos vzdump fora das pastas VMS/PVE. Por padrao, esta etapa fica desativada para nao deixar a auditoria semanal mais lenta.",
    )
    parser.add_argument(
        "--sem-varredura-backups-fora-do-lugar",
        action="store_true",
        help="Compatibilidade com a v4. Mantido apenas para nao quebrar comandos antigos; a varredura extra ja vem desativada por padrao.",
    )
    parser.add_argument(
        "--limite-backups-fora-do-lugar",
        type=int,
        default=0,
        help="Limita a quantidade de backups fora do lugar listados no relatorio. 0 = sem limite.",
    )
    parser.add_argument("--gerar-refresh-token", action="store_true", help="Gera refresh token Dropbox e encerra.")
    parser.add_argument("--self-test", action="store_true", help="Executa self-test do auditor original e encerra.")
    return parser.parse_args()


def caminho_em_pasta_permitida_para_backup(path: str) -> bool:
    """Aceita backup somente quando ele está abaixo da árvore VMS da empresa.

    Uma pasta chamada ``pve`` fora de VMS não torna o caminho válido. Isso evita
    que ``/Aplicativos/EMPRESA/arquivos/pve`` seja tratado como local correto.
    """
    partes = [parte.strip().lower() for parte in auditor.normalizar_path(path).split("/") if parte.strip()]
    try:
        indice_vms = partes.index("vms")
    except ValueError:
        return False
    return indice_vms >= 2 and indice_vms < len(partes) - 1


def empresa_do_caminho(path: str, raiz: str) -> str:
    relativo = auditor.relativo_a_raiz(path, raiz)
    if not relativo:
        return ""
    partes = [parte for parte in relativo.split("/") if parte]
    return partes[0] if partes else ""


def encontrar_backups_fora_do_lugar(
    dbx: Any,
    raiz: str,
    tzinfo: ZoneInfo,
    logger: Any,
    stats: auditor.EstatisticasExecucao,
    quieto: bool,
    limite: int = 0,
    max_paginas: int = 0,
    tempo_maximo_segundos: int = 0,
    empresas_ignoradas: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Varre a raiz em streaming e localiza vzdump fora da árvore VMS.

    Nenhuma página completa é acumulada em memória. ``limite`` restringe apenas
    quantos resultados são guardados no relatório; a varredura continua para
    contabilizar todos os achados, salvo limite de páginas/tempo.
    """
    raiz = auditor.normalizar_path(raiz)
    if not quieto:
        print("\nVarredura final: procurando backups vzdump fora da árvore VMS...")
        print(f"  Raiz analisada: {raiz}")

    achados: list[dict[str, Any]] = []
    ignoradas = {str(nome).casefold() for nome in (empresas_ignoradas or set())}
    vistos_logicos: set[str] = set()
    meta: dict[str, Any] = {
        "paginas": 0, "metadados": 0, "total_encontrado": 0,
        "resultados_omitidos": 0, "incompleto": False, "erro": "",
    }
    inicio = time.monotonic()
    try:
        resultado = auditor.chamada_com_retry(
            dbx.files_list_folder,
            "" if raiz == "/" else raiz,
            recursive=True, include_deleted=False, limit=500, logger=logger,
        )
        while True:
            meta["paginas"] += 1
            meta["metadados"] += len(resultado.entries)
            stats.paginas_api += 1
            stats.metadados_lidos += len(resultado.entries)
            for entrada in resultado.entries:
                if not isinstance(entrada, FileMetadata):
                    continue
                nome_logico = auditor.nome_backup_logico(entrada.name)
                parsed = auditor.parse_vzdump_datetime(nome_logico, tzinfo)
                if parsed is None:
                    continue
                caminho = auditor.normalizar_path(entrada.path_display or "")
                empresa_encontrada = empresa_do_caminho(caminho, raiz)
                if empresa_encontrada.casefold() in ignoradas:
                    continue
                if auditor.eh_chunk_rclone(entrada.name):
                    caminho = caminho.rsplit("/", 1)[0] + "/" + nome_logico
                if caminho_em_pasta_permitida_para_backup(caminho):
                    continue
                chave_logica = f"{empresa_encontrada.casefold()}|{caminho.casefold()}"
                if chave_logica in vistos_logicos:
                    continue
                vistos_logicos.add(chave_logica)
                meta["total_encontrado"] += 1
                if limite > 0 and len(achados) >= limite:
                    meta["resultados_omitidos"] += 1
                    continue
                server_modified = auditor.converter_dropbox_datetime(entrada.server_modified, tzinfo)
                achados.append({
                    "empresa": empresa_encontrada,
                    "vmid": parsed.vmid,
                    "tipo": parsed.tipo,
                    "arquivo": nome_logico,
                    "data_backup_arquivo": auditor.formatar_dt(parsed.data_hora),
                    "ultima_atividade_dropbox": auditor.formatar_dt(server_modified),
                    "tamanho_gb": round((int(entrada.size or 0) / (1024**3)), 3),
                    "caminho_dropbox": caminho,
                    "motivo": "Arquivo de backup de VM encontrado fora da arvore VMS.",
                })
            if not resultado.has_more:
                break
            if max_paginas > 0 and meta["paginas"] >= max_paginas:
                meta["incompleto"] = True
                break
            if tempo_maximo_segundos > 0 and time.monotonic() - inicio >= tempo_maximo_segundos:
                meta["incompleto"] = True
                break
            resultado = auditor.chamada_com_retry(
                dbx.files_list_folder_continue, resultado.cursor, logger=logger
            )
            if not quieto:
                print(
                    f"  Página {meta['paginas'] + 1} | metadados lidos {meta['metadados']} | "
                    f"backups fora do local {meta['total_encontrado']}"
                )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha na varredura de backups fora do lugar: %s", exc)
        meta["erro"] = str(exc)

    achados.sort(key=lambda item: (str(item.get("empresa", "")).lower(), str(item.get("caminho_dropbox", "")).lower()))
    if not quieto:
        print(
            f"  Backups fora do lugar encontrados: {meta['total_encontrado']} "
            f"(exibidos: {len(achados)}, páginas: {meta['paginas']})"
        )
        if meta["incompleto"]:
            print("  ATENÇÃO: varredura interrompida por limite de páginas/tempo.")
        if meta["erro"]:
            print(f"  ERRO na varredura: {meta['erro']}")
    return achados, meta


def gerar_csv_backups_fora_do_lugar(backups: list[dict[str, Any]], caminho: Path) -> None:
    with caminho.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BACKUP_FORA_LOCAL_COLUMNS, delimiter=";")
        writer.writeheader()
        for item in backups:
            writer.writerow(sanitize_csv_mapping({col: item.get(col, "") for col in BACKUP_FORA_LOCAL_COLUMNS}))

def gerar_csv(registros: list[dict[str, Any]], caminho: Path) -> None:
    with caminho.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, delimiter=";")
        writer.writeheader()
        for reg in sorted(registros, key=auditor.ordenacao_registro):
            writer.writerow(sanitize_csv_mapping({col: reg.get(col, "") for col in CSV_COLUMNS}))


def gerar_json(registros: list[dict[str, Any]], resumo: dict[str, Any], caminho: Path, executado_em: datetime, stats: auditor.EstatisticasExecucao, backups_fora_do_lugar: list[dict[str, Any]] | None = None, meta_backups_fora: dict[str, Any] | None = None) -> None:
    payload = {
        "schema_version": "1.0",
        "versao": VERSION,
        "tipo_relatorio": "vms",
        "executado_em": auditor.formatar_dt(executado_em),
        "resumo": resumo,
        "estatisticas_execucao": {
            "paginas_api": stats.paginas_api,
            "metadados_lidos": stats.metadados_lidos,
            "empresas_processadas": stats.empresas_processadas,
            "vms_processadas": stats.vms_processadas,
            "arquivos_lidos": stats.arquivos_lidos,
        },
        "registros": [enriquecer_registro(registro) for registro in registros],
        "varredura_backups_fora_do_lugar_executada": backups_fora_do_lugar is not None,
        "backups_fora_do_lugar": backups_fora_do_lugar or [],
        "meta_backups_fora_do_lugar": meta_backups_fora or {},
    }
    caminho.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")



def gerar_relatorios_erro(mensagem: str, base_dir: Path, log_path: Path) -> dict[str, Path]:
    """Gera PDF, CSV e JSON mínimos quando a preparação da auditoria falha."""
    tzinfo = ZoneInfo("America/Sao_Paulo")
    executado_em = datetime.now(tzinfo)
    registro = {
        "tipo_registro": "EMPRESA", "empresa": "EXECUCAO", "vm": "",
        "vmid": "", "status": "ERRO", "caminho_dropbox": "-",
        "periodicidade_detectada": "", "confianca": "",
        "quantidade_backups": 0, "observacao": mensagem,
    }
    registros = [registro]
    resumo = auditor.resumo_geral(registros, 0)
    relatorios_dir = base_dir / "relatorios"
    relatorios_dir.mkdir(parents=True, exist_ok=True)
    base = relatorios_dir / f"auditoria_vms_{executado_em:%Y%m%d_%H%M%S}"
    pdf_path, csv_path, json_path = base.with_suffix(".pdf"), base.with_suffix(".csv"), base.with_suffix(".json")
    pdf_reports.gerar_pdf_vms(registros, resumo, executado_em, pdf_path)
    gerar_csv(registros, csv_path)
    gerar_json(registros, resumo, json_path, executado_em, auditor.EstatisticasExecucao())
    return {"pdf": pdf_path, "csv": csv_path, "json": json_path, "log": log_path}

def executar(config_path: str, args: argparse.Namespace) -> int:
    base_dir = Path(__file__).resolve().parent.parent
    logger, log_path = auditor.configurar_logging(base_dir)
    stats = auditor.EstatisticasExecucao()

    try:
        config = auditor.carregar_config(auditor.resolver_config_path(config_path, base_dir))
        tzinfo = ZoneInfo(config["timezone"])
        raiz = auditor.normalizar_path(config["raiz_dropbox"])
        timeout = int(config.get("dropbox", {}).get("timeout_segundos", 120))
        dbx = auditor.criar_cliente_dropbox(logger, timeout_segundos=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao preparar auditoria de VMs: %s", exc)
        mensagem = f"Falha ao preparar auditoria de VMs: {exc}"
        print(f"ERRO: {mensagem}")
        try:
            gerados = gerar_relatorios_erro(mensagem, base_dir, log_path)
            for tipo, caminho in gerados.items():
                print(f"{tipo.upper()}: {caminho}")
        except Exception as report_exc:  # noqa: BLE001
            logger.exception("Também falhou ao gerar relatório de erro: %s", report_exc)
        return 3

    try:
        empresas = auditor.listar_empresas(
            dbx=dbx,
            raiz=raiz,
            logger=logger,
            stats=stats,
            empresas_filtradas=args.empresa,
            limite_empresas=args.limite_empresas,
            quieto=args.quieto,
        )
    except (AuthError, ApiError) as exc:
        mensagem = f"Falha ao listar empresas no Dropbox: {exc}"
        logger.exception(mensagem)
        print(f"ERRO: {mensagem}")
        try:
            gerados = gerar_relatorios_erro(mensagem, base_dir, log_path)
            for tipo, caminho in gerados.items():
                print(f"{tipo.upper()}: {caminho}")
        except Exception as report_exc:  # noqa: BLE001
            logger.exception("Também falhou ao gerar relatório de erro: %s", report_exc)
        return 3

    if args.listar_empresas:
        for idx, empresa in enumerate(empresas, start=1):
            print(f"{idx:03d}. {empresa}")
        return 0

    run_args = RunArgs(
        config=args.config,
        empresa=args.empresa,
        listar_empresas=args.listar_empresas,
        limite_empresas=args.limite_empresas,
        limite_vms=args.limite_vms,
        quieto=args.quieto,
    )

    config.setdefault("performance", {})["incluir_arquivos_por_padrao"] = False

    print(f"Auditoria de VMs v{VERSION} iniciada. Empresas: {len(empresas)}")
    logger.info("Auditoria de VMs v%s iniciada. empresas=%s", VERSION, len(empresas))
    registros = auditor.processar_empresas_modo_rapido(
        dbx=dbx,
        empresas=empresas,
        config=config,
        tzinfo=tzinfo,
        logger=logger,
        stats=stats,
        args=run_args,
        base_dir=base_dir,
    )
    registros = [r for r in registros if r.get("tipo_registro") in {"VM", "EMPRESA"}]
    registros = vms_history.aplicar_historico_vms(
        registros=registros,
        dbx=dbx,
        config=config,
        tzinfo=tzinfo,
        base_dir=base_dir,
        logger=logger,
        detectar_ausentes=not bool(args.limite_vms),
    )

    config_varredura = bool(config.get("auditoria_vms", {}).get("varrer_backups_fora_do_lugar", False))
    varredura_habilitada = bool(getattr(args, "buscar_backups_fora_do_lugar", False)) or config_varredura
    if bool(getattr(args, "sem_varredura_backups_fora_do_lugar", False)):
        varredura_habilitada = False
    backups_fora_do_lugar: list[dict[str, Any]] | None = None
    meta_backups_fora: dict[str, Any] | None = None
    if varredura_habilitada:
        cfg_vms = config.get("auditoria_vms", {}) if isinstance(config.get("auditoria_vms"), dict) else {}
        backups_fora_do_lugar, meta_backups_fora = encontrar_backups_fora_do_lugar(
            dbx=dbx,
            raiz=raiz,
            tzinfo=tzinfo,
            logger=logger,
            stats=stats,
            quieto=args.quieto,
            limite=int(getattr(args, "limite_backups_fora_do_lugar", 0) or 0),
            max_paginas=int(cfg_vms.get("max_paginas_backups_fora_do_lugar", 0) or 0),
            tempo_maximo_segundos=int(cfg_vms.get("tempo_maximo_backups_fora_do_lugar_minutos", 60) or 0) * 60,
            empresas_ignoradas={
                nome for nome in empresas
                if not auditor.empresa_audita(config, nome, "vms")
            },
        )
    else:
        print("Varredura extra de backups fora das pastas VMS/PVE nao executada. Use --buscar-backups-fora-do-lugar quando quiser rodar essa checagem.")

    resumo = auditor.resumo_geral(registros, len(empresas))

    relatorios_dir = base_dir / "relatorios"
    relatorios_dir.mkdir(parents=True, exist_ok=True)
    executado_em = datetime.now(tzinfo)
    sufixo = executado_em.strftime("%Y%m%d_%H%M%S")
    pdf_path = relatorios_dir / f"auditoria_vms_{sufixo}.pdf"
    csv_path = relatorios_dir / f"auditoria_vms_{sufixo}.csv"
    csv_fora_path = relatorios_dir / f"auditoria_vms_backups_fora_do_lugar_{sufixo}.csv"
    json_path = relatorios_dir / f"auditoria_vms_{sufixo}.json"

    pdf_reports.gerar_pdf_vms(registros, resumo, executado_em, pdf_path, backups_fora_do_lugar, meta_backups_fora)
    gerar_csv(registros, csv_path)
    if backups_fora_do_lugar is not None:
        gerar_csv_backups_fora_do_lugar(backups_fora_do_lugar, csv_fora_path)
    gerar_json(registros, resumo, json_path, executado_em, stats, backups_fora_do_lugar, meta_backups_fora)

    print("\nAuditoria de VMs concluida.")
    print(f"PDF:  {pdf_path}")
    print(f"CSV:  {csv_path}")
    if backups_fora_do_lugar is not None:
        print(f"CSV backups fora do lugar: {csv_fora_path}")
    print(f"JSON: {json_path}")
    print(f"Log:  {log_path}")
    print(f"VMs OK: {resumo.get('vms_ok', 0)} | Atrasadas: {resumo.get('vms_atrasadas', 0)} | Sem backup: {resumo.get('vms_sem_backup', 0)} | Erros: {resumo.get('vms_erro', 0)}")
    if backups_fora_do_lugar is not None:
        print(f"Backups fora da árvore VMS: {int((meta_backups_fora or {}).get('total_encontrado', len(backups_fora_do_lugar)))}")
    else:
        print("Backups fora das pastas VMS/PVE: nao verificado nesta execucao")
    statuses = {str(reg.get("status") or "") for reg in registros}
    if bool((meta_backups_fora or {}).get("erro")) or bool((meta_backups_fora or {}).get("incompleto")):
        codigo = 3
    else:
        codigo = codigo_saida_status(statuses)
        if codigo == 0 and int((meta_backups_fora or {}).get("total_encontrado", 0) or 0) > 0:
            codigo = 2
    logger.info("Auditoria de VMs concluida. codigo=%s statuses=%s", codigo, sorted(statuses))
    return codigo


def main() -> int:
    if not auditor.validar_versao_python():
        return 3
    args = parse_args()
    if args.gerar_refresh_token:
        return auditor.gerar_refresh_token_interativo()
    if args.self_test:
        return auditor.executar_self_test()
    return executar(args.config, args)


if __name__ == "__main__":
    raise SystemExit(main())
