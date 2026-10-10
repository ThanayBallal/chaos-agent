"""Conservative Robot Shop source scanner for service-to-service HTTP calls.

Each rule requires both a service-host configuration and executable call-site
evidence. Missing files or unmatched patterns simply produce no edge, so the
mapper never adds an edge solely because two services share a Docker network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceRule:
    """Evidence required to confirm one directed source dependency."""

    source: str
    target: str
    checks: tuple[tuple[str, str], ...]
    evidence: str


def _nginx_rule(target: str, variable: str, port: int) -> SourceRule:
    route = f"/api/{target}"
    return SourceRule(
        source="web",
        target=target,
        checks=(
            (
                "web/Dockerfile",
                rf"\b{re.escape(variable)}={re.escape(target)}\b",
            ),
            (
                "web/default.conf.template",
                rf"location\s+{re.escape(route)}/\s*\{{[^}}]*"
                rf"proxy_pass\s+http://\$\{{{re.escape(variable)}\}}:{port}/;",
            ),
        ),
        evidence=f"nginx proxy_pass {route} -> {target}:{port}",
    )


_SOURCE_RULES = (
    _nginx_rule("catalogue", "CATALOGUE_HOST", 8080),
    _nginx_rule("user", "USER_HOST", 8080),
    _nginx_rule("cart", "CART_HOST", 8080),
    _nginx_rule("shipping", "SHIPPING_HOST", 8080),
    _nginx_rule("payment", "PAYMENT_HOST", 8080),
    _nginx_rule("ratings", "RATINGS_HOST", 80),
    SourceRule(
        source="cart",
        target="catalogue",
        checks=(
            (
                "cart/server.js",
                r"process\.env\.CATALOGUE_HOST\s*\|\|\s*['\"]catalogue['\"]",
            ),
            (
                "cart/server.js",
                r"request\(\s*['\"]http://['\"]\s*\+\s*catalogueHost\s*\+"
                r"\s*['\"]:8080/product/",
            ),
        ),
        evidence=(
            "cart/server.js uses CATALOGUE_HOST default 'catalogue' "
            "for HTTP /product requests"
        ),
    ),
    SourceRule(
        source="payment",
        target="cart",
        checks=(
            (
                "payment/payment.py",
                r"CART\s*=\s*os\.getenv\(['\"]CART_HOST['\"],\s*['\"]cart['\"]\)",
            ),
            (
                "payment/payment.py",
                r"requests\.delete\(['\"]http://\{cart\}:8080/cart/",
            ),
        ),
        evidence=(
            "payment/payment.py uses CART_HOST default 'cart' "
            "for HTTP DELETE /cart"
        ),
    ),
    SourceRule(
        source="payment",
        target="user",
        checks=(
            (
                "payment/payment.py",
                r"USER\s*=\s*os\.getenv\(['\"]USER_HOST['\"],\s*['\"]user['\"]\)",
            ),
            (
                "payment/payment.py",
                r"requests\.(?:get|post)\(['\"]http://\{user\}:8080/(?:check|order)/",
            ),
        ),
        evidence=(
            "payment/payment.py uses USER_HOST default 'user' "
            "for HTTP /check and /order requests"
        ),
    ),
    SourceRule(
        source="shipping",
        target="cart",
        checks=(
            ("shipping/Dockerfile", r"\bCART_ENDPOINT=cart:8080\b"),
            (
                "shipping/src/main/java/com/instana/robotshop/shipping/Controller.java",
                r"String\.format\(['\"]http://%s/shipping/['\"],\s*"
                r"getenv\(['\"]CART_ENDPOINT['\"],\s*['\"]cart['\"]\)\)",
            ),
            (
                "shipping/src/main/java/com/instana/robotshop/shipping/CartHelper.java",
                r"new\s+HttpPost\(baseUrl\s*\+\s*id\)",
            ),
        ),
        evidence=(
            "shipping Controller/CartHelper uses CART_ENDPOINT=cart:8080 "
            "for HTTP POST /shipping"
        ),
    ),
    SourceRule(
        source="ratings",
        target="catalogue",
        checks=(
            (
                "ratings/html/src/Kernel.php",
                r"getenv\(['\"]CATALOGUE_URL['\"]\)\s*\?:\s*"
                r"['\"]http://catalogue:8080['\"]",
            ),
            (
                "ratings/html/src/Service/CatalogueService.php",
                r"curl_init\(\$url\)",
            ),
        ),
        evidence=(
            "ratings Kernel.php uses CATALOGUE_URL default "
            "'http://catalogue:8080'; CatalogueService cURL /product"
        ),
    ),
)


def scan_source_edges(root: Path, service_names: set[str]) -> list[dict[str, str]]:
    """Return source-confirmed HTTP edges found below a Robot Shop root."""
    source_root = Path(root)
    file_cache: dict[str, str | None] = {}

    def read(relative_path: str) -> str | None:
        if relative_path not in file_cache:
            path = source_root / relative_path
            try:
                file_cache[relative_path] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                file_cache[relative_path] = None
        return file_cache[relative_path]

    edges: list[dict[str, str]] = []
    for rule in _SOURCE_RULES:
        if rule.source not in service_names or rule.target not in service_names:
            continue

        confirmed = True
        for relative_path, pattern in rule.checks:
            content = read(relative_path)
            if content is None or re.search(pattern, content, flags=re.MULTILINE) is None:
                confirmed = False
                break

        if confirmed:
            edges.append(
                {
                    "src": rule.source,
                    "dst": rule.target,
                    "type": "http",
                    "evidence": rule.evidence,
                }
            )

    return edges
