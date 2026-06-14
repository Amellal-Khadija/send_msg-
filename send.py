import streamlit as st
import pandas as pd
import subprocess
import socket
import time
import os
import urllib.parse

try:
    from pyngrok import ngrok as _ngrok
    NGROK_OK = True
except ImportError:
    NGROK_OK = False

SENT_FILE   = "sent_numbers.txt"
DEBUG_PORT  = 9222

# Profil Chrome principal — WhatsApp Web y est déjà connecté
PROFILE_DIR = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.common.keys import Keys
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_sent():
    if not os.path.exists(SENT_FILE):
        return set()
    with open(SENT_FILE) as f:
        return set(l.strip() for l in f if l.strip())

def mark_sent(phone):
    with open(SENT_FILE, "a") as f:
        f.write(phone + "\n")

def format_phone(raw, code):
    p = str(raw).strip()
    if p.lower() in ("nan", "", "none"):
        return None
    p = p.split(".")[0].replace(" ", "").replace("-", "").lstrip("+")
    code_d = code.replace("+", "").replace(" ", "")
    if p.startswith("0"):
        p = p[1:]
    if not p.startswith(code_d):
        p = code_d + p
    return p

def debug_port_open():
    """Vérifie si Chrome écoute sur le port debug."""
    try:
        with socket.create_connection(("localhost", DEBUG_PORT), timeout=1):
            return True
    except Exception:
        return False

def clear_service_workers():
    """Supprime le cache Service Worker de WhatsApp pour forcer un rechargement propre."""
    import shutil
    sw_path = os.path.join(PROFILE_DIR, "Default", "Service Worker")
    if os.path.exists(sw_path):
        shutil.rmtree(sw_path, ignore_errors=True)


def open_whatsapp_chrome():
    """Ferme Chrome, le rouvre avec remote debugging sur le profil principal."""
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if not chrome:
        raise FileNotFoundError("Chrome introuvable dans Program Files.")

    # Ferme Chrome pour libérer le verrou du profil principal
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    time.sleep(1)

    # Nettoie les lock files du profil principal
    for fname in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try:
            os.remove(os.path.join(PROFILE_DIR, fname))
        except Exception:
            pass

    subprocess.Popen([
        chrome,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "https://web.whatsapp.com",
    ])
    for _ in range(15):
        if debug_port_open():
            return
        time.sleep(1)
    raise TimeoutError("Chrome n'a pas démarré dans les temps.")

def connect_selenium():
    """Attache Selenium à Chrome déjà ouvert sur le port debug."""
    options = Options()
    options.add_experimental_option("debuggerAddress", f"localhost:{DEBUG_PORT}")
    options.page_load_strategy = "none"   # drv.get() retourne immédiatement
    try:
        drv = webdriver.Chrome(options=options)
    except Exception:
        from webdriver_manager.chrome import ChromeDriverManager
        from selenium.webdriver.chrome.service import Service
        svc = Service(ChromeDriverManager().install())
        drv = webdriver.Chrome(service=svc, options=options)
    return drv

def driver_alive(drv):
    try:
        _ = drv.current_url
        return True
    except Exception:
        return False

def whatsapp_ready(drv):
    """Retourne True si WhatsApp Web est connecté (chats visibles)."""
    SELECTORS = [
        "#side",
        '[data-testid="chat-list"]',
        '[aria-label="Chat list"]',
        '[data-testid="chatlist-header"]',
        'div[role="grid"]',
    ]
    for sel in SELECTORS:
        try:
            drv.find_element(By.CSS_SELECTOR, sel)
            return True
        except Exception:
            pass
    return False

