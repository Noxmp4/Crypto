# daybot — bot de daytrading spot pour Coinbase Advanced

Bot Python **long-only, spot, sans levier** pour Coinbase Advanced Trade (paires EUR).
Il tourne en trois modes qui partagent le même moteur : **backtest**, **paper** (simulation
en temps réel), **live** (ordres réels).

> ⚠️ **À lire avant tout** : sur l'historique Coinbase d'avril à octobre 2026, la stratégie
> fournie **perd de l'argent**, frais compris (voir *Résultats du backtest*). Ce dépôt est un
> cadre d'exécution et de gestion du risque solide, **pas une stratégie gagnante**. Ne passez
> pas en live tant qu'un backtest *et* plusieurs semaines de paper ne sont pas positifs.

## Ce qui a été repris du rapport et du compte

| Constat | Traduction dans le bot |
|---|---|
| Frais lus sur votre compte (palier *Intro*) : 0,25 % maker / 0,50 % taker | `[fees]`. Entrées en **limite post-only** (maker) ; un filtre exige que le TP couvre ≥ 3× les frais aller-retour |
| Marché en range avec biais prudent, altcoins plus fragiles | Filtre de régime : aucune entrée si la tendance du timeframe supérieur n'est pas haussière (le bot reste en EUR) |
| « Éviter le levier » | Spot uniquement, aucune vente à découvert, aucune option dérivés |
| Catalyseur binaire : la Fed le 28/10 | `blackout` : aucune nouvelle entrée du 28/10 16h UTC au 29/10 2h UTC |
| Risque de change USD | Paires **EUR** par défaut |
| Fiscalité FR (PFU 31,4 %, formulaire 2086) | Journal SQLite et `export-tax` : CSV de toutes les cessions crypto → EUR, avec prix et frais |
| Sécurité | Clé API « Trade » seulement (jamais « Transfer »), kill switch par fichier |

Formules de sizing et de R:R reprises du skill `crypto-spot-futures-analysis`.
La recherche de skills (`/find-skills`) n'a trouvé aucun skill dédié aux bots de trading ou à Coinbase.

## Stratégie (`daybot/strategy.py`)

Sur chaque bougie **clôturée** du timeframe signal (15 min par défaut) :

