# Preparación opcional de entregas en AWS

Esta guía es preparatoria. La implementación entregada y su humo usan solo loopback, filesystem y SQLite: **no se ha desplegado ni probado infraestructura AWS**, no se han usado credenciales ni AWS CLI y no existe auto-merge.

## Repositorio: Git genérico primero

Toolbox Studio no exige un proveedor remoto. Cualquier servidor Git corporativo accesible por HTTPS/SSH puede alojar el repositorio; cada colaborador clona el repositorio, crea una rama, trabaja con Kiro, Orcha u otro editor y abre una revisión según el proceso interno. La publicación de Toolbox Studio no fusiona ramas automáticamente: el propietario revisa el ZIP, su manifiesto y SHA-256, y decide la integración.

AWS CodeCommit es solamente una opción si la organización **ya dispone de acceso y repositorios habilitados**. No se debe intentar crear ni descubrir recursos desde la aplicación. El propietario proporcionaría URL, región y método de autenticación por un canal corporativo. Si CodeCommit no está disponible, se conserva el flujo Git genérico sin cambiar el producto.

## Identidad, red y proxy

1. El administrador configura IAM Identity Center y asigna grupos separados de colaboradores, revisores y administradores.
2. El proxy corporativo autentica al usuario y firma la identidad dirigida a Toolbox Studio; TLS termina en infraestructura aprobada.
3. Se cambia el perfil a `shared`, se configura una versión de mapping soportada y se mapean identidades externas a identidades/roles internos. El secreto HMAC se obtiene de un almacén de secretos en runtime, nunca de Git.
4. Se restringen ingress/egress, CORS, hosts y DNS. El servicio de preview se mantiene en un origen distinto, sin acceso directo a credenciales ni a la API interna; su origen HTTPS debe figurar exactamente en `preview.allowed_origins` o el proceso falla antes de escuchar.
5. Los clientes usan el mecanismo corporativo aprobado para proxy y certificados. No se desactiva la validación TLS.

## IAM mínimo

Separar roles de ejecución, build y operación. Aplicar mínimo privilegio, recursos y prefijos concretos, condiciones de región/cuenta y sesiones cortas:

- API: lectura/escritura solo en el prefijo S3 de artefactos y metadatos necesarios; emisión de trabajos CodeBuild concretos; escritura de logs/métricas.
- CodeBuild: lectura del snapshot de entrada, escritura del artefacto de salida y logs del proyecto asignado; sin permisos administrativos ni credenciales persistentes.
- Notificaciones: únicamente `ses:SendEmail` para identidades/destinos aprobados o salida HTTPS al relay de Teams autorizado.
- Operación: lectura de CloudWatch y capacidad explícita para reintentos; publicación limitada a administradores.

No usar comodines de acción/recurso salvo exigencia documentada. Activar CloudTrail y revisar periódicamente permisos no utilizados.

## Servicios opcionales

- **S3:** bucket privado, Block Public Access, cifrado KMS administrado y versionado obligatorio. Toolbox persiste y vuelve a usar `VersionId` más checksum en HEAD/GET/URL firmada; los inputs de CodeBuild fijan `sourceVersionOverride`. Lifecycle separado para inputs, artefactos y cuarentena.
- **CodeBuild:** imagen fijada por digest, red sin egress por defecto, caché controlada, límites de tiempo/salida y comandos definidos por el propietario, no por datos no confiables. La entrada estándar solo admite el verificador allowlisted `aws-vpc-endpoints-v1`, que consulta rutas y endpoints VPC; no acepta imports de plugin configurables. Además exige un verificador administrativo de attestation; no considera evidencia las variables exportadas ni el eco de digests entregados al job.
- **Runtime aislado:** gateway HTTPS administrativo con host allowlisted y token inyectado mediante la variable nombrada en `runtime.gateway_token_env`. Debe implementar el contrato cerrado `/v1/projects/{slug}/app/{path}`, bloquear redirects y no compartir credenciales del API.
- **CloudWatch:** logs estructurados con correlation ID, alarmas de fallos/latencia/colas y redacción de tokens, cookies y secretos.
- **SES:** identidad verificada, sandbox resuelto por el propietario, remitente fijo y destinos permitidos. No incluir código ni secretos en correos.
- **Teams:** usar un relay corporativo o workflow aprobado; guardar la URL como secreto y enviar solo metadatos y enlaces autorizados.

