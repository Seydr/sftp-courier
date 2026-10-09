# -*- coding: utf-8 -*-
# =============================================================================
# SFTP Courier
# Service de surveillance et d'envoi automatique de fichiers vers un SFTP.
#
# Ce script est concu pour fonctionner comme un service Windows (via NSSM).
# Il gere le verrou mono-instance, la stabilite des fichiers, l'archivage
# local, la purge des archives et la reprise sur erreur.
#
# Configuration : voir config.example.json
# =============================================================================

import paramiko
import logging
import time
import os
import sys
import json
import signal
import posixpath
import shutil
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime


# =============================================================================
#  GESTION DU SIGNAL D ARRET
# =============================================================================

_arreter = False


def _handler_signal(signum, frame):
    global _arreter
    _arreter = True
    logging.info("Signal arret recu (%s). Arret propre en cours...", signum)
    logging.debug("Flag _arreter passe a True.")


signal.signal(signal.SIGINT, _handler_signal)
signal.signal(signal.SIGTERM, _handler_signal)


# =============================================================================
#  CHARGEMENT ET VALIDATION DE LA CONFIGURATION
# =============================================================================

CLES_OBLIGATOIRES = {
    "watch_folder": str,
    "archive_folder": str,
    "file_filter": str,
    "remote_path": str,
    "hostname": str,
    "port": int,
    "username": str,
    "known_hosts_file": str,
    "check_interval": (int, float),
    "temps_attente": (int, float),
    "retries_stabilite": int,
    "retries": int,
    "log_file": str,
    "log_level": str,
    "lock_file": str,
    "activer_logs": bool,
}


def charger_config(config_file):
    if not os.path.isfile(config_file):
        raise FileNotFoundError("Fichier de configuration introuvable : %s" % config_file)
    with open(config_file, "r", encoding="utf-8") as f:
        return json.load(f)


def valider_config(config):
    erreurs = []

    for cle, type_attendu in CLES_OBLIGATOIRES.items():
        if cle not in config:
            erreurs.append("Cle manquante : '%s'" % cle)
            continue
        if not isinstance(config[cle], type_attendu):
            erreurs.append(
                "Cle '%s' : type attendu %s, recu %s"
                % (cle, type_attendu, type(config[cle]))
            )

    if "stability_confirmations" in config and not isinstance(config["stability_confirmations"], int):
        erreurs.append("'stability_confirmations' doit etre un entier")
    if "archive_retention_hours" in config and not isinstance(config["archive_retention_hours"], (int, float)):
        erreurs.append("'archive_retention_hours' doit etre numerique")
    if "sending_age_minimum" in config and not isinstance(config["sending_age_minimum"], (int, float)):
        erreurs.append("'sending_age_minimum' doit etre numerique")
    if "purge_every_n_cycles" in config and not isinstance(config["purge_every_n_cycles"], int):
        erreurs.append("'purge_every_n_cycles' doit etre un entier")

    if "check_interval" in config and config["check_interval"] <= 0:
        erreurs.append("'check_interval' doit etre > 0")
    if "temps_attente" in config and config["temps_attente"] <= 0:
        erreurs.append("'temps_attente' doit etre > 0")
    if "retries" in config and config["retries"] < 1:
        erreurs.append("'retries' doit etre >= 1")

    confirmations = config.get("stability_confirmations", 2)
    if confirmations < 1:
        erreurs.append("'stability_confirmations' doit etre >= 1")

    if "retries_stabilite" in config and config["retries_stabilite"] < (confirmations + 1):
        erreurs.append(
            "'retries_stabilite' doit etre >= %s (confirmations=%s + 1)"
            % (confirmations + 1, confirmations)
        )

    if "archive_retention_hours" in config and config["archive_retention_hours"] < 0:
        erreurs.append("'archive_retention_hours' doit etre >= 0")
    if "sending_age_minimum" in config and config["sending_age_minimum"] < 0:
        erreurs.append("'sending_age_minimum' doit etre >= 0")
    if "purge_every_n_cycles" in config and config["purge_every_n_cycles"] < 0:
        erreurs.append("'purge_every_n_cycles' doit etre >= 0")

    if not config.get("password") and not config.get("private_key_path"):
        erreurs.append("Aucune authentification : 'password' ou 'private_key_path' requis")

    if config.get("private_key_path") and not os.path.isfile(config["private_key_path"]):
        erreurs.append("Cle privee introuvable : %s" % config["private_key_path"])

    if config.get("watch_folder") and not os.path.isdir(config["watch_folder"]):
        erreurs.append("Dossier surveille introuvable : %s" % config["watch_folder"])

    if config.get("known_hosts_file") and not os.path.isfile(config["known_hosts_file"]):
        erreurs.append("Fichier known_hosts introuvable : %s" % config["known_hosts_file"])

    if erreurs:
        raise ValueError("Configuration invalide :\n  - " + "\n  - ".join(erreurs))