1. **Régime** : sur le timeframe tendance (1 h), close > EMA50 et EMA50 montante.
2. **Déclencheur** : croisement EMA9 > EMA21, ou rebond (mèche sous l'EMA21 et clôture au-dessus).
3. **Momentum** : RSI(14) entre 50 et 70.
4. **Ordre** : achat limite post-only à `close − 0,1·ATR`, annulé au bout de 2 bougies s'il n'est pas rempli.
5. **Sorties** posées sur l'exchange dès le remplissage (ordre *bracket*) :
   stop = entrée − 1,5·ATR, TP = entrée + 2R. Sortie au marché après 8 h (pas de swing involontaire).
6. **Filtre d'edge** : on ne trade pas si `(TP − entrée)/entrée < 3 × (maker + taker)`.

Pas de biais de look-ahead : une bougie n'est utilisée qu'après sa clôture, y compris celles du timeframe supérieur.

## Gestion du risque (`daybot/risk.py`)

| Garde-fou | Défaut |
|---|---|
| Risque par trade (perte au stop) | 0,5 % du capital |
| Taille max d'une position | 25 % du capital |
| Positions simultanées | 2 (1 par paire) |
| Trades par jour | 6 |
| Perte journalière max | −2 % → plus d'entrée jusqu'au lendemain (UTC) |
| Drawdown max | −10 % depuis le plus haut → arrêt des entrées |
| Ordre minimum | 10 € |
| Kill switch | `touch data/STOP` bloque toute nouvelle entrée |

## Installation sur macOS (Intel ou Apple Silicon)

Python 3.9 ou plus récent. Le `/usr/bin/python3` d'Apple (3.9) fonctionne. Homebrew est
recommandé (`brew install python@3.12`).

```bash
git clone https://github.com/Noxmp4/crypto.git
cd crypto
scripts/install_macos.sh        # crée .venv, installe les dépendances, lance les 16 tests
```

Le script choisit le Python le plus récent disponible et peut être relancé sans risque.
Si macOS bloque l'exécution : `chmod +x scripts/*.sh`.

### Utilisation sur macOS

```bash
scripts/run_macos.sh backtest --days 180   # historique public, aucune clé requise
scripts/run_macos.sh paper                 # temps réel simulé, 1000 € fictifs
scripts/run_macos.sh status                # positions et P&L du journal
scripts/run_macos.sh export-tax --out data/cessions_2026.csv
touch data/STOP                            # kill switch : plus aucune nouvelle entrée
```

Spécificités macOS :

- **Pas de mise en veille** : en mode `paper` ou `live`, `run_macos.sh` lance le bot sous
  `caffeinate -is`. Le Mac ne s'endort pas tant que le bot tourne, sur secteur. Un MacBook
  **capot fermé** sans écran externe s'endort quand même : laissez-le ouvert ou utilisez un Mac mini.
- **Notifications** : chaque achat rempli, chaque sortie (avec le P&L) et chaque arrêt du bot
  s'affichent dans le Centre de notifications. Pour les couper : `notifications = false` sous
  `[macos]` dans `config.toml`. La première notification peut demander une autorisation pour
  « Script Editor » ou le Terminal dans *Réglages Système → Notifications*.
- **Trousseau** : la clé API peut être stockée dans le Trousseau macOS plutôt qu'en fichier
  (voir *Passer en réel*).
- **Arrêt et reprise** : `Ctrl+C` arrête le bot. Au relancement, il reprend son état depuis
  `data/` : positions ouvertes, solde paper, plus haut de capital pour le drawdown, perte du jour.
- **Logs** : `data/daybot.log`, avec rotation à 5 Mo et 3 archives. Suivi en direct :
  `tail -f data/daybot.log`.
- Le bot peut être lancé depuis n'importe quel dossier : les chemins de `config.toml` sont
  relatifs à son emplacement.

Pour qu'il tourne après fermeture du Terminal, lancez-le dans `tmux` ou `screen`
(`brew install tmux`, puis `tmux new -s daybot` et `scripts/run_macos.sh paper`).
Pour vous détacher sans l'arrêter : `Ctrl+B` puis `D`.

## Installation sur Linux / autres

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
python -m daybot backtest --days 180      # mêmes commandes, avec « python -m daybot »
```

### Passer en réel (seulement après validation)

1. Sur <https://portal.cdp.coinbase.com/>, créez une clé API **Advanced Trade** avec les permissions
   **View + Trade** uniquement. N'activez **pas Transfer**. Restreignez-la à votre IP.
2. Si possible, créez un **portefeuille dédié** au bot dans Coinbase Advanced, avec uniquement le capital
   que vous acceptez de perdre, et mettez son UUID dans `portfolio_id`.
3. Approvisionnez-le en EUR. Votre compte ne contient aujourd'hui qu'environ 0,0018 ETH (≈ 4 €) :
   c'est en dessous du minimum de 10 € par ordre, donc le bot ne peut rien faire en l'état.
4. Rangez la clé. Sur macOS, mettez-la dans le **Trousseau**, puis supprimez le JSON :

```bash
scripts/run_macos.sh keychain-import ~/Downloads/cdp_api_key.json
rm ~/Downloads/cdp_api_key.json          # la clé n'existe plus qu'en Trousseau
# pour la retirer plus tard : scripts/run_macos.sh keychain-forget
```

   Si macOS demande l'accès au Trousseau au lancement, cliquez « Toujours autoriser ».
   Autres options, prioritaires sur le Trousseau : `export COINBASE_KEY_FILE=~/cdp_api_key.json`,
   ou `COINBASE_API_KEY` / `COINBASE_API_SECRET`.
5. Ouvrez `config.toml` (par exemple `open -e config.toml`) et remplacez `mode = "paper"` par
   `mode = "live"`.
6. Lancez :

```bash
DAYBOT_LIVE_CONFIRM=OUI scripts/run_macos.sh live --live
```

Le mode réel exige ces trois verrous en même temps. Le bot ne vend jamais d'actifs qu'il n'a pas
achetés lui-même : vos ETH existants ne sont pas touchés. Comme le stop et le TP sont posés côté
Coinbase, une position reste protégée même si le bot s'arrête. Au redémarrage, l'état est relu
depuis `data/journal.sqlite`.

Pour faire tourner le bot 24/7, utilisez un Mac branché qui ne se met pas en veille, avec `tmux`,
ou un petit VPS Linux.

## Résultats du backtest (au 20/10/2026, 1 000 € de départ, frais du palier Intro)

Période du 23/04 au 20/10/2026, sur les quatre paires :

| Variante | Rendement net | Sans frais (mêmes trades) | Max DD | Trades | Réussite | Frais |
|---|---|---|---|---|---|---|
| 15 min / filtre 1 h (défaut) | **−9,9 %** | −2,3 % | −10,2 % | 46 | 28 % | 73 € |
| 1 h / filtre 6 h | **−10,2 %** | +0,3 % | −10,2 % | 33 | 18 % | 56 € |
| *Buy & hold, même période* | BTC +11 %, ETH +10 %, SOL +29 %, XRP −3 % | | | | | |

Sur 60 jours (21/08 au 20/10), la variante par défaut fait −6,9 % (sans frais : +1,8 %).

**Lecture** : sans frais, la stratégie est à peu près à l'équilibre. Les frais du palier Intro
(≈ 0,75 % par aller-retour) en font une stratégie perdante. Les deux variantes ont atteint le
drawdown de −10 %, ce qui a déclenché l'arrêt automatique. C'est le comportement attendu du
garde-fou. Sur cette période, conserver les actifs a largement battu le trading.
Pour que le daytrading devienne viable, il faudrait un *edge* brut nettement supérieur aux frais.
Ne cherchez pas à optimiser les paramètres sur ces mêmes données : c'est du surapprentissage.

Reproduire : `python -m daybot backtest --days 180`.

## Architecture

```
daybot/
  config.py      chargement de config.toml
  market.py      bougies et spécifications produit (API publique Coinbase)
  indicators.py  EMA, RSI, ATR
  strategy.py    features + règles d'entrée
  risk.py        sizing + garde-fous
  broker.py      PaperBroker (simulation conservatrice) / LiveBroker (SDK coinbase-advanced-py)
  engine.py      moteur commun : une bougie clôturée → gestion des ordres → nouveau signal
  journal.py     SQLite (trades, état du moteur) et export fiscal CSV
  backtest.py    rejoue l'historique à travers le même moteur
  credentials.py clés API : variables d'environnement, sinon Trousseau macOS (keyring)
  notify.py      notifications macOS (osascript), sans effet ailleurs
  __main__.py    CLI
scripts/
  install_macos.sh   installation (venv + dépendances + tests)
  run_macos.sh       lancement (caffeinate pour paper/live)
```

Pour changer de stratégie, il suffit de remplacer `strategy.signal_at`. Risque, exécution,
journal et backtest restent inchangés.

## Limites connues

- Le comportement exact de l'ordre bracket Coinbase n'a pas été testé avec de vrais fonds : son stop
  se déclenche puis s'exécute en limite. En cas de krach rapide, l'ordre peut ne pas être rempli.
  Testez-le d'abord avec 10–20 € et surveillez les premiers trades.
- La simulation suppose qu'un ordre limite est rempli dès que le prix le traverse, sans file d'attente.
  La réalité est souvent un peu moins favorable.
- Le CSV fiscal liste les cessions, mais la plus-value française (art. 150 VH bis) dépend de la valeur
  **globale** de votre portefeuille à chaque cession. Complétez-le avec un outil de déclaration
  ou faites-le valider par un professionnel.
- Le support macOS a été testé sous Linux avec Python 3.9 (version de `/usr/bin/python3` sur macOS)
  et 3.13, mais pas sur un Mac physique. Les appels propres à macOS (`osascript`, `caffeinate`,
  Trousseau) sont couverts par des tests simulés. Lancez `scripts/install_macos.sh` puis un
  backtest pour valider sur votre machine.
- **Ceci n'est pas un conseil en investissement.** Le daytrading crypto fait perdre de l'argent à la
  majorité des particuliers, surtout quand les frais sont élevés.
