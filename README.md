# Toolbox Studio

Toolbox Studio es una plataforma local-first para crear y probar herramientas Vue/Python con Kiro, Orcha o cualquier editor. La interfaz mantiene disponible **Enviar código**: crea un snapshot seguro, compila frontend y backend, genera un ZIP reproducible con manifiesto y SHA-256, y lo coloca en una bandeja de revisión. Un propietario comenta, revisa, aprueba y publica de forma explícita; **no existe auto-merge**.

El perfil local no necesita boto3, credenciales ni red AWS. Un Git corporativo genérico es suficiente para colaborar. CodeCommit es opcional únicamente cuando la organización ya proporciona acceso; consulta [`docs/aws-submissions.es.md`](docs/aws-submissions.es.md).

## Requisitos e instalación reproducible (PowerShell)

- Windows PowerShell, Python 3.13 y Node/npm compatibles con los locks.
- Un único entorno Python en `<repo>\.venv`.
- Dependencias Python desde locks con hashes; no instalar desde los `.txt` abiertos para operar normalmente.
- Dependencias frontend con `npm ci`, no `npm install`.

```powershell
Set-Location C:\ruta\a\ToolboxStudio
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r backend\requirements.lock
.venv\Scripts\python -m pip install --require-hashes -r backend\requirements-dev.lock
npm --prefix frontend ci
```

`backend/requirements-aws.lock` es opcional y solo se instala para desarrollar adaptadores AWS deliberadamente. No se necesita para arrancar, probar ni completar el humo local.

## Configuración local, v1 y v2

La configuración privada es `backend/config/studio.config.json` y está ignorada por Git. Copia manualmente `backend/config/studio.config.example.json`; nunca sustituyas ni publiques una configuración privada existente.

```powershell
Copy-Item backend\config\studio.config.example.json backend\config\studio.config.json
```

La URL `server.public_base_url` debe ser absoluta y no puede contener credenciales, query, fragmento ni una representación Unicode ambigua. En `local` su host debe ser loopback; en `shared` usa HTTPS y su host debe aparecer explícitamente en `server.cors_origins`. La configuración actual usa `identity.mapping_version: 1`, `identity.mapping` para identidad externa→interna y `identity.roles` con `admin`, `reviewer` o `submitter`. En perfil `local`, `identity.user_id` identifica al actor durable y la clave correspondiente de `identity.roles` declara sus permisos; `identity.display_name` se usa solo para presentación y autor visible. Los defaults compatibles son `local-user` y `Propietario local`. También pueden definirse para una ejecución local con `STUDIO_LOCAL_USER_ID` y `STUDIO_LOCAL_DISPLAY_NAME`; ambos valores se validan, y ninguna petición puede suplantarlos. Variables admitidas: `STUDIO_CONFIG_PATH`, `STUDIO_PROFILE`, `STUDIO_HOST`, `STUDIO_PORT`, `STUDIO_DATABASE_PATH`, `STUDIO_LOCAL_USER_ID`, `STUDIO_LOCAL_DISPLAY_NAME` y `STUDIO_PROXY_HMAC_SECRET`; los secretos se inyectan en runtime, nunca se escriben en Git.

Los datos persistidos tienen migraciones SQLite versionadas (esquema v2) y se crean/actualizan al arrancar. La v1 de mapping de identidad y la v2 de almacenamiento son conceptos distintos. No edites la base directamente ni cambies `mapping_version` a un valor no soportado.

Ejemplo mínimo local no secreto:

```json
{
  "schema_version": 2,
  "server": {
    "profile": "local",
    "bind": "127.0.0.1",
    "port": 5000,
    "public_base_url": "http://127.0.0.1:5173",
    "cors_origins": ["http://127.0.0.1:5173"]
  },
  "identity": {
    "user_id": "local-user",
    "display_name": "Propietario local",
    "mapping_version": 1,
    "mapping": {},
    "roles": {
      "local-user": ["admin", "reviewer", "submitter"]
    },
    "trusted_peer_cidrs": ["127.0.0.1/32", "::1/128"]
  },
  "runtime": {
    "root": "backend/data/runtime",
    "database_path": "backend/data/runtime/submissions.sqlite3"
  },
  "preview": {
    "bind": "127.0.0.1",
    "origin": "http://127.0.0.1:5001",
    "allowed_origins": ["http://127.0.0.1:5001"]
  },
  "providers": {
    "artifact": "local",
    "build": "local-subprocess",
    "logs": "local",
    "notifications": ["local"],
    "publication": "local",
    "runtime": "local-subprocess"
  }
}
```

