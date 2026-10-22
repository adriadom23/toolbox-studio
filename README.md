# Toolbox Studio

Toolbox Studio lets a developer build their own tool using the same
connections (SCC, Mercury, PerfectMile, QuickSight, SharePoint, Slack,
email, and more), the same Vue component library, and the same visual
theme the main Toolbox already uses — without touching the Toolbox
repository itself. When the tool is ready, the developer clicks **"Enviar
código"**; that packages the project and notifies the Toolbox owners, who
integrate it into the main Toolbox by hand.

Toolbox Studio is its own small Flask + Vue application, independent from
the Toolbox repo it vendors code from. See `VENDORING.md` for exactly
what was copied, from where, and how to re-sync it later.

## Running it locally

These commands deliberately mirror Toolbox's own `python -m app.main` /
`npm run dev` commands, so anyone already familiar with Toolbox needs
almost no new instructions.

### Backend

Create the virtual environment at the **repository root** (one level up
from `backend/`). This mirrors Toolbox's own layout: `frontend/scripts/
dev.mjs` looks for the interpreter at `<repo-root>/.venv/Scripts/
python.exe`, so a root `.venv` is what lets `npm run dev` boot the
backend for you (see the Frontend section below).

```powershell
# from the ToolboxStudio repo root
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements.txt
cd backend
..\.venv\Scripts\python -m app.main
```

Serves on `http://127.0.0.1:5000`. `GET /api/health` confirms it booted;
`GET /api/projects` lists every scaffolded project on disk. (The
`python -m app.main` command must run from inside `backend/` so the
`app` package resolves, exactly as in Toolbox's own backend.)

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

`npm run dev` boots the backend first if it isn't already running —
using the root `.venv` created above (see `frontend/scripts/dev.mjs`) —
then starts Vite on `http://127.0.0.1:5173`. If you prefer to run the
backend yourself, start it first (Backend section above) and `npm run
dev` will reuse the already-healthy server. Use `npm run build` for a
production bundle and `npm run preview` to serve that bundle locally.

## The create → edit → submit workflow

1. **Create a project.** From the Studio dashboard, click "New project"
   (or `POST /api/projects` with `{"name": "my-tool"}`) and give it a
   name. Studio scaffolds a starter `.vue` file (using the vendored
   components and theme) and a starter Flask blueprint — both already
   wired up, with zero manual registration steps.
2. **Edit it.** Open the project in Studio's built-in code editor (file
   tree + CodeMirror), or point any external editor at
   `frontend/src/projects/<name>/` and `backend/app/projects/<name>/`.
   Browse the Component Catalog inside Studio for what's available to
   import from `shared/components/` and `vendor/integrations/`.
3. **Check it live.** Use "Open in dev server" to open the project's
   `.vue` file standalone, with the real theme applied, on Vite's own
   dev server (full hot-reload, no sandboxed preview to fight with). Hit
   your project's backend routes at
   `http://127.0.0.1:5000/api/projects/<name>/app/<route>`.
4. **Submit it.** When it's ready, click "Enviar código". Studio zips
   the project's frontend and backend folders and notifies the configured
   owners (email and/or Slack) that a project is ready to review. A
   submitted project can still be opened and edited afterward — Studio
   does not track integration status; that happens by hand, in the
   Toolbox repo, on the owners' side.

## What's vendored from Toolbox, and how to re-sync it

See [`VENDORING.md`](VENDORING.md) for the full source-path list, file
counts, the snapshot date, and the per-module vendor/slim/stub decision
table. Re-syncing is a manual, periodic task — there is no automated
tooling that keeps Studio's copy of Toolbox's code in sync.
