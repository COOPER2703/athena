# Plugin Google Workspace — glossaire

Vocabulaire Google qui n'appartient qu'au plugin `google-workspace`. Le vocabulaire du
contrat (Plugin, Tool de plugin, Instruction de plugin) et les termes du noyau restent au
glossaire racine ; `GLOSSARY-MAP.md` dit où trouver chaque contexte.

Le **Compte Google** se distingue du **Compte** (identité d'accès à la Console) et de
l'**Utilisateur** (personne dont Athena se souvient) ; le **Contact Google** se distingue du
**Contact** Matrix (une personne joignable par `user_id`).

**Compte Google**:
Un compte Google dont Athena lit les mails et l'agenda, relié au plugin `google-workspace` par
un libellé stable (`perso`, `pro`). Se distingue du **Compte** (identité d'accès à la Console),
de l'**Utilisateur** (personne dont Athena se souvient) et du **Client** (appareil).
_Avoid_: compte Workspace, compte Gmail, profil Google

**Événement d'agenda**:
Une entrée de l'agenda Google d'un Compte Google : début, fin, titre, lieu et participants
éventuels. Se distingue de l'**Événement** (fait externe poussé vers Athena).
_Avoid_: événement (seul), meeting, créneau

**Carnet de contacts**:
L'ensemble des **Contacts Google** d'un Compte Google, contacts enregistrés et contacts
mémorisés par Gmail réunis. N'est pas un annuaire : l'annuaire du domaine Workspace n'en
fait pas partie.
_Avoid_: répertoire, annuaire, contacts (seul)

**Contact Google**:
Une personne du carnet d'un **Compte Google**, réduite à un nom d'affichage et à ses
adresses e-mail. Un nom seul ne la résout pas, et elle peut porter plusieurs adresses :
c'est une recherche, pas une clé. Se distingue du **Contact** (Matrix).
_Avoid_: contact (seul), fiche, répertoire, annuaire

**Correspondance approximative**:
Le résultat d'une **recherche de contacts** dont la requête ne se retrouve telle quelle
dans aucun nom ni aucune adresse, mais s'en approche à une ou deux fautes d'orthographe
près, sur le nom ou la partie locale d'une adresse. Une telle correspondance est marquée
`approximate` dans le retour et se confirme à l'oral avant tout envoi ; elle ne se mélange
jamais à des correspondances exactes.
_Avoid_: recherche floue, fuzzy, correspondance partielle
