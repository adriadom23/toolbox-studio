// Studio-shell-only i18n strings (design.md §5.1, plan.md Phase 4 step 18).
//
// Scope cut (design.md §2.5/§9): Studio's OWN UI is net-new and is NOT held
// to ToolBox's 5-locale-parity standing rule (that rule governs the ToolBox
// repo). Spanish is the maintained default; EN/FR/DE/IT are present so the
// vendored LanguageSwitcher has something to switch to, but are a best-effort
// scaffold, not a maintained parity surface. Any missing key falls back to
// English and then to the key itself (shared/i18n/index.js's t()).
//
// Integration with the vendored i18n engine: the engine (shared/i18n/index.js)
// reads from the single `messages` object exported by shared/i18n/messages.js
// (a byte-for-byte vendored file we must not edit). Rather than fork the
// engine, this module deep-merges Studio's `studio.*` subtree into that same
// imported object as an import-time side effect — so `useI18n().t("studio.x")`
// resolves everywhere without touching the vendored catalog file. main.js
// imports this module before mounting so the merge runs first.
import { messages as sharedMessages } from "../../shared/i18n/messages";

const studioMessages = {
  es: {
    studio: {
      shell: {
        wordmark: "Toolbox Studio",
      },
      list: {
        title: "Mis proyectos",
        subtitle: "Crea una herramienta usando las conexiones y los estilos de la Toolbox.",
        newProject: "Nuevo proyecto",
        empty: "Aún no hay proyectos. Pulsa «Nuevo proyecto» para empezar.",
        loadError: "No se pudieron cargar los proyectos: {error}",
        open: "Abrir",
        delete: "Eliminar",
        created: "Creado",
        modified: "Modificado",
        statusDraft: "Borrador",
        statusSubmitted: "Enviado",
        deleteTitle: "¿Eliminar «{name}»?",
        deleteText:
          "Se borrarán del disco las dos carpetas del proyecto (frontend y backend). Esta acción no se puede deshacer.",
        deleteConfirm: "Eliminar proyecto",
        deleteCancel: "Cancelar",
        deleteError: "No se pudo eliminar el proyecto: {error}",
      },
      newProject: {
        title: "Nuevo proyecto",
        nameLabel: "Nombre del proyecto",
        namePlaceholder: "p. ej. Dock Health Monitor",
        slugPreview: "Carpeta/módulo: {slug}",
        rule: "Empieza por una letra; solo minúsculas, números y guiones (a-z, 0-9, -).",
        invalid: "El nombre no es válido. {rule}",
        create: "Crear",
        cancel: "Cancelar",
        creating: "Creando…",
        createError: "No se pudo crear el proyecto: {error}",
        nameExists: "Ya existe un proyecto con ese nombre.",
      },
      editor: {
        back: "← Proyectos",
        files: "Archivos",
        refreshFiles: "Recargar archivos",
        save: "Guardar",
        saving: "Guardando…",
        saved: "Guardado ✓",
        saveError: "No se pudo guardar: {error}",
        openDevServer: "Abrir en servidor de desarrollo",
        componentLibrary: "📚 Biblioteca de componentes",
        submit: "Enviar código",
        loadError: "No se pudo abrir el proyecto: {error}",
        noFile: "Selecciona un archivo del árbol para editarlo.",
        editorFallback:
          "El resaltado de sintaxis no está disponible; usando un editor de texto simple.",
        unsavedHint: "Cambios sin guardar",
      },
      catalog: {
        title: "Biblioteca de componentes",
        subtitle:
          "Los {count} componentes compartidos que puedes importar en tu proyecto. Copia el fragmento y pégalo en tu .vue.",
        back: "← Volver al editor",
        importLabel: "Importar",
        exampleLabel: "Ejemplo",
        copy: "Copiar fragmento",
        copied: "¡Copiado!",
        livePreview: "Vista previa",
      },
      submit: {
        title: "Enviar código",
        intro: "Al enviar se empaqueta el proyecto y se notifica a los owners de la Toolbox.",
        willNotify: "Se notificará a: {owners}",
        noOwners: "No hay owners configurados.",
        bothDisabled:
          "⚠️ Email y Slack están desactivados en la configuración: nadie recibirá la notificación. El proyecto se empaquetará igualmente.",
        confirm: "Enviar código",
        cancel: "Cancelar",
        sending: "Enviando…",
        loadError: "No se pudo leer la configuración de owners: {error}",
        submitError: "No se pudo enviar el proyecto: {error}",
        done: "Proyecto enviado.",
        package: "Paquete: {path}",
        notifiedEmailOk: "Email enviado ✓",
        notifiedEmailFail: "Email no enviado",
        notifiedSlackOk: "Slack enviado ✓",
        notifiedSlackFail: "Slack no enviado",
        notifyWarning:
          "El proyecto se empaquetó, pero alguna notificación no se pudo entregar (revisa la configuración).",
      },
    },
  },
  en: {
    studio: {
      shell: { wordmark: "Toolbox Studio" },
      list: {
        title: "My projects",
        subtitle: "Build a tool using the Toolbox's connections and styles.",
        newProject: "New project",
        empty: "No projects yet. Click “New project” to get started.",
        loadError: "Could not load projects: {error}",
        open: "Open",
        delete: "Delete",
        created: "Created",
        modified: "Modified",
        statusDraft: "Draft",
        statusSubmitted: "Submitted",
        deleteTitle: "Delete “{name}”?",
        deleteText:
          "Both of the project's folders (frontend and backend) will be deleted from disk. This cannot be undone.",
        deleteConfirm: "Delete project",
        deleteCancel: "Cancel",
        deleteError: "Could not delete the project: {error}",
      },
      newProject: {
        title: "New project",
        nameLabel: "Project name",
        namePlaceholder: "e.g. Dock Health Monitor",
        slugPreview: "Folder/module: {slug}",
        rule: "Start with a letter; only lowercase letters, digits and hyphens (a-z, 0-9, -).",
        invalid: "The name is not valid. {rule}",
        create: "Create",
        cancel: "Cancel",
        creating: "Creating…",
        createError: "Could not create the project: {error}",
        nameExists: "A project with that name already exists.",
      },
      editor: {
        back: "← Projects",
        files: "Files",
        refreshFiles: "Reload files",
        save: "Save",
        saving: "Saving…",
        saved: "Saved ✓",
        saveError: "Could not save: {error}",
        openDevServer: "Open in dev server",
        componentLibrary: "📚 Component library",
        submit: "Send code",
        loadError: "Could not open the project: {error}",
        noFile: "Pick a file from the tree to edit it.",
        editorFallback: "Syntax highlighting unavailable; using a plain text editor.",
        unsavedHint: "Unsaved changes",
      },
      catalog: {
        title: "Component library",
        subtitle:
          "The {count} shared components you can import into your project. Copy the snippet and paste it into your .vue.",
        back: "← Back to editor",
        importLabel: "Import",
        exampleLabel: "Example",
        copy: "Copy snippet",
        copied: "Copied!",
        livePreview: "Preview",
      },
      submit: {
        title: "Send code",
        intro: "Submitting packages the project and notifies the Toolbox owners.",
        willNotify: "Will notify: {owners}",
        noOwners: "No owners configured.",
        bothDisabled:
          "⚠️ Email and Slack are both disabled in config: nobody will be notified. The project will still be packaged.",
        confirm: "Send code",
        cancel: "Cancel",
        sending: "Sending…",
        loadError: "Could not read the owner configuration: {error}",
        submitError: "Could not submit the project: {error}",
        done: "Project submitted.",
        package: "Package: {path}",
        notifiedEmailOk: "Email sent ✓",
        notifiedEmailFail: "Email not sent",
        notifiedSlackOk: "Slack sent ✓",
        notifiedSlackFail: "Slack not sent",
        notifyWarning:
          "The project was packaged, but a notification could not be delivered (check the config).",
      },
    },
  },
  fr: {
    studio: {
      shell: { wordmark: "Toolbox Studio" },
      list: {
        title: "Mes projets",
        subtitle: "Créez un outil avec les connexions et les styles de la Toolbox.",
        newProject: "Nouveau projet",
        empty: "Aucun projet pour l'instant. Cliquez sur « Nouveau projet » pour commencer.",
        loadError: "Impossible de charger les projets : {error}",
        open: "Ouvrir",
        delete: "Supprimer",
        created: "Créé",
        modified: "Modifié",
        statusDraft: "Brouillon",
        statusSubmitted: "Envoyé",
        deleteTitle: "Supprimer « {name} » ?",
        deleteText:
          "Les deux dossiers du projet (frontend et backend) seront supprimés du disque. Action irréversible.",
        deleteConfirm: "Supprimer le projet",
        deleteCancel: "Annuler",
        deleteError: "Impossible de supprimer le projet : {error}",
      },
      newProject: {
        title: "Nouveau projet",
        nameLabel: "Nom du projet",
        namePlaceholder: "p. ex. Dock Health Monitor",
        slugPreview: "Dossier/module : {slug}",
        rule: "Commencez par une lettre ; uniquement minuscules, chiffres et tirets (a-z, 0-9, -).",
        invalid: "Le nom n'est pas valide. {rule}",
        create: "Créer",
        cancel: "Annuler",
        creating: "Création…",
        createError: "Impossible de créer le projet : {error}",
        nameExists: "Un projet portant ce nom existe déjà.",
      },
      editor: {
        back: "← Projets",
        files: "Fichiers",
        refreshFiles: "Recharger les fichiers",
        save: "Enregistrer",
        saving: "Enregistrement…",
        saved: "Enregistré ✓",
        saveError: "Impossible d'enregistrer : {error}",
        openDevServer: "Ouvrir dans le serveur de dév",
        componentLibrary: "📚 Bibliothèque de composants",
        submit: "Envoyer le code",
        loadError: "Impossible d'ouvrir le projet : {error}",
        noFile: "Choisissez un fichier dans l'arborescence pour l'éditer.",
        editorFallback: "Coloration syntaxique indisponible ; éditeur de texte simple.",
        unsavedHint: "Modifications non enregistrées",
      },
      catalog: {
        title: "Bibliothèque de composants",
        subtitle:
          "Les {count} composants partagés importables dans votre projet. Copiez l'extrait dans votre .vue.",
        back: "← Retour à l'éditeur",
        importLabel: "Import",
        exampleLabel: "Exemple",
        copy: "Copier l'extrait",
        copied: "Copié !",
        livePreview: "Aperçu",
      },
      submit: {
        title: "Envoyer le code",
        intro: "L'envoi empaquette le projet et notifie les owners de la Toolbox.",
        willNotify: "Seront notifiés : {owners}",
        noOwners: "Aucun owner configuré.",
        bothDisabled:
          "⚠️ E-mail et Slack sont désactivés : personne ne sera notifié. Le projet sera quand même empaqueté.",
        confirm: "Envoyer le code",
        cancel: "Annuler",
        sending: "Envoi…",
        loadError: "Impossible de lire la configuration des owners : {error}",
        submitError: "Impossible d'envoyer le projet : {error}",
        done: "Projet envoyé.",
        package: "Paquet : {path}",
        notifiedEmailOk: "E-mail envoyé ✓",
        notifiedEmailFail: "E-mail non envoyé",
        notifiedSlackOk: "Slack envoyé ✓",
        notifiedSlackFail: "Slack non envoyé",
        notifyWarning:
          "Le projet a été empaqueté, mais une notification n'a pas pu être délivrée (vérifiez la config).",
      },
    },
  },
  de: {
    studio: {
      shell: { wordmark: "Toolbox Studio" },
      list: {
        title: "Meine Projekte",
        subtitle: "Baue ein Tool mit den Verbindungen und Stilen der Toolbox.",
        newProject: "Neues Projekt",
        empty: "Noch keine Projekte. Klicke auf „Neues Projekt“, um zu starten.",
        loadError: "Projekte konnten nicht geladen werden: {error}",
        open: "Öffnen",
        delete: "Löschen",
        created: "Erstellt",
        modified: "Geändert",
        statusDraft: "Entwurf",
        statusSubmitted: "Eingereicht",
        deleteTitle: "„{name}“ löschen?",
        deleteText:
          "Beide Projektordner (Frontend und Backend) werden von der Festplatte gelöscht. Nicht umkehrbar.",
        deleteConfirm: "Projekt löschen",
        deleteCancel: "Abbrechen",
        deleteError: "Projekt konnte nicht gelöscht werden: {error}",
      },
      newProject: {
        title: "Neues Projekt",
        nameLabel: "Projektname",
        namePlaceholder: "z. B. Dock Health Monitor",
        slugPreview: "Ordner/Modul: {slug}",
        rule: "Mit einem Buchstaben beginnen; nur Kleinbuchstaben, Ziffern und Bindestriche (a-z, 0-9, -).",
        invalid: "Der Name ist ungültig. {rule}",
        create: "Erstellen",
        cancel: "Abbrechen",
        creating: "Wird erstellt…",
        createError: "Projekt konnte nicht erstellt werden: {error}",
        nameExists: "Ein Projekt mit diesem Namen existiert bereits.",
      },
      editor: {
        back: "← Projekte",
        files: "Dateien",
        refreshFiles: "Dateien neu laden",
        save: "Speichern",
        saving: "Speichern…",
        saved: "Gespeichert ✓",
        saveError: "Konnte nicht gespeichert werden: {error}",
        openDevServer: "Im Dev-Server öffnen",
        componentLibrary: "📚 Komponentenbibliothek",
        submit: "Code senden",
        loadError: "Projekt konnte nicht geöffnet werden: {error}",
        noFile: "Wähle eine Datei im Baum zum Bearbeiten.",
        editorFallback: "Syntaxhervorhebung nicht verfügbar; einfacher Texteditor.",
        unsavedHint: "Nicht gespeicherte Änderungen",
      },
      catalog: {
        title: "Komponentenbibliothek",
        subtitle:
          "Die {count} geteilten Komponenten, die du importieren kannst. Kopiere das Snippet in deine .vue.",
        back: "← Zurück zum Editor",
        importLabel: "Import",
        exampleLabel: "Beispiel",
        copy: "Snippet kopieren",
        copied: "Kopiert!",
        livePreview: "Vorschau",
      },
      submit: {
        title: "Code senden",
        intro: "Beim Senden wird das Projekt verpackt und die Toolbox-Owner benachrichtigt.",
        willNotify: "Benachrichtigt werden: {owners}",
        noOwners: "Keine Owner konfiguriert.",
        bothDisabled:
          "⚠️ E-Mail und Slack sind deaktiviert: niemand wird benachrichtigt. Das Projekt wird trotzdem verpackt.",
        confirm: "Code senden",
        cancel: "Abbrechen",
        sending: "Senden…",
        loadError: "Owner-Konfiguration konnte nicht gelesen werden: {error}",
        submitError: "Projekt konnte nicht gesendet werden: {error}",
        done: "Projekt eingereicht.",
        package: "Paket: {path}",
        notifiedEmailOk: "E-Mail gesendet ✓",
        notifiedEmailFail: "E-Mail nicht gesendet",
        notifiedSlackOk: "Slack gesendet ✓",
        notifiedSlackFail: "Slack nicht gesendet",
        notifyWarning:
          "Das Projekt wurde verpackt, aber eine Benachrichtigung konnte nicht zugestellt werden (Konfiguration prüfen).",
      },
    },
  },
  it: {
    studio: {
      shell: { wordmark: "Toolbox Studio" },
      list: {
        title: "I miei progetti",
        subtitle: "Crea uno strumento usando le connessioni e gli stili della Toolbox.",
        newProject: "Nuovo progetto",
        empty: "Ancora nessun progetto. Clicca su «Nuovo progetto» per iniziare.",
        loadError: "Impossibile caricare i progetti: {error}",
        open: "Apri",
        delete: "Elimina",
        created: "Creato",
        modified: "Modificato",
        statusDraft: "Bozza",
        statusSubmitted: "Inviato",
        deleteTitle: "Eliminare «{name}»?",
        deleteText:
          "Verranno eliminate dal disco entrambe le cartelle del progetto (frontend e backend). Azione irreversibile.",
        deleteConfirm: "Elimina progetto",
        deleteCancel: "Annulla",
        deleteError: "Impossibile eliminare il progetto: {error}",
      },
      newProject: {
        title: "Nuovo progetto",
        nameLabel: "Nome del progetto",
        namePlaceholder: "es. Dock Health Monitor",
        slugPreview: "Cartella/modulo: {slug}",
        rule: "Inizia con una lettera; solo minuscole, cifre e trattini (a-z, 0-9, -).",
        invalid: "Il nome non è valido. {rule}",
        create: "Crea",
        cancel: "Annulla",
        creating: "Creazione…",
        createError: "Impossibile creare il progetto: {error}",
        nameExists: "Esiste già un progetto con questo nome.",
      },
      editor: {
        back: "← Progetti",
        files: "File",
        refreshFiles: "Ricarica i file",
        save: "Salva",
        saving: "Salvataggio…",
        saved: "Salvato ✓",
        saveError: "Impossibile salvare: {error}",
        openDevServer: "Apri nel dev server",
        componentLibrary: "📚 Libreria componenti",
        submit: "Invia codice",
        loadError: "Impossibile aprire il progetto: {error}",
        noFile: "Scegli un file dall'albero per modificarlo.",
        editorFallback: "Evidenziazione della sintassi non disponibile; editor di testo semplice.",
        unsavedHint: "Modifiche non salvate",
      },
      catalog: {
        title: "Libreria componenti",
        subtitle:
          "I {count} componenti condivisi che puoi importare nel progetto. Copia lo snippet nel tuo .vue.",
        back: "← Torna all'editor",
        importLabel: "Import",
        exampleLabel: "Esempio",
        copy: "Copia snippet",
        copied: "Copiato!",
        livePreview: "Anteprima",
      },
      submit: {
        title: "Invia codice",
        intro: "L'invio impacchetta il progetto e notifica gli owner della Toolbox.",
        willNotify: "Verranno notificati: {owners}",
        noOwners: "Nessun owner configurato.",
        bothDisabled:
          "⚠️ Email e Slack sono disattivati: nessuno verrà notificato. Il progetto verrà comunque impacchettato.",
        confirm: "Invia codice",
        cancel: "Annulla",
        sending: "Invio…",
        loadError: "Impossibile leggere la configurazione degli owner: {error}",
        submitError: "Impossibile inviare il progetto: {error}",
        done: "Progetto inviato.",
        package: "Pacchetto: {path}",
        notifiedEmailOk: "Email inviata ✓",
        notifiedEmailFail: "Email non inviata",
        notifiedSlackOk: "Slack inviato ✓",
        notifiedSlackFail: "Slack non inviato",
        notifyWarning:
          "Il progetto è stato impacchettato, ma una notifica non è stata consegnata (controlla la configurazione).",
      },
    },
  },
};

// Deep-merge Studio's subtree into each locale of the shared catalog, in
// place, so useI18n().t("studio.*") resolves against the same object the
// vendored engine reads. Only adds keys; never overwrites a vendored key.
function deepMergeInto(target, source) {
  for (const [key, value] of Object.entries(source)) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      if (!target[key] || typeof target[key] !== "object") target[key] = {};
      deepMergeInto(target[key], value);
    } else if (!(key in target)) {
      target[key] = value;
    }
  }
}

for (const [locale, subtree] of Object.entries(studioMessages)) {
  if (!sharedMessages[locale]) sharedMessages[locale] = {};
  deepMergeInto(sharedMessages[locale], subtree);
}

export { studioMessages };
