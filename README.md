# Agrégateur de sujets via sitemaps

Ce dépôt met en place une veille quotidienne par thématique à partir de fichiers CSV versionnés dans le repo.

## Fonctionnement

Chaque jour, le workflow GitHub Actions :

1. lit tous les fichiers `data/themes/*.csv`
2. récupère les URLs de chaque sitemap
3. compare avec l'état précédent
4. identifie les nouvelles URLs
5. récupère la balise `<title>` de chaque nouvelle page
6. extrait un mot-clé avec KeyBERT à partir du title
7. ajoute une ligne par nouvelle URL à l'historique de la thématique
8. envoie un email avec les CSV du jour
9. commit l'état mis à jour dans le repo

Le premier lancement doit se faire en mode bootstrap pour initialiser l'état sans remonter tout l'historique existant.

## Structure

```text
data/
  site_lists/
    immobilier.txt
  themes/
    immobilier.csv
    immobilier_missing.csv
scripts/
  discover_sitemaps.py
  monitor_sitemaps.py
state/
  snapshots/
reports/
  daily/
  history/
```

## Format du CSV thématique

Chaque fichier représente une thématique.

Exemple :

```csv
site,homepage_url,sitemap_url
www.dixmois.fr,https://www.dixmois.fr,https://www.dixmois.fr/sitemap_index.xml
www.lkeria.com,https://www.lkeria.com,https://www.lkeria.com/sitemap_agence.xml.gz
```

Seules les colonnes `site` et `sitemap_url` sont utilisées par le moniteur. `homepage_url` est conservée pour référence.

## Fichiers générés

- `state/snapshots/<theme>/<site>.json` : snapshot courant des URLs vues pour le site
- `reports/history/<theme>_all_urls.csv` : historique cumulé, une ligne par nouvelle URL
- `reports/daily/<YYYY-MM-DD>/<theme>_new_urls.csv` : fichier du jour

Colonnes produites :

- `domain`
- `title`
- `keyword_keybert`
- `url`
- `detected_on`

## Email

Le mail envoie les CSV journaliers en pièce jointe. Chaque ligne suit l'ordre :

- `domain`
- `title`
- `keyword_keybert`

## Découverte automatique des sitemaps

Le script suivant part d'une liste de domaines et tente de trouver le sitemap via `robots.txt`, des chemins standards et les liens de la homepage :

```powershell
python scripts/discover_sitemaps.py data/site_lists/immobilier.txt data/themes/immobilier.csv
```

Il génère aussi `data/themes/immobilier_missing.csv` pour les sites où aucun sitemap XML n'a été détecté automatiquement.

## Secrets GitHub à définir

Dans `Settings > Secrets and variables > Actions`, ajoute :

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `EMAIL_FROM`
- `EMAIL_TO`

Optionnel :

- `EMAIL_SUBJECT_PREFIX`
- `KEYBERT_MODEL`

## Premier lancement

Déclenche manuellement le workflow GitHub Actions avec `bootstrap_only=true`.

Effet :

- les snapshots sont créés
- aucun email n'est envoyé
- aucun CSV quotidien n'est généré

À partir du lendemain, le job planifié ne remontera que les nouvelles URLs.

## Lancement local

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/monitor_sitemaps.py
```

Si les variables SMTP ne sont pas définies, le script génère les fichiers mais n'envoie pas de mail.
