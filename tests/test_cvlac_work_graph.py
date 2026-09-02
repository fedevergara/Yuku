import unittest

from bs4 import BeautifulSoup
import mongomock

from yuku.cvlac_work_graph import (
    CheckpointedNormalizedWorkGraphBuilder,
    CompactUnionFind,
    CvlacWorkGraphBuilder,
    UnionFind,
    author_evidence,
    group_institution_for_year,
    is_identity_doi,
    materialize_work,
    membership_covers_publication_year,
    name_token_key,
    normalize_isbn_identity,
    parse_group_membership_period,
    WORK_GRAPH_PUBLICATIONS,
)
from yuku.cvlac_related_works import (
    extract_directed_title_and_affiliation,
    extract_authors,
    extract_title,
    parse_blockquote,
    split_oriented_people,
)
from yuku.gruplac_related_works import normalize_gruplac_document


def node(
    node_id,
    title,
    year=2024,
    family="article",
    doi="https://doi.org/10.1234/article.2024.001",
):
    return {
        "_id": node_id,
        "title": title,
        "title_key": title,
        "year": year,
        "type_family": family,
        "type_impactu": "Articulo de revista",
        "product_type": "Articulo",
        "profile_id": node_id,
        "profile_author": f"Author {node_id}",
        "authors": [],
        "dois": [doi] if doi else [],
        "identity_dois": [doi] if doi and is_identity_doi(doi) else [],
        "keywords": [],
        "areas": [],
        "advisor_role": "",
        "oriented_people": [],
        "issn": [],
        "isbn": [],
        "affiliation": "",
        "country": "",
    }


class DoiIdentityTest(unittest.TestCase):
    def test_rejects_placeholders_and_journal_level_values(self):
        rejected = [
            "10.0000/noaplica",
            "10.22517/issn.2344-7214",
            "10.4067/s0718",
            "10.17533/udea.rccp",
            "10.1088/1742-6596",
            "10.1101/2023.11.20.567688v1.full.pdf",
            "10.1234/article.2024.001?download=1",
        ]
        for doi in rejected:
            with self.subTest(doi=doi):
                self.assertFalse(is_identity_doi(doi))

    def test_accepts_specific_doi(self):
        self.assertTrue(is_identity_doi("10.1080/02640414.2023.2258666"))

    def test_accepts_only_checksum_valid_isbn_identities(self):
        self.assertEqual(normalize_isbn_identity("958-9160-60-3"), "9589160603")
        self.assertEqual(normalize_isbn_identity("978-958-683-966-2"), "9789586839662")
        self.assertEqual(normalize_isbn_identity("0"), "")
        self.assertEqual(normalize_isbn_identity("978-958-683-966-9"), "")


class CvlacBookMetadataTest(unittest.TestCase):
    def parse(self, section, value):
        blockquote = BeautifulSoup(f"<blockquote>{value}</blockquote>", "lxml").find(
            "blockquote"
        )
        return parse_blockquote("0000000001", section, blockquote)

    def test_extracts_classic_chapter_publication_context(self):
        record = self.parse(
            "Capitulos de libro",
            'ANA PERSONA, "Título del capítulo" Libro contenedor . En: Colombia '
            'ISBN: 978-958-26-0193-5 ed: Ediciones Universidad Central, '
            'v. 2, p.227 - 236 ,2013',
        )
        self.assertEqual(record["title"], "Título del capítulo")
        self.assertEqual(record["book_title"], "Libro contenedor")
        self.assertEqual(record["publisher"], "Ediciones Universidad Central")
        self.assertEqual(record["volume"], "2")
        self.assertEqual(record["pages"], "227 - 236")
        self.assertEqual(record["start_page"], "227")
        self.assertEqual(record["end_page"], "236")

    def test_extracts_new_labelled_book_without_absorbing_areas(self):
        record = self.parse(
            "Libro de Formacion",
            "<i>Nombre del libro:</i> Bioética y animales, "
            "<i>Fecha de presentación:</i> 2021 - Marzo, "
            "<i>Isbn:</i> 978-958-53393-0-9, "
            "<i>Medio de divulgación:</i> Papel, "
            "<i>Lugar de publicación:</i> Colombia, "
            "<i>Editorial:</i> Universidad de Córdoba, "
            "<br/><b>Areas:</b> Ciencias Naturales",
        )
        self.assertEqual(record["title"], "Bioética y animales")
        self.assertEqual(record["publisher"], "Universidad de Córdoba")
        self.assertEqual(record["publication_place"], "Colombia")
        self.assertEqual(record["dissemination_medium"], "Papel")

    def test_does_not_promote_isbn_repeated_in_editorial_slot(self):
        record = self.parse(
            "Libros",
            'ANA PERSONA, "Libro" En: Colombia 2019. '
            'ed:978-958-5533-03-5 ISBN: 978-958-5533-03-5 v. pags.',
        )
        self.assertEqual(record["publisher"], "")
        self.assertEqual(record["volume"], "")
        self.assertEqual(record["pages"], "")


