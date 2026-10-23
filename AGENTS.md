# Guía neutral para agentes y editores

Toolbox Studio es una aplicación local-first con Vue 3/Vite y Python/FastAPI-ASGI. Estas reglas no dependen de Kiro, Orcha ni de ningún editor: cualquier agente o persona debe seguirlas.

- Trabaja en el checkout existente y conserva los cambios del usuario. No edites `backend/vendor` a mano; los adaptadores propios van fuera de ese directorio.
- Mantén código de herramientas en `frontend/src/projects/<slug>` y `backend/app/projects/<slug>`.
- Usa exclusivamente `.venv` en la raíz. Instala Python desde los locks con hashes y frontend con `npm ci`.
- No guardes secretos, `.env*`, `backend/config/studio.config.json` ni datos de `backend/data/runtime` en Git.
- El perfil local debe funcionar sin boto3, credenciales ni red AWS. Git remoto, CodeCommit y AWS son opcionales y nunca deben asumirse.
- `frontend/src/studio/StudioShell.vue` es el propietario exclusivo del único control oficial **Enviar**. Una herramienta no puede añadirlo, ocultarlo ni renderizarlo dentro del iframe de preview.
- Trata build, validación backend y preview como ejecución de código no confiable. El subprocess local aplica límites, pero no es un sandbox fuerte; un perfil compartido debe usar proveedores aislados y fallar cerrado si faltan.
- El snapshot solo admite las mitades `frontend`/`backend` del slug y shared administrativo sellado. Excluye siempre `.git`, `.aws`, `.env*`, credenciales, configuración privada, claves/certificados, `node_modules`, venvs, caches, `dist`, runtime, logs, temporales y ZIP previos; el paquete final no incluye shared, harness, locks, preview ni logs.
- Flujo obligatorio: crear/editar/probar y guardar; pulsar **Enviar** para snapshot, build y ZIP reproducible; revisar manifiesto/logs/checksum y comentar; aprobar y publicar explícitamente. Git genérico es válido, CodeCommit solo es opcional con acceso existente y nunca hay commit, push, PR, merge, despliegue ni auto-merge implícito.

Antes de entregar cambios ejecuta, desde la raíz:

```powershell
.venv\Scripts\python -m pytest backend\tests -q
npm --prefix frontend run test:unit -- --run
npm --prefix frontend run lint
npm --prefix frontend run build
.venv\Scripts\python backend\scripts\smoke_submissions.py
```

Consulta `README.md` para operación y recuperación, y `docs/aws-submissions.es.md` para la preparación AWS opcional.
