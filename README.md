# SFTP Courier

> Service Python qui surveille un dossier (local ou partagé) et envoie automatiquement les fichiers vers un serveur SFTP.

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)]()

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Déploiement en service Windows](#déploiement-en-service-windows)
- [Comportement en cas d'erreur](#comportement-en-cas-derreur)
- [Dépannage](#dépannage)
- [Licence](#licence)

## Fonctionnalités

- **Surveillance continue** d'un dossier local ou partage UNC
- **Upload SFTP** avec reconnexion automatique et keepalive SSH
- **Verrou mono-instance** : impossible de lancer deux instances en parallèle
- **Détection de fin d'écriture** : attend que le fichier soit stable (taille + mtime)
- **Archivage local** : les fichiers envoyés sont déplacés dans `Archives/`
- **Purge automatique** des archives après une durée configurable
- **Reprise sur erreur** : en cas d'échec d'envoi, le fichier est restauré pour retry
- **Compatible drop zone** : aucune vérification distante post-upload
- **Logs rotatifs** avec mode DEBUG activable

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Service (NSSM)                                             │
│  - Verrou mono-instance                                     │
│  - Une connexion SFTP avec keepalive                        │
│  - Traitement sequentiel                                    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼  Pour chaque fichier detecte
┌─────────────────────────────────────────────────────────────┐
│  1. Attendre stabilite (taille + mtime + confirmations)     │
│  2. Renommer localement  fichier  ->  fichier.sending       │
│  3. Upload SFTP vers le serveur distant                     │
│  4. Deplacer vers Archives/ (nom unique si collision)       │
└─────────────────────────────────────────────────────────────┘
```

## Installation

### Prérequis

- Python 3.9+
- [NSSM](https://nssm.cc/) (pour l'installation en service Windows)

### 1. Cloner le projet

```bash
git clone https://github.com/VOTRE_USER/sftp-courier.git
cd sftp-courier
```

### 2. Installer les dépendances

```bash
pip install -r requirements.txt
```

### 3. Générer le `known_hosts`

```bash
ssh-keyscan -t ecdsa sftp.example.com > known_hosts
```

### 4. Configurer

```bash
cp config.example.json config.json
```

Éditer `config.json` avec vos valeurs (chemins, identifiants SFTP, etc.).

### 5. Test manuel

```bash
python sftp_courier.py config.json
```

Déposez un fichier correspondant au filtre dans `watch_folder` et vérifiez les logs.

## Configuration

### Surveillance

| Clé | Type | Description |
| :--- | :--- | :--- |
| `watch_folder` | string | Dossier à surveiller (local ou UNC) |
| `archive_folder` | string | Dossier d'archivage après envoi réussi |
| `file_filter` | string | Filtre de fichiers (ex: `*.txt`, `*.BL`) |
| `check_interval` | number | Intervalle de scan en secondes |

### Archivage

| Clé | Type | Description |
| :--- | :--- | :--- |
| `archive_retention_hours` | number | Durée de conservation (défaut: 48) |
| `purge_every_n_cycles` | int | Fréquence de purge en nombre de cycles |
| `sending_age_minimum` | number | Âge mini d'un `.sending` pour être traité comme orphelin |

### Stabilité

| Clé | Type | Description |
| :--- | :--- | :--- |
| `temps_attente` | number | Délai entre 2 vérifications de stabilité |
| `retries_stabilite` | int | Nombre max de vérifications |
| `stability_confirmations` | int | Confirmations consécutives requises |

### SFTP

| Clé | Type | Description |
| :--- | :--- | :--- |
| `hostname` | string | Serveur SFTP |
| `port` | int | Port (défaut: 22) |
| `username` | string | Utilisateur |
| `password` | string | Mot de passe (si pas de clé privée) |
| `private_key_path` | string ou null | Chemin vers la clé privée SSH |
| `remote_path` | string | Répertoire distant cible |
| `known_hosts_file` | string | Fichier known_hosts |
| `retries` | int | Nombre de tentatives de connexion |

### Logs

| Clé | Type | Description |
| :--- | :--- | :--- |
| `activer_logs` | bool | Active ou non les logs |
| `log_file` | string | Chemin du fichier de log |
| `log_level` | string | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `log_backup_count` | int | Nombre de jours d'historique |

### Divers

| Clé | Type | Description |
| :--- | :--- | :--- |
| `lock_file` | string | Fichier de verrou mono-instance |

## Déploiement en service Windows

Une fois le test manuel validé, installez le service avec NSSM :

```cmd
nssm install SftpCourier "C:\Python311\python.exe" "C:\Path\sftp_courier.py" "C:\Path\config.json"
nssm set SftpCourier AppDirectory "C:\Path"
nssm set SftpCourier AppStdout "C:\Path\logs\stdout.log"
nssm set SftpCourier AppStderr "C:\Path\logs\stderr.log"
nssm set SftpCourier AppRotateFiles 1
nssm set SftpCourier AppRotateOnline 1
nssm set SftpCourier AppRotateBytes 10485760
nssm set SftpCourier AppExit Default Restart
nssm set SftpCourier AppRestartDelay 5000
nssm set SftpCourier Start SERVICE_AUTO_START
nssm start SftpCourier
```

### Commandes utiles

| Action | Commande |
| :--- | :--- |
| Démarrer | `nssm start SftpCourier` |
| Arrêter | `nssm stop SftpCourier` |
| Redémarrer | `nssm restart SftpCourier` |
| Voir le statut | `nssm status SftpCourier` |
| Éditer la config | `nssm edit SftpCourier` |
| Supprimer | `nssm remove SftpCourier confirm` |

## Comportement en cas d'erreur

| Situation | Comportement |
| :--- | :--- |
| Fichier en cours d'écriture | Attend la stabilité (max ~8 s) |
| Coupure réseau pendant upload | Restaure le fichier en `.BL` pour retry |
| Coupure SFTP prolongée | Reconnexion auto (3 tentatives) |
| Échec définitif de reconnexion | Arrêt en erreur → NSSM redémarre |
| Crash pendant un upload | Le `.sending` est déplacé dans `Archives/*_incertain_*` |

## Dépannage

### Le service ne démarre pas

Vérifier que `config.json` est valide et que le `known_hosts_file` existe. Consulter `logs\stdout.log` et `logs\stderr.log`.

### Le dossier n'est pas trouvé (UNC)

Le compte de service doit avoir accès au partage. Se connecter une fois manuellement avec le compte de service pour mettre en cache les identifiants UNC.

### Les fichiers ne sont pas envoyés

Vérifier dans les logs :
- La connexion SFTP est-elle réussie ?
- Le filtre `file_filter` correspond-il bien aux fichiers déposés ?
- Y a-t-il des erreurs de stabilité répétées ?

### Activer le mode DEBUG

Dans `config.json` :
```json
"log_level": "DEBUG"
```

Puis redémarrer le service. Attention : le mode DEBUG génère beaucoup de volume.

## Licence

MIT — voir le fichier [LICENSE](LICENSE).

---

*Développé à l'origine pour un besoin d'envoi automatique de Bons de Livraison, ce script est générique et peut être adapté à tout flux de fichiers.*