class GrupLacAffiliationRuleTest(unittest.TestCase):
    def test_requires_a_month_precision_membership_covering_the_whole_year(self):
        self.assertEqual(
            parse_group_membership_period("2013/1 - Actual"),
            ((2013, 1), None),
        )
        self.assertTrue(
            membership_covers_publication_year("2013/1 - Actual", 2020)
        )
        self.assertTrue(
            membership_covers_publication_year("2019/1 - 2020/12", 2020)
        )
        self.assertFalse(
            membership_covers_publication_year("2020/8 - Actual", 2020)
        )
        self.assertFalse(
            membership_covers_publication_year("2018 - Actual", 2020)
        )

    def test_requires_exact_or_consistent_bracketing_group_institution(self):
        stable = [
            {"año": 2018, "institucion": "UNIVERSIDAD DEL VALLE"},
            {"año": 2021, "institucion": "UNIVERSIDAD DEL VALLE"},
        ]
        changed = [
            {"año": 2018, "institucion": "UNIVERSIDAD DEL VALLE"},
            {"año": 2021, "institucion": "OTRA UNIVERSIDAD"},
        ]
        self.assertEqual(
            group_institution_for_year(stable, 2020),
            "UNIVERSIDAD DEL VALLE",
        )
        self.assertEqual(group_institution_for_year(stable, 2018), "UNIVERSIDAD DEL VALLE")
        self.assertEqual(group_institution_for_year(changed, 2020), "")
        self.assertEqual(group_institution_for_year(stable, 2010), "")

    def test_materializes_only_closed_product_group_author_triangles(self):
        first = node("g1", "shared article", year=2020)
        first.update(
            {
                "source_kind": "gruplac",
                "source_id": "COL0000013",
                "group_code": "COL0000013",
                "group_name": "Grupo de estudios ecogenéticos",
                "authors": ["Diana Nataly Duque Gamboa", "Yorley Beatriz Lagos Alvarez"],
            }
        )
        authority = {
            "authors": [
                {"id": "0001413351", "full_name": "Diana Nataly Duque Gamboa"},
                {"id": "0001593328", "full_name": "Yorley Beatriz Lagos Alvarez"},
            ]
        }
        group_index = {
            "COL0000013": {
                "institutions": [
                    {"año": 2018, "institucion": "UNIVERSIDAD DEL VALLE"},
                    {"año": 2021, "institucion": "UNIVERSIDAD DEL VALLE"},
                ],
                "members": {"0001413351": ["2013/1 - Actual"]},
            },
            # Yorley's group does not report this product and therefore is not
            # present among the component nodes.
            "COL0089477": {
                "institutions": [
                    {"año": 2018, "institucion": "UNIVERSIDAD NACIONAL DE COLOMBIA"},
                    {"año": 2021, "institucion": "UNIVERSIDAD NACIONAL DE COLOMBIA"},
                ],
                "members": {"0001593328": ["2018/1 - Actual"]},
            },
        }

        work = materialize_work(
            [first], 1, authority=authority, group_affiliation_index=group_index
        )

        self.assertEqual(
            work["groups"],
            [
                {
                    "id": "COL0000013",
                    "name": "Grupo de estudios ecogenéticos",
                    "affiliations": "UNIVERSIDAD DEL VALLE",
                }
            ],
        )
        authors = {author["id"]: author for author in work["authors"]}
        self.assertEqual(
            authors["0001413351"]["affiliations"],
            [
                {
                    "institution": "UNIVERSIDAD DEL VALLE",
                    "source": "gruplac",
                    "group_code": "COL0000013",
                    "membership_period": "2013/1 - Actual",
                    "product_in_group": True,
                }
            ],
        )
        self.assertEqual(authors["0001593328"]["affiliations"], [])

    def test_rejects_author_affiliation_when_group_institution_changes(self):
        item = node("g1", "shared article", year=2020)
        item.update(
            {
                "source_kind": "gruplac",
                "group_code": "COL1",
                "group_name": "Changing group",
                "authors": ["Actual Author"],
            }
        )
        work = materialize_work(
            [item],
            1,
            authority={"authors": [{"id": "0000000001", "full_name": "Actual Author"}]},
            group_affiliation_index={
                "COL1": {
                    "institutions": [
                        {"año": 2018, "institucion": "FIRST UNIVERSITY"},
                        {"año": 2021, "institucion": "SECOND UNIVERSITY"},
                    ],
                    "members": {"0000000001": ["2010/1 - Actual"]},
                }
            },
        )
        self.assertEqual(work["groups"][0]["affiliations"], "")
        self.assertEqual(work["authors"][0]["affiliations"], [])


