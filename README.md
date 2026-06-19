# Cotizador de Insumos de Laboratorio

Sistema de búsqueda automática de precios en proveedores de insumos médicos y de laboratorio, con extracción inteligente usando IA (Claude).

---

## Arquitectura

```
frontend/index.html   →   backend (FastAPI en Railway/Render)   →   sitios de proveedores
                                      ↓
                               Claude IA extrae precios
```

---

## Requisitos

- Cuenta en [Railway](https://railway.app) o [Render](https://render.com) (ambos tienen plan gratuito)
- API Key de Anthropic: https://console.anthropic.com

---

## Despliegue del Backend (Railway — recomendado)

### Opción A: Desde GitHub

1. Sube la carpeta `backend/` a un repositorio de GitHub.
2. En Railway: **New Project → Deploy from GitHub repo**
3. Selecciona el repositorio.
4. En **Variables de entorno**, agrega:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   PORT=8000
   ```
5. Railway desplegará automáticamente. Copia la URL pública generada (ej: `https://cotizador-lab-production.up.railway.app`).

### Opción B: Desde CLI

```bash
npm install -g @railway/cli
cd backend/
railway login
railway init
railway up
railway variables set ANTHROPIC_API_KEY=sk-ant-...
```

---

## Despliegue del Backend (Render)

1. Sube la carpeta `backend/` a GitHub.
2. En Render: **New → Web Service**
3. Conecta el repositorio.
4. Configuración:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. En **Environment**, agrega `ANTHROPIC_API_KEY=sk-ant-...`
6. Copia la URL pública generada.

---

## Uso del Frontend

1. Abre `frontend/index.html` en el navegador (doble clic o arrastra al navegador).
2. En el campo superior derecho, pega la URL de tu backend desplegado.
   - Ejemplo: `https://cotizador-lab-production.up.railway.app`
3. Selecciona el insumo a cotizar.
4. Elige los proveedores que deseas consultar (por defecto todos).
5. Haz clic en **Buscar y comparar precios**.
6. El sistema consultará cada proveedor, extraerá precios con IA y mostrará la matriz comparativa.
7. Usa **Exportar CSV** para descargar los resultados.

> El frontend puede servirse desde cualquier lugar: GitHub Pages, Netlify, o simplemente como archivo local.

---

## Agregar proveedores

En `backend/main.py`, agrega un objeto al arreglo `PROVIDERS`:

```python
{"name": "NuevoProveedor", "baseSearch": "https://nuevoproveedor.cl/search?q="},
```

---

## Agregar productos

En `backend/main.py`, agrega un objeto al arreglo `PRODUCTS`:

```python
{"name": "Nombre del producto", "quantity": "X.XXX unidades"},
```

---

## Estructura de archivos

```
cotizador-lab/
├── backend/
│   ├── main.py           # API FastAPI + scraping + extracción IA
│   ├── requirements.txt  # Dependencias Python
│   ├── Procfile          # Para Railway/Render
│   └── runtime.txt       # Versión Python
└── frontend/
    └── index.html        # Interfaz completa (un solo archivo)
```

---

## Notas técnicas

- El backend hace peticiones HTTP reales a los sitios de proveedores desde el servidor cloud (sin restricciones CORS).
- El texto HTML de cada página es limpiado y enviado a Claude para extracción estructurada de precios.
- Si un proveedor bloquea el scraping, el sistema lo reporta como "Error de acceso" sin detener la búsqueda en los demás.
- Tiempo estimado por búsqueda: 8-15 segundos dependiendo de la respuesta de cada proveedor.
