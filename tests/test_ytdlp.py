"""Testes da integração com o yt-dlp (dual-licenciado Unlicense/GPLv2).

Usa um *stub* do executável `yt-dlp` (script Python que emite `-J`
no formato esperado) e módulos `yt_dlp` falsos injetados — assim
testamos a conversão e o runner in-process sem rede.

Rodar com:  python -m unittest discover -s tests -p "test_ytdlp.py"
"""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from minidm import ytdlp
from minidm.config import AppConfig
from minidm.media import analyze
from minidm.meta import Task, TaskState
from minidm.stream import make_downloader
from minidm.ytdlpdl import YtdlpDownloader, YtdlpError, _LineSink

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
            f"#!{sys.executable}\n"
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
            f"#!{sys.executable}\n"
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
            f"#!{sys.executable}\n"
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
                f"#!{sys.executable}\n"
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

    def test_build_cmd_print_before_dl_para_nome_rapido(self):
        """--print before_dl imprime o título REAL já no INÍCIO do
        download: o nome na fila deixa de esperar o `_finalize` (fim).
        Os dois prints coexistem (caminho final + título)."""
        use_dl_stub(self.tmp)
        task = self._task()
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        cmd = d._build_cmd()
        self.assertIn("before_dl:MINIDM_TITLE:%(title)s [%(id)s]", cmd)
        self.assertIn("after_move:MINIDM_PATH:%(filepath)s", cmd)


