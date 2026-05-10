#!/usr/bin/env python3
"""将已完成 LLM 提取的论文 JSON 导入 Neo4j Bronze Layer。"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

try:
    from neo4j import GraphDatabase
    from neo4j import Driver, ManagedTransaction
    NEO4J_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover
    GraphDatabase = None
    Driver = Any
    ManagedTransaction = Any
    NEO4J_IMPORT_ERROR = exc

from pipeline.utils import DATA_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


class GraphIngestor:
    ENTITY_MAPPINGS = {
        "research_tasks": {
            "label": "TaskMention",
            "relationship": "ADDRESSES_TASK",
        },
        "proposed_methods": {
            "label": "MethodMention",
            "relationship": "PROPOSES_METHOD",
        },
        "datasets": {
            "label": "DatasetMention",
            "relationship": "USES_DATASET",
        },
        "evaluation_metrics": {
            "label": "MetricMention",
            "relationship": "USES_METRIC",
        },
        "baselines": {
            "label": "BaselineMention",
            "relationship": "COMPARES_WITH_BASELINE",
        },
        "self_admitted_limitations": {
            "label": "LimitationMention",
            "relationship": "HAS_LIMITATION",
        },
        "addressed_existing_flaws": {
            "label": "FlawMention",
            "relationship": "ADDRESSES_FLAW",
            "extra_relationship_properties": ["targeted_baseline"],
        },
    }

    EXCLUDED_FILENAMES = {"metadata.json", "sections.json", "layout.json"}
    EXCLUDED_SUFFIXES = ("_llm_raw.json",)

    def __init__(
        self,
        uri: str,
        username: str,
        password: str,
        database: str | None = None,
    ) -> None:
        if NEO4J_IMPORT_ERROR is not None or GraphDatabase is None:
            raise ImportError(
                "neo4j Python package is required. Install it with `pip install neo4j`."
            ) from NEO4J_IMPORT_ERROR

        self.driver: Driver = GraphDatabase.driver(uri, auth=(username, password))
        self.database = database
        self.driver.verify_connectivity()

    def __enter__(self) -> "GraphIngestor":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.driver.close()

    def initialize_schema(self) -> None:
        statements = [
            """
            CREATE CONSTRAINT paper_id_unique IF NOT EXISTS
            FOR (p:Paper)
            REQUIRE p.paper_id IS UNIQUE
            """
        ]

        for mapping in self.ENTITY_MAPPINGS.values():
            label = mapping["label"]
            statements.append(
                f"""
                CREATE CONSTRAINT {label.lower()}_name_unique IF NOT EXISTS
                FOR (m:{label})
                REQUIRE m.name IS UNIQUE
                """
            )

        with self.driver.session(database=self.database) as session:
            for statement in statements:
                session.run(statement).consume()

    def discover_final_json_files(self, data_dir: str | Path, limit: int | None = None) -> list[Path]:
        root = Path(data_dir)
        if not root.exists():
            raise FileNotFoundError(f"Data directory not found: {root}")

        result_files: list[Path] = []
        for paper_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            json_path = self._resolve_result_json(paper_dir)
            if json_path is not None:
                result_files.append(json_path)

        if limit is not None:
            result_files = result_files[:limit]
        return result_files

    def resolve_single_target(self, data_dir: str | Path, single: str) -> Path:
        candidate = Path(single)
        if candidate.exists():
            if candidate.is_file():
                return candidate
            if candidate.is_dir():
                json_path = self._resolve_result_json(candidate)
                if json_path is None:
                    raise FileNotFoundError(f"No final result JSON found in {candidate}")
                return json_path

        paper_dir = Path(data_dir) / single
        if paper_dir.exists() and paper_dir.is_dir():
            json_path = self._resolve_result_json(paper_dir)
            if json_path is None:
                raise FileNotFoundError(f"No final result JSON found in {paper_dir}")
            return json_path

        json_path = Path(data_dir) / single / f"{single}.json"
        if json_path.exists():
            return json_path

        raise FileNotFoundError(f"Unable to resolve --single target: {single}")

    def ingest_json_dir(self, data_dir: str | Path, limit: int | None = None) -> tuple[int, list[tuple[Path, str]]]:
        targets = self.discover_final_json_files(data_dir, limit=limit)
        success = 0
        failures: list[tuple[Path, str]] = []

        for index, json_path in enumerate(targets, 1):
            log.info("=" * 60)
            log.info("[%d/%d] Importing: %s", index, len(targets), json_path)
            try:
                paper_id = self.ingest_json_file(json_path)
                success += 1
                log.info("[%s] Import completed", paper_id)
            except Exception as exc:  # noqa: BLE001
                failures.append((json_path, str(exc)))
                log.error("[%s] Import failed: %s", json_path, exc)

        return success, failures

    def ingest_json_file(self, json_path: str | Path) -> str:
        path = Path(json_path)
        with path.open("r", encoding="utf-8") as file:
            paper_json = json.load(file)

        normalized = self._normalize_paper_json(paper_json)

        with self.driver.session(database=self.database) as session:
            session.execute_write(self._write_paper_transaction, normalized)

        return normalized["paper_id"]

    @classmethod
    def _write_paper_transaction(
        cls,
        tx: ManagedTransaction,
        paper_json: dict[str, Any],
    ) -> None:
        paper_id = paper_json["paper_id"]
        cls._merge_paper(tx, paper_json)
        cls._delete_existing_sections(tx, paper_id)
        cls._delete_existing_mention_relationships(tx, paper_id)
        cls._create_sections(tx, paper_id, paper_json["sections"])
        cls._merge_mentions_and_relationships(tx, paper_id, paper_json["extracted_entities"])
        cls._delete_orphan_mentions(tx)

    @staticmethod
    def _merge_paper(tx: ManagedTransaction, paper_json: dict[str, Any]) -> None:
        tx.run(
            """
            MERGE (p:Paper {paper_id: $paper_id})
            SET p += $properties
            """,
            paper_id=paper_json["paper_id"],
            properties=GraphIngestor._drop_empty_values(
                {
                    "title": paper_json.get("title"),
                    "authors": paper_json.get("authors"),
                    "publication_year": paper_json.get("publication_year"),
                    "venue": paper_json.get("venue"),
                    "DOI": paper_json.get("DOI"),
                    "url": paper_json.get("url"),
                }
            ),
        ).consume()

    @staticmethod
    def _delete_existing_sections(tx: ManagedTransaction, paper_id: str) -> None:
        tx.run(
            """
            MATCH (p:Paper {paper_id: $paper_id})-[:HAS_SECTION]->(s:Section)
            DETACH DELETE s
            """,
            paper_id=paper_id,
        ).consume()

    @classmethod
    def _delete_existing_mention_relationships(cls, tx: ManagedTransaction, paper_id: str) -> None:
        relationship_types = [mapping["relationship"] for mapping in cls.ENTITY_MAPPINGS.values()]
        tx.run(
            """
            MATCH (p:Paper {paper_id: $paper_id})-[r]->(m)
            WHERE type(r) IN $relationship_types
              AND any(label IN labels(m) WHERE label IN $mention_labels)
            DELETE r
            """,
            paper_id=paper_id,
            relationship_types=relationship_types,
            mention_labels=[mapping["label"] for mapping in cls.ENTITY_MAPPINGS.values()],
        ).consume()

    @classmethod
    def _create_sections(
        cls,
        tx: ManagedTransaction,
        paper_id: str,
        sections: list[dict[str, Any]],
    ) -> None:
        for section in sections:
            if not isinstance(section, dict):
                raise ValueError(f"Section must be an object: {section!r}")

            tx.run(
                """
                MATCH (p:Paper {paper_id: $paper_id})
                CREATE (s:Section {
                    type: $type,
                    content: $content
                })
                CREATE (p)-[:HAS_SECTION]->(s)
                """,
                paper_id=paper_id,
                type=cls._normalize_string(section.get("type")),
                content=cls._normalize_string(section.get("content")),
            ).consume()

    @classmethod
    def _merge_mentions_and_relationships(
        cls,
        tx: ManagedTransaction,
        paper_id: str,
        extracted_entities: dict[str, Any],
    ) -> None:
        for entity_key, mapping in cls.ENTITY_MAPPINGS.items():
            entities = extracted_entities.get(entity_key) or []
            if not isinstance(entities, list):
                raise ValueError(
                    f"extracted_entities.{entity_key} must be a list, got {type(entities).__name__}"
                )

            for entity in entities:
                cls._merge_single_mention_relationship(
                    tx=tx,
                    paper_id=paper_id,
                    entity=entity,
                    label=mapping["label"],
                    relationship=mapping["relationship"],
                    extra_relationship_properties=mapping.get("extra_relationship_properties", []),
                )

    @classmethod
    def _merge_single_mention_relationship(
        cls,
        tx: ManagedTransaction,
        paper_id: str,
        entity: dict[str, Any],
        label: str,
        relationship: str,
        extra_relationship_properties: list[str],
    ) -> None:
        if not isinstance(entity, dict):
            raise ValueError(f"{label} entity must be an object: {entity!r}")

        name = cls._normalize_string(entity.get("name_in_paper"))
        if not name:
            raise ValueError(f"{label} entity is missing name_in_paper: {entity!r}")

        relationship_properties = cls._drop_empty_values(
            {
                "evidence_quote": cls._normalize_string(entity.get("evidence_quote")),
                "evidence_block_id": cls._normalize_int(entity.get("evidence_block_id")),
                **{
                    property_name: cls._normalize_string(entity.get(property_name))
                    for property_name in extra_relationship_properties
                },
            }
        )

        tx.run(
            f"""
            MERGE (m:{label} {{name: $name}})
            ON CREATE SET m.aliases = $aliases, m.semantic_definition = $semantic_definition
            WITH m
            MATCH (p:Paper {{paper_id: $paper_id}})
            MERGE (p)-[r:{relationship}]->(m)
            SET r += $relationship_properties
            """,
            paper_id=paper_id,
            name=name,
            aliases=cls._ensure_string_list(entity.get("aliases")),
            semantic_definition=cls._normalize_string(entity.get("semantic_definition")),
            relationship_properties=relationship_properties,
        ).consume()

    @classmethod
    def _delete_orphan_mentions(cls, tx: ManagedTransaction) -> None:
        for mapping in cls.ENTITY_MAPPINGS.values():
            label = mapping["label"]
            tx.run(
                f"""
                MATCH (m:{label})
                WHERE NOT (m)--()
                DELETE m
                """
            ).consume()

    @classmethod
    def _resolve_result_json(cls, paper_dir: Path) -> Path | None:
        preferred = paper_dir / f"{paper_dir.name}.json"
        if cls._is_final_result_json(preferred):
            return preferred

        candidates = [
            path for path in sorted(paper_dir.glob("*.json"))
            if cls._is_final_result_json(path)
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    @classmethod
    def _is_final_result_json(cls, path: Path) -> bool:
        if not path.exists() or not path.is_file() or path.suffix.lower() != ".json":
            return False
        if path.name in cls.EXCLUDED_FILENAMES:
            return False
        return not any(path.name.endswith(suffix) for suffix in cls.EXCLUDED_SUFFIXES)

    @classmethod
    def _normalize_paper_json(cls, paper_json: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(paper_json, dict):
            raise ValueError("Paper JSON must be an object")

        metadata = paper_json.get("metadata") or {}
        if metadata and not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")

        paper_id = cls._normalize_string(paper_json.get("paper_id") or metadata.get("paper_id"))
        if not paper_id:
            raise ValueError("Missing required field: paper_id")

        sections = paper_json.get("sections") or []
        if not isinstance(sections, list):
            raise ValueError("sections must be a list")

        extracted_entities = paper_json.get("extracted_entities") or {}
        if not isinstance(extracted_entities, dict):
            raise ValueError("extracted_entities must be an object")

        return {
            "paper_id": paper_id,
            "title": cls._pick_string(metadata, paper_json, "title"),
            "authors": cls._ensure_string_list(metadata.get("authors", paper_json.get("authors"))),
            "publication_year": cls._pick_int(metadata, paper_json, "publication_year"),
            "venue": cls._pick_string(metadata, paper_json, "venue"),
            "DOI": cls._pick_string(metadata, paper_json, "DOI"),
            "url": cls._pick_string(metadata, paper_json, "url"),
            "sections": sections,
            "extracted_entities": extracted_entities,
        }

    @staticmethod
    def _pick_string(primary: dict[str, Any], fallback: dict[str, Any], key: str) -> str | None:
        return GraphIngestor._normalize_string(primary.get(key) if key in primary else fallback.get(key))

    @staticmethod
    def _pick_int(primary: dict[str, Any], fallback: dict[str, Any], key: str) -> int | None:
        value = primary.get(key) if key in primary else fallback.get(key)
        return GraphIngestor._normalize_int(value)

    @staticmethod
    def _normalize_string(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _normalize_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ensure_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        normalized: list[str] = []
        for item in value:
            text = GraphIngestor._normalize_string(item)
            if text is not None:
                normalized.append(text)
        return normalized

    @staticmethod
    def _drop_empty_values(properties: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in properties.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, list) and not value:
                continue
            cleaned[key] = value
        return cleaned


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Bronze Layer paper JSON into Neo4j")
    parser.add_argument("--data-dir", type=str, default=str(DATA_DIR), help="论文数据目录")
    parser.add_argument("--single", type=str, default=None, help="只导入指定 cite_key、目录路径或 JSON 文件路径")
    parser.add_argument("--limit", type=int, default=None, help="限制导入论文数量")
    parser.add_argument("--uri", type=str, default=os.getenv("NEO4J_URI", "neo4j://localhost:7687"), help="Neo4j URI")
    parser.add_argument("--username", type=str, default=os.getenv("NEO4J_USERNAME", "neo4j"), help="Neo4j 用户名")
    parser.add_argument("--password", type=str, default=os.getenv("NEO4J_PASSWORD"), help="Neo4j 密码")
    parser.add_argument("--database", type=str, default=os.getenv("NEO4J_DATABASE"), help="Neo4j database 名称")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要导入的文件")
    parser.add_argument("--init-schema-only", action="store_true", help="只初始化约束，不导入数据")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir)

    if args.single:
        preview_ingestor = GraphIngestor.__new__(GraphIngestor)
        target_files = [preview_ingestor.resolve_single_target(data_dir, args.single)]
    else:
        preview_ingestor = GraphIngestor.__new__(GraphIngestor)
        target_files = preview_ingestor.discover_final_json_files(data_dir, limit=args.limit)

    if args.dry_run:
        if not target_files:
            log.warning("No final result JSON files found under %s", data_dir)
            return 0
        log.info("Dry run: %d files would be imported", len(target_files))
        for path in target_files:
            log.info("  %s", path)
        return 0

    if not args.password:
        raise SystemExit("Neo4j password is required. Pass --password or set NEO4J_PASSWORD.")

    with GraphIngestor(
        uri=args.uri,
        username=args.username,
        password=args.password,
        database=args.database,
    ) as ingestor:
        log.info("Initializing Neo4j schema")
        ingestor.initialize_schema()

        if args.init_schema_only:
            log.info("Schema initialization completed")
            return 0

        if args.single:
            paper_id = ingestor.ingest_json_file(target_files[0])
            log.info("[%s] Import completed", paper_id)
            return 0

        if not target_files:
            log.warning("No final result JSON files found under %s", data_dir)
            return 0

        log.info("Found %d papers to import", len(target_files))
        success = 0
        failures: list[tuple[Path, str]] = []

        for index, json_path in enumerate(target_files, 1):
            log.info("=" * 60)
            log.info("[%d/%d] Importing: %s", index, len(target_files), json_path)
            try:
                paper_id = ingestor.ingest_json_file(json_path)
                success += 1
                log.info("[%s] Import completed", paper_id)
            except Exception as exc:  # noqa: BLE001
                failures.append((json_path, str(exc)))
                log.error("[%s] Import failed: %s", json_path, exc)

        log.info("=" * 60)
        log.info("Done. %d/%d papers imported successfully", success, len(target_files))
        if failures:
            log.error("Failed imports: %d", len(failures))
            for path, error in failures:
                log.error("  %s -> %s", path, error)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
