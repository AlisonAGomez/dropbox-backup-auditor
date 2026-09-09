from __future__ import annotations

import argparse
import csv
import difflib
import getpass
import json
import logging
import os
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import dataclass
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import dropbox
import yaml
import requests
from dateutil.relativedelta import relativedelta
from dropbox.exceptions import ApiError, AuthError, HttpError, InternalServerError, RateLimitError
from dropbox.files import FileMetadata, FolderMetadata
from dropbox.oauth import DropboxOAuth2FlowNoRedirect

from . import pdf_reports
from .config_validation import validate_config
from .security import RedactingFormatter, sanitize_csv_mapping
from .version import VERSION
from .status_catalog import enriquecer_registro


STATUS_GRAVIDADE = {
    "ERRO": 1,
    "ATRASADO": 2,
    "SEM_BACKUP": 3,
    "IRREGULAR": 4,
    "AMOSTRA_INSUFICIENTE": 5,
    "SEM_UPLOAD_RECENTE": 5,
    "EM_APRENDIZADO": 6,
    "MIGRADO_PARA_DRIVE": 7,
    "NAO_APLICAVEL": 7,
    "OK": 8,
    "COM_ATIVIDADE": 7,
    "VAZIA": 8,
    "NAO_EXISTE": 9,
    "SEM_PASTA_VMS": 10,
    "VM_NAO_ENCONTRADA": 2,
}

CSV_COLUMNS = [
    "tipo_registro",
    "empresa",
    "caminho_dropbox",
    "vm",
    "vmid",
    "status",
    "periodicidade_detectada",
    "confianca",
    "quantidade_backups",
    "quantidade_backups_atuais",
    "quantidade_backups_historico",
    "exclusoes_detectadas_ultima_execucao",
    "origem_periodicidade",
    "ultimo_backup",
    "penultimo_backup",
    "proximo_backup_previsto",
    "tolerancia_horas",
    "atraso_horas",
    "ultimo_arquivo",
    "arquivo_mais_recente",
    "caminho_ultimo_arquivo",
    "caminho_arquivo_mais_recente",
    "tamanho_ultimo_arquivo_gb",
    "server_modified_ultimo_arquivo",
    "ultima_atualizacao_dropbox",
    "quantidade_arquivos",
    "tamanho_total_gb",
    "caminho_vms_usado",
    "pasta_vms_origem",
    "pasta_vms_alternativa",
    "extensao_backup",
    "compactacao",
    "arquivo_zst",
    "observacao",
]

VZDUMP_RE = re.compile(
    r"^vzdump-(?P<tipo>qemu|lxc|openvz)-(?P<vmid>\d+)-"
    r"(?P<data>\d{4}_\d{2}_\d{2})-(?P<hora>\d{2}_\d{2}_\d{2})"
    r"(?P<sufixo>.*)$",
    re.IGNORECASE,
)

IGNORAR_EXTENSOES = (".log", ".notes")
IGNORAR_TEMPORARIOS = (
    ".tmp",
    ".temp",
    ".part",
    ".partial",
    ".download",
    ".incomplete",
    ".crdownload",
)


@dataclass(frozen=True)
class ParsedVzdump:
    tipo: str
    vmid: str
    data_hora: datetime
    extensao_backup: str = ""
    compactacao: str = ""
    arquivo_zst: str = "NAO"


@dataclass(frozen=True)
class Periodicidade:
    periodicidade: str
    confianca: str
    mediana: timedelta | None
    diferencas_horas: list[float]
    observacao: str


@dataclass
class EstatisticasExecucao:
    paginas_api: int = 0
    metadados_lidos: int = 0
    empresas_processadas: int = 0
    vms_processadas: int = 0
    arquivos_lidos: int = 0


@dataclass(frozen=True)
class VMFolderEncontrada:
    folder: FolderMetadata
    caminho_vms_usado: str
    pasta_alternativa: bool
    observacao_descoberta: str = ""
    pasta_vms_origem: str = ""


class ProgressEstimator:
    def __init__(self) -> None:
        self.inicio = time.monotonic()

    def elapsed(self) -> float:
        return max(time.monotonic() - self.inicio, 0.001)

    def format_seconds(self, segundos: float | None) -> str:
        if segundos is None:
            return "--"
        segundos = max(int(segundos), 0)
        horas, resto = divmod(segundos, 3600)
        minutos, segundos = divmod(resto, 60)
        if horas:
            return f"{horas}h {minutos}m {segundos}s"
        if minutos:
            return f"{minutos}m {segundos}s"
        return f"{segundos}s"

    def eta_by_units(self, done: int, total: int) -> str:
        if done <= 0 or total <= 0 or done > total:
            return "--"
        restante = total - done
        media = self.elapsed() / done
        return self.format_seconds(media * restante)

    def elapsed_text(self) -> str:
        return self.format_seconds(self.elapsed())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audita Arquivos e backups de VMs armazenados no Dropbox sem baixar o conteúdo."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Caminho do arquivo YAML de configuracao. Padrao: config.yaml",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Executa validacoes simples de parsing/periodicidade sem acessar o Dropbox.",
    )
    parser.add_argument(
        "--gerar-refresh-token",
        action="store_true",
        help="Gera um refresh token Dropbox via OAuth offline e encerra.",
    )
    parser.add_argument(
        "--empresa",
        action="append",
        default=[],
        help=(
            "Opcional. Processa somente uma empresa. Pode repetir. "
            "Se nao informar, o auditor busca automaticamente todas as empresas "
            "diretamente dentro da raiz configurada. Ex.: --empresa EMPRESA-EXEMPLO-BACKUP"
        ),
    )
    parser.add_argument(
        "--listar-empresas",
        action="store_true",
        help="Lista todas as empresas encontradas diretamente na raiz do Dropbox e encerra sem auditar VMs.",
    )
    parser.add_argument(
        "--incluir-arquivos",
        action="store_true",
        help="Inclui auditoria informativa da pasta arquivos/. Por padrao, fica desligado para ser rapido.",
    )
    parser.add_argument(
        "--limite-empresas",
        type=int,
        default=0,
        help="Limita a quantidade de empresas processadas. Uso recomendado para teste.",
    )
    parser.add_argument(
        "--limite-vms",
        type=int,
        default=0,
        help="Limita a quantidade de VMs por empresa. Uso recomendado para teste.",
    )
    parser.add_argument(
        "--salvar-parcial-a-cada",
        type=int,
        default=0,
        help="Gera relatorio parcial a cada N empresas. Padrao: 0 (desligado).",
    )
    parser.add_argument(
        "--quieto",
        action="store_true",
        help="Reduz mensagens no terminal.",
    )
    return parser.parse_args()


