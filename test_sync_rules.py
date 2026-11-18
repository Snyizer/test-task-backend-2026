import unittest

from abstract_customer import SyncGraph, marked_for_deletion
from amocrm import Customer, Lead, normalize_custom_fields, should_fetch_next_page


def _customer(crm_id, login, name="Покупатель", contacts=None, companies=None):
    embedded = {}
    if contacts is not None:
        embedded["contacts"] = contacts
    if companies is not None:
        embedded["companies"] = companies
    return Customer(
        id=crm_id,
        name=name,
        next_price=None,
        next_date=None,
        responsible_user_id=None,
        status_id=1,
        periodicity=None,
        created_by=None,
        updated_by=None,
        created_at=None,
        updated_at=None,
        closest_task_at=None,
        is_deleted=False,
        ltv=None,
        purchases_count=None,
        average_check=None,
        account_id=None,
        custom_fields_values=[{"field_name": "Логин", "field_id": 1, "values": [{"value": login}]}],
        _embedded=embedded or None,
    )


def _lead(crm_id, login, name="Сделка", contacts=None):
    embedded = {"contacts": contacts} if contacts is not None else None
    fields = None
    if login is not None:
        fields = [{"field_name": "Логин", "field_id": 2, "values": [{"value": login}]}]
    return Lead(id=crm_id, name=name, custom_fields_values=fields, _embedded=embedded)


class MatchRulesTest(unittest.TestCase):
    def test_customer_without_login_is_skipped(self):
        graph = SyncGraph()
        graph.consume_customer(_customer(1, None))
        self.assertEqual(graph.accepted_customers, {})
        self.assertEqual(graph.kept_roots(), set())

    def test_customer_uses_single_saved_lead_login(self):
        graph = SyncGraph()
        graph.index_lead(10, "same", "Сделка", 7)
        root = graph.consume_customer(_customer(1, "same"))
        self.assertEqual(root, 7)
        self.assertEqual(graph.accepted_customers[1]["abstract_id"], 7)

    def test_ambiguous_lead_login_creates_new_customer(self):
        graph = SyncGraph()
        graph.index_lead(10, "same", "A", 1)
        graph.index_lead(11, "same", "B", 2)
        root = graph.consume_customer(_customer(5, "same"))
        self.assertLess(root, 0)
        self.assertNotIn(root, (1, 2))

    def test_lead_uses_single_customer_login(self):
        graph = SyncGraph()
        graph.consume_customer(_customer(5, "same"))
        root = graph.consume_lead(_lead(10, "same"))
        self.assertEqual(root, graph.resolved_root(graph.accepted_customers[5]["abstract_id"]))
        self.assertIn(10, graph.accepted_leads)

    def test_ambiguous_customer_login_is_not_used(self):
        graph = SyncGraph()
        graph.index_customer(1, "same", "A", 2)
        graph.index_customer(2, "same", "B", 3)
        root = graph.consume_lead(_lead(10, "same"))
        self.assertLess(root, 0)
        self.assertNotEqual(graph.resolved_root(2), root)
        self.assertNotEqual(graph.resolved_root(3), root)

    def test_login_index_keeps_root_until_last_record_leaves(self):
        for kind in ("customer", "lead"):
            with self.subTest(kind=kind):
                graph = SyncGraph()
                bind = getattr(graph, f"bind_{kind}")
                drop = getattr(graph, f"drop_{kind}")
                logins = getattr(graph, f"{kind}_logins")
                bind(1, "same", 7)
                bind(2, "same", 7)
                bind(2, "same", 7)  # Unchanged records must not count twice.

                bind(1, "new", 7)
                self.assertEqual(graph.roots_with_login(logins, "same"), {7})
                self.assertEqual(graph.roots_with_login(logins, "new"), {7})

                drop(2)
                self.assertNotIn("same", logins)
                bind(1, "new", 8)
                self.assertEqual(graph.roots_with_login(logins, "new"), {8})

    def test_two_roots_merge_into_lead_root(self):
        graph = SyncGraph()
        graph.index_lead(10, "deal", "Сделка", 1)
        graph.index_customer(20, "deal", "Покупатель", 2)
        root = graph.consume_lead(_lead(10, "deal"))
        self.assertEqual(root, 1)
        self.assertEqual(graph.resolved_root(2), 1)
        self.assertEqual(graph.accepted_leads[10]["abstract_id"], 1)

    def test_existing_lead_ignores_ambiguous_customer_login(self):
        graph = SyncGraph()
        graph.index_lead(10, "deal", "Сделка", 1)
        graph.index_customer(20, "deal", "A", 2)
        graph.index_customer(21, "deal", "B", 3)
        root = graph.consume_lead(_lead(10, "deal"))
        self.assertEqual(root, 1)
        self.assertEqual(graph.resolved_root(2), 2)
        self.assertEqual(graph.resolved_root(3), 3)

    def test_delete_marker_is_case_insensitive_and_none_safe(self):
        self.assertTrue(marked_for_deletion("надо Удалить"))
        self.assertFalse(marked_for_deletion(None))
        graph = SyncGraph()
        graph.index_customer(1, "keep", "Живой", 4)
        graph.consume_customer(_customer(1, "keep", name="Удалить покупателя"))
        self.assertNotIn(1, graph.accepted_customers)
        self.assertNotIn(1, graph.customer_by_crm)
        graph.consume_lead(_lead(8, "x", name=None))
        self.assertIn(8, graph.accepted_leads)

    def test_embed_without_name_does_not_fail_and_delete_marker_is_dropped(self):
        graph = SyncGraph()
        graph.consume_customer(_customer(
            1,
            "login",
            contacts=[{"id": 3, "name": None}, {"id": 4, "name": "удалить контакт"}],
            companies=[{"id": 9, "name": "Компания"}],
        ))
        root = graph.resolved_root(graph.accepted_customers[1]["abstract_id"])
        contacts, companies = graph.folded_embeds()
        self.assertEqual(set(contacts[root]), {3})
        self.assertEqual(set(companies[root]), {9})


