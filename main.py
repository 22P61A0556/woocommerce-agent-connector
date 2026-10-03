"""Read-only FastAPI connector for the WooCommerce REST API."""

from __future__ import annotations

import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
import requests
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from woocommerce import WooCommerceClient, WooCommerceError

load_dotenv()


def _configure_logging() -> None:
	level_name = os.getenv("LOG_LEVEL", "INFO").upper()
	level = getattr(logging, level_name, logging.INFO)
	logging.basicConfig(
		level=level,
		format="%(asctime)s %(levelname)s %(name)s %(message)s",
	)


_configure_logging()
logger = logging.getLogger("woocommerce_connector")


@dataclass(frozen=True)
class Settings:
	api_key: str
	store_url: str
	consumer_key: str
	consumer_secret: str
	timeout_seconds: float
	rate_limit: str

	@classmethod
	def from_env(cls) -> "Settings":
		required = {
			"API_KEY": os.getenv("API_KEY", "").strip(),
			"WOOCOMMERCE_URL": os.getenv("WOOCOMMERCE_URL", "").strip(),
			"WOOCOMMERCE_CONSUMER_KEY": os.getenv("WOOCOMMERCE_CONSUMER_KEY", "").strip(),
			"WOOCOMMERCE_CONSUMER_SECRET": os.getenv("WOOCOMMERCE_CONSUMER_SECRET", "").strip(),
		}
		missing = [name for name, value in required.items() if not value]
		if missing:
			# Names only: never include secret values in logs or exception messages.
			raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

		try:
			timeout_seconds = float(os.getenv("WOOCOMMERCE_TIMEOUT_SECONDS", "20"))
		except ValueError as exc:
			raise RuntimeError("WOOCOMMERCE_TIMEOUT_SECONDS must be a number") from exc
		if timeout_seconds <= 0:
			raise RuntimeError("WOOCOMMERCE_TIMEOUT_SECONDS must be greater than zero")

		return cls(
			api_key=required["API_KEY"],
			store_url=required["WOOCOMMERCE_URL"],
			consumer_key=required["WOOCOMMERCE_CONSUMER_KEY"],
			consumer_secret=required["WOOCOMMERCE_CONSUMER_SECRET"],
			timeout_seconds=timeout_seconds,
			rate_limit=os.getenv("RATE_LIMIT", "60/minute").strip(),
		)


try:
	_rate_limit = os.getenv("RATE_LIMIT", "60/minute").strip() or "60/minute"
	limiter = Limiter(key_func=get_remote_address, default_limits=[_rate_limit])
except Exception as exc:
	raise RuntimeError("RATE_LIMIT must use a SlowAPI limit string, e.g. 60/minute") from exc


@asynccontextmanager
async def lifespan(application: FastAPI):
	settings = Settings.from_env()
	application.state.settings = settings
	dummyjson_mode = _is_dummyjson(settings.store_url)
	logger.info(
		"Connector configuration loaded store_url=%s dummyjson_mode=%s",
		settings.store_url,
		dummyjson_mode,
	)
	if dummyjson_mode:
		application.state.dummyjson_session = requests.Session()
		application.state.woocommerce = None
		logger.info("Connector started with DummyJSON carts adapter")
	else:
		application.state.dummyjson_session = None
		application.state.woocommerce = WooCommerceClient(
			store_url=settings.store_url,
			consumer_key=settings.consumer_key,
			consumer_secret=settings.consumer_secret,
			timeout=settings.timeout_seconds,
		)
		logger.info("WooCommerce connector started")
	try:
		yield
	finally:
		if application.state.woocommerce is not None:
			application.state.woocommerce.close()
		if application.state.dummyjson_session is not None:
			application.state.dummyjson_session.close()
		logger.info("WooCommerce connector stopped")


