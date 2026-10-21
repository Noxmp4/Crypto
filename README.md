# daybot — bot d'investissement crypto pour Coinbase Advanced

Bot Python **spot, sans levier, long uniquement**, pour Coinbase Advanced Trade (paires EUR).
Il **analyse** le marché (indicateurs et rapport rédigé par Claude), **conseille** (rapport
quotidien et questions depuis le site) et **investit** en trois poches indépendantes :

| Poche | Ce qu'elle fait | Par défaut |
|---|---|---|
| **Cœur DCA** | Achats hebdomadaires par ordres limites (frais maker). Elle achète d'abord l'actif sous-pondéré et **ne vend jamais**. | 100 €/mois, BTC 70 % / ETH 30 % |
| **Tendance** | Suivi de tendance journalier (cassure Donchian 20/10, SMA200), stop suiveur posé sur Coinbase | 300 €, BTC/ETH/SOL, 1 % de risque par trade |
| **Intraday** | Daytrading 15 min (v1) | **désactivée** : perdante à cause des frais |

Sur le VPS, il tourne 24/7 dans Docker, avec un **site web privé sur ton domaine Cloudflare**
(Tunnel + Access) : tableau de bord, analyses IA, trades, critères de passage en réel,
contrôles (pause, kill switch, rétrogradation, clôture). → **[DEPLOY.md](DEPLOY.md)**

> ⚠️ **À lire avant tout.**
> - Sur 2018–2026, la poche tendance est **légèrement positive mais très en dessous du simple
>   fait de conserver les actifs** (voir *Résultats*).
> - Le daytrading est perdant.
> - L'essentiel de la performance historique vient du **DCA**, c'est-à-dire de la détention.
>
> Ceci n'est pas un conseil en investissement.

## Comment l'argent réel est protégé

- **Paper d'abord, toujours** : chaque poche tourne en simulation (« ombre ») avant le réel, et
  continue en parallèle ensuite.
- **Passage en réel automatique, poche par poche**, seulement si *toutes* ces conditions sont
  vraies (`daybot/promotion.py`, page **Stratégies**) :
  - ton accord au déploiement : `DAYBOT_LIVE_CONFIRM=OUI`, une clé Coinbase, et un plafond
    `max_live_capital_eur` > 0 (0 par défaut) ;
  - au moins 7 jours de paper sans erreur pour le DCA, 30 jours pour la tendance ;
  - pour la tendance : profit factor hors échantillon ≥ 1,2 et drawdown ≤ 25 %, recalculés
    chaque dimanche.
- **Plafond d'euros réels** : le bot n'engage jamais plus que `max_live_capital_eur`, positions
  et DCA cumulés.
- **Stops posés sur Coinbase** dès le remplissage d'un achat : une position reste protégée si
  le VPS s'éteint. Une **réconciliation horaire** annule les ordres orphelins du bot et repose
  toute protection manquante.
- **L'IA ne peut que réduire le risque** :
  - elle peut diviser par deux ou couper la taille des positions de la poche tendance pendant
    24 h, et ajouter au plus trois fenêtres datées sans nouvelle entrée (48 h maximum chacune) ;
  - elle ne passe aucun ordre et ne touche pas au DCA ;
  - son budget est plafonné chaque mois (15 $ par défaut).
- **Clé Coinbase** : View + Trade uniquement, **jamais Transfer**, restreinte à l'IP du VPS.
- **Garde-fous** : 1 % de risque par trade, perte journalière max, drawdown max par poche,
  kill switch, pause, blackouts macro (le FOMC du 28/10 est préconfiguré).

## Analyse et conseils (API Claude)

- **Rapport quotidien** (7 h 30, heure de Paris), en deux appels :
  1. **recherche web** : actualité, ETF, agenda macro, avec les sources ;
  2. **synthèse structurée** :
     - le régime de marché ;
     - pour chaque paire, sa tendance, ses supports et ses résistances ;
     - les scénarios conditionnels, les conseils et les risques ;
     - les leviers de risque proposés, filtrés par les garde-fous ci-dessus.
