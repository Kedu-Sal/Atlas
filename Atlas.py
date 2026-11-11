from datetime import datetime
import csv
import io
import re
import ssl
import smtplib
from email.mime.text import MIMEText
import streamlit as st
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# Correction pour l'erreur SSL sur Streamlit Cloud
ssl._create_default_https_context = ssl._create_unverified_context

# ==========================================
# CONSTANTES
# ==========================================
DOSSIER_DRIVE_ID = "1eI92fD9kOM1dlAO7DgdMkt7QzyMHw-58"
SCOPES = ["https://www.googleapis.com/auth/drive"]

NIVEAUX = ["SJP1", "SJP2", "SJP3", "SJP4", "SJP5"]
FILIERES = [
    "Génie Civil",
    "Génie Infotronique",
    "Génie Mécatronique",
    "Génie des Procédés",
    "Génie Minier",
]
TYPES_DOC = [
    "Cours",
    "TD (Travaux Dirigés)",
    "TP (Travaux Pratiques)",
    "Examen / CC",
]
TAILLE_MAX_MO = 25
MAX_UPLOADS_PAR_SESSION = 5
LONGUEUR_MAX_NOM_FICHIER = 80


# ==========================================
# FONCTIONS UTILITAIRES
# ==========================================
def _sanitize(nom: str) -> str:
    nom = nom.strip()
    nom = re.sub(r'[\\/:*?"<>|]', "-", nom)
    nom = re.sub(r"\s+", " ", nom)
    return nom[:200]


def _est_vrai_pdf(contenu: bytes) -> bool:
    return contenu.startswith(b"%PDF-")


def _extraire_annee(nom: str) -> str:
    match = re.search(r"(\d{4}-\d{4})", nom)
    return match.group(1) if match else ""


def _extraire_niveau(nom: str) -> str:
    match = re.search(r"\[(SJP\d)\]", nom)
    return match.group(1) if match else ""


def _extraire_type(nom: str) -> str:
    for t in TYPES_DOC:
        if t in nom:
            return t
    return ""


def _extraire_contributeur(nom: str) -> str:
    match = re.search(r"_PAR_([^_]+)_", nom)
    if match:
        return match.group(1).replace("_", " ")
    return ""


def _formater_taille(octets) -> str:
    if not octets:
        return "—"
    try:
        taille = int(octets)
    except (ValueError, TypeError):
        return "—"
    if taille < 1024:
        return f"{taille} o"
    if taille < 1024 * 1024:
        return f"{taille / 1024:.1f} Ko"
    return f"{taille / (1024 * 1024):.1f} Mo"


def _formater_date(iso_date: str) -> str:
    if not iso_date:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return iso_date[:10]


def _debloquer_telechargement():
    st.session_state["peut_telecharger"] = True


def _est_autorise() -> bool:
    return st.session_state.get("peut_telecharger", False)


def envoyer_email_signalement(nom_fichier, raison, details):
    """Envoie un email à l'admin quand un document est signalé."""
    expediteur = st.secrets.get("email_expediteur", "")
    mot_de_passe = st.secrets.get("email_mot_de_passe", "")
    
    if not expediteur or not mot_de_passe:
        return False, "Configuration email manquante dans les secrets."

    destinataire = expediteur  # L'admin se l'envoie à lui-même
    sujet = f"🚨 ALERTE ATLAS : Signalement de document"
    
    corps = f"""
    Un document a été signalé sur la plateforme Atlas.
    
    Détails du signalement :
    ------------------------
    Fichier concerné : {nom_fichier}
    Raison : {raison}
    Détails (optionnel) : {details}
    Date : {datetime.now().strftime("%d/%m/%Y à %H:%M")}
    
    Rendez-vous sur votre Google Drive pour vérifier ce document.
    """

    msg = MIMEText(corps, "plain", "utf-8")
    msg["Subject"] = sujet
    msg["From"] = expediteur
    msg["To"] = destinataire

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(expediteur, mot_de_passe)
            server.sendmail(expediteur, destinataire, msg.as_string())
        return True, "Email envoyé avec succès."
    except Exception as e:
        return False, str(e)


