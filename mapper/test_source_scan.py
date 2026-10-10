"""Tests for source-confirmed Robot Shop HTTP dependencies."""

import tempfile
import unittest
from pathlib import Path

from mapper.mapper import build_depmap
from mapper.source_scan import scan_source_edges


SERVICES = {
    "web",
    "catalogue",
    "user",
    "cart",
    "shipping",
    "payment",
    "ratings",
}

SOURCE_FILES = {
    "web/Dockerfile": """
ENV CATALOGUE_HOST=catalogue USER_HOST=user CART_HOST=cart \\
    SHIPPING_HOST=shipping PAYMENT_HOST=payment RATINGS_HOST=ratings
""",
    "web/default.conf.template": """
location /api/catalogue/ { proxy_pass http://${CATALOGUE_HOST}:8080/; }
location /api/user/ { proxy_pass http://${USER_HOST}:8080/; }
location /api/cart/ { proxy_pass http://${CART_HOST}:8080/; }
location /api/shipping/ { proxy_pass http://${SHIPPING_HOST}:8080/; }
location /api/payment/ { proxy_pass http://${PAYMENT_HOST}:8080/; }
location /api/ratings/ { proxy_pass http://${RATINGS_HOST}:80/; }
""",
    "cart/server.js": """
var catalogueHost = process.env.CATALOGUE_HOST || 'catalogue';
request('http://' + catalogueHost + ':8080/product/' + sku, callback);
""",
    "payment/payment.py": """
CART = os.getenv('CART_HOST', 'cart')
USER = os.getenv('USER_HOST', 'user')
requests.get('http://{user}:8080/check/{id}'.format(user=USER, id=id))
requests.delete('http://{cart}:8080/cart/{id}'.format(cart=CART, id=id))
""",
    "shipping/Dockerfile": "ENV CART_ENDPOINT=cart:8080\n",
    "shipping/src/main/java/com/instana/robotshop/shipping/Controller.java": """
String CART_URL = String.format("http://%s/shipping/", getenv("CART_ENDPOINT", "cart"));
""",
    "shipping/src/main/java/com/instana/robotshop/shipping/CartHelper.java": """
HttpPost postRequest = new HttpPost(baseUrl + id);
""",
    "ratings/html/src/Kernel.php": """
getenv('CATALOGUE_URL') ?: 'http://catalogue:8080';
""",
    "ratings/html/src/Service/CatalogueService.php": "curl_init($url);\n",
}


class SourceScanTests(unittest.TestCase):
    def make_source_tree(self, files=SOURCE_FILES):
        temporary_directory = tempfile.TemporaryDirectory()
        root = Path(temporary_directory.name)
        for relative_path, content in files.items():
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return temporary_directory, root

    def test_all_confirmed_http_edges_are_found(self):
        temporary_directory, root = self.make_source_tree()
        self.addCleanup(temporary_directory.cleanup)

        edges = scan_source_edges(root, SERVICES)
        pairs = {(edge["src"], edge["dst"]) for edge in edges}

        self.assertEqual(
            pairs,
            {
                ("web", "catalogue"),
                ("web", "user"),
                ("web", "cart"),
                ("web", "shipping"),
                ("web", "payment"),
                ("web", "ratings"),
                ("cart", "catalogue"),
                ("payment", "cart"),
                ("payment", "user"),
                ("shipping", "cart"),
                ("ratings", "catalogue"),
            },
        )
        self.assertTrue(all(edge["type"] == "http" for edge in edges))
        self.assertTrue(all(edge["evidence"] for edge in edges))

    def test_missing_call_site_does_not_create_edge(self):
        files = dict(SOURCE_FILES)
        files["cart/server.js"] = """
var catalogueHost = process.env.CATALOGUE_HOST || 'catalogue';
"""
        temporary_directory, root = self.make_source_tree(files)
        self.addCleanup(temporary_directory.cleanup)

        pairs = {
            (edge["src"], edge["dst"])
            for edge in scan_source_edges(root, SERVICES)
        }
        self.assertNotIn(("cart", "catalogue"), pairs)

    def test_missing_hostname_default_does_not_create_edge(self):
        files = dict(SOURCE_FILES)
        files["payment/payment.py"] = """
requests.get('http://{user}:8080/check/{id}'.format(user=USER, id=id))
requests.delete('http://{cart}:8080/cart/{id}'.format(cart=CART, id=id))
"""
        temporary_directory, root = self.make_source_tree(files)
        self.addCleanup(temporary_directory.cleanup)

        pairs = {
            (edge["src"], edge["dst"])
            for edge in scan_source_edges(root, SERVICES)
        }
        self.assertNotIn(("payment", "user"), pairs)
        self.assertNotIn(("payment", "cart"), pairs)

    def test_build_depmap_merges_compose_and_source_evidence(self):
        files = {
            "web/Dockerfile": "ENV CART_HOST=cart\n",
            "web/default.conf.template": """
location /api/cart/ { proxy_pass http://${CART_HOST}:8080/; }
""",
        }
        temporary_directory, root = self.make_source_tree(files)
        self.addCleanup(temporary_directory.cleanup)
        compose_path = root / "docker-compose.yaml"
        compose_path.write_text(
            """
services:
  cart:
    image: example/rs-cart:1
  web:
    image: example/rs-web:1
    depends_on: [cart]
""",
            encoding="utf-8",
        )

        depmap = build_depmap(compose_path)
        edges = depmap["edges"]

        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["src"], "web")
        self.assertEqual(edges[0]["dst"], "cart")
        self.assertEqual(
            edges[0]["evidence"],
            "compose depends_on; nginx proxy_pass /api/cart -> cart:8080",
        )


if __name__ == "__main__":
    unittest.main()
