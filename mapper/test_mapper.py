"""Unit tests for the Compose dependency mapper."""

import tempfile
import unittest
from pathlib import Path

from mapper.mapper import _mock_differences, build_depmap, to_mermaid


COMPOSE = """
services:
  database:
    image: postgres:16
    networks:
      backend:
        aliases: [orders-db]
  cache:
    image: redis:7-alpine
    networks: [backend]
  broker:
    image: rabbitmq:3-management
    networks: [backend]
  api:
    image: example/orders-api:1
    depends_on:
      database:
        condition: service_started
    environment:
      CACHE_HOST: cache
      DATABASE_URL: postgres://user:secret@orders-db:5432/orders
      SELF_URL: http://api:8080
    networks: [backend]
  worker:
    image: example/worker:1
    environment:
      - AMQP_URL=amqp://broker:5672
    networks: [backend]
  web:
    image: nginx:1.27
    depends_on: [api]
    networks: [backend]
"""


class MapperTests(unittest.TestCase):
    def build(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            compose_path = Path(temporary_directory) / "compose.yaml"
            compose_path.write_text(COMPOSE, encoding="utf-8")
            return build_depmap(compose_path)

    def test_build_depmap_contract_and_dependencies(self):
        depmap = self.build()
        services = {item["name"]: item for item in depmap["services"]}
        edges = {(item["src"], item["dst"]): item for item in depmap["edges"]}

        self.assertEqual(depmap["app"], "robot-shop")
        self.assertEqual(services["database"]["kind"], "db")
        self.assertEqual(services["cache"]["kind"], "cache")
        self.assertEqual(services["broker"]["kind"], "queue")
        self.assertEqual(services["web"]["kind"], "proxy")
        self.assertEqual(services["api"]["kind"], "app")
        self.assertEqual(services["api"]["depends_on"], ["database", "cache"])
        self.assertEqual(edges[("api", "database")]["type"], "db")
        self.assertIn("compose depends_on", edges[("api", "database")]["evidence"])
        self.assertIn("DATABASE_URL", edges[("api", "database")]["evidence"])
        self.assertNotIn("secret", edges[("api", "database")]["evidence"])
        self.assertEqual(edges[("api", "cache")]["type"], "cache")
        self.assertEqual(edges[("api", "cache")]["evidence"], "env CACHE_HOST=cache")
        self.assertEqual(edges[("worker", "broker")]["type"], "queue")
        self.assertNotIn(("api", "api"), edges)
        self.assertTrue(all(item["evidence"] for item in depmap["edges"]))

    def test_mermaid_contains_nodes_edges_and_styles(self):
        diagram = to_mermaid(self.build())
        self.assertTrue(diagram.startswith("graph LR\n"))
        self.assertIn('"api (app)"', diagram)
        self.assertIn('-->|"cache"|', diagram)
        self.assertIn("classDef queue", diagram)

    def test_mock_comparison_ignores_evidence(self):
        depmap = self.build()
        copy = {
            "app": depmap["app"],
            "services": [{**item, "evidence": ["mock"]} for item in depmap["services"]],
            "edges": [{**item, "evidence": "mock"} for item in depmap["edges"]],
        }
        self.assertEqual(_mock_differences(depmap, copy), [])

    def test_missing_compose_file_has_actionable_error(self):
        with self.assertRaisesRegex(FileNotFoundError, "sibling directory named 'robot-shop'"):
            build_depmap("does-not-exist.yaml")


if __name__ == "__main__":
    unittest.main()
