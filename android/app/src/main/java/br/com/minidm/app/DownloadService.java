package br.com.minidm.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Foreground service que mantém os downloads vivos com a Activity em
 * segundo plano (frente 2 — estilo ADM).
 *
 * <p>O serviço <b>não</b> baixa nada: a engine Python (daemon + API em
 * 127.0.0.1) roda no mesmo processo e continua viva enquanto o processo
 * viver. O que este serviço faz é:
 *
 * <ul>
 *   <li>{@code startForeground()} imediato com notificação provisória
 *       (exigência do Android desde que o serviço é iniciado);</li>
 *   <li>manter {@link WifiManager.WifiLock} + {@link PowerManager.WakeLock}
 *       para a transferência não ser cortada com a tela desligada;</li>
 *   <li>poll de {@code GET /status} a cada 1,5 s para mostrar progresso
 *       na notificação (campos do snapshot: {@code state}, {@code filename},
 *       {@code progress} 0..1);</li>
 *   <li>parar SOZINHO quando a fila esvazia (nenhuma tarefa
 *       {@code queued}/{@code downloading}) — sem notificação fantasma;</li>
 *   <li>{@code START_STICKY}: o sistema recria o serviço se matar o
 *       processo (downloads retomam a partir do estado salvo).</li>
 * </ul>
 *
 * <p>Ciclo: {@code MainActivity.onPause()} → {@link #start} (saiu da
 * tela) e {@code MainActivity.onResume()} → {@link #stop} (voltou — a
 * notificação não faz falta). Iniciado fora do contexto permitido
 * (Android 12+), {@link #start} engole a exceção e segue sem serviço.
 */
public class DownloadService extends Service {

    private static final String TAG = "MinidmService";
    private static final String CHANNEL_ID = "minidm_downloads";
    private static final int NOTIF_ID = 42;
    private static final long POLL_MS = 1500;
    /** falhas seguidas do /status antes de concluir que a API morreu. */
    private static final int MAX_NET_FAILS = 20;   // ~30 s

    // o poll NÃO pode rodar na main thread (NetworkOnMainThreadException
    // desde a API 11): thread própria com reagendamento automático
    private final ScheduledExecutorService exec =
            Executors.newSingleThreadScheduledExecutor();
    private WifiManager.WifiLock wifiLock;
    private PowerManager.WakeLock wakeLock;
    private int netFails = 0;
    private boolean polling = false;   // hasCallbacks() só existe na API 28+

    private final Runnable poller = new Runnable() {
        @Override
        public void run() {
            int port = MainActivity.apiPort();
            int active = 0;
            int queued = 0;
            String name = "";
            int pct = 0;
            if (port > 0) {
                try {
                    URL url = new URL("http://127.0.0.1:" + port + "/status");
                    HttpURLConnection c =
                            (HttpURLConnection) url.openConnection();
                    c.setConnectTimeout(800);
                    c.setReadTimeout(800);
                    try (InputStream in = c.getInputStream();
                         BufferedReader r = new BufferedReader(
                                 new InputStreamReader(in,
                                         StandardCharsets.UTF_8))) {
                        StringBuilder sb = new StringBuilder();
                        String line;
                        while ((line = r.readLine()) != null) {
                            sb.append(line);
                        }
                        JSONObject obj = new JSONObject(sb.toString());
                        JSONArray tasks = obj.optJSONArray("tasks");
                        if (tasks != null) {
                            for (int i = 0; i < tasks.length(); i++) {
                                JSONObject t = tasks.getJSONObject(i);
                                String st = t.optString("state", "");
                                if ("downloading".equals(st)) {
                                    active++;
                                    if (name.isEmpty()) {
                                        name = t.optString("filename", "…");
                                        pct = (int) Math.round(
                                                t.optDouble("progress", 0)
                                                 * 100);
                                    }
                                } else if ("queued".equals(st)) {
                                    queued++;
                                }
                            }
                        }
                    } finally {
                        c.disconnect();
                    }
                    netFails = 0;
                } catch (Exception e) {
                    // API ainda subindo (ou morreu): conta e tenta de novo
                    netFails++;
                    if (netFails > MAX_NET_FAILS) {
                        Log.i(TAG, "API local não responde mais — parando");
                        stopForegroundCompat();
                        stopSelf();
                        return;
                    }
                }
            }
            if (port > 0 && active + queued == 0) {
                // fila vazia: o serviço não é mais necessário
                stopForegroundCompat();
                stopSelf();
                return;
            }
            if (active > 0) {
                updateNotification("Baixando: " + name,
                        pct + "% · " + queued + " na fila", pct, false);
            } else if (queued > 0) {
                updateNotification("Na fila: " + (active + queued)
                        + " download(s)", "aguardando conexão…", 0, true);
            }
            try {
                exec.schedule(this, POLL_MS, TimeUnit.MILLISECONDS);
            } catch (RejectedExecutionException e) {
                // onDestroy já desligou o executor — fim do poll
            }
        }
    };

    /** Inicia o serviço em primeiro plano (idempotente). */
    public static void start(Context ctx) {
        Intent i = new Intent(ctx, DownloadService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                ctx.startForegroundService(i);
            } else {
                ctx.startService(i);
            }
        } catch (RuntimeException e) {
            // Android 12+: pode negar se o app saiu de primeiro plano
            Log.w(TAG, "não foi possível iniciar o serviço: " + e);
        }
    }

    /** Para o serviço (idempotente — usado no onResume). */
    public static void stop(Context ctx) {
        try {
            ctx.stopService(new Intent(ctx, DownloadService.class));
        } catch (RuntimeException e) {
            Log.w(TAG, "não foi possível parar o serviço: " + e);
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
        // notificação provisória IMEDIATA: sem ela o startForeground
        // derrubaria o serviço nos primeiros segundos
        startForeground(NOTIF_ID,
                build("minidm", "verificando a fila…", 0, true));
        WifiManager wm = (WifiManager) getApplicationContext()
                .getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
            wifiLock = wm.createWifiLock(
                    WifiManager.WIFI_MODE_FULL_HIGH_PERF, "minidm:wifi");
            wifiLock.acquire();
        }
        PowerManager pm = (PowerManager)
                getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK, "minidm:cpu");
            wakeLock.acquire();
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!polling) {
            polling = true;
            exec.execute(poller);
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        polling = false;
        exec.shutdownNow();   // corta o poll em andamento (se houver)
        if (wifiLock != null && wifiLock.isHeld()) {
            wifiLock.release();
        }
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        stopForegroundCompat();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;   // serviço exclusivamente em primeiro plano
    }

    /** Limite de 6 h do tipo dataSync (Android 14+) — encerra com calma. */
    @Override
    public void onTimeout(int startId) {
        Log.i(TAG, "timeout do foreground service — encerrando");
        stopForegroundCompat();
        stopSelf(startId);
    }

    // ------------------------------------------------------------ internos

    private void stopForegroundCompat() {
        stopForeground(STOP_FOREGROUND_REMOVE);   // minSdk 24
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID,
                    "Downloads", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) {
                nm.createNotificationChannel(ch);
            }
        }
    }

    /** Monta a notificação de progresso (toque reabre a Activity). */
    private Notification build(String title, String text, int pct,
                               boolean indeterminate) {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP
                | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, flags);
        Notification.Builder b = (Build.VERSION.SDK_INT >= 26)
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        b.setContentTitle(title)
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentIntent(pi)
                .setOnlyAlertOnce(true)
                .setOngoing(true)
                .setProgress(100, pct, indeterminate);
        return b.build();
    }

    private void updateNotification(String title, String text, int pct,
                                    boolean indeterminate) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.notify(NOTIF_ID, build(title, text, pct, indeterminate));
        }
    }
}