def send_one(drv, phone_plus, msg, timeout=25):
    """Envoie un message. Réessaie une fois si échec. Retourne (ok, erreur)."""
    def _try(drv, phone_plus, msg, timeout):
        url = f"https://web.whatsapp.com/send?phone={phone_plus}&text={urllib.parse.quote(msg)}"
        drv.get(url)   # retourne immédiatement grâce à page_load_strategy=none

        def box_ready(d):
            for el in d.find_elements(By.CSS_SELECTOR, 'div[contenteditable="true"]'):
                try:
                    if el.get_attribute("textContent").strip():
                        return el
                except Exception:
                    pass
            return False

        try:
            box = WebDriverWait(drv, timeout).until(box_ready)
        except Exception:
            try:
                drv.save_screenshot(f"err_{phone_plus.replace('+','')}.png")
            except Exception:
                pass
            return False, "Chat non chargé — numéro sans WhatsApp ou connexion lente"

        drv.execute_script("arguments[0].click();", box)

        # Essaie le bouton envoyer, sinon simule Enter via JS (évite les erreurs stale element)
        sent = False
        try:
            btn = drv.find_element(By.CSS_SELECTOR, 'span[data-icon="send"]')
            drv.execute_script("arguments[0].click();", btn)
            sent = True
        except Exception:
            pass

        if not sent:
            drv.execute_script(
                "arguments[0].dispatchEvent("
                "new KeyboardEvent('keydown',{key:'Enter',keyCode:13,which:13,bubbles:true}))",
                box
            )

        time.sleep(1)
        return True, ""

    ok, err = _try(drv, phone_plus, msg, timeout)
    if not ok:
        time.sleep(2)
        ok, err = _try(drv, phone_plus, msg, timeout)
    return ok, err


# ── Interface ─────────────────────────────────────────────────────────────────

st.set_page_config(page_title="WhatsApp Bulk Sender", layout="centered")
st.title("📤 WhatsApp Bulk Sender")

# ── Lien public ngrok ─────────────────────────────────────────────────────────
if NGROK_OK:
    if "ngrok_url" not in st.session_state:
        try:
            tunnel = _ngrok.connect(8502)
            st.session_state.ngrok_url = tunnel.public_url
        except Exception:
            st.session_state.ngrok_url = None
    if st.session_state.ngrok_url:
        st.success(f"🌐 Lien public : **{st.session_state.ngrok_url}**  \n"
                   f"Partage ce lien pour accéder à l'app depuis n'importe où.")

if not SELENIUM_OK:
    st.error("❌ Installe selenium : `pip install selenium`")
    st.stop()

if "drv" not in st.session_state:
    st.session_state.drv = None

# ── Étape 1 ───────────────────────────────────────────────────────────────────
st.subheader("1️⃣ WhatsApp Web")

connected = bool(st.session_state.drv and driver_alive(st.session_state.drv)
                 and whatsapp_ready(st.session_state.drv))

if connected:
    st.success("✅ WhatsApp Web détecté et prêt — tu peux envoyer !")
else:
    st.warning("⚠️ WhatsApp Web non détecté.")

c1, c2, c3, c4 = st.columns(4)

with c1:
    if st.button("🌐 Ouvrir WhatsApp", use_container_width=True):
        if debug_port_open():
            st.info("Chrome WhatsApp déjà ouvert.")
        else:
            try:
                open_whatsapp_chrome()
                st.success("Chrome ouvert — scanne le QR si nécessaire.")
            except Exception as e:
                st.error(f"Erreur : {e}")

with c4:
    if st.button("🔧 Réparer", use_container_width=True,
                  help="WhatsApp reste en chargement ? Clique ici pour vider le cache"):
        if st.session_state.drv:
            try:
                st.session_state.drv.quit()
            except Exception:
                pass
            st.session_state.drv = None
        clear_service_workers()
        try:
            open_whatsapp_chrome()
            st.success("Cache nettoyé — Chrome relancé. Connecte-toi à WhatsApp.")
        except Exception as e:
            st.error(f"Erreur : {e}")

with c2:
    if st.button("🔗 Connecter", use_container_width=True):
        if not debug_port_open():
            st.error("Chrome pas encore ouvert. Clique d'abord sur **Ouvrir WhatsApp**.")
        else:
            try:
                if st.session_state.drv and driver_alive(st.session_state.drv):
                    try:
                        st.session_state.drv.quit()
                    except Exception:
                        pass
                st.session_state.drv = connect_selenium()
                st.rerun()
            except Exception as e:
                st.error(f"Impossible de connecter : {e}")

with c3:
    if st.button("❌ Déconnecter", use_container_width=True):
        if st.session_state.drv:
            try:
                st.session_state.drv.quit()
            except Exception:
                pass
            st.session_state.drv = None
        st.rerun()