class AmoCrmEntityTest(unittest.TestCase):
    def test_lead_reads_embedded(self):
        lead = Lead(
            id=1,
            name="L",
            _embedded={
                "contacts": [{"id": 2, "name": "C"}],
                "companies": [{"id": 3, "name": "Co"}],
                "tags": [{"id": 4, "name": "T"}],
            },
        )
        self.assertEqual(lead.contacts[0]["id"], 2)
        self.assertEqual(lead.companies[0]["id"], 3)
        self.assertEqual(lead.tags[0]["id"], 4)
        self.assertIn("contacts", lead.as_dict())

    def test_schema_normalizes_custom_fields_and_none(self):
        self.assertEqual(normalize_custom_fields(None), {})
        loaded = Lead.get_schema(many=True).load([
            {
                "id": 5,
                "name": "N",
                "custom_fields_values": [
                    {"field_name": "Логин", "field_id": 8, "values": [{"value": "ann"}]}
                ],
                "_embedded": {"contacts": [{"id": 1}]},
            }
        ])
        self.assertEqual(loaded[0].get_field("Логин"), "ann")
        self.assertEqual(loaded[0].contacts[0]["id"], 1)
        self.assertIsNone(Lead(custom_fields_values=None).get_field("Логин"))

    def test_full_page_without_next_link_stops(self):
        self.assertFalse(should_fetch_next_page(250, 250, None, None))
        self.assertTrue(should_fetch_next_page(250, 250, None, "https://next"))
        self.assertFalse(should_fetch_next_page(249, 250, None, "https://next"))
        self.assertFalse(should_fetch_next_page(250, 250, 1, "https://next"))


if __name__ == "__main__":
    unittest.main()
