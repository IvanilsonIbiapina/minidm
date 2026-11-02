"""Instalação/detecção da extensão do navegador (Chrome/Edge) — híbrido.

Dois mecanismos, escolhidos por máquina:

* **guiado** — copia o caminho da pasta p/ clipboard, abre no Explorer e
  imprime passo a passo ("Load unpacked"). É o caminho *garantido*: o
  Chrome já aceita a extensão assim (é como ela está carregada hoje).
* **policy** — escreve a política ``ExtensionInstallForcelist`` (HKCU, sem
  admin) apontando p/ um ``update.xml`` + CRX3 auto-hospedados na API
  local. Tenta instalar sozinho; se o Chrome recusar o ``update_url``
  http local, o usuário cai no modo guiado.

Por que híbrido (evidência da engenharia reversa + testes neste Chrome 156):

* ``chrome://`` não abre por linha de comando nem ShellExecute (o esquema
  ``chrome`` nem está registrado no Windows) — o fluxo guiado não consegue
  abrir a página sozinho, só orientar.
* O registro legado ``HKCU\\...\\Chrome\\Extensions\\<id>`` = ``path``->
  CRX local, que o **próprio IDM** usa, é ignorado pelo Chrome desde a
  v33 (prova: o IDM aponta p/ ``IDMGCExt.crx`` 6.42.60, mas a extensão
  instalada veio da Web Store em 6.43.1). Por isso NÃO implementamos essa
  via morta — só lemos, informativamente, no ``status``.
* CRX3 é o formato atual (``components/crx_file/crx3.proto``): magic
  ``Cr24`` + header protobuf (proof RSA-PSS + ``signed_header_data`` com o
  ``crx_id``) + zip. O id da extensão são os 16 primeiros bytes do
  SHA-256 da pubkey (SubjectPublicKeyInfo DER), cada hexdigito mapeado a
  ``a..p``.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import zipfile
from typing import Optional

# porta padrão da API local (bate com AppConfig.api_port)
DEFAULT_API_PORT = 9742

# nome dos artefatos hospedados pela API
CRX_NAME = "minidm.crx"
UPDATE_XML_NAME = "update.xml"

# ----------------------------------------------------------------- protobuf
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_bytes(field: int, data: bytes) -> bytes:
    """protobuf: campo `field` (wire type 2 = length-delimited) com `data`."""
    return _varint((field << 3) | 2) + _varint(len(data)) + data


# ------------------------------------------------------------------- CRX3
def crx_id_to_ext_id(crx_id: bytes) -> str:
    """16 bytes -> 32 chars em ``a..p`` (cada hexdigito vira ``a+d``)."""
    return "".join(chr(ord("a") + int(h, 16)) for h in crx_id.hex())


def extension_id_from_pubkey(pub_der: bytes) -> str:
    crx_id = hashlib.sha256(pub_der).digest()[:16]
    return crx_id_to_ext_id(crx_id)


def _zip_extension(ext_dir: str) -> bytes:
    """Empacota a pasta da extensão num zip determinístico (manifest na raiz)."""
    import io

    buf = io.BytesIO()
    # ZIP_DEFLATED: Chrome aceita; ordem ordenada p/ saída estável
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(ext_dir):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, ext_dir).replace(os.sep, "/")
                z.write(full, rel)
    return buf.getvalue()


def build_crx3(ext_dir: str, private_key_pem: bytes) -> tuple[bytes, str]:
    """Monta um CRX3 (assinado RSA-PSS/SHA-256) da pasta `ext_dir`.

    Devolve ``(crx_bytes, extension_id)``. A assinatura cobre exatamente
    ``b"CRX3 SignedData\\x00" + len(signed_header, 4 octets LE) +
    signed_header + zip`` (crx3.proto).
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    priv = serialization.load_pem_private_key(private_key_pem, password=None)
    pub_der = priv.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    crx_id = hashlib.sha256(pub_der).digest()[:16]
    ext_id = crx_id_to_ext_id(crx_id)

    archive = _zip_extension(ext_dir)
    signed_header = _pb_bytes(1, crx_id)          # SignedData{crx_id=16B}
    payload = (b"CRX3 SignedData\x00"
               + struct.pack("<I", len(signed_header))
               + signed_header + archive)
    # RSASSA-PSS, MGF1(SHA-256), salt = tamanho do digest (32), como no
    # Chromium (SignatureCreator RSA-PSS).
    signature = priv.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256())

    proof = _pb_bytes(1, pub_der) + _pb_bytes(2, signature)  # AsymmetricKeyProof
    header = (_pb_bytes(2, proof)                  # CrxFileHeader.sha256_with_rsa
              + _pb_bytes(10000, signed_header))   # .signed_header_data
    crx = b"Cr24" + struct.pack("<II", 3, len(header)) + header + archive
    return crx, ext_id


