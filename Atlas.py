from datetime import datetime
import io
import re
import streamlit as st
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

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
    uploaded_file, matiere, niveau, filiere, chapitre, annee, type_doc
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

    nom_fichier = _sanitize(
        f"[{niveau}]{part_filiere}_{matiere}{part_chapitre}_{annee}_"
        f"{type_doc}_{uploaded_file.name}"
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


# ==========================================
# CONFIGURATION DE LA PAGE
# ==========================================
st.set_page_config(
    page_title="Atlas - Bibliothèque Numérique SJP",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialisation de l'état de session
if "peut_telecharger" not in st.session_state:
    st.session_state["peut_telecharger"] = False

# CSS personnalisé
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

    menu = st.radio(
        "Navigation :",
        ["📁 Consulter les documents", "📤 Partager un document"],
        label_visibility="collapsed",
    )

    st.markdown("---")

    # Statut de déblocage dans la sidebar
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
        "Remplissez les informations ci-dessous, puis sélectionnez un ou "
        "plusieurs fichiers PDF. **Partager un document débloque les "
        "téléchargements.**"
    )

    with st.container(border=True):
        st.markdown("##### 🎓 Informations académiques")
        col1, col2 = st.columns(2)
        with col1:
            niveau = st.selectbox("Niveau *", NIVEAUX)
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
                "Matière *", placeholder="ex: Béton Armé, RDM, Algorithmique"
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
        "🚀 Envoyer les documents", type="primary", use_container_width=True
    )

    if submit_button:
        if not uploaded_files:
            st.warning("⚠️ Sélectionnez au moins un fichier PDF.")
        elif not matiere.strip():
            st.warning("⚠️ Renseignez le nom de la matière.")
        elif not annee.strip():
            st.warning("⚠️ Renseignez l'année académique.")
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
                        matiere.strip(),
                        niveau,
                        filiere,
                        chapitre,
                        annee.strip(),
                        type_doc,
                    )
                    succes.append((nom_final, lien))
                except Exception as e:
                    erreurs.append((uploaded_file.name, str(e)))

            progress.empty()
            lister_fichiers_drive.clear()

            # 🔓 Débloquer les téléchargements si au moins 1 upload a réussi
            if succes:
                _debloquer_telechargement()

            if succes:
                st.success(
                    f"✅ {len(succes)} fichier(s) uploadé(s) avec succès !"
                )
                if _est_autorise():
                    st.balloons()
                    st.info(
                        "🎉 **Téléchargements débloqués !** Vous pouvez "
                        "maintenant télécharger tous les documents de la "
                        "bibliothèque."
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
else:
    st.subheader("📁 Consulter les documents")

    # 🔐 Bandeau d'état verrouillé / débloqué
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

    # Récupération des fichiers
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

    # Statistiques
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

        recherche = st.text_input(
            "🔎 Recherche libre (matière, chapitre, mot-clé...)",
            placeholder="ex: Analyse, Béton, Chapitre 2...",
            key="f_recherche",
        )

    # Application des filtres
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

    # Tri
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
                    meta_parts.append(
                        f"💾 {_formater_taille(f.get('size'))}"
                    )
                    meta_parts.append(
                        f"📅 {_formater_date(f.get('createdTime', ''))}"
                    )
                    st.caption("  ·  ".join(meta_parts))

                with col_action:
                    if _est_autorise():
                        try:
                            contenu = telecharger_contenu(service, f["id"])
                            file_name = (
                                f["name"]
                                if f["name"].endswith(".pdf")
                                else f["name"] + ".pdf"
                            )
                            st.download_button(
                                label="⬇️ Télécharger",
                                data=contenu,
                                file_name=file_name,
                                mime="application/pdf",
                                key=f"dl_{f['id']}_{idx}",
                                use_container_width=True,
                            )
                        except Exception:
                            st.caption("⚠️ Indisponible")
                    else:
                        st.button(
                            "🔒 Verrouillé",
                            disabled=True,
                            key=f"lock_{f['id']}_{idx}",
                            use_container_width=True,
                            help="Partagez un document pour débloquer",
                        )

                    if f.get("webViewLink"):
                        st.markdown(
                            f"[🔗 Drive]({f['webViewLink']})",
                            unsafe_allow_html=False,
                        )

# ==========================================
# PIED DE PAGE
# ==========================================
st.markdown("---")
st.markdown(
    "<div class='footer'>"
    "Développé par <b>ØH</b> &nbsp;|&nbsp; Date : <b>le 05/11/2026</b>"
    "</div>",
    unsafe_allow_html=True,
)