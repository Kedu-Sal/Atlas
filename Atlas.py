from datetime import datetime
import io
import re
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

DOSSIER_DRIVE_ID = "1eI92fD9kOM1dlAO7DgdMkt7QzyMHw-58"
SCOPES = ["https://www.googleapis.com/auth/drive"]


# ==========================================
# FONCTIONS UTILITAIRES
# ==========================================

def _sanitize(nom: str) -> str:
  """Retire les caractères interdits pour les noms de fichiers Drive."""
  nom = nom.strip()
  nom = re.sub(r'[\\/:*?"<>|]', "-", nom)
  nom = re.sub(r"\s+", " ", nom)
  return nom[:200]


def _est_vrai_pdf(contenu: bytes) -> bool:
  """Vérifie que le fichier commence bien par la signature PDF."""
  return contenu.startswith(b"%PDF-")


def _extraire_annee_depuis_nom(nom: str) -> str:
  """Extrait l'année académique (ex: 2025-2026) du nom du fichier."""
  match = re.search(r"(\d{4}-\d{4})", nom)
  return match.group(1) if match else ""


def _extraire_type_depuis_nom(nom: str) -> str:
  """Extrait le type de document du nom du fichier."""
  types = [
      "TD (Travaux Dirigés)",
      "TP (Travaux Pratiques)",
      "Examen / CC",
      "Cours",
  ]
  for t in types:
    if t in nom:
      return t
  return ""


def _formater_taille(octets) -> str:
  """Convertit une taille en octets en format lisible (Ko, Mo)."""
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


# ==========================================
# CONNEXION GOOGLE DRIVE (COMPTE DE SERVICE)
# ==========================================

@st.cache_resource
def connecter_google_drive():
  """Connexion via compte de service Google (recommandé pour le déploiement)."""
  try:
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)
  except KeyError:
    st.error(
        "❌ Configuration manquante : la section `[gcp_service_account]`"
        " n'existe pas dans les secrets. Consulte les instructions de"
        " déploiement."
    )
    st.stop()
  except Exception as e:
    st.error(f"❌ Impossible de se connecter à Google Drive : {e}")
    st.stop()


def fichier_existe(service, nom_fichier: str) -> bool:
  """Vérifie si un fichier du même nom existe déjà dans le dossier."""
  nom_echappe = nom_fichier.replace("'", "\\'")
  query = (
      f"'{DOSSIER_DRIVE_ID}' in parents and name = '{nom_echappe}' "
      "and trashed = false"
  )
  res = (
      service.files()
      .list(
          q=query,
          fields="files(id)",
          supportsAllDrives=True,
          includeItemsFromAllDrives=True,
          corpora="allDrives",
      )
      .execute()
  )
  return bool(res.get("files"))


def uploader_vers_drive(
    uploaded_file, matiere, niveau, filiere, chapitre, annee, type_doc
):
  """Envoie un fichier PDF sur Google Drive avec un nom structuré."""
  contenu = uploaded_file.getvalue()

  if not _est_vrai_pdf(contenu):
    raise ValueError(
        "Le fichier n'est pas un PDF valide (signature manquante)."
    )

  service = connecter_google_drive()
  part_filiere = f"_{filiere}" if filiere else ""
  part_chapitre = f"_{chapitre}" if chapitre.strip() else ""

  nom_fichier = _sanitize(
      f"[{niveau}]{part_filiere}_{matiere}{part_chapitre}_{annee}_"
      f"{type_doc}_{uploaded_file.name}"
  )

  if fichier_existe(service, nom_fichier):
    raise ValueError(
        f"Le fichier « {nom_fichier} » existe déjà sur Drive."
    )

  metadata = {"name": nom_fichier, "parents": [DOSSIER_DRIVE_ID]}
  media = MediaIoBaseUpload(
      io.BytesIO(contenu),
      mimetype="application/pdf",
      resumable=True,
  )

  file = (
      service.files()
      .create(
          body=metadata,
          media_body=media,
          fields="id, webViewLink",
          supportsAllDrives=True,
      )
      .execute()
  )
  return file.get("webViewLink"), nom_fichier


