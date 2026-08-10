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
CONFIGURATION - ET MISE À JOUR
------------------------------------------------------------------------------
> [!CAUTION]
> Le changement du mot de passe est obligatoire pour le lancement du panel<br/>Il est possible de le changer depuis le code dans la variable `PASSWORD = "change-moi-STP"`
> Le changement du mot de passe est possible depuis un premier démarrage dans le Shell
> 
La mise à jour se fait directement depuis l'interface du panel et télécharge le code depuis github.com

> [!TIP]
> Il vous sera demander de rentrer mot de passe, vous pouvez donc en entrer un nouveau ou le laisser identique.
------------------------------------------------------------------------------
Accessibilité web
------------------------------------------------------------------------------
Il est possible d'accéder au panel depuis le web via l'adresse suivante: `https://luantipanel.local:8877/`
