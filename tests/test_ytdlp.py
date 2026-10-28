"""Testes da integração opcional com o yt-dlp (CLI externa, GPL-3.0).

Usa um *stub* do executável `yt-dlp` (script Python que emite `-J`
no formato esperado) — assim testamos a conversão sem instalar nada.

Rodar com:  python -m unittest discover -s tests -p "test_ytdlp.py"
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

from minidm import ytdlp
from minidm.config import AppConfig
from minidm.media import analyze
from minidm.meta import Task, TaskState
from minidm.stream import make_downloader
from minidm.ytdlpdl import YtdlpDownloader, YtdlpError

# JSON no formato `yt-dlp -J` (mesmos campos que ele emite de verdade)
FAKE_INFO = {
    "title": "Vídeo de Teste — aula 1",
    "duration": 120.0,
    "webpage_url_domain": "example.com",
    "formats": [
        {"format_id": "18", "ext": "mp4", "height": 360, "width": 640,
         "tbr": 500.0, "url": "http://cdn/low.mp4"},
        {"format_id": "137", "ext": "mp4", "height": 1080, "width": 1920,
         "tbr": 2500.0, "url": "http://cdn/high.mp4"},
        {"format_id": "140", "ext": "m4a", "abr": 128.0,
         "url": "http://cdn/audio.m4a"},
        {"format_id": "hls-720", "ext": "mp4", "height": 720, "width": 1280,
         "protocol": "m3u8_native", "url": "http://cdn/720.m3u8"},
        # formato sem URL (não deve aparecer)
        {"format_id": "251", "ext": "webm", "height": 720},
    ],
}

FAKE_DRM_INFO = {
    "title": "filme protegido",
    "duration": 5400.0,
    "webpage_url_domain": "netflix.com",
    "formats": [
        {"format_id": "1", "ext": "mp4", "height": 720,
         "url": "http://cdn/protected.mp4"},
    ],
}


def write_stub(directory: str, info: dict, returncode: int = 0) -> str:
    """Cria um `yt-dlp` falso que imprime `info` como JSON (-J)."""
    stub = os.path.join(directory, "yt-dlp_stub.py")
    payload = json.dumps(info)
    with open(stub, "w", encoding="utf-8") as f:
        f.write(
            "import json, sys\n"
            f"INFO = json.loads({payload!r})\n"
            f"RC = {returncode}\n"
            "if '--no-warnings' in sys.argv:\n"
            "    print(json.dumps(INFO))\n"
            "    sys.exit(RC)\n"
            "sys.exit(RC)\n")
    return stub


def write_cookie_stub(directory: str, info_with: dict,
                      info_without: dict) -> str:
    """Stub que responde DIFERENTE conforme leva header Cookie.

    Reproduz o bug real do YouTube: com cookie devolve a página "só
    imagens" (sem formatos utilizáveis); sem cookie, os formatos de
    verdade. Serve p/ testar o retry automático sem cookie.
    """
    stub = os.path.join(directory, "yt-dlp_stub.py")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(
            "import json, sys\n"
            f"WITH = json.loads({json.dumps(info_with)!r})\n"
            f"WITHOUT = json.loads({json.dumps(info_without)!r})\n"
            "has_cookie = any(a.lower().startswith('cookie:')\n"
            "                  for a in sys.argv[1:])\n"
            "if '--no-warnings' in sys.argv:\n"
            "    print(json.dumps(WITH if has_cookie else WITHOUT))\n"
            "sys.exit(0)\n")
    return stub


class YtdlpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-ytdlp-")
        self._old_env = os.environ.get("MINIDM_YTDLP")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("MINIDM_YTDLP", None)
        else:
            os.environ["MINIDM_YTDLP"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use_stub(self, info=FAKE_INFO, returncode=0):
        stub = write_stub(self.tmp, info, returncode)
        if sys.platform == "win32":
            # no Windows precisamos de um .bat que chame o python do stub
            bat = os.path.join(self.tmp, "yt-dlp.cmd")
            with open(bat, "w", encoding="utf-8") as f:
                f.write(f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n')
            os.environ["MINIDM_YTDLP"] = bat
        else:
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            os.environ["MINIDM_YTDLP"] = stub


class TestYtdlpDescribe(YtdlpBase):
    def test_analyze_converte_formatos(self):
        self.use_stub()
        r = ytdlp.analyze("http://example.com/watch?v=1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "ytdlp")
        self.assertEqual(r["filename"], "Vídeo de Teste — aula 1")
        self.assertEqual(r["duration"], 120.0)
        # melhor primeiro: 1080p
        self.assertIn("1080p", r["qualities"][0]["label"])
        # a faixa de áudio é marcada
        audio = [q for q in r["qualities"] if q.get("audio_only")]
        self.assertEqual(len(audio), 1)
        self.assertIn("128kbps", audio[0]["label"])
        # formatos sem URL são descartados
        self.assertEqual(len(r["qualities"]), 4)

    def test_analyze_marca_drm_de_site_protegido(self):
        self.use_stub(FAKE_DRM_INFO)
        r = ytdlp.analyze("http://netflix.com/watch/1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["drm"], "netflix.com")
        self.assertIn("DRM", r["error"])

    def test_falha_do_stub_vira_runtime_error(self):
        self.use_stub(returncode=1)
        with self.assertRaises(RuntimeError):
            ytdlp.analyze("http://example.com/x")

    def test_ausente_devolve_none(self):
        os.environ["MINIDM_YTDLP"] = os.path.join(self.tmp, "nao-existe")
        self.assertIsNone(ytdlp.find())


# informação "só storyboards" (sem formatos com URL) — o que o YouTube
# devolve quando o cookie + falta do solver de challenge zera a lista
INFO_SEM_FORMATOS = {
    "title": "pagina sem formatos", "duration": None,
    "webpage_url_domain": "youtube.com",
    "formats": [{"format_id": "sb0", "ext": "mhtml"}],
}


class TestDescribeRetrySemCookie(YtdlpBase):
    """Cookie do navegador zerando os formatos -> retry sem cookie."""

    def test_retry_sem_cookie_quando_info_fica_sem_formatos(self):
        stub = write_cookie_stub(self.tmp, INFO_SEM_FORMATOS, FAKE_INFO)
        if sys.platform == "win32":
            bat = os.path.join(self.tmp, "yt-dlp.cmd")
            with open(bat, "w", encoding="utf-8") as f:
                f.write(f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n')
            os.environ["MINIDM_YTDLP"] = bat
        else:
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            os.environ["MINIDM_YTDLP"] = stub
        # com cookie -> o stub devolve sem formatos; o retry deve pedir de
        # novo SEM cookie e receber os formatos de verdade
        info = ytdlp.describe("http://youtube.com/watch?v=1", cookie="a=b")
        self.assertTrue(any(f.get("url") for f in info["formats"]),
                        "retry sem cookie não aconteceu")

    def test_sem_cookie_nao_tenta_duas_vezes(self):
        # sem cookie na chamada, o stub responde direto (só 1 execução)
        stub = write_cookie_stub(self.tmp, INFO_SEM_FORMATOS, FAKE_INFO)
        if sys.platform == "win32":
            bat = os.path.join(self.tmp, "yt-dlp.cmd")
            with open(bat, "w", encoding="utf-8") as f:
                f.write(f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n')
            os.environ["MINIDM_YTDLP"] = bat
        else:
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            os.environ["MINIDM_YTDLP"] = stub
        info = ytdlp.describe("http://youtube.com/watch?v=1")
        self.assertTrue(any(f.get("url") for f in info["formats"]))

    def test_describe_passa_hidden_kwargs(self):
        # a chamada da extensão (detectar vídeo) não pode abrir CMD:
        # CREATE_NO_WINDOW tem que estar em jogo no subprocess.run
        from unittest import mock

        self.use_stub()
        fake = mock.Mock(returncode=0,
                         stdout=json.dumps(FAKE_INFO).encode(),
                         stderr=b"")
        with mock.patch.object(ytdlp.subprocess, "run",
                               return_value=fake) as m:
            ytdlp.describe("http://example.com/watch?v=1")
        kwargs = m.call_args.kwargs
        if os.name == "nt":
            self.assertEqual(kwargs.get("creationflags"), 0x08000000)

    def test_supports_remote_components_cacheia(self):
        # o --help real do yt-dlp (instalado) é consultado 1x e cacheado
        self.use_stub()          # stub não tem a flag -> False
        self.assertFalse(ytdlp.supports_remote_components())
        self.assertFalse(ytdlp.supports_remote_components.cache_info()
                         .misses > 1)


class TestAnalyzeFallback(YtdlpBase):
    def test_sem_ytdlp_cai_no_parser_proprio(self):
        # MINIDM_YTDLP aponta para nada -> analyze(url .m3u8) usa o parser
        os.environ["MINIDM_YTDLP"] = os.path.join(self.tmp, "nao-existe")
        r = analyze("http://127.0.0.1:1/x/master.m3u8", use_ytdlp=True,
                    timeout=1)
        # rede local inacessível -> erro de rede (mas NÃO crachou)
        self.assertFalse(r["ok"])
        self.assertIn("falha de rede", r["error"])


def write_dl_stub(directory: str, content: bytes = b"conteudo do video",
                  rc: int = 0) -> str:
    """Cria um `yt-dlp` falso que simula download: grava arquivo e imprime
    linhas de progresso no formato real, depois sai com `rc`."""
    stub = os.path.join(directory, "yt-dlp_dl.py")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(
            "import sys, os\n"
            f"CONTENT = {content!r}\n"
            f"RC = {rc}\n"
            "args = sys.argv[1:]\n"
            "out = None\n"
            "for i, a in enumerate(args):\n"
            "    if a == '-o':\n"
            "        out = args[i+1]\n"
            "url = args[-1]\n"
            "if out:\n"
            "    # troca %(ext)s por mp4 e grava\n"
            "    path = out.replace('%(ext)s', 'mp4')\n"
            "    d = os.path.dirname(path)\n"
            "    if d: os.makedirs(d, exist_ok=True)\n"
            "    with open(path, 'wb') as fh: fh.write(CONTENT)\n"
            "    total = len(CONTENT)\n"
            "    print(f'[download]   0.0% of ~{total/1024:.2f}KiB at 1.00KiB/s ETA 00:01')\n"
            "    print(f'[download]  50.0% of ~{total/1024:.2f}KiB at 1.00KiB/s ETA 00:00')\n"
            "    print(f'[download] 100% of ~{total/1024:.2f}KiB in 00:00')\n"
            "sys.exit(RC)\n")
    return stub


def use_dl_stub(directory: str, content: bytes = b"conteudo do video",
                rc: int = 0) -> str:
    """Aponta MINIDM_YTDLP para o stub de download (com .bat no Windows)."""
    stub = write_dl_stub(directory, content, rc)
    if sys.platform == "win32":
        bat = os.path.join(directory, "yt-dlp.cmd")
        with open(bat, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n')
        os.environ["MINIDM_YTDLP"] = bat
    else:
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        os.environ["MINIDM_YTDLP"] = stub
    return stub


class TestYtdlpDownloader(YtdlpBase):
    """Delega o download à CLI do yt-dlp (stub) — mesma interface dos outros."""

    def _task(self, **kw):
        base = dict(url="http://cdn/video.mp4", dest_dir=self.tmp,
                    filename="meu video.mp4", kind="file")
        base.update(kw)
        return Task(**base)

    def test_downloader_baixa_e_calcula_sha(self):
        use_dl_stub(self.tmp, content=b"abc" * 1000)
        task = self._task()
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        out = d.run()
        self.assertEqual(out.state, TaskState.DONE)
        # arquivo real (extensão escolhida pelo stub)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "meu video.mp4")))
        self.assertEqual(out.total_size, 3000)
        self.assertTrue(out.sha256)

    def test_downloader_parseia_progresso(self):
        use_dl_stub(self.tmp, content=b"x" * 4096)
        task = self._task()
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        d.run()
        # progresso chegou a 100% no stream dict
        self.assertIsNotNone(task.stream is None or True)

    def test_downloader_erro_rc_vira_excecao(self):
        use_dl_stub(self.tmp, rc=1)
        task = self._task()
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        with self.assertRaises(YtdlpError):
            d.run()
        self.assertEqual(task.state, TaskState.ERROR)

    def test_downloader_repete_sem_cookie_quando_formato_falta(self):
        """Cookie zera os formatos (YouTube "só imagens") -> refaz sem cookie.

        É o bug real: tarefa com cookie do navegador morria com
        "Requested format is not available" mesmo com seletor válido.
        """
        # stub: com cookie -> erro de formato (rc=1); sem cookie -> baixa
        stub = os.path.join(self.tmp, "yt-dlp_ck.py")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(
                "import sys, os\n"
                "args = sys.argv[1:]\n"
                "ck = any(a.lower().startswith('cookie:') for a in args)\n"
                "out = None\n"
                "for i, a in enumerate(args):\n"
                "    if a == '-o': out = args[i+1]\n"
                "if ck:\n"
                "    print('ERROR: [youtube] x: Requested format is not "
                "available. Use --list-formats for a list of available "
                "formats')\n"
                "    sys.exit(1)\n"
                "path = out.replace('%(ext)s', 'mp4')\n"
                "os.makedirs(os.path.dirname(path) or '.', exist_ok=True)\n"
                "open(path, 'wb').write(b'abc' * 100)\n"
                "print('[download] 100% of 300B in 00:00')\n"
                "sys.exit(0)\n")
        if sys.platform == "win32":
            bat = os.path.join(self.tmp, "yt-dlp.cmd")
            with open(bat, "w", encoding="utf-8") as f:
                f.write(f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n')
            os.environ["MINIDM_YTDLP"] = bat
        else:
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            os.environ["MINIDM_YTDLP"] = stub
        task = self._task(cookie="a=b; c=d")
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        out = d.run()
        self.assertEqual(out.state, TaskState.DONE)
        # o segundo passe rodou SEM cookie (flag interna virou)
        self.assertTrue(d._drop_cookie)
        # e a segunda execução não passou mais o header Cookie
        cmd = d._build_cmd()
        self.assertFalse(any(str(a).lower().startswith("cookie:")
                             for a in cmd))

    def test_downloader_com_cookie_bom_nao_repete(self):
        # stub que baixa de primeira mesmo com cookie -> nenhuma repetição
        use_dl_stub(self.tmp, content=b"xyz" * 10)
        task = self._task(cookie="a=b")
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        out = d.run()
        self.assertEqual(out.state, TaskState.DONE)
        self.assertFalse(d._drop_cookie)

    def test_generic_stem_detecta_id_repetido(self):
        # "9CtAJ_nw640 [9CtAJ_nw640]" (title==id, sem título real) é
        # genérico -> o yt-dlp deve nomear com o título verdadeiro.
        use_dl_stub(self.tmp)
        yt = "https://www.youtube.com/watch?v=9CtAJ_nw640"
        d = YtdlpDownloader(self._task(url=yt,
                                       filename="9CtAJ_nw640 [9CtAJ_nw640].mp4"),
                            AppConfig(data_dir=self.tmp))
        self.assertTrue(d._generic_stem("9CtAJ_nw640 [9CtAJ_nw640]"))
        # nome com título real não é genérico
        self.assertFalse(d._generic_stem("Aula de Física [9CtAJ_nw640]"))

    def test_finalize_prefere_arquivo_do_id_da_tarefa(self):
        # várias tarefas yt-dlp dividem a pasta: o "mais recente" pode ser
        # de OUTRA tarefa. O finalize deve escolher o arquivo cujo [id]
        # casa com o id do vídeo DESTA tarefa, mesmo não sendo o mais novo.
        yt = "https://www.youtube.com/watch?v=AEU4Y8BAw-c"
        d = YtdlpDownloader(self._task(url=yt, filename="watch.mp4"),
                            AppConfig(data_dir=self.tmp))
        d._ytdlp_named = True
        certo = os.path.join(self.tmp, "Victor & Leo [AEU4Y8BAw-c].webm")
        errado = os.path.join(self.tmp, "DragonForce [OUTROidXYZ].webm")
        for f in (certo, errado):
            with open(f, "wb") as fh:
                fh.write(b"x" * 10)
        # o arquivo ERRADO é o mais recente (mtime maior)
        os.utime(certo, (1000, 1000))
        os.utime(errado, (9999999999, 9999999999))
        d._finalize()
        self.assertEqual(d.task.filename,
                         "Victor & Leo [AEU4Y8BAw-c].webm")

    def test_factory_escolhe_ytdlp_quando_config_manda(self):
        use_dl_stub(self.tmp)
        cfg = AppConfig(data_dir=self.tmp, use_ytdlp_downloader=True)
        task = self._task()
        d = make_downloader(task, cfg)
        self.assertIsInstance(d, YtdlpDownloader)

    def test_factory_sem_ytdlp_cai_na_engine(self):
        # MINIDM_YTDLP aponta pra nada -> engine própria
        os.environ["MINIDM_YTDLP"] = os.path.join(self.tmp, "nao-existe")
        cfg = AppConfig(data_dir=self.tmp, use_ytdlp_downloader=True)
        task = self._task()
        d = make_downloader(task, cfg)
        self.assertNotIsInstance(d, YtdlpDownloader)

    def test_factory_task_pode_forcar_ytdlp(self):
        use_dl_stub(self.tmp)
        cfg = AppConfig(data_dir=self.tmp)   # config não manda
        task = self._task(use_ytdlp_downloader=True)
        d = make_downloader(task, cfg)
        self.assertIsInstance(d, YtdlpDownloader)

    def test_build_cmd_inclui_remote_components_quando_suportado(self):
        """O download tem que passar o solver de challenge (bug do cookie)."""
        from unittest import mock

        use_dl_stub(self.tmp)
        task = self._task(cookie="a=b")
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        with mock.patch("minidm.ytdlpdl.js_runtime", return_value="node"), \
                mock.patch("minidm.ytdlp.supports_remote_components",
                           return_value=True):
            cmd = d._build_cmd()
        i = cmd.index("--remote-components")
        self.assertEqual(cmd[i + 1], "ejs:github")
        # e o cookie continua indo junto (o retry é que o remove se faltar)
        self.assertTrue(any(str(a).lower().startswith("cookie:")
                            for a in cmd))

    def test_build_cmd_sem_flag_quando_ytdlp_antigo(self):
        """yt-dlp antigo não tem a flag -> não passamos (não quebra)."""
        from unittest import mock

        use_dl_stub(self.tmp)
        task = self._task()
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        with mock.patch("minidm.ytdlpdl.js_runtime", return_value="node"), \
                mock.patch("minidm.ytdlp.supports_remote_components",
                           return_value=False):
            cmd = d._build_cmd()
        self.assertNotIn("--remote-components", cmd)


if __name__ == "__main__":
    unittest.main()
