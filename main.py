import os
import asyncio
import re
import json
import hashlib
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from typing import Optional, List, Dict, Any

import httpx
import anthropic
from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

# ─────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
CHILECOMPRA_TICKET  = os.environ.get("CHILECOMPRA_TICKET", "D61D704F-AF97-4F8A-8D65-A85B74F29C92")
APP_API_KEY         = os.environ.get("APP_API_KEY", "")          # vacío = sin auth (dev)
CHILECOMPRA_BASE    = "https://api.mercadopublico.cl/servicios/v1/publico"
CHILECOMPRA_BASE_V2 = "https://api.mercadopublico.cl/servicios/v2/publico"

# Sprint 2 — timeouts
TIMEOUT_PROVEEDOR   = 10      # segundos por proveedor (antes: 15)
TIMEOUT_GLOBAL_API  = 55      # segundos total endpoint /api/search (Railway free: 60s)
TIMEOUT_CA_FECHA    = 12      # segundos por fecha en ChileCompra

# Sprint 2 — caché (TTL en segundos)
CACHE_TTL_SEARCH    = 86_400  # 24 horas para cotización de proveedores
CACHE_TTL_CA        = 43_200  # 12 horas para Compra Ágil (datos menos volátiles)

PROVIDERS = [
    {"name": "Bioquimica",   "baseSearch": "https://bioquimica.cl/?s="},
    {"name": "Prodelab",     "baseSearch": "https://prodelab.cl/?s="},
    {"name": "Quorux",       "baseSearch": "https://quorux.webnode.cl/search/?text="},
    {"name": "Winkler Ltda", "baseSearch": "https://winklerltda.cl/quimicav2/?s="},
    {"name": "Valtek",       "baseSearch": "https://valtek.cl/?s="},
    {"name": "Dilaco",       "baseSearch": "https://www.dilaco.com/buscar?q="},
    {"name": "Insumolab",    "baseSearch": "https://www.insumolab.cl/buscar?q="},
]

PRODUCTS = [
    {"name": "Placas de Agar Mueller Hinton",                    "quantity": "15.500+ unidades"},
    {"name": "Placas de Agar MacConkey",                         "quantity": "13.500+ unidades"},
    {"name": "Placas de Agar Sangre de Cordero (5%)",            "quantity": "9.000+ unidades"},
    {"name": "Placas de Agar Cromo UTI / Orientacion",           "quantity": "7.000+ unidades"},
    {"name": "Placas de Agar Chocolate Estandar y suplementado", "quantity": "6.800+ unidades"},
    {"name": "Placas de Agar Duo Urocultivo CPS CNA",            "quantity": "3.800 unidades"},
    {"name": "Placas de Agar Tripticase 5% Sangre de Cordero",   "quantity": "3.600 unidades"},
    {"name": "Tubos de Ensayo 16x150mm con tapa rosca",          "quantity": "2.000 unidades"},
    {"name": "Tubos de Agar TSI Triple Sugar Iron",               "quantity": "1.600 unidades"},
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

# ─────────────────────────────────────────────
# Sprint 2 — Caché en memoria
# ─────────────────────────────────────────────

_cache: Dict[str, Dict[str, Any]] = {}

def _cache_key(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

def _cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > entry["ttl"]:
        del _cache[key]
        return None
    return entry["data"]

def _cache_set(key: str, data: Any, ttl: int) -> None:
    _cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}

def _cache_stats() -> dict:
    now = time.time()
    validas = sum(1 for e in _cache.values() if now - e["ts"] <= e["ttl"])
    return {"entradas_totales": len(_cache), "entradas_validas": validas}

def _cache_clear() -> int:
    n = len(_cache)
    _cache.clear()
    return n

# ─────────────────────────────────────────────
# Sprint 2 — Autenticación por API Key
# ─────────────────────────────────────────────

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: Optional[str] = Depends(_api_key_header)) -> None:
    if not APP_API_KEY:
        return                                      # dev: sin APP_API_KEY configurada → libre
    if not api_key or not secrets.compare_digest(api_key, APP_API_KEY):
        raise HTTPException(
            status_code=401,
            detail="API key inválida o ausente. Incluye el header X-API-Key.",
        )