# ==========================================
# CONNEXION GOOGLE DRIVE (OAUTH)
# ==========================================
@st.cache_resource
def connecter_google_drive():
    try:
        o = st.secrets["oauth"]
        creds = Credentials(
            token=None,
            refresh_token=o["refresh_token"],
            client_id=o["client_id"],
            client_secret=o["client_secret"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except KeyError:
        st.error(
            "❌ Configuration manquante : la section `[oauth]` n'existe pas "
            "dans les secrets."
        )
        st.stop()
    except Exception as e:
        if "invalid_grant" in str(e).lower():
            st.error(
                "🔑 **Le token Google a expiré.** "
                "Le gestionnaire doit régénérer le refresh_token "
                "(voir l'onglet Maintenance)."
            )
            st.stop()
        st.error(f"❌ Impossible de se connecter à Google Drive : {e}")
        st.stop()


def fichier_existe(service, nom_fichier: str) -> bool:
    nom_echappe = nom_fichier.replace("'", "\\'")
    query = (
        f"'{DOSSIER_DRIVE_ID}' in parents and name = '{nom_echappe}' "
        "and trashed = false"
    )
    res = service.files().list(
        q=query,
        fields="files(id)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute()
    return bool(res.get("files"))


def uploader_vers_drive(
    uploaded_file, contributeur, niveau, filiere, matiere, chapitre,
    annee, type_doc
):
    contenu = uploaded_file.getvalue()

    if not _est_vrai_pdf(contenu):
        raise ValueError(
            "Le fichier n'est pas un PDF valide (signature manquante)."
        )

    taille_mo = len(contenu) / (1024 * 1024)
    if taille_mo > TAILLE_MAX_MO:
        raise ValueError(
            f"Le fichier dépasse la limite de {TAILLE_MAX_MO} Mo "
            f"({taille_mo:.1f} Mo)."
        )

    service = connecter_google_drive()
    part_filiere = f"_{filiere}" if filiere else ""
    part_chapitre = f"_{chapitre}" if chapitre.strip() else ""
    contributeur_clean = contributeur.strip().replace(" ", "_")

    nom_original = uploaded_file.name[:LONGUEUR_MAX_NOM_FICHIER]

    nom_fichier = _sanitize(
        f"[{niveau}]{part_filiere}_{matiere}{part_chapitre}_{annee}_"
        f"{type_doc}_PAR_{contributeur_clean}_{nom_original}"
    )

    if fichier_existe(service, nom_fichier):
        raise ValueError(f"Le fichier « {nom_fichier} » existe déjà.")

    metadata = {"name": nom_fichier, "parents": [DOSSIER_DRIVE_ID]}
    media = MediaIoBaseUpload(
        io.BytesIO(contenu),
        mimetype="application/pdf",
        resumable=True,
    )

    file = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()

    return file.get("webViewLink"), nom_fichier


@st.cache_data(ttl=60, show_spinner=False)
def lister_fichiers_drive():
    service = connecter_google_drive()
    query = f"'{DOSSIER_DRIVE_ID}' in parents and trashed = false"
    fichiers = []
    page_token = None
    while True:
        resultat = service.files().list(
            q=query,
            pageSize=100,
            pageToken=page_token,
            fields=(
                "nextPageToken, files(id, name, webViewLink, createdTime, "
                "size, mimeType)"
            ),
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
            orderBy="createdTime desc",
        ).execute()
        fichiers.extend(resultat.get("files", []))
        page_token = resultat.get("nextPageToken")
        if not page_token:
            break
    return fichiers


def telecharger_contenu(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    return request.execute()


def generer_csv_documents(fichiers) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Nom du fichier", "Niveau", "Année", "Type",
        "Contributeur", "Taille", "Date de création", "Lien Drive"
    ])
    for f in fichiers:
        writer.writerow([
            f["name"],
            _extraire_niveau(f["name"]),
            _extraire_annee(f["name"]),
            _extraire_type(f["name"]),
            _extraire_contributeur(f["name"]),
            _formater_taille(f.get("size")),
            _formater_date(f.get("createdTime", "")),
            f.get("webViewLink", ""),
        ])
    return output.getvalue().encode("utf-8-sig")


# ==========================================
# CONFIGURATION DE LA PAGE
# ==========================================
st.set_page_config(
    page_title="Atlas - Bibliothèque Numérique SJP",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "peut_telecharger" not in st.session_state:
    st.session_state["peut_telecharger"] = False
if "uploads_session" not in st.session_state:
    st.session_state["uploads_session"] = 0
if "mode_admin" not in st.session_state:
    st.session_state["mode_admin"] = False

st.markdown(
    """
    <style>
    .main-title {
        text-align: center;
        padding: 1rem 0;
        background: linear-gradient(90deg, #1e3a8a 0%, #3b82f6 100%);
        color: white;
        border-radius: 12px;
        margin-bottom: 1.5rem;
    }
    .main-title h1 { margin: 0; font-size: 2rem; }
    .main-title p { margin: 0.3rem 0 0 0; opacity: 0.9; font-size: 0.95rem; }
    .stat-card {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 1rem;
        text-align: center;
    }
    .stat-card .value {
        font-size: 1.8rem;
        font-weight: bold;
        color: #1e3a8a;
    }
    .stat-card .label { color: #64748b; font-size: 0.85rem; }
    .stButton > button[kind="primary"] {
        background: linear-gradient(90deg, #1e3a8a, #3b82f6);
        border: none;
    }
    .footer {
        text-align: center;
        color: #94a3b8;
        font-size: 0.85rem;
        padding-top: 2rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ==========================================
# BARRE LATÉRALE
# ==========================================
with st.sidebar:
    st.markdown("### 📚 Atlas")
    st.caption("Bibliothèque Numérique SJP")
    st.markdown("---")

    options_menu = [
        "📁 Consulter les documents",
        "📤 Partager un document",
        "🏆 Contributeurs",
    ]
    
    if st.session_state["mode_admin"]:
        options_menu.append("ℹ️ Maintenance")

    menu = st.radio(
        "Navigation :",
        options_menu,
        label_visibility="collapsed",
    )

    st.markdown("---")

    if _est_autorise():
        st.success("✅ Téléchargements débloqués")
    else:
        st.warning("🔒 Téléchargements verrouillés")

    st.markdown("---")

    try:
        fichiers_sidebar = lister_fichiers_drive()
        nb_total = len(fichiers_sidebar)
        nb_par_niveau = {}
        for f in fichiers_sidebar:
            niv = _extraire_niveau(f["name"])
            if niv:
                nb_par_niveau[niv] = nb_par_niveau.get(niv, 0) + 1

        st.markdown("### 📊 Statistiques")
        st.metric("Documents disponibles", nb_total)
        if nb_par_niveau:
            for niv in NIVEAUX:
                if niv in nb_par_niveau:
                    st.caption(f"• {niv} : {nb_par_niveau[niv]} doc(s)")
    except Exception:
        st.caption("Statistiques indisponibles")

    st.markdown("---")
    st.info(
        "Plateforme académique dédiée aux étudiants de "
        "**Saint Jérôme Polytechnique (SJP)**."
    )

# ==========================================
# EN-TÊTE PRINCIPAL
# ==========================================
st.markdown(
    """
    <div class='main-title'>
        <h1>📚 Atlas — Bibliothèque Numérique</h1>
        <p>Saint Jérôme Polytechnique (SJP)</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ==========================================
# PAGE 1 : PARTAGER UN DOCUMENT
# ==========================================
if menu == "📤 Partager un document":
    st.subheader("📤 Partager un nouveau document")
    st.caption(
        "Remplissez vos informations et celles du document. "
        "**Partager un document débloque les téléchargements.**"
    )

    if st.session_state["uploads_session"] >= MAX_UPLOADS_PAR_SESSION:
        st.warning(
            f"⚠️ Vous avez atteint la limite de "
            f"{MAX_UPLOADS_PAR_SESSION} partages par session. "
            "Merci pour votre contribution !"
        )
    else:
        st.caption(
            f"📊 Partages cette session : "
            f"{st.session_state['uploads_session']}"
            f"/{MAX_UPLOADS_PAR_SESSION}"
        )

        with st.container(border=True):
            st.markdown("##### 👤 Vos informations")
            col_id1, col_id2 = st.columns(2)
            with col_id1:
                contributeur = st.text_input(
                    "Nom et Prénom *",
                    placeholder="ex: HASSANA Oumar",
                )
            with col_id2:
                niveau_contributeur = st.selectbox(
                    "Niveau *", NIVEAUX, key="niveau_contributeur"
                )

        with st.container(border=True):
            st.markdown("##### 🎓 Informations du document")
            col1, col2 = st.columns(2)
            with col1:
                niveau = st.selectbox(
                    "Niveau du document *", NIVEAUX, key="niveau_doc"
                )
            with col2:
                filiere = ""
                if niveau in ["SJP3", "SJP4", "SJP5"]:
                    filiere = st.selectbox("Filière *", FILIERES)
                else:
                    st.text_input(
                        "Filière", value="— Non requise —", disabled=True
                    )

            col3, col4 = st.columns(2)
            with col3:
                matiere = st.text_input(
                    "Matière *",
                    placeholder="ex: Béton Armé, RDM, Algorithmique",
                )
            with col4:
                annee = st.text_input(
                    "Année académique *", placeholder="ex: 2025-2026"
                )

            col5, col6 = st.columns(2)
            with col5:
                chapitre = st.text_input(
                    "Chapitre / Partie (optionnel)",
                    placeholder="ex: Chapitre 1, Module 2",
                )
            with col6:
                type_doc = st.selectbox("Type de document *", TYPES_DOC)

        with st.container(border=True):
            st.markdown("##### 📎 Fichiers PDF")
            uploaded_files = st.file_uploader(
                "Sélectionnez un ou plusieurs fichiers PDF",
                type=["pdf"],
                accept_multiple_files=True,
                label_visibility="collapsed",
            )

            if uploaded_files:
                st.markdown(
                    f"**{len(uploaded_files)} fichier(s) sélectionné(s) :**"
                )
                for f in uploaded_files:
                    taille_mo = len(f.getvalue()) / (1024 * 1024)
                    emoji = "⚠️" if taille_mo > TAILLE_MAX_MO else "✅"
                    st.caption(f"{emoji} {f.name} — {taille_mo:.2f} Mo")

        submit_button = st.button(
            "🚀 Envoyer les documents",
            type="primary",
            use_container_width=True,
        )

        if submit_button:
            erreurs_validation = []
            if not contributeur.strip():
                erreurs_validation.append("votre nom et prénom")
            if not uploaded_files:
                erreurs_validation.append("au moins un fichier PDF")
            if not matiere.strip():
                erreurs_validation.append("la matière")
            if not annee.strip():
                erreurs_validation.append("l'année académique")

            if erreurs_validation:
                st.warning(
                    "⚠️ Veuillez renseigner : "
                    + ", ".join(erreurs_validation)
                    + "."
                )
            else:
                succes, erreurs = [], []
                progress = st.progress(0, text="Téléversement en cours...")

                for i, uploaded_file in enumerate(uploaded_files):
                    progress.progress(
                        (i + 1) / len(uploaded_files),
                        text=f"Traitement de {uploaded_file.name}...",
                    )
                    try:
                        lien, nom_final = uploader_vers_drive(
                            uploaded_file,
                            contributeur.strip(),
                            niveau,
                            filiere,
                            matiere.strip(),
                            chapitre,
                            annee.strip(),
                            type_doc,
                        )
                        succes.append((nom_final, lien))
                    except Exception as e:
                        erreurs.append((uploaded_file.name, str(e)))

                progress.empty()
                lister_fichiers_drive.clear()

                if succes:
                    _debloquer_telechargement()
                    st.session_state["uploads_session"] += len(succes)

                if succes:
                    premier_mot = (
                        contributeur.strip().split()[0].capitalize()
                    )
                    st.success(
                        f"✅ Merci {premier_mot} ! "
                        f"{len(succes)} fichier(s) uploadé(s) avec succès."
                    )
                    if _est_autorise():
                        st.balloons()
                        st.info(
                            "🎉 **Téléchargements débloqués !** Vous "
                            "pouvez maintenant télécharger tous les "
                            "documents."
                        )
                    st.markdown("### 📎 Fichiers envoyés")
                    for nom_f, lien_f in succes:
                        st.markdown(f"- 📄 [{nom_f}]({lien_f})")

                if erreurs:
                    st.error(f"❌ {len(erreurs)} fichier(s) en échec :")
                    for nom_f, msg in erreurs:
                        st.markdown(f"- **{nom_f}** : {msg}")

# ==========================================
# PAGE 2 : CONSULTER LES DOCUMENTS
# ==========================================
elif menu == "📁 Consulter les documents":
    st.subheader("📁 Consulter les documents")

    if not _est_autorise():
        st.markdown(
            """
            <div style="
                background: linear-gradient(90deg, #fef3c7, #fde68a);
                border-left: 5px solid #f59e0b;
                padding: 1rem 1.2rem;
                border-radius: 10px;
                margin-bottom: 1rem;
            ">
                <h4 style="margin:0; color:#92400e;">🔒 Accès verrouillé</h4>
                <p style="margin:0.4rem 0 0 0; color:#78350f;">
                    Pour télécharger un document, vous devez d'abord
                    <b>partager au moins un document</b>.<br>
                    👉 Allez dans l'onglet <b>📤 Partager un document</b>.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div style="
                background: linear-gradient(90deg, #d1fae5, #a7f3d0);
                border-left: 5px solid #10b981;
                padding: 0.8rem 1.2rem;
                border-radius: 10px;
                margin-bottom: 1rem;
            ">
                <b style="color:#065f46;">✅ Téléchargements débloqués</b>
                <span style="color:#047857;"> — merci pour votre
                contribution !</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    try:
        fichiers = lister_fichiers_drive()
    except Exception as e:
        st.error(f"Erreur lors de la récupération des fichiers : {e}")
        st.stop()

    if not fichiers:
        st.info(
            "📭 Aucun document disponible pour le moment. "
            "Soyez le premier à partager un document !"
        )
        st.stop()

    stats_niveau = {n: 0 for n in NIVEAUX}
    stats_type = {t: 0 for t in TYPES_DOC}
    for f in fichiers:
        niv = _extraire_niveau(f["name"])
        if niv in stats_niveau:
            stats_niveau[niv] += 1
        typ = _extraire_type(f["name"])
        if typ in stats_type:
            stats_type[typ] += 1

    st.markdown("##### 📊 Vue d'ensemble")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(
            f"<div class='stat-card'><div class='value'>{len(fichiers)}</div>"
            f"<div class='label'>Documents</div></div>",
            unsafe_allow_html=True,
        )
    with c2:
        nb_matieres = len({
            re.sub(r"^\[[^\]]+\]_?", "", f["name"]).split("_")[0]
            for f in fichiers
        })
        st.markdown(
            f"<div class='stat-card'><div class='value'>{nb_matieres}</div>"
            f"<div class='label'>Matières</div></div>",
            unsafe_allow_html=True,
        )
    with c3:
        nb_niveaux = sum(1 for v in stats_niveau.values() if v > 0)
        st.markdown(
            f"<div class='stat-card'><div class='value'>{nb_niveaux}</div>"
            f"<div class='label'>Niveaux actifs</div></div>",
            unsafe_allow_html=True,
        )
    with c4:
        nb_types = sum(1 for v in stats_type.values() if v > 0)
        st.markdown(
            f"<div class='stat-card'><div class='value'>{nb_types}</div>"
            f"<div class='label'>Types de doc</div></div>",
            unsafe_allow_html=True,
        )

    st.download_button(
        "📥 Exporter la liste en CSV",
        data=generer_csv_documents(fichiers),
        file_name=f"atlas_documents_{datetime.now():%Y%m%d}.csv",
        mime="text/csv",
        use_container_width=False,
    )

    st.markdown("---")

    # Filtres
    with st.container(border=True):
        st.markdown("##### 🔍 Filtres de recherche")
        col_f1, col_f2, col_f3, col_f4 = st.columns([2, 2, 2, 2])
        with col_f1:
            choix_niveau = st.selectbox(
                "Niveau", ["Tous"] + NIVEAUX, key="f_niveau"
            )
        with col_f2:
            choix_filiere = st.selectbox(
                "Filière", ["Toutes"] + FILIERES, key="f_filiere"
            )
        with col_f3:
            choix_type = st.selectbox(
                "Type", ["Tous"] + TYPES_DOC, key="f_type"
            )
        with col_f4:
            choix_tri = st.selectbox(
                "Trier par",
                ["Plus récents", "Plus anciens", "Nom (A-Z)", "Nom (Z-A)"],
                key="f_tri",
            )

        # 👇 PORTE DÉROBÉE ADMIN 👇
        mot_de_passe_attendu = st.secrets.get("admin_password", "")
        
        col_search, col_btn = st.columns([4, 1])
        with col_search:
            recherche = st.text_input(
                "🔎 Recherche libre (matière, chapitre, contributeur...)",
                placeholder="ex: Analyse, Béton, HASSANA...",
                key="f_recherche",
            )
        with col_btn:
            st.write("") 
            bouton_secret = st.button("🔓", help="Déverrouiller l'espace admin")

        if recherche == mot_de_passe_attendu and mot_de_passe_attendu != "":
            if not st.session_state["mode_admin"]:
                st.session_state["mode_admin"] = True
                st.success("🔓 Mode Administrateur activé ! L'onglet Maintenance est apparu.")
                st.rerun()

        if recherche == mot_de_passe_attendu and mot_de_passe_attendu != "":
            recherche = ""

    fichiers_filtres = []
    for f in fichiers:
        nom = f["name"]

        if choix_niveau != "Tous" and f"[{choix_niveau}]" not in nom:
            continue
        if choix_filiere != "Toutes" and choix_filiere not in nom:
            continue
        if choix_type != "Tous" and choix_type not in nom:
            continue
        if recherche.strip() and recherche.lower() not in nom.lower():
            continue

        fichiers_filtres.append(f)

    if choix_tri == "Plus récents":
        fichiers_filtres.sort(
            key=lambda x: x.get("createdTime", ""), reverse=True
        )
    elif choix_tri == "Plus anciens":
        fichiers_filtres.sort(key=lambda x: x.get("createdTime", ""))
    elif choix_tri == "Nom (A-Z)":
        fichiers_filtres.sort(key=lambda x: x["name"].lower())
    elif choix_tri == "Nom (Z-A)":
        fichiers_filtres.sort(
            key=lambda x: x["name"].lower(), reverse=True
        )

    st.markdown("---")

    if not fichiers_filtres:
        st.warning(
            "🔍 Aucun document ne correspond à vos critères de recherche."
        )
    else:
        st.markdown(
            f"#### 📄 {len(fichiers_filtres)} document(s) trouvé(s)"
        )

        service = connecter_google_drive()

        for idx, f in enumerate(fichiers_filtres):
            with st.container(border=True):
                col_info, col_action = st.columns([4, 1])

                with col_info:
                    st.markdown(f"**📄 {f['name']}**")

                    meta_parts = []
                    niv = _extraire_niveau(f["name"])
                    if niv:
                        meta_parts.append(f"🎓 {niv}")
                    annee_doc = _extraire_annee(f["name"])
                    if annee_doc:
                        meta_parts.append(f"🗓️ {annee_doc}")
                    type_doc_trouve = _extraire_type(f["name"])
                    if type_doc_trouve:
                        meta_parts.append(f"🏷️ {type_doc_trouve}")
                    contributeur = _extraire_contributeur(f["name"])
                    if contributeur:
                        meta_parts.append(f"👤 {contributeur}")
                    meta_parts.append(
                        f"💾 {_formater_taille(f.get('size'))}"
                    )
                    meta_parts.append(
                        f"📅 {_formater_date(f.get('createdTime', ''))}"
                    )
                    st.caption("  ·  ".join(meta_parts))

                with col_action:
                    # Création de 3 petites colonnes pour aligner les icônes
                    c_dl, c_drive, c_sign = st.columns(3)

                    with c_dl:
                        if _est_autorise():
                            try:
                                contenu = telecharger_contenu(service, f["id"])
                                file_name = (
                                    f["name"]
                                    if f["name"].endswith(".pdf")
                                    else f["name"] + ".pdf"
                                )
                                st.download_button(
                                    label="⬇️",
                                    help="Télécharger le document",
                                    data=contenu,
                                    file_name=file_name,
                                    mime="application/pdf",
                                    key=f"dl_{f['id']}_{idx}",
                                    use_container_width=True,
                                )
                            except Exception:
                                st.caption("⚠️")
                        else:
                            st.button(
                                "🔒",
                                disabled=True,
                                key=f"lock_{f['id']}_{idx}",
                                use_container_width=True,
                                help="Partagez un document pour débloquer",
                            )

                    with c_drive:
                        if _est_autorise() and f.get("webViewLink"):
                            st.link_button(
                                "🔗", 
                                f['webViewLink'], 
                                use_container_width=True, 
                                help="Ouvrir sur Google Drive"
                            )

                    with c_sign:
                        with st.popover("⚠️", use_container_width=True):
                            st.caption("**Signaler ce document**")
                            raison = st.selectbox(
                                "Raison",
                                [
                                    "Fichier vide / corrompu",
                                    "Hors sujet",
                                    "Contenu inapproprié",
                                    "Doublon",
                                    "Autre",
                                ],
                                key=f"raison_{f['id']}_{idx}",
                            )
                            details = st.text_area(
                                "Détails (optionnel)",
                                key=f"details_{f['id']}_{idx}",
                            )
                            if st.button(
                                "Envoyer le signalement",
                                key=f"signal_{f['id']}_{idx}",
                            ):
                                with st.spinner("Envoi en cours..."):
                                    succes_email, msg = envoyer_email_signalement(f['name'], raison, details)
                                    if succes_email:
                                        st.success("✅ Signalement envoyé à l'administrateur. Merci !")
                                    else:
                                        st.error(f"Erreur : {msg}")

# ==========================================
# PAGE 3 : CONTRIBUTEURS
# ==========================================
elif menu == "🏆 Contributeurs":
    st.subheader("🏆 Contributeurs de la bibliothèque")
    st.caption(
        "Merci à tous les étudiants qui enrichissent cette bibliothèque !"
    )

    try:
        fichiers = lister_fichiers_drive()
    except Exception as e:
        st.error(f"Erreur lors de la récupération : {e}")
        st.stop()

    if not fichiers:
        st.info("Aucun contributeur pour le moment.")
        st.stop()

    contributions = {}
    for f in fichiers:
        nom = _extraire_contributeur(f["name"])
        if nom:
            contributions[nom] = contributions.get(nom, 0) + 1

    if not contributions:
        st.info("Aucun contributeur identifié pour le moment.")
        st.stop()

    classement = sorted(
        contributions.items(), key=lambda x: x[1], reverse=True
    )

    st.markdown("### 🥇 Classement des contributeurs")
    for rang, (nom, count) in enumerate(classement, start=1):
        if rang == 1:
            medaille = "🥇"
        elif rang == 2:
            medaille = "🥈"
        elif rang == 3:
            medaille = "🥉"
        else:
            medaille = f"#{rang}"

        with st.container(border=True):
            col1, col2 = st.columns([1, 4])
            with col1:
                st.markdown(f"### {medaille}")
            with col2:
                st.markdown(f"**{nom}**")
                st.caption(f"📄 {count} document(s) partagé(s)")

    st.markdown("---")

    c1, c2 = st.columns(2)
    with c1:
        st.metric("Total contributeurs", len(contributions))
    with c2:
        st.metric(
            "Total documents partagés", sum(contributions.values())
        )

# ==========================================
# PAGE 4 : MAINTENANCE (ADMIN UNIQUEMENT)
# ==========================================
elif menu == "ℹ️ Maintenance":
    st.subheader("ℹ️ Maintenance & informations")
    st.caption("Notes privées pour le gestionnaire de la plateforme.")

    st.markdown("### 🔑 Régénération du `refresh_token` OAuth")
    st.markdown(
        """
        Si l'application affiche une erreur `invalid_grant`, c'est que le 
        `refresh_token` a expiré.

        **Procédure :**
        1. Aller sur [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/)
        2. ⚙️ → *Use your own OAuth credentials*
        3. Coller Client ID et Client Secret
        4. Scope : `https://www.googleapis.com/auth/drive`
        5. Authorize → Copier le nouveau Refresh token
        6. Mettre à jour les secrets Streamlit Cloud.
        """
    )

    st.markdown("---")
    st.markdown("### 🗂️ Dossier Drive")
    st.code(f"ID : {DOSSIER_DRIVE_ID}")

    st.markdown("---")
    st.markdown("### 📊 Statistiques techniques")
    try:
        fichiers = lister_fichiers_drive()
        total_octets = sum(int(f.get("size", 0) or 0) for f in fichiers)
        st.metric("Nombre de fichiers", len(fichiers))
        st.metric("Taille totale", _formater_taille(total_octets))
    except Exception as e:
        st.error(f"Impossible de récupérer les statistiques : {e}")

    st.markdown("---")
    if st.button("Se déconnecter du mode Admin"):
        st.session_state["mode_admin"] = False
        st.rerun()

# ==========================================
# PIED DE PAGE
# ==========================================
st.markdown("---")
st.markdown(
    "<div class='footer'>"
    "Développé par <b>ØH</b> le <b>05/11/2026</b>"
    "</div>",
    unsafe_allow_html=True,
)