class DoiGraphGuardTest(unittest.TestCase):
    def setUp(self):
        self.graph = UnionFind()

    def add_pair(self, left, right):
        self.graph.add(left)
        self.graph.add(right)
        return self.graph.can_union_by_doi(left["_id"], right["_id"])

    def test_requires_compatible_title(self):
        allowed, reason = self.add_pair(
            node("a", "machine learning for crop prediction"),
            node("b", "water turbine mechanical performance"),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "doi_title_conflict")

    def test_allows_small_title_variation_and_adjacent_year(self):
        allowed, reason = self.add_pair(
            node("a", "machine learning for crop yield prediction", 2023),
            node("b", "machine learning for crop yield predictions", 2024),
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "compatible")

    def test_rejects_distant_year(self):
        allowed, reason = self.add_pair(
            node("a", "machine learning for crop yield prediction", 2021),
            node("b", "machine learning for crop yield prediction", 2024),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "doi_year_conflict")

    def test_rejects_type_conflict(self):
        allowed, reason = self.add_pair(
            node("a", "machine learning for crop yield prediction"),
            node(
                "b",
                "machine learning for crop yield prediction",
                family="book_chapter",
            ),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "doi_type_conflict")

    def test_guards_entire_component_against_title_drift(self):
        first = node("a", "abcdefghijklmnopqrst")
        middle = node("b", "xxcdefghijklmnopqrst")
        distant = node("c", "xxcdefghijklmnopqrzz")
        for item in (first, middle, distant):
            self.graph.add(item)
        self.assertTrue(self.graph.can_union_by_doi("a", "b")[0])
        self.graph.union("a", "b")
        allowed, reason = self.graph.can_union_by_doi("b", "c")
        self.assertFalse(allowed)
        self.assertEqual(reason, "doi_title_conflict")


class CompactUnionFindTest(unittest.TestCase):
    def test_matches_union_rules_with_fixed_width_parent_storage(self):
        capacity = 100_000
        graph = CompactUnionFind(capacity)
        first = node("a", "machine learning for crop yield prediction", 2023)
        second = node("b", "machine learning for crop yield predictions", 2024)
        first["node_seq"] = 10
        second["node_seq"] = 20
        graph.add(first)
        graph.add(second)

        self.assertEqual(
            graph.can_union_by_doi(10, 20),
            (True, "compatible"),
        )
        root, merged = graph.union(10, 20)
        self.assertTrue(merged)
        self.assertEqual(graph.find(10), graph.find(20))
        self.assertEqual(root, 10)
        self.assertLessEqual(graph.storage_bytes, capacity * 9)
        self.assertEqual(len(graph.summary), 1)


class ReviewCheckpointTest(unittest.TestCase):
    def test_repeated_review_is_an_idempotent_upsert(self):
        db = mongomock.MongoClient().dam
        builder = CvlacWorkGraphBuilder(db=db, batch_size=2)
        builder.run_id = "review_run"
        builder.current_stage = "connect_doi"
        builder.review_collection = db.reviews
        left = node("left", "first incompatible title")
        right = node("right", "second incompatible title")

        builder._review_edge(left, right, "doi_title_conflict")
        builder._review_edge(left, right, "doi_title_conflict")
        builder._flush_reviews()
        builder._review_edge(left, right, "doi_title_conflict")
        builder._flush_reviews()

        self.assertEqual(db.reviews.count_documents({}), 1)
        review = db.reviews.find_one({})
        self.assertEqual(review["reason"], "doi_title_conflict")
        self.assertEqual(builder.review_buffer, [])

    def test_deferred_publish_keeps_the_public_pointer_unchanged(self):
        db = mongomock.MongoClient().dam
        previous = {"_id": "current", "current_collection": "safe_graph"}
        db[WORK_GRAPH_PUBLICATIONS].insert_one(previous)
        builder = CheckpointedNormalizedWorkGraphBuilder(
            db=db,
            collection="candidate_graph",
            source_collection="normalized_cvlac",
            group_source_collection="normalized_gruplac",
            gate={},
            publish_pointer=False,
        )
        builder.run_id = "candidate_run"
        builder.output_name = "candidate_output"
        builder.runs.insert_one(
            {"_id": builder.run_id, "audit": {"works": 1}}
        )
        db[builder.output_name].insert_one({"_id": "work"})

        result = builder._publish(db[builder.output_name])

        self.assertFalse(result["pointer_published"])
        self.assertIn("candidate_graph", db.list_collection_names())
        self.assertEqual(
            db[WORK_GRAPH_PUBLICATIONS].find_one({"_id": "current"}),
            previous,
        )