def validar_versao_python() -> bool:
    if sys.version_info >= (3, 11):
        return True

    print("ERRO: este projeto requer Python 3.11 ou superior.")
    print(f"Versao atual: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return False


def resolver_config_path(caminho: str | Path, base_dir: Path) -> Path:
    config_path = Path(caminho)
    if config_path.is_absolute() or config_path.exists():
        return config_path

    candidato = base_dir / config_path
    if candidato.exists():
        return candidato

    return config_path


def aplicar_defaults_config(config: dict[str, Any]) -> dict[str, Any]:
    config.setdefault("raiz_dropbox", "/Aplicativos")
    config.setdefault("timezone", "America/Sao_Paulo")

    config.setdefault("estrutura", {})
    config["estrutura"].setdefault("pasta_arquivos", "arquivos")
    config["estrutura"].setdefault("pasta_vms", "VMS/pve")
    config["estrutura"].setdefault("prefixo_vm", "vm-")

    config.setdefault("analise", {})
    config["analise"].setdefault("max_backups_por_vm_para_relatorio", 10)
    config["analise"].setdefault("arquivos_max_idade_horas", 36)
    config["analise"].setdefault("arquivos_max_paginas_por_empresa", 50)
    config["analise"].setdefault("arquivos_max_subpastas_por_empresa", 10)
    config["analise"].setdefault("arquivos_max_paginas_por_subpasta", 3)

    config.setdefault("tolerancias", {})
    config["tolerancias"].setdefault("diario_horas", 12)
    config["tolerancias"].setdefault("semanal_horas", 36)
    config["tolerancias"].setdefault("quinzenal_horas", 48)
    config["tolerancias"].setdefault("mensal_horas", 96)
    config["tolerancias"].setdefault("irregular_horas", 24)

    config.setdefault("relatorio", {})
    config["relatorio"].setdefault("gerar_csv", True)
    config["relatorio"].setdefault("gerar_json", True)
    config["relatorio"].setdefault("gerar_pdf", True)
    config["relatorio"].pop("gerar_html", None)

    config.setdefault("performance", {})
    config["performance"].setdefault("modo_rapido_vms", True)
    config["performance"].setdefault("incluir_arquivos_por_padrao", True)

    config.setdefault("dropbox", {})
    # A auditoria de arquivos percorre arvores grandes; 120s evita muitos falsos erros por timeout.
    config["dropbox"].setdefault("timeout_segundos", 120)

    config.setdefault("auditoria_arquivos", {})
    # Se a API falhar depois de algumas paginas, o relatorio usa o que ja foi lido e marca como INCOMPLETO.
    config["auditoria_arquivos"].setdefault("continuar_com_resultado_parcial", True)
    config["auditoria_arquivos"].setdefault("tentativas_dropbox", 6)

    config.setdefault("auditoria_vms", {})
    config["auditoria_vms"].setdefault("minimo_eventos_aprender", 4)
    config["auditoria_vms"].setdefault("idade_maxima_sem_historico_dias", 45)
    config.setdefault("empresas", {})

    return config


def carregar_config(caminho: str | Path) -> dict[str, Any]:
    config_path = Path(caminho)
    if not config_path.exists():
        raise FileNotFoundError(f"Arquivo de configuracao nao encontrado: {config_path}")

    with config_path.open("r", encoding="utf-8-sig") as file:
        config = yaml.safe_load(file) or {}

    validate_config(config, config_path)
    return aplicar_defaults_config(config)


def configurar_logging(base_dir: Path) -> tuple[logging.Logger, Path]:
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"auditoria_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logger = logging.getLogger("dropbox_backup_auditor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger, log_path


def normalizar_path(path: str) -> str:
    if not path:
        return "/"
    normalizado = "/" + path.strip("/")
    return normalizado.replace("\\", "/")


def path_lower(path: str) -> str:
    return normalizar_path(path).lower()


def path_join(*partes: str) -> str:
    return "/" + "/".join(str(parte).strip("/") for parte in partes if str(parte).strip("/"))


def politica_empresa(config: dict[str, Any], empresa: str) -> dict[str, Any]:
    """Retorna a politica da empresa sem diferenciar maiusculas/minusculas."""
    empresas_cfg = config.get("empresas", {})
    if not isinstance(empresas_cfg, dict):
        return {}
    for nome, valor in empresas_cfg.items():
        if str(nome).casefold() == str(empresa).casefold() and isinstance(valor, dict):
            return valor
    return {}


def empresa_audita(config: dict[str, Any], empresa: str, area: str) -> bool:
    politica = politica_empresa(config, empresa)
    chave = "auditar_arquivos" if area.lower() == "arquivos" else "auditar_vms"
    return bool(politica.get(chave, True))


def status_empresa_fora_escopo(config: dict[str, Any], empresa: str) -> str:
    politica = politica_empresa(config, empresa)
    estado = str(politica.get("estado") or "").strip().lower()
    return "MIGRADO_PARA_DRIVE" if estado in {"migrado_drive", "migrado_para_drive", "drive"} else "NAO_APLICAVEL"


def observacao_empresa(config: dict[str, Any], empresa: str, area: str) -> str:
    politica = politica_empresa(config, empresa)
    texto = str(politica.get("observacao") or "").strip()
    if texto:
        return texto
    return f"Auditoria de {area} desativada para esta empresa no config.yaml."


def nome_backup_logico(nome: str) -> str:
    """Agrupa partes .rclone_chunk.NNN como um unico backup logico."""
    return re.sub(r"(?i)\.rclone_chunk\.\d+$", "", str(nome or "").strip())


def eh_chunk_rclone(nome: str) -> bool:
    return nome_backup_logico(nome) != str(nome or "").strip()


def relativo_a_raiz(path: str, raiz: str) -> str | None:
    path_norm = normalizar_path(path)
    raiz_norm = normalizar_path(raiz)
    if path_norm.lower() == raiz_norm.lower():
        return ""
    prefixo = raiz_norm.rstrip("/") + "/"
    if not path_norm.lower().startswith(prefixo.lower()):
        return None
    return path_norm[len(prefixo) :]


def nome_path(path: str) -> str:
    return normalizar_path(path).rstrip("/").rsplit("/", 1)[-1]


def normalizar_nome_busca(nome: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", nome)
    ascii_nome = "".join(char for char in sem_acento if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", ascii_nome.lower())


def nome_parece_pasta_vms(nome: str) -> bool:
    normalizado = normalizar_nome_busca(nome)
    if normalizado in {"vm", "vms"}:
        return True
    if "vms" in normalizado:
        return True
    if "backup" in normalizado and "vm" in normalizado:
        return True
    return difflib.SequenceMatcher(None, normalizado, "vms").ratio() >= 0.67


def detalhes_extensao_backup(nome_arquivo: str) -> tuple[str, str, str]:
    nome = nome_arquivo.lower()
    compactacao = ""
    arquivo_zst = "SIM" if nome.endswith(".zst") else "NAO"

    for candidato in (".zst", ".gz", ".lzo"):
        if nome.endswith(candidato):
            compactacao = candidato.lstrip(".")
            break

    extensoes_compostas = (
        ".vma.zst",
        ".tar.zst",
        ".vma.gz",
        ".tar.gz",
        ".vma.lzo",
        ".tar.lzo",
        ".zst",
        ".gz",
        ".lzo",
    )
    for extensao in extensoes_compostas:
        if nome.endswith(extensao):
            return extensao, compactacao, arquivo_zst

    match = re.search(r"(\.[a-z0-9]+(?:\.[a-z0-9]+)?)$", nome)
    return (match.group(1) if match else "", compactacao, arquivo_zst)


def eh_arquivo_temporario(nome_arquivo: str) -> bool:
    nome = nome_arquivo.lower()
    return nome.endswith(IGNORAR_TEMPORARIOS) or ".tmp." in nome or ".part." in nome


def parse_vzdump_datetime(nome_arquivo: str, tzinfo: ZoneInfo | None = None) -> ParsedVzdump | None:
    nome = nome_arquivo.strip()
    nome_lower = nome.lower()

    if nome_lower.endswith(IGNORAR_EXTENSOES) or eh_arquivo_temporario(nome):
        return None

    match = VZDUMP_RE.match(nome)
    if not match:
        return None

    try:
        data_hora = datetime.strptime(
            f"{match.group('data')}-{match.group('hora')}",
            "%Y_%m_%d-%H_%M_%S",
        )
    except ValueError:
        return None

    if tzinfo is not None:
        data_hora = data_hora.replace(tzinfo=tzinfo)

    extensao_backup, compactacao, arquivo_zst = detalhes_extensao_backup(nome)

    return ParsedVzdump(
        tipo=match.group("tipo").lower(),
        vmid=match.group("vmid"),
        data_hora=data_hora,
        extensao_backup=extensao_backup,
        compactacao=compactacao,
        arquivo_zst=arquivo_zst,
    )


def _mediana_timedelta(diferencas: list[timedelta]) -> timedelta:
    segundos = [d.total_seconds() for d in diferencas]
    return timedelta(seconds=statistics.median(segundos))


def _classificar_mediana(dias: float) -> str:
    if 0.75 <= dias <= 1.35:
        return "diario"
    if 6.0 <= dias <= 8.5:
        return "semanal"
    if 13.0 <= dias <= 17.5:
        return "quinzenal"
    if 27.0 <= dias <= 35.0:
        return "mensal"
    return "irregular"


def detectar_periodicidade(datas: list[datetime]) -> Periodicidade:
    datas_ordenadas = sorted(datas)
    if len(datas_ordenadas) < 2:
        return Periodicidade(
            periodicidade="amostra_insuficiente",
            confianca="INSUFICIENTE",
            mediana=None,
            diferencas_horas=[],
            observacao="Menos de 2 backups validos para prever periodicidade.",
        )

    diferencas = [
        datas_ordenadas[i] - datas_ordenadas[i - 1]
        for i in range(1, len(datas_ordenadas))
    ]
    diferencas_horas = [round(d.total_seconds() / 3600, 2) for d in diferencas]
    mediana = _mediana_timedelta(diferencas)
    mediana_dias = mediana.total_seconds() / 86400
    periodicidade = _classificar_mediana(mediana_dias)

    if len(datas_ordenadas) == 2:
        if periodicidade == "irregular":
            return Periodicidade(
                periodicidade="irregular",
                confianca="BAIXA",
                mediana=mediana,
                diferencas_horas=diferencas_horas,
                observacao="Estimativa com apenas 2 backups, sem encaixe em padrao conhecido.",
            )
        return Periodicidade(
            periodicidade=periodicidade,
            confianca="BAIXA",
            mediana=mediana,
            diferencas_horas=diferencas_horas,
            observacao="Estimativa com apenas 2 backups; confianca baixa.",
        )

    if periodicidade == "irregular":
        return Periodicidade(
            periodicidade="irregular",
            confianca="IRREGULAR",
            mediana=mediana,
            diferencas_horas=diferencas_horas,
            observacao="Mediana das diferencas nao corresponde a um padrao diario, semanal, quinzenal ou mensal.",
        )

    mediana_horas = max(mediana.total_seconds() / 3600, 0.01)
    # Lacunas equivalentes a multiplos inteiros do ciclo nao tornam todo o padrao irregular.
    # Ex.: 7, 14, 7 dias continua sendo semanal, com um ciclo possivelmente ausente.
    erros_ciclo: list[float] = []
    multiplos: list[int] = []
    for diferenca in diferencas:
        horas = max(diferenca.total_seconds() / 3600, 0.01)
        multiplo = max(1, round(horas / mediana_horas))
        multiplos.append(multiplo)
        erros_ciclo.append(abs(horas - (multiplo * mediana_horas)) / mediana_horas)
    maior_erro_ciclo = max(erros_ciclo)
    houve_lacuna = any(m > 1 for m in multiplos)

    if periodicidade == "mensal":
        # Para meses, a mediana pode variar entre 28 e 31 dias. Aceitamos multiplos com margem maior.
        limite_alta, limite_media = 0.20, 0.38
    else:
        limite_alta, limite_media = 0.15, 0.35

    if maior_erro_ciclo <= limite_alta and not houve_lacuna:
        confianca = "ALTA"
        observacao = "Diferencas consistentes pelo historico da pasta."
    elif maior_erro_ciclo <= limite_media:
        confianca = "MEDIA"
        observacao = "Padrao consistente com variacao de horario."
        if houve_lacuna:
            observacao += " Ha intervalo equivalente a multiplos do ciclo; um ou mais uploads podem ter faltado no historico."
    else:
        return Periodicidade(
            periodicidade="irregular",
            confianca="IRREGULAR",
            mediana=mediana,
            diferencas_horas=diferencas_horas,
            observacao="Os intervalos nao correspondem ao ciclo nem a multiplos inteiros dele; revisar historico.",
        )

    return Periodicidade(
        periodicidade=periodicidade,
        confianca=confianca,
        mediana=mediana,
        diferencas_horas=diferencas_horas,
        observacao=observacao,
    )


def prever_proximo_backup(
    ultimo_backup: datetime,
    periodicidade: str,
    mediana: timedelta | None = None,
) -> datetime | None:
    if periodicidade == "diario":
        return ultimo_backup + timedelta(days=1)
    if periodicidade == "semanal":
        return ultimo_backup + timedelta(days=7)
    if periodicidade == "quinzenal":
        return ultimo_backup + timedelta(days=15)
    if periodicidade == "mensal":
        return ultimo_backup + relativedelta(months=1)
    if periodicidade == "irregular" and mediana is not None:
        return ultimo_backup + mediana
    return None


def calcular_status(now: datetime, proximo_backup: datetime | None, tolerancia: timedelta) -> str:
    if proximo_backup is None:
        return "AMOSTRA_INSUFICIENTE"
    return "ATRASADO" if now > proximo_backup + tolerancia else "OK"


def formatar_bytes_gb(bytes_value: int | None) -> float | str:
    if bytes_value is None:
        return ""
    return round(bytes_value / (1024**3), 3)


def formatar_dt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.isoformat(timespec="seconds")


def converter_dropbox_datetime(dt: datetime | None, tzinfo: ZoneInfo) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tzinfo)


def chamada_com_retry(func: Any, *args: Any, logger: logging.Logger, max_tentativas: int = 6, **kwargs: Any) -> Any:
    espera = 2.0
    for tentativa in range(1, max_tentativas + 1):
        try:
            return func(*args, **kwargs)
        except RateLimitError as exc:
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is None:
                retry_after = espera
            retry_after = max(float(retry_after), espera)
            logger.warning("Rate limit na API Dropbox. Tentativa %s/%s; aguardando %.1fs.", tentativa, max_tentativas, retry_after)
            print(f"Rate limit do Dropbox. Aguardando {retry_after:.0f}s antes de tentar novamente...")
            time.sleep(retry_after)
            espera = min(espera * 2, 60)
        except (InternalServerError, HttpError, requests.exceptions.RequestException) as exc:
            if tentativa >= max_tentativas:
                raise
            logger.warning("Erro transitorio Dropbox (%s). Tentativa %s/%s; aguardando %.1fs.", exc, tentativa, max_tentativas, espera)
            time.sleep(espera)
            espera = min(espera * 2, 60)

    raise RuntimeError("Tentativas esgotadas ao chamar a API do Dropbox.")


def erro_pasta_nao_encontrada(exc: ApiError) -> bool:
    texto = str(exc).lower()
    return "not_found" in texto or "not found" in texto


def gerar_refresh_token_interativo() -> int:
    app_key = os.environ.get("DROPBOX_APP_KEY") or input("DROPBOX_APP_KEY: ").strip()
    app_secret = os.environ.get("DROPBOX_APP_SECRET") or getpass.getpass("DROPBOX_APP_SECRET: ").strip()

    if not app_key or not app_secret:
        print("ERRO: App key e app secret sao obrigatorios.")
        return 2

    auth_flow = DropboxOAuth2FlowNoRedirect(
        consumer_key=app_key,
        consumer_secret=app_secret,
        token_access_type="offline",
        scope=["files.metadata.read"],
    )

    authorize_url = auth_flow.start()
    print("\nAbra este link no navegador, autorize o app e copie o codigo gerado:\n")
    print(authorize_url)
    auth_code = input("\nCole o codigo de autorizacao aqui: ").strip()

    try:
        result = auth_flow.finish(auth_code)
    except Exception as exc:  # noqa: BLE001
        print(f"ERRO ao concluir OAuth: {exc}")

        response = getattr(exc, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", "")
            text = getattr(response, "text", "")
            if status_code:
                print(f"Status HTTP: {status_code}")
            if text:
                print("Resposta do Dropbox:")
                print(text)

        print("\nCausas mais comuns:")
        print("1. O codigo de autorizacao ja foi usado. Gere outro link e outro codigo.")
        print("2. O codigo expirou. Gere outro link e use o codigo imediatamente.")
        print("3. Foi colado access token em vez do codigo de autorizacao.")
        print("4. App key ou app secret foram digitados errados.")
        print("5. App key e app secret pertencem a apps diferentes.")
        print("6. A permissao files.metadata.read nao foi salva na aba Permissions do app.")
        return 3

    print("\nRefresh token gerado com sucesso.")
    print("Guarde este valor em local seguro. Ele permite renovar access tokens automaticamente.\n")
    print("DROPBOX_REFRESH_TOKEN:")
    print(result.refresh_token)
    print("\nPowerShell sugerido:")
    print(f'$env:DROPBOX_APP_KEY = "{app_key}"')
    print('$env:DROPBOX_APP_SECRET = "COLE_O_APP_SECRET_AQUI"')
    print(f'$env:DROPBOX_REFRESH_TOKEN = "{result.refresh_token}"')
    return 0


def criar_cliente_dropbox(logger: logging.Logger, timeout_segundos: int | None = None) -> dropbox.Dropbox:
    refresh_token = os.environ.get("DROPBOX_REFRESH_TOKEN")
    app_key = os.environ.get("DROPBOX_APP_KEY")
    app_secret = os.environ.get("DROPBOX_APP_SECRET")
    access_token = os.environ.get("DROPBOX_ACCESS_TOKEN")

    if timeout_segundos is None:
        try:
            timeout_segundos = int(os.environ.get("DROPBOX_TIMEOUT", "120"))
        except ValueError:
            timeout_segundos = 120
    timeout_segundos = max(int(timeout_segundos), 30)

    if refresh_token:
        if not app_key or not app_secret:
            raise RuntimeError(
                "DROPBOX_REFRESH_TOKEN foi definido, mas faltam DROPBOX_APP_KEY e/ou DROPBOX_APP_SECRET."
            )

        logger.info("Criando cliente Dropbox com refresh token. Timeout=%ss", timeout_segundos)
        return dropbox.Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
            timeout=timeout_segundos,
            user_agent="DropboxBackupAuditor/2.5",
        )

    if access_token:
        logger.info("Criando cliente Dropbox com access token temporario. Timeout=%ss", timeout_segundos)
        return dropbox.Dropbox(
            oauth2_access_token=access_token,
            timeout=timeout_segundos,
            user_agent="DropboxBackupAuditor/2.5",
        )

    raise RuntimeError(
        "Defina DROPBOX_REFRESH_TOKEN + DROPBOX_APP_KEY + DROPBOX_APP_SECRET "
        "ou, para teste, DROPBOX_ACCESS_TOKEN."
    )


def listar_pasta_dropbox(
    dbx: dropbox.Dropbox,
    pasta: str,
    logger: logging.Logger,
    stats: EstatisticasExecucao,
    recursive: bool = False,
    quieto: bool = False,
) -> list[Any]:
    pasta = normalizar_path(pasta)
    try:
        resultado = chamada_com_retry(
            dbx.files_list_folder,
            "" if pasta == "/" else pasta,
            recursive=recursive,
            include_deleted=False,
            limit=500,
            logger=logger,
        )
    except ApiError as exc:
        if erro_pasta_nao_encontrada(exc):
            return []
        raise

    entradas = list(resultado.entries)
    stats.paginas_api += 1
    stats.metadados_lidos += len(resultado.entries)

    paginas_local = 1
    if not quieto:
        tipo = "recursiva" if recursive else "direta"
        print(f"Listagem {tipo}: {pasta} | pagina {paginas_local} | itens nesta pasta: {len(entradas)}")

    while resultado.has_more:
        resultado = chamada_com_retry(
            dbx.files_list_folder_continue,
            resultado.cursor,
            logger=logger,
        )
        paginas_local += 1
        stats.paginas_api += 1
        stats.metadados_lidos += len(resultado.entries)
        entradas.extend(resultado.entries)

        if not quieto:
            print(
                f"Listagem: {pasta} | pagina {paginas_local} | "
                f"itens acumulados nesta pasta: {len(entradas)} | "
                f"metadados totais lidos: {stats.metadados_lidos}"
            )

    return entradas


def resumir_pasta_arquivos_dropbox(
    dbx: dropbox.Dropbox,
    pasta: str,
    logger: logging.Logger,
    stats: EstatisticasExecucao,
    quieto: bool,
    max_paginas: int = 0,
) -> dict[str, Any]:
    pasta = normalizar_path(pasta)

    try:
        resultado = chamada_com_retry(
            dbx.files_list_folder,
            "" if pasta == "/" else pasta,
            recursive=True,
            include_deleted=False,
            limit=500,
            logger=logger,
            max_tentativas=1,
        )
    except ApiError as exc:
        if erro_pasta_nao_encontrada(exc):
            return {"existe": False, "quantidade": 0, "tamanho_total": 0, "mais_recente": None}
        raise

    resumo: dict[str, Any] = {
        "existe": True,
        "quantidade": 0,
        "tamanho_total": 0,
        "mais_recente": None,
        "incompleto": False,
        "paginas_lidas": 0,
    }
    paginas_local = 0

    while True:
        paginas_local += 1
        resumo["paginas_lidas"] = paginas_local
        stats.paginas_api += 1
        stats.metadados_lidos += len(resultado.entries)

        for entrada in resultado.entries:
            if not isinstance(entrada, FileMetadata):
                continue
            resumo["quantidade"] += 1
            resumo["tamanho_total"] += int(entrada.size or 0)
            stats.arquivos_lidos += 1
            mais_recente = resumo["mais_recente"]
            if mais_recente is None or (entrada.server_modified or datetime.min) > (
                mais_recente.server_modified or datetime.min
            ):
                resumo["mais_recente"] = entrada

        if not quieto:
            print(
                f"  Pasta arquivos: {pasta} | pagina {paginas_local} | "
                f"arquivos lidos: {resumo['quantidade']} | metadados totais: {stats.metadados_lidos}"
            )

        if not resultado.has_more:
            break

        if max_paginas > 0 and paginas_local >= max_paginas:
            resumo["incompleto"] = True
            logger.warning("Limite de %s pagina(s) atingido ao listar %s.", max_paginas, pasta)
            break

        resultado = chamada_com_retry(
            dbx.files_list_folder_continue,
            resultado.cursor,
            logger=logger,
            max_tentativas=1,
        )

    return resumo


def listar_subpastas_arquivos(
    dbx: dropbox.Dropbox,
    arquivos_path: str,
    logger: logging.Logger,
    stats: EstatisticasExecucao,
    quieto: bool,
    max_subpastas: int,
) -> list[str]:
    subpastas: list[str] = []
    visitadas: set[str] = set()

    def adicionar(path: str) -> None:
        path_norm = normalizar_path(path)
        if path_norm.lower() in visitadas:
            return
        visitadas.add(path_norm.lower())
        subpastas.append(path_norm)

    diretas = listar_pasta_dropbox(dbx, arquivos_path, logger, stats, recursive=False, quieto=True)
    for entrada in diretas:
        if not isinstance(entrada, FolderMetadata):
            continue
        subpasta_path = normalizar_path(entrada.path_display or path_join(arquivos_path, entrada.name))
        adicionar(subpasta_path)
        if max_subpastas > 0 and len(subpastas) >= max_subpastas:
            return subpastas

        try:
            segundo_nivel = listar_pasta_dropbox(dbx, subpasta_path, logger, stats, recursive=False, quieto=True)
        except ApiError:
            continue
        for subentrada in segundo_nivel:
            if not isinstance(subentrada, FolderMetadata):
                continue
            adicionar(normalizar_path(subentrada.path_display or path_join(subpasta_path, subentrada.name)))
            if max_subpastas > 0 and len(subpastas) >= max_subpastas:
                return subpastas

    if not quieto and subpastas:
        print(f"  Subpastas de arquivos para analisar: {len(subpastas)}")
    return subpastas


def listar_empresas(
    dbx: dropbox.Dropbox,
    raiz: str,
    logger: logging.Logger,
    stats: EstatisticasExecucao,
    empresas_filtradas: list[str],
    limite_empresas: int,
    quieto: bool,
) -> dict[str, str]:
    entradas_raiz = listar_pasta_dropbox(dbx, raiz, logger, stats, recursive=False, quieto=quieto)
    empresas: dict[str, str] = {}

    filtros_lower = {empresa.lower() for empresa in empresas_filtradas}

    for entrada in entradas_raiz:
        if not isinstance(entrada, FolderMetadata):
            continue
        nome = entrada.name
        if filtros_lower and nome.lower() not in filtros_lower:
            continue
        empresas[nome] = normalizar_path(entrada.path_display or path_join(raiz, nome))

    empresas = dict(sorted(empresas.items(), key=lambda item: item[0].lower()))

    if limite_empresas > 0:
        empresas = dict(list(empresas.items())[:limite_empresas])

    return empresas


def listar_vm_folders_empresa(
    dbx: dropbox.Dropbox,
    empresa_path: str,
    config: dict[str, Any],
    logger: logging.Logger,
    stats: EstatisticasExecucao,
    quieto: bool,
) -> list[VMFolderEncontrada]:
    pasta_vms = config["estrutura"].get("pasta_vms", "VMS/pve")
    prefixo_vm = config["estrutura"].get("prefixo_vm", "vm-")
    caminho_padrao = path_join(empresa_path, pasta_vms)
    prefixo_lower = prefixo_vm.lower()
    cache: dict[str, list[Any]] = {}

    def listar_direto(pasta: str) -> list[Any]:
        pasta_norm = normalizar_path(pasta)
        if pasta_norm not in cache:
            cache[pasta_norm] = listar_pasta_dropbox(dbx, pasta_norm, logger, stats, recursive=False, quieto=True)
        return cache[pasta_norm]

    def vm_folders_em(pasta: str, caminho_vms_usado: str, alternativa: bool) -> list[VMFolderEncontrada]:
        encontrados: list[VMFolderEncontrada] = []
        for entrada in listar_direto(pasta):
            if not isinstance(entrada, FolderMetadata):
                continue
            nome_vm = nome_path(entrada.path_display or entrada.name).lower()
            if not (nome_vm.startswith(prefixo_lower) or nome_vm.isdigit()):
                continue
            obs = ""
            if alternativa:
                obs = f"Backups encontrados em pasta alternativa: {normalizar_path(pasta)}"
            encontrados.append(
                VMFolderEncontrada(
                    folder=entrada,
                    caminho_vms_usado=normalizar_path(caminho_vms_usado),
                    pasta_alternativa=alternativa,
                    observacao_descoberta=obs,
                    pasta_vms_origem=normalizar_path(caminho_vms_usado),
                )
            )
        return encontrados

    def coletar_candidatos_vms(raiz_candidata: str) -> list[str]:
        caminhos = [path_join(raiz_candidata, "pve"), raiz_candidata]
        nivel_1 = [entrada for entrada in listar_direto(raiz_candidata) if isinstance(entrada, FolderMetadata)]
        for subpasta in nivel_1:
            subpasta_path = normalizar_path(subpasta.path_display or path_join(raiz_candidata, subpasta.name))
            caminhos.append(subpasta_path)
            nome_subpasta = nome_path(subpasta_path).lower()
            if nome_subpasta.startswith(prefixo_lower) or nome_subpasta.isdigit():
                continue
            for subsubpasta in listar_direto(subpasta_path):
                if not isinstance(subsubpasta, FolderMetadata):
                    continue
                caminhos.append(normalizar_path(subsubpasta.path_display or path_join(subpasta_path, subsubpasta.name)))
        return caminhos

    empresa_entradas = listar_direto(empresa_path)
    pastas_empresa = [entrada for entrada in empresa_entradas if isinstance(entrada, FolderMetadata)]
    candidatos: list[tuple[str, bool]] = [(caminho_padrao, False)]

    for pasta in pastas_empresa:
        pasta_path = normalizar_path(pasta.path_display or path_join(empresa_path, pasta.name))
        if not nome_parece_pasta_vms(pasta.name):
            continue
        caminhos_possiveis = coletar_candidatos_vms(pasta_path)

        for caminho in caminhos_possiveis:
            caminho_norm = normalizar_path(caminho)
            alternativa = caminho_norm.lower() != caminho_padrao.lower()
            candidatos.append((caminho_norm, alternativa))

    vistos_candidatos: set[str] = set()
    vm_folders: list[VMFolderEncontrada] = []
    vistos_vms: set[str] = set()

    for candidato, alternativa in candidatos:
        chave_candidato = candidato.lower()
        if chave_candidato in vistos_candidatos:
            continue
        vistos_candidatos.add(chave_candidato)
        for vm_info in vm_folders_em(candidato, candidato, alternativa):
            vm_path = normalizar_path(vm_info.folder.path_display or "")
            if vm_path.lower() in vistos_vms:
                continue
            vistos_vms.add(vm_path.lower())
            vm_folders.append(vm_info)

    vm_folders.sort(key=lambda item: (item.pasta_alternativa, nome_path(item.folder.path_display or "").lower()))

    if not quieto:
        print(f"  VMs encontradas em pastas candidatas: {len(vm_folders)}")
        caminhos_usados = sorted({item.caminho_vms_usado for item in vm_folders})
        for caminho in caminhos_usados:
            print(f"    caminho VMS usado: {caminho}")

    return vm_folders


def listar_vms_flat_empresa(
    dbx: dropbox.Dropbox,
    empresa_path: str,
    config: dict[str, Any],
    logger: logging.Logger,
    stats: EstatisticasExecucao,
    quieto: bool,
    tzinfo: ZoneInfo,
) -> list[dict[str, Any]]:
    """Encontra backups vzdump salvos diretamente dentro de uma pasta VMS.

    Alguns clientes nao usam a estrutura /VMS/pve/vm-XXX. Em vez disso, salvam
    os arquivos diretamente em /VMS, misturando varias VMs no mesmo diretorio.
    Nesse caso agrupamos os arquivos pelo VMID encontrado no nome do vzdump.
    """
    pasta_vms_config = config["estrutura"].get("pasta_vms", "VMS/pve")
    caminho_padrao = path_join(empresa_path, pasta_vms_config)
    cache: dict[str, list[Any]] = {}

    def listar_direto(pasta: str) -> list[Any]:
        pasta_norm = normalizar_path(pasta)
        if pasta_norm not in cache:
            cache[pasta_norm] = listar_pasta_dropbox(dbx, pasta_norm, logger, stats, recursive=False, quieto=True)
        return cache[pasta_norm]

    def candidatos_vms() -> list[tuple[str, bool]]:
        candidatos: list[tuple[str, bool]] = [(caminho_padrao, False)]
        vistos: set[str] = {normalizar_path(caminho_padrao).lower()}

        empresa_entradas = listar_direto(empresa_path)
        for entrada in empresa_entradas:
            if not isinstance(entrada, FolderMetadata):
                continue
            if not nome_parece_pasta_vms(entrada.name):
                continue
            pasta_path = normalizar_path(entrada.path_display or path_join(empresa_path, entrada.name))
            for caminho in (pasta_path, path_join(pasta_path, "pve")):
                caminho_norm = normalizar_path(caminho)
                chave = caminho_norm.lower()
                if chave in vistos:
                    continue
                vistos.add(chave)
                candidatos.append((caminho_norm, caminho_norm.lower() != caminho_padrao.lower()))
        return candidatos

    grupos: dict[tuple[str, str], dict[str, Any]] = {}

    for pasta_candidata, alternativa in candidatos_vms():
        entradas = listar_direto(pasta_candidata)
        arquivos_vzdump: list[FileMetadata] = []
        for entrada in entradas:
            if not isinstance(entrada, FileMetadata):
                continue
            parsed = parse_vzdump_datetime(entrada.name, tzinfo)
            if parsed is None:
                continue
            arquivos_vzdump.append(entrada)
            chave = (pasta_candidata.lower(), parsed.vmid)
            if chave not in grupos:
                grupos[chave] = {
                    "vmid": parsed.vmid,
                    "vm_nome": f"vm-{parsed.vmid}",
                    "vm_path": path_join(pasta_candidata, f"vm-{parsed.vmid}"),
                    "caminho_vms_usado": pasta_candidata,
                    "pasta_vms_origem": pasta_candidata,
                    "pasta_vms_alternativa": alternativa,
                    "arquivos": [],
                    "observacao_descoberta": (
                        f"Backups encontrados diretamente em {normalizar_path(pasta_candidata)}, sem subpasta vm-{parsed.vmid}."
                    ),
                }
            grupos[chave]["arquivos"].append(entrada)

        if arquivos_vzdump and not quieto:
            vmids = sorted({parse_vzdump_datetime(arquivo.name, tzinfo).vmid for arquivo in arquivos_vzdump if parse_vzdump_datetime(arquivo.name, tzinfo) is not None})
            print(f"  Backups VMS diretos encontrados em {pasta_candidata}: {len(arquivos_vzdump)} arquivo(s), VMIDs: {', '.join(vmids)}")

    return sorted(grupos.values(), key=lambda item: (item.get("pasta_vms_alternativa", False), str(item.get("vm_nome", ""))))


def tolerancia_para(periodicidade: str, config: dict[str, Any]) -> int:
    tolerancias = config["tolerancias"]
    chave = f"{periodicidade}_horas"
    return int(tolerancias.get(chave, tolerancias.get("irregular_horas", 24)))


def vmid_da_pasta(vm_nome: str) -> str:
    match = re.search(r"(\d+)$", vm_nome)
    return match.group(1) if match else ""


def analisar_vm_por_arquivos(
    empresa: str,
    vm_folder: FolderMetadata,
    arquivos_vm: list[FileMetadata],
    config: dict[str, Any],
    tzinfo: ZoneInfo,
    now: datetime,
    logger: logging.Logger,
    caminho_vms_usado: str = "",
    observacao_extra: str = "",
    pasta_vms_origem: str = "",
    pasta_vms_alternativa: bool = False,
) -> dict[str, Any]:
    vm_path = normalizar_path(vm_folder.path_display or "")
    vm_nome = nome_path(vm_path)
    max_backups = int(config["analise"].get("max_backups_por_vm_para_relatorio", 10))

    try:
        backups: list[dict[str, Any]] = []
        grupos_arquivos: dict[str, list[FileMetadata]] = {}
        for arquivo in arquivos_vm:
            nome_logico = nome_backup_logico(arquivo.name)
            if parse_vzdump_datetime(nome_logico, tzinfo) is None:
                continue
            grupos_arquivos.setdefault(nome_logico, []).append(arquivo)

        for nome_logico, partes in grupos_arquivos.items():
            parsed = parse_vzdump_datetime(nome_logico, tzinfo)
            if parsed is None:
                continue
            partes_ordenadas = sorted(
                partes,
                key=lambda item: converter_dropbox_datetime(item.server_modified, tzinfo) or parsed.data_hora,
            )
            ultima_parte = partes_ordenadas[-1]
            datas_modificacao = [
                dt for dt in (converter_dropbox_datetime(item.server_modified, tzinfo) for item in partes_ordenadas)
                if dt is not None
            ]
            server_modified = max(datas_modificacao) if datas_modificacao else None
            tamanho_bytes = sum(int(item.size or 0) for item in partes_ordenadas)
            caminho = normalizar_path(ultima_parte.path_display or "")
            if eh_chunk_rclone(ultima_parte.name):
                caminho = caminho.rsplit("/", 1)[0] + "/" + nome_logico
            data_referencia = server_modified or parsed.data_hora
            backups.append(
                {
                    "nome": nome_logico,
                    "caminho_dropbox": caminho,
                    "tipo": parsed.tipo,
                    "vmid": parsed.vmid,
                    "data_hora": parsed.data_hora,
                    "server_modified": server_modified,
                    "data_referencia": data_referencia,
                    "tamanho_bytes": tamanho_bytes,
                    "tamanho_gb": formatar_bytes_gb(tamanho_bytes),
                    "extensao_backup": parsed.extensao_backup,
                    "compactacao": parsed.compactacao,
                    "arquivo_zst": parsed.arquivo_zst,
                    "quantidade_partes": len(partes_ordenadas),
                }
            )

        backups.sort(key=lambda item: item["data_referencia"] or item["data_hora"])
        quantidade = len(backups)

        base = {
            "tipo_registro": "VM",
            "empresa": empresa,
            "caminho_dropbox": vm_path,
            "vm": vm_nome,
            "vmid": vmid_da_pasta(vm_nome),
            "status": "",
            "periodicidade_detectada": "",
            "confianca": "",
            "quantidade_backups": quantidade,
            "ultimo_backup": "",
            "data_backup_arquivo": "",
            "referencia_status": "atividade_dropbox",
            "penultimo_backup": "",
            "proximo_backup_previsto": "",
            "tolerancia_horas": "",
            "atraso_horas": "",
            "ultimo_arquivo": "",
            "arquivo_mais_recente": "",
            "caminho_ultimo_arquivo": "",
            "caminho_arquivo_mais_recente": "",
            "tamanho_ultimo_arquivo_gb": "",
            "server_modified_ultimo_arquivo": "",
            "ultima_atualizacao_dropbox": "",
            "quantidade_arquivos": "",
            "tamanho_total_gb": "",
            "caminho_vms_usado": caminho_vms_usado,
            "pasta_vms_origem": pasta_vms_origem or caminho_vms_usado,
            "pasta_vms_alternativa": "SIM" if pasta_vms_alternativa else "NAO",
            "extensao_backup": "",
            "compactacao": "",
            "arquivo_zst": "",
            "observacao": "",
            "ultimos_backups": [],
            "backups_atuais_inventario": [],
        }

        if quantidade == 0:
            observacao_sem_backup = "Nenhum arquivo vzdump valido encontrado na pasta da VM."
            if observacao_extra:
                observacao_sem_backup = f"{observacao_sem_backup} {observacao_extra}"
            base.update(
                {
                    "status": "SEM_BACKUP",
                    "periodicidade_detectada": "sem_backup",
                    "confianca": "INSUFICIENTE",
                    "observacao": observacao_sem_backup,
                }
            )
            return base

        ultimo = backups[-1]
        penultimo = backups[-2] if quantidade >= 2 else None
        datas = [backup["data_referencia"] or backup["data_hora"] for backup in backups]
        periodicidade = detectar_periodicidade(datas)
        proximo = prever_proximo_backup(ultimo["data_referencia"] or ultimo["data_hora"], periodicidade.periodicidade, periodicidade.mediana)
        tolerancia_horas = tolerancia_para(periodicidade.periodicidade, config)
        tolerancia = timedelta(hours=tolerancia_horas)

        if periodicidade.periodicidade == "amostra_insuficiente":
            status = "AMOSTRA_INSUFICIENTE"
        elif periodicidade.periodicidade == "irregular" or periodicidade.confianca == "IRREGULAR":
            status = "IRREGULAR"
        else:
            status = calcular_status(now, proximo, tolerancia)

        atraso_horas: float | str = ""
        if proximo is not None and status == "ATRASADO":
            atraso_horas = round((now - (proximo + tolerancia)).total_seconds() / 3600, 2)
        elif proximo is not None:
            atraso_horas = 0

        vmids_encontrados = sorted({backup["vmid"] for backup in backups})
        observacoes = [periodicidade.observacao]
        if observacao_extra:
            observacoes.append(observacao_extra)
        vmid_pasta = vmid_da_pasta(vm_nome)
        if vmid_pasta and vmids_encontrados and vmids_encontrados != [vmid_pasta]:
            observacoes.append(f"VMID da pasta ({vmid_pasta}) difere dos arquivos ({', '.join(vmids_encontrados)}).")
        observacoes.append("Status calculado pela ultima atividade do arquivo no Dropbox, nao somente pela data escrita no nome do backup.")
        if status == "IRREGULAR":
            observacoes.append("REVISAR: ha historico, mas sem padrao confiavel para cobrar atraso automaticamente.")

        ultimos_backups = [
                    {
                        "arquivo": backup["nome"],
                        "backup_datetime": formatar_dt(backup["data_hora"]),
                        "server_modified": formatar_dt(backup["server_modified"]),
                        "referencia_status": formatar_dt(backup.get("data_referencia")),
                        "tamanho_gb": backup["tamanho_gb"],
                        "extensao_backup": backup["extensao_backup"],
                        "compactacao": backup["compactacao"],
                        "arquivo_zst": backup["arquivo_zst"],
                        "quantidade_partes": backup.get("quantidade_partes", 1),
                    }
            for backup in reversed(backups[-max_backups:])
        ]

        base.update(
            {
                "vmid": vmid_pasta or ultimo["vmid"],
                "status": status,
                "periodicidade_detectada": periodicidade.periodicidade,
                "confianca": periodicidade.confianca,
                # Mantemos a data do arquivo para auditoria tecnica, mas o status e a primeira tela usam a atividade no Dropbox.
                "ultimo_backup": formatar_dt(ultimo["data_referencia"] or ultimo["data_hora"]),
                "data_backup_arquivo": formatar_dt(ultimo["data_hora"]),
                "referencia_status": "atividade_dropbox" if ultimo.get("server_modified") else "data_nome_arquivo",
                "penultimo_backup": formatar_dt((penultimo["data_referencia"] or penultimo["data_hora"])) if penultimo else "",
                "proximo_backup_previsto": formatar_dt(proximo),
                "tolerancia_horas": tolerancia_horas,
                "atraso_horas": atraso_horas,
                "ultimo_arquivo": ultimo["nome"],
                "arquivo_mais_recente": ultimo["nome"],
                "caminho_ultimo_arquivo": ultimo["caminho_dropbox"],
                "caminho_arquivo_mais_recente": ultimo["caminho_dropbox"],
                "tamanho_ultimo_arquivo_gb": ultimo["tamanho_gb"],
                "server_modified_ultimo_arquivo": formatar_dt(ultimo["server_modified"]),
                "ultima_atualizacao_dropbox": formatar_dt(ultimo["server_modified"]),
                "extensao_backup": ultimo["extensao_backup"],
                "compactacao": ultimo["compactacao"],
                "arquivo_zst": ultimo["arquivo_zst"],
                "observacao": " ".join(observacoes).strip(),
                # Inventário completo da pasta da VM usado somente pelo histórico.
                # A lista limitada acima continua sendo usada na apresentação do relatório.
                "backups_atuais_inventario": [
                    {
                        "arquivo": backup["nome"],
                        "backup_datetime": formatar_dt(backup["data_hora"]),
                        "server_modified": formatar_dt(backup["server_modified"]),
                        "referencia_status": formatar_dt(backup.get("data_referencia")),
                        "caminho": backup["caminho_dropbox"],
                        "tamanho_gb": backup["tamanho_gb"],
                        "extensao_backup": backup["extensao_backup"],
                        "compactacao": backup["compactacao"],
                        "arquivo_zst": backup["arquivo_zst"],
                        "quantidade_partes": backup.get("quantidade_partes", 1),
                    }
                    for backup in backups
                ],
                "ultimos_backups": ultimos_backups,
                "diferencas_horas": periodicidade.diferencas_horas,
            }
        )
        return base

    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao processar VM %s (%s): %s", vm_nome, vm_path, exc)
        return {
            "tipo_registro": "VM",
            "empresa": empresa,
            "caminho_dropbox": vm_path,
            "vm": vm_nome,
            "vmid": vmid_da_pasta(vm_nome),
            "status": "ERRO",
            "periodicidade_detectada": "",
            "confianca": "",
            "quantidade_backups": 0,
            "ultimo_backup": "",
            "data_backup_arquivo": "",
            "referencia_status": "atividade_dropbox",
            "penultimo_backup": "",
            "proximo_backup_previsto": "",
            "tolerancia_horas": "",
            "atraso_horas": "",
            "ultimo_arquivo": "",
            "arquivo_mais_recente": "",
            "caminho_ultimo_arquivo": "",
            "caminho_arquivo_mais_recente": "",
            "tamanho_ultimo_arquivo_gb": "",
            "server_modified_ultimo_arquivo": "",
            "ultima_atualizacao_dropbox": "",
            "quantidade_arquivos": "",
            "tamanho_total_gb": "",
            "caminho_vms_usado": caminho_vms_usado,
            "pasta_vms_origem": pasta_vms_origem or caminho_vms_usado,
            "pasta_vms_alternativa": "SIM" if pasta_vms_alternativa else "NAO",
            "extensao_backup": "",
            "compactacao": "",
            "arquivo_zst": "",
            "observacao": f"Falha ao processar pasta: {exc}",
            "ultimos_backups": [],
            "backups_atuais_inventario": [],
        }


def analisar_pasta_arquivos_empresa(
    dbx: dropbox.Dropbox,
    empresa: str,
    empresa_path: str,
    config: dict[str, Any],
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    stats: EstatisticasExecucao,
    quieto: bool,
) -> dict[str, Any]:
    pasta_arquivos = config["estrutura"].get("pasta_arquivos", "arquivos")
    arquivos_path = path_join(empresa_path, pasta_arquivos)
    max_paginas = int(config["analise"].get("arquivos_max_paginas_por_empresa", 50))
    return analisar_pasta_arquivos_generica(
        dbx=dbx,
        empresa=empresa,
        pasta_path=arquivos_path,
        tipo_registro="ARQUIVOS_EMPRESA",
        config=config,
        tzinfo=tzinfo,
        logger=logger,
        stats=stats,
        quieto=quieto,
        max_paginas=max_paginas,
        observacao_nao_existe="Pasta arquivos nao existe para esta empresa.",
    )


def analisar_pasta_arquivos_generica(
    dbx: dropbox.Dropbox,
    empresa: str,
    pasta_path: str,
    tipo_registro: str,
    config: dict[str, Any],
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    stats: EstatisticasExecucao,
    quieto: bool,
    max_paginas: int,
    observacao_nao_existe: str,
) -> dict[str, Any]:
    arquivos_path = normalizar_path(pasta_path)
    base = {
        "tipo_registro": tipo_registro,
        "empresa": empresa,
        "caminho_dropbox": arquivos_path,
        "vm": "",
        "vmid": "",
        "status": "NAO_EXISTE",
        "periodicidade_detectada": "nao_aplicavel",
        "confianca": "",
        "quantidade_backups": "",
        "ultimo_backup": "",
        "penultimo_backup": "",
        "proximo_backup_previsto": "",
        "tolerancia_horas": "",
        "atraso_horas": "",
        "ultimo_arquivo": "",
        "arquivo_mais_recente": "",
        "caminho_ultimo_arquivo": "",
        "caminho_arquivo_mais_recente": "",
        "tamanho_ultimo_arquivo_gb": "",
        "server_modified_ultimo_arquivo": "",
        "ultima_atualizacao_dropbox": "",
        "quantidade_arquivos": 0,
        "tamanho_total_gb": 0,
        "caminho_vms_usado": "",
        "pasta_vms_origem": "",
        "pasta_vms_alternativa": "",
        "extensao_backup": "",
        "compactacao": "",
        "arquivo_zst": "",
        "observacao": observacao_nao_existe,
        "ultimos_backups": [],
    }

    if not quieto:
        print(f"  Analisando {tipo_registro}: {arquivos_path}")

    try:
        resumo = resumir_pasta_arquivos_dropbox(
            dbx,
            arquivos_path,
            logger,
            stats,
            quieto=quieto,
            max_paginas=max_paginas,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao analisar pasta arquivos %s: %s", arquivos_path, exc)
        base.update(
            {
                "status": "ERRO",
                "observacao": f"Falha ao analisar pasta arquivos: {exc}",
            }
        )
        return base

    if not resumo["existe"]:
        return base

    quantidade = int(resumo["quantidade"])
    tamanho_total = int(resumo["tamanho_total"])

    if quantidade == 0:
        base.update(
            {
                "status": "VAZIA",
                "observacao": "Pasta arquivos existe, mas nao possui arquivos.",
                "quantidade_arquivos": 0,
                "tamanho_total_gb": formatar_bytes_gb(0),
            }
        )
        return base

    mais_recente = resumo["mais_recente"]
    server_modified = converter_dropbox_datetime(mais_recente.server_modified, tzinfo)
    max_idade_horas = int(config["analise"].get("arquivos_max_idade_horas", 36))
    idade_horas = ""
    status = "ERRO"
    observacao_status = "Nao foi possivel calcular a idade do arquivo mais recente."
    if server_modified is not None:
        now = datetime.now(tzinfo)
        idade_horas = round(max((now - server_modified).total_seconds() / 3600, 0), 2)
        status = "OK" if idade_horas <= max_idade_horas else "ATRASADO"
        observacao_status = (
            f"Pasta arquivos dentro do limite de {max_idade_horas}h."
            if status == "OK"
            else f"Arquivo mais recente acima do limite de {max_idade_horas}h."
        )

    if resumo.get("incompleto"):
        observacao_status = (
            f"{observacao_status} Listagem interrompida apos {resumo.get('paginas_lidas')} pagina(s) "
            f"para evitar execucao longa. A contagem e o arquivo mais recente podem estar parciais."
        )

    base.update(
        {
            "status": status,
            "atraso_horas": idade_horas,
            "tolerancia_horas": max_idade_horas,
            "ultimo_arquivo": mais_recente.name,
            "arquivo_mais_recente": mais_recente.name,
            "caminho_ultimo_arquivo": normalizar_path(mais_recente.path_display or ""),
            "caminho_arquivo_mais_recente": normalizar_path(mais_recente.path_display or ""),
            "tamanho_ultimo_arquivo_gb": formatar_bytes_gb(int(mais_recente.size or 0)),
            "server_modified_ultimo_arquivo": formatar_dt(server_modified),
            "ultima_atualizacao_dropbox": formatar_dt(server_modified),
            "observacao": (
                f"Pasta arquivos possui {quantidade} arquivo(s), "
                f"{formatar_bytes_gb(tamanho_total)} GB no total. "
                f"{observacao_status}"
            ),
            "quantidade_arquivos": quantidade,
            "tamanho_total_gb": formatar_bytes_gb(tamanho_total),
        }
    )
    return base


def registro_sem_pasta_vms(empresa: str, empresa_path: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "tipo_registro": "EMPRESA",
        "empresa": empresa,
        "caminho_dropbox": path_join(empresa_path, config["estrutura"].get("pasta_vms", "VMS/pve")),
        "vm": "",
        "vmid": "",
        "status": "SEM_PASTA_VMS",
        "periodicidade_detectada": "nao_aplicavel",
        "confianca": "",
        "quantidade_backups": "",
        "ultimo_backup": "",
        "penultimo_backup": "",
        "proximo_backup_previsto": "",
        "tolerancia_horas": "",
        "atraso_horas": "",
        "ultimo_arquivo": "",
        "arquivo_mais_recente": "",
        "caminho_ultimo_arquivo": "",
        "caminho_arquivo_mais_recente": "",
        "tamanho_ultimo_arquivo_gb": "",
        "server_modified_ultimo_arquivo": "",
        "ultima_atualizacao_dropbox": "",
        "quantidade_arquivos": "",
        "tamanho_total_gb": "",
        "caminho_vms_usado": path_join(empresa_path, config["estrutura"].get("pasta_vms", "VMS/pve")),
        "pasta_vms_origem": path_join(empresa_path, config["estrutura"].get("pasta_vms", "VMS/pve")),
        "pasta_vms_alternativa": "NAO",
        "extensao_backup": "",
        "compactacao": "",
        "arquivo_zst": "",
        "observacao": "Pasta VMS nao encontrada ou sem pastas de VM reconhecidas (vm-100, 100 ou backups diretos).",
        "ultimos_backups": [],
        "backups_atuais_inventario": [],
    }


def resumo_geral(registros: list[dict[str, Any]], total_empresas: int) -> dict[str, Any]:
    vms = [registro for registro in registros if registro.get("tipo_registro") == "VM"]
    arquivos_empresa = [registro for registro in registros if registro.get("tipo_registro") == "ARQUIVOS_EMPRESA"]
    arquivos_subpastas = [registro for registro in registros if registro.get("tipo_registro") == "ARQUIVOS_SUBPASTA"]
    empresas_com_vm = {registro.get("empresa") for registro in vms if registro.get("empresa")}
    empresas_sem_pasta_vms = {
        registro.get("empresa")
        for registro in registros
        if registro.get("status") == "SEM_PASTA_VMS" and registro.get("empresa")
    }
    empresas_com_erro = {
        registro.get("empresa")
        for registro in registros
        if registro.get("tipo_registro") == "EMPRESA"
        and registro.get("status") == "ERRO"
        and registro.get("empresa")
    }
    return {
        "total_empresas": total_empresas,
        "empresas_com_vm": len(empresas_com_vm),
        "empresas_sem_pasta_vms": len(empresas_sem_pasta_vms),
        "empresas_com_erro": len(empresas_com_erro),
        "total_vms": len(vms),
        "vms_ok": sum(1 for vm in vms if vm.get("status") == "OK"),
        "vms_atrasadas": sum(1 for vm in vms if vm.get("status") == "ATRASADO"),
        "vms_irregulares": sum(1 for vm in vms if vm.get("status") == "IRREGULAR"),
        "vms_amostra_insuficiente": sum(1 for vm in vms if vm.get("status") == "AMOSTRA_INSUFICIENTE"),
        "vms_em_aprendizado": sum(1 for vm in vms if vm.get("status") == "EM_APRENDIZADO"),
        "vms_sem_upload_recente": sum(1 for vm in vms if vm.get("status") == "SEM_UPLOAD_RECENTE"),
        "empresas_migradas_drive": sum(1 for item in registros if item.get("status") == "MIGRADO_PARA_DRIVE"),
        "vms_sem_backup": sum(1 for vm in vms if vm.get("status") == "SEM_BACKUP"),
        "vms_erro": sum(1 for vm in vms if vm.get("status") == "ERRO"),
        "vms_nao_encontradas": sum(1 for vm in vms if vm.get("status") == "VM_NAO_ENCONTRADA"),
        "eventos_vms_incompletos": sum(1 for item in registros if item.get("status") == "EVENTOS_VMS_INCOMPLETOS"),
        "eventos_vms_erro": sum(1 for item in registros if item.get("status") == "ERRO_EVENTOS_VMS"),
        "total_pastas_arquivos_ok": sum(1 for item in arquivos_empresa if item.get("status") == "OK"),
        "total_pastas_arquivos_atrasadas": sum(1 for item in arquivos_empresa if item.get("status") == "ATRASADO"),
        "total_pastas_arquivos_vazias": sum(1 for item in arquivos_empresa if item.get("status") == "VAZIA"),
        "total_pastas_arquivos_nao_existe": sum(1 for item in arquivos_empresa if item.get("status") == "NAO_EXISTE"),
        "total_pastas_arquivos_erro": sum(1 for item in arquivos_empresa if item.get("status") == "ERRO"),
        "total_subpastas_arquivos_ok": sum(1 for item in arquivos_subpastas if item.get("status") == "OK"),
        "total_subpastas_arquivos_atrasadas": sum(1 for item in arquivos_subpastas if item.get("status") == "ATRASADO"),
        "total_subpastas_arquivos_vazias": sum(1 for item in arquivos_subpastas if item.get("status") == "VAZIA"),
        "total_subpastas_arquivos_erro": sum(1 for item in arquivos_subpastas if item.get("status") == "ERRO"),
    }


def linha_csv(registro: dict[str, Any]) -> dict[str, Any]:
    return {coluna: registro.get(coluna, "") for coluna in CSV_COLUMNS}


def ordenacao_registro(registro: dict[str, Any]) -> tuple[int, str, str, str]:
    return (
        STATUS_GRAVIDADE.get(str(registro.get("status", "")), 99),
        str(registro.get("empresa", "")).lower(),
        str(registro.get("tipo_registro", "")).lower(),
        str(registro.get("vm", "")).lower(),
    )


def gerar_csv(registros: list[dict[str, Any]], caminho: Path) -> None:
    with caminho.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS, delimiter=";")
        writer.writeheader()
        for registro in sorted(registros, key=ordenacao_registro):
            writer.writerow(sanitize_csv_mapping(linha_csv(registro)))


def gerar_json(
    registros: list[dict[str, Any]],
    resumo: dict[str, Any],
    caminho: Path,
    executado_em: datetime,
    stats: EstatisticasExecucao,
    parcial: bool,
) -> None:
    payload = {
        "schema_version": "1.0",
        "versao": VERSION,
        "parcial": parcial,
        "executado_em": formatar_dt(executado_em),
        "resumo": resumo,
        "estatisticas_execucao": {
            "paginas_api": stats.paginas_api,
            "metadados_lidos": stats.metadados_lidos,
            "empresas_processadas": stats.empresas_processadas,
            "vms_processadas": stats.vms_processadas,
            "arquivos_lidos": stats.arquivos_lidos,
        },
        "registros": [enriquecer_registro(registro) for registro in registros],
    }
    with caminho.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def gerar_relatorios(
    registros: list[dict[str, Any]],
    config: dict[str, Any],
    base_dir: Path,
    tzinfo: ZoneInfo,
    stats: EstatisticasExecucao,
    parcial: bool = False,
    total_empresas_encontradas: int | None = None,
) -> dict[str, Path | dict[str, Any]]:
    relatorios_dir = base_dir / "relatorios"
    relatorios_dir.mkdir(parents=True, exist_ok=True)
    executado_em = datetime.now(tzinfo)
    sufixo = executado_em.strftime("%Y%m%d_%H%M%S")
    prefixo = "auditoria_backups_dropbox_parcial" if parcial else "auditoria_backups_dropbox"

    resumo = resumo_geral(
        registros=registros,
        total_empresas=(
            total_empresas_encontradas
            if total_empresas_encontradas is not None
            else len({registro.get("empresa") for registro in registros if registro.get("empresa")})
        ),
    )

    gerados: dict[str, Path | dict[str, Any]] = {}
    relatorio_cfg = config.get("relatorio", {})

    if relatorio_cfg.get("gerar_csv", True):
        caminho_csv = relatorios_dir / f"{prefixo}_{sufixo}.csv"
        gerar_csv(registros, caminho_csv)
        gerados["csv"] = caminho_csv

    if relatorio_cfg.get("gerar_json", True):
        caminho_json = relatorios_dir / f"{prefixo}_{sufixo}.json"
        gerar_json(registros, resumo, caminho_json, executado_em, stats, parcial)
        gerados["json"] = caminho_json

    if relatorio_cfg.get("gerar_pdf", True):
        caminho_pdf = relatorios_dir / f"{prefixo}_{sufixo}.pdf"
        pdf_reports.gerar_pdf_vms(registros, resumo, executado_em, caminho_pdf)
        gerados["pdf"] = caminho_pdf

    gerados["_resumo"] = resumo
    return gerados


def salvar_parcial(
    registros: list[dict[str, Any]],
    config: dict[str, Any],
    base_dir: Path,
    tzinfo: ZoneInfo,
    stats: EstatisticasExecucao,
    logger: logging.Logger,
) -> None:
    if not registros:
        return
    try:
        gerados = gerar_relatorios(registros, config, base_dir, tzinfo, stats, parcial=True)
        logger.info("Relatorio parcial salvo: %s", {k: str(v) for k, v in gerados.items() if k != "_resumo"})
        print("Relatorio parcial salvo em relatorios/.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao salvar relatorio parcial: %s", exc)
        print(f"ATENCAO: nao foi possivel salvar relatorio parcial: {exc}")


def dt_registro(registro: dict[str, Any]) -> datetime | None:
    valor = registro.get("ultimo_backup") or registro.get("server_modified_ultimo_arquivo")
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor))
    except ValueError:
        return None


