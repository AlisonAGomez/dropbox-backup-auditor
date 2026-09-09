from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import logging
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dropbox.exceptions import ApiError, AuthError
from dropbox.files import DeletedMetadata, FileMetadata, FolderMetadata

from . import auditor
from . import pdf_reports
from .version import VERSION
from .security import sanitize_csv_mapping
from .status_catalog import enriquecer_registro, codigo_saida as codigo_saida_status

CACHE_VERSION = 3


@dataclass
class ResumoDelta:
    arquivos_criados_ou_alterados: int = 0
    arquivos_excluidos: int = 0
    pastas_alteradas: int = 0
    itens_ignorados: int = 0
    bytes_alterados: int = 0
    ultima_modificacao: str = ""
    ultimo_arquivo: str = ""
    caminho_ultimo_arquivo: str = ""
    amostras: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Any) -> "ResumoDelta":
        if not isinstance(value, dict):
            return cls()
        campos = {f.name for f in cls.__dataclass_fields__.values()}
        dados = {k: v for k, v in value.items() if k in campos}
        if not isinstance(dados.get("amostras", []), list):
            dados["amostras"] = []
        return cls(**dados)

    def registrar_amostra(self, item: dict[str, Any], limite: int) -> None:
        if limite <= 0:
            return
        chave = (str(item.get("tipo", "")), str(item.get("caminho", "")))
        existentes = {
            (str(amostra.get("tipo", "")), str(amostra.get("caminho", "")))
            for amostra in self.amostras
        }
        if chave in existentes:
            return
        self.amostras.append(item)
        if len(self.amostras) > limite:
            self.amostras = self.amostras[-limite:]


@dataclass
class ResumoCompleto:
    arquivos: int = 0
    pastas: int = 0
    tamanho_total_bytes: int = 0
    ultima_modificacao: str = ""
    ultimo_arquivo: str = ""
    caminho_ultimo_arquivo: str = ""
    top_level: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Any) -> "ResumoCompleto":
        if not isinstance(value, dict):
            return cls()
        campos = {f.name for f in cls.__dataclass_fields__.values()}
        dados = {k: v for k, v in value.items() if k in campos}
        if not isinstance(dados.get("top_level", {}), dict):
            dados["top_level"] = {}
        return cls(**dados)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auditoria rápida da pasta Arquivos usando cursor do Dropbox e inventário completo sob demanda."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--empresa", action="append", default=[])
    parser.add_argument("--listar-empresas", action="store_true")
    parser.add_argument("--limite-empresas", type=int, default=0)
    parser.add_argument("--modo", choices=["semanal", "cursor", "completo", "diagnostico"], default="semanal")
    parser.add_argument("--resetar-cache", action="store_true")
    parser.add_argument("--max-paginas", type=int, default=0)
    parser.add_argument("--tempo-maximo-empresa-minutos", type=int, default=0)
    parser.add_argument("--tentativas-dropbox", type=int, default=0)
    parser.add_argument("--nao-retomar", action="store_true")
    parser.add_argument("--quieto", action="store_true")
    parser.add_argument("--gerar-refresh-token", action="store_true")
    parser.add_argument("--recriar-baseline", action="store_true", help="Cria um cursor atual sem inventariar a arvore.")
    parser.add_argument("--motivo-rebaseline", default="Recriacao manual solicitada pelo operador.")
    return parser.parse_args()


def base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def agora_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def carregar_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"versao": CACHE_VERSION, "empresas": {}}
    try:
        cache = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        corrompido = path.with_suffix(path.suffix + f".corrompido_{datetime.now():%Y%m%d_%H%M%S}")
        try:
            path.replace(corrompido)
        except OSError:
            pass
        return {"versao": CACHE_VERSION, "empresas": {}}

    if not isinstance(cache, dict):
        return {"versao": CACHE_VERSION, "empresas": {}}
    try:
        versao_original = int(cache.get("versao", 1) or 1)
    except (TypeError, ValueError):
        versao_original = 1
    if versao_original < CACHE_VERSION:
        backup = path.with_name(f"{path.stem}.backup_v{versao_original}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        if not backup.exists():
            try:
                shutil.copy2(path, backup)
            except OSError:
                pass
    empresas = cache.setdefault("empresas", {})
    if not isinstance(empresas, dict):
        cache["empresas"] = empresas = {}

    # Migração sem apagar os cursores existentes da v12.
    for registro in empresas.values():
        if not isinstance(registro, dict):
            continue
        if registro.get("cursor") and not registro.get("cursor_confirmado"):
            registro["cursor_confirmado"] = registro.get("cursor")
            registro["cursor_inclui_exclusoes"] = False
            registro["migrado_da_v12"] = True
        # Checkpoint completo da v12 não tinha os agregados das páginas anteriores;
        # não é seguro retomá-lo nas versões atuais.
        if registro.get("full_scan_cursor"):
            registro["checkpoint_v12_descartado"] = True
        if registro.get("politica_backlog_v13_3_aplicada") and not registro.get("politica_backlog_alto_volume_aplicada"):
            registro["politica_backlog_alto_volume_aplicada"] = True
            registro.pop("politica_backlog_v13_3_aplicada", None)
        for campo in (
            "full_scan_cursor",
            "full_scan_paginas",
            "full_scan_itens",
            "full_scan_ultima_atualizacao",
            "full_scan_path",
        ):
            registro.pop(campo, None)
    cache["versao"] = CACHE_VERSION
    return cache


def salvar_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def chave_cache(empresa: str, pasta: str) -> str:
    return f"{empresa.lower()}|{auditor.normalizar_path(pasta).lower()}"


def dt_iso(value: datetime | None, tzinfo: ZoneInfo) -> str:
    convertido = auditor.converter_dropbox_datetime(value, tzinfo)
    return auditor.formatar_dt(convertido)


def dt_obj(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def atualizar_mais_recente(
    resumo: ResumoDelta | ResumoCompleto,
    data_iso: str,
    nome: str,
    caminho: str,
) -> None:
    atual = dt_obj(getattr(resumo, "ultima_modificacao", ""))
    candidato = dt_obj(data_iso)
    if candidato is None:
        return
    if atual is None or candidato > atual:
        resumo.ultima_modificacao = data_iso
        resumo.ultimo_arquivo = nome
        resumo.caminho_ultimo_arquivo = caminho


def config_arquivos(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config.get("auditoria_arquivos", {}) or {})
    cfg.setdefault("max_paginas_incremental_por_empresa", int(cfg.get("max_paginas_semanais", 80)))
    cfg.setdefault("max_paginas_completo_por_empresa", 200)
    cfg.setdefault("tempo_maximo_empresa_minutos", 15)
    cfg.setdefault("tentativas_dropbox", 6)
    cfg.setdefault("checkpoint_a_cada_paginas", 1)
    cfg.setdefault("amostras_por_empresa", 5)
    cfg.setdefault("recuperar_cursor_invalido_automaticamente", True)
    cfg.setdefault("ignorar_nomes", [".DS_Store", "Thumbs.db", "desktop.ini"])
    cfg.setdefault("ignorar_extensoes", [".tmp", ".temp", ".part", ".partial", ".lock", ".crdownload"])
    cfg.setdefault("ignorar_prefixos", ["~$", ".~lock."])
    cfg.setdefault("heartbeat_arquivo", "")
    cfg.setdefault("heartbeat_max_idade_horas", int(config.get("analise", {}).get("arquivos_max_idade_horas", 168)))
    cfg.setdefault("cache_dir", "cache")
    cfg.setdefault("cache_cursor_arquivo", "auditoria_arquivos_cursor_cache.json")
    return cfg


def config_arquivos_empresa(config: dict[str, Any], empresa: str, cfg_global: dict[str, Any]) -> dict[str, Any]:
    """Mescla as regras globais com a politica especifica da empresa."""
    resultado = dict(cfg_global)
    empresas = config.get("empresas", {}) if isinstance(config.get("empresas"), dict) else {}
    empresa_cfg = next((v for k, v in empresas.items() if str(k).lower() == empresa.lower()), {})
    if isinstance(empresa_cfg, dict):
        local = empresa_cfg.get("auditoria_arquivos", {})
        if isinstance(local, dict):
            resultado.update(local)
    resultado.setdefault("estrategia", "incremental")
    resultado.setdefault("max_paginas_amostragem", 3)
    resultado.setdefault("rebaseline_apos_confirmacao", True)
    resultado.setdefault("rebaseline_mesmo_sem_atividade", False)
    resultado.setdefault("abandonar_checkpoint_existente", False)
    resultado.setdefault("bloquear_inventario_completo", False)
    return resultado


def _nome_seguro(value: str) -> str:
    limpo = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "empresa")).strip("_")
    return limpo or "empresa"


