Mi Negocio Cloud + Sync

Start: gunicorn cloud_launcher:app

Variables de entorno:
- DATABASE_URL: PostgreSQL del servicio cloud.
- MI_NEGOCIO_SECRET: secreto de sesión de la app web.
- MI_NEGOCIO_SYNC_URL: URL HTTPS del servidor central de sincronización, sin / final.
- MI_NEGOCIO_SYNC_KEY: misma clave secreta configurada en el servidor central.
- MI_NEGOCIO_DEVICE_ID: identificador de esta instalación web; si no se define usa MI_NEGOCIO_CLOUD_DEVICE_ID o ANDROID-WEB.
- MI_NEGOCIO_CLOUD_DEVICE_ID: identificador alternativo de la instalación cloud.

La sincronización cloud conserva las operaciones pendientes en sync_outbox, las envía al servidor central y descarga las operaciones de otros dispositivos. No reemplaza la base local/central por una copia remota.
