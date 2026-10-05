# budget_etat

Outil perso pour explorer le budget de l'État français et simuler des scénarios « et si » (solde, dette, charge d'intérêts).

> **État d'avancement** : le moteur de projection (testé) et la couche de téléchargement sont en place. L'ingestion normalisée, le front et le branchement du simulateur sur les données attendent que les fichiers sources aient été **réellement inspectés**. L'environnement de développement initial n'avait pas accès aux domaines sources (voir « Données »).

## Lancer

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/budget fetch      # télécharge les sources dans data/raw/ (cache + manifest.json)
.venv/bin/pytest            # tests du moteur de projection
```

## Données

| Source | URL | Usage prévu | Statut |
|---|---|---|---|
| DGFiP – Situation mensuelle de l'État | https://www.data.gouv.fr/datasets/dgfip-situation-mensuelle-de-letat (API : `/api/1/datasets/dgfip-situation-mensuelle-de-letat/`) | solde, dépenses par titre / mission / programme, recettes, dette de l'État | **non inspecté** : 403 depuis l'environnement de dev |
| data.economie.gouv.fr (Opendatasoft) | https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets | exécution annuelle par mission/programme, recettes fiscales par impôt | **non inspecté** : 403 |
| Eurostat `gov_10dd_edpt1` | https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/gov_10dd_edpt1 | PIB nominal, dette et déficit APU (Maastricht) | **non inspecté** : 403 |
| INSEE (comptes des APU) | https://www.insee.fr / https://bdm.insee.fr | alternative à Eurostat | **non inspecté** : 403 |

`data/raw/manifest.json` enregistre, pour chaque fichier, l'URL effective, la date de récupération (UTC), la taille et le sha256. La commande est idempotente : elle ne retélécharge pas une ressource data.gouv dont la date de modification annoncée n'a pas changé, et les écritures passent par un fichier temporaire.

Aucun chiffre n'est codé en dur. Tant qu'une donnée manque, elle reste un paramètre (`BaseYear`, `Assumptions`).

## Périmètres : ne pas mélanger

- **Dette de l'État** : dette financière de l'État (périmètre DGFiP). C'est le **seul** périmètre du simulateur (`projection.PERIMETRE`).
- **Dette publique au sens de Maastricht** : toutes les APU (État, ASSO, APUL). Elle sera affichée séparément, à titre de contexte, et n'entre dans aucun calcul du simulateur.

## Simulateur : formules (`budget_etat/projection.py`)

Notations pour l'année *t* (année de base *t = 0*, montants en M€ courants) :

- PIB tendanciel : `Y*_t = Y*_{t-1} · (1 + g_t)`
- Dépense tendancielle par ligne : `D*_{i,t} = D*_{i,t-1} · (1 + s_{i,t})` (taux propre à la ligne, sinon taux commun)
- Recette tendancielle par ligne : `R*_{j,t} = R_{j,0} · (Y*_t / Y_0)^ε` (ε = élasticité au PIB, 1 par défaut)
- Mesure sur une ligne ou un groupe (mission) : `Δ_t = φ_t · v` en M€, ou `φ_t · v% · base_t` en %, avec la montée en charge `φ_t = min(1, max(0, (t − début + 1) / N))`. Un montant en M€ visant un groupe est réparti au prorata des lignes.
- Multiplicateur (désactivé par défaut) : `Y_t = Y*_t + m_D · ΔD_t − m_R · ΔR_t`, puis recettes × `(Y_t / Y*_t)^ε`. L'effet porte sur le niveau de l'année, sans persistance.
- Taux apparent avec refinancement progressif :
  - roulement : `r_t = (1 − ρ) · r̃_{t-1} + ρ · m_t`, où ρ est la part du stock refinancée par an (≈ 1/maturité moyenne) et m_t le taux de marché ;
  - intérêts : `I_t = r_t · B_{t-1}` ;
  - déficit : `d_t = Σ D_{i,t} + I_t − Σ R_{j,t}` (> 0 = besoin de financement) ;
  - dette : `B_t = B_{t-1} + d_t + autres flux_t` ;
  - les émissions nettes de l'année entrent au taux de marché : `r̃_t = (B_{t-1}·r_t + e_t·m_t) / B_t` si `e_t > 0`, sinon `r̃_t = r_t`.
  - r_0 = I_0 / B_0 (taux apparent observé).
- `compare(ref, scénario)` donne les écarts de déficit, de dette, d'intérêts et de ratio dette/PIB (en points).

## Limites connues du modèle

- Modèle comptable annuel, sans bouclage macro complet. Le multiplicateur est un simple effet de niveau, sans persistance.
- Un seul taux de marché, sans courbe des taux. Pas d'inflation séparée (tout est en nominal) et pas d'OATi.
- Les flux de trésorerie, primes et décotes, et la variation des dépôts sont regroupés dans un paramètre exogène (« autres flux »).
- Recettes indexées sur le PIB avec une élasticité unique. Pas de comportement de base fiscale propre à chaque impôt.
- Les rapprochements de nomenclature mission/programme, et ce qui n'a pas pu être rapproché, seront documentés après inspection des fichiers.