app = FastAPI(
	title="WooCommerce Read-only Connector",
	description="Authenticated, rate-limited read-only access to WooCommerce orders and products.",
	version="1.0.0",
	lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@app.middleware("http")
async def request_logging(request: Request, call_next):
	started = time.perf_counter()
	try:
		response = await call_next(request)
	except Exception:
		logger.exception("Unhandled request failure method=%s path=%s", request.method, request.url.path)
		raise
	elapsed_ms = (time.perf_counter() - started) * 1000
	logger.info(
		"http_request method=%s path=%s status=%s duration_ms=%.1f",
		request.method,
		request.url.path,
		response.status_code,
		elapsed_ms,
	)
	return response


async def require_api_key(
	request: Request,
	supplied_key: str | None = Depends(api_key_header),
) -> None:
	settings: Settings = request.app.state.settings
	if not supplied_key or not hmac.compare_digest(supplied_key, settings.api_key):
		logger.warning("Rejected unauthenticated request path=%s", request.url.path)
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Invalid or missing API key",
			headers={"WWW-Authenticate": "ApiKey"},
		)


def get_woocommerce(request: Request) -> WooCommerceClient | None:
	return request.app.state.woocommerce


def _is_dummyjson(store_url: str) -> bool:
	return urlparse(store_url).hostname in {"dummyjson.com", "www.dummyjson.com"}


