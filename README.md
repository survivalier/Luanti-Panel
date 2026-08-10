# Luanti Server Panel
Panneau web pour administrer un serveur Luanti (Minetest) :
- démarrer / arrêter le serveur
- voir la console en direct + envoyer des commandes
- gérer les mods (upload zip, clone git, suppression, activation dans world.mt)
- explorateur de fichiers cantonné au dossier des mods
- configuration guidée + brute de minetest.conf
- journal de débogage (debug.txt) colorisé
- aperçu des connexions réseau actives sur le port du serveur
Ne dépend que de la bibliothèque standard Python. `git` est appelé en
sous-processus uniquement pour la fonction "installer un mod depuis un dépôt",
et `ss`/`netstat` pour lister les connexions actives (aucune capture de
paquets, uniquement l'état de la table des sockets du système).
------------------------------------------------------------------------------
CONFIGURATION — modifie ces valeurs avant de lancer
------------------------------------------------------------------------------
Changer le mot de passe depuis
`PASSWORD = "change-moi-STP"`
La configuration du mot de passe, s'effectue automatiquement dans le shell au lancement