if st.session_state.drv and driver_alive(st.session_state.drv) and not whatsapp_ready(st.session_state.drv):
    st.warning("⏳ Scanne le QR code dans Chrome, puis clique ici →")
    if st.button("🔄 J'ai scanné — Vérifier"):
        st.rerun()

st.markdown("---")

# ── Étape 2 ───────────────────────────────────────────────────────────────────
st.subheader("2️⃣ Configuration")

country_code  = st.text_input("🌍 Indicatif pays", "+212",
                               help="+212 Maroc | +33 France | +1 USA")
uploaded_file = st.file_uploader("📁 Fichier Excel", type=["xlsx"])
message       = st.text_area("✉️ Message", "Bonjour {name} 👋")
delay         = st.slider("⏱ Délai entre messages (secondes)", 1, 30, 5)

st.markdown("---")

# ── Étape 3 ───────────────────────────────────────────────────────────────────
st.subheader("3️⃣ Envoi")

if not uploaded_file:
    st.info("Uploade un fichier Excel pour continuer.")
else:
    df = pd.read_excel(uploaded_file)
    st.dataframe(df)

    col_map   = {c.lower().strip(): c for c in df.columns}
    aliases   = ["phone","tele","numero","numéro","tel","telephone",
                 "téléphone","mobile","gsm","whatsapp"]
    phone_col = next((col_map[a] for a in aliases if a in col_map), None)
    name_col  = col_map.get("nom") or col_map.get("name")

    if not phone_col:
        st.error(f"❌ Colonne téléphone introuvable. Colonnes : {', '.join(df.columns)}")
    else:
        st.success(f"✅ Colonne téléphone : **{phone_col}**")

        with st.expander("🔍 Vérifier les numéros"):
            for _, row in df.head(8).iterrows():
                fmt = format_phone(row[phone_col], country_code)
                st.write(f"`{row[phone_col]}` → {'**+'+fmt+'**' if fmt else '❌ invalide'}")

        sent_set = load_sent()
        already  = sum(1 for _, r in df.iterrows()
                       if format_phone(r[phone_col], country_code) in sent_set)
        if already:
            st.warning(f"⚠️ {already} numéro(s) déjà envoyés — ignorés.")

        c1, c2 = st.columns(2)
        with c1:
            send_btn = st.button("🚀 Envoyer à TOUS", type="primary",
                                  disabled=not connected)
        with c2:
            if st.button("🗑️ Réinitialiser historique"):
                if os.path.exists(SENT_FILE):
                    os.remove(SENT_FILE)
                st.success("Historique effacé.")
                st.rerun()

        if not connected:
            st.warning("⚠️ Connecte WhatsApp Web (Étape 1) avant d'envoyer.")

        if send_btn:
            drv = st.session_state.drv
            pending = []
            for _, row in df.iterrows():
                phone = format_phone(row[phone_col], country_code)
                if phone and phone not in sent_set:
                    name     = str(row[name_col]) if name_col else ""
                    msg_text = message.replace("{name}", name)
                    pending.append((phone, msg_text))

            if not pending:
                st.success("✅ Tout le monde a déjà reçu le message.")
            else:
                total   = len(pending)
                prog    = st.progress(0)
                status  = st.empty()
                logs    = []
                log_box = st.empty()
                ok = fail = 0

                for i, (phone, msg_text) in enumerate(pending):
                    status.info(f"📨 Envoi **{i+1}/{total}** → +{phone}")
                    result, err = send_one(drv, f"+{phone}", msg_text)

                    if result:
                        mark_sent(phone)
                        ok += 1
                        logs.append(f"✅ +{phone}")
                    else:
                        fail += 1
                        logs.append(f"❌ +{phone} : {err}")

                    log_box.text("\n".join(logs))
                    prog.progress((i + 1) / total)
                    if i < total - 1:
                        time.sleep(delay)

                status.empty()
                prog.empty()
                st.balloons()
                st.success(f"✅ Terminé — Envoyés : {ok} / {total}")
                if fail:
                    st.error(f"❌ Échecs : {fail}")
