package com.everetro.poc;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.util.Log;
import android.widget.Toast;

import androidx.appcompat.app.AlertDialog;
import androidx.core.content.FileProvider;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * In-app update pro sideload APK. Stáhne version.json z prerelease
 * "android-latest", porovná versionCode s nainstalovaným (BuildConfig) a když
 * je vzdálený novější, nabídne stažení + instalaci přes systémový instalátor.
 *
 * Podmínka funkčnosti: všechny buildy podepsané stejným klíčem (CI release
 * keystore) — jinak Android update přes stávající appku odmítne.
 */
public class Updater {
    private static final String TAG = "EveRetroUpdate";
    private static final String VERSION_URL =
        "https://github.com/ScoopEMPRetro/Eve-retroindustry/releases/download/android-latest/version.json";

    /** Spustí kontrolu na pozadí; při dostupném updatu ukáže dialog (UI thread). */
    public static void check(Activity act) {
        new Thread(() -> {
            try {
                JSONObject meta = fetchJson(VERSION_URL);
                int remote = meta.optInt("versionCode", -1);
                String name = meta.optString("versionName", "?");
                String apkUrl = meta.optString("apkUrl", "");
                int local = BuildConfig.VERSION_CODE;
                Log.i(TAG, "local=" + local + " remote=" + remote);
                if (remote > local && !apkUrl.isEmpty()) {
                    act.runOnUiThread(() -> promptUpdate(act, name, apkUrl));
                }
            } catch (Throwable t) {
                Log.w(TAG, "update check failed", t);  // tiché — offline apod.
            }
        }, "eve-update-check").start();
    }

    private static void promptUpdate(Activity act, String name, String apkUrl) {
        new AlertDialog.Builder(act)
            .setTitle("Aktualizace k dispozici")
            .setMessage("Je dostupná verze " + name + ". Stáhnout a nainstalovat?")
            .setPositiveButton("Aktualizovat", (d, w) -> startUpdate(act, apkUrl))
            .setNegativeButton("Později", null)
            .show();
    }

    private static void startUpdate(Activity act, String apkUrl) {
        // Android 8+: appka musí mít povolenu instalaci z neznámých zdrojů.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                && !act.getPackageManager().canRequestPackageInstalls()) {
            Toast.makeText(act, "Povol instalaci aktualizací pro tuto aplikaci a zkus to znovu.",
                    Toast.LENGTH_LONG).show();
            act.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + act.getPackageName())));
            return;
        }
        Toast.makeText(act, "Stahuji aktualizaci…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                File dir = new File(act.getExternalFilesDir(null), "updates");
                dir.mkdirs();
                File apk = new File(dir, "EveRetroindustry.apk");
                download(apkUrl, apk);
                Uri uri = FileProvider.getUriForFile(
                        act, act.getPackageName() + ".fileprovider", apk);
                Intent i = new Intent(Intent.ACTION_VIEW);
                i.setDataAndType(uri, "application/vnd.android.package-archive");
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
                act.startActivity(i);
            } catch (Throwable t) {
                Log.e(TAG, "update download/install failed", t);
                act.runOnUiThread(() -> Toast.makeText(act,
                        "Aktualizace selhala: " + t.getMessage(), Toast.LENGTH_LONG).show());
            }
        }, "eve-update-dl").start();
    }

    // ── HTTP helpers (sledují GitHub redirecty na objects.githubusercontent.com) ──

    private static JSONObject fetchJson(String url) throws Exception {
        HttpURLConnection c = open(url);
        try (InputStream in = c.getInputStream()) {
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            return new JSONObject(bos.toString("UTF-8"));
        } finally {
            c.disconnect();
        }
    }

    private static void download(String url, File out) throws Exception {
        HttpURLConnection c = open(url);
        try (InputStream in = c.getInputStream();
             OutputStream os = new FileOutputStream(out)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
        } finally {
            c.disconnect();
        }
    }

    /** Otevře spojení a ručně dosleduje až 5 redirectů (i cross-host https). */
    private static HttpURLConnection open(String url) throws Exception {
        for (int hop = 0; hop < 5; hop++) {
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            c.setInstanceFollowRedirects(false);
            c.setConnectTimeout(15000);
            c.setReadTimeout(30000);
            c.setRequestProperty("User-Agent", "EveRetroindustry-Android");
            int code = c.getResponseCode();
            if (code >= 300 && code < 400) {
                String loc = c.getHeaderField("Location");
                c.disconnect();
                if (loc == null) throw new Exception("redirect bez Location");
                url = loc;
                continue;
            }
            if (code != 200) {
                c.disconnect();
                throw new Exception("HTTP " + code + " pro " + url);
            }
            return c;
        }
        throw new Exception("příliš mnoho redirectů");
    }
}
