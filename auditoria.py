"""Entrada principal do Auditor Dropbox v2.5.

Este arquivo mantém uma interface simples para técnicos e delega a lógica de
negócio ao pacote ``auditor_bkp``. A integração com outros sistemas deve usar
``auditor_bkp.integration`` ou ``integracao.py``.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from auditor_bkp import pdf_reports
from auditor_bkp.managerial_policy import avaliar_registro, consolidar_avaliacoes
from auditor_bkp.config_validation import ConfigurationError, validate_config
from auditor_bkp.security import (
    RedactingFormatter,
    redact_mapping,
    redact_text,
    sanitize_csv_mapping,
    secure_file_permissions,
)
from auditor_bkp.status_catalog import info as status_info
from auditor_bkp.version import BUILD, VERSION
from auditor_bkp.release_guard import validar_politicas_criticas

ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / "cache" / "auditoria_em_execucao.lock"
MODULE_MAP = {
    "auditor.py": "auditor_bkp.auditor",
    "auditor_arquivos.py": "auditor_bkp.auditor_arquivos",
    "auditor_vms.py": "auditor_bkp.auditor_vms",
    "auditor_bkp.auditor": "auditor_bkp.auditor",
    "auditor_bkp.auditor_arquivos": "auditor_bkp.auditor_arquivos",
    "auditor_bkp.auditor_vms": "auditor_bkp.auditor_vms",
}


def resolver_config_path(caminho: str | Path) -> Path:
    path = Path(caminho)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def validar_config_antes_de_executar(caminho: str | Path) -> Path:
    path = resolver_config_path(caminho)
    if not path.exists():
        raise FileNotFoundError(f"Configuração não encontrada: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"YAML inválido em {path}: {exc}") from exc
    validate_config(data, path)
    validar_politicas_criticas(data)
    return path


def normalizar_codigo(codigo: int) -> int:
    """Contrato público: 0=OK/informativo, 2=atenção e 3=erro/incompleto."""
    return 0 if codigo == 0 else (2 if codigo == 2 else 3)


def executar_modulo(script_ou_modulo: str, argumentos: list[str]) -> int:
    """Executa um módulo do auditor sem shell e preserva o código público."""
    modulo = MODULE_MAP.get(script_ou_modulo, script_ou_modulo)
    if modulo not in set(MODULE_MAP.values()):
        raise ValueError(f"Módulo não permitido: {script_ou_modulo}")
    comando = [sys.executable, "-m", modulo, *argumentos]
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    processo = subprocess.run(comando, cwd=ROOT, env=env, check=False, shell=False)
    return normalizar_codigo(int(processo.returncode))


def adquirir_lock() -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            idade = time.time() - LOCK_PATH.stat().st_mtime
            # Compatibilidade com o comportamento histórico: locks órfãos muito
            # antigos podem ser limpos. Um processo normal da rotina não deve
            # permanecer 24h sem intervenção.
            if idade > 24 * 3600:
                LOCK_PATH.unlink(missing_ok=True)
                return adquirir_lock()
        except OSError:
            pass
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as arquivo:
        arquivo.write(
            f"pid={os.getpid()}\n"
            f"inicio={datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            f"versao={VERSION}\n"
        )
    secure_file_permissions(LOCK_PATH)
    return True


def liberar_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def configurar_log_mestre() -> tuple[logging.Logger, Path]:
    pasta = ROOT / "logs"
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"rotina_semanal_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("auditor_rotina_semanal")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(caminho, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    secure_file_permissions(caminho)
    return logger, caminho


def _json_mais_recente(padrao: str, inicio_epoch: float) -> Path | None:
    pasta = ROOT / "relatorios"
    if not pasta.exists():
        return None
    candidatos: list[Path] = []
    for path in pasta.glob(padrao):
        try:
            if path.is_file() and path.stat().st_mtime >= inicio_epoch - 2:
                candidatos.append(path)
        except OSError:
            continue
    return max(candidatos, key=lambda p: p.stat().st_mtime) if candidatos else None


def _carregar_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        valor = json.loads(path.read_text(encoding="utf-8-sig"))
        return valor if isinstance(valor, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _nivel_status(status: str) -> tuple[int, str]:
    nivel = status_info(status).nivel
    if nivel == "erro":
        return 3, "ERRO"
    if nivel == "atencao":
        return 2, "ATENCAO"
    if nivel == "informativo":
        return 1, "INFORMATIVO"
    return 0, "OK"


def gerar_relatorio_consolidado(
    inicio_epoch: float,
    codigo_arquivos: int,
    codigo_vms: int,
    config: dict[str, Any] | None = None,
) -> dict[str, Path] | None:
    arquivos_path = _json_mais_recente("auditoria_arquivos_*.json", inicio_epoch)
    vms_path = _json_mais_recente("auditoria_vms_*.json", inicio_epoch)
    arquivos = _carregar_json(arquivos_path)
    vms = _carregar_json(vms_path)
    if not arquivos and not vms:
        return None

    config = config if isinstance(config, dict) else {}

    # Consolidação case-insensitive: evita duas linhas para a mesma empresa
    # quando fontes diferentes variam apenas maiúsculas/minúsculas.
    por_empresa: dict[str, dict[str, Any]] = {}

    def adicionar(reg: dict[str, Any], area: str) -> None:
        empresa = str(reg.get("empresa") or "").strip()
        if not empresa:
            return
        chave = empresa.casefold()
        item = por_empresa.setdefault(
            chave, {"empresa": empresa, "arquivos": [], "vms": []}
        )
        # Prefere a grafia mais informativa já recebida, sem normalizar o nome
        # mostrado no template.
        if len(empresa) > len(str(item.get("empresa") or "")):
            item["empresa"] = empresa
        item[area].append(reg)

    for reg in arquivos.get("registros", []) or []:
        if isinstance(reg, dict):
            adicionar(reg, "arquivos")
    for reg in vms.get("registros", []) or []:
        if isinstance(reg, dict):
            adicionar(reg, "vms")

    linhas: list[dict[str, Any]] = []
    for item in sorted(por_empresa.values(), key=lambda x: str(x.get("empresa", "")).casefold()):
        empresa = str(item.get("empresa") or "")
        regs_arq = list(item.get("arquivos") or [])
        regs_vms = list(item.get("vms") or [])

        # O auditor de Arquivos normalmente produz uma linha por empresa. Se
        # houver mais de uma, usamos a primeira principal e ainda avaliamos
        # todas para não esconder uma condição relevante.
        reg_arq = next(
            (r for r in regs_arq if str(r.get("tipo_registro") or "").upper() in {"ARQUIVOS_EMPRESA", "EMPRESA", ""}),
            regs_arq[0] if regs_arq else {},
        )
        status_arq = str(reg_arq.get("status") or "NAO_EXECUTADO")

        avaliacoes_arq = [avaliar_registro(r, "arquivos", config) for r in regs_arq] or [
            avaliar_registro({"empresa": empresa, "status": "NAO_EXECUTADO"}, "arquivos", config)
        ]
        avaliacoes_vms = [avaliar_registro(r, "vms", config) for r in regs_vms]
        avaliacao_geral = consolidar_avaliacoes([*avaliacoes_arq, *avaliacoes_vms])
        geral = avaliacao_geral.status

        vm_regs = [r for r in regs_vms if r.get("tipo_registro") == "VM"]
        vm_avaliacoes = [(r, avaliar_registro(r, "vms", config)) for r in vm_regs]
        vms_criticas = [r for r, av in vm_avaliacoes if av.nivel == 3]
        vms_atencao = [r for r, av in vm_avaliacoes if av.nivel == 2]
        vms_info = [r for r, av in vm_avaliacoes if av.nivel == 1]

        statuses_vms = [str(r.get("status") or "") for r in regs_vms if r.get("status")]

        # A ação vem da causa de maior impacto gerencial. Ela é específica,
        # mas continua ocupando a mesma coluna e não altera o template.
        acao = avaliacao_geral.acao

        linhas.append({
            "empresa": empresa,
            "status_geral": geral,
            "status_geral_rotulo": status_info(geral).rotulo,
            "status_arquivos": status_arq,
            "status_arquivos_rotulo": status_info(status_arq).rotulo,
            "atividade_arquivos": int(reg_arq.get("arquivos_criados_ou_alterados", 0) or 0),
            "exclusoes_arquivos": int(reg_arq.get("arquivos_excluidos", 0) or 0),
            "ultima_atividade_arquivos": reg_arq.get("ultima_modificacao", ""),
            "vms_total": len(vm_regs),
            "vms_criticas": len(vms_criticas),
            "vms_atencao": len(vms_atencao),
            "vms_informativas": len(vms_info),
            "status_vms": ", ".join(sorted(set(statuses_vms))) or "NAO_EXECUTADO",
            "acao": acao,
        })

    relatorios = ROOT / "relatorios"
    relatorios.mkdir(parents=True, exist_ok=True)
    executado_em = datetime.now().astimezone()
    base = relatorios / f"auditoria_semanal_consolidada_{executado_em:%Y%m%d_%H%M%S}"
    csv_path = base.with_suffix(".csv")
    json_path = base.with_suffix(".json")
    pdf_path = base.with_suffix(".pdf")

    campos = list(linhas[0].keys()) if linhas else ["empresa", "status_geral"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as arquivo:
        writer = csv.DictWriter(arquivo, fieldnames=campos, delimiter=";")
        writer.writeheader()
        for linha in linhas:
            writer.writerow(sanitize_csv_mapping(linha))

    codigo_gerencial = max(
        ({"ERRO": 3, "ATENCAO": 2}.get(str(linha.get("status_geral") or ""), 0) for linha in linhas),
        default=0,
    )
    payload = {
        "schema_version": "1.0",
        "versao": VERSION,
        "tipo": "rotina_semanal_consolidada",
        "executado_em": executado_em.isoformat(timespec="seconds"),
        # Códigos abaixo continuam técnicos/compatíveis com a execução dos módulos.
        "codigo_arquivos": codigo_arquivos,
        "codigo_vms": codigo_vms,
        "codigo_final": max(codigo_arquivos, codigo_vms),
        # Código gerencial considera a política anti-falso-positivo do consolidado.
        "codigo_gerencial": codigo_gerencial,
        "fonte_arquivos": str(arquivos_path or ""),
        "fonte_vms": str(vms_path or ""),
        "empresas": linhas,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # O PDF consolidado é gerencial: o cartão "Codigo final" deve refletir as
    # prioridades reais do consolidado, não limitações técnicas dos módulos.
    # Os códigos técnicos continuam preservados no JSON para integração/diagnóstico.
    pdf_reports.gerar_pdf_consolidado(linhas, executado_em, pdf_path, codigo_gerencial, 0)
    return {"pdf": pdf_path, "csv": csv_path, "json": json_path}


def rotina_semanal(empresa: str = "", config_path: str | Path = "config.yaml") -> int:
    try:
        config_real = validar_config_antes_de_executar(config_path)
    except Exception as exc:  # noqa: BLE001
        print(f"ERRO de configuração: {redact_text(exc)}")
        return 3

    if not adquirir_lock():
        print(f"ERRO: já existe uma auditoria em execução. Lock: {LOCK_PATH}")
        return 3

    inicio = time.time()
    logger, log_mestre = configurar_log_mestre()
    logger.info("Rotina semanal v%s iniciada. Empresa=%s", VERSION, empresa or "TODAS")
    try:
        filtro = ["--empresa", empresa] if empresa else []
        config_arg = ["--config", str(config_real)]
        print("\n=== 1/2: Auditoria de Arquivos ===\n")
        codigo_arquivos = executar_modulo(
            "auditor_arquivos.py", [*config_arg, "--modo", "semanal", *filtro]
        )
        logger.info("Módulo Arquivos concluído com código %s", codigo_arquivos)

        print("\n=== 2/2: Auditoria de VMs ===\n")
        codigo_vms = executar_modulo("auditor_vms.py", [*config_arg, *filtro])
        logger.info("Módulo VMs concluído com código %s", codigo_vms)

        config_data = yaml.safe_load(config_real.read_text(encoding="utf-8-sig")) or {}
        gerados = gerar_relatorio_consolidado(inicio, codigo_arquivos, codigo_vms, config_data)
        if gerados:
            print("\nRelatório semanal consolidado:")
            for tipo, caminho in gerados.items():
                print(f"{tipo.upper()}: {caminho}")
                logger.info("Consolidado %s: %s", tipo, caminho)
        else:
            mensagem = "Não foi possível gerar o consolidado; os JSONs desta execução não foram localizados."
            print(f"\nATENÇÃO: {mensagem}")
            logger.error(mensagem)

        codigo_final = max(codigo_arquivos, codigo_vms)
        logger.info("Rotina semanal concluída. Código final=%s. Duração=%.2fs", codigo_final, time.time() - inicio)
        print(f"LOG MESTRE: {log_mestre}")
        return codigo_final
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha inesperada na rotina semanal: %s", redact_text(exc))
        print(f"ERRO inesperado. Consulte o log mestre: {log_mestre}")
        return 3
    finally:
        liberar_lock()


def submenu_empresa(config_path: str = "config.yaml") -> int:
    empresa = input("Nome exato da empresa: ").strip()
    if not empresa:
        return 3
    print("\n1 - Somente Arquivos (semanal)")
    print("2 - Somente VMs no Dropbox")
    print("3 - Arquivos + VMs (semanal)")
    print("4 - Diagnóstico leve de Arquivos (não avança referência)")
    print("5 - Inventário completo de Arquivos")
    opcao = input("Escolha: ").strip()
    cfg = ["--config", str(resolver_config_path(config_path))]
    if opcao == "1":
        return executar_modulo("auditor_arquivos.py", [*cfg, "--modo", "semanal", "--empresa", empresa])
    if opcao == "2":
        return executar_modulo("auditor_vms.py", [*cfg, "--empresa", empresa])
    if opcao == "3":
        return rotina_semanal(empresa, config_path)
    if opcao == "4":
        return executar_modulo("auditor_arquivos.py", [*cfg, "--modo", "diagnostico", "--empresa", empresa])
    if opcao == "5":
        paginas = input("Máximo de páginas por execução [5000]: ").strip() or "5000"
        minutos = input("Tempo máximo por execução em minutos [240]: ").strip() or "240"
        return executar_modulo(
            "auditor_arquivos.py",
            [*cfg, "--modo", "completo", "--empresa", empresa,
             "--max-paginas", paginas, "--tempo-maximo-empresa-minutos", minutos],
        )
    return 3


def submenu_diagnosticos(config_path: str = "config.yaml") -> int:
    cfg = ["--config", str(resolver_config_path(config_path))]
    print("\n1 - Diagnóstico leve de Arquivos por empresa")
    print("2 - Inventário completo de Arquivos de todas as empresas")
    print("3 - Procurar backups de VM fora da árvore VMS")
    print("4 - Recriar referência atual de Arquivos de uma empresa")
    print("5 - Voltar")
    opcao = input("Escolha: ").strip()
    if opcao == "1":
        empresa = input("Nome exato da empresa: ").strip()
        return executar_modulo("auditor_arquivos.py", [*cfg, "--modo", "diagnostico", "--empresa", empresa]) if empresa else 3
    if opcao == "2":
        confirma = input("Pode demorar muitas horas. Digite SIM para continuar: ").strip().upper()
        return executar_modulo("auditor_arquivos.py", [*cfg, "--modo", "completo"]) if confirma == "SIM" else 0
    if opcao == "3":
        return executar_modulo("auditor_vms.py", [*cfg, "--buscar-backups-fora-do-lugar"])
    if opcao == "4":
        empresa = input("Nome exato da empresa: ").strip()
        if not empresa:
            return 3
        confirma = input("A referência atual será criada e o backlog pendente será arquivado. Digite RECRIAR: ").strip().upper()
        if confirma != "RECRIAR":
            return 0
        motivo = input("Motivo [Recriação manual solicitada pelo operador]: ").strip() or "Recriação manual solicitada pelo operador."
        return executar_modulo(
            "auditor_arquivos.py",
            [*cfg, "--empresa", empresa, "--recriar-baseline", "--motivo-rebaseline", motivo],
        )
    return 0


def _zip_texto_sanitizado(zf: zipfile.ZipFile, origem: Path, destino: str) -> None:
    try:
        conteudo = origem.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return
    zf.writestr(destino, redact_text(conteudo))


def _zip_json_sanitizado(zf: zipfile.ZipFile, origem: Path, destino: str) -> None:
    try:
        data = json.loads(origem.read_text(encoding="utf-8-sig"))
        conteudo = json.dumps(redact_mapping(data), ensure_ascii=False, indent=2)
    except (OSError, json.JSONDecodeError):
        _zip_texto_sanitizado(zf, origem, destino)
        return
    zf.writestr(destino, conteudo)


def gerar_pacote_suporte(incluir_cache: bool = False) -> Path:
    """Gera pacote de diagnóstico sem credenciais por padrão.

    O cache contém nomes/caminhos e histórico operacional. Por privacidade, só
    é incluído quando solicitado explicitamente pelo técnico.
    """
    destino_dir = ROOT / "suporte"
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / f"pacote_analise_v{VERSION}_{datetime.now():%Y%m%d_%H%M%S}.zip"

    with zipfile.ZipFile(destino, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "LEIA-ME.txt",
            "Pacote de suporte do Auditor Dropbox v2.5.\n"
            "Credenciais conhecidas foram mascaradas.\n"
            f"Cache operacional incluído: {'SIM' if incluir_cache else 'NAO'}\n",
        )
        for nome in ("VERSAO.txt",):
            path = ROOT / nome
            if path.exists():
                _zip_texto_sanitizado(zf, path, nome)

        config_path = ROOT / "config.yaml"
        if config_path.exists():
            try:
                config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
                zf.writestr("config.yaml", yaml.safe_dump(redact_mapping(config), allow_unicode=True, sort_keys=False))
            except Exception:  # noqa: BLE001
                _zip_texto_sanitizado(zf, config_path, "config.yaml")

        for pasta_nome in ("logs", "relatorios"):
            pasta = ROOT / pasta_nome
            if not pasta.exists():
                continue
            for arquivo in pasta.rglob("*"):
                if not arquivo.is_file():
                    continue
                relativo = arquivo.relative_to(ROOT).as_posix()
                if arquivo.suffix.lower() in {".log", ".txt", ".csv", ".json", ".yaml", ".yml", ".md"}:
                    if arquivo.suffix.lower() == ".json":
                        _zip_json_sanitizado(zf, arquivo, relativo)
                    else:
                        _zip_texto_sanitizado(zf, arquivo, relativo)
                else:
                    # PDFs são produzidos pelo próprio auditor e não recebem
                    # credenciais; são copiados sem transformação.
                    zf.write(arquivo, relativo)

        if incluir_cache:
            pasta = ROOT / "cache"
            if pasta.exists():
                for arquivo in pasta.rglob("*"):
                    if not arquivo.is_file() or arquivo.name == LOCK_PATH.name:
                        continue
                    relativo = arquivo.relative_to(ROOT).as_posix()
                    if arquivo.suffix.lower() == ".json":
                        _zip_json_sanitizado(zf, arquivo, relativo)
                    else:
                        _zip_texto_sanitizado(zf, arquivo, relativo)

    secure_file_permissions(destino)
    return destino


def menu(config_path: str = "config.yaml") -> int:
    ultimo_codigo = 0
    while True:
        print("\033[2J\033[H", end="")  # limpa a tela sem invocar shell
        print("=" * 72)
        print(f"Auditor Dropbox v{VERSION} - revisão {BUILD}")
        print("=" * 72)
        print("1 - ROTINA SEMANAL: Arquivos + VMs (recomendado)")
        print("2 - Auditoria somente de Arquivos")
        print("3 - Auditoria somente de VMs no Dropbox")
        print("4 - Auditar uma empresa")
        print("5 - Diagnósticos e inventário")
        print("6 - Listar empresas do Dropbox")
        print("7 - Resetar referência incremental de Arquivos")
        print("8 - Gerar pacote ZIP para suporte")
        print("9 - Gerar refresh token do Dropbox")
        print("0 - Sair")
        print()
        opcao = input("Escolha: ").strip()
        cfg = ["--config", str(resolver_config_path(config_path))]

        if opcao == "0":
            return ultimo_codigo
        if opcao == "1":
            ultimo_codigo = rotina_semanal(config_path=config_path)
        elif opcao == "2":
            ultimo_codigo = executar_modulo("auditor_arquivos.py", [*cfg, "--modo", "semanal"])
        elif opcao == "3":
            ultimo_codigo = executar_modulo("auditor_vms.py", cfg)
        elif opcao == "4":
            ultimo_codigo = submenu_empresa(config_path)
        elif opcao == "5":
            ultimo_codigo = submenu_diagnosticos(config_path)
        elif opcao == "6":
            ultimo_codigo = executar_modulo("auditor_vms.py", [*cfg, "--listar-empresas"])
        elif opcao == "7":
            confirma = input("Digite RESETAR para apagar as referências incrementais de Arquivos: ").strip().upper()
            ultimo_codigo = executar_modulo("auditor_arquivos.py", [*cfg, "--resetar-cache"]) if confirma == "RESETAR" else 0
        elif opcao == "8":
            incluir = input("Incluir cache operacional no pacote? Digite SIM somente se necessário [NAO]: ").strip().upper() == "SIM"
            caminho = gerar_pacote_suporte(incluir_cache=incluir)
            print(f"Pacote gerado: {caminho}")
            ultimo_codigo = 0
        elif opcao == "9":
            ultimo_codigo = executar_modulo("auditor_vms.py", ["--gerar-refresh-token"])
        else:
            print("Opção inválida.")
            ultimo_codigo = 3

        print(f"\nCódigo de saída: {ultimo_codigo}")
        input("Pressione ENTER para voltar ao menu...")


def _valor_opcao(argumentos: list[str], nome: str, padrao: str = "") -> str:
    try:
        indice = argumentos.index(nome)
        return argumentos[indice + 1]
    except (ValueError, IndexError):
        return padrao


def _tem_opcao(argumentos: Iterable[str], nome: str) -> bool:
    return nome in argumentos


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Entrada principal do Auditor Dropbox v{VERSION}.")
    parser.add_argument(
        "comando",
        nargs="?",
        choices=["menu", "rotina-semanal", "arquivos", "vms", "listar-empresas", "resetar-cache", "pacote-suporte", "versao"],
        default="menu",
    )
    parser.add_argument("argumentos", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = _valor_opcao(args.argumentos, "--config", os.environ.get("AUDITOR_CONFIG_PATH", "config.yaml"))

    if args.comando == "versao":
        print(VERSION)
        return 0
    if args.comando == "menu":
        return menu(config)
    if args.comando == "rotina-semanal":
        empresa = _valor_opcao(args.argumentos, "--empresa", "")
        return rotina_semanal(empresa, config)
    if args.comando == "arquivos":
        argumentos = list(args.argumentos)
        if "--config" not in argumentos:
            argumentos = ["--config", str(resolver_config_path(config)), *argumentos]
        if "--modo" not in argumentos:
            argumentos += ["--modo", "semanal"]
        return executar_modulo("auditor_arquivos.py", argumentos)
    if args.comando == "vms":
        argumentos = list(args.argumentos)
        if "--config" not in argumentos:
            argumentos = ["--config", str(resolver_config_path(config)), *argumentos]
        return executar_modulo("auditor_vms.py", argumentos)
    if args.comando == "listar-empresas":
        return executar_modulo("auditor_vms.py", ["--config", str(resolver_config_path(config)), "--listar-empresas"])
    if args.comando == "resetar-cache":
        return executar_modulo("auditor_arquivos.py", ["--config", str(resolver_config_path(config)), "--resetar-cache"])
    if args.comando == "pacote-suporte":
        incluir_cache = _tem_opcao(args.argumentos, "--incluir-cache")
        print(gerar_pacote_suporte(incluir_cache=incluir_cache))
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