## Arranque conjunto

Desde la raíz:

```powershell
npm --prefix frontend run dev
```

El supervisor espera a que cada servicio esté listo y usa puertos fijos:

- API ASGI: `http://127.0.0.1:5000` (`/api/health`, `/api/ready` y `/docs`).
- Preview aislado: `http://127.0.0.1:5001`.
- Vue/Vite: `http://127.0.0.1:5173`.

Puede reutilizar una API o preview ya saludable, pero nunca los termina; al cerrar solo detiene procesos que él inició. Si un puerto contiene otro servicio, falla con un diagnóstico en vez de terminarlo. También se puede arrancar por separado:

```powershell
Set-Location backend
..\.venv\Scripts\python -m uvicorn app.asgi:app --host 127.0.0.1 --port 5000 --no-proxy-headers
# En otra consola, desde backend:
..\.venv\Scripts\python -m app.preview_main
# En otra consola, desde la raíz:
npm --prefix frontend exec vite -- --host 127.0.0.1 --port 5173 --strictPort
```

## Identidad y perfiles

En `local`, la API solo admite clientes loopback y crea una sesión para `identity.user_id` (por defecto `local-user`) con cookie CSRF; `identity.display_name` solo aparece como nombre de presentación y autor visible. El ID configurado gobierna roles, ownership, reservas y auditoría, y procede exclusivamente de configuración o de las variables locales documentadas, nunca del cuerpo ni de cabeceras del navegador. La UI gestiona la cookie y cabecera. En `shared`, una capa corporativa debe autenticar y firmar **cada petición** con HMAC sobre peer, método, raw path/query, SHA-256 del body, actor, roles, timestamp, nonce y request ID; no se emite una sesión reutilizable. Los nonces se reservan en SQLite para impedir replay tras reinicios.

El perfil `shared` falla antes de escuchar si la selección no es autoritativa: exige `providers.artifact=s3`, `providers.build=codebuild`, `providers.logs=cloudwatch`, `providers.runtime=isolated-gateway`, publicación `local` y uno o más canales únicos `ses`/`teams`; rechaza canales `local` o una lista vacía. También falla si falta `identity.proxy_hmac_secret_env`/`trusted_peer_cidrs`, las URLs públicas no son HTTPS, CORS no contiene orígenes exactos o `preview.origin` no figura explícitamente en `preview.allowed_origins`. El perfil `local` acepta exclusivamente filesystem/subprocess/log/notificación/publicación/runtime locales, no carga boto3 y rechaza cualquier selección remota con `config_invalid`; el contexto de Enviar expone los providers efectivos, no valores nominales distintos. En `local`, el origen preview debe usar el mismo host/puerto que su bind loopback y ser distinto del origen de Studio. `shared` deshabilita edición del checkout, preview de workspace, runtime local y el puente Flask. Instala el lock AWS opcional y configura contratos S3/CodeBuild/CloudWatch/SES o Teams solo en el entorno compartido; el ejemplo versionado permanece local y no contiene secretos. Los propietarios de revisión son una capacidad de aplicación, no credenciales Git/AWS.