def arquivar_backlog(
    cache_path: Path,
    empresa: str,
    estado: dict[str, Any],
    motivo: str,
) -> Path | None:
    """Preserva cursor e resumo descartados em um JSON separado para auditoria."""
    pendente = estado.get("pendente") if isinstance(estado.get("pendente"), dict) else None
    if not pendente and not estado.get("cursor_confirmado") and not estado.get("cursor"):
        return None
    destino_dir = cache_path.parent / "backlogs_arquivados"
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / f"{_nome_seguro(empresa)}_{datetime.now():%Y%m%d_%H%M%S}.json"
    payload = {
        "schema_version": "1.0",
        "versao": VERSION,
        "empresa": empresa,
        "arquivado_em": agora_iso(),
        "motivo": motivo,
        "cursor_confirmado_anterior": estado.get("cursor_confirmado") or estado.get("cursor") or "",
        "cursor_migracao_exclusoes": estado.get("cursor_migracao_exclusoes", ""),
        "pendente": pendente or {},
        "ultimo_status": estado.get("ultimo_status", ""),
    }
    destino.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return destino


def recriar_baseline_sem_inventario(
    dbx: Any,
    empresa: str,
    pasta: str,
    estado: dict[str, Any],
    cache: dict[str, Any],
    cache_path: Path,
    logger: logging.Logger,
    tentativas: int,
    motivo: str,
    status: str = "BASELINE_RECRIADA_POLITICA",
) -> tuple[str, Path | None, dict[str, Any]]:
    pendente = estado.get("pendente") if isinstance(estado.get("pendente"), dict) else {}
    resumo = ResumoDelta.from_dict(pendente.get("resumo"))
    paginas_arquivadas = int(pendente.get("paginas", 0) or 0)
    arquivo = arquivar_backlog(cache_path, empresa, estado, motivo)
    novo_cursor = criar_cursor_baseline(dbx, pasta, logger, tentativas)
    agora = agora_iso()
    for campo in ("pendente", "cursor_migracao_exclusoes", "cursor_migracao_criado_em", "cursor"):
        estado.pop(campo, None)
    estado.update(
        cursor_confirmado=novo_cursor,
        cursor_inclui_exclusoes=True,
        baseline_recriada_em=agora,
        baseline_criada_em=estado.get("baseline_criada_em") or agora,
        ultima_execucao_em=agora,
        ultimo_status=status,
        ultima_estrategia="atividade_com_rebaseline",
        backlog_arquivado=str(arquivo or ""),
        paginas_backlog_arquivadas=paginas_arquivadas,
        resumo_backlog_arquivado=asdict(resumo),
        politica_backlog_alto_volume_aplicada=True,
    )
    salvar_cache(cache_path, cache)
    return novo_cursor, arquivo, {
        "paginas": paginas_arquivadas,
        "arquivos": resumo.arquivos_criados_ou_alterados,
        "exclusoes": resumo.arquivos_excluidos,
        "ultima_modificacao": resumo.ultima_modificacao,
    }


def ignorar_arquivo(nome: str, cfg: dict[str, Any]) -> bool:
    lower = nome.lower()
    if lower in {str(v).lower() for v in cfg.get("ignorar_nomes", [])}:
        return True
    if any(lower.startswith(str(v).lower()) for v in cfg.get("ignorar_prefixos", [])):
        return True
    if any(lower.endswith(str(v).lower()) for v in cfg.get("ignorar_extensoes", [])):
        return True
    for padrao in cfg.get("ignorar_padroes", []) or []:
        if fnmatch.fnmatch(lower, str(padrao).lower()):
            return True
    return False


def processar_delta(
    entradas: list[Any],
    resumo: ResumoDelta,
    cfg: dict[str, Any],
    tzinfo: ZoneInfo,
) -> None:
    limite_amostras = int(cfg.get("amostras_por_empresa", 5))
    for entrada in entradas:
        caminho = auditor.normalizar_path(getattr(entrada, "path_display", "") or getattr(entrada, "path_lower", "") or "")
        nome = str(getattr(entrada, "name", "") or auditor.nome_path(caminho))
        if isinstance(entrada, FileMetadata):
            if ignorar_arquivo(nome, cfg):
                resumo.itens_ignorados += 1
                continue
            modificado = dt_iso(getattr(entrada, "server_modified", None), tzinfo)
            resumo.arquivos_criados_ou_alterados += 1
            resumo.bytes_alterados += int(getattr(entrada, "size", 0) or 0)
            atualizar_mais_recente(resumo, modificado, nome, caminho)
            resumo.registrar_amostra(
                {"tipo": "CRIADO_OU_ALTERADO", "arquivo": nome, "caminho": caminho, "data": modificado},
                limite_amostras,
            )
        elif isinstance(entrada, DeletedMetadata):
            if ignorar_arquivo(nome, cfg):
                resumo.itens_ignorados += 1
                continue
            resumo.arquivos_excluidos += 1
            resumo.registrar_amostra(
                {"tipo": "EXCLUIDO", "arquivo": nome, "caminho": caminho, "data": "detectado nesta execução"},
                limite_amostras,
            )
        elif isinstance(entrada, FolderMetadata):
            resumo.pastas_alteradas += 1


def top_level_para(caminho: str, base_path: str) -> str:
    relativo = auditor.relativo_a_raiz(caminho, base_path)
    if not relativo:
        return "(raiz)"
    partes = [p for p in relativo.split("/") if p]
    return partes[0] if len(partes) > 1 else "(raiz)"


def processar_completo(
    entradas: list[Any],
    resumo: ResumoCompleto,
    base_path: str,
    tzinfo: ZoneInfo,
) -> None:
    for entrada in entradas:
        caminho = auditor.normalizar_path(getattr(entrada, "path_display", "") or getattr(entrada, "path_lower", "") or "")
        nome = str(getattr(entrada, "name", "") or auditor.nome_path(caminho))
        grupo = top_level_para(caminho, base_path)
        grupo_info = resumo.top_level.setdefault(grupo, {"arquivos": 0, "pastas": 0, "bytes": 0})
        if isinstance(entrada, FileMetadata):
            tamanho = int(getattr(entrada, "size", 0) or 0)
            modificado = dt_iso(getattr(entrada, "server_modified", None), tzinfo)
            resumo.arquivos += 1
            resumo.tamanho_total_bytes += tamanho
            grupo_info["arquivos"] += 1
            grupo_info["bytes"] += tamanho
            atualizar_mais_recente(resumo, modificado, nome, caminho)
        elif isinstance(entrada, FolderMetadata):
            resumo.pastas += 1
            grupo_info["pastas"] += 1


