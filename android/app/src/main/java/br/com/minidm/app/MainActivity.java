package br.com.minidm.app;

import android.Manifest;
import android.app.Activity;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.webkit.MimeTypeMap;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;

/**
 * Casca mínima do APK: sobe a engine minidm (daemon + API local) e mostra
 * o webui — a mesma página servida em http://127.0.0.1:9742/ — num WebView.
 *
 * Toda a lógica (fila, downloads, categorias, tema) vive no Python
 * (minidm/server.py + webui/) e no JavaScript da página; esta Activity só
 * garante que a API subiu antes de carregar a UI.
 *
 * Também expõe {@link #publish} — chamado do Python depois que uma tarefa
 * conclui, para mover o arquivo do armazenamento PRIVADO do app para a
 * pasta pública Download/ (visível no app "Downloads" do sistema).
 */
public class MainActivity extends Activity {
    private static final String HOST = "127.0.0.1";
    private static final int DEFAULT_PORT = 9742;   // AppConfig.api_port
    private static final int BOOT_TRIES = 100;      // ~10 s esperando a API
    private static final int PERMISSION_REQ = 1;

    /** context da app (sobrevive à Activity) p/ o Python publicar arquivos. */
    private static volatile Context appCtx;

    /** Porta da API local (0 = ainda não subiu) — lida pelo DownloadService. */
    private static volatile int apiPort = 0;

    /** Porta da API p/ o {@link DownloadService} fazer o poll de progresso. */
    public static int apiPort() {
        return apiPort;
    }

    private WebView web;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        appCtx = getApplicationContext();

