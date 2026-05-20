# Agrégateur de sujets via sitemaps

Ce dépôt met en place une veille quotidienne par thématique à partir de fichiers CSV versionnés dans le repo.

## Fonctionnement

Chaque jour, le workflow GitHub Actions :

1. lit tous les fichiers `data/themes/*.csv`
2. regroupe les sitemaps par site
3. lit tous les sitemaps du site et agrège leurs URLs
4. normalise les URLs puis compare avec l'état précédent
5. ignore le site si un sitemap échoue après 3 tentatives
6. identifie les nouvelles URLs jamais vues
7. récupère en parallèle la balise `<title>` de chaque nouvelle page
8. extrait un mot-clé avec KeyBERT à partir du title
9. ajoute une ligne par nouvelle URL à l'historique de la thématique
10. envoie un email avec les CSV du jour
11. commit l'état mis à jour dans le repo

Le premier lancement doit se faire en mode bootstrap pour initialiser l'état sans remonter tout l'historique existant.

Un site sans snapshot existant est initialisé silencieusement au premier run : ses URLs sont enregistrées dans `state/`, mais aucune notification n'est envoyée pour ce site ce jour-là. Cela permet d'ajouter une nouvelle thématique sans toucher au suivi normal des thématiques déjà actives.

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
  ever_seen/
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
- `state/ever_seen/<theme>/<site>.json` : toutes les URLs déjà vues historiquement pour le site
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
- les fichiers `ever_seen` sont créés
- aucun email n'est envoyé
- aucun CSV quotidien n'est généré

À partir du lendemain, le job planifié ne remontera que les nouvelles URLs.

## Règles de robustesse

- une URL n'est notifiée que si elle est absente du snapshot précédent et du fichier `ever_seen`
- si un sitemap du site échoue après 3 tentatives, le snapshot du site n'est pas écrasé ce jour-là
- la récupération des `<title>` est parallélisée et limitée à cette seule balise

## Lancement local

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/monitor_sitemaps.py
```

Si les variables SMTP ne sont pas définies, le script génère les fichiers mais n'envoie pas de mail.