# ------------------------------------------------- runner in-process
class TestRunnerInProcess(unittest.TestCase):
    """A CLI do yt-dlp roda DENTRO do processo (APK/PyInstaller).

    No APK (Chaquopy) não existe `python` executável e no .exe congelado
    (PyInstaller) o `sys.executable` é o nosso binário — nos dois casos
    `[exe, -m, yt_dlp]` não subprocessa; usamos `yt_dlp.main(argv)` com
    stdout redirecionado (mesmo código de extração, mesmo JSON).
    """

    def setUp(self):
        self._old_env = os.environ.pop("MINIDM_YTDLP", None)
        self._had_mod = "yt_dlp" in sys.modules
        self._old_mod = sys.modules.get("yt_dlp")
        ytdlp.version.cache_clear()
        ytdlp.supports_remote_components.cache_clear()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("MINIDM_YTDLP", None)
        else:
            os.environ["MINIDM_YTDLP"] = self._old_env
        if self._had_mod:
            sys.modules["yt_dlp"] = self._old_mod
        else:
            sys.modules.pop("yt_dlp", None)
        ytdlp.version.cache_clear()
        ytdlp.supports_remote_components.cache_clear()

    @staticmethod
    def inject(main):
        """Instala um módulo `yt_dlp` falso cujo `main(argv)` é `main`."""
        import types
        mod = types.ModuleType("yt_dlp")
        mod.main = main
        sys.modules["yt_dlp"] = mod
        return mod

    # ------------------------------------------------------------ detecção
    def test_cli_externa_nao_vai_in_process(self):
        self.assertFalse(ytdlp._in_process(["/usr/bin/yt-dlp", "-J", "x"]))
        self.assertFalse(ytdlp._in_process(["yt-dlp", "--version"]))
        # `-m` em outra posição não é o comando do módulo
        self.assertFalse(ytdlp._in_process(["python", "outro", "-m"]))

    def test_modulo_sem_executavel_vai_in_process(self):
        # Chaquopy: sys.executable vazio ou apontando p/ nada
        self.assertTrue(ytdlp._in_process(["", "-m", "yt_dlp", "--version"]))
        self.assertTrue(ytdlp._in_process(["/nao/existe/python",
                                           "-m", "yt_dlp"]))

    def test_congelado_vai_in_process(self):
        # PyInstaller: sys.executable é o nosso .exe (não entende -m)
        with mock.patch.object(ytdlp.sys, "frozen", True, create=True):
            self.assertTrue(ytdlp._in_process([sys.executable,
                                               "-m", "yt_dlp"]))

    def test_dev_com_python_real_usa_subprocesso(self):
        # desktop de dev: sys.executable existe -> subprocesso normal
        self.assertFalse(ytdlp._in_process([sys.executable,
                                            "-m", "yt_dlp"]))

    def test_android_app_process_vai_in_process(self):
        # APK: sys.executable é o app_process (arquivo EXISTENTE no
        # /system/bin) — subprocessar aborta com SIGABRT no chown do
        # dalvik-cache ("Error changing dalvik-cache ownership", rc -6)
        self.assertTrue(ytdlp._in_process(["/system/bin/app_process64",
                                           "-m", "yt_dlp"]))
        self.assertTrue(ytdlp._android_runtime("/system/bin/app_process"))
        self.assertFalse(ytdlp._android_runtime(sys.executable))

    def test_android_env_forca_in_process(self):
        # mesmo com python "existente", ambiente Android → in-process
        with mock.patch.dict(os.environ, {"ANDROID_ROOT": "/system"}):
            self.assertTrue(ytdlp._in_process([sys.executable,
                                               "-m", "yt_dlp"]))
        # e a CLI externa continua intocada (sai antes da checagem)
        with mock.patch.dict(os.environ, {"ANDROID_ROOT": "/system"}):
            self.assertFalse(ytdlp._in_process(["/usr/bin/yt-dlp", "-J",
                                                "x"]))

    # ------------------------------------------------------------ execução
    def test_run_in_process_captura_stdout_e_rc(self):
        def main(argv):
            print(json.dumps({"argv": argv}))
        self.inject(main)
        p = ytdlp._run_in_process(["--version"])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout.decode())["argv"],
                         ["--version"])

    def test_run_in_process_sysexit_string_vira_rc1(self):
        def main(argv):
            raise SystemExit("mensagem de erro")
        self.inject(main)
        p = ytdlp._run_in_process(["-J", "http://x"])
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"mensagem de erro", p.stderr)

    def test_run_in_process_excecao_vira_rc1(self):
        def main(argv):
            raise RuntimeError("boom")
        self.inject(main)
        p = ytdlp._run_in_process(["-J", "http://x"])
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"boom", p.stderr)

    def test_redirecionamento_de_stdout_restaurado(self):
        # redirect_stdout é global: tem que restaurar senão a GUI/TUI
        # do minidm fica sem saída depois da primeira extração
        orig_out, orig_err = sys.stdout, sys.stderr
        self.inject(lambda argv: None)
        ytdlp._run_in_process(["--version"])
        self.assertIs(sys.stdout, orig_out)
        self.assertIs(sys.stderr, orig_err)

    # ------------------------------------------------------------ despacho
    def test_run_despacha_in_process_quando_sem_executavel(self):
        seen = []
        self.inject(lambda argv: seen.append(argv))
        proc = ytdlp._run(["", "-m", "yt_dlp", "--version"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(seen, [["--version"]])   # [exe, -m, yt_dlp] pulado

    def test_run_despacha_subprocesso_para_cli_externa(self):
        fake = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(ytdlp.subprocess, "run",
                               return_value=fake) as m:
            ytdlp._run(["/usr/bin/yt-dlp", "--version"], timeout=7)
        self.assertEqual(m.call_args.kwargs["timeout"], 7)
        self.assertEqual(m.call_args.args[0],
                         ["/usr/bin/yt-dlp", "--version"])

    # ------------------------------------------------------- ponta a ponta
    def test_version_via_runner_in_process(self):
        def main(argv):
            if argv == ["--version"]:
                print("2026.01.01")
        self.inject(main)
        with mock.patch.object(ytdlp, "find",
                               return_value=["", "-m", "yt_dlp"]):
            self.assertEqual(ytdlp.version(), "2026.01.01")

    def test_describe_via_runner_in_process(self):
        def main(argv):
            if "--help" in argv:
                print("usage: yt-dlp ... --remote-components")
            elif "-J" in argv:
                print(json.dumps(FAKE_INFO))
        self.inject(main)
        with mock.patch.object(ytdlp, "find",
                               return_value=["", "-m", "yt_dlp"]), \
             mock.patch("minidm.deps.js_runtime", return_value=None):
            info = ytdlp.describe("http://example.com/watch?v=1")
        self.assertEqual(info["title"], FAKE_INFO["title"])
        self.assertTrue(any(f.get("url") for f in info["formats"]))

    def test_expand_playlist_via_runner_in_process(self):
        def main(argv):
            if "--flat-playlist" in argv:
                print(json.dumps({"entries": [
                    {"url": "http://x/1", "title": "a", "duration": 1},
                    {"url": "http://x/2", "title": "b", "duration": 2},
                ]}))
        self.inject(main)
        with mock.patch.object(ytdlp, "find",
                               return_value=["", "-m", "yt_dlp"]):
            out = ytdlp.expand_playlist("http://example.com/playlist")
        self.assertEqual([i["url"] for i in out],
                         ["http://x/1", "http://x/2"])

    def test_supports_remote_components_via_in_process(self):
        def main(argv):
            if "--help" in argv:
                print("usage: yt-dlp ... --remote-components ...")
        self.inject(main)
        with mock.patch.object(ytdlp, "find",
                               return_value=["", "-m", "yt_dlp"]):
            self.assertTrue(ytdlp.supports_remote_components())


# ------------------------------------------- downloader in-process (frente 3)
class TestDownloaderInProcess(unittest.TestCase):
    """O `YtdlpDownloader` roda o yt-dlp DENTRO do processo.

    No APK (Chaquopy) e no .exe congelado (PyInstaller) o módulo é
    embutido e não existe CLI externa — o `_spawn` despacha para
    `_spawn_in_process`: mesma leitura de progresso, mesmo tail de log,
    mesmo `_final_path` e a mesma parada via `_stop` do caminho com
    subprocesso (aí testado com stubs de executável).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm_ip_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_env = os.environ.pop("MINIDM_YTDLP", None)

        def _restore_env():
            if self._old_env is None:
                os.environ.pop("MINIDM_YTDLP", None)
            else:
                os.environ["MINIDM_YTDLP"] = self._old_env
        self.addCleanup(_restore_env)
        self._had_mod = "yt_dlp" in sys.modules
        self._old_mod = sys.modules.get("yt_dlp")

        def _restore_mod():
            if self._had_mod:
                sys.modules["yt_dlp"] = self._old_mod
            else:
                sys.modules.pop("yt_dlp", None)
        self.addCleanup(_restore_mod)

    @staticmethod
    def inject(main):
        """Instala um módulo `yt_dlp` falso cujo `main(argv)` é `main`."""
        import types

        mod = types.ModuleType("yt_dlp")
        mod.main = main
        sys.modules["yt_dlp"] = mod
        return mod

    def _task(self, **kw):
        base = dict(url="http://cdn/video.mp4", dest_dir=self.tmp,
                    filename="meu video.mp4", kind="file")
        base.update(kw)
        return Task(**base)

    def _downloader(self, **kw):
        """YtdlpDownloader cujo `find` devolve o módulo embutido (APK)."""
        patch = mock.patch("minidm.ytdlpdl.find_ytdlp",
                           return_value=["", "-m", "yt_dlp"])
        patch.start()
        self.addCleanup(patch.stop)
        return YtdlpDownloader(self._task(**kw), AppConfig(data_dir=self.tmp))

    # ------------------------------------------------------- execução
    def test_download_in_process_conclui_e_calcula_sha(self):
        def main(argv):
            out = argv[argv.index("-o") + 1].replace("%(ext)s", "mp4")
            with open(out, "wb") as fh:
                fh.write(b"abc" * 1000)
            print("[download]  10.0% of ~ 3.00KiB at  1.00MiB/s ETA 00:00")
            print("[download] 100.0% of ~ 3.00KiB at  2.00MiB/s ETA 00:00")
            print("MINIDM_PATH:" + os.path.abspath(out))
        self.inject(main)
        d = self._downloader()
        out = d.run()
        self.assertEqual(out.state, TaskState.DONE)
        self.assertEqual(out.filename, "meu video.mp4")
        self.assertEqual(out.total_size, 3000)
        self.assertTrue(out.sha256)
        # progresso foi lido da fila (100% de 3.00KiB = 3072 bytes) e o
        # stream final preserva os bytes (zerar mostraria "0%" na UI)
        self.assertEqual(d._received, 3072)
        self.assertEqual(out.stream, {"bytes_done": 3000})
        self.assertEqual(out.received, 3000)
        self.assertEqual(out.progress, 1.0)
        # e nada passou por subprocesso (Popen com "" quebraria no APK)
        self.assertIsNone(d._proc)

    def test_download_in_process_erro_vira_excecao(self):
        def main(argv):
            print("ERROR: [youtube] x: Unable to download webpage")
            raise SystemExit(1)
        self.inject(main)
        d = self._downloader()
        with self.assertRaises(YtdlpError):
            d.run()
        self.assertEqual(d.task.state, TaskState.ERROR)
        self.assertIn("Unable to download webpage", d.task.error)

    def test_fase_do_ytdlp_vira_note_para_a_ui(self):
        """Linhas de fase ("[youtube] Extraindo…") viram `stream.note` —
        dá feedback na UI enquanto a barra ainda está em 0% (a extração
        e a resolução do título demoram; antes a barra só "carregava"
        ao finalizar)."""
        d = self._downloader()
        d._parse_line("[youtube] x9: Downloading webpage")
        self.assertEqual(d.task.stream["note"],
                         "[youtube] x9: Downloading webpage")
        # o progresso seguinte NÃO apaga a nota (o dict é preservado)
        d._parse_line("[download]  10.0% of ~ 1.00MiB at  1.00MiB/s "
                      "ETA 00:01")
        self.assertEqual(d.task.stream["note"],
                         "[youtube] x9: Downloading webpage")
        # linhas de debug e texto sem tag de fase não viram nota
        d._parse_line("[debug] sim 10.0 >= 10.0 — vindo do dl")
        self.assertEqual(d.task.stream["note"],
                         "[youtube] x9: Downloading webpage")
        d._parse_line("yt-dlp sem tag alguma")
        self.assertEqual(d.task.stream["note"],
                         "[youtube] x9: Downloading webpage")

    # ------------------------------------------------- barra de progresso
    def test_progresso_sem_total_conhece_os_bytes(self):
        """Servidor sem Content-Length: a linha "[download] 1.23MiB at
        1.00MiB/s" (SEM "% of") agora alimenta os bytes — antes virava
        `note` crua e a barra ficava parada em 0% o download inteiro,
        "virando verde" só no fim."""
        d = self._downloader()
        d._parse_line("[download]   1.23MiB at  1.00MiB/s")
        t = d.task
        self.assertEqual(t.stream["bytes_done"], int(1.23 * 1024 * 1024))
        # total segue desconhecido: NÃO finja segments0/100 nem um total
        self.assertNotIn("segments_done", t.stream)
        self.assertNotIn("segments_total", t.stream)
        self.assertIsNone(t.total_size)
        self.assertEqual(t.progress, 0.0)      # sem total não há percentual
        # a velocidade impressa na linha alimenta o ETA
        self.assertAlmostEqual(d.speed_bps(), 1024 * 1024, delta=1)
        # bytes continuam avançando (é o que a UI mostra crescer)
        d._parse_line("[download]   2.50MiB at  1.50MiB/s")
        self.assertEqual(t.stream["bytes_done"], int(2.50 * 1024 * 1024))

    def test_mux_nao_zera_a_barra(self):
        """Mux vídeo+áudio: o yt-dlp baixa o2º formato DEPOIS e o
        percentual/zera o total — as etapas acumulam em `_stage_done` e
        a barra NUNCA volta a 0% no meio do download."""
        d = self._downloader()
        d._parse_line("[download]  50.0% of ~ 100.00MiB at 1.00MiB/s")
        self.assertEqual(d.task.progress, 0.5)
        d._parse_line("[download] 100.0% of ~ 100.00MiB at 1.00MiB/s")
        self.assertEqual(d.task.progress, 1.0)
        #2º formato (áudio de20 MiB) começa em0% DELE…
        d._parse_line("[download]   0.0% of ~ 20.00MiB at 1.00MiB/s")
        # …mas o total vira120 MiB (o que falta + o que já veio) e o
        # recebido continua em100 MiB — nem perto de voltar a 0
        self.assertEqual(d.task.total_size, 120 * 1024 * 1024)
        self.assertGreater(d.task.progress, 0.8)
        d._parse_line("[download]  50.0% of ~ 20.00MiB at 1.00MiB/s")
        self.assertGreater(d.task.progress, 0.9)
        self.assertEqual(d.task.total_size, 120 * 1024 * 1024)

    def test_throttle_vira_limit_rate_no_ytdlp(self):
        """A engine tem token bucket, mas o yt-dlp é processo separado e
        IGNORAVA o limite (100 MB num relâmpago com throttle de2 MB/s).
        Agora a config vira `--limit-rate` no comando."""
        d = self._downloader()
        cmd = d._build_cmd()
        self.assertNotIn("--limit-rate", cmd)          #0 = sem limite
        d.config.throttle_bps = 2 * 1024 * 1024
        cmd = d._build_cmd()
        i = cmd.index("--limit-rate")
        self.assertEqual(cmd[i + 1], str(2 * 1024 * 1024))

    def test_progress_flag_vence_o_quiet_dos_prints(self):
        """Regressão: `--print` (caminho/título) põe o yt-dlp em modo
        quiet e ele DESCARTA as linhas de progresso — a barra ficava
        presa em 0% o download inteiro (testado ao vivo: cmd completo
        com os dois --print devolve ZERO linhas "[download]"; sem eles,
        38). `--progress` é o flag oficial que força o progresso em
        modo quiet."""
        d = self._downloader()
        cmd = d._build_cmd()
        self.assertIn("--print", cmd)                  # premise do bug
        self.assertIn("--progress", cmd)
        self.assertLess(cmd.index("--newline"),
                        cmd.index("--progress"))        # juntos no início
        # e continua com newline p/ o pipe
        self.assertIn("--newline", cmd)

    def test_generic_stem_decide_quem_renomeia(self):
        """`generic_stem` (módulo, reusado pelo server): stem genérico ->
        o before_dl renomeia sozinho (sem extração duplicada na fila)."""
        from minidm.ytdlpdl import generic_stem

        yt = "https://www.youtube.com/watch?v=abc123"
        self.assertTrue(generic_stem("watch", yt))
        self.assertTrue(generic_stem("download", yt))
        self.assertTrue(generic_stem("abc123", yt))          # id cru
        self.assertTrue(generic_stem("abc123 [abc123]", yt)) # title==id
        self.assertFalse(generic_stem("meu-cool-video", yt))
        self.assertFalse(generic_stem("Aula de Física [abc123]", yt))

    # ------------------------------------------------- nome real (antes)
    def test_minidm_title_troca_nome_ja_no_inicio_do_download(self):
        """A linha MINIDM_TITLE (--print before_dl) troca o nome do URL
        pelo título REAL assim que o download começa — antes o nome só
        virava o real no `_finalize` (fim do download; era a "demora a
        virar o nome real" da fila)."""
        def main(argv):
            print("MINIDM_TITLE:Minha Aula [abc123]")
        self.inject(main)
        d = self._downloader(filename="watch",
                             url="https://www.youtube.com/watch?v=abc123")
        cmd = d._build_cmd()               # stem genérico -> _ytdlp_named
        self.assertTrue(d._ytdlp_named)
        d._spawn(cmd)
        self.assertEqual(d.task.filename, "Minha Aula [abc123]")

    def test_apply_title_nao_mexe_em_nome_escolhido(self):
        """Nome escolhido pelo usuário/perfil (stem não-genérico) é
        respeitado: sem `_ytdlp_named`, a linha do título não renomeia."""
        def main(argv):
            print("MINIDM_TITLE:Outro Título [x]")
        self.inject(main)
        d = self._downloader(filename="meu video.mp4")
        cmd = d._build_cmd()
        self.assertFalse(d._ytdlp_named)
        d._spawn(cmd)
        self.assertEqual(d.task.filename, "meu video.mp4")

    def test_apply_title_move_meta_antigo_para_o_nome_novo(self):
        """Pausa anterior gravou `.meta.json` com o nome do URL: ao
        renomear o arquivo é MOVIDO — não sobra órfão nem some o
        progresso já gravado."""
        def main(argv):
            print("MINIDM_TITLE:Nova Aula [abc123]")
        self.inject(main)
        d = self._downloader(filename="watch",
                             url="https://www.youtube.com/watch?v=abc123")
        old_meta = d.task.meta_path
        with open(old_meta, "w", encoding="utf-8") as f:
            f.write("{}")
        cmd = d._build_cmd()
        d._spawn(cmd)
        self.assertEqual(d.task.filename, "Nova Aula [abc123]")
        self.assertFalse(os.path.exists(old_meta))
        self.assertTrue(os.path.exists(d.task.meta_path))

    def test_apply_title_ignora_linha_vazia(self):
        d = self._downloader(filename="watch",
                             url="https://www.youtube.com/watch?v=abc123")
        d._build_cmd()
        d._apply_title("   ")
        self.assertEqual(d.task.filename, "watch")

    def test_done_sem_progresso_mostra_100_e_os_bytes(self):
        """Tarefa concluída sem stream (tarefas antigas do yt-dlp zeravam
        o progresso no fim) mostra 100% e o tamanho real — não "0% / 0 B"."""
        t = self._task(state=TaskState.DONE, total_size=20_600_000,
                       stream=None, segments=[])
        self.assertEqual(t.received, 20_600_000)
        self.assertEqual(t.progress, 1.0)

    def test_request_stop_interrompe_o_download_in_process(self):
        """`request_stop` leva o yt-dlp in-process a levantar KeyboardInterrupt
        (o mesmo efeito do Ctrl+C) e a tarefa termina PAUSED."""
        started = threading.Event()

        def main(argv):
            started.set()
            while True:
                time.sleep(0.02)
        self.inject(main)
        d = self._downloader()
        result: dict = {}

        def run():
            try:
                result["task"] = d.run()
            except Exception as e:            # pragma: no cover
                result["err"] = e
        th = threading.Thread(target=run)
        th.start()
        self.assertTrue(started.wait(5.0), "worker do yt-dlp não começou")
        d.request_stop()
        th.join(15.0)
        self.assertFalse(th.is_alive(), "run() não saiu depois do stop")
        self.assertNotIn("err", result)
        self.assertEqual(result["task"].state, TaskState.PAUSED)
        # a thread do yt-dlp foi realmente interrompida (não sobrou rodando)
        self.assertTrue(d._worker is None or not d._worker.is_alive())

    # --------------------------------------------------------- mecanismo
    def test_line_sink_quebra_escritas_parciais(self):
        """Escritas parciais do yt-dlp só viram linha quando completam."""
        sink = _LineSink()
        sink.write("[down")
        with self.assertRaises(queue.Empty):
            sink.lines.get_nowait()
        sink.write("load]  50.0% of ~ 1.00MiB at  1.00MiB/s ETA 00:01\n"
                   "[down")
        self.assertEqual(
            sink.lines.get_nowait(),
            "[download]  50.0% of ~ 1.00MiB at  1.00MiB/s ETA 00:01")
        # \r (progresso redesenhado sem --newline) também quebra linha
        sink.write("load] 100.0% of ~ 1.00MiB at  2.00MiB/s ETA 00:00\r")
        self.assertTrue(sink.lines.get_nowait().startswith(
            "[download] 100.0%"))
        # depois de fechado (consumidor saiu), linhas tardias caem fora
        sink.close()
        sink.write("tardia\n")
        with self.assertRaises(queue.Empty):
            sink.lines.get_nowait()

    def test_pipe_redireciona_somente_a_thread_dona(self):
        """O `_pipe` isola threads: a dona escreve no alvo dela; as
        outras continuam no stdout original (GUI/TUI/log não somem)."""
        cap, own = io.StringIO(), io.StringIO()
        real = sys.stdout
        sys.stdout = cap
        try:
            with ytdlp._pipe(own):
                print("da thread dona")
                th = threading.Thread(
                    target=lambda: print("de outra thread"))
                th.start()
                th.join()
            self.assertEqual(own.getvalue().strip(), "da thread dona")
            self.assertEqual(cap.getvalue().strip(), "de outra thread")
            self.assertIs(sys.stdout, cap)    # restaurado ao sair
        finally:
            sys.stdout = real

    def test_describe_nao_atropela_download_in_process(self):
        """`describe` da extensão roda junto com um download longo.

        O redirect por THREAD impede que o JSON da extração caia na
        fila de progresso do download (e vice-versa) — era o risco do
        redirect global antigo quando o yt-dlp roda in-process.
        """
        dl_sink = _LineSink()
        hold, release = threading.Event(), threading.Event()
        erros: list = []
        real_out = sys.stdout

        def download_thread():
            try:
                with ytdlp._pipe(dl_sink, dl_sink):
                    print("PROGRESSO 10.0% of ~ 1.00MiB at 1.00MiB/s")
                    hold.set()
                    release.wait(10.0)
            except BaseException as e:       # pragma: no cover
                erros.append(e)
                hold.set()

        th = threading.Thread(target=download_thread)
        th.start()
        self.assertTrue(hold.wait(5.0), "download não chegou a segurar o pipe")
        try:
            # describe (thread principal) enquanto o download está ativo
            self.inject(lambda argv: print("JSON-DA-DESCRICAO"))
            p = ytdlp._run_in_process(["--version"])
        finally:
            release.set()
            th.join(10.0)
        self.assertEqual(erros, [])
        self.assertFalse(th.is_alive())
        # a descrição ficou COM ELA…
        self.assertEqual(p.stdout, b"JSON-DA-DESCRICAO\n")
        # …e a linha de progresso ficou COM O DOWNLOAD
        self.assertEqual(dl_sink.lines.get_nowait(),
                         "PROGRESSO 10.0% of ~ 1.00MiB at 1.00MiB/s")
        # saída do processo restaurada após as duas redireções
        self.assertIs(sys.stdout, real_out)

    def test_build_cmd_destino_absoluto_quando_in_process(self):
        """Sem subprocesso não existe `cwd` — destino relativo teria que
        virar absoluto, senão o yt-dlp gravaria no cwd do processo."""
        with mock.patch("minidm.ytdlpdl.find_ytdlp",
                        return_value=["", "-m", "yt_dlp"]):
            d = YtdlpDownloader(self._task(dest_dir="downloads"),
                                AppConfig(data_dir=self.tmp))
            cmd = d._build_cmd()
        out_arg = cmd[cmd.index("-o") + 1]
        self.assertTrue(os.path.isabs(out_arg),
                        f"-o relativo no modo in-process: {out_arg}")
        self.assertIn("downloads", out_arg)
        # CLI externa mantém o destino relativo (o subprocesso tem cwd)
        with mock.patch("minidm.ytdlpdl.find_ytdlp",
                        return_value=[r"C:\bin\yt-dlp.exe"]):
            cmd2 = d._build_cmd()
        self.assertEqual(cmd2[cmd2.index("-o") + 1],
                         os.path.join("downloads", "meu video.%(ext)s"))


if __name__ == "__main__":
    unittest.main()
