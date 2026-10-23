"""Verificação de integridade e origem dos arquivos baixados.

* SHA-256 local do arquivo concluído;
* checksums publicados pelo servidor (`<arquivo>.sha256` / `<arquivo>.md5`);
* assinatura Authenticode (Windows) de instaladores/bibliotecas — mesmo
  princípio de checagem de origem que ferramentas de gerenciamento usam.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess

import requests

TIMEOUT = 10
SIGNED_EXTS = (".exe", ".dll", ".sys", ".msi", ".ocx", ".cpl", ".scr", ".efi")

_HEX = re.compile(r"\b[0-9a-fA-F]{32}\b|\b[0-9a-fA-F]{64}\b")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksum_text(text: str) -> tuple[str | None, str | None]:
    """Aceita formatos comuns: '<hash> *arquivo' e 'SHA256 (arq) = <hash>'."""
    sha256 = md5 = None
    for token in _HEX.findall(text or ""):
        t = token.lower()
        if len(t) == 64 and sha256 is None:
            sha256 = t
        elif len(t) == 32 and md5 is None:
            md5 = t
    return sha256, md5


def fetch_remote_checksums(url: str, user_agent: str) -> tuple[str | None, str | None]:
    """Tenta baixar `<url>.sha256` e `<url>.md5` publicados ao lado do arquivo."""
    sha = md5 = None
    for suffix in (".sha256", ".md5", ".sha256sum", ".md5sum"):
        try:
            r = requests.get(url + suffix, timeout=TIMEOUT,
                             headers={"User-Agent": user_agent})
            if r.status_code != 200 or len(r.text) > 4096:
                continue
            s, m = parse_checksum_text(r.text)
            sha = sha or s
            md5 = md5 or m
        except requests.RequestException:
            continue
    return sha, md5


def authenticode_status(path: str) -> tuple[str, str]:
    """(status, assinante) via Get-AuthenticodeSignature. Statuses:
    Valid, NotSigned, HashMismatch, NotTrusted, UnknownError..."""
    if os.name != "nt":
        return ("unsupported", "")
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$s = Get-AuthenticodeSignature -FilePath '{path.replace(chr(39), chr(39)*2)}';"
        "$who = ''; if ($s.SignerCertificate) { $who = $s.SignerCertificate.Subject };"
        "Write-Output ($s.Status.ToString() + '|' + $who)"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30)
        line = (out.stdout or "").strip().splitlines()
        if line and "|" in line[-1]:
            status, who = line[-1].split("|", 1)
            return (status.strip(), who.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ("UnknownError", "")


def should_check_signature(path: str) -> bool:
    return path.lower().endswith(SIGNED_EXTS)


def verify_file(path: str, expected_sha256: str | None = None,
                remote_sha256: str | None = None,
                remote_md5: str | None = None,
                check_signature: bool = True) -> dict:
    """Consolida todas as checagens disponíveis para um arquivo."""
    actual = sha256_file(path)
    result = {
        "sha256": actual,
        "expected_sha256_ok": (actual == expected_sha256) if expected_sha256 else None,
        "remote_sha256_ok": (actual == remote_sha256) if remote_sha256 else None,
        "remote_md5_ok": None,
        "signature_status": None,
        "signature_signer": None,
    }
    if remote_md5:
        import hashlib as _h
        m = _h.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                m.update(chunk)
        result["remote_md5_ok"] = (m.hexdigest() == remote_md5)
    if check_signature and should_check_signature(path):
        status, who = authenticode_status(path)
        result["signature_status"] = status
        result["signature_signer"] = who
    return result