def lister_fichiers_drive():
  """Récupère la liste de tous les fichiers du dossier Google Drive."""
  service = connecter_google_drive()
  query = f"'{DOSSIER_DRIVE_ID}' in parents and trashed = false"
  fichiers = []
  page_token = None
  while True:
    resultat = (
        service.files()
        .list(
            q=query,
            pageSize=100,
            pageToken=page_token,
            fields=(
                "nextPageToken, files(id, name, webViewLink, createdTime,"
                " size)"
            ),
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
            orderBy="createdTime desc",
        )
        .execute()
    )
    fichiers.extend(resultat.get("files", []))
    page_token = resultat.get("nextPageToken")
    if not page_token:
      break
  return fichiers


def telecharger_contenu(service, file_id: str) -> bytes:
  """Télécharge le contenu brut d'un fichier Drive en mémoire."""
  request = service.files().get_media(fileId=file_id)
  return request.execute()


# ==========================================
# CONFIGURATION DE LA PAGE
# ==========================================
st.set_page_config(
    page_title="Atlas - Bibliothèque Numérique",
    page_icon="📚",
    layout="wide",
)

# ==========================================
# BARRE LATÉRALE
# ==========================================
st.sidebar.title("📚 Navigation")
st.sidebar.markdown("---")

menu = st.sidebar.radio(
    "Aller vers :",
    ["📁 Consulter les documents", "📤 Partager un document"],
)

st.sidebar.markdown("---")
st.sidebar.info(
    "Plateforme académique dédiée aux étudiants de Saint Jérôme"
    " Polytechnique (SJP)."
)

# ==========================================
# CONTENU PRINCIPAL
# ==========================================
st.title("📚 Atlas - Bibliothèque Numérique")

# ------------------------------------------
# PAGE 1 : PARTAGER UN DOCUMENT
# ------------------------------------------
if menu == "📤 Partager un document":
  st.subheader("Uploader de nouveaux documents académiques")

  col1, col2 = st.columns(2)
  with col1:
    niveau = st.selectbox(
        "Sélectionnez votre niveau",
        ["SJP1", "SJP2", "SJP3", "SJP4", "SJP5"],
    )
  with col2:
    filiere = ""
    if niveau in ["SJP3", "SJP4", "SJP5"]:
      filiere = st.selectbox(
          "Sélectionnez votre filière",
          [
              "Génie Civil",
              "Génie Infotronique",
              "Génie Mécatronique",
              "Génie des Procédés",
              "Génie Minier",
          ],
      )

  col3, col4 = st.columns(2)
  with col3:
    matiere = st.text_input(
        "Nom de la matière (ex: Béton Armé, RDM, Algorithmique)"
    )
  with col4:
    annee = st.text_input("Année académique (ex: 2025-2026)")

  chapitre = st.text_input(
      "Chapitre ou Partie (Optionnel, ex: Chapitre 1 ou Module 2)"
  )

  type_doc = st.selectbox(
      "Type de document",
      [
          "Cours",
          "TD (Travaux Dirigés)",
          "TP (Travaux Pratiques)",
          "Examen / CC",
      ],
  )

  uploaded_files = st.file_uploader(
      "Choisissez les fichiers PDF à téléverser (sélection multiple"
      " autorisée)",
      type=["pdf"],
      accept_multiple_files=True,
  )

  if uploaded_files:
    for f in uploaded_files:
      taille = len(f.getvalue()) / (1024 * 1024)
      if taille > 20:
        st.warning(
            f"⚠️ Le fichier **{f.name}** dépasse 20 Mo"
            f" ({taille:.1f} Mo). L'upload peut échouer ou être lent."
        )

  submit_button = st.button(
      label="Envoyer les documents", type="primary"
  )

  if submit_button:
    if not uploaded_files:
      st.warning("⚠️ Veuillez sélectionner au moins un fichier PDF.")
    elif not matiere.strip():
      st.warning("⚠️ Veuillez renseigner le nom de la matière.")
    elif not annee.strip():
      st.warning("⚠️ Veuillez renseigner l'année académique.")
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

      if succes:
        st.success(
            f"✅ {len(succes)} document(s) uploadé(s) avec succès sur"
            " Google Drive !"
        )
        st.markdown("### Accès direct aux fichiers envoyés :")
        for nom_f, lien_f in succes:
          st.markdown(f"- 📄 [{nom_f}]({lien_f})")

      if erreurs:
        st.error(f"❌ {len(erreurs)} fichier(s) en échec :")
        for nom_f, msg in erreurs:
          st.markdown(f"- **{nom_f}** : {msg}")