## Aprobación y rotación del gate CodeBuild

El perfil `shared` permanece cerrado hasta completar todos estos pasos con evidencia revisada:

1. Obtener desde el manifiesto inmutable de ECR el SHA-256 de la imagen aprobada, no una etiqueta, y registrarlo en `build.approved_image_digest` junto con `build.account_id` y `build.region`.
2. Calcular el SHA-256 del wrapper `/opt/toolbox/bin/build-submission` incluido en esa imagen, revisar su fuente y fijarlo en `build.approved_wrapper_sha256`. El wrapper debe ejecutar Vite, el runner backend, empaquetado y canarios de credenciales/output; no puede limitarse a copiar valores de entorno.
3. Asignar una versión inmutable al probe en `build.probe_version`. El probe debe producir fuera de las variables exportadas una attestation autenticada y cerrada, ligada a cuenta, región, proyecto, build ID, marcador, versión S3, input, harness, imagen y wrapper, con las fases `input`, `frontend`, `backend`, `package`, `credentials_canary` y `output_canary`.
4. Implementar y revisar el adaptador administrativo `codebuild_attestation_verifier` que valida firma/origen, frescura y unicidad antes de devolver esa attestation. El launcher estándar no inventa este verificador ni confía en datos del job: si no se inyecta, `shared` falla con `config_invalid`/`probe_attested`.
5. Ejecutar en sandbox pruebas positivas y negativas: ausencia, digest distinto, fase ausente, firma/origen inválidos y replay entre build IDs. Guardar solo IDs/digests no secretos como evidencia operativa.
6. Para rotar, publicar una imagen y wrapper nuevos, repetir probe y revisión, actualizar juntos los cinco valores aprobados durante una ventana controlada y reiniciar. Un mismatch bloquea builds; nunca se acepta temporalmente una etiqueta o digest anterior sin aprobación explícita.

## Coste, retención y seguridad

Definir presupuestos y alarmas antes de habilitar recursos. Los principales costes son almacenamiento/requests S3, minutos de CodeBuild, ingesta/retención de CloudWatch, KMS y salida de red. Configurar retención explícita: logs cortos según política, artefactos rechazados con caducidad, aprobados según auditoría y eliminación legalmente autorizada. Probar restauración, rotación de claves/secretos, aislamiento de builds, análisis de dependencias y respuesta ante artefactos comprometidos. Nunca registrar contenido fuente completo, cookies o credenciales.

## Pasos manuales del propietario

1. Elegir Git corporativo; usar CodeCommit solo si ya existe acceso.
2. Aprobar arquitectura, región, clasificación de datos, retención y presupuesto con Seguridad/FinOps.
3. Provisionar mediante IaC revisada los buckets, claves, proyectos de build, logs, alarmas, identidades y notificaciones.
4. Crear roles mínimos y secretos en el servicio corporativo; configurar proxy, TLS, DNS y mappings de identidad.
5. Completar pruebas en una cuenta sandbox con datos sintéticos: build, checksum, revisión, publicación, reintentos, auditoría y recuperación.
6. Ejecutar revisión de seguridad y operación, documentar rollback y obtener aprobación para producción.
7. Configurar manualmente Toolbox Studio con identificadores no secretos: `build.network_preflight=aws-vpc-endpoints-v1`, `build.approved_image_digest`, `build.approved_wrapper_sha256`, `build.probe_version`, `build.account_id`, `build.region`, `runtime.gateway_url`, `runtime.gateway_allowed_hosts` y `runtime.gateway_token_env`; inyectar los secretos y el verificador administrativo de attestation solo en runtime. Cualquier ausencia o verificación insuficiente mantiene `shared` fail-closed con `config_invalid`.

Estos pasos no se ejecutan automáticamente desde el checkout. La alternativa local y Git genérico debe seguir funcionando aunque AWS, CodeCommit, SES o Teams no estén disponibles.
