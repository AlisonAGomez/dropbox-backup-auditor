"""Guarda de integridade da revisão do Auditor Dropbox v2.5.

A validação desta camada é deliberadamente genérica: políticas de clientes e
exceções operacionais pertencem ao ``config.yaml`` local e nunca ao código
público. A função mantém a API da v2.5 para compatibilidade com os launchers.
"""
from __future__ import annotations

from typing import Any

from .version import BUILD

EXPECTED_BUILD = "20260909-r4"


class ReleaseGuardError(RuntimeError):
    """A revisão instalada não corresponde ao build esperado."""


def validar_politicas_criticas(config: dict[str, Any]) -> None:
    """Valida somente invariantes de release, sem regras de clientes.

    Regras operacionais são validadas por ``config_validation.validate_config``
    e devem permanecer exclusivamente no arquivo de configuração local.
    """
    if not isinstance(config, dict):
        raise ReleaseGuardError("Configuração operacional deve ser um objeto.")
    if BUILD != EXPECTED_BUILD:
        raise ReleaseGuardError(
            f"Build interno inesperado: {BUILD} (esperado {EXPECTED_BUILD})."
        )
