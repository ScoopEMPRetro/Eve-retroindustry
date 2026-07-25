package com.everetro.poc;

import android.annotation.SuppressLint;
import android.os.Bundle;
import android.util.Log;
import android.view.View;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;

import androidx.activity.OnBackPressedCallback;
import androidx.appcompat.app.AppCompatActivity;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

public class MainActivity extends AppCompatActivity {
    private static final String TAG = "EveRetro";

    private WebView web;
    private TextView status;
    private View statusScroll;   // wrapper for the status text — must be hidden entirely, otherwise it eats touches
    private String url;
    // The server runs once per process (the Android process survives an Activity relaunch).
    // sPort = the port of OUR server for this process (0 = not started yet). We pick a
    // free port dynamically — a hardcoded 8000 could be held by a zombie from last time.
    private static volatile boolean sServerLaunched = false;
    private static volatile int sPort = 0;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        web = findViewById(R.id.webview);
        status = findViewById(R.id.status);
        statusScroll = findViewById(R.id.status_scroll);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);            // localStorage (last stations/blueprints)
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        // The page is http (localhost), but pulls Bootstrap from an https CDN → allow mixing.
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        web.setWebViewClient(new WebViewClient() {
            // When the main frame (our local server) fails to load, show the Python
            // server.log in the app — so the error can be diagnosed even without adb.
            @Override
            public void onReceivedError(WebView view, android.webkit.WebResourceRequest req,
                                        android.webkit.WebResourceError err) {
                if (req != null && req.isForMainFrame()) {
                    showServerLog("Load failed: " + err.getErrorCode()
                            + " " + err.getDescription());
                }
            }
        });
        // WebChromeClient: without it window.alert/confirm/prompt don't work (e.g.
        // confirming character deletion) and console.* logs get lost. We forward
        // them to logcat (tag EveRetro) for diagnostics.
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onConsoleMessage(ConsoleMessage m) {
                Log.i(TAG, "console: " + m.message()
                        + " @ " + m.sourceId() + ":" + m.lineNumber());
                return true;
            }
        });
        // JS bridge: the web UI (About → Check for updates) triggers the native
        // updater. window.AndroidApp.checkForUpdate()
        web.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public void checkForUpdate() {
                Updater.checkManual(MainActivity.this);
            }
        }, "AndroidApp");

        // Hardware back button = WebView history.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() {
                if (web.canGoBack()) web.goBack();
                else { setEnabled(false); getOnBackPressedDispatcher().onBackPressed(); }
            }
        });

        new Thread(this::boot, "eve-boot").start();
    }

    /** Runs in the background: unpacks assets, starts the Python server, waits for the port. */
    private void boot() {
        try {
            File filesDir = getFilesDir();
            setStatus("Unpacking data…");
            extractBundle(filesDir);

            setStatus("Starting Python…");
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
            }
            final Python py = Python.getInstance();
            final PyObject mod = py.getModule("android_main");
            // Pass the Activity to Python — needed to open the SSO login via an Intent.
            mod.callAttr("set_context", this);

            // Start the server only ONCE per process (the Android process survives
            // an Activity relaunch). Grab OUR OWN free port — reusing an existing
            // listener on a fixed port could hit a stuck zombie from last time
            // (ERR_EMPTY_RESPONSE). Within the process we keep our port in sPort.
            final int port;
            if (sServerLaunched && sPort != 0) {
                port = sPort;   // our server from earlier in this process
            } else {
                port = findFreePort();
                sPort = port;
                sServerLaunched = true;
                new Thread(() -> {
                    try {
                        mod.callAttr("start_server", filesDir.getAbsolutePath(), port);
                    } catch (Throwable t) {
                        sServerLaunched = false;   // allow a retry after a crash
                        Log.e(TAG, "server crashed", t);
                        showServerLog("Server crashed: " + t);
                    }
                }, "eve-uvicorn").start();
            }
            url = "http://127.0.0.1:" + port;

            setStatus("Waiting for server…");
            if (!waitForServer(mod, port, 30_000)) {
                setStatus("Server didn't start within 30 s — see logcat (python.stdout).");
                return;
            }

            runOnUiThread(() -> {
                statusScroll.setVisibility(View.GONE);   // hide the whole overlay so the WebView gets touches
                web.setVisibility(View.VISIBLE);
                web.loadUrl(url);
            });

            // Once the UI is up, check for an available update (silently, in the background).
            Updater.check(this);
        } catch (Throwable t) {
            Log.e(TAG, "boot failed", t);
            setStatus("Start failed:\n" + Log.getStackTraceString(t));
        }
    }

    /** Finds a free TCP port on loopback (new server = new port, no conflict). */
    private int findFreePort() {
        try (java.net.ServerSocket ss = new java.net.ServerSocket(
                0, 1, java.net.InetAddress.getByName("127.0.0.1"))) {
            return ss.getLocalPort();
        } catch (Exception e) {
            return 8000;   // fallback
        }
    }

    /** Poll via the Python helper until the server accepts connections (or timeout). */
    private boolean waitForServer(PyObject mod, int port, long timeoutMs) {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            try {
                if (mod.callAttr("is_up", port).toBoolean()) return true;
                Thread.sleep(200);
            } catch (InterruptedException e) {
                return false;
            }
        }
        return false;
    }

    /**
     * Unpacks assets/bundle/** into filesDir/** (sde_base.db + app/web/templates).
     * Overwrites on every start — cheap, and guarantees fresh templates after an update.
     * eve_cache.db (user data) is NOT deleted — it lives alongside, not in bundle/.
     */
    private void extractBundle(File filesDir) throws IOException {
        copyAsset("bundle", filesDir);
    }

    private void copyAsset(String path, File outRoot) throws IOException {
        String[] children = getAssets().list(path);
        if (children == null || children.length == 0) {
            // the entry is a file (not a directory) → copy it. Strip the "bundle/" prefix.
            String rel = path.substring("bundle/".length());
            File out = new File(outRoot, rel);
            File parent = out.getParentFile();
            if (parent != null) parent.mkdirs();
            try (InputStream in = getAssets().open(path);
                 OutputStream os = new FileOutputStream(out)) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
            }
            return;
        }
        for (String child : children) {
            copyAsset(path + "/" + child, outRoot);
        }
    }

    private void setStatus(String msg) {
        runOnUiThread(() -> status.setText(msg));
    }

    /** Shows the tail of the Python server.log in the app (server error diagnostics). */
    private void showServerLog(String header) {
        new Thread(() -> {
            String log;
            try {
                PyObject mod = Python.getInstance().getModule("android_main");
                log = mod.callAttr("get_log", getFilesDir().getAbsolutePath()).toString();
            } catch (Throwable t) {
                log = "(log unavailable: " + t + ")";
            }
            final String text = header + "\n\n=== server.log (tail) ===\n" + log;
            Log.e(TAG, text);
            runOnUiThread(() -> {
                web.setVisibility(View.GONE);
                statusScroll.setVisibility(View.VISIBLE);
                status.setVisibility(View.VISIBLE);
                status.setText(text);
            });
        }, "eve-log").start();
    }
}
