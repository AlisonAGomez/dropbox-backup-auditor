"""Validação preventiva da configuração operacional."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .security import is_secret_key


class ConfigurationError(ValueError):
    """Configuração inválida ou insegura."""


# Esses campos só fazem sentido dentro de cada empresa. Na raiz, normalmente indicam erro de indentação.
_FORBIDDEN_TOP_LEVEL = {"auditar_arquivos", "auditar_vms", "observacao", "estado"}


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"'{name}' deve ser um bloco YAML (chave/valor).")
    return value


def _positive_number(value: Any, name: str, allow_zero: bool = False) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        raise ConfigurationError(f"'{name}' deve ser numérico.")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"'{name}' deve ser numérico.") from exc
    if numeric < 0 or (numeric == 0 and not allow_zero):
        op = "maior ou igual a zero" if allow_zero else "maior que zero"
        raise ConfigurationError(f"'{name}' deve ser {op}.")


def _find_secrets(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_secret_key(key) and item not in (None, "", "[REDACTED]"):
                found.append(path)
            found.extend(_find_secrets(item, path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(_find_secrets(item, f"{prefix}[{idx}]"))
    return found


def _validate_local_relative_path(value: Any, name: str, *, filename_only: bool = False) -> None:
    if value in (None, ""):
        return
    text = str(value).strip().replace("\\", "/")
    posix = PurePosixPath(text)
    if posix.is_absolute() or ".." in posix.parts:
        raise ConfigurationError(f"'{name}' deve permanecer dentro da pasta do auditor e não pode conter '..'.")
    if filename_only and len(posix.parts) != 1:
        raise ConfigurationError(f"'{name}' deve conter apenas um nome de arquivo, sem subpastas.")


def validate_config(config: Any, path: Path | None = None) -> dict[str, Any]:
    origem = f" em {path}" if path else ""
    if not isinstance(config, dict):
        raise ConfigurationError(f"A configuração{origem} deve ter um objeto YAML na raiz.")

    misplaced = sorted(_FORBIDDEN_TOP_LEVEL.intersection(config))
    if misplaced:
        campos = ", ".join(misplaced)
        raise ConfigurationError(
            f"Campos de empresa encontrados na raiz do YAML: {campos}. "
            "Isso normalmente indica erro de indentação dentro de 'empresas'."
        )

    secrets = _find_secrets(config)
    if secrets:
        raise ConfigurationError(
            "Credenciais sensíveis não podem ficar no config.yaml. "
            "Use as variáveis de ambiente DROPBOX_APP_KEY, DROPBOX_APP_SECRET e DROPBOX_REFRESH_TOKEN. "
            f"Campos encontrados: {', '.join(secrets)}."
        )

    raiz = str(config.get("raiz_dropbox", "/Aplicativos") or "")
    partes = PurePosixPath(raiz).parts
    if not raiz.startswith("/") or ".." in partes:
        raise ConfigurationError("'raiz_dropbox' deve ser um caminho absoluto do Dropbox, sem '..'.")

    timezone = str(config.get("timezone", "America/Sao_Paulo") or "")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Timezone inválido: {timezone}") from exc

    dropbox_cfg = _require_mapping(config.get("dropbox"), "dropbox")
    _positive_number(dropbox_cfg.get("timeout_segundos"), "dropbox.timeout_segundos")

    empresas = _require_mapping(config.get("empresas"), "empresas")
    for nome, cfg in empresas.items():
        if not str(nome).strip():
            raise ConfigurationError("Existe uma empresa sem nome em 'empresas'.")
        if cfg is None:
            raise ConfigurationError(
                f"empresas.{nome} está vazio. Informe ao menos auditar_arquivos/auditar_vms; "
                "isso também pode indicar erro de indentação."
            )
        cfg = _require_mapping(cfg, f"empresas.{nome}")
        for campo in ("auditar_arquivos", "auditar_vms", "exigir_atividade_arquivos", "arquivos_exigem_atividade"):
            if campo in cfg and not isinstance(cfg[campo], bool):
                raise ConfigurationError(f"empresas.{nome}.{campo} deve ser true ou false.")
        estado = str(cfg.get("estado") or "").strip().lower()
        estados_validos = {"", "ativo", "migrado_drive", "migrado_para_drive", "drive", "fora_escopo", "fora_do_escopo"}
        if estado not in estados_validos:
            raise ConfigurationError(f"empresas.{nome}.estado inválido: {estado}.")
        if estado in {"migrado_drive", "migrado_para_drive", "drive", "fora_escopo", "fora_do_escopo"}:
            if cfg.get("auditar_arquivos") is not False or cfg.get("auditar_vms") is not False:
                raise ConfigurationError(
                    f"empresas.{nome}: estado '{estado}' exige auditar_arquivos=false e auditar_vms=false para evitar alertas contraditórios."
                )
        if "auditoria_arquivos" in cfg:
            _require_mapping(cfg.get("auditoria_arquivos"), f"empresas.{nome}.auditoria_arquivos")

    arq = _require_mapping(config.get("auditoria_arquivos"), "auditoria_arquivos")
    for campo in (
        "tentativas_dropbox", "max_paginas_incremental_por_empresa", "max_paginas_completo_por_empresa",
        "tempo_maximo_empresa_minutos", "checkpoint_a_cada_paginas",
    ):
        _positive_number(arq.get(campo), f"auditoria_arquivos.{campo}")
    _validate_local_relative_path(arq.get("cache_dir"), "auditoria_arquivos.cache_dir")
    _validate_local_relative_path(arq.get("cache_cursor_arquivo"), "auditoria_arquivos.cache_cursor_arquivo", filename_only=True)

    vms = _require_mapping(config.get("auditoria_vms"), "auditoria_vms")
    for campo in (
        "minimo_eventos_aprender", "idade_maxima_sem_historico_dias",
        "tempo_maximo_eventos_por_raiz_minutos", "max_eventos_historico_por_vm",
    ):
        _positive_number(vms.get(campo), f"auditoria_vms.{campo}")
    _positive_number(vms.get("max_paginas_backups_fora_do_lugar"), "auditoria_vms.max_paginas_backups_fora_do_lugar", allow_zero=True)

    politicas = _require_mapping(config.get("politicas_vms"), "politicas_vms")
    for empresa, vms_cfg in politicas.items():
        vms_cfg = _require_mapping(vms_cfg, f"politicas_vms.{empresa}")
        for vmid, vm_cfg in vms_cfg.items():
            vm_cfg = _require_mapping(vm_cfg, f"politicas_vms.{empresa}.{vmid}")
            for campo in ("isento", "obrigatorio", "auditar"):
                if campo in vm_cfg and not isinstance(vm_cfg[campo], bool):
                    raise ConfigurationError(
                        f"politicas_vms.{empresa}.{vmid}.{campo} deve ser true ou false."
                    )
            if "tolerancia_horas" in vm_cfg:
                _positive_number(
                    vm_cfg.get("tolerancia_horas"),
                    f"politicas_vms.{empresa}.{vmid}.tolerancia_horas",
                    allow_zero=True,
                )
            frequencia = str(vm_cfg.get("frequencia") or vm_cfg.get("periodicidade") or "").strip().lower()
            if vm_cfg.get("isento") is True and frequencia:
                raise ConfigurationError(
                    f"politicas_vms.{empresa}.{vmid}: item isento não deve declarar frequência; remova uma das duas configurações."
                )
            if frequencia and frequencia not in {"diario", "semanal", "quinzenal", "mensal"}:
                raise ConfigurationError(
                    f"politicas_vms.{empresa}.{vmid}.frequencia inválida: {frequencia}."
                )
    return config
