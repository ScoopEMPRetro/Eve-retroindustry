package com.everetro.poc;

import android.annotation.SuppressLint;
import android.os.Bundle;
import android.util.Log;
import android.view.View;
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
    private static final int PORT = 8000;
    private static final String URL = "http://127.0.0.1:" + PORT;

    private WebView web;
    private TextView status;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        web = findViewById(R.id.webview);
        status = findViewById(R.id.status);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);            // localStorage (poslední stanice/blueprinty)
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        // Stránka je http (localhost), ale tahá Bootstrap z https CDN → povolit mix.
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        web.setWebViewClient(new WebViewClient());   // navigace zůstane ve WebView

        // Hardwarové tlačítko zpět = historie WebView.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() {
                if (web.canGoBack()) web.goBack();
                else { setEnabled(false); getOnBackPressedDispatcher().onBackPressed(); }
            }
        });

        new Thread(this::boot, "eve-boot").start();
    }

    /** Běží na pozadí: rozbalí assety, nastartuje Python server, počká na port. */
    private void boot() {
        try {
            File filesDir = getFilesDir();
            setStatus("Rozbaluji data…");
            extractBundle(filesDir);

            setStatus("Spouštím Python…");
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
            }
            final Python py = Python.getInstance();
            final PyObject mod = py.getModule("android_main");
            // Předej Activity Pythonu — potřebné pro otevření SSO loginu přes Intent.
            mod.callAttr("set_context", this);

            // start_server je blokující (uvicorn.serve) → vlastní vlákno.
            new Thread(() -> {
                try {
                    mod.callAttr("start_server", filesDir.getAbsolutePath(), PORT);
                } catch (Throwable t) {
                    Log.e(TAG, "server crashed", t);
                    setStatus("Server spadl:\n" + Log.getStackTraceString(t));
                }
            }, "eve-uvicorn").start();

            setStatus("Čekám na server…");
            if (!waitForServer(mod, 30_000)) {
                setStatus("Server nenaběhl do 30 s — viz logcat (python.stdout).");
                return;
            }

            runOnUiThread(() -> {
                status.setVisibility(View.GONE);
                web.setVisibility(View.VISIBLE);
                web.loadUrl(URL);
            });
        } catch (Throwable t) {
            Log.e(TAG, "boot failed", t);
            setStatus("Start selhal:\n" + Log.getStackTraceString(t));
        }
    }

    /** Poll přes Python helper, dokud server nepřijímá spojení (nebo timeout). */
    private boolean waitForServer(PyObject mod, long timeoutMs) {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            try {
                if (mod.callAttr("is_up", PORT).toBoolean()) return true;
                Thread.sleep(200);
            } catch (InterruptedException e) {
                return false;
            }
        }
        return false;
    }

    /**
     * Rozbalí assets/bundle/** do filesDir/** (sde_base.db + app/web/templates).
     * Přepisuje při každém startu — levné a zajistí čerstvé šablony po updatu.
     * eve_cache.db (uživatelská data) se NEMAŽE — leží vedle, ne v bundle/.
     */
    private void extractBundle(File filesDir) throws IOException {
        copyAsset("bundle", filesDir);
    }

    private void copyAsset(String path, File outRoot) throws IOException {
        String[] children = getAssets().list(path);
        if (children == null || children.length == 0) {
            // list je soubor (ne adresář) → zkopíruj. "bundle/" prefix odřízni.
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
}