def criar_cursor_baseline(
    dbx: Any,
    pasta: str,
    logger: logging.Logger,
    tentativas: int,
) -> str:
    resultado = auditor.chamada_com_retry(
        dbx.files_list_folder_get_latest_cursor,
        "" if pasta == "/" else pasta,
        recursive=True,
        include_deleted=True,
        logger=logger,
        max_tentativas=tentativas,
    )
    return str(resultado.cursor)


def cursor_invalido(exc: Exception) -> bool:
    texto = str(exc).lower()
    return "reset" in texto or "cursor" in texto and any(v in texto for v in ("invalid", "expired", "not_found"))


def verificar_heartbeat(
    dbx: Any,
    arquivos_path: str,
    cfg: dict[str, Any],
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    tentativas: int,
) -> dict[str, Any]:
    relativo = str(cfg.get("heartbeat_arquivo", "") or "").strip()
    if not relativo:
        return {"configurado": False, "ok": False, "estado": "NAO_CONFIGURADO"}
    caminho = auditor.path_join(arquivos_path, relativo)
    try:
        metadata = auditor.chamada_com_retry(
            dbx.files_get_metadata,
            caminho,
            logger=logger,
            max_tentativas=tentativas,
        )
    except ApiError as exc:
        if auditor.erro_pasta_nao_encontrada(exc):
            return {
                "configurado": True, "ok": False, "estado": "AUSENTE",
                "caminho": caminho, "erro": "Marcador não encontrado.",
            }
        return {
            "configurado": True, "ok": False, "estado": "ERRO",
            "caminho": caminho, "erro": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "configurado": True, "ok": False, "estado": "ERRO",
            "caminho": caminho, "erro": str(exc),
        }
    if not isinstance(metadata, FileMetadata):
        return {
            "configurado": True, "ok": False, "estado": "ERRO",
            "caminho": caminho, "erro": "O marcador não é um arquivo.",
        }
    modificado = auditor.converter_dropbox_datetime(metadata.server_modified, tzinfo)
    limite = int(cfg.get("heartbeat_max_idade_horas", 168))
    idade = (datetime.now(tzinfo) - modificado).total_seconds() / 3600 if modificado else None
    ok = idade is not None and idade <= limite
    return {
        "configurado": True,
        "ok": ok,
        "estado": "OK" if ok else "ATRASADO",
        "caminho": caminho,
        "modificado": auditor.formatar_dt(modificado),
        "idade_horas": round(idade, 2) if idade is not None else None,
        "limite_horas": limite,
    }


def resolver_pasta_arquivos(
    dbx: Any,
    empresa_path: str,
    config: dict[str, Any],
    logger: logging.Logger,
    tentativas: int,
) -> str:
    """Monta o caminho de Arquivos; o Dropbox resolve a grafia sem diferenciar caixa."""
    del dbx, logger, tentativas
    nome_config = str(config.get("estrutura", {}).get("pasta_arquivos", "arquivos")).strip("/")
    return auditor.path_join(empresa_path, nome_config)


def registro_base(empresa: str, pasta: str, modo: str) -> dict[str, Any]:
    return {
        "empresa": empresa,
        "status": "ERRO",
        "modo": modo,
        "caminho_dropbox": pasta,
        "periodo_desde": "",
        "periodo_ate": agora_iso(),
        "arquivos_criados_ou_alterados": 0,
        "arquivos_excluidos": 0,
        "pastas_alteradas": 0,
        "itens_ignorados": 0,
        "arquivos_total": "",
        "pastas_total": "",
        "tamanho_total_gb": "",
        "ultima_modificacao": "",
        "ultimo_arquivo": "",
        "caminho_ultimo_arquivo": "",
        "paginas_lidas": 0,
        "paginas_nesta_execucao": 0,
        "paginas_acumuladas": 0,
        "paginas_backlog_arquivadas": 0,
        "backlog_descartado": False,
        "arquivo_backlog_arquivado": "",
        "estrategia": "incremental",
        "tempo_segundos": 0,
        "heartbeat": {},
        "amostras": [],
        "top_level": {},
        "observacao": "",
    }


