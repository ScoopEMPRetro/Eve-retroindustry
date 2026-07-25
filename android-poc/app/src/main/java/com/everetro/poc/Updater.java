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
 * In-app update for the sideloaded APK. Downloads version.json from the
 * "android-latest" prerelease, compares versionCode against the installed one
 * (BuildConfig), and if the remote is newer, offers to download + install it
 * via the system installer.
 *
 * Prerequisite: all builds signed with the same key (CI release keystore) —
 * otherwise Android refuses an update over the existing app.
 */
public class Updater {
    private static final String TAG = "EveRetroUpdate";
    // Stable URL for the LATEST normal release (ignores prereleases like
    // android-latest). version.json is an asset of every release (from release.yml).
    private static final String VERSION_URL =
        "https://github.com/ScoopEMPRetro/Eve-retroindustry/releases/latest/download/version.json";

    /** Runs the check in the background; shows a dialog (UI thread) if an update is available. */
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
                Log.w(TAG, "update check failed", t);  // silent — offline etc.
            }
        }, "eve-update-check").start();
    }

    /** Manual check (from a UI button) — always gives feedback, even when everything is up to date. */
    public static void checkManual(Activity act) {
        act.runOnUiThread(() ->
            Toast.makeText(act, "Checking for updates…", Toast.LENGTH_SHORT).show());
        new Thread(() -> {
            try {
                JSONObject meta = fetchJson(VERSION_URL);
                int remote = meta.optInt("versionCode", -1);
                String name = meta.optString("versionName", "?");
                String apkUrl = meta.optString("apkUrl", "");
                int local = BuildConfig.VERSION_CODE;
                act.runOnUiThread(() -> {
                    if (remote > local && !apkUrl.isEmpty()) {
                        promptUpdate(act, name, apkUrl);
                    } else {
                        Toast.makeText(act, "You have the latest version (" + BuildConfig.VERSION_NAME + ").",
                                Toast.LENGTH_LONG).show();
                    }
                });
            } catch (Throwable t) {
                Log.w(TAG, "manual update check failed", t);
                act.runOnUiThread(() -> Toast.makeText(act,
                        "Update check failed: " + t.getMessage(), Toast.LENGTH_LONG).show());
            }
        }, "eve-update-manual").start();
    }

    private static void promptUpdate(Activity act, String name, String apkUrl) {
        new AlertDialog.Builder(act)
            .setTitle("Update available")
            .setMessage("Version " + name + " is available. Download and install?")
            .setPositiveButton("Update", (d, w) -> startUpdate(act, apkUrl))
            .setNegativeButton("Later", null)
            .show();
    }

    private static void startUpdate(Activity act, String apkUrl) {
        // Android 8+: the app must be allowed to install from unknown sources.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                && !act.getPackageManager().canRequestPackageInstalls()) {
            Toast.makeText(act, "Allow installing updates for this app and try again.",
                    Toast.LENGTH_LONG).show();
            act.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + act.getPackageName())));
            return;
        }
        Toast.makeText(act, "Downloading update…", Toast.LENGTH_SHORT).show();
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
                        "Update failed: " + t.getMessage(), Toast.LENGTH_LONG).show());
            }
        }, "eve-update-dl").start();
    }

    // ── HTTP helpers (follow GitHub redirects to objects.githubusercontent.com) ──

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

    /** Opens the connection and manually follows up to 5 redirects (incl. cross-host https). */
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
                if (loc == null) throw new Exception("redirect without Location");
                url = loc;
                continue;
            }
            if (code != 200) {
                c.disconnect();
                throw new Exception("HTTP " + code + " for " + url);
            }
            return c;
        }
        throw new Exception("too many redirects");
    }
}