class MaterializationTest(unittest.TestCase):
    def test_detects_identity_collisions_across_write_batches_without_global_set(self):
        db = mongomock.MongoClient().dam
        first = node("source-a", "the same reported article")
        second = node("source-b", "the same reported article")
        first["cluster_id"] = "cluster-a"
        second["cluster_id"] = "cluster-b"
        db.nodes.insert_many([first, second])
        builder = CvlacWorkGraphBuilder(db=db, batch_size=1)
        builder.run_id = "collision_test"
        builder.current_stage = "materialize"
        builder.review_collection = db.reviews

        builder._materialize(db.nodes, db.output, 1)

        works = list(db.output.find({}))
        self.assertEqual(len(works), 2)
        collisions = [work for work in works if work.get("identity_collision_of")]
        self.assertEqual(len(collisions), 1)
        self.assertEqual(db.reviews.count_documents({}), 1)

    def test_profile_owner_is_not_an_implicit_author(self):
        item = node("reporter", "a reported article")
        item["profile_author"] = "Profile Reporter"
        item["authors"] = ["Actual Author"]
        work = materialize_work([item], 1)
        self.assertEqual(
            work["authors"],
            [{"id": "", "full_name": "Actual Author", "affiliations": []}],
        )

    def test_conflicting_book_claims_are_not_published_as_canonical_authors(self):
        names_by_profile = {
            "0000006530": (
                "Gladys Jaimes Carvajal",
                [
                    "BLANCA LILIA BOJACA BOJACA",
                    "ROSA DELIA MORALES VILLOTA",
                    "RAQUEL PINILLA VASQUEZ",
                    "MARIA ELVIRA RODRIGUEZ LUNA",
                    "GLADYS JAIMES CARVAJAL",
                ],
            ),
            "0000006556": ("Raquel Pinilla Vásquez", ["RAQUEL PINILLA VASQUEZ"]),
            "0000006564": (
                "Rosa Delia Morales Villota",
                [
                    "BLANCA LILIA BOJACA BOJACA",
                    "RAQUEL PINILLA VASQUEZ",
                    "MARIA ELVIRA RODRIGUEZ LUNA",
                    "GLADYS JAIMES DE CASADIEGO",
                    "ROSA DELIA MORALES VILLOTA",
                ],
            ),
            "0000006572": (
                "Blanca Lilia Bojacá Bojacá",
                [
                    "ROSA MORALES",
                    "RAQUEL PINILLA",
                    "MARIA RODRIGUEZ",
                    "GLADYS JAIMES",
                    "BLANCA LILIA BOJACA BOJACA",
                    "BLANCA BOJACA",
                ],
            ),
        }
        items = []
        for profile_id, (owner, authors) in names_by_profile.items():
            item = node(
                profile_id,
                "pedagogia de proyectos opcion de cambio social",
                year=1999,
                family="book",
                doi="",
            )
            item["profile_author"] = owner
            item["type_impactu"] = "Libro"
            item["authors"] = authors
            item["isbn"] = ["958-9160-60-3"]
            items.append(item)
        profile_index = {
            name_token_key(name): {profile_id}
            for profile_id, (name, _) in names_by_profile.items()
        }
        profile_index[name_token_key("Maria Elvira Rodriguez Luna")] = {"0000216887"}
        work = materialize_work(items, 1, profile_index)
        self.assertEqual(work["authors"], [])
        self.assertEqual(work["author_count"], 0)
        self.assertEqual(work["authorship_status"], "conflict")
        self.assertTrue(
            any(
                author.get("id") == "0000216887"
                for author in work["authorship_candidates"]
            )
        )

    def test_gruplac_group_is_a_relation_not_an_author(self):
        item = node("group-node", "a group work", doi="")
        item.update(
            {
                "profile_id": "",
                "profile_author": "",
                "source_kind": "gruplac",
                "source_id": "COL0002081",
                "group_code": "COL0002081",
                "group_name": "Centro de Estudios de Ergonomía",
                "authors": ["Actual Author"],
            }
        )
        work = materialize_work([item], 1)
        self.assertEqual(
            work["authors"],
            [{"id": "", "full_name": "Actual Author", "affiliations": []}],
        )
        self.assertEqual(
            work["groups"],
            [
                {
                    "id": "COL0002081",
                    "name": "Centro de Estudios de Ergonomía",
                    "affiliations": "",
                }
            ],
        )

    def test_verified_authority_replaces_conflicting_book_claims(self):
        first = node("a", "a disputed book", family="book", doi="")
        second = node("b", "a disputed book", family="book", doi="")
        first["authors"] = ["Reported Person A"]
        second["authors"] = ["Official Author"]
        authority = {
            "authority_id": "catalog-record",
            "authors": [
                {"id": "0000000001", "full_name": "Official Author", "type": "author"}
            ],
            "source": "institutional_catalog",
            "evidence": "https://example.edu/catalog/record",
        }
        work = materialize_work([first, second], 1, authority=authority)
        self.assertEqual(work["authorship_status"], "verified")
        self.assertEqual(
            work["authors"],
            [
                {
                    "id": "0000000001",
                    "full_name": "Official Author",
                    "type": "author",
                    "affiliations": [],
                }
            ],
        )
        self.assertEqual(work["authorship_authority"]["authority_id"], "catalog-record")

    def test_book_id_is_stable_when_gruplac_evidence_is_added(self):
        first = node("cvlac-node", "a stable book", family="book", doi="")
        first["isbn"] = ["958-9160-60-3"]
        first["identity_isbns"] = ["9589160603"]
        second = node("gruplac-node", "a stable book", family="book", doi="")
        second["isbn"] = ["9589160603"]
        second["identity_isbns"] = ["9589160603"]
        second.update(
            {
                "source_kind": "gruplac",
                "source_id": "COL0002081",
                "profile_id": "",
                "profile_author": "",
                "group_code": "COL0002081",
            }
        )
        self.assertEqual(
            materialize_work([first], 1)["_id"],
            materialize_work([first, second], 1)["_id"],
        )

    def test_multiple_book_isbn_anchors_merge_duplicates_per_edition(self):
        builder = CvlacWorkGraphBuilder(db=None)
        builder.run_id = "test"
        builder.review_buffer = []

        def book(node_id, isbn, authors=None):
            item = node(
                node_id,
                "diseno gestion y evaluacion de un programa de formacion",
                year=2017,
                family="book",
                doi="",
            )
            item.update(
                {
                    "profile_id": "0000353493",
                    "profile_author": "Elizabeth Hurtado Martinez",
                    "authors": authors or [],
                    "isbn": [isbn],
                    "identity_isbns": [isbn],
                }
            )
            return item

        edition_857 = [
            book("a", "9789588770857"),
            book("c", "9789588770857", ["Elizabeth Hurtado Martinez"]),
            book("e", "9789588770857"),
        ]
        edition_840 = [
            book("b", "9789588770840", ["Elizabeth Hurtado Martinez"]),
            book("d", "9789588770840"),
        ]
        builder._connect_title_group_members(
            [edition_857[0], edition_840[0], edition_857[1], edition_840[1], edition_857[2]]
        )

        self.assertEqual(
            {builder.graph.find(item["_id"]) for item in edition_857},
            {builder.graph.find(edition_857[0]["_id"])},
        )
        self.assertEqual(
            {builder.graph.find(item["_id"]) for item in edition_840},
            {builder.graph.find(edition_840[0]["_id"])},
        )
        self.assertNotEqual(
            builder.graph.find(edition_857[0]["_id"]),
            builder.graph.find(edition_840[0]["_id"]),
        )

    def test_same_rejected_doi_does_not_collapse_document_ids(self):
        doi = "https://doi.org/10.0000/noaplica"
        first = materialize_work([node("a", "first unrelated work", doi=doi)], 1)
        second = materialize_work([node("b", "second unrelated work", doi=doi)], 1)
        self.assertNotEqual(first["_id"], second["_id"])
        self.assertEqual(first["doi"], doi)
        self.assertEqual(second["doi"], doi)

    def test_keeps_eventually_enrichable_work_fields(self):
        work = materialize_work(
            [node("a", "a work title", year=None, doi="")],
            1,
        )
        absent = {
            "abstracts",
            "apc",
            "bibliographic_info",
            "citations",
            "citations_by_year",
            "citations_count",
            "citations_count_openalex",
            "date_published",
            "external_urls",
            "open_access",
            "primary_topic",
            "ranking",
            "references",
            "references_count",
            "rights",
            "source",
            "topics",
        }
        self.assertTrue(absent.isdisjoint(work))
        self.assertEqual(work["groups"], [])
        self.assertEqual(work["doi"], "")
        self.assertEqual(work["keywords"], [])
        self.assertIsNone(work["year_published"])
        self.assertEqual(work["subjects"], [])
        self.assertEqual(work["titles"][0]["lang"], "")

    def test_materializes_book_source_and_exact_provenance(self):
        item = node("book", "chapter title", family="book_chapter", doi="")
        item.update(
            {
                "source_kind": "cvlac",
                "source_id": "0000000001",
                "source_record_index": 7,
                "book_title": "Libro contenedor",
                "publisher": "Editorial Ejemplo",
                "pages": "10 - 20",
                "start_page": "10",
                "end_page": "20",
                "isbn": ["978-958-683-966-2"],
                "identity_isbns": ["9789586839662"],
            }
        )
        work = materialize_work([item], 1)
        self.assertEqual(work["source"]["name"], "Libro contenedor")
        self.assertEqual(
            work["source"]["publisher"],
            {"name": "Editorial Ejemplo", "country_code": ""},
        )
        self.assertEqual(work["bibliographic_info"]["start_page"], "10")
        publisher = work["bibliographic_info"]["scienti"]["fields"]["publisher"]
        self.assertEqual(publisher["status"], "consistent")
        self.assertEqual(
            publisher["candidates"][0]["occurrences"][0]["record_index"], 7
        )

    def test_publisher_conflict_is_preserved_but_not_consolidated(self):
        first = node("a", "same chapter", family="book_chapter", doi="")
        second = node("b", "same chapter", family="book_chapter", doi="")
        for item, publisher in ((first, "Editorial Uno"), (second, "Editorial Dos")):
            item.update(
                {
                    "book_title": "Libro compartido",
                    "publisher": publisher,
                    "source_kind": "cvlac",
                    "source_id": item["_id"],
                }
            )
        work = materialize_work([first, second], 1)
        self.assertEqual(work["source"]["publisher"], {})
        self.assertNotIn("publisher", work["bibliographic_info"])
        evidence = work["bibliographic_info"]["scienti"]["fields"]["publisher"]
        self.assertEqual(evidence["status"], "conflict")
        self.assertEqual(len(evidence["candidates"]), 2)

    def test_preserves_author_and_subject_work_shape(self):
        item = node("a", "a work title")
        item["authors"] = ["Additional Author"]
        item["areas"] = ["Ciencias Sociales"]
        work = materialize_work([item], 1)
        unresolved = next(
            author for author in work["authors"]
            if author["full_name"] == "Additional Author"
        )
        self.assertEqual(unresolved["id"], "")
        self.assertEqual(
            work["subjects"],
            [
                {
                    "source": "minciencias",
                    "subjects": [
                        {"id": "", "name": "Ciencias Sociales", "level": None}
                    ],
                }
            ],
        )

    def test_doi_is_also_an_external_id(self):
        doi = "https://doi.org/10.1234/article.2024.001"
        work = materialize_work([node("a", "a work title", doi=doi)], 1)
        self.assertEqual(work["doi"], doi)
        self.assertIn(
            {
                "provenance": "minciencias",
                "source": "doi",
                "id": doi,
            },
            work["external_ids"],
        )

    def test_updated_precedes_titles(self):
        work = materialize_work([node("a", "a work title")], 1)
        keys = list(work)
        self.assertLess(keys.index("updated"), keys.index("titles"))

    def test_materializes_thesis_advisor_and_students(self):
        item = node("advisor-id", "a thesis", family="graduate_thesis", doi="")
        item["profile_author"] = "Advisor Name"
        item["type_impactu"] = "Tesis de posgrado"
        item["advisor_role"] = "advisor"
        item["authors"] = ["Additional Participant"]
        item["oriented_people"] = ["Student One", "Student Two"]
        work = materialize_work([item], 1)
        by_name = {author["full_name"]: author for author in work["authors"]}
        self.assertEqual(
            by_name["Advisor Name"],
            {
                "id": "advisor-id",
                "full_name": "Advisor Name",
                "type": "advisor",
                "affiliations": [],
            },
        )
        self.assertEqual(by_name["Student One"]["type"], "author")
        self.assertEqual(by_name["Student One"]["id"], "")
        self.assertEqual(by_name["Student Two"]["type"], "author")
        self.assertEqual(by_name["Additional Participant"]["type"], "author")
        self.assertEqual(work["author_count"], 4)

    def test_normalizes_author_display_names(self):
        item = node("advisor-id", "a thesis", family="graduate_thesis", doi="")
        item["profile_author"] = "ADVISOR NAME"
        item["oriented_people"] = ["andres chavez salazar"]
        work = materialize_work([item], 1)
        self.assertEqual(
            [author["full_name"] for author in work["authors"]],
            ["Advisor Name", "Andres Chavez Salazar"],
        )

    def test_ignores_title_leaked_as_oriented_person(self):
        title = "Evaluation of a clinical intervention in neonatal patients"
        item = node("advisor-id", title, family="graduate_thesis", doi="")
        item["profile_author"] = "Advisor Name"
        item["oriented_people"] = [title]
        work = materialize_work([item], 1)
        self.assertEqual(
            work["authors"],
            [
                {
                    "id": "advisor-id",
                    "full_name": "Advisor Name",
                    "type": "advisor",
                    "affiliations": [],
                }
            ],
        )

    def test_keeps_student_when_work_title_is_their_name(self):
        item = node(
            "advisor-id",
            "Juan David Vargas Acevedo",
            family="graduate_thesis",
            doi="",
        )
        item["profile_author"] = "Advisor Name"
        item["oriented_people"] = ["Juan David Vargas Acevedo"]
        work = materialize_work([item], 1)
        self.assertEqual(
            {author["full_name"] for author in work["authors"]},
            {"Advisor Name", "Juan David Vargas Acevedo"},
        )

    def test_resolves_student_against_unique_cvlac_profile_name(self):
        item = node("advisor-id", "a thesis", family="graduate_thesis", doi="")
        item["profile_author"] = "Advisor Name"
        item["oriented_people"] = ["Identified Student"]
        work = materialize_work(
            [item],
            1,
            {name_token_key("Identified Student"): {"student-id"}},
        )
        student = next(
            author for author in work["authors"]
            if author["full_name"] == "Identified Student"
        )
        self.assertEqual(student["id"], "student-id")
        self.assertEqual(student["type"], "author")

    def test_deduplicates_student_reported_by_multiple_advisors(self):
        first = node("advisor-a", "a thesis", family="graduate_thesis", doi="")
        second = node("advisor-b", "a thesis", family="graduate_thesis", doi="")
        first["oriented_people"] = ["Shared Student"]
        second["oriented_people"] = ["SHARED STUDENT"]
        work = materialize_work([first, second], 1)
        students = [
            author for author in work["authors"]
            if author.get("type") == "author"
        ]
        self.assertEqual(len(students), 1)
        self.assertEqual(work["author_count"], 3)

    def test_common_student_is_author_evidence_for_generic_thesis(self):
        first = node("advisor-a", "generic title", family="graduate_thesis")
        second = node("advisor-b", "generic title", family="graduate_thesis")
        first["oriented_people"] = ["Shared Student"]
        second["oriented_people"] = ["shared student"]
        self.assertTrue(author_evidence(first, second))