# =============================================================================
#  LOGGER
# =============================================================================

def setup_logger(config):
    log_file = config["log_file"]
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    if config.get("activer_logs", True):
        handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            interval=1,
            backupCount=config.get("log_backup_count", 30),
            encoding="utf-8",
        )
        handler.suffix = "%Y-%m-%d.log"

        niveaux = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "CRITICAL": logging.CRITICAL,
        }
        level = niveaux.get(config["log_level"].upper(), logging.INFO)

        logging.basicConfig(
            level=level,
            handlers=[handler],
            format="%(asctime)s - %(levelname)s - %(message)s",
        )
        logging.info("======================================================================")
        logging.info("Demarrage du service SFTP Courier")
        logging.info("PID             : %s", os.getpid())
        logging.info("Paramiko        : %s", getattr(paramiko, "__version__", "unknown"))
        logging.info("Surveillance de : %s", config["watch_folder"])
        logging.info("Archives        : %s", config["archive_folder"])
        logging.info("Filtre          : %s", config["file_filter"])
        logging.info("Serveur SFTP    : %s:%s", config["hostname"], config["port"])
        logging.info("Repertoire dist.: %s", config["remote_path"])
        logging.info("======================================================================")
        logging.debug("Niveau de log actif : %s", config["log_level"].upper())
        logging.debug("Fichier de log : %s", log_file)
        logging.debug("Intervalle de scan : %s s", config["check_interval"])
        logging.debug("Retries connexion SFTP : %s", config["retries"])
        logging.debug("Retries stabilite fichier : %s", config["retries_stabilite"])
        logging.debug("Confirmations stabilite : %s", config.get("stability_confirmations", 2))
        logging.debug("Temps attente stabilite : %s s", config["temps_attente"])
        logging.debug("Retention Archives : %s h", config.get("archive_retention_hours", 48))
        logging.debug("Age mini .sending orphelin : %s s", config.get("sending_age_minimum", 300))
        logging.debug("Purge Archives tous les %s cycles", config.get("purge_every_n_cycles", 1000))
    else:
        logging.disable(logging.CRITICAL)


# =============================================================================
#  VERROU MONO-INSTANCE
# =============================================================================

def verifier_instance_unique(lock_file):
    lock_dir = os.path.dirname(lock_file)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)

    try:
        f = open(lock_file, "w")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        f.write("PID=%s - Demarre le %s\n" % (os.getpid(), datetime.now()))
        f.flush()
        return f
    except (IOError, OSError) as e:
        print("ERREUR : une autre instance tourne deja (%s). Arret." % e)
        sys.exit(1)


# =============================================================================
#  CONNEXION SFTP
# =============================================================================

def connecter_sftp(config):
    logging.debug(
        "Sequence de connexion SFTP vers %s:%s (max %s tentatives).",
        config["hostname"], config["port"], config["retries"]
    )

    for tentative in range(config["retries"]):
        logging.debug("Tentative SFTP %s/%s...", tentative + 1, config["retries"])
        try:
            ssh = paramiko.SSHClient()
            ssh.load_host_keys(config["known_hosts_file"])
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

            if config.get("private_key_path"):
                logging.debug("Auth par cle privee : %s", config["private_key_path"])
                ssh.connect(
                    config["hostname"],
                    config["port"],
                    config["username"],
                    key_filename=config["private_key_path"],
                    timeout=30,
                )
            else:
                logging.debug("Auth par mot de passe (user=%s)", config["username"])
                ssh.connect(
                    config["hostname"],
                    config["port"],
                    config["username"],
                    password=config["password"],
                    timeout=30,
                )

            transport = ssh.get_transport()
            if transport is not None:
                transport.set_keepalive(30)
                logging.debug("Keepalive SSH active (30 s).")

            sftp = ssh.open_sftp()
            logging.info("Connexion SFTP reussie.")
            return ssh, sftp

        except Exception as e:
            logging.error(
                "Erreur connexion SFTP (tentative %s/%s) : %s",
                tentative + 1,
                config["retries"],
                e,
            )
            if tentative < config["retries"] - 1:
                time.sleep(5)
            else:
                logging.error("Echec de toutes les tentatives de connexion.")
                return None, None


