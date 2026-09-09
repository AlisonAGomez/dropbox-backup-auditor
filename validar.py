"""Validação local não destrutiva do Auditor Dropbox v2.5.

Não acessa a API do Dropbox. Os testes usam doubles/mocks locais.
"""
from __future__ import annotations

import argparse
import compileall
import importlib.metadata
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml
from packaging.requirements import Requirement

from auditor_bkp.config_validation import validate_config
from auditor_bkp.version import BUILD, VERSION
from auditor_bkp.release_guard import validar_politicas_criticas

ROOT = Path(__file__).resolve().parent
PDF_TEMPLATE_SHA256 = "f54ef7952f1218965945ddde8b4c5495a30b647c8ce0287dbc9d220ee1f2124e"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validação local do Auditor Dropbox v2.5")
    parser.add_argument(
        "--offline", action="store_true",
        help="Valida código/configuração/testes sem exigir o SDK Dropbox instalado. Não substitui a validação pós-instalação.",
    )
    return parser.parse_args()


def ok(texto: str) -> None:
    print(f"[OK] {texto}")


def fail(texto: str) -> None:
    print(f"[FALHA] {texto}")
    raise RuntimeError(texto)


def run(command: list[str], descricao: str) -> None:
    proc = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        fail(f"{descricao} retornou código {proc.returncode}")
    ok(descricao)


def validar_construcoes_inseguras() -> None:
    padroes = {
        "shell=True": re.compile(r"shell\s*=\s*True"),
        "os.system": re.compile(r"os\.system\s*\("),
        "eval": re.compile(r"\beval\s*\("),
        "exec": re.compile(r"\bexec\s*\("),
        "pickle": re.compile(r"pickle\."),
        "yaml.load": re.compile(r"yaml\.load\s*\("),
        "verify=False": re.compile(r"verify\s*=\s*False"),
    }
    arquivos = list((ROOT / "auditor_bkp").rglob("*.py")) + [
        ROOT / "auditoria.py", ROOT / "integracao.py", ROOT / "validar_dropbox.py"
    ]
    achados: list[str] = []
    for arquivo in arquivos:
        texto = arquivo.read_text(encoding="utf-8", errors="replace")
        for nome, padrao in padroes.items():
            for match in padrao.finditer(texto):
                linha = texto[:match.start()].count("\n") + 1
                achados.append(f"{arquivo.relative_to(ROOT)}:{linha}: {nome}")
    if achados:
        fail("construções inseguras encontradas: " + "; ".join(achados[:8]))
    ok("varredura estática de construções inseguras")


def validar_requisitos_declarados(offline: bool) -> None:
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        req = Requirement(line)
        if offline and req.name.lower() == "dropbox":
            continue
        try:
            versao = importlib.metadata.version(req.name)
        except importlib.metadata.PackageNotFoundError:
            fail(f"dependência não instalada: {req.name}")
        if req.specifier and versao not in req.specifier:
            fail(f"dependência {req.name} {versao} fora do intervalo {req.specifier}")
        ok(f"requisito {req.name} {versao} atende {req.specifier or 'qualquer versão'}")


def pip_check_informativo() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "check"], cwd=ROOT, check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode == 0:
        ok("pip check do ambiente")
    else:
        resumo = (proc.stdout or proc.stderr or "inconsistência não detalhada").strip().splitlines()
        print("[AVISO] pip check encontrou conflito no ambiente Python atual; isso pode ser de software não relacionado ao auditor.")
        for linha in resumo[:3]:
            print(f"        {linha}")


def main() -> int:
    args = parse_args()
    print(f"Auditor Dropbox v{VERSION} - revisão {BUILD} - validação local" + (" (OFFLINE)" if args.offline else ""))
    print("=" * 60)

    if sys.version_info < (3, 11):
        fail(f"Python 3.11+ obrigatório; atual={sys.version.split()[0]}")
    ok(f"Python {sys.version.split()[0]}")

    if VERSION != "2.5":
        fail(f"versão interna inesperada: {VERSION}")
    versao_txt = (ROOT / "VERSAO.txt").read_text(encoding="utf-8-sig")
    if "v2.5" not in versao_txt:
        fail("VERSAO.txt não está alinhado com v2.5")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if 'version = "2.5"' not in pyproject:
        fail("pyproject.toml não está alinhado com v2.5")
    ok("versão consistente em código e metadados")

    if (ROOT / "app").exists():
        fail("pasta legada app/ ainda existe")
    if not (ROOT / "auditor_bkp").is_dir():
        fail("pacote auditor_bkp ausente")
    ok("estrutura do pacote")

    pdf_template = ROOT / "auditor_bkp" / "pdf_reports.py"
    pdf_hash = hashlib.sha256(pdf_template.read_bytes()).hexdigest()
    if pdf_hash != PDF_TEMPLATE_SHA256:
        fail("template de PDF foi alterado em relação ao modelo aprovado")
    ok("template de PDF preservado")

    for nome_bat in ("instalar.bat", "executar.bat", "validar.bat", "validar_dropbox.bat"):
        texto_bat = (ROOT / nome_bat).read_text(encoding="utf-8", errors="replace").lower()
        if ".venv" not in texto_bat:
            fail(f"{nome_bat} não usa o ambiente Python isolado .venv")
    ok("launchers Windows usam ambiente Python isolado")

    config_path = ROOT / "config.yaml"
    if not config_path.exists():
        config_path = ROOT / "config.example.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    validate_config(config, config_path)
    validar_politicas_criticas(config)
    ok(f"{config_path.name} válido e sem segredos")

    cache_dir = ROOT / "cache"
    caches = sorted(cache_dir.rglob("*.json")) if cache_dir.exists() else []
    for cache in caches:
        try:
            json.loads(cache.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            fail(f"cache inválido: {cache.relative_to(ROOT)}: {exc}")
    if caches:
        ok(f"{len(caches)} arquivo(s) de cache JSON legível(is)")
    else:
        ok("repositório público sem cache operacional; cache será criado em execução")

    if not compileall.compile_dir(ROOT / "auditor_bkp", quiet=1, force=True):
        fail("falha de compilação no pacote auditor_bkp")
    if not compileall.compile_file(str(ROOT / "auditoria.py"), quiet=1, force=True):
        fail("falha de compilação em auditoria.py")
    if not compileall.compile_file(str(ROOT / "integracao.py"), quiet=1, force=True):
        fail("falha de compilação em integracao.py")
    if not compileall.compile_file(str(ROOT / "validar_dropbox.py"), quiet=1, force=True):
        fail("falha de compilação em validar_dropbox.py")
    ok("compilação sintática")
    validar_construcoes_inseguras()

    validar_requisitos_declarados(args.offline)
    pip_check_informativo()
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], "testes automatizados")
    if args.offline:
        ok("self-test com SDK real adiado para validação pós-instalação")
    else:
        run([sys.executable, "-m", "auditor_bkp.auditor", "--self-test"], "self-test de parsing e periodicidade")
    run([sys.executable, "auditoria.py", "versao"], "smoke test do lançador")
    run([sys.executable, "integracao.py", "--help"], "smoke test da interface de integração")

    print("=" * 60)
    print("VALIDAÇÃO LOCAL APROVADA. Nenhuma chamada real ao Dropbox foi executada." + (" SDK Dropbox não foi exigido neste modo." if args.offline else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"VALIDAÇÃO REPROVADA: {exc}", file=sys.stderr)
        raise SystemExit(3)
