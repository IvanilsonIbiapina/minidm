package br.com.minidm.app;

import android.app.Activity;
import android.os.Bundle;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import java.net.InetSocketAddress;
import java.net.Socket;

/**
 * Casca mínima do APK: sobe a engine minidm (daemon + API local) e mostra
 * o webui — a mesma página servida em http://127.0.0.1:9742/ — num WebView.
 *
 * Toda a lógica (fila, downloads, categorias, tema) vive no Python
 * (minidm/server.py + webui/) e no JavaScript da página; esta Activity só
 * garante que a API subiu antes de carregar a UI.
 */
public class MainActivity extends Activity {
    private static final String HOST = "127.0.0.1";
    private static final int DEFAULT_PORT = 9742;   // AppConfig.api_port
    private static final int BOOT_TRIES = 100;      // ~10 s esperando a API

    private WebView web;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
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
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }
}