class RelatedWorkParsingTest(unittest.TestCase):
    def test_directed_work_uses_html_lines_to_preserve_institution_in_title(self):
        soup = BeautifulSoup(
            """<blockquote>
            Diseño para estudiantes de la Institución Educativa Maestro Arenas
            INSTITUCIÓN UNIVERSITARIA POLITÉCNICO GRANCOLOMBIANO
            Estado: Tesis concluida
            <i>Dirigió como:</i> Tutor principal
            </blockquote>""",
            "html.parser",
        )
        title, affiliation = extract_directed_title_and_affiliation(soup.blockquote)
        self.assertEqual(
            title,
            "Diseño para estudiantes de la Institución Educativa Maestro Arenas",
        )
        self.assertEqual(
            affiliation,
            "INSTITUCIÓN UNIVERSITARIA POLITÉCNICO GRANCOLOMBIANO",
        )

    def test_directed_work_stops_before_institucion_universitaria(self):
        text = (
            "CARLOS ANDRES MADRIGAL GONZALEZ, Detección Inteligente de Objetos "
            "Abandonados en Lugares Concurridos INSTITUCIÓN UNIVERSITARIA ITM "
            "Estado: Tesis concluida, 2013. Dirigió como: Tutor principal"
        )
        self.assertEqual(
            extract_title(
                text,
                {},
                "Trabajos dirigidos/tutorias",
                "Carlos Andrés Madrigal González",
            ),
            "Detección Inteligente de Objetos Abandonados en Lugares Concurridos",
        )

    def test_removes_accented_inline_product_type_from_author_name(self):
        text = 'Tipo: Otro capítulo de libro publicado LUZ MARINA MELGAREJO MUNOZ "A title"'
        self.assertEqual(
            extract_authors(
                text,
                "A title",
                {},
                "Otro capítulo de libro publicado",
                "Capitulos de libro",
            ),
            ["LUZ MARINA MELGAREJO MUNOZ"],
        )

    def test_directed_work_does_not_use_quoted_institution_as_title(self):
        text = (
            "MIGUEL GONZALEZ, Retos y Desafíos en la Frontera Colombo-panameña: "
            'Un Análisis en Prospectiva Escuela Militar de Cadetes "General José '
            'María Córdova" Estado: Tesis concluida, 2017.'
        )
        self.assertEqual(
            extract_title(text, {}, "Trabajos dirigidos/tutorias"),
            "Retos y Desafíos en la Frontera Colombo-panameña: Un Análisis en Prospectiva",
        )

    def test_directed_work_without_owner_prefix_starts_with_title(self):
        text = (
            "Extension Rural y Sostenibilidad: Una Mirada a la Caficultura "
            "Colombiana UNIVERSIDAD NACIONAL DE COLOMBIA Estado: Tesis concluida "
            "Maestría, 2022. Dirigió como: Tutor principal"
        )
        title = extract_title(
            text,
            {},
            "Trabajos dirigidos/tutorias",
            "Robinson Osorio Hernandez",
        )
        self.assertEqual(
            title,
            "Extension Rural y Sostenibilidad: Una Mirada a la Caficultura Colombiana",
        )
        self.assertEqual(
            extract_authors(
                text,
                title,
                {},
                "Trabajos dirigidos/Tutorías - Trabajo de grado de maestría",
                "Trabajos dirigidos/tutorias",
                "Robinson Osorio Hernandez",
            ),
            [],
        )

    def test_directed_work_title_stops_at_advisor_label(self):
        text = (
            "GUILLERMO GARZON GARCIA, Diseño de un sistema de calidad "
            "Dirigió como: Tutor principal, Persona(s) orientada(s): Diana Montes"
        )
        self.assertEqual(
            extract_title(
                text,
                {},
                "Trabajos dirigidos/tutorias",
                "Guillermo Garzon Garcia",
            ),
            "Diseño de un sistema de calidad",
        )

    def test_splits_students_and_removes_advisor_suffix(self):
        self.assertEqual(
            split_oriented_people(
                "ana maria / juan pablo y luz elena asesor(es): director name"
            ),
            ["ana maria", "juan pablo", "luz elena"],
        )
        self.assertEqual(split_oriented_people("asesor(es): director name"), [])
        self.assertEqual(
            split_oriented_people("silvia johana asesor: cesar pabon"),
            ["silvia johana"],
        )
        self.assertEqual(
            split_oriented_people(
                "daniela arcila laverde corporacion unificada nacional "
                "daniela.arcila@cun.edu.co"
            ),
            ["daniela arcila laverde"],
        )

    def test_parses_gruplac_book_without_promoting_group_members(self):
        html = """
        <body>
          <span class="celdaEncabezado">Centro de Estudios de Ergonomía</span>
          <table><tr><td class="celdaEncabezado" colspan="2">Datos básicos</td></tr>
            <tr><td class="celdasTitulo">Líder</td><td class="celdas2">Group Leader</td></tr>
          </table>
          <table><tr><td class="celdaEncabezado" colspan="2">Libros publicados</td></tr>
            <tr><td class="celdas_1"><img src="/gruplac/images/chulo_1.jpg"/></td>
              <td class="celdas1">1.- <strong>Libro resultado de investigación</strong> :
                A Reliable Book<br/>Colombia, 2020, ISBN: 9786200430748,
                Ed. Example Press<br/>Autores: ACTUAL AUTHOR</td></tr>
          </table>
        </body>
        """
        document = normalize_gruplac_document("COL0002081", html, nro="8014")
        self.assertEqual(document["group_name"], "Centro de Estudios de Ergonomía")
        self.assertEqual(document["production_count"], 1)
        work = document["production"][0]
        self.assertEqual(work["title"], "A Reliable Book")
        self.assertEqual(work["authors"], ["ACTUAL AUTHOR"])
        self.assertTrue(work["validated"])


if __name__ == "__main__":
    unittest.main()