def read_crx_extension_id(crx: bytes) -> Optional[str]:
    """Lê o ``crx_id`` de um CRX3 de volta (para validação/testes)."""
    if crx[:4] != b"Cr24":
        return None
    ver, hdr_len = struct.unpack("<II", crx[4:12])
    if ver != 3:
        return None
    hdr = crx[12:12 + hdr_len]
    # procura o campo 10000 (signed_header_data) no header
    i = 0
    n = len(hdr)
    while i < n:
        key, i2 = _read_varint(hdr, i)
        field, wire = key >> 3, key & 7
        if wire != 2:
            return None
        ln, i = _read_varint(hdr, i2)
        val = hdr[i:i + ln]
        i += ln
        if field == 10000:
            # SignedData: campo 1 (bytes, 16B)
            j = 0
            k, j2 = _read_varint(val, j)
            if (k >> 3) != 1:
                return None
            ln2, j = _read_varint(val, j2)
            return val[j:j + ln2].hex()
    return None


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


# ------------------------------------------------------- chave estável (id)
def load_or_create_key(path: str) -> bytes:
    """Carrega a chave privada PEM (estável -> id da extensão estável).

    Se não existir, gera RSA-2048 e salva. O id da extensão depende da
    pubkey; sem chave fixa, cada instalação geraria um id novo.
    """
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(pem)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return pem


def extension_id_from_key_file(path: str) -> Optional[str]:
    """Id da extensão da chave já salva, ou None se não houver chave."""
    if not os.path.isfile(path):
        return None
    try:
        from cryptography.hazmat.primitives import serialization

        pem = load_or_create_key(path)
        priv = serialization.load_pem_private_key(pem, password=None)
        pub = priv.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        return extension_id_from_pubkey(pub)
    except Exception:
        return None


# ----------------------------------------------------------- update.xml
UPDATE_XML_TMPL = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<gupdate xmlns="http://www.google.com/update2/response" protocol="2.0">\n'
    '  <app appid="{app_id}">\n'
    '    <updatecheck codebase="{codebase}" version="{version}"/>\n'
    '  </app>\n'
    '</gupdate>\n'
)


def build_update_xml(ext_id: str, codebase_url: str, version: str) -> str:
    return UPDATE_XML_TMPL.format(app_id=ext_id, codebase=codebase_url,
                                  version=version)


def _ext_version(ext_dir: str) -> str:
    try:
        with open(os.path.join(ext_dir, "manifest.json"), "r",
                  encoding="utf-8") as f:
            return json.load(f).get("version", "0")
    except (OSError, ValueError):
        return "0"