def auditar_incremental_empresa(
    dbx: Any,
    empresa: str,
    empresa_path: str,
    config: dict[str, Any],
    cfg: dict[str, Any],
    cache: dict[str, Any],
    cache_path: Path,
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    max_paginas: int,
    tempo_limite: int,
    tentativas: int,
    resetar: bool,
    quieto: bool,
) -> dict[str, Any]:
    inicio = time.monotonic()
    pasta = resolver_pasta_arquivos(dbx, empresa_path, config, logger, tentativas)
    registro = registro_base(empresa, pasta, "semanal")
    chave = chave_cache(empresa, pasta)
    empresas_cache = cache.setdefault("empresas", {})
    if resetar:
        empresas_cache.pop(chave, None)
    estado = empresas_cache.setdefault(chave, {"empresa": empresa, "path": pasta})
    registro["periodo_desde"] = str(estado.get("ultima_execucao_em") or estado.get("baseline_criada_em") or "")

    cursor = str(estado.get("cursor_confirmado") or estado.get("cursor") or "")
    if not cursor:
        try:
            cursor = criar_cursor_baseline(dbx, pasta, logger, tentativas)
        except ApiError as exc:
            if auditor.erro_pasta_nao_encontrada(exc):
                registro.update(status="NAO_EXISTE", observacao="A pasta Arquivos não foi encontrada.")
                return registro
            raise
        estado.update(
            cursor_confirmado=cursor,
            cursor_inclui_exclusoes=True,
            baseline_criada_em=agora_iso(),
            ultima_execucao_em=agora_iso(),
            ultimo_status="BASELINE_CRIADA",
        )
        salvar_cache(cache_path, cache)
        heartbeat = verificar_heartbeat(dbx, pasta, cfg, tzinfo, logger, tentativas)
        registro["heartbeat"] = heartbeat
        observacao = "Linha de base criada sem percorrer a árvore. A próxima execução mostrará apenas as mudanças."
        if heartbeat.get("ok"):
            observacao += " O marcador de execução está atualizado dentro do prazo."
        elif heartbeat.get("configurado"):
            observacao += f" Marcador configurado com estado {heartbeat.get('estado', 'DESCONHECIDO')}."
        registro.update(
            status="BASELINE_CRIADA",
            observacao=observacao,
            tempo_segundos=round(time.monotonic() - inicio, 2),
        )
        return registro

    # O cursor legado não registrava exclusões. Criamos uma linha de base paralela
    # antes de consumi-lo; ao final, ela passa a ser usada nas próximas semanas.
    cursor_migracao = str(estado.get("cursor_migracao_exclusoes") or "")
    if not bool(estado.get("cursor_inclui_exclusoes", False)) and not cursor_migracao:
        try:
            cursor_migracao = criar_cursor_baseline(dbx, pasta, logger, tentativas)
            estado["cursor_migracao_exclusoes"] = cursor_migracao
            estado["cursor_migracao_criado_em"] = agora_iso()
            salvar_cache(cache_path, cache)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Não foi possível preparar cursor com exclusões para %s: %s", empresa, exc)

    pendente = estado.get("pendente") if isinstance(estado.get("pendente"), dict) else None
    resumo = ResumoDelta.from_dict(pendente.get("resumo") if pendente else None)
    paginas = int(pendente.get("paginas", 0) if pendente else 0)
    paginas_execucao = 0
    cursor_atual = str(pendente.get("cursor") if pendente else cursor)
    periodo_desde = str(pendente.get("iniciado_em") if pendente else registro["periodo_desde"])
    registro["periodo_desde"] = periodo_desde
    incompleto = False

    while True:
        try:
            resultado = auditor.chamada_com_retry(
                dbx.files_list_folder_continue,
                cursor_atual,
                logger=logger,
                max_tentativas=tentativas,
            )
        except ApiError as exc:
            if cursor_invalido(exc):
                if not bool(cfg.get("recuperar_cursor_invalido_automaticamente", True)):
                    registro.update(status="CURSOR_INVALIDO", observacao="O Dropbox invalidou o cursor. Execute o reset de cache para recriar a linha de base.")
                    return registro
                novo = criar_cursor_baseline(dbx, pasta, logger, tentativas)
                estado.update(
                    cursor_confirmado=novo,
                    cursor_inclui_exclusoes=True,
                    baseline_criada_em=agora_iso(),
                    ultima_execucao_em=agora_iso(),
                    ultimo_status="CURSOR_RECRIADO",
                )
                estado.pop("pendente", None)
                salvar_cache(cache_path, cache)
                registro.update(
                    status="CURSOR_RECRIADO",
                    observacao="O cursor anterior foi invalidado pelo Dropbox. Uma nova linha de base foi criada; o intervalo perdido deve ser revisado manualmente.",
                    tempo_segundos=round(time.monotonic() - inicio, 2),
                )
                return registro
            raise

        paginas += 1
        paginas_execucao += 1
        processar_delta(list(resultado.entries), resumo, cfg, tzinfo)
        cursor_atual = str(resultado.cursor)
        estado["pendente"] = {
            "cursor": cursor_atual,
            "resumo": asdict(resumo),
            "paginas": paginas,
            "iniciado_em": periodo_desde or agora_iso(),
            "ultima_atualizacao": agora_iso(),
        }
        salvar_cache(cache_path, cache)
        if not quieto:
            print(
                f"  {empresa}: página {paginas} | arquivos {resumo.arquivos_criados_ou_alterados} | "
                f"exclusões {resumo.arquivos_excluidos}"
            )

        if not resultado.has_more:
            break
        if max_paginas > 0 and paginas_execucao >= max_paginas:
            incompleto = True
            break
        if tempo_limite > 0 and time.monotonic() - inicio >= tempo_limite:
            incompleto = True
            break

    registro.update(
        arquivos_criados_ou_alterados=resumo.arquivos_criados_ou_alterados,
        arquivos_excluidos=resumo.arquivos_excluidos,
        pastas_alteradas=resumo.pastas_alteradas,
        itens_ignorados=resumo.itens_ignorados,
        ultima_modificacao=resumo.ultima_modificacao,
        ultimo_arquivo=resumo.ultimo_arquivo,
        caminho_ultimo_arquivo=resumo.caminho_ultimo_arquivo,
        paginas_lidas=paginas_execucao,
        paginas_nesta_execucao=paginas_execucao,
        paginas_acumuladas=paginas,
        amostras=resumo.amostras,
    )

    if incompleto:
        registro.update(
            status="INCOMPLETO",
            observacao=(
                f"A leitura parou no limite configurado. Foram lidas {paginas_execucao} pagina(s) nesta execucao "
                f"e {paginas} pagina(s) no acumulado. O resumo e o cursor parcial foram salvos."
            ),
            tempo_segundos=round(time.monotonic() - inicio, 2),
        )
        estado["ultimo_status"] = "INCOMPLETO"
        salvar_cache(cache_path, cache)
        return registro

    # Migração: o cursor com exclusões foi criado no início desta execução.
    # Pode haver pequena repetição na próxima rodada, mas não há salto silencioso.
    estado["cursor_confirmado"] = cursor_migracao or cursor_atual
    estado["cursor_inclui_exclusoes"] = bool(cursor_migracao) or bool(estado.get("cursor_inclui_exclusoes", False))
    if cursor_migracao:
        estado.pop("cursor_migracao_exclusoes", None)
        estado.pop("cursor_migracao_criado_em", None)
    estado["ultima_execucao_em"] = agora_iso()
    estado["ultimo_resumo"] = asdict(resumo)
    estado.pop("pendente", None)

    heartbeat = verificar_heartbeat(dbx, pasta, cfg, tzinfo, logger, tentativas)
    registro["heartbeat"] = heartbeat
    heartbeat_estado = str(heartbeat.get("estado") or "")
    if heartbeat.get("ok"):
        status = "BACKUP_CONFIRMADO"
        observacao = "O arquivo marcador de execução foi atualizado dentro do prazo configurado."
    elif heartbeat.get("configurado") and heartbeat_estado == "ATRASADO":
        status = "HEARTBEAT_ATRASADO"
        observacao = (
            f"O marcador de execução está atrasado: {heartbeat.get('idade_horas')} hora(s), "
            f"limite {heartbeat.get('limite_horas')} hora(s)."
        )
        if resumo.arquivos_criados_ou_alterados > 0:
            observacao += " Apesar disso, houve arquivos criados ou alterados no período."
    elif heartbeat.get("configurado") and heartbeat_estado == "AUSENTE":
        status = "HEARTBEAT_AUSENTE"
        observacao = "O marcador de execução configurado não foi encontrado."
        if resumo.arquivos_criados_ou_alterados > 0:
            observacao += " Ainda assim, houve arquivos criados ou alterados no período."
    elif heartbeat.get("configurado") and heartbeat_estado == "ERRO":
        status = "HEARTBEAT_ERRO"
        observacao = f"Falha ao consultar o marcador de execução: {heartbeat.get('erro', 'erro não detalhado')}."
        if resumo.arquivos_criados_ou_alterados > 0:
            observacao += " O cursor encontrou atividade de arquivos, mas a prova por marcador falhou."
    elif resumo.arquivos_criados_ou_alterados > 0:
        status = "ATIVIDADE_CONFIRMADA"
        observacao = "O cursor do Dropbox encontrou arquivos criados ou alterados desde a execução anterior."
    else:
        empresa_cfg = auditor.politica_empresa(config, empresa)
        exigir_atividade = bool(
            empresa_cfg.get("exigir_atividade_arquivos") is True
            or empresa_cfg.get("arquivos_exigem_atividade") is True
        )
        if exigir_atividade:
            status = "ATIVIDADE_ESPERADA_AUSENTE"
            observacao = (
                "A política da empresa exige atividade de Arquivos nesta janela, mas nenhuma criação/alteração foi observada."
            )
            if resumo.arquivos_excluidos > 0:
                observacao += f" Foram detectadas {resumo.arquivos_excluidos} exclusão(ões)."
        elif resumo.arquivos_excluidos > 0:
            status = "SOMENTE_EXCLUSOES"
            observacao = "Foram detectadas exclusões, mas nenhum arquivo criado ou alterado no período."
        else:
            status = "SEM_MUDANCAS"
            observacao = "Nenhuma mudança de arquivo foi detectada desde o cursor anterior. Isso não prova, por si só, que o job de backup deixou de executar."

    if cursor_migracao:
        observacao += " O cache da v12 foi migrado; o rastreamento de exclusões passa a valer a partir desta execução."
    estado["ultimo_status"] = status
    if status in {"ATIVIDADE_CONFIRMADA", "BACKUP_CONFIRMADO"}:
        estado["ultima_atividade_confirmada_em"] = resumo.ultima_modificacao or heartbeat.get("modificado") or agora_iso()
    salvar_cache(cache_path, cache)
    registro.update(status=status, observacao=observacao, tempo_segundos=round(time.monotonic() - inicio, 2))
    return registro