La entrada estándar `app.asgi:app` solo arranca `shared` con los adaptadores administrativos incorporados y allowlisted: `build.network_preflight` debe ser exactamente `aws-vpc-endpoints-v1`, que verifica mediante EC2 todas las tablas efectivas de las subredes CodeBuild, rechaza rutas por Internet/NAT/Transit Gateway/peering/VPN/egress-only y exige endpoints S3/Logs/ECR con asociaciones, private DNS, security groups y policies no wildcard de mínimo privilegio; `runtime.gateway_url` debe ser HTTPS, su host debe figurar en `runtime.gateway_allowed_hosts` y `runtime.gateway_token_env` debe nombrar un secreto presente en el entorno. No se cargan imports o plugins indicados por usuarios. S3 y los inputs de CodeBuild fijan `VersionId` y checksum; el gateway debe implementar el contrato cerrado `/v1/projects/{slug}/app/{path}` y declarar aislamiento. Si falta cualquier capacidad o attestation, el arranque falla con `config_invalid` accionable. El proceso de preview deriva la cookie del origen configurado: HTTPS exige `__Host-tb_preview` con `Secure`, host-only y `Path=/`; HTTP loopback usa la cookie local sin `Secure`.

Los límites operativos se configuran en `limits`: cuerpos HTTP/editor/multipart, archivos y bytes de snapshot/artefacto/log, deadline total de build, concurrencia global/por proyecto, spool y espacio libre mínimo. La admisión reserva capacidad antes del trabajo costoso; saturación devuelve `429` y falta de almacenamiento `507` sin reservar versión adicional.

## Flujo crear → editar → preview → enviar → revisar

1. Crea una herramienta en **Nuevo proyecto**. El slug genera `frontend/src/projects/<slug>` y `backend/app/projects/<slug>`.
2. Edita en Studio o con cualquier editor/agente. Los paths de la API de edición son POSIX canónicos (`frontend/...` o `backend/...`): se rechazan backslash, `%`, segmentos vacíos/punto/traversal, aliases Unicode y dispositivos Windows. El guardado multiarchivo registra hashes y recovery durable; mientras una promoción no haya convergido, lectura, preview, runtime y envío responden `503 workspace_recovering`. No edites `backend/vendor`.
3. Abre preview. Studio muestra primero metadatos y una advertencia inerte; solo tras pulsar **Abrir preview** reserva una operación durable con `Idempotency-Key`, muestra sus fases mediante polling y solicita un grant de un solo uso cuando queda `ready`. Puedes salir mientras compila y reanudar el mismo intento sin duplicar el build. El snapshot se carga desde el origen aislado 5001; cerrar descarta el iframe y reabrir exige nuevo consentimiento.
4. Pulsa **Enviar código**. La API valida, compila, guarda logs, sella preview y empaqueta un ZIP reproducible.
5. La entrega aparece en la bandeja y detalle con estado, manifiesto, logs, comentarios, eventos, descarga y checksum.
6. Un revisor inicia revisión y aprueba, rechaza o solicita cambios. Rechazo/cambios requieren motivo. Si la aprobación selecciona explícitamente `local-index`, intenta publicar en ese mismo flujo; un fallo conserva `approved`, registra el efecto durable y permite retry autorizado.
7. Para targets sin autopublicación o cuando se aprueba con `none`, un administrador publica explícitamente mediante la acción separada. Publicar no fusiona Git ni despliega AWS.

## Bootstrap e importación heredada

Al arrancar, Studio migra la base, indexa proyectos válidos presentes en ambos árboles e importa metadatos necesarios. Las mitades incompletas, enlaces/reparse points y metadata inválida quedan como diagnósticos durables en `/api/ready`; se resuelven lógicamente cuando el checkout vuelve a ser válido, sin mover ni reescribir árboles. Los ZIP históricos bajo `backend/submissions` se consideran fuentes legacy inmutables: se indexan por ruta confinada, identidad de archivo, tamaño, mtime y SHA-256 con apertura no-follow; no se recorren junctions, no se reescriben ni se mueven. Un fallo de integridad los pone en cuarentena lógica y bloquea la descarga.

Para incorporar trabajo existente, coloca o clona manualmente el código bajo los dos directorios del mismo slug y conserva `project.meta.json`. Usa Git genérico para compartir el repositorio. No copies secretos, runtime ni `node_modules`; ejecuta instalación reproducible y `doctor` antes de enviar.

## Recuperación y doctor