def connexion_sftp_active(ssh, sftp):
    try:
        if ssh is None or sftp is None:
            return False

        transport = ssh.get_transport()
        if transport is None or not transport.is_active():
            return False

        canal = sftp.get_channel()
        if canal is None or canal.closed:
            return False

        return True
    except Exception:
        return False


# =============================================================================
#  VERIFICATION DE STABILITE DU FICHIER SOURCE
# =============================================================================

def attendre_fichier_stable(chemin_local, temps_attente, retries, confirmations=2):
    precedent = None
    nb_ok = 0

    for i in range(retries):
        try:
            st = os.stat(chemin_local)
            courant = (st.st_size, st.st_mtime_ns)
        except OSError as e:
            logging.debug("Stabilite %s : stat impossible (%s)", chemin_local, e)
            return False

        logging.debug(
            "Stabilite %s : check %s/%s -> (size=%s, mtime_ns=%s)",
            chemin_local, i + 1, retries, courant[0], courant[1]
        )

        if courant == precedent:
            nb_ok += 1
            logging.debug("Stabilite %s : confirmation %s/%s", chemin_local, nb_ok, confirmations)
            if nb_ok >= confirmations:
                logging.debug("Stabilite %s : fichier STABLE.", chemin_local)
                return True
        else:
            nb_ok = 0
            precedent = courant

        time.sleep(temps_attente)

    logging.warning(
        "Fichier %s : taille/mtime instable apres %s verifications.",
        chemin_local,
        retries,
    )
    return False


# =============================================================================
#  UPLOAD DIRECT VERS SFTP
# =============================================================================

def envoyer_fichier(sftp, chemin_local, remote_path, nom_fichier):
    chemin_distant = posixpath.join(remote_path, nom_fichier)

    logging.debug("Upload : source = %s", chemin_local)
    logging.debug("Upload : cible = %s", chemin_distant)

    taille_locale = os.path.getsize(chemin_local)
    logging.debug("Upload : taille locale = %s octets", taille_locale)

    t_debut = time.time()
    sftp.put(chemin_local, chemin_distant, confirm=False)
    duree = time.time() - t_debut

    logging.debug(
        "Upload : PUT termine en %.3f s (%.0f octets/s)",
        duree,
        taille_locale / duree if duree > 0 else 0,
    )


# =============================================================================
#  CONSTRUCTION D UN NOM D ARCHIVE UNIQUE
# =============================================================================

def construire_nom_archive_unique(archive_folder, nom_fichier):
    candidat = os.path.join(archive_folder, nom_fichier)
    if not os.path.exists(candidat):
        return candidat

    base, ext = os.path.splitext(nom_fichier)
    suffixe = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(archive_folder, "%s_%s%s" % (base, suffixe, ext))


# =============================================================================
#  DEPLACEMENT ROBUSTE
# =============================================================================

def deplacer_fichier(source, destination):
    try:
        os.replace(source, destination)
        return True
    except OSError:
        try:
            shutil.move(source, destination)
            return True
        except Exception as e:
            logging.error("Echec deplacement %s -> %s : %s", source, destination, e)
            return False


# =============================================================================
#  TRAITEMENT D UN FICHIER
# =============================================================================