def auditar_atividade_com_rebaseline(
    dbx: Any,
    empresa: str,
    empresa_path: str,
    config: dict[str, Any],
    cfg: dict[str, Any],
    cache: dict[str, Any],
    cache_path: Path,
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    tentativas: int,
    resetar: bool,
    quieto: bool,
) -> dict[str, Any]:
    """Confirma atividade em poucas paginas e corta backlog excessivo por politica."""
    inicio = time.monotonic()
    pasta = resolver_pasta_arquivos(dbx, empresa_path, config, logger, tentativas)
    registro = registro_base(empresa, pasta, "semanal")
    registro["estrategia"] = "atividade_com_rebaseline"
    chave = chave_cache(empresa, pasta)
    empresas_cache = cache.setdefault("empresas", {})
    if resetar:
        empresas_cache.pop(chave, None)
    estado = empresas_cache.setdefault(chave, {"empresa": empresa, "path": pasta})
    registro["periodo_desde"] = str(estado.get("ultima_execucao_em") or estado.get("baseline_criada_em") or "")

    if (
        isinstance(estado.get("pendente"), dict)
        and bool(cfg.get("abandonar_checkpoint_existente", False))
        and not bool(estado.get("politica_backlog_alto_volume_aplicada", estado.get("politica_backlog_v13_3_aplicada", False)))
    ):
        motivo = (
            "Checkpoint historico excessivo arquivado ao migrar para a estrategia de atividade com rebaseline. "
            "Os eventos anteriores nao foram inventariados integralmente."
        )
        try:
            _, arquivo, arquivado = recriar_baseline_sem_inventario(
                dbx, empresa, pasta, estado, cache, cache_path, logger, tentativas, motivo
            )
        except ApiError as exc:
            if auditor.erro_pasta_nao_encontrada(exc):
                registro.update(status="NAO_EXISTE", observacao="A pasta Arquivos nao foi encontrada.")
                return registro
            raise
        registro.update(
            status="BASELINE_RECRIADA_POLITICA",
            paginas_lidas=0,
            paginas_nesta_execucao=0,
            paginas_acumuladas=0,
            paginas_backlog_arquivadas=arquivado["paginas"],
            backlog_descartado=True,
            arquivo_backlog_arquivado=str(arquivo or ""),
            arquivos_criados_ou_alterados=arquivado["arquivos"],
            arquivos_excluidos=arquivado["exclusoes"],
            ultima_modificacao=arquivado["ultima_modificacao"],
            observacao=(
                f"Backlog historico arquivado ({arquivado['paginas']} pagina(s), "
                f"{arquivado['arquivos']} arquivo(s) observado(s)). Uma nova baseline atual foi criada sem "
                "percorrer a arvore. A proxima execucao medira somente eventos novos."
            ),
            tempo_segundos=round(time.monotonic() - inicio, 2),
        )
        logger.warning("%s: backlog antigo arquivado em %s e baseline recriada por politica.", empresa, arquivo)
        return registro

    cursor = str(estado.get("cursor_confirmado") or estado.get("cursor") or "")
    if not cursor:
        try:
            cursor = criar_cursor_baseline(dbx, pasta, logger, tentativas)
        except ApiError as exc:
            if auditor.erro_pasta_nao_encontrada(exc):
                registro.update(status="NAO_EXISTE", observacao="A pasta Arquivos nao foi encontrada.")
                return registro
            raise
        agora = agora_iso()
        estado.update(
            cursor_confirmado=cursor,
            cursor_inclui_exclusoes=True,
            baseline_criada_em=agora,
            ultima_execucao_em=agora,
            ultimo_status="BASELINE_CRIADA",
            ultima_estrategia="atividade_com_rebaseline",
        )
        salvar_cache(cache_path, cache)
        registro.update(
            status="BASELINE_CRIADA",
            observacao="Linha de base criada sem inventariar a arvore. A proxima execucao fara amostragem das mudancas.",
            tempo_segundos=round(time.monotonic() - inicio, 2),
        )
        return registro

    resumo = ResumoDelta()
    cursor_atual = cursor
    limite_paginas = max(1, int(cfg.get("max_paginas_amostragem", 3) or 3))
    paginas = 0
    has_more = False
    atividade_encontrada = False
    while paginas < limite_paginas:
        try:
            resultado = auditor.chamada_com_retry(
                dbx.files_list_folder_continue, cursor_atual, logger=logger, max_tentativas=tentativas
            )
        except ApiError as exc:
            if cursor_invalido(exc):
                novo = criar_cursor_baseline(dbx, pasta, logger, tentativas)
                estado.update(
                    cursor_confirmado=novo,
                    cursor_inclui_exclusoes=True,
                    baseline_recriada_em=agora_iso(),
                    ultima_execucao_em=agora_iso(),
                    ultimo_status="CURSOR_RECRIADO",
                )
                salvar_cache(cache_path, cache)
                registro.update(
                    status="CURSOR_RECRIADO",
                    observacao="O Dropbox invalidou o cursor. Uma nova baseline atual foi criada sem inventario.",
                    tempo_segundos=round(time.monotonic() - inicio, 2),
                )
                return registro
            raise
        paginas += 1
        processar_delta(list(resultado.entries), resumo, cfg, tzinfo)
        cursor_atual = str(resultado.cursor)
        has_more = bool(resultado.has_more)
        atividade_encontrada = resumo.arquivos_criados_ou_alterados > 0
        if not quieto:
            print(
                f"  {empresa}: amostra pagina {paginas}/{limite_paginas} | "
                f"arquivos {resumo.arquivos_criados_ou_alterados} | exclusoes {resumo.arquivos_excluidos}"
            )
        if not has_more:
            break
        if atividade_encontrada and bool(cfg.get("rebaseline_apos_confirmacao", True)):
            break

    heartbeat = verificar_heartbeat(dbx, pasta, cfg, tzinfo, logger, tentativas)
    registro["heartbeat"] = heartbeat
    precisa_cortar = has_more and (
        atividade_encontrada
        or bool(cfg.get("rebaseline_mesmo_sem_atividade", False))
        or bool(heartbeat.get("ok"))
    )
    arquivo_backlog: Path | None = None
    if precisa_cortar:
        snapshot = {
            "empresa": empresa,
            "path": pasta,
            "cursor_confirmado": cursor,
            "pendente": {
                "cursor": cursor_atual,
                "resumo": asdict(resumo),
                "paginas": paginas,
                "iniciado_em": registro["periodo_desde"],
                "ultima_atualizacao": agora_iso(),
            },
            "ultimo_status": estado.get("ultimo_status", ""),
        }
        arquivo_backlog = arquivar_backlog(
            cache_path,
            empresa,
            snapshot,
            "Backlog remanescente descartado apos amostragem operacional de atividade.",
        )
        novo_cursor = criar_cursor_baseline(dbx, pasta, logger, tentativas)
        cursor_final = novo_cursor
    else:
        cursor_final = cursor_atual

    agora = agora_iso()
    estado.update(
        cursor_confirmado=cursor_final,
        cursor_inclui_exclusoes=True,
        ultima_execucao_em=agora,
        ultimo_resumo=asdict(resumo),
        ultima_estrategia="atividade_com_rebaseline",
        backlog_arquivado=str(arquivo_backlog or ""),
    )
    estado.pop("pendente", None)

    if heartbeat.get("ok"):
        status = "BACKUP_CONFIRMADO"
        observacao = "Heartbeat atualizado dentro do prazo configurado."
    elif atividade_encontrada:
        status = "ATIVIDADE_CONFIRMADA"
        observacao = (
            f"Atividade confirmada por amostragem em {paginas} pagina(s). "
            "O restante do backlog foi descartado por politica de alto volume."
            if precisa_cortar
            else f"Atividade confirmada; todas as {paginas} pagina(s) pendentes foram processadas."
        )
    elif not has_more and resumo.arquivos_excluidos > 0:
        status = "SOMENTE_EXCLUSOES"
        observacao = "A amostragem terminou e encontrou somente exclusoes."
    elif not has_more:
        status = "SEM_MUDANCAS"
        observacao = "Nenhuma mudanca foi encontrada desde o cursor anterior."
    else:
        status = "AMOSTRAGEM_INCONCLUSIVA"
        observacao = (
            f"Nenhum arquivo novo foi encontrado nas primeiras {paginas} pagina(s). "
            "A baseline foi avancada por politica para impedir backlog permanente; confirme a execucao do job na origem."
        )
    estado["ultimo_status"] = status
    if status in {"ATIVIDADE_CONFIRMADA", "BACKUP_CONFIRMADO"}:
        estado["ultima_atividade_confirmada_em"] = resumo.ultima_modificacao or heartbeat.get("modificado") or agora
    salvar_cache(cache_path, cache)
    registro.update(
        status=status,
        arquivos_criados_ou_alterados=resumo.arquivos_criados_ou_alterados,
        arquivos_excluidos=resumo.arquivos_excluidos,
        pastas_alteradas=resumo.pastas_alteradas,
        itens_ignorados=resumo.itens_ignorados,
        ultima_modificacao=resumo.ultima_modificacao,
        ultimo_arquivo=resumo.ultimo_arquivo,
        caminho_ultimo_arquivo=resumo.caminho_ultimo_arquivo,
        paginas_lidas=paginas,
        paginas_nesta_execucao=paginas,
        paginas_acumuladas=paginas,
        paginas_backlog_arquivadas=paginas if precisa_cortar else 0,
        backlog_descartado=precisa_cortar,
        arquivo_backlog_arquivado=str(arquivo_backlog or ""),
        amostras=resumo.amostras,
        observacao=observacao,
        tempo_segundos=round(time.monotonic() - inicio, 2),
    )
    return registro