El arranque ejecuta recuperación de entregas y guardados multiarchivo interrumpidos y reconcilia estados seguros/idempotentes. También reconcilia automáticamente cualquier publicación cuyo efecto externo ya figure `delivered` pero cuya entrega siga `approved`, sin repetir el side effect. Una entrega que ya persistió `snapshot_ref`, manifiesto y digest se reanuda exclusivamente desde esa snapshot sellada: nunca vuelve a capturar el workspace ni incorpora ediciones posteriores; si la referencia o sus hashes no verifican, termina con `sealed_snapshot_invalid` recuperable. `/api/ready` devuelve `503` con `submission_recovery_pending` durante el primer pase; un journal de workspace no convergido mantiene `503 workspace_recovering` en sus consumidores y aparece con un código seguro en readiness. Reintenta después de que readiness vuelva a `200`; si persiste, conserva el journal y revisa permisos/espacio sin editarlo ni borrarlo. Los artefactos, logs, previews, staging, publicaciones e SQLite viven bajo `backend/data/runtime` y están ignorados. No borres runtime para “arreglar” un estado: conserva evidencia y usa recuperación/reintento desde la API o UI. Una exportación CloudWatch que excede su timeout queda ambigua y no se relanza automáticamente mientras el intento siga vivo; una finalización tardía se reconcilia. Las fases se dividen por bytes en eventos dentro del límite de CloudWatch, conservando orden y el límite total de fase. SES y Teams mantienen efectos separados, visibles por canal en el detalle, y un retry autorizado solo reenvía el canal pendiente o fallido. Si el intento conserva un lease verificable, la API responde `409 effect_in_flight` con estado seguro y plazo de reconsulta; la UI deshabilita nuevos retries y hace polling hasta que termine o expire de forma segura. Los índices de publicación local y cualquier alias retenido quedan sellados en solo lectura y revalidados tras la promoción.

Ejecuta primero el diagnóstico de almacenamiento desde `backend` (el doctor solo propone/aplica marcas lógicas; nunca elimina objetos):

```powershell
Push-Location backend
$doctor = ..\.venv\Scripts\python -c "from pathlib import Path; from app.submissions.doctor import storage_plan; import json; print(json.dumps(storage_plan(Path('data/runtime/submissions.sqlite3'), Path('data/runtime'))))" | ConvertFrom-Json
$doctor | ConvertTo-Json -Depth 8
# Solo tras revisar el plan; aplica exclusivamente marcas lógicas:
..\.venv\Scripts\python -c "from pathlib import Path; from app.submissions.doctor import apply_logical_plan; import json; print(json.dumps(apply_logical_plan(Path('data/runtime/submissions.sqlite3'), Path('data/runtime'), '$($doctor.plan_hash)')))"
# Recovery seguro: reinicia la API con la configuración habitual.
# El lifespan construye los providers configurados y reanuda con artifact store y callback correctos.
Pop-Location
npm --prefix frontend run dev
```

Importa ZIP heredados en dos pasos, conservando el `plan_hash` emitido por el dry-run:

```powershell
Push-Location backend
..\.venv\Scripts\python -m app.submissions.legacy_import --dry-run --root submissions --database data/runtime/submissions.sqlite3
..\.venv\Scripts\python -m app.submissions.legacy_import --apply --plan-hash <PLAN_HASH> --root submissions --database data/runtime/submissions.sqlite3
Pop-Location
```

Comprueba espacio, permisos, integridad de objetos, referencias y migraciones. Ante corrupción, copia la evidencia conforme a la política interna y restaura desde backup; no edites SQLite ni ZIP a mano.

## Pruebas y verificación

Desde la raíz, en modo single-run:

```powershell
.venv\Scripts\python -m pytest backend\tests -q
npm --prefix frontend run test:unit -- --run
npm --prefix frontend run lint
npm --prefix frontend run build
.venv\Scripts\python backend\scripts\smoke_submissions.py
```

El humo usa configuración, proyectos y runtime aislados, levanta HTTP real local, y verifica crear→editar→preview→enviar/compilar/empaquetar→listar/detalle/log→comentar→iniciar revisión→aprobar/publicar local→descargar/SHA-256. Conserva su runtime ignorado bajo `backend/data/runtime/smoke` para diagnóstico y no carga boto3.