- **Questions** depuis le site. Le contexte du portefeuille et du marché est joint
  automatiquement. 20 questions par jour au maximum.
- **Modèle** : `claude-opus-5-5`, avec repli serveur activé si le modèle refuse une requête
  (`fallbacks: "default"`). Coût estimé : environ 10 $ par mois.
- Les pages web lues par l'IA sont traitées comme de la **donnée**, jamais comme des
  instructions.

## Résultats (calculés le 21/10/2026 sur l'historique public Coinbase, frais du palier Intro)

**Tendance**, capital de 300 €, paramètres de manuel fixés à l'avance (non optimisés) :

| Période | Rendement | Annualisé | Drawdown max | Trades | Réussite | Profit factor |
|---|---|---|---|---|---|---|
| Référence 2018–2022 | +23,0 % | +4,2 % | −5,5 % | 42 | 60 % | 3,05 |
| **Hors échantillon 2023 → 10/2026** | **+5,6 %** | **+1,4 %** | −7,1 % | 62 | 42 % | **1,26** |
| Complet 2018 → 10/2026 | +29,9 % | +3,0 % | −7,1 % | 104 | 49 % | 1,78 |

Sur la même période hors échantillon, conserver les actifs aurait rapporté **BTC +376 %,
ETH +95 %, SOL +917 %**.

La poche tendance risque peu et rapporte peu. Elle passe de justesse le seuil de promotion
(profit factor ≥ 1,2). Sans frais, elle aurait fait +11,9 % hors échantillon : les frais en
mangent la moitié.

**DCA**, 100 €/mois, BTC 70 % / ETH 30 %, du 01/2019 au 10/2026 :

| Variante | Investi | Valeur | Gain |
|---|---|---|---|
| DCA simple (défaut) | 9 392 € | 38 179 € | +306 % |
| DCA « tilt » (×1,5 sous la SMA200) | 11 079 € | 46 551 € | +320 % |

Le tilt investit plus quand les prix sont bas, donc les montants investis diffèrent. Il reste
désactivé par défaut (`[sleeves.dca] tilt`).

**Intraday 15 min** (v1), du 23/04 au 20/10/2026 : **−9,9 %** net, −2,3 % sans frais.
Désactivé.

Pour reproduire : `python -m daybot research` (tendance et DCA) et
`python -m daybot backtest --days 180` (intraday). Le bot relance la recherche chaque dimanche
et l'affiche sur la page **Stratégies**.

## Installation sur macOS (Intel ou Apple Silicon)

Python 3.9 ou plus récent. Python 3.10+ est nécessaire pour l'analyse IA, car le SDK
`anthropic` 1.x l'exige. Homebrew est recommandé : `brew install python@3.12`.

```bash
git clone https://github.com/Noxmp4/crypto.git
cd crypto
scripts/install_macos.sh        # crée .venv, installe les dépendances, lance les tests
```

### Utilisation sur macOS

```bash
scripts/run_macos.sh research               # backtests tendance (hors échantillon) + DCA
scripts/run_macos.sh run                    # bot complet en local (paper ombre + IA si clé)
DAYBOT_WEB_DEV=1 scripts/run_macos.sh web   # site sur http://127.0.0.1:8080, sans authentification
scripts/run_macos.sh promotion              # critères de passage en réel
scripts/run_macos.sh backtest --days 180    # intraday v1
touch data/STOP                             # kill switch : plus aucune nouvelle entrée
```

Lance `run` et `web` dans deux onglets du Terminal. Le mode développement du site, sans
authentification, est refusé ailleurs que sur `127.0.0.1`.

Spécificités macOS :

- **Pas de mise en veille** : `run_macos.sh run|paper|live` lance le bot sous `caffeinate -is`.
  Un MacBook capot fermé sans écran externe s'endort quand même. Pour du 24/7, préfère le VPS.
- **Notifications** : chaque achat, chaque sortie et chaque arrêt s'affichent dans le Centre
  de notifications (`[macos] notifications`).