def auditar_semanal_empresa(
    dbx: Any,
    empresa: str,
    empresa_path: str,
    config: dict[str, Any],
    cfg: dict[str, Any],
    cache: dict[str, Any],
    cache_path: Path,
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    max_paginas: int,
    tempo_limite: int,
    tentativas: int,
    resetar: bool,
    quieto: bool,
) -> dict[str, Any]:
    cfg_empresa = config_arquivos_empresa(config, empresa, cfg)
    estrategia = str(cfg_empresa.get("estrategia", "incremental") or "incremental").lower().strip()
    if estrategia in {"atividade_com_rebaseline", "amostragem", "alto_volume"}:
        return auditar_atividade_com_rebaseline(
            dbx, empresa, empresa_path, config, cfg_empresa, cache, cache_path,
            tzinfo, logger, tentativas, resetar, quieto,
        )
    return auditar_incremental_empresa(
        dbx, empresa, empresa_path, config, cfg_empresa, cache, cache_path,
        tzinfo, logger, max_paginas, tempo_limite, tentativas, resetar, quieto,
    )


def recriar_baseline_empresa(
    dbx: Any,
    empresa: str,
    empresa_path: str,
    config: dict[str, Any],
    cfg: dict[str, Any],
    cache: dict[str, Any],
    cache_path: Path,
    logger: logging.Logger,
    tentativas: int,
    motivo: str,
) -> dict[str, Any]:
    pasta = resolver_pasta_arquivos(dbx, empresa_path, config, logger, tentativas)
    registro = registro_base(empresa, pasta, "rebaseline")
    chave = chave_cache(empresa, pasta)
    estado = cache.setdefault("empresas", {}).setdefault(chave, {"empresa": empresa, "path": pasta})
    _, arquivo, arquivado = recriar_baseline_sem_inventario(
        dbx, empresa, pasta, estado, cache, cache_path, logger, tentativas, motivo,
        status="BASELINE_RECRIADA_MANUAL",
    )
    registro.update(
        status="BASELINE_RECRIADA_MANUAL",
        estrategia=str(config_arquivos_empresa(config, empresa, cfg).get("estrategia", "incremental")),
        paginas_backlog_arquivadas=arquivado["paginas"],
        backlog_descartado=bool(arquivado["paginas"] or arquivado["arquivos"] or arquivado["exclusoes"]),
        arquivo_backlog_arquivado=str(arquivo or ""),
        observacao=(
            f"Baseline atual recriada sem inventariar a arvore. Backlog arquivado: {arquivado['paginas']} pagina(s), "
            f"{arquivado['arquivos']} arquivo(s) observado(s). Motivo: {motivo}"
        ),
    )
    return registro


def diagnosticar_empresa(
    dbx: Any,
    empresa: str,
    empresa_path: str,
    config: dict[str, Any],
    cfg: dict[str, Any],
    cache: dict[str, Any],
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    tentativas: int,
) -> dict[str, Any]:
    """Diagnóstico leve: consulta metadados, heartbeat e estado do cache sem consumir o cursor."""
    inicio = time.monotonic()
    pasta = resolver_pasta_arquivos(dbx, empresa_path, config, logger, tentativas)
    registro = registro_base(empresa, pasta, "diagnostico")
    chave = chave_cache(empresa, pasta)
    estado = cache.get("empresas", {}).get(chave, {}) if isinstance(cache.get("empresas"), dict) else {}
    try:
        metadata = auditor.chamada_com_retry(
            dbx.files_get_metadata, pasta, logger=logger, max_tentativas=tentativas
        )
        if not isinstance(metadata, FolderMetadata):
            registro.update(status="NAO_EXISTE", observacao="O caminho Arquivos existe, mas não é uma pasta.")
            return registro
    except ApiError as exc:
        if auditor.erro_pasta_nao_encontrada(exc):
            registro.update(status="NAO_EXISTE", observacao="A pasta Arquivos não foi encontrada.")
            return registro
        raise

    heartbeat = verificar_heartbeat(dbx, pasta, cfg, tzinfo, logger, tentativas)
    registro["heartbeat"] = heartbeat
    registro["periodo_desde"] = str(estado.get("ultima_execucao_em") or estado.get("baseline_criada_em") or "")
    ultimo_resumo = estado.get("ultimo_resumo") if isinstance(estado.get("ultimo_resumo"), dict) else {}
    registro.update(
        arquivos_criados_ou_alterados=int(ultimo_resumo.get("arquivos_criados_ou_alterados", 0) or 0),
        arquivos_excluidos=int(ultimo_resumo.get("arquivos_excluidos", 0) or 0),
        ultima_modificacao=str(ultimo_resumo.get("ultima_modificacao", "")),
        ultimo_arquivo=str(ultimo_resumo.get("ultimo_arquivo", "")),
        caminho_ultimo_arquivo=str(ultimo_resumo.get("caminho_ultimo_arquivo", "")),
        paginas_lidas=int((estado.get("pendente") or {}).get("paginas", 0) if isinstance(estado.get("pendente"), dict) else 0),
        amostras=list(ultimo_resumo.get("amostras", []) or [])[: int(cfg.get("amostras_por_empresa", 5))],
    )
    detalhes = [
        f"Último status: {estado.get('ultimo_status', 'sem execução concluída')}",
        f"Cursor confirmado: {'SIM' if estado.get('cursor_confirmado') or estado.get('cursor') else 'NÃO'}",
        f"Checkpoint pendente: {'SIM' if isinstance(estado.get('pendente'), dict) else 'NÃO'}",
    ]
    if isinstance(estado.get("pendente"), dict):
        registro["status"] = "INCOMPLETO"
        detalhes.append("Existe leitura incremental parcial que será retomada na próxima execução semanal.")
    elif not (estado.get("cursor_confirmado") or estado.get("cursor")):
        registro["status"] = "BASELINE_NAO_CRIADA"
        detalhes.append("Execute o modo semanal uma vez para criar a linha de base.")
    elif heartbeat.get("configurado") and not heartbeat.get("ok"):
        mapa = {"ATRASADO": "HEARTBEAT_ATRASADO", "AUSENTE": "HEARTBEAT_AUSENTE", "ERRO": "HEARTBEAT_ERRO"}
        registro["status"] = mapa.get(str(heartbeat.get("estado")), "HEARTBEAT_ERRO")
        detalhes.append(f"Heartbeat: {heartbeat.get('estado')}; {heartbeat.get('erro', '')}".strip())
    else:
        registro["status"] = "OK"
        detalhes.append("Cache e metadados disponíveis; o diagnóstico não avançou o cursor.")
    registro["observacao"] = " | ".join(detalhes)
    registro["tempo_segundos"] = round(time.monotonic() - inicio, 2)
    return registro