def chave_vm_registro(registro: dict[str, Any]) -> str:
    vmid = str(registro.get("vmid") or "").strip()
    if vmid:
        return f"vmid:{vmid}"
    return f"nome:{str(registro.get('vm') or '').strip().lower()}"


def deve_substituir_vm(atual: dict[str, Any], candidato: dict[str, Any]) -> bool:
    data_atual = dt_registro(atual)
    data_candidato = dt_registro(candidato)
    if data_atual is None and data_candidato is not None:
        return True
    if data_atual is not None and data_candidato is None:
        return False
    if data_atual is not None and data_candidato is not None and data_candidato != data_atual:
        return data_candidato > data_atual

    atual_alternativo = bool(str(atual.get("observacao", "")).find("pasta alternativa") >= 0)
    candidato_padrao = not bool(str(candidato.get("observacao", "")).find("pasta alternativa") >= 0)
    return candidato_padrao and atual_alternativo


def mesclar_observacao(registro: dict[str, Any], observacao: str) -> None:
    if not observacao:
        return
    atual = str(registro.get("observacao") or "").strip()
    if observacao in atual:
        return
    registro["observacao"] = f"{atual} {observacao}".strip()


def processar_empresas_modo_rapido(
    dbx: dropbox.Dropbox,
    empresas: dict[str, str],
    config: dict[str, Any],
    tzinfo: ZoneInfo,
    logger: logging.Logger,
    stats: EstatisticasExecucao,
    args: argparse.Namespace,
    base_dir: Path,
) -> list[dict[str, Any]]:
    registros: list[dict[str, Any]] = []
    now = datetime.now(tzinfo)
    progress = ProgressEstimator()
    total_empresas = len(empresas)
    incluir_arquivos = bool(args.incluir_arquivos or config.get("performance", {}).get("incluir_arquivos_por_padrao", True))

    for indice, (empresa, empresa_path) in enumerate(empresas.items(), start=1):
        eta = progress.eta_by_units(indice - 1, total_empresas) if indice > 1 else "--"
        print(
            f"\nEmpresa {indice}/{total_empresas}: {empresa} | "
            f"decorrido: {progress.elapsed_text()} | ETA: {eta}"
        )
        logger.info("Processando empresa %s/%s: %s", indice, total_empresas, empresa)

        try:
            if not empresa_audita(config, empresa, "vms"):
                status_fora = status_empresa_fora_escopo(config, empresa)
                registros.append({
                    "tipo_registro": "EMPRESA", "empresa": empresa, "caminho_dropbox": empresa_path,
                    "vm": "", "vmid": "", "status": status_fora,
                    "periodicidade_detectada": "nao_aplicavel", "confianca": "",
                    "quantidade_backups": 0, "ultimo_backup": "", "penultimo_backup": "",
                    "proximo_backup_previsto": "", "tolerancia_horas": "", "atraso_horas": "",
                    "ultimo_arquivo": "", "tamanho_ultimo_arquivo_gb": "",
                    "server_modified_ultimo_arquivo": "",
                    "observacao": observacao_empresa(config, empresa, "VMs no Dropbox"),
                    "ultimos_backups": [],
                })
                stats.empresas_processadas += 1
                logger.info("Empresa %s ignorada na auditoria de VMs: %s", empresa, status_fora)
                continue

            if incluir_arquivos and empresa_audita(config, empresa, "arquivos"):
                pasta_arquivos = config["estrutura"].get("pasta_arquivos", "arquivos")
                arquivos_path = path_join(empresa_path, pasta_arquivos)
                registros.append(
                    analisar_pasta_arquivos_empresa(
                        dbx=dbx,
                        empresa=empresa,
                        empresa_path=empresa_path,
                        config=config,
                        tzinfo=tzinfo,
                        logger=logger,
                        stats=stats,
                        quieto=args.quieto,
                    )
                )
                registro_arquivos_empresa = registros[-1]
                if registro_arquivos_empresa.get("status") != "NAO_EXISTE":
                    max_subpastas = int(config["analise"].get("arquivos_max_subpastas_por_empresa", 10))
                    max_paginas_subpasta = int(config["analise"].get("arquivos_max_paginas_por_subpasta", 3))
                    subpastas = listar_subpastas_arquivos(
                        dbx=dbx,
                        arquivos_path=arquivos_path,
                        logger=logger,
                        stats=stats,
                        quieto=args.quieto,
                        max_subpastas=max_subpastas,
                    )
                    for sub_indice, subpasta_path in enumerate(subpastas, start=1):
                        if not args.quieto:
                            print(f"  Subpasta arquivos {sub_indice}/{len(subpastas)}: {subpasta_path}")
                        registros.append(
                            analisar_pasta_arquivos_generica(
                                dbx=dbx,
                                empresa=empresa,
                                pasta_path=subpasta_path,
                                tipo_registro="ARQUIVOS_SUBPASTA",
                                config=config,
                                tzinfo=tzinfo,
                                logger=logger,
                                stats=stats,
                                quieto=args.quieto,
                                max_paginas=max_paginas_subpasta,
                                observacao_nao_existe="Subpasta de arquivos nao encontrada.",
                            )
                        )

            vm_folders = listar_vm_folders_empresa(
                dbx=dbx,
                empresa_path=empresa_path,
                config=config,
                logger=logger,
                stats=stats,
                quieto=args.quieto,
            )
            vm_flat = listar_vms_flat_empresa(
                dbx=dbx,
                empresa_path=empresa_path,
                config=config,
                logger=logger,
                stats=stats,
                quieto=args.quieto,
                tzinfo=tzinfo,
            )

            if not vm_folders and not vm_flat:
                registros.append(registro_sem_pasta_vms(empresa, empresa_path, config))
            else:
                registros_vm_por_chave: dict[str, dict[str, Any]] = {}

                def adicionar_registro_vm(registro_vm: dict[str, Any]) -> None:
                    chave_vm = chave_vm_registro(registro_vm)
                    registro_atual = registros_vm_por_chave.get(chave_vm)
                    if registro_atual is None:
                        registros_vm_por_chave[chave_vm] = registro_vm
                    elif deve_substituir_vm(registro_atual, registro_vm):
                        mesclar_observacao(
                            registro_vm,
                            f"VM duplicada tambem encontrada em {registro_atual.get('caminho_dropbox')}; mantida a origem com backup mais recente.",
                        )
                        registros_vm_por_chave[chave_vm] = registro_vm
                    else:
                        mesclar_observacao(
                            registro_atual,
                            f"VM duplicada tambem encontrada em {registro_vm.get('caminho_dropbox')}; mantida a origem com backup mais recente.",
                        )

                for vm_indice, vm_info in enumerate(vm_folders, start=1):
                    vm_path = normalizar_path(vm_info.folder.path_display or "")
                    if not args.quieto:
                        print(f"  VM {vm_indice}/{len(vm_folders)}: {nome_path(vm_path)}")

                    entradas_vm = listar_pasta_dropbox(
                        dbx=dbx,
                        pasta=vm_path,
                        logger=logger,
                        stats=stats,
                        recursive=False,
                        quieto=True,
                    )
                    arquivos_vm = [entrada for entrada in entradas_vm if isinstance(entrada, FileMetadata)]
                    stats.arquivos_lidos += len(arquivos_vm)

                    registro_vm = analisar_vm_por_arquivos(
                        empresa=empresa,
                        vm_folder=vm_info.folder,
                        arquivos_vm=arquivos_vm,
                        config=config,
                        tzinfo=tzinfo,
                        now=now,
                        logger=logger,
                        caminho_vms_usado=vm_info.caminho_vms_usado,
                        observacao_extra=vm_info.observacao_descoberta,
                        pasta_vms_origem=vm_info.pasta_vms_origem,
                        pasta_vms_alternativa=vm_info.pasta_alternativa,
                    )
                    adicionar_registro_vm(registro_vm)

                for flat_indice, flat_info in enumerate(vm_flat, start=1):
                    if not args.quieto:
                        print(f"  VM flat {flat_indice}/{len(vm_flat)}: {flat_info.get('vm_nome')} em {flat_info.get('caminho_vms_usado')}")
                    arquivos_vm = list(flat_info.get("arquivos", []))
                    stats.arquivos_lidos += len(arquivos_vm)
                    fake_folder = SimpleNamespace(
                        path_display=flat_info.get("vm_path"),
                        name=flat_info.get("vm_nome"),
                    )
                    registro_vm = analisar_vm_por_arquivos(
                        empresa=empresa,
                        vm_folder=fake_folder,
                        arquivos_vm=arquivos_vm,
                        config=config,
                        tzinfo=tzinfo,
                        now=now,
                        logger=logger,
                        caminho_vms_usado=str(flat_info.get("caminho_vms_usado") or ""),
                        observacao_extra=str(flat_info.get("observacao_descoberta") or ""),
                        pasta_vms_origem=str(flat_info.get("pasta_vms_origem") or ""),
                        pasta_vms_alternativa=bool(flat_info.get("pasta_vms_alternativa")),
                    )
                    adicionar_registro_vm(registro_vm)

                registros_vm = list(registros_vm_por_chave.values())
                if args.limite_vms and args.limite_vms > 0:
                    registros_vm = registros_vm[: args.limite_vms]
                registros.extend(registros_vm)
                stats.vms_processadas += len(registros_vm)

            stats.empresas_processadas += 1

            if args.salvar_parcial_a_cada > 0 and stats.empresas_processadas % args.salvar_parcial_a_cada == 0:
                salvar_parcial(registros, config, base_dir, tzinfo, stats, logger)

        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha ao processar empresa %s: %s", empresa, exc)
            registros.append(
                {
                    "tipo_registro": "EMPRESA",
                    "empresa": empresa,
                    "caminho_dropbox": empresa_path,
                    "vm": "",
                    "vmid": "",
                    "status": "ERRO",
                    "periodicidade_detectada": "",
                    "confianca": "",
                    "quantidade_backups": "",
                    "ultimo_backup": "",
                    "penultimo_backup": "",
                    "proximo_backup_previsto": "",
                    "tolerancia_horas": "",
                    "atraso_horas": "",
                    "ultimo_arquivo": "",
                    "tamanho_ultimo_arquivo_gb": "",
                    "server_modified_ultimo_arquivo": "",
                    "observacao": f"Falha ao processar empresa: {exc}",
                    "ultimos_backups": [],
                }
            )
            stats.empresas_processadas += 1

    return registros


