# EVE Retroindustry — Chaquopy Android PoC

Cíl: ověřit, že celý Python závislostní stack aplikace naběhne na Androidu
(přes Chaquopy = CPython runtime v APK). **Make-or-break je `pydantic-core`
(Rust binary)** — pokud se naimportuje, zbytek je přímočará práce.

Spuštěná appka zobrazí report typu:

```
EVE Retroindustry — Chaquopy dependency PoC
Python 3.12.x on x86_64
--------------------------------------------
  OK   fastapi        0.115.0
  OK   pydantic       2.9.2
  OK   pydantic_core  2.23.4      ← tohle je ten klíčový řádek
  OK   sqlalchemy     2.0.35
  ...
  OK   FastAPI + pydantic model/validate works
  OK   sqlite3 read/write works
```

Pokud `pydantic_core` ukáže FAIL, řeší se to (pin staršího FastAPI s pydantic v1,
nebo náhrada za pydantic v našich pár Form endpointech).

---

## 1. Toolchain na CachyOS (zatím nic nainstalováno)

Nejjednodušší je **Android Studio** (přináší SDK, emulátor i správu AVD):

```bash
# Android Studio + JDK (potřebné pro Gradle/AGP)
paru -S android-studio jdk17-openjdk        # nebo: yay -S android-studio
# při prvním spuštění Studio stáhne: Android SDK, platform-tools (API 34).
# system image / emulator NEPOTŘEBUJEME — testuje se na fyzickém telefonu.
```

Po instalaci nastav env (přidej do ~/.zshrc):

```bash
export ANDROID_HOME="$HOME/Android/Sdk"
export JAVA_HOME="/usr/lib/jvm/java-17-openjdk"
export PATH="$PATH:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator"
```

CLI-only varianta bez Studia: `paru -S android-sdk android-sdk-platform-tools
android-emulator jdk17-openjdk` (z AUR; build delší).

---

## 2. Testování — na fyzickém telefonu (bez emulátoru)

Cílíme **pouze na `arm64-v8a`** (reálné telefony). Emulátor nepoužíváme, takže
APK neobsahuje x86_64 a v emulátoru by nenaběhl — to je záměr.

Telefon přes USB (zapnuté USB debugging v Developer options):

```bash
adb devices                            # ověř, že telefon vidíš
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n com.everetro.poc/.MainActivity
adb logcat -s python.stdout python.stderr
```

Bez kabelu: APK přenes do telefonu a nainstaluj ručně (povol "instalaci
z neznámých zdrojů" pro danou appku).

---

## 3. Build + instalace PoC

```bash
cd android-poc
# Gradle wrapper se vygeneruje sám přes Studio, nebo:
gradle wrapper        # pokud máš systémový gradle (paru -S gradle)

./gradlew assembleDebug                # první build stáhne Python + wheely (chvíli to trvá)
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n com.everetro.poc/.MainActivity
# report uvidíš v appce; nebo log:
adb logcat -s python.stdout python.stderr
```

První build je pomalý — Chaquopy stahuje CPython 3.12 pro Android a všechny
wheely (vč. pydantic-core). Když projde a appka zobrazí samé `OK`, je stack
ověřený a můžeme plánovat plný port.

---

## Co dál, když PoC projde

1. Místo `poc.py` se nabundluje skutečná `app/` (FastAPI), spustí se uvicorn
   na `127.0.0.1:8000` ve vlákně (jako pywebview na desktopu).
2. MainActivity místo TextView ukáže `WebView` mířící na `http://127.0.0.1:8000`.
3. OAuth: loopback redirect (`127.0.0.1`) funguje stejně jako na desktopu.
4. Data (`eve_cache.db`, SDE, tokeny) v app-private storage = u uživatele.
