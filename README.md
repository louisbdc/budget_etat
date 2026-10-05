# budget_etat

Outil perso pour explorer le budget de l'État français et simuler des scénarios « et si » (solde, dette, charge d'intérêts).

> **État d'avancement.** Les parseurs sont écrits d'après trois inspections des fichiers réels.
>
> - **Agrégats officiels mensuels 2013 → 2026** (SMB de la DGFiP) : solde budgétaire, dépenses et recettes nettes, recettes par grand impôt, charge de la dette.
> - **Détail mission → programme → titre** : 2010–2014, 2018–2020, 2023–2025.
> - **Dette négociable de l'État et PIB** : 2009–2025.
>
> Restent sans détail par programme : 2015–2017 et 2021–2022 (aucun jeu au catalogue) ; les agrégats SMB couvrent ces années dans « Évolution ».

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

### Ce que les inspections ont montré

| Source | Contenu réel | Exercices | Statut |
|---|---|---|---|
| [data.gouv.fr – SME DGFiP](https://www.data.gouv.fr/datasets/dgfip-situation-mensuelle-de-letat) + miroir `situation-mensuelle-de-l-etat` | **index de 212 PDF**, aucune donnée | 2010–2026 | non utilisé |
| `situations-mensuelles-budgetaires-series-longues` | **SMB DGFiP** : tableau large, 26 « lignes d'information » (solde budgétaire, dépenses nettes par titre, PSR, recettes nettes par grand impôt, soldes CS/BA, R&D) × fins de mois (`jj_mm_aaaa`, cumul en €). Notice PDF jointe. | export : 2024-01 → 2026-07 ; pièce jointe 2013–2023 | ingéré ; les pièces jointes ont des fins de ligne **CR seules** (gérées) |
| `execution-AAAA-du-budget-general-en-cp` | CP par programme × action × titre (t1…t7), € ; 2010 sans mission | 2010, 2011 | ingéré |
| `execution-2012-…-nomenclature-mission-programm` | idem, sans total, comptes spéciaux inclus | 2012 | ingéré (budget général) |
| `execution-2013-…` / `plr-2014-…` (pièces jointes cp1252) | tableau croisé : 4 lignes de titre puis `Mission;Programme;…;T1…T7;Total général`, nombres `24 928 201` | 2013, 2014 | ingéré |
| `projet-de-loi-de-reglement-2019-plr-20190` (titre « PLR 2018 ») | destination × nature : programme × action × catégorie × titre, `exec_cp_2018_rap_2018` | 2018 (`annee_rap`) | ingéré ; la synthèse par programme (`…-plr-2019`) sert de contrôle, le tableau par titre (`…-plr-20191`) est un doublon ignoré |
| `…-plr-20192`, `…-plr-2020` (pièces jointes `credits_destination_nature_xls`) | classeurs Excel, feuille `Credits` : `exercice;typeBudget;mission(code);programme;action;categorie;titre;AE EXEC;CP EXEC` ; libellés dans le classeur `nomenclature` joint (lignes MSN / PGM) | 2019, 2020 | ingéré |
| `plrg-2024` | `annexe1_etat_titre_cat` (dépenses), `annexe1_etat_recettes` (recettes par section), `annexe1_etat_cp` (contrôle) | 2024 | ingéré ; 130 contrôles exacts |
| PLRG 2023, PLRG 2025 (`projet-de-loi-relatif-aux-resultats-…`) | même format que 2024 pour les dépenses ; recettes 2025 en recouvrements bruts par ligne (autre format) | 2023, 2025 | dépenses ingérées ; recettes prises dans la SMB |
| `plf-2013/2014-recettes-fiscales-nettes` (pièces jointes) | colonne « Exécution N-2 » : nettes par impôt (IR, IS, TICPE, TVA, autres), non fiscales, prélèvements, M€ | 2011, 2012 | ingéré |
| `plf-2012-recettes-fiscales-nettes` | milliers d'€, impôts nets mais **sans recettes non fiscales, ni prélèvements, ni R&D d'impôts locaux** | 2010 | **non ingéré** : un total net 2010 serait faux |
| `plf-2024/plf25-recettes-…`, `plf25-depenses-…-selon-destination` | prévisions PLF / LFI | 2024, 2025 | ingéré (nature `plf` / `lfi`) |
| INSEE série **001739081** | encours de la dette négociable totale de l'État (AFT), mensuel, M€ (`UNIT_MULT=6`) | 2009-01 → 2026-08 | ingéré : décembre = fin d'année |
| Eurostat `gov_10dd_edpt1` | `MIO_EUR` × `S13` × `GD` / `B9` (codes confirmés) | 1995–2025 | ingéré (périmètre APU) |
| Eurostat `nama_10_gdp` | PIB `B1GQ` `CP_MEUR` | 1975–2025 | ingéré |

**Trous connus.**
- **Détail par programme 2015–2017 et 2021–2022** : aucun jeu au catalogue data.economie. Ces années n'ont que les agrégats SMB.
- **Recettes 2010** : tableau source incomplet (voir ci-dessus).

**Vérifications faites sur les données réelles** (`VALIDATION.md`) :
- 501 contrôles programme par programme exacts au centime ;
- dépenses nettes et charge de la dette : détail par programme = SMB au M€ près pour 2013, 2018, 2019, 2020, 2023, 2024 et 2025 (2014 : −1,7 M€, arrondis) ; total fiscal 2024 PLRG = SMB (325 679 M€) ;
- 2011 : recettes nettes des prélèvements = ligne « Recettes totales nettes des prélèvements » du PLF 2013 (199 151 M€) ;
- solde SMB 2024 = recettes nettes + fonds de concours − dépenses nettes − prélèvements + soldes des comptes spéciaux et budgets annexes, au centime.

`data/raw/manifest.json` trace chaque fichier (URL, date UTC, sha256). Le statut de chaque fichier est affiché : ingéré, ignoré, à inspecter (format inconnu, avec ses en-têtes) ou en erreur. **Aucun chiffre n'est codé en dur, et un format inconnu n'est jamais deviné.**

### Sources multiples et priorité

Quand plusieurs fichiers couvrent le même exercice, `ingest` ne garde qu'une source par exercice (et par mois et indicateur pour les agrégats). Ordre de priorité : SMB / INSEE / Eurostat > PLRG > PLR > exécution data.economie > PLF. Les sources écartées et les écarts éventuels sont listés dans `VALIDATION.md`.

### Conventions de normalisation

- Montants convertis en **M€** ; nombres français (`1 234,56`), encodages UTF-8, cp1252 et latin-1 gérés.
- Budget général seulement, selon la numérotation des programmes : < 600 budget général, 6xx budgets annexes, 7xx CAS, 8xx CCF, 9xx comptes de commerce.
- **Vue nette**, alignée sur la SMB : le programme 200 (remboursements et dégrèvements d'impôts d'État) est toujours exclu. Le programme 201 (impôts locaux) est exclu jusqu'en 2022 et **inclus depuis 2023** (loi organique du 28/12/2021, rappelée dans la notice SMB).
- **Évolution** : les agrégats officiels de la SMB (dépenses nettes, charge de la dette, solde budgétaire) priment sur l'agrégation du détail. Le solde officiel inclut les comptes spéciaux et budgets annexes ; sinon le solde est calculé, et c'est indiqué sous le graphique.
- Recettes : SMB (nettes par grand impôt) à partir de 2013, PLF N+2 pour 2011–2012. Les sections PLRG (« Total des recettes** », nettes : le total fiscal 2024 est égal à celui de la SMB) servent de contrôle.
- Prélèvements sur recettes (UE, collectivités) : stockés en positif, retranchés des recettes nettes.
- Charge de la dette : programme 117 (titre 4).

### Ce qui reste à faire

1. Relancer `budget ingest` et lire `VALIDATION.md` : rapprochement SMB / détail pour 2019, 2020 et 2023.
2. Écrire les parseurs restants (séries longues, PLF 2012) d'après ce rapport.
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

- **Dette de l'État** : dette **négociable** de l'État (AFT, série INSEE 001739081, fin décembre). C'est le **seul** périmètre des calculs du simulateur et des graphiques « État ». Elle est inférieure à la dette financière totale de l'État.
- **Dette publique au sens de Maastricht** : toutes les APU (État + ASSO + APUL), source Eurostat. Elle est affichée dans un bloc séparé, à titre de contexte, et n'entre dans aucun calcul.

Chaque graphique porte une étiquette de périmètre.

## Interface

- **Évolution** : recettes nettes et dépenses, solde, dette de l'État, charge de la dette (graphique à part), en Md€ ou en % du PIB. Bloc APU séparé.
- **Flux** : Sankey recettes (12 principales + « autres ») → budget général → missions → programmes. Les prélèvements sur recettes sortent du budget général, le déficit y entre comme ressource.
- **Détail** : drill-down mission → programme → titre (clic), avec montant, % du total et % du PIB.
- **Simulateur** : année de base (pré-remplie depuis la base, modifiable), hypothèses, mesures en % ou en M€ sur un programme, une mission entière, un impôt ou une catégorie de recettes, avec une année de début et une montée en charge. Scénarios nommés enregistrés dans `data/scenarios/*.json`, comparaison de plusieurs scénarios sur les mêmes graphiques.
- Les postes **peu pilotables** sont marqués ⚠ partout : charge de la dette (P117), prélèvement UE, titre 2 (personnel) et pensions.

## Programmes (politiques ou personnels)

Un programme est un fichier `data/programmes/<id>.json`. Le modèle commenté est `data/programmes/_modele.json`. Dans le simulateur, « Charger un programme » remplit le tableau des mesures ; « Comparer avec » superpose plusieurs programmes et scénarios.

Chaque mesure indique :
- `cote` : `depense` ou `recette` ;
- `cible` : `P:146` (programme), `M:Défense` (mission, libellé comparé sans accents ni casse), `R:TVA` / `R:IR` (poste de recette), `C:fiscale` (catégorie de recettes) ou `*` (toutes les lignes du côté visé, au prorata) ;
- `mode` (`pct` ou `meur`), `valeur`, `debut` (année), `montee_en_charge` (années) ;
- `chiffrage` : `porteur`, `tiers` (Cour des comptes, OFCE, institut…) ou `utilisateur` (hypothèse personnelle) ;
- `source` : `url`, `citation` exacte qui donne le chiffre, `auteur`, `consulte_le`. **Obligatoire** pour `porteur` et `tiers`, sinon le programme est refusé ;
- `note` : comment une annonce a été traduite en mesure (ex. « 10 Md€ d'économies sur le quinquennat » → montée en charge sur 5 ans).

Règles :
- **aucun chiffre n'est deviné** : une valeur manquante rend le programme invalide (affiché comme tel) ;
- une cible introuvable dans l'année de base est affichée « non appliquée », jamais ignorée en silence ;
- **neutralité** : tous les programmes et scénarios comparés sont projetés avec les mêmes hypothèses macro (celles de l'interface). Les écarts ne viennent que des mesures ;
- limites : un programme est réduit à des variations de lignes du budget général de l'État. Les mesures portant sur la Sécurité sociale, les collectivités, la réglementation ou les dépenses fiscales sans montant chiffré ne sont pas représentables. Les effets de comportement (sauf le multiplicateur optionnel) ne sont pas modélisés.

### Programmes fournis : présidentielle 2022 (test)

`data/programmes/2022_*.json` contient les six principaux candidats de 2022 (Macron, Le Pen, Mélenchon, Pécresse, Zemmour, Jadot), traités de la même façon :
- **une seule source pour tous** : les chiffrages mesure par mesure de l'Institut Montaigne, un tiers, plutôt que les chiffrages des équipes de campagne ;
- **chiffres relevés dans des résultats de moteur de recherche, pages non ouvertes** (l'environnement de développement n'y avait pas accès) : chaque mesure porte l'URL de sa page et l'avertissement « à vérifier » ;
- **rejoués pour test** à partir de 2027 sur la base 2025 : ils servent à tester l'outil, pas à évaluer ce qu'auraient donné ces programmes en 2022 ;
- la plupart des grosses mesures (retraites, CSG, cotisations, impôts de production locaux, hôpitaux) sont **hors budget de l'État** : elles sont listées dans `non_representees` avec la raison, et affichées dans l'interface, mais pas simulées. Le programme Zemmour n'a ainsi aucune mesure simulable ;
- quand le chiffre est une fourchette : estimation centrale de la source si elle existe, sinon borne basse, indiquée dans la note de la mesure.

Graphiques associés : déficit / PIB ; décomposition de l'écart de déficit (dépenses, intérêts, recettes) ; dépenses par mission et recettes par poste, en écart à la référence, année par année (ce qui monte, ce qui baisse, et quand) ; tableau comparatif à l'horizon.

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
