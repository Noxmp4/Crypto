# Déployer daybot sur un VPS (24/7) avec un site sur ton domaine Cloudflare

Résultat : le bot tourne en permanence dans Docker. Le site `https://bot.<ton-domaine>` n'est
accessible qu'à toi : il passe par un **Cloudflare Tunnel**, donc aucun port n'est ouvert sur le
VPS, et par **Cloudflare Access**, qui demande un code reçu par e-mail.

```
Toi ──HTTPS──► Cloudflare Access (code e-mail) ──► Tunnel ──► web:8080 (lecture + commandes)
                                                                  │ SQLite partagé (data/)
                                                     bot (Coinbase, Claude, planificateur)
```

Compte environ 1 h la première fois.

## 1. Le VPS

- **Système** : Ubuntu 24.04 LTS, 2 vCPU, 2 à 4 Go de RAM, 20 Go de disque.
- **Région** : de préférence l'UE, ton compte Coinbase étant géré par Coinbase Luxembourg.
- **Accès** : ajoute ta **clé SSH** à la création du serveur.
- **IP** : note l'adresse IP **fixe** du VPS. Elle servira à restreindre la clé API Coinbase.

Depuis ton Mac :

```bash
ssh root@IP_DU_VPS
curl -fsSLO https://raw.githubusercontent.com/Noxmp4/crypto/main/deploy/setup_vps.sh
less setup_vps.sh          # relis ce qu'il fait
bash setup_vps.sh
```

Le script installe Docker depuis le dépôt officiel et les mises à jour de sécurité
automatiques. Il crée l'utilisateur `daybot` et coupe la connexion SSH par mot de passe
(seulement si une clé est en place). Il configure le pare-feu pour n'accepter que SSH en
entrée, puis clone le dépôt dans `/opt/daybot`. Il ne démarre rien.

> Si le dépôt GitHub est **privé**, le `git clone` demandera une authentification. Crée un
> *fine-grained token* GitHub en lecture seule sur ce dépôt, puis relance le script ainsi :
> `DAYBOT_REPO=https://<token>@github.com/Noxmp4/crypto.git bash setup_vps.sh`.

## 2. Cloudflare : le tunnel

Dans le tableau de bord Cloudflare, ouvre **Zero Trust**. La première fois, choisis un nom
d'équipe (*team name*) et l'offre gratuite. Ensuite :

1. **Networks → Tunnels → Create a tunnel** : type *Cloudflared*, nom `daybot`.
2. À l'étape *Install connector*, copie le **jeton**, la longue chaîne après `--token`. Sur le VPS :
   ```bash
   sudo nano /opt/daybot/.env        # TUNNEL_TOKEN=<le jeton>
   ```
3. **Public hostname** :
   - *Subdomain* : `bot`, et choisis ton domaine.
   - *Service* : type `HTTP`, URL `web:8080`.

## 3. Cloudflare : la connexion (Access)

1. **Access → Applications → Add an application → Self-hosted**.
   - Nom : `daybot`.
   - *Session duration* : 24 h.
   - Domaine : `bot.<ton-domaine>`.
2. **Policy** :
   - Nom : `moi` ; *Action* : **Allow** ;
   - *Include* : **Emails**, avec ton adresse ;
   - méthode de connexion : **One-time PIN**. C'est la méthode par défaut : un code reçu par e-mail.
3. Une fois l'application créée, copie son **Application Audience (AUD) Tag**, visible dans
   *Overview* ou *Basic information*.
4. Ton **team domain** s'affiche dans *Settings → Custom pages* (ou *Team domain*). Il a la
   forme `monequipe.cloudflareaccess.com`.
5. Sur le VPS :
   ```bash
   sudo nano /opt/daybot/config.toml
   # [web]
   # cf_team_domain = "monequipe.cloudflareaccess.com"
   # cf_aud = "<AUD tag>"
   ```

Le site vérifie lui-même le jeton envoyé par Cloudflare (signature, audience, émetteur). Sans
ces deux valeurs, il **refuse tout accès**.

## 4. Les clés

| Fichier sur le VPS | Contenu | Quand |
|---|---|---|
| `/opt/daybot/secrets/anthropic_api_key` | Ta clé API Anthropic (`sk-ant-…`) | Pour l'analyse IA |
| `/opt/daybot/secrets/cdp_api_key.json` | Le JSON téléchargé sur portal.cdp.coinbase.com | Seulement pour passer en réel |

