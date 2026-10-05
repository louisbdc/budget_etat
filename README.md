# budget_etat

Outil perso pour explorer le budget de l'État français et simuler des scénarios « et si » (solde, dette, charge d'intérêts).

> **État d'avancement.** Les parseurs sont écrits d'après une première inspection des fichiers réels (`data/INSPECTION.md`, octobre 2026). Ils couvrent l'exécution 2010–2012 et 2024. Il manque encore 2015–2023, la dette de l'État et les recettes 2010–2014 : une deuxième passe `budget fetch` + `budget inspect` est nécessaire (voir « Ce qui reste à faire »).

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
  sources.py      parseurs (formats reconnus par les en-têtes)
  ingest.py       orchestration -> base canonique, nomenclature, VALIDATION.md
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

### Ce que la 1re inspection a montré

| Source | Contenu réel | Utilisé |
|---|---|---|
| [data.gouv.fr – SME DGFiP](https://www.data.gouv.fr/datasets/dgfip-situation-mensuelle-de-letat) et son miroir data.economie `situation-mensuelle-de-l-etat` | **index de 212 PDF** (2010 → 2026), aucune donnée tabulaire | non (PDF) |
| data.economie `execution-2010-du-budget-general-en-cp` | exécution CP par programme × action × titre (t1…t7), en €, **nomenclature ministère** (sans mission) | oui ; mission déduite par correspondance |
| `execution-2011-du-budget-general-en-cp` | idem, nomenclature mission | oui |
| `execution-2012-…-en-cp-suivant-la-nomenclature-mission-programm` | idem, sans colonne total, inclut les comptes spéciaux | oui, budget général seulement |
| `execution-2013-…`, `plr-2014-…` (pièces jointes) | non profilés (encodage) | parseur générique prêt ; à confirmer |
| `plrg-2024` (pièces jointes) | `annexe1_etat_titre_cat` (programme × titre × catégorie, €), `annexe1_etat_cp` (dépenses constatées), `annexe1_etat_recettes` (recettes par section) | oui ; `etat_cp` sert de contrôle |
| `plf-2024-recettes-du-budget-general`, `plf25-recettes-…`, `plf25-depenses-2025-selon-destination` | prévisions PLF / LFI par ligne | oui (nature `plf` / `lfi`, hors graphiques d'exécution) |
| `plf-2012/2013/2014-recettes-fiscales-nettes` | tableaux mis en page, M€, colonne « Exécution N-2 » | **pas encore** : en-têtes sur 2 lignes, à relire |
| Eurostat `gov_10dd_edpt1`, `nama_10_gdp` | JSON-stat valide ; codes de dimensions non visibles dans le 1er rapport | parseur prêt ; il liste les codes présents s'ils diffèrent de ceux attendus |
| INSEE BDM, famille « Dette négociable de l'État » | séries mensuelles (AFT) | parseur prêt ; nouvelle source, à confirmer |

**Trous connus.**
- **2015 → 2023** : aucun jeu trouvé lors de la 1re passe. Le `fetch` liste désormais tout le catalogue data.economie et sélectionne par identifiant et titre.
- **Dette de l'État** : absente des fichiers DGFiP en données. On prend la **dette négociable** (AFT, via INSEE), qui est une partie de la dette financière de l'État ; elle est affichée sous ce nom.
- **Solde d'exécution officiel** : uniquement dans les PDF de la SME. L'outil affiche le **solde du budget général calculé** (recettes nettes − dépenses nettes), sans les comptes spéciaux.
- **Recettes 2010–2023** : seule 2024 est ingérée pour l'instant.

`data/raw/manifest.json` trace chaque fichier : URL effective, date de récupération (UTC), taille, sha256. `budget ingest` produit aussi :
- `data/VALIDATION.md` : contrôles croisés entre montants publiés et agrégation des tables, et couverture par exercice (les trous y apparaissent en **manquant**) ;
- `data/NON_RAPPROCHES.md` : programmes non rapprochés d'une année à l'autre.

Le statut de chaque fichier est affiché : ingéré, ignoré (doublon AE, compte spécial, annexe non utilisée), à inspecter (format inconnu, avec ses en-têtes) ou en erreur. **Aucun chiffre n'est codé en dur, et un format inconnu n'est jamais deviné.**

### Conventions de normalisation

- Montants convertis en **M€** ; nombres français (`1 234,56`), encodages UTF-8, cp1252 et latin-1 gérés.
- Budget général seulement, selon la numérotation des programmes : < 600 budget général, 6xx budgets annexes, 7xx CAS, 8xx CCF, 9xx comptes de commerce.
- **Vue nette** : les programmes 200/201 (remboursements et dégrèvements) sont stockés mais exclus des dépenses, car les recettes fiscales sont nettes.
- Recettes 2024 : totaux par section de la colonne « Total des recettes** ». Le sens exact de `**` reste à confirmer dans la notice (zip `notices_par_fichiers_csv_plrg_2024`, que le nouvel `inspect` décompresse).
- Prélèvements sur recettes (UE, collectivités) : stockés en positif, retranchés des recettes nettes.
- Charge de la dette : programme 117 (titre 4).

### Ce qui reste à faire

1. Relancer `budget fetch` puis `budget inspect`, et partager `data/INSPECTION.md`. On y verra : le catalogue complet, les fichiers 2013–2014, la notice PLRG, les codes Eurostat et les séries INSEE.
2. Écrire le parseur des tableaux « recettes fiscales nettes », puis ceux des formats 2015–2023 qui ne seraient pas déjà reconnus.
3. Vérifier `rigidites.py` sur les libellés réels.

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

- **Dette de l'État** : dette **négociable** de l'État (AFT, série INSEE). C'est le **seul** périmètre des calculs du simulateur et des graphiques « État ». Elle est inférieure à la dette financière totale de l'État.
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
