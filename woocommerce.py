"""Small read-only client for the WooCommerce REST API v3."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger("woocommerce_connector.client")


class WooCommerceError(Exception):
	"""Safe, client-facing WooCommerce error."""

	def __init__(
		self,
		public_message: str,
		status_code: int = 502,
		headers: dict[str, str] | None = None,
		*,
		category: str = "upstream_error",
		upstream_status_code: int | None = None,
		upstream_body: str | None = None,
	) -> None:
		super().__init__(public_message)
		self.public_message = public_message
		self.status_code = status_code
		self.headers = headers or {}
		self.category = category
		self.upstream_status_code = upstream_status_code
		self.upstream_body = upstream_body


class WooCommerceClient:
	"""Per-process HTTP client that only exposes GET operations."""

	def __init__(
		self,
		store_url: str,
		consumer_key: str,
		consumer_secret: str,
		timeout: float = 20,
	) -> None:
		parsed = urlparse(store_url)
		is_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
		if parsed.scheme not in ({"https", "http"} if is_local else {"https"}) or not parsed.netloc:
			raise RuntimeError("WOOCOMMERCE_URL must be an HTTPS URL (HTTP is permitted for localhost only)")
		if parsed.query or parsed.fragment:
			raise RuntimeError("WOOCOMMERCE_URL must not contain a query string or fragment")

		self.base_url = f"{store_url.rstrip('/')}/wp-json/wc/v3"
		self.timeout = timeout
		self.session = requests.Session()
		self.session.auth = (consumer_key, consumer_secret)
		self.session.headers.update(
			{
				"Accept": "application/json",
				"User-Agent": "woocommerce-readonly-connector/1.0",
			}
		)

	def close(self) -> None:
		self.session.close()

	def get_resource(self, resource: str) -> dict[str, Any]:
		response = self._get(resource)
		self._raise_for_status(response, resource=resource, is_collection=False)
		try:
			body = response.json()
		except requests.exceptions.JSONDecodeError as exc:
			logger.error("WooCommerce returned invalid JSON for resource=%s", resource)
			raise WooCommerceError("WooCommerce returned an invalid response") from exc
		if not isinstance(body, dict):
			raise WooCommerceError("WooCommerce returned an unexpected response")
		return body

	def get_collection(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
		response = self._get(resource, params=params)
		self._raise_for_status(response, resource=resource, is_collection=True)
		try:
			body = response.json()
		except requests.exceptions.JSONDecodeError as exc:
			logger.error("WooCommerce returned invalid JSON for resource=%s", resource)
			raise WooCommerceError("WooCommerce returned an invalid response") from exc
		if not isinstance(body, list):
			raise WooCommerceError("WooCommerce returned an unexpected collection response")

		return {
			"data": body,
			"pagination": {
				"page": params["page"],
				"per_page": params["per_page"],
				"total": _int_header(response.headers.get("X-WP-Total")),
				"total_pages": _int_header(response.headers.get("X-WP-TotalPages")),
			},
		}

	def _get(self, resource: str, params: dict[str, Any] | None = None) -> requests.Response:
		# Resource paths are only composed from fixed route names and numeric IDs.
		url = f"{self.base_url}/{resource}"
		try:
			response = self.session.get(url, params=params, timeout=self.timeout)
		except requests.Timeout as exc:
			logger.exception("WooCommerce request timed out resource=%s", resource)
			raise WooCommerceError(
				f"WooCommerce request timed out while requesting {resource}",
				status_code=504,
				category="timeout",
			) from exc
		except (
			requests.exceptions.InvalidURL,
			requests.exceptions.MissingSchema,
			requests.exceptions.InvalidSchema,
		) as exc:
			logger.exception("Invalid WooCommerce URL resource=%s url=%s", resource, url)
			raise WooCommerceError(
				f"Invalid WooCommerce URL for resource {resource}: {type(exc).__name__}: {exc}",
				status_code=502,
				category="invalid_url",
			) from exc
		except requests.RequestException as exc:
			logger.exception("WooCommerce HTTP request failed resource=%s url=%s", resource, url)
			raise WooCommerceError(
				f"Unable to reach WooCommerce for {resource}: {type(exc).__name__}: {exc}",
				status_code=502,
				category="connection_error",
			) from exc
		if not response.ok:
			logger.error(
				"WooCommerce HTTP error method=GET resource=%s status=%s body=%s",
				resource,
				response.status_code,
				response.text[:8192],
			)
		else:
			logger.info("woocommerce_request method=GET resource=%s status=%s", resource, response.status_code)
		return response

	@staticmethod
	def _raise_for_status(
		response: requests.Response,
		*,
		resource: str,
		is_collection: bool,
	) -> None:
		if response.ok:
			return
		body = response.text[:8192]
		api_message = _api_error_message(body)

		if response.status_code == 429:
			retry_after = response.headers.get("Retry-After", "60")
			logger.warning("WooCommerce rate limit reached")
			raise WooCommerceError(
				api_message or "WooCommerce rate limit reached; retry later",
				status_code=503,
				headers={"Retry-After": retry_after},
				category="rate_limited",
				upstream_status_code=response.status_code,
				upstream_body=body,
			)
		if response.status_code in {401, 403}:
			raise WooCommerceError(
				api_message or "WooCommerce rejected the configured credentials",
				status_code=502,
				category="authentication_failure",
				upstream_status_code=response.status_code,
				upstream_body=body,
			)
		if response.status_code == 404:
			message = api_message or (
				"WooCommerce API route was not found; verify the REST API base URL"
				if is_collection
				else "WooCommerce resource not found"
			)
			category = "endpoint_mismatch" if is_collection else "resource_not_found"
			raise WooCommerceError(
				message,
				status_code=502 if is_collection else 404,
				category=category,
				upstream_status_code=response.status_code,
				upstream_body=body,
			)
		raise WooCommerceError(
			api_message or f"WooCommerce returned HTTP {response.status_code} for {resource}",
			status_code=502,
			category="upstream_error",
			upstream_status_code=response.status_code,
			upstream_body=body,
		)


def _api_error_message(body: str) -> str | None:
	"""Extract WordPress/WooCommerce's human-readable error without hiding its body."""
	try:
		payload = json.loads(body)
		message = payload.get("message") if isinstance(payload, dict) else None
		if isinstance(message, str) and message.strip():
			return message.strip()
	except (ValueError, TypeError):
		pass
	return body.strip() or None


def _int_header(value: str | None) -> int | None:
	if value is None:
		return None
	try:
		return int(value)
	except ValueError:
		return None