# ─────────────────────────────────────────────
# App FastAPI
# ─────────────────────────────────────────────

app = FastAPI(title="Cotizador de Insumos de Laboratorio", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://csalassanhueza-prog.github.io", "http://localhost", "*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ─────────────────────────────────────────────
# Modelos
# ─────────────────────────────────────────────

class SearchRequest(BaseModel):
    product_name: str
    quantity: str
    provider_names: Optional[List[str]] = None

class CompraAgilRequest(BaseModel):
    product_name: str
    dias_atras: Optional[int] = 365
    max_resultados: Optional[int] = 20

class CompraAgilItem(BaseModel):
    codigo: str
    descripcion: str
    organismo: str
    proveedor: Optional[str] = None
    cantidad: Optional[float] = None
    precio_unitario: Optional[float] = None
    precio_unitario_fmt: Optional[str] = None
    monto_total: Optional[float] = None
    fecha: Optional[str] = None
    estado: Optional[str] = None
    url: Optional[str] = None

class CompraAgilResponse(BaseModel):
    product_name: str
    total_encontrados: int
    precio_promedio: Optional[float] = None
    precio_minimo: Optional[float] = None
    precio_maximo: Optional[float] = None
    precio_promedio_fmt: Optional[str] = None
    precio_minimo_fmt: Optional[str] = None
    precio_maximo_fmt: Optional[str] = None
    compras: List[CompraAgilItem]
    resumen_ia: Optional[str] = None
    fuente: str
    advertencia: Optional[str] = None
    desde_cache: bool = False

class ProviderResult(BaseModel):
    proveedor: str
    url: str
    producto: Optional[str] = None
    precio_unitario: Optional[str] = None
    precio_numerico: Optional[float] = None
    stock: str = "no_encontrado"
    notas: Optional[str] = None
    error: Optional[str] = None

# ─────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────

def fmt_clp(valor: float) -> str:
    return "${:,.0f}".format(valor).replace(",", ".")

def clean_html(html: str) -> str:
    html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>',   ' ', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'&nbsp;', ' ', html)
    html = re.sub(r'&[a-z]+;', ' ', html)
    html = re.sub(r'\s+', ' ', html)
    return html.strip()

# ─────────────────────────────────────────────
# Módulo scraping de proveedores
# ─────────────────────────────────────────────

async def fetch_provider(client: httpx.AsyncClient, provider: dict, query: str) -> dict:
    url  = provider["baseSearch"] + quote_plus(query)
    name = provider["name"]
    try:
        resp = await client.get(
            url, headers=HEADERS,
            timeout=TIMEOUT_PROVEEDOR,   # Sprint 2: reducido de 15 → 10s
            follow_redirects=True,
        )
        resp.raise_for_status()
        text = clean_html(resp.text)
        return {"name": name, "url": url, "text": text[:5000], "error": None}
    except httpx.TimeoutException:
        return {"name": name, "url": url, "text": "", "error": "Tiempo de espera agotado"}
    except httpx.HTTPStatusError as exc:
        return {"name": name, "url": url, "text": "", "error": f"Error HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"name": name, "url": url, "text": "", "error": str(exc)}


def analyze_with_ai(product_name: str, quantity: str, provider_results: list) -> dict:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY no configurada.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    entries = []
    for r in provider_results:
        if r["error"]:
            entries.append(f"=== PROVEEDOR: {r['name']} ===\nERROR: {r['error']}")
        else:
            entries.append(f"=== PROVEEDOR: {r['name']} ===\nURL: {r['url']}\n{r['text']}")

    content_block = "\n\n---\n\n".join(entries)

    prompt = (
        "Eres un asistente de compras para laboratorio clinico chileno.\n\n"
        f"Analiza el contenido de paginas web de proveedores y extrae informacion de productos relacionados con:\n"
        f"Producto buscado: \"{product_name}\"\n"
        f"Cantidad requerida: {quantity}\n\n"
        "Para cada proveedor, identifica el producto mas relevante encontrado y extrae:\n"
        "- nombre exacto del producto\n"
        "- precio unitario en pesos chilenos CLP\n"
        "- disponibilidad o stock\n"
        "- notas relevantes (presentacion, marca, tamano, codigo)\n\n"
        "Si un proveedor tuvo ERROR, indica stock \"no_encontrado\".\n\n"
        "RESPONDE UNICAMENTE con JSON valido. Sin texto adicional, sin backticks, sin comentarios.\n\n"
        "Estructura exacta requerida:\n"
        "{{\n"
        "  \"resultados\": [\n"
        "    {{\n"
        "      \"proveedor\": \"nombre\",\n"
        "      \"producto\": \"nombre exacto o null\",\n"
        "      \"precio_unitario\": \"$X.XXX o null\",\n"
        "      \"precio_numerico\": numero_float_o_null,\n"
        "      \"stock\": \"disponible | sin_stock | consultar | no_encontrado\",\n"
        "      \"notas\": \"observaciones o null\"\n"
        "    }}\n"
        "  ],\n"
        "  \"resumen\": \"Recomendacion indicando el mejor proveedor por precio y disponibilidad.\"\n"
        "}}\n\n"
        f"Contenido de los proveedores:\n{content_block}"
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw   = message.content[0].text
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ─────────────────────────────────────────────
# Módulo ChileCompra — Compra Ágil
# ─────────────────────────────────────────────

async def _fetch_compras_agiles_por_fecha(
    client: httpx.AsyncClient,
    fecha: str,
    keyword: str,
) -> list:
    endpoints = [
        f"{CHILECOMPRA_BASE_V2}/comprasagiles.json?ticket={CHILECOMPRA_TICKET}&fecha={fecha}",
        f"{CHILECOMPRA_BASE}/ordenesdecompra.json?ticket={CHILECOMPRA_TICKET}&fecha={fecha}&tipo=compraagil",
        f"{CHILECOMPRA_BASE}/ordenesdecompra.json?ticket={CHILECOMPRA_TICKET}&fecha={fecha}",
    ]

    data = None
    for url in endpoints:
        try:
            r = await client.get(url, timeout=TIMEOUT_CA_FECHA, follow_redirects=True)
            if r.status_code == 200:
                data = r.json()
                break
        except Exception:
            continue

    if not data:
        return []

    items_raw = (
        data.get("ComprasAgiles") or
        data.get("Listado") or
        data.get("Items") or
        data.get("OrdenesDeCompra") or
        []
    )

    kw = keyword.lower()
    resultados = []

    for item in items_raw:
        descripcion = (
            item.get("Nombre") or item.get("Descripcion") or
            item.get("NombreProducto") or item.get("Descripcion_item") or ""
        ).lower()

        if kw not in descripcion and not any(w in descripcion for w in kw.split()):
            continue

        codigo = (
            item.get("CodigoOC") or item.get("Codigo") or
            item.get("ID") or item.get("NumeroOC") or "—"
        )
        organismo = (
            item.get("Nombre_organismo") or item.get("NombreOrganismo") or
            item.get("Organismo") or "—"
        )
        proveedor = (
            item.get("Nombre_proveedor") or item.get("NombreProveedor") or
            item.get("Proveedor") or None
        )

        precio_unit = None
        for key in ["PrecioUnitario", "Precio_unitario", "ValorUnitario", "PrecioNeto"]:
            v = item.get(key)
            if v is not None:
                try:
                    precio_unit = float(str(v).replace(".", "").replace(",", "."))
                    break
                except Exception:
                    pass

        cantidad = None
        for key in ["Cantidad", "CantidadOC", "TotalCantidad"]:
            v = item.get(key)
            if v is not None:
                try:
                    cantidad = float(v)
                    break
                except Exception:
                    pass

        monto_total = None
        for key in ["MontoTotal", "Monto", "Total", "MontoNeto"]:
            v = item.get(key)
            if v is not None:
                try:
                    monto_total = float(str(v).replace(".", "").replace(",", "."))
                    break
                except Exception:
                    pass

        if precio_unit is None and monto_total and cantidad and cantidad > 0:
            precio_unit = monto_total / cantidad

        fecha_oc = (
            item.get("FechaCreacion") or item.get("Fecha") or item.get("FechaOC") or None
        )
        estado = item.get("Estado") or item.get("EstadoOC") or None
        url_oc = (
            f"https://www.mercadopublico.cl/Procurement/Modules/RFB/Details.aspx?idlicitacion={codigo}"
            if codigo and codigo != "—" else None
        )

        resultados.append(CompraAgilItem(
            codigo=str(codigo),
            descripcion=item.get("Nombre") or item.get("Descripcion") or descripcion,
            organismo=organismo,
            proveedor=proveedor,
            cantidad=cantidad,
            precio_unitario=precio_unit,
            precio_unitario_fmt=fmt_clp(precio_unit) if precio_unit else None,
            monto_total=monto_total,
            fecha=fecha_oc,
            estado=estado,
            url=url_oc,
        ))

    return resultados


async def buscar_compras_agiles(
    product_name: str,
    dias_atras: int = 365,
    max_resultados: int = 20,
) -> CompraAgilResponse:
    # Sprint 2 — caché
    ck = _cache_key("ca", product_name, dias_atras, max_resultados)
    cached = _cache_get(ck)
    if cached:
        cached["desde_cache"] = True
        return CompraAgilResponse(**cached)

    hoy = datetime.now()
    fechas = [
        (hoy - timedelta(days=i)).strftime("%d%m%Y")
        for i in range(0, dias_atras, 7)
    ]

    keyword = product_name.lower()
    for rem in ["placas de ", "tubos de ", "agar ", "(5%)", "estándar y suplementado", "estandar y suplementado"]:
        keyword = keyword.replace(rem, "").strip()
    keyword = keyword[:30]

    async with httpx.AsyncClient() as client:
        lote_size = 10
        todos = []
        for i in range(0, len(fechas), lote_size):
            lote = fechas[i:i + lote_size]
            tareas = [_fetch_compras_agiles_por_fecha(client, f, keyword) for f in lote]
            lote_res = await asyncio.gather(*tareas, return_exceptions=True)
            for r in lote_res:
                if isinstance(r, list):
                    todos.extend(r)

    vistos: set = set()
    unicos = []
    for item in todos:
        if item.codigo not in vistos:
            vistos.add(item.codigo)
            unicos.append(item)

    unicos.sort(key=lambda x: x.fecha or "", reverse=True)
    unicos = unicos[:max_resultados]

    precios = [i.precio_unitario for i in unicos if i.precio_unitario and i.precio_unitario > 0]
    precio_prom = sum(precios) / len(precios) if precios else None
    precio_min  = min(precios) if precios else None
    precio_max  = max(precios) if precios else None

    advertencia = None
    if not unicos:
        advertencia = (
            "No se encontraron compras ágiles para este producto en el período consultado. "
            "Prueba ampliar el rango de fechas o verificar que la API Beta ya incluya datos de Compra Ágil."
        )

    resumen_ia = None
    if unicos and ANTHROPIC_API_KEY:
        try:
            resumen_ia = _resumir_compras_con_ia(product_name, unicos, precio_prom, precio_min, precio_max)
        except Exception:
            pass

    payload = dict(
        product_name=product_name,
        total_encontrados=len(unicos),
        precio_promedio=precio_prom,
        precio_minimo=precio_min,
        precio_maximo=precio_max,
        precio_promedio_fmt=fmt_clp(precio_prom) if precio_prom else None,
        precio_minimo_fmt=fmt_clp(precio_min) if precio_min else None,
        precio_maximo_fmt=fmt_clp(precio_max) if precio_max else None,
        compras=[c.model_dump() for c in unicos],
        resumen_ia=resumen_ia,
        fuente="api.mercadopublico.cl (ticket Beta)",
        advertencia=advertencia,
        desde_cache=False,
    )
    _cache_set(ck, payload, CACHE_TTL_CA)
    return CompraAgilResponse(**payload)


def _resumir_compras_con_ia(
    product_name: str,
    compras: list,
    precio_prom: Optional[float],
    precio_min: Optional[float],
    precio_max: Optional[float],
) -> str:
    if not ANTHROPIC_API_KEY:
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    resumen_compras = [
        f"- {c.fecha or '?'} | {c.organismo} | {c.proveedor or 'S/D'} | "
        f"Precio unit: {c.precio_unitario_fmt or 'N/D'} | Cant: {c.cantidad or '?'}"
        for c in compras[:10]
    ]

    prompt = (
        "Eres un asesor de compras para un laboratorio clínico chileno.\n\n"
        f"Se encontraron {len(compras)} compras ágiles históricas del Estado para: '{product_name}'\n\n"
        f"Precio promedio histórico: {fmt_clp(precio_prom) if precio_prom else 'N/D'}\n"
        f"Precio mínimo: {fmt_clp(precio_min) if precio_min else 'N/D'}\n"
        f"Precio máximo: {fmt_clp(precio_max) if precio_max else 'N/D'}\n\n"
        "Muestra de compras recientes:\n" + "\n".join(resumen_compras) + "\n\n"
        "En 2-3 oraciones, entrega una recomendación práctica sobre:\n"
        "1. Si el precio histórico del Estado es una buena referencia de negociación\n"
        "2. Qué precio unitario máximo debería pagar este laboratorio\n"
        "Responde directo, sin encabezados."
    )

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()

# ─────────────────────────────────────────────
# Endpoints — públicos (sin auth)
# ─────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Cotizador de Insumos de Laboratorio",
        "version": "3.0.0",
        "auth": "activa" if APP_API_KEY else "desactivada (dev)",
    }

@app.get("/api/products")
def get_products():
    return {"products": PRODUCTS}

@app.get("/api/providers")
def get_providers():
    return {"providers": [{"name": p["name"], "baseSearch": p["baseSearch"]} for p in PROVIDERS]}

@app.get("/api/chilecompra/status")
async def chilecompra_status():
    ticket = CHILECOMPRA_TICKET
    if not ticket:
        raise HTTPException(status_code=500, detail="CHILECOMPRA_TICKET no configurado.")

    hoy = datetime.now().strftime("%d%m%Y")
    urls_test = [
        f"{CHILECOMPRA_BASE_V2}/comprasagiles.json?ticket={ticket}&fecha={hoy}",
        f"{CHILECOMPRA_BASE}/ordenesdecompra.json?ticket={ticket}&fecha={hoy}",
    ]

    async with httpx.AsyncClient() as client:
        for url in urls_test:
            try:
                r = await client.get(url, timeout=10, follow_redirects=True)
                return {
                    "status": "ok" if r.status_code == 200 else "error",
                    "http_code": r.status_code,
                    "endpoint": url.split("?")[0],
                    "ticket_activo": ticket[:8] + "****",
                    "respuesta_preview": r.text[:300],
                }
            except Exception:
                continue

    return {"status": "sin_conexion", "detalle": "No se pudo conectar con ChileCompra."}

# Sprint 2 — endpoint de estado del caché (público, solo lectura)
@app.get("/api/cache/stats")
def cache_stats():
    return {"cache": _cache_stats(), "ttl_search_h": CACHE_TTL_SEARCH // 3600, "ttl_ca_h": CACHE_TTL_CA // 3600}

# ─────────────────────────────────────────────
# Endpoints — protegidos con X-API-Key
# ─────────────────────────────────────────────

@app.post("/api/search", dependencies=[Depends(verify_api_key)])
async def search(req: SearchRequest):
    # Sprint 2 — caché
    providers_key = sorted(req.provider_names or [p["name"] for p in PROVIDERS])
    ck = _cache_key("search", req.product_name, req.quantity, *providers_key)
    cached = _cache_get(ck)
    if cached:
        cached["desde_cache"] = True
        return cached

    selected = PROVIDERS
    if req.provider_names:
        selected = [p for p in PROVIDERS if p["name"] in req.provider_names]
    if not selected:
        raise HTTPException(status_code=400, detail="No se encontraron proveedores válidos.")

    # Sprint 2 — timeout global para no superar límite Railway free (60s)
    try:
        async with asyncio.timeout(TIMEOUT_GLOBAL_API):
            async with httpx.AsyncClient() as client:
                tasks = [fetch_provider(client, prov, req.product_name) for prov in selected]
                raw_results = await asyncio.gather(*tasks)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"La búsqueda superó el límite de {TIMEOUT_GLOBAL_API}s. Selecciona menos proveedores o intenta de nuevo.",
        )

    # Sprint 2 — resultado parcial: si todos fallaron, no llamar a Claude
    exitosos = [r for r in raw_results if not r["error"]]
    if not exitosos:
        raise HTTPException(
            status_code=502,
            detail="Todos los proveedores retornaron error. Verifica la conexión del servidor.",
        )

    ai_data = analyze_with_ai(req.product_name, req.quantity, list(raw_results))
    url_map = {r["name"]: r["url"] for r in raw_results}

    resultados = [
        {
            "proveedor":       r.get("proveedor", ""),
            "url":             url_map.get(r.get("proveedor", ""), ""),
            "producto":        r.get("producto"),
            "precio_unitario": r.get("precio_unitario"),
            "precio_numerico": r.get("precio_numerico"),
            "stock":           r.get("stock", "no_encontrado"),
            "notas":           r.get("notas"),
            "error":           None,
        }
        for r in ai_data.get("resultados", [])
    ]

    payload = {
        "product_name":  req.product_name,
        "quantity":      req.quantity,
        "resultados":    resultados,
        "resumen":       ai_data.get("resumen", ""),
        "desde_cache":   False,
        "proveedores_ok": len(exitosos),
        "proveedores_error": len(raw_results) - len(exitosos),
    }
    _cache_set(ck, payload, CACHE_TTL_SEARCH)
    return payload


@app.post("/api/compra-agil/buscar", dependencies=[Depends(verify_api_key)])
async def buscar_compra_agil(req: CompraAgilRequest):
    if not CHILECOMPRA_TICKET:
        raise HTTPException(status_code=500, detail="CHILECOMPRA_TICKET no configurado.")
    return await buscar_compras_agiles(
        product_name=req.product_name,
        dias_atras=req.dias_atras,
        max_resultados=req.max_resultados,
    )


@app.get("/api/compra-agil/buscar", dependencies=[Depends(verify_api_key)])
async def buscar_compra_agil_get(
    producto: str = Query(..., description="Nombre del producto"),
    dias_atras: int = Query(365, description="Días hacia atrás"),
    max_resultados: int = Query(20, description="Máximo de resultados"),
):
    if not CHILECOMPRA_TICKET:
        raise HTTPException(status_code=500, detail="CHILECOMPRA_TICKET no configurado.")
    return await buscar_compras_agiles(
        product_name=producto,
        dias_atras=dias_atras,
        max_resultados=max_resultados,
    )


# Sprint 2 — limpiar caché (solo con auth)
@app.delete("/api/cache", dependencies=[Depends(verify_api_key)])
def cache_clear():
    n = _cache_clear()
    return {"eliminadas": n, "mensaje": f"Caché limpiado. {n} entradas eliminadas."}
