from datetime import datetime
import io
import json
import os
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# Chargement direct des secrets JSON bruts
if "GCP_JSON" in st.secrets:
  with open("credentials.json", "w") as f:
    f.write(st.secrets["GCP_JSON"])

# Configuration de l'ID du dossier Google Drive et des permissions
DOSSIER_DRIVE_ID = "1eI92fD9kOM1dlAO7DgdMkt7QzyMHw-58"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def connecter_google_drive():
  """Établit la connexion sécurisée via le Compte de service Google Cloud"""
  creds = service_account.Credentials.from_service_account_file(
      "credentials.json", scopes=SCOPES
  )
  return build("drive", "v3", credentials=creds)


def uploader_vers_drive(
    uploaded_file, matiere, niveau, filiere, chapitre, annee, type_doc
):
  """Envoie un fichier PDF sur Google Drive avec un nom structuré"""
  service = connecter_google_drive()
  part_filiere = f"_{filiere}" if filiere else ""
  part_chapitre = f"_{chapitre}" if chapitre.strip() else ""

  nom_fichier = f"[{niveau}]{part_filiere}_{matiere}{part_chapitre}_{annee}_{type_doc}_{uploaded_file.name}"

  metadata = {"name": nom_fichier, "parents": [DOSSIER_DRIVE_ID]}

  media = MediaIoBaseUpload(
      io.BytesIO(uploaded_file.getvalue()),
      mimetype="application/pdf",
      resumable=True,
  )

  file = (
      service.files()
      .create(body=metadata, media_body=media, fields="id, webViewLink")
      .execute()
  )

  return file.get("webViewLink")


def lister_fichiers_drive():
  """Récupère la liste de tous les fichiers du dossier Google Drive"""
  service = connecter_google_drive()
  query = f"'{DOSSIER_DRIVE_ID}' in parents and trashed = false"
  results = (
      service.files()
      .list(
          q=query, pageSize=100, fields="files(id, name, webViewLink, createdTime)"
      )
      .execute()
  )
  return results.get("files", [])


# Configuration de la page Streamlit
st.set_page_config(
    page_title="Atlas - Bibliothèque Numérique", page_icon="📚", layout="wide"
)

# ==========================================
# BARRE LATÉRALE (Sidebar)
# ==========================================
st.sidebar.title("📚 Navigation")
st.sidebar.markdown("---")

menu = st.sidebar.radio(
    "Aller vers :", ["📁 Consulter les documents", "📤 Partager un document"]
)

st.sidebar.markdown("---")
st.sidebar.info(
    "Plateforme académique dédiée aux étudiants de Saint Jérôme Polytechnique"
    " (SJP)."
)

# ==========================================
# CONTENU PRINCIPAL
# ==========================================
st.title("📚 Atlas - Bibliothèque Numérique")

if menu == "📤 Partager un document":
  st.subheader("Uploader de nouveaux documents académiques")

  col1, col2 = st.columns(2)
  with col1:
    niveau = st.selectbox(
        "Sélectionnez votre niveau", ["SJP1", "SJP2", "SJP3", "SJP4", "SJP5"]
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
      ["Cours", "TD (Travaux Dirigés)", "TP (Travaux Pratiques)", "Examen / CC"],
  )

  uploaded_files = st.file_uploader(
      "Choisissez les fichiers PDF à téléverser (sélection multiple"
      " autorisée)",
      type=["pdf"],
      accept_multiple_files=True,
  )

  submit_button = st.button(label="Envoyer les documents", type="primary")

  if submit_button:
    if uploaded_files and matiere.strip() != "" and annee.strip() != "":
      try:
        with st.spinner("Téléversement sécurisé en cours vers Google Drive..."):
          liens_generes = []
          for uploaded_file in uploaded_files:
            lien = uploader_vers_drive(
                uploaded_file,
                matiere.strip(),
                niveau,
                filiere,
                chapitre,
                annee.strip(),
                type_doc,
            )
            liens_generes.append((uploaded_file.name, lien))

        st.success(
            f"{len(uploaded_files)} document(s) uploadé(s) et sauvegardé(s) avec"
            " succès sur votre Drive !"
        )
        st.markdown("### Accès direct aux fichiers envoyés :")
        for nom_f, lien_f in liens_generes:
          st.markdown(f"- 📄 [{nom_f}]({lien_f})")

      except Exception as e:
        st.error(f"Une erreur est survenue lors de l'envoi : {e}")
    else:
      st.warning(
          "⚠️ Veuillez renseigner le nom de la matière, l'année académique et"
          " sélectionner au moins un fichier PDF avant d'envoyer."
      )

else:
  st.subheader("Consultation des documents")

  col_f1, col_f2 = st.columns(2)
  with col_f1:
    choix_niveau_consulte = st.selectbox(
        "Filtrer par niveau",
        ["Tous les niveaux", "SJP1", "SJP2", "SJP3", "SJP4", "SJP5"],
    )
  with col_f2:
    choix_matiere = st.text_input("Que cherchez-vous ? (ex: Analyse, Algèbre,...)")

  if st.button("Lancer la recherche", type="primary"):
    try:
      with st.spinner("Recherche des documents sur Google Drive..."):
        fichiers = lister_fichiers_drive()

      if not fichiers:
        st.info("Aucun document trouvé dans le dossier Google Drive pour le moment.")
      else:
        fichiers_filtres = []
        for f in fichiers:
          nom = f["name"]
          match_niveau = True
          if choix_niveau_consulte != "Tous les niveaux":
            if f"[{choix_niveau_consulte}]" not in nom:
              match_niveau = False

          match_matiere = True
          if choix_matiere.strip() != "":
            if choix_matiere.lower() not in nom.lower():
              match_matiere = False

          if match_niveau and match_matiere:
            fichiers_filtres.append(f)

        if fichiers_filtres:
          st.success(f"{len(fichiers_filtres)} document(s) trouvé(s) !")
          st.markdown("---")
          for f in fichiers_filtres:
            st.markdown(f"📄 **[{f['name']}]({f['webViewLink']})**")
        else:
          st.warning("Aucun document ne correspond à vos critères de recherche.")
    except Exception as e:
      st.error(f"Erreur lors de la récupération des fichiers : {e}")

# ==========================================
# Pied de page personnalisé
# ==========================================
st.markdown("---")
date_creation = datetime.now().strftime("%d/%m/%Y")
st.markdown(
    f"<div style='text-align: center; color: gray; font-size: 14px;'>"
    f"Développé par <b>OUMAR HASSANA Etudiant en Génie Civil</b> | Créé le :"
    f" <b>{date_creation}</b>"
    f"</div>",
    unsafe_allow_html=True,
)