def traiter_fichier(chemin_local, config, sftp):
    nom_fichier = os.path.basename(chemin_local)
    chemin_sending = chemin_local + ".sending"

    logging.info("Traitement de : %s", nom_fichier)
    logging.debug("Source : %s", chemin_local)
    logging.debug("Sending : %s", chemin_sending)

    t_debut = time.time()
    confirmations = config.get("stability_confirmations", 2)

    if not attendre_fichier_stable(
        chemin_local,
        config["temps_attente"],
        config["retries_stabilite"],
        confirmations,
    ):
        logging.warning("Fichier %s non stable, report au prochain cycle.", nom_fichier)
        return False

    try:
        os.rename(chemin_local, chemin_sending)
    except OSError as e:
        logging.error("Impossible de renommer %s : %s", nom_fichier, e)
        return False

    try:
        envoyer_fichier(sftp, chemin_sending, config["remote_path"], nom_fichier)
        logging.info("Fichier envoye avec succes : %s", nom_fichier)
    except Exception as e:
        logging.error("Echec de l envoi de %s : %s", nom_fichier, e)
        logging.debug("Restauration du nom original pour retry...")
        try:
            os.rename(chemin_sending, chemin_local)
            logging.info("Fichier restaure : %s (sera retente)", nom_fichier)
        except OSError as e2:
            logging.error("Impossible de restaurer %s : %s", nom_fichier, e2)
        return False

    try:
        os.makedirs(config["archive_folder"], exist_ok=True)
        chemin_archive = construire_nom_archive_unique(config["archive_folder"], nom_fichier)
        if deplacer_fichier(chemin_sending, chemin_archive):
            logging.info("Fichier archive : %s", os.path.basename(chemin_archive))
        else:
            logging.error("Fichier envoye mais archivage echoue : %s", nom_fichier)
            return False
    except OSError as e:
        logging.error("Erreur creation Archives pour %s : %s", nom_fichier, e)
        return False

    duree = time.time() - t_debut
    logging.debug("Fichier %s traite en %.3f s.", nom_fichier, duree)
    return True


# =============================================================================
#  PURGE DES ARCHIVES
# =============================================================================

