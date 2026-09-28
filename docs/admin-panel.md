# Panel de administración — Diseño

Fecha: 2026-09-28
Estado: implementado
Base: [design.md](design.md) (v0.1.0)

## 1. Objetivo

Que una persona no técnica pueda administrar el agente desde el navegador, sin terminal:
subir y borrar documentos, ver su estado, sincronizar la carpeta y gestionar usuarios.

Es el primero de dos subproyectos. El segundo (rediseño visual del chat y de toda la
interfaz) viene después y se construye sobre la misma hoja de estilos.

### Criterios de éxito

1. Un admin sube un PDF arrastrándolo, elige su colección, ve "Procesando" y en unos
   segundos "Listo"; a partir de ahí el chat responde con citas a ese documento.
2. Un admin crea un usuario, le asigna colecciones y le da una contraseña temporal sin usar
   la terminal.
3. Un usuario normal o una API key no pueden usar ninguna función de administración (403).
4. Nada de lo subido puede escribirse fuera de la carpeta de documentos.
5. Los cambios hechos directamente en la carpeta (OneDrive) se incorporan con "Sincronizar
   carpeta" o con la carga nocturna, como hasta ahora.

### Fuera de alcance

- Líderes de área que suban solo a sus colecciones (preparado: ver §3, `can_manage`).
- Gestión de API keys desde el panel (siguen en el CLI).
- Rediseño visual del chat (subproyecto siguiente).
- Envío de correos (restablecer contraseña = el admin escribe una temporal).
- Varias réplicas de la API (el procesamiento en segundo plano vive en el proceso).

## 2. Interfaz

- **Estructura:** barra lateral izquierda con Chat, Documentos y Usuarios; abajo, el usuario
  y "Salir". Documentos y Usuarios solo aparecen para admins. En pantallas estrechas la barra
  se oculta tras un botón de menú.
- **Iconos:** SVG dibujados a mano, en línea. **Sin emojis.**
- **Tecnología:** HTML + CSS + JavaScript sin framework ni paso de compilación. Una hoja de
  estilos compartida (`static/app.css`) y un script común (`static/app.js`: llamadas a la
  API, barra lateral, sesión). Cada página es un archivo HTML.
- **Páginas:** `/` (chat, ya existe, pasa a usar la barra lateral), `/documents`, `/users`.
  Si un no-admin abre `/documents` o `/users`, se le redirige al chat.

### Documentos (`/documents`)

- Cabecera: "Documentos", contador ("42 documentos · 3 colecciones") y botón
  "Sincronizar carpeta".
- Zona de subida: arrastrar o hacer clic; admite varios archivos a la vez. Texto de ayuda
  con formatos y tamaño máximo. Selector de colección con las existentes y
  "Nueva colección…" (pide el nombre).
- Si ya existe un archivo con el mismo nombre en esa colección, avisa de que se
  reemplazará y pide confirmación.
- Filtros por colección (chips) y buscador por nombre de archivo.
- Tabla: Archivo · Colección · Estado · Actualizado · borrar.
- Estados: **Listo**, **Procesando**, **Sin texto** (PDF escaneado), **Error** (el motivo
  aparece al pasar el ratón y es legible por lector de pantalla).
- Mientras haya documentos en "Procesando", la lista se refresca cada 3 segundos.
- Borrar pide confirmación.

### Usuarios (`/users`)

- Cabecera: "Usuarios", contador y botón "Nuevo usuario".
- Tabla: Usuario · Colecciones (etiquetas) · Rol (Admin / Usuario) · acciones
  (editar, restablecer contraseña, borrar).
- Formulario de alta/edición: nombre (solo en alta), contraseña temporal (solo en alta),
  colecciones (casillas; `general` siempre marcada y bloqueada; "+ nueva"), casilla
  Administrador.