def executar_auditoria(config_path: str | Path, args: argparse.Namespace) -> int:
    base_dir = Path(__file__).resolve().parent.parent
    logger, log_path = configurar_logging(base_dir)
    stats = EstatisticasExecucao()

    try:
        config = carregar_config(resolver_config_path(config_path, base_dir))
        tzinfo = ZoneInfo(config["timezone"])
        raiz = normalizar_path(config["raiz_dropbox"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao carregar configuracao: %s", exc)
        print(f"ERRO ao carregar configuracao: {exc}")
        print(f"Log gerado: {log_path}")
        return 1

    try:
        dbx = criar_cliente_dropbox(logger)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha ao criar cliente Dropbox: %s", exc)
        print(f"ERRO ao preparar cliente Dropbox: {exc}")
        print("Use refresh token: DROPBOX_APP_KEY + DROPBOX_APP_SECRET + DROPBOX_REFRESH_TOKEN.")
        print("Ou, para teste temporario: DROPBOX_ACCESS_TOKEN.")
        return 2

    print("Cliente Dropbox preparado. A permissao sera validada ao listar a pasta raiz.")
    print("Modo rapido ativo: o script NAO vai listar /Aplicativos inteiro recursivamente.")
    print("Por padrao, ele procura VMs em VMS/pve e em pastas alternativas parecidas com VMS.")
    print("A pasta arquivos tambem sera auditada por padrao, por empresa.")

    try:
        empresas = listar_empresas(
            dbx=dbx,
            raiz=raiz,
            logger=logger,
            stats=stats,
            empresas_filtradas=args.empresa,
            limite_empresas=args.limite_empresas,
            quieto=args.quieto,
        )
    except AuthError as exc:
        logger.exception("Token Dropbox invalido ou sem permissao para listar %s: %s", raiz, exc)
        print("ERRO: o Dropbox recusou a listagem da pasta raiz.")
        print("Verifique se a permissao files.metadata.read esta marcada e salva no app.")
        print(f"Detalhe: {exc}")
        return 3
    except ApiError as exc:
        logger.exception("Erro da API Dropbox ao listar raiz %s: %s", raiz, exc)
        print(f"ERRO: falha na API Dropbox ao listar {raiz}: {exc}")
        return 5

    if not empresas:
        print(f"Nenhuma empresa/pasta encontrada diretamente em {raiz}.")
        return 0

    print(f"Empresas encontradas diretamente em {raiz}: {len(empresas)}")

    if args.listar_empresas:
        print("\nEmpresas encontradas:")
        for indice, empresa in enumerate(empresas.keys(), start=1):
            print(f"{indice:03d}. {empresa}")
        print("\nListagem concluida. Nenhuma VM foi auditada porque --listar-empresas foi usado.")
        return 0

    if args.empresa:
        print("Filtro --empresa ativo. Apenas as empresas informadas acima serao auditadas.")
    else:
        print("Nenhum filtro --empresa informado. Todas as empresas encontradas serao auditadas.")

    print("Iniciando auditoria...\n")

    registros: list[dict[str, Any]] = []
    try:
        registros = processar_empresas_modo_rapido(
            dbx=dbx,
            empresas=empresas,
            config=config,
            tzinfo=tzinfo,
            logger=logger,
            stats=stats,
            args=args,
            base_dir=base_dir,
        )
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuario. Salvando relatorio parcial...")
        logger.warning("Execucao interrompida por KeyboardInterrupt.")
        salvar_parcial(registros, config, base_dir, tzinfo, stats, logger)
        print(f"Log gerado: {log_path}")
        return 130

    gerados = gerar_relatorios(
        registros,
        config,
        base_dir,
        tzinfo,
        stats,
        parcial=False,
        total_empresas_encontradas=len(empresas),
    )
    resumo = gerados.pop("_resumo")

    logger.info("Relatorios gerados: %s", {k: str(v) for k, v in gerados.items()})
    logger.info("Resumo: %s", resumo)
    logger.info("Estatisticas: %s", stats)
    logger.info("Log salvo em: %s", log_path)

    print("\nAuditoria concluida.")
    if "csv" in gerados:
        print(f"CSV gerado: {gerados['csv']}")
    if "pdf" in gerados:
        print(f"PDF gerado: {gerados['pdf']}")
    if "json" in gerados:
        print(f"JSON gerado: {gerados['json']}")
    print(f"Log gerado: {log_path}")
    print(f"Total de empresas processadas: {stats.empresas_processadas}")
    print(f"Total de VMs processadas: {stats.vms_processadas}")
    print(f"Paginas API lidas: {stats.paginas_api}")
    print(f"Metadados lidos: {stats.metadados_lidos}")
    print(f"Total de empresas encontradas: {resumo['total_empresas']}")
    print(f"Empresas com VM: {resumo.get('empresas_com_vm', 0)}")
    print(f"Empresas sem VMS/VMS-pve: {resumo.get('empresas_sem_pasta_vms', 0)}")
    print(f"Total de VMs no relatorio: {resumo['total_vms']}")
    print(f"Total de atrasados: {resumo['vms_atrasadas']}")
    print(f"Total de erros: {resumo['vms_erro']}")
    print(f"Pastas arquivos OK: {resumo.get('total_pastas_arquivos_ok', 0)}")
    print(f"Pastas arquivos atrasadas: {resumo.get('total_pastas_arquivos_atrasadas', 0)}")
    print(f"Pastas arquivos vazias: {resumo.get('total_pastas_arquivos_vazias', 0)}")
    print(f"Pastas arquivos nao existem: {resumo.get('total_pastas_arquivos_nao_existe', 0)}")
    print(f"Pastas arquivos com erro: {resumo.get('total_pastas_arquivos_erro', 0)}")
    print(f"Subpastas arquivos OK: {resumo.get('total_subpastas_arquivos_ok', 0)}")
    print(f"Subpastas arquivos atrasadas: {resumo.get('total_subpastas_arquivos_atrasadas', 0)}")
    print(f"Subpastas arquivos vazias: {resumo.get('total_subpastas_arquivos_vazias', 0)}")
    print(f"Subpastas arquivos com erro: {resumo.get('total_subpastas_arquivos_erro', 0)}")

    return 0


def executar_self_test() -> int:
    tzinfo = ZoneInfo("America/Sao_Paulo")
    nomes_validos = [
        "vzdump-qemu-100-2026_05_09-19_00_01.vma.zst",
        "vzdump-qemu-100-2026_05_02-19_00_11.vma.gz",
        "vzdump-lxc-101-2026_05_09-01_02_03.tar.zst",
        "vzdump-openvz-200-2026_05_09-01_02_03.tar.gz",
    ]
    nomes_invalidos = [
        "vzdump-qemu-100.log",
        "vzdump-qemu-100-2026_05_09.log",
        "arquivo-sem-data.vma.zst",
        "vzdump-qemu-100-2026_05_09-19_00_01.vma.zst.tmp",
    ]

    assert all(parse_vzdump_datetime(nome, tzinfo) is not None for nome in nomes_validos)
    assert all(parse_vzdump_datetime(nome, tzinfo) is None for nome in nomes_invalidos)

    datas_diarias = [
        datetime(2026, 5, 1, 19, 0, tzinfo=tzinfo),
        datetime(2026, 5, 2, 19, 3, tzinfo=tzinfo),
        datetime(2026, 5, 3, 18, 59, tzinfo=tzinfo),
    ]
    resultado = detectar_periodicidade(datas_diarias)
    assert resultado.periodicidade == "diario"
    assert resultado.confianca == "ALTA"

    datas_semanais = [
        datetime(2026, 5, 1, 19, 0, tzinfo=tzinfo),
        datetime(2026, 5, 8, 19, 0, tzinfo=tzinfo),
    ]
    resultado = detectar_periodicidade(datas_semanais)
    assert resultado.periodicidade == "semanal"
    assert resultado.confianca == "BAIXA"

    print("Self-test OK: parsing e periodicidade basica funcionando.")
    return 0


def main() -> int:
    if not validar_versao_python():
        return 10

    args = parse_args()

    if args.gerar_refresh_token:
        return gerar_refresh_token_interativo()

    if args.self_test:
        return executar_self_test()

    return executar_auditoria(args.config, args)


if __name__ == "__main__":
    sys.exit(main())