def purger_archives(config):
    archive_folder = config["archive_folder"]
    retention_s = config.get("archive_retention_hours", 48) * 3600
    maintenant = time.time()
    nb_purges = 0

    if not os.path.isdir(archive_folder):
        return 0

    try:
        for f in os.listdir(archive_folder):
            chemin = os.path.join(archive_folder, f)
            if not os.path.isfile(chemin):
                continue
            try:
                age = maintenant - os.path.getmtime(chemin)
                if age > retention_s:
                    os.remove(chemin)
                    logging.info("Archive purgee (>%sh) : %s", retention_s // 3600, f)
                    nb_purges += 1
            except OSError as e:
                logging.debug("Erreur purge archive %s : %s", f, e)
    except Exception as e:
        logging.error("Erreur purge Archives : %s", e)

    return nb_purges


# =============================================================================
#  NETTOYAGE AU DEMARRAGE
# =============================================================================

def nettoyage_demarrage(config):
    watch_folder = config["watch_folder"]
    archive_folder = config["archive_folder"]
    age_sending = config.get("sending_age_minimum", 300)

    logging.debug("Nettoyage demarrage : watch=%s, archive=%s", watch_folder, archive_folder)

    try:
        os.makedirs(archive_folder, exist_ok=True)
    except OSError as e:
        logging.error("Impossible de creer Archives %s : %s", archive_folder, e)
        return

    nb_incertains = 0

    try:
        for f in os.listdir(watch_folder):
            if not f.endswith(".sending"):
                continue

            chemin_sending = os.path.join(watch_folder, f)
            try:
                age = time.time() - os.path.getmtime(chemin_sending)
            except OSError:
                continue

            if age < age_sending:
                logging.info("Orphelin .sending recent ignore : %s", f)
                continue

            nom_incertain = "%s_incertain_%s" % (
                f, datetime.now().strftime("%Y%m%d_%H%M%S")
            )
            chemin_incertain = os.path.join(archive_folder, nom_incertain)

            if deplacer_fichier(chemin_sending, chemin_incertain):
                logging.warning(
                    "Orphelin .sending ambigu deplace en Archives : %s -> %s",
                    f, nom_incertain
                )
                nb_incertains += 1
    except Exception as e:
        logging.error("Erreur nettoyage .sending : %s", e)

    nb_purges = purger_archives(config)

    logging.info(
        "Nettoyage demarrage : %s .sending ambigu(s), %s archive(s) purge(s)",
        nb_incertains, nb_purges
    )


# =============================================================================
#  BOUCLE DE SURVEILLANCE
# =============================================================================

def surveiller(config):
    _lock_handle = None
    ssh = None
    sftp = None

    try:
        _lock_handle = verifier_instance_unique(config["lock_file"])
        setup_logger(config)
        nettoyage_demarrage(config)

        logging.debug("Connexion SFTP initiale...")
        ssh, sftp = connecter_sftp(config)
        if sftp is None:
            logging.critical("Impossible de se connecter au SFTP. Arret.")
            sys.exit(1)

        extension_filtre = config["file_filter"].lstrip("*").upper()
        logging.debug("Extension filtree : '%s'", extension_filtre)

        numero_cycle = 0
        purge_tous_les_n = config.get("purge_every_n_cycles", 1000)

        while not _arreter:
            numero_cycle += 1
            logging.debug("--- Debut cycle #%s ---", numero_cycle)

            if not connexion_sftp_active(ssh, sftp):
                logging.warning("Connexion SFTP perdue. Reconnexion...")
                try:
                    if sftp is not None:
                        sftp.close()
                except Exception:
                    pass
                try:
                    if ssh is not None:
                        ssh.close()
                except Exception:
                    pass

                ssh, sftp = connecter_sftp(config)
                if sftp is None:
                    logging.critical("Reconnexion SFTP impossible. Arret en erreur.")
                    raise RuntimeError("Reconnexion SFTP impossible")
            else:
                logging.debug("Verification connexion SFTP : OK")

            try:
                tous_fichiers = os.listdir(config["watch_folder"])
                logging.debug("Dossier scanne : %s entrees.", len(tous_fichiers))

                fichiers = []
                for f in tous_fichiers:
                    if not f.upper().endswith(extension_filtre):
                        continue
                    chemin = os.path.join(config["watch_folder"], f)
                    if os.path.isfile(chemin):
                        fichiers.append(chemin)
                        logging.debug("Fichier retenu : %s", f)
            except Exception as e:
                logging.error("Erreur de lecture dossier %s : %s", config["watch_folder"], e)
                time.sleep(config["check_interval"])
                continue

            try:
                fichiers.sort(key=lambda p: (os.path.getmtime(p), os.path.basename(p)))
            except OSError:
                fichiers.sort(key=os.path.basename)

            if fichiers:
                noms = [os.path.basename(f) for f in fichiers]
                logging.info("%s fichier(s) a traiter : %s", len(fichiers), noms)
                for chemin in fichiers:
                    if _arreter:
                        logging.debug("Arret demande, on stoppe.")
                        break
                    try:
                        traiter_fichier(chemin, config, sftp)
                    except Exception as e:
                        logging.error(
                            "Erreur traitement %s : %s",
                            os.path.basename(chemin),
                            e,
                            exc_info=True,
                        )
            else:
                logging.debug("Aucun fichier a traiter dans ce cycle.")

            if purge_tous_les_n > 0 and numero_cycle % purge_tous_les_n == 0:
                logging.debug("Purge periodique des Archives (cycle #%s)...", numero_cycle)
                purger_archives(config)

            logging.debug("--- Fin cycle #%s ---", numero_cycle)
            time.sleep(config["check_interval"])

    finally:
        logging.info("Arret du service...")
        try:
            if sftp is not None:
                sftp.close()
        except Exception:
            pass
        try:
            if ssh is not None:
                ssh.close()
        except Exception:
            pass
        try:
            if _lock_handle is not None:
                _lock_handle.close()
        except Exception:
            pass
        logging.info("Service arrete proprement.")


# =============================================================================
#  POINT D ENTREE
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.json"
        )

    try:
        config = charger_config(config_path)
    except Exception as e:
        print("ERREUR : chargement de la config impossible : %s" % e)
        sys.exit(1)

    try:
        valider_config(config)
    except ValueError as e:
        print("ERREUR : %s" % e)
        sys.exit(1)

    try:
        surveiller(config)
    except Exception as e:
        try:
            logging.critical("Erreur fatale : %s", e, exc_info=True)
        except Exception:
            print("ERREUR FATALE : %s" % e)
        sys.exit(1)