- **Trousseau** : `scripts/run_macos.sh keychain-import ~/Downloads/cdp_api_key.json` range la
  clé Coinbase dans le Trousseau. Supprime ensuite le JSON.
- **Arrêt et reprise** : `Ctrl+C` termine la tâche en cours puis s'arrête. Au relancement, tout
  reprend depuis `data/` : positions, solde paper, plus haut de capital, perte du jour,
  prochaines exécutions.

## Installation sur Linux / autres

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
python -m daybot research
```

## Commandes

| Commande | Rôle |
|---|---|
| `run` | Processus permanent : paper ombre, réel automatique, DCA, IA, réconciliation, sauvegardes |
| `web [--host --port]` | Site (FastAPI). Exige Cloudflare Access ou `DAYBOT_WEB_DEV=1` en local |
| `research` | Backtests tendance (2018–2022 / 2023 → aujourd'hui) et DCA simple contre tilt |
| `promotion` | Critères de passage en réel, poche par poche |
| `status` | Positions et P&L des journaux |
| `export-tax [--out]` | CSV des cessions crypto → EUR (formulaire 2086) |
| `backtest`, `paper`, `live` | Intraday v1 (boucle 15 min), conservé |
| `keychain-import`, `keychain-forget` | Clé Coinbase dans le Trousseau macOS |

## Architecture

```
daybot/
  runner.py        processus permanent : poches × (paper, live), commandes du site, tâches
  scheduler.py     planificateur persistant (prochaines exécutions dans SQLite)
  engine.py        moteur commun backtest / paper / live pour les poches à position
  strategies/      base.py (interface), trend.py (Donchian + chandelier), intraday.py (v1)
  sleeves/dca.py   DCA : répartition sans vente, ordres post-only, reprix, bascule taker
  promotion.py     critères paper → réel, plafond d'euros réels
  analyst.py       API Claude : recherche web + synthèse structurée, garde-fous, budget
  risk.py          sizing, garde-fous, contrôles (pause, blocages, multiplicateur IA)
  broker.py        PaperBroker (simulation prudente) / LiveBroker (SDK Coinbase, réconciliation)
  journal.py       SQLite (trades, lots DCA, commandes, analyses, événements, capital)
  market.py        API publique Coinbase + cache SQLite incrémental
  backtest.py      backtests (même moteur que le live), recherche hors échantillon, DCA
  web/             FastAPI + Jinja2 : pages, auth Cloudflare Access, graphiques SVG
  credentials.py   clés : fichier, variables d'environnement, Trousseau macOS, secrets Docker
deploy/setup_vps.sh   préparation d'un VPS Ubuntu 24.04
Dockerfile, docker-compose.yml   bot + web + cloudflared
```

## Limites connues

- **Ordres réels jamais testés avec de vrais fonds** : ni le stop-limit de Coinbase, ni ses
  ordres bracket. Dans un krach rapide, un stop-limit peut ne pas être exécuté. Commence avec
  un petit plafond (`max_live_capital_eur` = 20 à 50 €) et surveille les premiers trades.
- **Simulation optimiste sur les fills** : la simulation remplit un ordre limite dès que le prix
  le traverse, sans file d'attente.
- **Rattrapage limité** : après un arrêt, seule la dernière bougie est traitée, sans rattrapage
  des signaux manqués. Un DCA manqué le lundi s'exécute au redémarrage.
- **Fiscalité incomplète** : le CSV fiscal liste les cessions. Le calcul français
  (art. 150 VH bis) dépend de la valeur **globale** du portefeuille à chaque cession : à faire
  valider.
- **Analyse IA sans garantie** : elle peut se tromper, d'où les garde-fous et le fait qu'elle
  ne passe aucun ordre.
- **Pas d'alerte push** : il n'y a pas encore d'alerte e-mail ou téléphone. Il faut consulter
  le site.
- **Ceci n'est pas un conseil en investissement.** Les cryptoactifs sont très volatils : tu peux
  perdre tout ou partie du capital engagé.