### Seguridad de dependencias frontend

A fecha 2026-10-23, `npm --prefix frontend audit --json` informa 6 incidencias (`3 moderate`, `1 high`, `2 critical`); `npm --prefix frontend audit --omit=dev --json` reduce el conjunto a Vite/esbuild (`1 moderate`, `1 high`, `0 critical`). Las dos críticas pertenecen exclusivamente a Vitest/tinypool, se ejecutan solo en pruebas single-run sobre código del checkout y nunca se sirven en runtime. Las incidencias Vite/esbuild afectan principalmente al servidor de desarrollo/launch-editor: Studio fija loopback/`strictPort`, no habilita acceso remoto y el build de entregas usa un resolver administrativo que confina imports a snapshot/shared y paquetes presentes en `package-lock.json`.

**Aceptación de riesgo temporal de esta entrega:** se conservan los pins exigidos por el diseño (`Vite 4.5.14`, `Vitest 0.34.6`, ESLint Vue 9.32.0) para no introducir una migración mayor no validada. No se autoriza exponer Vite fuera de loopback ni ejecutar Vitest en modo UI/watch. La remediación es migrar conjuntamente a Vite 8/Vitest 5/eslint-plugin-vue 10 o posteriores, regenerar el lock y repetir toda la verificación antes de retirar esta aceptación; `npm audit fix --force` no debe ejecutarse automáticamente.

## Diagnóstico rápido

- **`.venv` no encontrado:** créalo en la raíz, no dentro de `backend`.
- **Dependencia o hash inválido:** usa Python compatible y los locks correctos; no quite `--require-hashes`.
- **`npm ci` falla:** comprueba que `package.json` y `package-lock.json` pertenecen al mismo checkout.
- **Puerto 5000/5001/5173 ocupado:** identifica el proceso; el supervisor no terminará procesos ajenos.
- **API no ready:** durante el primer pase es normal `503 submission_recovery_pending`; si persiste, revisa la categoría segura de recuperación, espacio y permisos bajo `backend/data/runtime`. `workspace_recovering` bloquea el proyecto hasta verificar todos los hashes del guardado.
- **CSRF 403:** primero abre sesión y conserva cookies; envía `x-csrf-token` en mutaciones.
- **Build fallido:** revisa logs frontend/backend en el detalle; confirma `frontend/node_modules` y que existe el componente `<PascalSlug>.vue`. La snapshot rechaza de forma fail-closed `.env*`, `credentials*`, claves y certificados (`.pem`, `.key`, `.p12`, `.pfx`, `.crt`, `.cer`, `.der`) sin registrar su contenido.
- **Preview vacío/403:** confirma servicio 5001, origen distinto, grant vigente y cookies permitidas en loopback.
- **Checksum inválido:** no use el artefacto; preserve evidencia y repita diagnóstico de almacenamiento.
- **Notificación remota ausente:** local registra auditoría; SES/Teams requieren configuración opcional del propietario.

## Estructura y contribución

El código de herramientas vive únicamente en `frontend/src/projects/<slug>` y `backend/app/projects/<slug>`. `backend/vendor` es un snapshot: consulta [`VENDORING.md`](VENDORING.md) y añade adaptadores propios fuera de él. Las reglas neutrales para cualquier agente/editor están en [`AGENTS.md`](AGENTS.md). Nunca versionar `.env*`, `backend/config/studio.config.json`, runtime generado o credenciales.

## Pasos del propietario

1. Instalar con locks y ejecutar toda la verificación anterior.
2. Revisar bandeja, manifiesto, logs, comentarios, preview y SHA descargado.
3. Aprobar y publicar explícitamente solo artefactos válidos.
4. Elegir Git corporativo y permisos; CodeCommit solo si ya está disponible.
5. Si se desea AWS, completar manualmente seguridad, IAM, red, costes y sandbox descritos en la guía AWS.
6. Integrar el código revisado en Toolbox mediante el proceso del repositorio destino. Toolbox Studio nunca hace auto-merge.
