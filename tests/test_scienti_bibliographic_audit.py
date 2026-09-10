import unittest

import mongomock

from yuku.scienti_bibliographic_audit import (
    ScientiBibliographicEnrichmentAuditor,
)


class ScientiBibliographicEnrichmentAuditorTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.auditor = ScientiBibliographicEnrichmentAuditor(self.db)

    def _run(self, documents):
        self.db.base.insert_many(documents)
        self.db.final.insert_many(documents)
        self.db.minciencias_measurement_runs.insert_one(
            {
                "_id": "materialization",
                "status": "complete",
                "summary": {
                    "documents": len(documents),
                    "critical_anomalies": 0,
                },
            }
        )
        return self.auditor.audit(
            audit_name="audit_test",
            run_name="run_test",
            base_collection="base",
            final_collection="final",
            materialization_run_name="materialization",
        )

    def test_clean_bibliographic_materialization_passes(self):
        result = self._run(
            [
                {
                    "_id": "clean",
                    "source": {
                        "name": "Libro contenedor",
                        "publisher": {"name": "Voluntad"},
                    },
                    "bibliographic_info": {
                        "publisher": {"name": "Voluntad"},
                        "book_title": "Libro contenedor",
                        "scienti": {
                            "fields": {
                                "publisher": {
                                    "status": "consistent",
                                    "value": "Voluntad",
                                },
                                "book_title": {
                                    "status": "consistent",
                                    "value": "Libro contenedor",
                                },
                                "volume": {"status": "consistent", "value": "2"},
                                "edition": {"status": "consistent", "value": "3a"},
                                "publication_place": {
                                    "status": "consistent",
                                    "value": "Bogotá",
                                },
                                "language": {
                                    "status": "consistent",
                                    "value": "Español",
                                },
                            }
                        },
                    },
                }
            ]
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["critical_anomalies"], 0)

    def test_accented_language_names_pass_the_mongodb_gate(self):
        documents = []
        for index, language in enumerate(
            ["Español", "Inglés", "Portugués", "Francés", "Alemán"]
        ):
            documents.append(
                {
                    "_id": f"language-{index}",
                    "source": {},
                    "bibliographic_info": {
                        "scienti": {
                            "fields": {
                                "language": {
                                    "status": "consistent",
                                    "value": language,
                                }
                            }
                        }
                    },
                }
            )
        result = self._run(documents)
        self.assertEqual(result["checks"]["invalid_language"], 0)
        self.assertEqual(result["status"], "passed")

    def test_valid_multiple_publisher_entities_pass_without_mutating_raw(self):
        raw = "Instituto A Instituto B"
        result = self._run(
            [
                {
                    "_id": "multiple-publisher",
                    "source": {"publisher": {"name": raw}},
                    "bibliographic_info": {
                        "publisher": {"name": raw},
                        "scienti": {
                            "fields": {
                                "publisher": {
                                    "status": "consistent",
                                    "value": raw,
                                }
                            },
                            "publisher_entities": {
                                "status": "resolved_multiple",
                                "raw_value": raw,
                                "rule": "curated_exact_composite",
                                "rule_version": "scienti-publisher-entities-v1",
                                "confidence": "high",
                                "entities": [
                                    {
                                        "authority_id": "publisher:a",
                                        "name": "Instituto A",
                                        "normalized_name": "instituto a",
                                    },
                                    {
                                        "authority_id": "publisher:b",
                                        "name": "Instituto B",
                                        "normalized_name": "instituto b",
                                    },
                                ],
                            },
                        },
                    },
                }
            ]
        )
        self.assertEqual(result["checks"]["invalid_publisher_entities"], 0)
        self.assertEqual(result["checks"]["publisher_entity_raw_mismatch"], 0)
        self.assertEqual(result["status"], "passed")

    def test_malformed_or_mutating_publisher_entities_fail(self):
        result = self._run(
            [
                {
                    "_id": "bad-entities",
                    "source": {"publisher": {"name": "Changed value"}},
                    "bibliographic_info": {
                        "scienti": {
                            "fields": {},
                            "publisher_entities": {
                                "status": "resolved_multiple",
                                "raw_value": "Original composite",
                                "entities": [
                                    {
                                        "name": "Only one",
                                        "normalized_name": "only one",
                                    }
                                ],
                            },
                        }
                    },
                }
            ]
        )
        self.assertEqual(result["checks"]["invalid_publisher_entities"], 1)
        self.assertEqual(result["checks"]["publisher_entity_raw_mismatch"], 1)
        self.assertEqual(
            result["checks"]["publisher_entities_without_consistent_source"], 1
        )
        self.assertEqual(result["status"], "failed")

    def test_malformed_publisher_entity_container_is_reported_not_crashed(self):
        result = self._run(
            [
                {
                    "_id": "bad-container",
                    "source": {"publisher": {"name": "Editorial A / Editorial B"}},
                    "bibliographic_info": {
                        "publisher": {"name": "Editorial A / Editorial B"},
                        "scienti": {
                            "fields": {
                                "publisher": {
                                    "status": "consistent",
                                    "value": "Editorial A / Editorial B",
                                }
                            },
                            "publisher_entities": ["invalid"],
                        }
                    },
                }
            ]
        )
        self.assertEqual(result["checks"]["invalid_publisher_entities"], 1)
        self.assertEqual(result["status"], "failed")

    def test_metadata_leaks_fail_the_gate_and_are_reported_separately(self):
        result = self._run(
            [
                {
                    "_id": "publisher",
                    "source": {"publisher": {"name": "978-958-44-2350-4"}},
                    "bibliographic_info": {"scienti": {"fields": {}}},
                },
                {
                    "_id": "volume",
                    "source": {},
                    "bibliographic_info": {
                        "scienti": {
                            "fields": {
                                "volume": {"status": "consistent", "value": "págs"}
                            }
                        }
                    },
                },
                {
                    "_id": "edition",
                    "source": {},
                    "bibliographic_info": {
                        "scienti": {
                            "fields": {
                                "edition": {
                                    "status": "consistent",
                                    "value": "3, Serie: 1, Autor del documento original: A",
                                }
                            }
                        }
                    },
                },
                {
                    "_id": "place",
                    "source": {},
                    "bibliographic_info": {
                        "scienti": {
                            "fields": {
                                "publication_place": {
                                    "status": "consistent",
                                    "value": "2014",
                                }
                            }
                        }
                    },
                },
                {
                    "_id": "pages",
                    "source": {},
                    "bibliographic_info": {
                        "scienti": {
                            "fields": {
                                "pages": {
                                    "status": "consistent",
                                    "value": "Autores: A",
                                }
                            }
                        }
                    },
                },
                {
                    "_id": "language",
                    "source": {},
                    "bibliographic_info": {
                        "scienti": {
                            "fields": {
                                "language": {
                                    "status": "consistent",
                                    "value": "educación continua, Número 155",
                                }
                            }
                        }
                    },
                },
            ]
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"]["suspicious_publisher"], 1)
        self.assertEqual(result["checks"]["suspicious_volume"], 1)
        self.assertEqual(result["checks"]["suspicious_edition"], 1)
        self.assertEqual(result["checks"]["suspicious_publication_place"], 1)
        self.assertEqual(result["checks"]["invalid_pages"], 1)
        self.assertEqual(result["checks"]["invalid_language"], 1)
        self.assertEqual(result["critical_anomalies"], 6)


if __name__ == "__main__":
    unittest.main()
