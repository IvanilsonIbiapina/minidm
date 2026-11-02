"""Testes da extensão do navegador (minidm/extsetup.py) — híbrido.

Cobre o construtor CRX3 (formato, id e assinatura), a detecção por perfil
(Secure Preferences) e a política `ExtensionInstallForcelist` — tudo com
perfis falsos em diretório temporário e `winreg` simulado (nunca toca o
registro real nem abre o Explorador).

Rodar só estes testes:

    python -m unittest discover -s tests -p "test_extsetup.py" -v
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import tempfile
import unittest
import zipfile
from unittest import mock

from cryptography.hazmat.primitives import serialization

from minidm import extsetup as E


def _mk_ext_dir(files: dict[str, bytes] | None = None) -> str:
    """Cria uma pasta de extensão mínima (manifest válido)."""
    d = tempfile.mkdtemp()
    mf = {"manifest_version": 3, "name": "minidm bridge", "version": "2.0"}
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(mf, f)
    for name, data in (files or {}).items():
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
    return d


def _mk_profile(user_data: str, profile: str = "Default",
                settings: dict | None = None) -> str:
    """Cria `<user_data>/<profile>/Secure Preferences` falso."""
    pdir = os.path.join(user_data, profile)
    os.makedirs(pdir, exist_ok=True)
    sp = {"extensions": {"settings": settings or {}}}
    with open(os.path.join(pdir, "Secure Preferences"), "w",
              encoding="utf-8") as f:
        json.dump(sp, f)
    # `Preferences` vazio p/ `_profiles` marcar como perfil válido
    with open(os.path.join(pdir, "Preferences"), "w", encoding="utf-8") as f:
        json.dump({}, f)
    return pdir


# =================================================================== CRX3
class TestCrx3(unittest.TestCase):
    def setUp(self):
        self.ext_dir = _mk_ext_dir({"background.js": b"// sw\n"})
        self.pem = E.load_or_create_key(
            os.path.join(tempfile.mkdtemp(), "k.pem"))

    def test_formato_crx3(self):
        crx, ext_id = E.build_crx3(self.ext_dir, self.pem)
        self.assertEqual(crx[:4], b"Cr24")
        ver, hdr_len = struct.unpack("<II", crx[4:12])
        self.assertEqual(ver, 3)
        # header + zip = resto do arquivo
        self.assertLess(12 + hdr_len, len(crx))

    def test_id_dedicado_da_pubkey(self):
        from cryptography.hazmat.primitives import serialization

        crx, ext_id = E.build_crx3(self.ext_dir, self.pem)
        # id lido de volta do CRX bate com o devolvido
        hex_id = E.read_crx_extension_id(crx)
        recon = "".join(chr(ord("a") + int(h, 16)) for h in hex_id)
        self.assertEqual(recon, ext_id)
        # id tem 32 chars, todos em a..p
        self.assertEqual(len(ext_id), 32)
        self.assertTrue(all("a" <= c <= "p" for c in ext_id))

    def test_assinatura_rsa_pss_valida(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        crx, _ = E.build_crx3(self.ext_dir, self.pem)
        hdr_len = struct.unpack("<I", crx[8:12])[0]
        hdr, archive = crx[12:12 + hdr_len], crx[12 + hdr_len:]
        # acha proof (campo 2) e signed_header (campo 10000)
        proof = sh = None
        i = 0
        while i < len(hdr):
            key, i2 = E._read_varint(hdr, i)
            field = key >> 3
            ln, i = E._read_varint(hdr, i2)
            val = hdr[i:i + ln]
            i += ln
            if field == 2:
                proof = val
            elif field == 10000:
                sh = val
        pub = sig = None
        j = 0
        while j < len(proof):
            k, j2 = E._read_varint(proof, j)
            f2 = k >> 3
            ln2, j = E._read_varint(proof, j2)
            v2 = proof[j:j + ln2]
            j += ln2
            if f2 == 1:
                pub = v2
            elif f2 == 2:
                sig = v2
        payload = (b"CRX3 SignedData\x00" + struct.pack("<I", len(sh))
                   + sh + archive)
        pubk = serialization.load_der_public_key(pub)
        pubk.verify(sig, payload,
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                salt_length=32), hashes.SHA256())  # não levanta
        # e o id casa com a pubkey
        self.assertEqual(E.extension_id_from_pubkey(pub),
                         E.crx_id_to_ext_id(
                             hashlib.sha256(pub).digest()[:16]))

    def test_zip_contem_manifest(self):
        crx, _ = E.build_crx3(self.ext_dir, self.pem)
        hdr_len = struct.unpack("<I", crx[8:12])[0]
        z = zipfile.ZipFile(io.BytesIO(crx[12 + hdr_len:]))
        mf = json.loads(z.read("manifest.json"))
        self.assertEqual(mf["name"], "minidm bridge")
        self.assertIn("background.js", z.namelist())

    def test_chave_estavel_mesmo_id(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "k.pem")
        pem1 = E.load_or_create_key(p)
        pem2 = E.load_or_create_key(p)   # recarrega, não regenera
        self.assertEqual(pem1, pem2)
        pub = (serialization.load_pem_private_key(pem1, None)
               .public_key().public_bytes(
                   serialization.Encoding.DER,
                   serialization.PublicFormat.SubjectPublicKeyInfo))
        self.assertEqual(E.extension_id_from_key_file(p),
                         E.extension_id_from_pubkey(pub))


# ============================================================ update.xml
class TestUpdateXml(unittest.TestCase):
    def test_update_xml(self):
        xml = E.build_update_xml("aabbcc", "http://127.0.0.1:9742/x.crx", "2.0")
        self.assertIn('appid="aabbcc"', xml)
        self.assertIn('codebase="http://127.0.0.1:9742/x.crx"', xml)
        self.assertIn('version="2.0"', xml)
        self.assertIn("gupdate", xml)

    def test_build_artifacts(self):
        ext_dir = _mk_ext_dir()
        data = tempfile.mkdtemp()
        art = E.build_artifacts(data, ext_dir, 9742)
        self.assertTrue(os.path.isfile(art["crx_path"]))
        self.assertTrue(os.path.isfile(art["update_xml_path"]))
        self.assertEqual(art["update_url"],
                         "http://127.0.0.1:9742/update.xml")
        self.assertEqual(art["version"], "2.0")
        # id estável: segunda chamada usa a mesma chave
        art2 = E.build_artifacts(data, ext_dir, 9742)
        self.assertEqual(art["ext_id"], art2["ext_id"])


# ============================================================== detecção
class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.chrome = os.path.join(self.tmp, "Google", "Chrome", "User Data")
        self.edge = os.path.join(self.tmp, "Microsoft", "Edge", "User Data")
        self.ext_dir = _mk_ext_dir()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_detecta_unpacked_por_path(self):
        # extensão unpacked: id NÃO é o nosso (derivado do path); acha pelo path
        _mk_profile(self.chrome, "Default",
                    {"someunpackedid1234567890abcdef12": {
                        "location": 4, "path": self.ext_dir,
                        "manifest": {"name": "minidm bridge", "version": "2.0"}}})
        st = E.status(tempfile.mkdtemp(), ext_dir=self.ext_dir)
        ch = next(b for b in st["browsers"] if b["name"] == "chrome")
        prof = ch["profiles"][0]
        self.assertTrue(prof["installed"])
        self.assertEqual(prof["location"], 4)
        self.assertEqual(prof["path"], self.ext_dir)
        self.assertEqual(prof["ext_name"], "minidm bridge")

    def test_detecta_por_id_calculado(self):
        data = tempfile.mkdtemp()
        art = E.build_artifacts(data, self.ext_dir, 9742)
        _mk_profile(self.edge, "Default", {
            art["ext_id"]: {"location": 1, "version": "2.0",
                            "state": 1,
                            "manifest": {"name": "minidm bridge"}}})
        st = E.status(data, ext_dir=self.ext_dir)
        ed = next(b for b in st["browsers"] if b["name"] == "edge")
        self.assertTrue(ed["profiles"][0]["installed"])
        self.assertEqual(st["ext_id"], art["ext_id"])

    def test_ausente_quando_sem_entrada(self):
        _mk_profile(self.chrome, "Default", {})   # perfil vazio
        st = E.status(tempfile.mkdtemp(), ext_dir=self.ext_dir)
        ch = next(b for b in st["browsers"] if b["name"] == "chrome")
        self.assertFalse(ch["profiles"][0]["installed"])

    def test_perfis_enumerados(self):
        _mk_profile(self.chrome, "Default")
        _mk_profile(self.chrome, "Profile 1")
        self.assertEqual(sorted(E._profiles(self.chrome)),
                         ["Default", "Profile 1"])


# ============================================================== política
class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.ext_id = "aabbccddeeffgghhiijjkkllmmnnpp"

    def test_write_remove_chama_winreg(self):
        fake_key = mock.MagicMock()
        with mock.patch("winreg.CreateKey", return_value=fake_key) as ck, \
             mock.patch("winreg.SetValueEx") as sv, \
             mock.patch("winreg.CloseKey") as ck2, \
             mock.patch("winreg.OpenKey", return_value=fake_key), \
             mock.patch("winreg.DeleteValue") as dv:
            path = E.write_forcelist("chrome", self.ext_id,
                                     "http://127.0.0.1:9742/update.xml")
            self.assertIn("ExtensionInstallForcelist", path)
            sv.assert_called_once()
            # value name = ext_id, dado = "id;update_url"
            args = sv.call_args[0]
            self.assertEqual(args[1], self.ext_id)
            self.assertEqual(args[4], f"{self.ext_id};"
                                      f"http://127.0.0.1:9742/update.xml")
            ok = E.remove_forcelist("chrome", self.ext_id)
            self.assertTrue(ok)
            dv.assert_called_once_with(fake_key, self.ext_id)

    def test_edge_usa_vendor_correto(self):
        with mock.patch("winreg.CreateKey", return_value=mock.MagicMock()) as ck, \
             mock.patch("winreg.SetValueEx"), \
             mock.patch("winreg.CloseKey"):
            path = E.write_forcelist("edge", self.ext_id, "u")
            self.assertIn("Microsoft\\Edge", path)

    def test_remove_ausente_devolve_false(self):
        import winreg as _wr  # noqa: F401  (só p/ garantir que existe)
        with mock.patch("winreg.OpenKey", side_effect=OSError):
            self.assertFalse(E.remove_forcelist("chrome", self.ext_id))


# ================================================================ guiado
class TestGuided(unittest.TestCase):
    def test_guided_passos_e_dir(self):
        ext_dir = _mk_ext_dir()
        with mock.patch.object(E, "copy_text", return_value=True), \
             mock.patch.object(E, "open_explorer", return_value=True):
            rep = E.guided(tempfile.mkdtemp(), ext_dir=ext_dir)
        self.assertEqual(rep["ext_dir"], os.path.abspath(ext_dir))
        self.assertTrue(rep["copied_to_clipboard"])
        self.assertTrue(rep["explorer_opened"])
        self.assertTrue(any("Carregar expandida" in s for s in rep["steps"]))
        self.assertTrue(any("Ctrl+V" in s for s in rep["steps"]))


if __name__ == "__main__":
    unittest.main()