# ------------------------------------------
# PAGE 2 : CONSULTER LES DOCUMENTS
# ------------------------------------------
else:
  st.subheader("Consultation des documents")

  col_f1, col_f2, col_f3 = st.columns([2, 2, 2])
  with col_f1:
    choix_niveau_consulte = st.selectbox(
        "Filtrer par niveau",
        ["Tous les niveaux", "SJP1", "SJP2", "SJP3", "SJP4", "SJP5"],
    )
  with col_f2:
    choix_type = st.selectbox(
        "Filtrer par type",
        [
            "Tous les types",
            "Cours",
            "TD (Travaux Dirigés)",
            "TP (Travaux Pratiques)",
            "Examen / CC",
        ],
    )
  with col_f3:
    choix_matiere = st.text_input(
        "Recherche (ex: Analyse, Algèbre, RDM...)"
    )

  if st.button("🔍 Lancer la recherche", type="primary"):
    try:
      with st.spinner("Recherche des documents sur Google Drive..."):
        fichiers = lister_fichiers_drive()

      if not fichiers:
        st.info(
            "Aucun document trouvé dans le dossier Google Drive pour le"
            " moment."
        )
      else:
        fichiers_filtres = []
        for f in fichiers:
          nom = f["name"]

          if (
              choix_niveau_consulte != "Tous les niveaux"
              and f"[{choix_niveau_consulte}]" not in nom
          ):
            continue

          if choix_type != "Tous les types" and choix_type not in nom:
            continue

          if (
              choix_matiere.strip() != ""
              and choix_matiere.lower() not in nom.lower()
          ):
            continue

          fichiers_filtres.append(f)

        if fichiers_filtres:
          st.success(
              f"📊 {len(fichiers_filtres)} document(s) trouvé(s) !"
          )
          st.markdown("---")

          service = connecter_google_drive()

          for idx, f in enumerate(fichiers_filtres):
            with st.container(border=True):
              col_info, col_action = st.columns([4, 1])

              with col_info:
                st.markdown(f"#### 📄 {f['name']}")
                meta_parts = []
                annee_doc = _extraire_annee_depuis_nom(f["name"])
                if annee_doc:
                  meta_parts.append(f"🗓️ {annee_doc}")
                type_doc_trouve = _extraire_type_depuis_nom(f["name"])
                if type_doc_trouve:
                  meta_parts.append(f"🏷️ {type_doc_trouve}")
                meta_parts.append(
                    f"💾 {_formater_taille(f.get('size'))}"
                )
                st.caption("  ·  ".join(meta_parts))

              with col_action:
                try:
                  contenu = telecharger_contenu(service, f["id"])
                  st.download_button(
                      label="⬇️ Télécharger",
                      data=contenu,
                      file_name=(
                          f["name"]
                          if f["name"].endswith(".pdf")
                          else f["name"] + ".pdf"
                      ),
                      mime="application/pdf",
                      key=f"dl_{f['id']}_{idx}",
                      use_container_width=True,
                  )
                except Exception:
                  st.caption("⚠️ Indisponible")

                if f.get("webViewLink"):
                  st.markdown(
                      f"[🔗 Ouvrir sur Drive]({f['webViewLink']})",
                      unsafe_allow_html=False,
                  )
        else:
          st.warning(
              "Aucun document ne correspond à vos critères de recherche."
          )
    except Exception as e:
      st.error(f"Erreur lors de la récupération des fichiers : {e}")

# ==========================================
# PIED DE PAGE
# ==========================================
st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: gray; font-size: 14px;'>"
    "Développé par <b>ØH — Étudiant en Génie Civil</b> | "
    "Saint Jérôme Polytechnique (SJP)"
    "</div>",
    unsafe_allow_html=True,
)