        // Android 7–9 (API 24–28): gravar em Download/ público exige a
        // permissão em runtime; pedida uma vez na abertura. API 29+ usa
        // MediaStore e não precisa de permissão nenhuma.
        if (Build.VERSION.SDK_INT < 29
                && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE},
                    PERMISSION_REQ);
        }

        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);   // o webui é JS puro (sem frameworks)
        s.setDomStorageEnabled(true);   // localStorage p/ o tema claro/escuro
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        web.setWebViewClient(new WebViewClient());

        boot();
    }

    /** Inicia o daemon Python e só carrega a página quando a API responder. */
    private void boot() {
        new Thread(() -> {
            int port = DEFAULT_PORT;
            try {
                // main.start() sobe scheduler + servidor numa thread e
                // devolve a porta efetiva da API
                PyObject started = Python.getInstance()
                        .getModule("main").callAttr("start");
                port = started.toInt();
                apiPort = port;   // DownloadService polla esta porta
            } catch (Throwable t) {
                // sem Python o WebView mostra o erro de carregamento padrão
            }
            awaitPort(port);
            final String url = "http://" + HOST + ":" + port + "/";
            runOnUiThread(() -> web.loadUrl(url));
        }, "minidm-boot").start();
    }

    /** Espera o socket da API abrir (o daemon sobe em thread separada). */
    private static void awaitPort(int port) {
        for (int i = 0; i < BOOT_TRIES; i++) {
            try (Socket s = new Socket()) {
                s.connect(new InetSocketAddress(HOST, port), 200);
                return;
            } catch (Exception e) {
                try {
                    Thread.sleep(100);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
    }

    // ------------------------------------------------------- Download/ público

    /**
     * Move um arquivo concluído para a pasta pública Download/ do sistema.
     *
     * Chamado pelo Python (minidm/androidpub.py) logo após a engine concluir
     * a tarefa: no Android o HOME do app é o armazenamento privado
     * (/data/data/.../files), invisível para o app "Downloads" e para file
     * managers; aqui o arquivo vira visível em Download/&lt;categoria&gt;/.
     *
     * API 29+ (scoped storage): insere via MediaStore.Downloads com
     * IS_PENDING — cópia por stream + apaga a origem, sem permissão.
     * API 24–28: escrita direta na pasta, exige WRITE_EXTERNAL_STORAGE
     * (pedida em onCreate; se negada, devolve null e o arquivo fica onde está).
     *
     * @param srcPath caminho absoluto do arquivo concluído
     * @param subdir  subpasta dentro de Download/ ("Videos"…, "" = na raiz)
     * @param name    nome do arquivo (colisão vira "nome (1).ext")
     * @return caminho público do arquivo movido; null em falha (o download
     *         continua válido — só não fica visível); se o arquivo JÁ estava
     *         em Download/, devolve o próprio caminho de origem.
     *         Nunca lança exceção: qualquer erro vira null.
     */
    public static String publish(String srcPath, String subdir, String name) {
        Context ctx = appCtx;
        if (ctx == null || srcPath == null || name == null || name.isEmpty()) {
            return null;
        }
        File src = new File(srcPath);
        if (!src.isFile()) return null;
        if (src.length() == 0) return null;   // nunca publica 0 bytes
        String sub = subdir == null ? "" : subdir.trim();
        try {
            // já está na pasta pública? (ex.: download_base apontando p/ lá)
            File root = Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS);
            String pub = (sub.isEmpty() ? root : new File(root, sub))
                    .getAbsolutePath();
            if (src.getAbsolutePath().startsWith(pub + File.separator)) {
                return src.getAbsolutePath();
            }
        } catch (Throwable ignore) {
            return null;
        }
        if (Build.VERSION.SDK_INT >= 29) {
            return publishScoped(ctx, src, sub, name);
        }
        return publishLegacy(ctx, src, sub, name);
    }

    /** API 29+: MediaStore.Downloads (IS_PENDING evita file meio-baixado). */
    private static String publishScoped(Context ctx, File src, String sub,
                                        String name) {
        ContentResolver cr = ctx.getContentResolver();
        String rel = sub.isEmpty() ? Environment.DIRECTORY_DOWNLOADS
                : Environment.DIRECTORY_DOWNLOADS + "/" + sub;
        String usedRel = rel;
        String usedName = uniqueName(cr, rel, name);
        Uri out = insertPending(cr, rel, usedName, src.length());
        if (out == null && !sub.isEmpty()) {
            // nem todo aparelho/recorte aceita subpasta: tenta na raiz
            usedRel = Environment.DIRECTORY_DOWNLOADS;
            usedName = uniqueName(cr, usedRel, name);
            out = insertPending(cr, usedRel, usedName, src.length());
        }
        if (out == null) return null;

        boolean ok = copyTo(cr, out, src);
        // caminho final na pasta pública (scan + retorno ao Python)
        File root = Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DOWNLOADS);
        String dir = usedRel.equals(Environment.DIRECTORY_DOWNLOADS)
                ? root.getAbsolutePath()
                : root.getAbsolutePath() + usedRel.substring(
                        Environment.DIRECTORY_DOWNLOADS.length());
        String outPath = new File(dir, usedName).getAbsolutePath();
        if (ok) {
            try {                       // fim do pending: arquivo visível
                ContentValues fin = new ContentValues();
                fin.put(MediaStore.MediaColumns.IS_PENDING, 0);
                cr.update(out, fin, null, null);
            } catch (Throwable ignore) {
            }
        }
        if (!ok || !src.delete()) {
            // não copiou ou não apagou a origem: desfaz (sem duplicar)
            try {
                cr.delete(out, null, null);
            } catch (Throwable ignore) {
            }
            return null;
        }
        // garante que SIZE/índices fiquem atualizados no MediaStore
        // (sem isso alguns file managers mostram 0 B no arquivo)
        try {
            MediaScannerConnection.scanFile(ctx, new String[]{outPath},
                    new String[]{mimeOf(usedName)}, null);
        } catch (Throwable ignore) {
        }
        return outPath;
    }

    /** API 24–28: escrita direta em Download/ (com permissão concedida). */
    @SuppressWarnings("deprecation")
    private static String publishLegacy(Context ctx, File src, String sub,
                                        String name) {
        try {
            if (ctx.checkSelfPermission(
                    Manifest.permission.WRITE_EXTERNAL_STORAGE)
                    != PackageManager.PERMISSION_GRANTED) {
                return null;            // negada: arquivo fica no privado
            }
            File root = Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS);
            File dir = sub.isEmpty() ? root : new File(root, sub);
            if (!dir.isDirectory() && !dir.mkdirs()) return null;

            String stem = name, ext = "";
            int dot = name.lastIndexOf('.');
            if (dot > 0) {
                stem = name.substring(0, dot);
                ext = name.substring(dot);
            }
            File dst = new File(dir, name);
            for (int i = 1; dst.exists() && i < 500; i++) {
                dst = new File(dir, stem + " (" + i + ")" + ext);
            }
            if (dst.exists()) return null;

            if (!copyFile(src, dst)) {
                dst.delete();
                return null;
            }
            if (!src.delete()) {        // não apagou a origem: desfaz
                dst.delete();
                return null;
            }
            // pré-Q não indexa sozinho: avisa o MediaScanner p/ o app
            // "Downloads" enxergar o arquivo na hora
            MediaScannerConnection.scanFile(ctx,
                    new String[]{dst.getAbsolutePath()},
                    new String[]{mimeOf(name)}, null);
            return dst.getAbsolutePath();
        } catch (Throwable t) {
            return null;
        }
    }

    private static Uri insertPending(ContentResolver cr, String rel,
                                     String name, long size) {
        try {
            ContentValues v = new ContentValues();
            v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
            v.put(MediaStore.MediaColumns.MIME_TYPE, mimeOf(name));
            v.put(MediaStore.MediaColumns.RELATIVE_PATH, rel);
            // tamanho conhecido desde o insert: sem isso alguns file
            // managers mostram "0 B" até o próximo scan do sistema
            v.put(MediaStore.MediaColumns.SIZE, size);
            v.put(MediaStore.MediaColumns.IS_PENDING, 1);
            return cr.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
        } catch (Throwable t) {
            return null;
        }
    }

    /** Nome livre na pasta (mesma lógica de "arquivo (1).ext" do sistema). */
    private static String uniqueName(ContentResolver cr, String rel,
                                     String name) {
        String stem = name, ext = "";
        int dot = name.lastIndexOf('.');
        if (dot > 0) {
            stem = name.substring(0, dot);
            ext = name.substring(dot);
        }
        String cand = name;
        for (int i = 1; i < 500; i++) {
            if (!existsIn(cr, rel, cand)) return cand;
            cand = stem + " (" + i + ")" + ext;
        }
        return name;
    }

    private static boolean existsIn(ContentResolver cr, String rel,
                                    String name) {
        Cursor c = null;
        try {
            // o MediaStore guarda RELATIVE_PATH com barra final
            String path = rel.endsWith("/") ? rel : rel + "/";
            c = cr.query(MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                    new String[]{MediaStore.MediaColumns.DISPLAY_NAME},
                    MediaStore.MediaColumns.DISPLAY_NAME + "=? AND "
                            + MediaStore.MediaColumns.RELATIVE_PATH + "=?",
                    new String[]{name, path}, null);
            return c != null && c.moveToFirst();
        } catch (Throwable t) {
            return false;               // sem query: o insert pode falhar
        } finally {
            if (c != null) c.close();
        }
    }

    private static boolean copyTo(ContentResolver cr, Uri out, File src) {
        try (InputStream in = new FileInputStream(src);
             OutputStream os = cr.openOutputStream(out)) {
            if (os == null) return false;
            byte[] buf = new byte[128 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    private static boolean copyFile(File src, File dst) {
        try (InputStream in = new FileInputStream(src);
             OutputStream out = new FileOutputStream(dst)) {
            byte[] buf = new byte[128 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    private static String mimeOf(String name) {
        try {
            String ext = "";
            int dot = name.lastIndexOf('.');
            if (dot > 0 && dot < name.length() - 1) {
                ext = name.substring(dot + 1).toLowerCase();
            }
            String mime = ext.isEmpty() ? null
                    : MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext);
            return mime != null ? mime : "application/octet-stream";
        } catch (Throwable t) {
            return "application/octet-stream";
        }
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();               // volta p/ visita anterior da webui
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onPause() {
        // saiu da tela: o download NÃO pode morrer com a Activity —
        // vira serviço em primeiro plano (notificação + wake/wifi lock)
        DownloadService.start(this);
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onResume() {
        // voltou ao app: a notificação de segundo plano não faz falta
        // (a engine segue rodando no processo; volta a serviço ao sair)
        DownloadService.stop(this);
        web.onResume();
        super.onResume();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }
}