def auditar_completo_empresa(
    dbx: Any,
    empresa: str,
    empresa_path: str,
    config: dict[str, Any],
    cache: dict[str, Any],
    cache_path: Path,
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    max_paginas: int,
    tempo_limite: int,
    tentativas: int,
    retomar: bool,
    quieto: bool,
) -> dict[str, Any]:
    inicio = time.monotonic()
    pasta = resolver_pasta_arquivos(dbx, empresa_path, config, logger, tentativas)
    registro = registro_base(empresa, pasta, "completo")
    cfg_empresa = config_arquivos_empresa(config, empresa, config_arquivos(config))
    registro["estrategia"] = str(cfg_empresa.get("estrategia", "incremental"))
    if bool(cfg_empresa.get("bloquear_inventario_completo", False)):
        registro.update(
            status="INVENTARIO_BLOQUEADO",
            observacao=(
                "Inventario completo bloqueado por politica de alto volume. Use a auditoria semanal por amostragem "
                "ou remova explicitamente o bloqueio no config.yaml."
            ),
            tempo_segundos=round(time.monotonic() - inicio, 2),
        )
        return registro
    chave = chave_cache(empresa, pasta)
    estado = cache.setdefault("empresas", {}).setdefault(chave, {"empresa": empresa, "path": pasta})
    full = estado.get("full_scan") if retomar and isinstance(estado.get("full_scan"), dict) else None
    resumo = ResumoCompleto.from_dict(full.get("resumo") if full else None)
    paginas = int(full.get("paginas", 0) if full else 0)
    paginas_execucao = 0
    cursor_semanal_inicio = str(full.get("cursor_semanal_inicio", "") if full else "")
    if not cursor_semanal_inicio:
        try:
            cursor_semanal_inicio = criar_cursor_baseline(dbx, pasta, logger, tentativas)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Não foi possível preparar o cursor semanal antes do inventário de %s: %s", empresa, exc)

    try:
        if full and full.get("cursor"):
            resultado = auditor.chamada_com_retry(
                dbx.files_list_folder_continue,
                str(full["cursor"]),
                logger=logger,
                max_tentativas=tentativas,
            )
        else:
            resultado = auditor.chamada_com_retry(
                dbx.files_list_folder,
                pasta,
                recursive=True,
                include_deleted=False,
                limit=500,
                logger=logger,
                max_tentativas=tentativas,
            )
    except ApiError as exc:
        if auditor.erro_pasta_nao_encontrada(exc):
            registro.update(status="NAO_EXISTE", observacao="A pasta Arquivos não foi encontrada.")
            return registro
        raise

    incompleto = False
    while True:
        paginas += 1
        paginas_execucao += 1
        processar_completo(list(resultado.entries), resumo, pasta, tzinfo)
        estado["full_scan"] = {
            "cursor": str(resultado.cursor),
            "resumo": asdict(resumo),
            "paginas": paginas,
            "iniciado_em": str(full.get("iniciado_em") if full else agora_iso()),
            "ultima_atualizacao": agora_iso(),
            "cursor_semanal_inicio": cursor_semanal_inicio,
        }
        salvar_cache(cache_path, cache)
        if not quieto:
            print(f"  {empresa}: página {paginas} | arquivos {resumo.arquivos} | {resumo.tamanho_total_bytes / (1024**3):.2f} GB")
        if not resultado.has_more:
            break
        if max_paginas > 0 and paginas_execucao >= max_paginas:
            incompleto = True
            break
        if tempo_limite > 0 and time.monotonic() - inicio >= tempo_limite:
            incompleto = True
            break
        resultado = auditor.chamada_com_retry(
            dbx.files_list_folder_continue,
            str(resultado.cursor),
            logger=logger,
            max_tentativas=tentativas,
        )

    registro.update(
        arquivos_total=resumo.arquivos,
        pastas_total=resumo.pastas,
        tamanho_total_gb=round(resumo.tamanho_total_bytes / (1024**3), 3),
        ultima_modificacao=resumo.ultima_modificacao,
        ultimo_arquivo=resumo.ultimo_arquivo,
        caminho_ultimo_arquivo=resumo.caminho_ultimo_arquivo,
        paginas_lidas=paginas,
        top_level=resumo.top_level,
        tempo_segundos=round(time.monotonic() - inicio, 2),
    )
    if incompleto:
        registro.update(
            status="INCOMPLETO",
            observacao="Inventário interrompido no limite configurado. Os totais e o cursor foram salvos e serão retomados sem perder as páginas anteriores.",
        )
        return registro

    estado.pop("full_scan", None)
    estado["varredura_completa_concluida_em"] = agora_iso()
    estado["ultimo_inventario"] = asdict(resumo)
    if cursor_semanal_inicio:
        estado["cursor_confirmado"] = cursor_semanal_inicio
        estado["cursor_inclui_exclusoes"] = True
        estado["baseline_criada_em"] = agora_iso()
    else:
        try:
            estado["cursor_confirmado"] = criar_cursor_baseline(dbx, pasta, logger, tentativas)
            estado["cursor_inclui_exclusoes"] = True
            estado["baseline_criada_em"] = agora_iso()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Inventário concluído, mas não foi possível atualizar o cursor semanal de %s: %s", empresa, exc)
    salvar_cache(cache_path, cache)
    if resumo.arquivos:
        registro.update(status="OK", observacao="Inventário completo concluído com agregação página a página, sem manter milhões de metadados na memória.")
    else:
        registro.update(status="VAZIA", observacao="A pasta existe, mas nenhum arquivo foi encontrado.")
    return registro