def _dummyjson_get(request: Request, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
	"""GET DummyJSON data with request/response diagnostics and safe error mapping."""
	settings: Settings = request.app.state.settings
	session: requests.Session = request.app.state.dummyjson_session
	url = f"{settings.store_url.rstrip('/')}/{path.lstrip('/')}"
	prepared = session.prepare_request(requests.Request("GET", url, params=params))
	final_url = prepared.url or url
	logger.info("DummyJSON request method=GET final_url=%s", final_url)
	try:
		response = session.send(prepared, timeout=settings.timeout_seconds)
	except requests.RequestException as exc:
		logger.exception("DummyJSON request exception final_url=%s", final_url)
		raise HTTPException(
			status_code=504 if isinstance(exc, requests.Timeout) else 502,
			detail={
				"message": "DummyJSON request failed",
				"exception_type": type(exc).__name__,
				"exception": str(exc),
				"upstream_status_code": None,
				"upstream_body": None,
			},
		) from exc

	response_body = response.text[:1000]
	logger.info(
		"DummyJSON response final_url=%s status_code=%s body=%s%s",
		response.url or final_url,
		response.status_code,
		response_body,
		" [truncated to 1000 chars]" if len(response.text) > 1000 else "",
	)
	try:
		response.raise_for_status()
	except requests.HTTPError as exc:
		logger.exception(
			"DummyJSON returned an HTTP error final_url=%s status_code=%s body=%s%s",
			response.url or final_url,
			response.status_code,
			response_body,
			" [truncated to 1000 chars]" if len(response.text) > 1000 else "",
		)
		mapped_status = response.status_code if 400 <= response.status_code < 500 else 502
		raise HTTPException(
			status_code=mapped_status,
			detail={
				"message": "DummyJSON request failed",
				"upstream_status_code": response.status_code,
				"upstream_body": response_body,
			},
		) from exc

	try:
		data = response.json()
	except requests.exceptions.JSONDecodeError as exc:
		logger.exception("DummyJSON returned invalid JSON final_url=%s body=%s", final_url, response_body)
		raise HTTPException(status_code=502, detail="DummyJSON returned invalid JSON") from exc
	if not isinstance(data, dict):
		logger.error("DummyJSON returned unexpected JSON type final_url=%s type=%s", final_url, type(data).__name__)
		raise HTTPException(status_code=502, detail="DummyJSON returned an unexpected response")
	return data


def _dummyjson_cart_to_order(cart: dict[str, Any]) -> dict[str, Any]:
	"""Map a DummyJSON cart into the subset of WooCommerce order fields we expose."""
	line_items = []
	for product in cart.get("products", []):
		quantity = int(product.get("quantity", 0))
		unit_price = float(product.get("price", 0))
		line_items.append(
			{
				"id": product.get("id"),
				"name": product.get("title", ""),
				"product_id": product.get("id"),
				"variation_id": 0,
				"quantity": quantity,
				"subtotal": f"{unit_price * quantity:.2f}",
				"total": f"{float(product.get('discountedTotal', unit_price * quantity)):.2f}",
				"sku": "",
			}
		)

	total = float(cart.get("total", 0))
	discounted_total = float(cart.get("discountedTotal", total))
	return {
		"id": cart.get("id"),
		"status": "processing",
		"currency": "USD",
		"date_created": None,
		"date_modified": None,
		"customer_id": cart.get("userId", 0),
		"total": f"{discounted_total:.2f}",
		"discount_total": f"{max(total - discounted_total, 0):.2f}",
		"shipping_total": "0.00",
		"total_tax": "0.00",
		"billing": {},
		"shipping": {},
		"line_items": line_items,
	}


def _get_dummyjson_orders(
	request: Request,
	*,
	page: int,
	per_page: int,
	customer: int | None = None,
	search: str | None = None,
) -> dict[str, Any]:
	filter_locally = search is not None or customer is not None
	params: dict[str, Any] = (
		{"limit": 0}
		if filter_locally
		else {"limit": per_page, "skip": (page - 1) * per_page}
	)
	result = _dummyjson_get(request, "/carts", params=params)
	carts = result.get("carts", [])
	if customer is not None:
		carts = [cart for cart in carts if cart.get("userId") == customer]
	if search:
		needle = search.casefold()
		carts = [
			cart for cart in carts
			if needle in str(cart.get("id", "")).casefold()
			or needle in str(cart.get("userId", "")).casefold()
			or any(needle in str(item.get("title", "")).casefold() for item in cart.get("products", []))
		]
	if filter_locally:
		total = len(carts)
		skip = (page - 1) * per_page
		carts = carts[skip:skip + per_page]
	else:
		total = result.get("total", len(carts))
		skip = result.get("skip", params["skip"])
	return {
		"data": [_dummyjson_cart_to_order(cart) for cart in carts],
		"pagination": {
			"page": page,
			"per_page": per_page,
			"total": total,
			"total_pages": (total + per_page - 1) // per_page,
			"skip": skip,
		},
	}


@app.exception_handler(WooCommerceError)
async def woocommerce_error_handler(_: Request, exc: WooCommerceError) -> Response:
	logger.error(
		"WooCommerce request failed category=%s upstream_status=%s upstream_body=%s",
		getattr(exc, "category", "upstream_error"),
		getattr(exc, "upstream_status_code", None),
		getattr(exc, "upstream_body", None),
		exc_info=(type(exc), exc, exc.__traceback__),
	)
	return JSONResponse(
		content={
			"detail": exc.public_message,
			"category": getattr(exc, "category", "upstream_error"),
			"upstream_status_code": getattr(exc, "upstream_status_code", None),
			"upstream_body": getattr(exc, "upstream_body", None),
		},
		status_code=exc.status_code,
		headers=exc.headers,
	)


@app.get("/health", tags=["Operations"])
async def health() -> dict[str, str]:
	"""Liveness probe; does not disclose store configuration."""
	return {"status": "ok"}


@app.get("/debug/config", tags=["Operations"], dependencies=[Depends(require_api_key)])
async def debug_config(request: Request) -> dict[str, Any]:
	"""Show connector mode and initialized clients, without returning credentials."""
	settings: Settings = request.app.state.settings
	return {
		"store_url": settings.store_url,
		"is_dummyjson": _is_dummyjson(settings.store_url),
		"woocommerce_client_exists": request.app.state.woocommerce is not None,
		"dummyjson_session_exists": request.app.state.dummyjson_session is not None,
	}


@app.get("/orders", tags=["Orders"], dependencies=[Depends(require_api_key)])
async def get_orders(
	request: Request,
	page: int = Query(1, ge=1),
	per_page: int = Query(100, ge=1, le=100),
	status_filter: str | None = Query(None, alias="status"),
	customer: int | None = Query(None, ge=1),
	after: str | None = Query(None, description="ISO-8601 lower bound for order creation date"),
	before: str | None = Query(None, description="ISO-8601 upper bound for order creation date"),
	order: str | None = Query(None, pattern="^(asc|desc)$"),
	orderby: str | None = Query(None),
	client: WooCommerceClient = Depends(get_woocommerce),
) -> dict[str, Any]:
	"""List a page of orders. DummyJSON carts are converted to order-shaped records."""
	if client is None:
		unsupported = {"status": status_filter, "after": after, "before": before, "order": order, "orderby": orderby}
		if any(value is not None for value in unsupported.values()):
			raise HTTPException(
				status_code=400,
				detail="DummyJSON carts do not support WooCommerce status, date, or sort filters",
			)
		return _get_dummyjson_orders(request, page=page, per_page=per_page, customer=customer)
	params = _provided(
		page=page,
		per_page=per_page,
		status=status_filter,
		customer=customer,
		after=after,
		before=before,
		order=order,
		orderby=orderby,
	)
	return client.get_collection("orders", params)


@app.get("/orders/search", tags=["Orders"], dependencies=[Depends(require_api_key)])
async def search_orders(
	request: Request,
	q: str = Query(..., min_length=1, max_length=200, description="WooCommerce order search text"),
	page: int = Query(1, ge=1),
	per_page: int = Query(100, ge=1, le=100),
	status_filter: str | None = Query(None, alias="status"),
	client: WooCommerceClient = Depends(get_woocommerce),
) -> dict[str, Any]:
	"""Search orders by WooCommerce-supported search text."""
	if client is None:
		return _get_dummyjson_orders(request, page=page, per_page=per_page, search=q)
	return client.get_collection(
		"orders",
		_provided(page=page, per_page=per_page, search=q, status=status_filter),
	)


@app.get("/orders/{order_id}", tags=["Orders"], dependencies=[Depends(require_api_key)])
async def get_order(
	order_id: int,
	request: Request,
	client: WooCommerceClient = Depends(get_woocommerce),
) -> dict[str, Any]:
	"""Retrieve a single order by its numeric WooCommerce ID."""
	if order_id < 1:
		raise HTTPException(status_code=422, detail="order_id must be greater than zero")
	if client is None:
		result = _dummyjson_get(request, f"/carts/{order_id}")
		return _dummyjson_cart_to_order(result)
	return client.get_resource(f"orders/{order_id}")


@app.get("/products", tags=["Products"], dependencies=[Depends(require_api_key)])
async def get_products(
	page: int = Query(1, ge=1),
	per_page: int = Query(100, ge=1, le=100),
	category: int | None = Query(None, ge=1),
	tag: int | None = Query(None, ge=1),
	stock_status: str | None = Query(None),
	order: str | None = Query(None, pattern="^(asc|desc)$"),
	orderby: str | None = Query(None),
	client: WooCommerceClient = Depends(get_woocommerce),
) -> dict[str, Any]:
	"""List a page of products. Use page/per_page to traverse the collection."""
	if client is None:
		raise HTTPException(status_code=501, detail="Product endpoints are unavailable with the DummyJSON carts adapter")
	return client.get_collection(
		"products",
		_provided(
			page=page,
			per_page=per_page,
			category=category,
			tag=tag,
			stock_status=stock_status,
			order=order,
			orderby=orderby,
		),
	)


@app.get("/products/search", tags=["Products"], dependencies=[Depends(require_api_key)])
async def search_products(
	q: str = Query(..., min_length=1, max_length=200, description="Product name, SKU, or other WooCommerce-supported search text"),
	page: int = Query(1, ge=1),
	per_page: int = Query(100, ge=1, le=100),
	client: WooCommerceClient = Depends(get_woocommerce),
) -> dict[str, Any]:
	"""Search products using WooCommerce's search parameter."""
	if client is None:
		raise HTTPException(status_code=501, detail="Product endpoints are unavailable with the DummyJSON carts adapter")
	return client.get_collection("products", _provided(page=page, per_page=per_page, search=q))


def _provided(**values: Any) -> dict[str, Any]:
	"""Remove unset optional query parameters while retaining pagination values."""
	return {key: value for key, value in values.items() if value is not None}