**Clé Coinbase** : sur <https://portal.cdp.coinbase.com/>, crée une clé **Advanced Trade**.
- Permissions **View** et **Trade** uniquement. **Jamais Transfer.**
- **Liste blanche d'IP** : l'adresse du VPS seulement.
- Idéalement, crée un **portefeuille dédié** au bot et mets son UUID dans `portfolio_id`
  (`config.toml`).

Pour écrire un secret (exemple avec la clé Anthropic) :

```bash
cd /opt/daybot
sudo sh -c 'cat > secrets/anthropic_api_key'     # colle la clé, puis Entrée et Ctrl+D
sudo chown 10001:10001 secrets/* && sudo chmod 400 secrets/*
```

## 5. Démarrer

```bash
cd /opt/daybot
sudo -u daybot docker compose up -d --build
sudo -u daybot docker compose ps            # bot et web doivent passer « healthy »
sudo -u daybot docker compose logs -f bot   # Ctrl+C pour quitter les logs (le bot continue)
```

Ouvre `https://bot.<ton-domaine>` : Cloudflare te demande ton e-mail, puis le code reçu.

Au premier démarrage, le bot télécharge l'historique et calcule le backtest (moins d'une
minute). Il commence ensuite le **paper**. Les premières courbes apparaissent au bout de
quelques heures, avec un point par heure.

## 6. Passer en réel (quand tu le décides)

Le réel ne s'active **que si toutes ces conditions sont réunies** :

1. `DAYBOT_LIVE_CONFIRM=OUI` dans `/opt/daybot/.env` ;
2. la clé Coinbase dans `secrets/cdp_api_key.json` ;
3. un plafond `max_live_capital_eur` supérieur à 0 dans `config.toml`. C'est le nombre maximum
   d'euros réels que le bot peut engager, positions et achats DCA cumulés ;
4. des EUR disponibles sur le compte ou le portefeuille dédié. Approvisionne-le par virement
   SEPA, le moins cher ;
5. les critères de la page **Stratégies** :
   - **DCA** : au moins 7 jours de paper sans erreur ;
   - **tendance** : au moins 30 jours de paper sans erreur, avec un profit factor hors
     échantillon ≥ 1,2 et un drawdown ≤ 25 %.

Après avoir modifié `.env` ou `config.toml` : `sudo -u daybot docker compose up -d`.

À tout moment, depuis la page **Contrôle** du site :
- **Pause** : plus de nouvelles entrées ;
- **Kill switch** : arrêt de toutes les entrées ;
- **Rétrograder** une poche : retour en paper ;
- **Clôturer** une position.

Les stops déjà posés restent actifs chez Coinbase, même si le VPS s'éteint.

## 7. Exploitation

| Besoin | Commande (dans `/opt/daybot`) |
|---|---|
| Mettre à jour | `sudo -u daybot git pull && sudo -u daybot docker compose up -d --build` |
| Arrêter le bot seulement | `sudo -u daybot docker compose stop bot` |
| Tout arrêter | `sudo -u daybot docker compose down` |
| Logs | `sudo -u daybot docker compose logs --tail 200 bot` |
| Critères de promotion | `sudo -u daybot docker compose exec bot python -m daybot --config /app/config.toml promotion` |
| Export fiscal (2086) | Bouton sur la page **Trades** du site |

**Sauvegardes** : chaque nuit, une copie des bases va dans `data/backups/AAAA-MM-JJ/` (14 jours
conservés). Copie-les régulièrement **hors du VPS**. Depuis ton Mac :
`rsync -a daybot@IP_DU_VPS:/opt/daybot/data/backups/ ~/daybot-backups/`.

**Régénérer `requirements.lock`** après un changement de `requirements.txt`. Dans un venv
Python 3.12 sous Linux :
`pip install -r <(grep -v -E '^(pytest|httpx)' requirements.txt) && pip freeze > requirements.lock`.

## 8. Sécurité : ce qui est en place

- Aucun port entrant à part SSH (par clé). Le site n'est joignable que par le tunnel.
- Le site vérifie le jeton Cloudflare Access en plus de Cloudflare. Il ajoute une protection
  CSRF sur chaque action, une politique CSP stricte, et une case « Je confirme » sur toute
  action de trading.
- Le conteneur `web` ne reçoit **aucune** clé Coinbase ou Anthropic. Il dépose des commandes
  que le bot exécute.
- Les conteneurs tournent en utilisateur non-root, avec un système de fichiers en lecture
  seule (sauf `data/`), sans aucune capability Linux.
- La clé Coinbase n'a pas le droit de retirer des fonds et ne fonctionne que depuis l'IP du VPS.
- L'IA ne peut que **réduire** le risque. Elle ne passe aucun ordre, et son budget est plafonné
  chaque mois.