def gerar_relatorios(
    registros: list[dict[str, Any]],
    modo: str,
    tzinfo: ZoneInfo,
    destino: Path,
) -> dict[str, Path]:
    destino.mkdir(parents=True, exist_ok=True)
    executado_em = datetime.now(tzinfo)
    sufixo = executado_em.strftime("%Y%m%d_%H%M%S")
    base = destino / f"auditoria_arquivos_{modo}_{sufixo}"
    csv_path = base.with_suffix(".csv")
    json_path = base.with_suffix(".json")
    pdf_path = base.with_suffix(".pdf")

    colunas = [
        "empresa", "status", "modo", "caminho_dropbox", "periodo_desde", "periodo_ate",
        "arquivos_criados_ou_alterados", "arquivos_excluidos", "pastas_alteradas", "itens_ignorados",
        "arquivos_total", "pastas_total", "tamanho_total_gb", "ultima_modificacao", "ultimo_arquivo",
        "caminho_ultimo_arquivo", "estrategia", "paginas_lidas", "paginas_nesta_execucao",
        "paginas_acumuladas", "paginas_backlog_arquivadas", "backlog_descartado",
        "arquivo_backlog_arquivado", "tempo_segundos", "observacao",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as arquivo:
        writer = csv.DictWriter(arquivo, fieldnames=colunas, delimiter=";")
        writer.writeheader()
        for registro in registros:
            writer.writerow(sanitize_csv_mapping({c: registro.get(c, "") for c in colunas}))

    resumo_status: dict[str, int] = {}
    for registro in registros:
        status = str(registro.get("status", ""))
        resumo_status[status] = resumo_status.get(status, 0) + 1
    payload = {
        "schema_version": "1.0",
        "versao": VERSION,
        "tipo": "auditoria_arquivos",
        "modo": modo,
        "executado_em": executado_em.isoformat(timespec="seconds"),
        "resumo_status": resumo_status,
        "registros": [enriquecer_registro(registro) for registro in registros],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pdf_reports.gerar_pdf_arquivos(registros, modo, executado_em, pdf_path)
    return {"pdf": pdf_path, "csv": csv_path, "json": json_path}



def gerar_relatorios_erro(mensagem: str, modo: str, destino: Path) -> dict[str, Path]:
    """Gera artefatos mínimos quando a auditoria falha antes de listar empresas."""
    tzinfo = ZoneInfo("America/Sao_Paulo")
    registro = registro_base("EXECUCAO", "-", modo)
    registro.update(status="ERRO", observacao=mensagem, tempo_segundos=0)
    return gerar_relatorios([registro], modo, tzinfo, destino)

def executar(args: argparse.Namespace) -> int:
    raiz_local = base_dir()
    logger, log_path = auditor.configurar_logging(raiz_local)
    stats = auditor.EstatisticasExecucao()
    try:
        config_path = auditor.resolver_config_path(args.config, raiz_local)
        config = auditor.carregar_config(config_path)
        tzinfo = ZoneInfo(config.get("timezone", "America/Sao_Paulo"))
        cfg = config_arquivos(config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao carregar configuração da auditoria de Arquivos: %s", exc)
        mensagem = f"Falha ao carregar configuração: {exc}"
        print(f"ERRO: {mensagem}")
        try:
            gerados = gerar_relatorios_erro(mensagem, getattr(args, "modo", "semanal"), raiz_local / "relatorios")
            for tipo, caminho in gerados.items():
                print(f"{tipo.upper()}: {caminho}")
        except Exception as report_exc:  # noqa: BLE001
            logger.exception("Também falhou ao gerar relatório de erro: %s", report_exc)
        print(f"Log: {log_path}")
        return 3

    cache_path = raiz_local / str(cfg.get("cache_dir", "cache")) / str(cfg.get("cache_cursor_arquivo", "auditoria_arquivos_cursor_cache.json"))
    cache = carregar_cache(cache_path)
    if args.resetar_cache and not args.empresa:
        cache["empresas"] = {}
        salvar_cache(cache_path, cache)
        print("Cache de Arquivos apagado. A próxima execução criará novas linhas de base.")
        print(f"Backup do cache anterior preservado ao lado do arquivo, quando aplicável: {cache_path.parent}")
        return 0

    try:
        timeout = int(config.get("dropbox", {}).get("timeout_segundos", 120))
        dbx = auditor.criar_cliente_dropbox(logger, timeout_segundos=timeout)
        raiz_dropbox = auditor.normalizar_path(config.get("raiz_dropbox", "/Aplicativos"))
        empresas = auditor.listar_empresas(dbx, raiz_dropbox, logger, stats, args.empresa, args.limite_empresas, args.quieto)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao preparar auditoria de Arquivos: %s", exc)
        mensagem = f"Falha ao preparar auditoria: {exc}"
        print(f"ERRO: {mensagem}")
        try:
            gerados = gerar_relatorios_erro(mensagem, getattr(args, "modo", "semanal"), raiz_local / "relatorios")
            for tipo, caminho in gerados.items():
                print(f"{tipo.upper()}: {caminho}")
        except Exception as report_exc:  # noqa: BLE001
            logger.exception("Também falhou ao gerar relatório de erro: %s", report_exc)
        print(f"Log: {log_path}")
        return 3

    if args.listar_empresas:
        for indice, empresa in enumerate(empresas, 1):
            print(f"{indice:03d}. {empresa}")
        return 0

    modo = "semanal" if args.modo == "cursor" else args.modo
    max_paginas = args.max_paginas or int(cfg.get("max_paginas_incremental_por_empresa" if modo == "semanal" else "max_paginas_completo_por_empresa", 0))
    minutos = args.tempo_maximo_empresa_minutos or int(cfg.get("tempo_maximo_empresa_minutos", 15))
    tempo_limite = max(minutos, 0) * 60
    tentativas = args.tentativas_dropbox or int(cfg.get("tentativas_dropbox", 6))

    print(f"Auditoria de Arquivos v{VERSION} - modo {modo} - {len(empresas)} empresa(s)")
    logger.info("Auditoria de Arquivos v%s iniciada. modo=%s empresas=%s", VERSION, modo, len(empresas))
    registros: list[dict[str, Any]] = []
    for indice, (empresa, empresa_path) in enumerate(empresas.items(), 1):
        if not args.quieto:
            print(f"\n[{indice}/{len(empresas)}] {empresa}")
        try:
            if not auditor.empresa_audita(config, empresa, "arquivos"):
                pasta_prevista = auditor.path_join(empresa_path, config.get("estrutura", {}).get("pasta_arquivos", "arquivos"))
                registro = registro_base(empresa, pasta_prevista, modo)
                registro.update(
                    status=auditor.status_empresa_fora_escopo(config, empresa),
                    observacao=auditor.observacao_empresa(config, empresa, "Arquivos no Dropbox"),
                    tempo_segundos=0,
                )
            elif args.recriar_baseline:
                registro = recriar_baseline_empresa(
                    dbx, empresa, empresa_path, config, cfg, cache, cache_path, logger,
                    tentativas, args.motivo_rebaseline,
                )
            elif modo == "semanal":
                registro = auditar_semanal_empresa(
                    dbx, empresa, empresa_path, config, cfg, cache, cache_path, tzinfo, logger,
                    max_paginas, tempo_limite, tentativas, args.resetar_cache, args.quieto,
                )
            elif modo == "diagnostico":
                registro = diagnosticar_empresa(
                    dbx, empresa, empresa_path, config, cfg, cache, tzinfo, logger, tentativas
                )
            else:
                registro = auditar_completo_empresa(
                    dbx, empresa, empresa_path, config, cache, cache_path, tzinfo, logger,
                    max_paginas, tempo_limite, tentativas, not args.nao_retomar, args.quieto,
                )
        except AuthError as exc:
            logger.exception("Autenticação recusada: %s", exc)
            registro = registro_base(empresa, empresa_path, modo)
            registro.update(status="ERRO", observacao="O Dropbox recusou a autenticação ou permissão.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha na empresa %s: %s", empresa, exc)
            registro = registro_base(empresa, empresa_path, modo)
            registro.update(status="ERRO", observacao=f"Falha ao consultar a empresa: {exc}")
        registros.append(registro)
        logger.info(
            "Arquivos empresa=%s status=%s paginas=%s atividade=%s exclusoes=%s tempo=%ss",
            empresa, registro.get("status"), registro.get("paginas_nesta_execucao", registro.get("paginas_lidas")),
            registro.get("arquivos_criados_ou_alterados"), registro.get("arquivos_excluidos"),
            registro.get("tempo_segundos"),
        )

    gerados = gerar_relatorios(registros, modo, tzinfo, raiz_local / "relatorios")
    print("\nAuditoria concluída.")
    for tipo, caminho in gerados.items():
        print(f"{tipo.upper()}: {caminho}")
    print(f"LOG: {log_path}")
    print(f"CACHE: {cache_path}")
    logger.info("Relatorios gerados: %s", {k: str(v) for k, v in gerados.items()})
    logger.info("Cache utilizado: %s", cache_path)

    status = {str(r.get("status", "")) for r in registros}
    codigo = codigo_saida_status(status)
    logger.info("Auditoria de Arquivos concluida. codigo=%s status=%s", codigo, sorted(status))
    return codigo


def main() -> int:
    if not auditor.validar_versao_python():
        return 3
    args = parse_args()
    if args.gerar_refresh_token:
        return auditor.gerar_refresh_token_interativo()
    return executar(args)


if __name__ == "__main__":
    raise SystemExit(main())
