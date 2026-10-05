# budget_etat

Outil perso pour explorer le budget de l'État français et simuler des scénarios « et si » (solde, dette, charge d'intérêts).

> **État d'avancement.** Tout est en place sauf les **parseurs des fichiers DGFiP / data.economie** : le téléchargement avec cache, l'inspection des fichiers, la base canonique, le rapprochement de nomenclature, l'API, le front (évolution, Sankey, drill-down) et le simulateur. Les parseurs n'ont pas pu être écrits parce que l'environnement de développement n'avait **aucun accès** aux domaines sources (voir « Données »), et aucun schéma n'a été supposé. Pour débloquer : lancer `budget fetch` puis `budget inspect` sur une machine connectée, et partager `data/INSPECTION.md`.

## Lancer

Une seule commande (télécharge, inspecte, ingère, puis ouvre l'interface sur http://127.0.0.1:8000) :

```bash
uv run budget all
```

Étape par étape :

```bash
uv run budget fetch     # sources brutes -> data/raw/ (cache + manifest.json), idempotent
uv run budget inspect   # profil des fichiers bruts -> data/INSPECTION.md
uv run budget ingest    # data/raw/ -> data/budget.duckdb (reconstruction atomique)
uv run budget serve     # interface web locale
uv run --extra dev pytest
```

Sans base, l'interface s'ouvre quand même : les graphiques indiquent « pas de donnée » et les champs de l'année de base du simulateur se saisissent à la main.

## Architecture

```
budget_etat/
  fetch.py        sources -> data/raw/ + manifest (URL, date UTC, sha256), sans retéléchargement inutile
  inspect_raw.py  profil des fichiers bruts : colonnes, types, vides, cardinalité, valeurs fréquentes
  ingest.py       parseurs -> base canonique, puis rapprochement de nomenclature
  db.py           schéma canonique DuckDB (M€ courants)
  nomenclature.py rapprochement mission/programme entre exercices
  jsonstat.py     décodeur JSON-stat 2.0 (Eurostat)
  queries.py      séries, Sankey, drill-down, base du simulateur
  rigidites.py    règles de marquage « peu pilotable »
  projection.py   MOTEUR DE PROJECTION (pur, sans I/O, testé)
  server.py       API FastAPI + fichiers statiques
web/              front HTML/JS, ECharts embarqué dans web/vendor (aucun CDN)
data/correspondances.csv   correspondances de nomenclature saisies à la main
```

Schéma canonique (`db.py`) : `depense` (exercice, mois, periode, nature, mission, programme, titre, montant), `recette` (catégorie fiscale / non fiscale / prélèvement, sens brut ou net, poste), `agregat_etat` (solde, dette de l'État, charge de la dette), `macro` (PIB nominal, dette et solde APU), `nomenclature_programme`, `source_file`.

## Données

| Source | URL | Usage prévu | Statut |
|---|---|---|---|
| DGFiP – Situation mensuelle de l'État | https://www.data.gouv.fr/datasets/dgfip-situation-mensuelle-de-letat (API `/api/1/datasets/dgfip-situation-mensuelle-de-letat/`) | solde, dépenses par titre / mission / programme, recettes, dette de l'État | **non inspecté** (403 depuis l'environnement de dev) |
| Même jeu, miroir data.economie | https://data.economie.gouv.fr/explore/dataset/situation-mensuelle-de-l-etat/ | idem, export CSV complet | **non inspecté** |
| PLRG 2024 (résultats et gestion) | https://data.economie.gouv.fr/explore/dataset/plrg-2024/ (pièces jointes, dont `annexe1_etat_recettes_csv`) | exécution annuelle, recettes par impôt | **non inspecté** |
| Autres jeux data.economie | recherche au catalogue (`ODS_QUERIES` dans `fetch.py`) ; les jeux `plr*`, `execution-*` et `*recettes*` sont aussi téléchargés | historique pluriannuel | **non inspecté** |
| Eurostat `gov_10dd_edpt1` | https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/gov_10dd_edpt1 | dette et solde APU (Maastricht) | parseur écrit (format JSON-stat standard) ; codes `MIO_EUR` / `S13` / `GD` / `B9` à confirmer |
| Eurostat `nama_10_gdp` | même API, `na_item=B1GQ&unit=CP_MEUR` | PIB nominal | idem, codes `B1GQ` / `CP_MEUR` à confirmer |
| INSEE (comptes des APU) | https://www.insee.fr | alternative à Eurostat | non utilisé pour l'instant |

Les identifiants des jeux data.economie ont été trouvés par recherche web (les pages elles-mêmes étaient inaccessibles). `fetch` signale ceux qui renvoient une erreur, sans s'interrompre. Chaque fichier est tracé dans `data/raw/manifest.json` : URL effective, date de récupération (UTC), taille, sha256. Ces informations apparaissent aussi dans l'onglet « Données ».

**Aucun chiffre n'est codé en dur.** Quand une donnée manque, elle reste paramétrable dans l'interface (champ en rouge). Les tests utilisent des fixtures abstraites (« Mission A », montants ronds) qui ne sont jamais servies comme données.

### Ce qui reste à faire après inspection

1. Écrire les parseurs `dgfip_sme`, `economie_sme` et `economie_plrg` dans `ingest.py` (aujourd'hui ils lèvent `NotInspected`, et `ingest` les liste comme « en attente »).
2. Confirmer les codes Eurostat : si un code manque, le parseur échoue en listant les codes présents.
3. Vérifier les règles de `rigidites.py` sur les libellés réels.

## Nomenclature mission / programme

Le rapprochement est automatique (`nomenclature.py`), avec comme référence l'exercice le plus récent :

| méthode | règle |
|---|---|
| `manuel` | ligne de `data/correspondances.csv` (exercice vide = toutes années) |
| `code+libelle` | même numéro de programme, libellé proche (Jaccard ≥ 0,5 sur les mots, accents et mots vides ignorés) |
| `code_seul` | même numéro, libellé différent : rapproché mais **signalé** |
| `non_rapproche` | numéro absent de l'exercice de référence |

`budget ingest` écrit `data/NON_RAPPROCHES.md`, la liste des cas `non_rapproche` et `code_seul` à trancher.

## Périmètres : ne pas mélanger

- **Dette de l'État** : dette financière de l'État (DGFiP). C'est le **seul** périmètre des calculs du simulateur et des graphiques « État ».
- **Dette publique au sens de Maastricht** : toutes les APU (État + ASSO + APUL), source Eurostat. Elle est affichée dans un bloc séparé, à titre de contexte, et n'entre dans aucun calcul.

Chaque graphique porte une étiquette de périmètre.

## Interface

- **Évolution** : recettes nettes et dépenses, solde, dette de l'État, charge de la dette (graphique à part), en Md€ ou en % du PIB. Bloc APU séparé.
- **Flux** : Sankey recettes (12 principales + « autres ») → budget général → missions → programmes. Les prélèvements sur recettes sortent du budget général, le déficit y entre comme ressource.
- **Détail** : drill-down mission → programme → titre (clic), avec montant, % du total et % du PIB.
- **Simulateur** : année de base (pré-remplie depuis la base, modifiable), hypothèses, mesures en % ou en M€ sur un programme, une mission entière, un impôt ou une catégorie de recettes, avec une année de début et une montée en charge. Scénarios nommés enregistrés dans `data/scenarios/*.json`, comparaison de plusieurs scénarios sur les mêmes graphiques.
- Les postes **peu pilotables** sont marqués ⚠ partout : charge de la dette (P117), prélèvement UE, titre 2 (personnel) et pensions.

## Simulateur : formules (`budget_etat/projection.py`)

Notations pour l'année *t* (année de base *t = 0*, montants en M€ courants). Lignes de dépense = programmes **hors programme 117** (la charge d'intérêts est calculée par le modèle), groupées par mission. Lignes de recette = postes, groupés par catégorie ; les prélèvements sur recettes sont des lignes négatives.

- PIB tendanciel : `Y*_t = Y*_{t-1} · (1 + g_t)`
- Dépense tendancielle par ligne : `D*_{i,t} = D*_{i,t-1} · (1 + s_{i,t})`
- Recette tendancielle par ligne : `R*_{j,t} = R_{j,0} · (Y*_t / Y_0)^ε`
- Mesure : `Δ_t = φ_t · v` (M€) ou `φ_t · v% · base_t` (%), avec `φ_t = min(1, max(0, (t − début + 1) / N))`. Un montant en M€ visant un groupe est réparti au prorata des lignes.
- Multiplicateur (désactivé par défaut) : `Y_t = Y*_t + m_D · ΔD_t − m_R · ΔR_t`, puis recettes × `(Y_t / Y*_t)^ε`. L'effet porte sur le niveau de l'année, sans persistance.
- Taux apparent et dette :
  - roulement : `r_t = (1 − ρ) · r̃_{t-1} + ρ · m_t`, où ρ est la part du stock refinancée par an (≈ 1/maturité moyenne) et m_t le taux de marché ;
  - intérêts : `I_t = r_t · B_{t-1}` ;
  - déficit : `d_t = Σ D_{i,t} + I_t − Σ R_{j,t}` (> 0 = besoin de financement) ;
  - dette : `B_t = B_{t-1} + d_t + autres flux_t` ;
  - les émissions nettes entrent au taux de marché : `r̃_t = (B_{t-1}·r_t + e_t·m_t) / B_t` si `e_t > 0`, sinon `r̃_t = r_t` ;
  - initialisation : `r_0 = I_0 / B_0`.
- Sorties : déficit, dette de l'État, dette/PIB, intérêts, taux apparent, et écarts au scénario de référence (sans mesure, mêmes hypothèses).

Les hypothèses par défaut de l'interface (croissance 2,5 %, taux 3 %, ρ 12 %, tendance des dépenses 2 %, élasticité 1, multiplicateurs 0,5 / 0,3) sont **indicatives** et ne viennent pas des données. À ajuster.

## Limites du modèle

- Modèle comptable annuel, sans bouclage macro complet. Le multiplicateur est un simple effet de niveau, sans persistance.
- Un seul taux de marché, sans courbe des taux, sans OATi ni inflation séparée (tout est en nominal).
- Trésorerie, primes et décotes, variation des dépôts : regroupées dans « autres flux » (exogène).
- Recettes indexées sur le PIB avec une élasticité unique. Pas de base fiscale propre à chaque impôt.
- Le programme 117 contient aussi des frais de trésorerie. Si la source publie un agrégat « charge de la dette », celui-ci est préféré.
- Budget général seulement : les comptes spéciaux (dont le CAS Pensions) et les budgets annexes ne sont pas projetés.
- Les rapprochements de nomenclature restent heuristiques : lire `data/NON_RAPPROCHES.md`.