# ------------------------------------------------------- caminhos/atalhos
def default_ext_dir() -> str:
    """Pasta da extensão embarcada (irmã do pacote ``minidm``)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "browser_ext")


def build_artifacts(data_dir: str, ext_dir: str, api_port: int) -> dict:
    """Gera o CRX3 + update.xml em ``<data_dir>/ext/`` (id estável).

    Devolve ``{ext_id, crx_path, update_xml_path, codebase, version}``.
    """
    ext_dir = os.path.abspath(ext_dir or default_ext_dir())
    out = os.path.join(data_dir, "ext")
    os.makedirs(out, exist_ok=True)
    pem = load_or_create_key(os.path.join(data_dir, "ext_key.pem"))
    crx, ext_id = build_crx3(ext_dir, pem)
    version = _ext_version(ext_dir)
    codebase = f"http://127.0.0.1:{api_port}/{CRX_NAME}"
    crx_path = os.path.join(out, CRX_NAME)
    xml_path = os.path.join(out, UPDATE_XML_NAME)
    with open(crx_path, "wb") as f:
        f.write(crx)
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(build_update_xml(ext_id, codebase, version))
    return {
        "ext_id": ext_id,
        "crx_path": crx_path,
        "update_xml_path": xml_path,
        "codebase": codebase,
        "update_url": f"http://127.0.0.1:{api_port}/{UPDATE_XML_NAME}",
        "version": version,
    }


# ----------------------------------------------------------- detect perfis
# Navegadores comuns no Windows: (rótulo, subpasta de user data sob LOCALAPPDATA)
BROWSERS = (
    ("chrome", os.path.join("Google", "Chrome", "User Data")),
    ("edge", os.path.join("Microsoft", "Edge", "User Data")),
    ("brave", os.path.join("BraveSoftware", "Brave-Browser", "User Data")),
)

# rótulos best-effort do campo `location` do Secure Preferences
_LOCATION = {0: "desconhecida", 1: "loja/webstore", 2: "desconhecida",
             3: "registro externo (legado)", 4: "unpacked (carregada)",
             5: "componente", 6: "componente externo", 7: "pref externa",
             8: "pref/baixada", 9: "política externa", 10: "política"}


def _user_data_dir(browser_sub: str) -> str:
    base = os.environ.get("LOCALAPPDATA", "")
    return os.path.join(base, browser_sub) if base else ""


def _profiles(user_data_dir: str) -> list[str]:
    """Perfis que têm Preferências (Default, Perfil 1, …)."""
    if not user_data_dir or not os.path.isdir(user_data_dir):
        return []
    out = []
    try:
        entries = sorted(os.listdir(user_data_dir))
    except OSError:
        return []
    for name in entries:
        pdir = os.path.join(user_data_dir, name)
        if not os.path.isdir(pdir):
            continue
        is_profile = (name == "Default" or name.startswith("Profile ")
                      or name.startswith("Person "))
        if is_profile and os.path.isfile(os.path.join(pdir, "Preferences")):
            out.append(name)
    return out


def _read_secure_prefs(profile_dir: str) -> dict:
    """Lê o ``Secure Preferences`` (ou ``Preferences``) de um perfil.

    O Secure Preferences tem HMAC; só leitura mesmo — nunca escrevemos nele
    (é assinado e o Chrome invalidaria). Aqui é só detecção.
    """
    for fname in ("Secure Preferences", "Preferences"):
        p = os.path.join(profile_dir, fname)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                continue
    return {}


def _find_ext_in_profile(prefs: dict, ext_id: str, ext_dir: str) -> Optional[dict]:
    """Procura a extensão do minidm nas settings de um perfil.

    Casa por id calculado OU por `path` apontando dentro da nossa pasta
    (o unpacked tem o id derivado do caminho, que muda se a pasta move).
    """
    settings = (prefs.get("extensions", {}) or {}).get("settings", {}) or {}
    ext_dir_abs = os.path.abspath(ext_dir) if ext_dir else ""
    for eid, s in settings.items():
        if not isinstance(s, dict):
            continue
        path = s.get("path", "") or ""
        if eid == ext_id or (ext_dir_abs and path
                             and os.path.abspath(path) == ext_dir_abs):
            loc = s.get("location")
            return {
                "ext_id": eid,
                "location": loc,
                "location_label": _LOCATION.get(loc, str(loc)),
                "state": s.get("state"),
                "version": s.get("version")
                or (s.get("manifest", {}) or {}).get("version"),
                "ext_name": (s.get("manifest", {}) or {}).get("name"),
                "path": path,
                "from_webstore": s.get("from_webstore", False),
            }
    return None


def status(data_dir: str, ext_dir: str = "", ext_id: str = "") -> dict:
    """Estado da extensão em todos os navegadores + política + API."""
    ext_dir = os.path.abspath(ext_dir or default_ext_dir())
    if not ext_id:
        ext_id = extension_id_from_key_file(
            os.path.join(data_dir, "ext_key.pem")) or ""

    browsers = []
    for label, sub in BROWSERS:
        udd = _user_data_dir(sub)
        profiles = []
        for pname in _profiles(udd):
            prefs = _read_secure_prefs(os.path.join(udd, pname))
            hit = _find_ext_in_profile(prefs, ext_id, ext_dir) if prefs else None
            profiles.append({
                "name": pname,
                "installed": bool(hit),
                **(hit or {}),
            })
        browsers.append({"name": label, "user_data_dir": udd,
                         "profiles": profiles,
                         "present": os.path.isdir(udd)})

    return {
        "ext_dir": ext_dir,
        "ext_id": ext_id,
        "key_present": os.path.isfile(os.path.join(data_dir, "ext_key.pem")),
        "crx_present": os.path.isfile(
            os.path.join(data_dir, "ext", CRX_NAME)),
        "browsers": browsers,
        "policy": {label: _read_forcelist(label) for label, _ in BROWSERS
                   if label in ("chrome", "edge")},
        "api_running": _api_running(DEFAULT_API_PORT),
    }


# ------------------------------------------------------------- política
def _policy_read(browser: str, hive="hkcu") -> dict:
    """Lê ``ExtensionInstallForcelist`` do navegador (HKCU). Testável."""
    if os.name != "nt":
        return {}
    import winreg

    root = winreg.HKEY_CURRENT_USER if hive == "hkcu" else winreg.HKEY_LOCAL_MACHINE
    vendor = "Google\\Chrome" if browser == "chrome" else "Microsoft\\Edge"
    path = f"Software\\Policies\\{vendor}\\ExtensionInstallForcelist"
    try:
        key = winreg.OpenKey(root, path)
    except OSError:
        return {}
    out = {}
    try:
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            out[name] = value
            i += 1
    finally:
        winreg.CloseKey(key)
    return out


def _read_forcelist(browser: str) -> dict:
    """Política atual do `forcelist` (HKCU) -> {valor_name: 'id;update_url'}."""
    return _policy_read(browser)


def write_forcelist(browser: str, ext_id: str, update_url: str) -> str:
    """Escreve a política `ExtensionInstallForcelist` (HKCU, sem admin).

    O navegador entra como "gerenciado" p/ esta extensão; ela instala
    silenciosamente mas não aparece pra desinstalar na UI (remover é via
    `minidm ext uninstall`). Devolve o caminho da chave escrita.
    """
    if os.name != "nt":
        raise RuntimeError("política de extensão só existe no Windows")
    import winreg

    vendor = "Google\\Chrome" if browser == "chrome" else "Microsoft\\Edge"
    path = f"Software\\Policies\\{vendor}\\ExtensionInstallForcelist"
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, path)
    try:
        winreg.SetValueEx(key, ext_id, 0, winreg.REG_SZ,
                          f"{ext_id};{update_url}")
    finally:
        winreg.CloseKey(key)
    return f"HKCU\\{path}"


def remove_forcelist(browser: str, ext_id: str) -> bool:
    """Remove a política desta extensão (devolve True se apagou algo)."""
    if os.name != "nt":
        return False
    import winreg

    vendor = "Google\\Chrome" if browser == "chrome" else "Microsoft\\Edge"
    path = f"Software\\Policies\\{vendor}\\ExtensionInstallForcelist"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                             winreg.KEY_SET_VALUE)
    except OSError:
        return False
    try:
        winreg.DeleteValue(key, ext_id)
        return True
    except OSError:
        return False
    finally:
        winreg.CloseKey(key)


def _api_running(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


# ------------------------------------------------- ações (guiado/policy)
def install_policy(data_dir: str, ext_dir: str = "", browsers=("chrome", "edge"),
                   api_port: int = DEFAULT_API_PORT) -> dict:
    """Via automática: gera CRX3 + update.xml e escreve a política.

    Requer a API local rodando p/ o Chrome baixar o CRX
    (``minidm daemon`` ou ``minidm serve``).
    """
    art = build_artifacts(data_dir, ext_dir, api_port)
    written = {}
    for b in browsers:
        if b not in ("chrome", "edge"):
            continue
        try:
            written[b] = write_forcelist(b, art["ext_id"], art["update_url"])
        except (RuntimeError, OSError) as e:
            written[b] = f"erro: {e}"
    art["policy_keys"] = written
    art["api_running"] = _api_running(api_port)
    return art


def uninstall_policy(data_dir: str, browsers=("chrome", "edge")) -> dict:
    ext_id = extension_id_from_key_file(
        os.path.join(data_dir, "ext_key.pem")) or ""
    removed = {}
    for b in browsers:
        if b in ("chrome", "edge"):
            removed[b] = bool(ext_id) and remove_forcelist(b, ext_id)
    return {"ext_id": ext_id, "removed": removed}


def copy_text(text: str) -> bool:
    """Copia `text` p/ a área de transferência (Windows)."""
    if os.name != "nt":
        return False
    import ctypes

    cf_unicode = 13
    user32 = ctypes.windll.user32
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        # GMEM_MOVEABLE
        h = ctypes.windll.kernel32.GlobalAlloc(0x0002,
                                               (len(text) + 1) * 2)
        if not h:
            return False
        ptr = ctypes.windll.kernel32.GlobalLock(h)
        ctypes.memmove(ptr, buf, (len(text) + 1) * 2)
        ctypes.windll.kernel32.GlobalUnlock(h)
        user32.SetClipboardData(cf_unicode, h)
        return True
    finally:
        user32.CloseClipboard()


def open_explorer(path: str) -> bool:
    """Abre o Explorador de Arquivos em `path` (Windows)."""
    if os.name != "nt":
        return False
    try:
        os.startfile(path)  # noqa: S606
        return True
    except OSError:
        return False


def guided(data_dir: str, ext_dir: str = "") -> dict:
    """Via guiada: copia o caminho, abre o Explorer, devolve as instruções.

    O Chrome ignora ``chrome://`` na linha de comando, então não dá p/
    abrir a página sozinho — orientamos o usuário no passo final.
    """
    ext_dir = os.path.abspath(ext_dir or default_ext_dir())
    copied = copy_text(ext_dir)
    opened = open_explorer(ext_dir)
    steps = [
        "No Chrome/Edge: abra a página de extensões "
        "(… > Complementos > Gerenciar complementos, ou Ctrl+Shift+O).",
        "Ative o 'Modo dos desenvolvedores' (interruptor no canto superior).",
        "Clique 'Carregar expandida' (Load unpacked) e aponte para a pasta "
        "que está no seu clipboard (Ctrl+V no diálogo).",
        "Pronto — a extensão 'minidm bridge' aparece habilitada e passa a "
        "enviar links p/ o minidm.",
    ]
    return {
        "ext_dir": ext_dir,
        "copied_to_clipboard": copied,
        "explorer_opened": opened,
        "steps": steps,
    }