- Restablecer contraseña: el admin escribe una contraseña temporal (mínimo 8 caracteres).
  Cierra todas las sesiones abiertas de ese usuario (ya ocurre al cambiar la contraseña).
- El admin conectado no ve las acciones de borrarse ni de quitarse el rol.

## 3. Modelo de datos y permisos

Migraciones idempotentes al arrancar (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`):

```sql
ALTER TABLE users     ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'ready';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS error  text;
ALTER TABLE documents ALTER COLUMN sha256 DROP NOT NULL;
```

- `documents.status`: `processing` · `ready` · `empty` · `error`. Una fila en `error` o
  `processing` tiene `sha256` NULL, así la siguiente carga la reintenta.
- La carga completa (`ingest`) también escribe `status`/`error`, de modo que los errores de
  la sincronización y de la carga nocturna se ven en el panel.
- `Principal` gana `is_admin: bool`. Las API keys siempre `False`.
- `auth.can_manage(principal, collection) -> bool`: hoy devuelve `principal.is_admin`.
  Todas las comprobaciones de administración pasan por aquí. Para los líderes de área
  bastará con añadir `users.editor_collections` y ampliar esta función.
- Gestión de usuarios: exige `is_admin` (no depende de colección).

## 4. API

Todas las rutas nuevas exigen sesión de admin (API key → 403, sin sesión → 401).

| Método | Ruta | Cuerpo / respuesta |
|---|---|---|
| `GET` | `/api/admin/documents` | lista `{path, collection, status, error, updated_at}` |
| `POST` | `/api/admin/documents` | multipart `file` + `collection` → 202 `{path, status: "processing"}` |
| `DELETE` | `/api/admin/documents/{path:path}` | 204 (la ruta incluye la colección, p. ej. `rrhh/a.pdf`) |
| `POST` | `/api/admin/sync` | 202; 409 si ya hay una carga en curso |
| `GET` | `/api/admin/collections` | lista de nombres (carpetas existentes ∪ colecciones indexadas) |
| `GET` | `/api/admin/users` | lista `{username, collections, is_admin}` |
| `POST` | `/api/admin/users` | `{username, password, collections, is_admin}` → 201; 409 si existe |
| `PATCH` | `/api/admin/users/{u}` | `{collections?, is_admin?}` → 200 |
| `POST` | `/api/admin/users/{u}/password` | `{password}` → 204 |
| `DELETE` | `/api/admin/users/{u}` | 204 |

- `GET /me` añade `is_admin`.
- Rutas existentes (`/login`, `/logout`, `/me`, `/ask`, `/health`, `/`) sin cambios de
  contrato.
- Mensajes de error en español, como el resto de la API.

## 5. Flujos

### Subida

1. Validar (ver §6). Si ya existe una carpeta cuyo nombre en minúsculas coincide con la
   colección (p. ej. `RRHH/` para `rrhh`), se usa esa carpeta; si no, se crea la subcarpeta.
2. Escribir en `DOCS_DIR/<colección>/.<nombre>.upload` y renombrar a
   `DOCS_DIR/<colección>/<nombre>` (atómico; el nombre temporal empieza por `.` y la
   carga lo ignora).
3. Upsert en `documents` con `status='processing'`, `sha256=NULL`. Responder 202.
4. En segundo plano (`BackgroundTasks`), bajo el candado de carga: `ingest_file(...)`
   procesa ese archivo con el mismo código que la carga completa y deja
   `ready` / `empty` / `error`.

### Borrado

Comprobar que la ruta existe en `documents` y que, resuelta, queda dentro de `DOCS_DIR`.
Borrar el archivo (si existe) y la fila (los fragmentos caen en cascada).

### Sincronizar

`POST /api/admin/sync` lanza la carga completa en segundo plano bajo el candado. Si el
candado está ocupado, 409 "Ya hay una carga en curso". Mantiene las protecciones de la carga
(carpeta vacía o ilegible → no borra nada).

### Candado

`threading.Lock` del proceso compartido por subidas y sincronización. Las subidas esperan
su turno; la sincronización no espera (409).
`ponytail:` candado en memoria del proceso; con varias réplicas habría que pasar a una cola.

### Arranque

Al crear la app: los documentos en `processing` pasan a `error` con
`error='interrumpido (la API se reinició)'`.

### Refactor de la carga

`ingest.py` se divide en:
- `ingest_file(conn, docs_dir, rel, embedder) -> str` (estado final) — un archivo.
- `ingest(conn, docs_dir, embedder) -> IngestReport` — recorre la carpeta y usa
  `ingest_file`; conserva todo su comportamiento y protecciones actuales.

## 6. Validación y seguridad

- **Extensión:** `.pdf`, `.docx`, `.md`, `.txt` → si no, 400 "Formato no soportado".
- **Tamaño:** `MAX_UPLOAD_MB` (por defecto 50) → si se supera, 413. Se corta la lectura al
  pasar el límite (no se carga el archivo entero en memoria antes de comprobarlo).
- **Nombre de archivo:** solo el último componente del nombre enviado; se rechazan vacíos,
  `.`/`..`, los que empiezan por `.` o `~$`, los que contienen `/`, `\` o caracteres de
  control → 400 "Nombre de archivo no válido".
- **Colección:** `^[a-z0-9][a-z0-9_-]{0,63}$` → si no, 400 "Nombre de colección no válido".
- **Ruta final:** se comprueba que `resolve()` queda dentro de `DOCS_DIR` antes de escribir
  o borrar.
- **CSRF:** la cookie de sesión es `SameSite=Lax`, que impide peticiones POST/DELETE
  autenticadas desde otros sitios.
- **Autoprotección:** un admin no puede quitarse el rol ni borrarse a sí mismo → 409.
- **Logs de auditoría:** `admin=<u> action=upload|delete|sync|user_create|user_update|
  user_password|user_delete target=<ruta o usuario>`. Nunca contenido ni contraseñas.

## 7. Despliegue

- `docker-compose.yml`: `DOCS_HOST_DIR` se monta con escritura (se quita `:ro`).
- El usuario del contenedor se crea con UID 1000 explícito, que suele coincidir con el
  usuario del host en Linux/WSL.
- `.env.example`: `MAX_UPLOAD_MB=50`.
- CLI: `cka create-user <u> --admin`, `cka set-admin <u>` / `cka set-admin <u> --no`.
- README (en/es): sección del panel, cómo crear el primer admin, nota de permisos de la
  carpeta.

## 8. Pruebas

Con Postgres real, LLM y embeddings falsos, como las actuales. La subida en segundo plano se
prueba con `TestClient` (ejecuta las `BackgroundTasks` antes de devolver la respuesta).

- 403 para usuario normal y para API key, y 401 sin sesión, en todas las rutas
  `/api/admin/*`.
- Subida → archivo en disco → estado `ready` → `/ask` lo cita.
- Rechazos: extensión, tamaño, nombres peligrosos (`../x.pdf`, `.x.pdf`, `~$x.docx`,
  `a/b.pdf`), colección no válida.
- Reemplazo del mismo nombre actualiza el contenido.
- Archivo que falla al procesarse → `error` con motivo; PDF escaneado → `empty`.
- Borrado quita archivo y fragmentos; ruta desconocida → 404.
- Sincronizar: 202; con el candado ocupado → 409.
- Arranque: `processing` → `error` interrumpido.
- La carga completa escribe `status` y `error`.
- Usuarios: alta, 409 duplicado, edición de colecciones y admin, restablecer contraseña
  (cierra sesiones), borrado, autoprotección (409).
- `can_manage` y `is_admin` en `Principal`; `/me` devuelve `is_admin`.
- CLI: `create-user --admin`, `set-admin`.
- La interfaz (HTML/JS) se prueba a mano en el navegador.
