import logging
from collections import Counter, defaultdict
from itertools import islice
from typing import Dict, Iterable, List, Optional, TypeVar

from sqlalchemy import column, delete, exists, func, select, update, values
from sqlalchemy.dialects.postgresql import insert as pg_insert

from amocrm import Customer, Lead
from models import AmoCustomer, AmoLead, AbstractCustomer

logger = logging.getLogger(__name__)

BATCH_SIZE = 2000
T = TypeVar("T")


def marked_for_deletion(name) -> bool:
    return "удалить" in (name or "").lower()


def _field_value(entity, field_name):
    value = entity.get_field(field_name)
    return value


def customer_document(customer: Customer) -> dict:
    fields = customer.custom_fields_values or {}

    def raw(key):
        item = fields.get(key) or {}
        if isinstance(item, dict):
            return item.get('value')
        return None

    return {
        'general': {
            'id': customer.id,
            'login': raw('Логин'),
            'billing_type': raw('Вид оплаты'),
            'is_robot': bool(fields.get('Робот')),
            'is_nds': bool(fields.get('Оплата с НДС')),
            'system': raw('Система'),
            'host': raw('Хост'),
            'name': customer.name,
        },
        'api_value': customer.as_dict(),
    }


def lead_document(lead: Lead) -> dict:
    return {
        'general': {
            'id': lead.id,
            'login': _field_value(lead, 'Логин'),
            'name': lead.name,
        },
        'api_value': lead.as_dict(),
    }


def embed_document(raw, kind: str) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if marked_for_deletion(name):
        return None
    general = {"id": raw.get("id"), "name": name}
    if kind == "contact":
        general["phone"] = raw.get("phone")
    elif kind == "company":
        general["inn"] = raw.get("inn")
    return {'general': general, "api_value": raw}


def _batches(rows: Iterable[T], size: int = BATCH_SIZE) -> Iterable[List[T]]:
    iterator = iter(rows)
    while batch := list(islice(iterator, size)):
        yield batch


class SyncGraph:
    """Связи выгрузки в памяти.

    Покупатели сопоставляются со уже сохранёнными сделками.
    Сделки сопоставляются с покупателями и при двух разных корнях
    сливаются в корень, найденный по id сделки.
    """

    def __init__(self):
        self._parent: Dict[int, int] = {}
        self._temp = 0
        self.customer_by_crm: Dict[int, int] = {}
        self.lead_by_crm: Dict[int, int] = {}
        self.customer_login_by_crm = {}
        self.lead_login_by_crm = {}
        # Each login maps to counts per root, so removing one CRM record
        # does not remove the root while other records still reference it.
        self.customer_logins = defaultdict(Counter)
        self.lead_logins = defaultdict(Counter)
        self.accepted_customers: Dict[int, dict] = {}
        self.accepted_leads: Dict[int, dict] = {}
        self.contacts = defaultdict(dict)
        self.companies = defaultdict(dict)

    def find(self, node: int) -> int:
        parent = self._parent
        parent.setdefault(node, node)
        root = node
        while parent[root] != root:
            root = parent[root]
        while node != root:
            nxt = parent[node]
            parent[node] = root
            node = nxt
        return root

    def new_root(self) -> int:
        self._temp -= 1
        self._parent[self._temp] = self._temp
        return self._temp

    def merge_loser_into_winner(self, loser: int, winner: int) -> int:
        loser_root = self.find(loser)
        winner_root = self.find(winner)
        if loser_root != winner_root:
            self._parent[loser_root] = winner_root
        return winner_root

    def roots_with_login(self, login_index, login: str) -> set:
        if not login:
            return set()
        return {self.find(node) for node in login_index.get(login, ())}

    def _rebind(self, by_crm, login_index, crm_id, login, abstract_id):
        old = by_crm.get(crm_id)
        if old == (login, abstract_id):
            return
        if old is not None and old[0]:
            old_login, old_abs = old
            roots = login_index[old_login]
            roots[old_abs] -= 1
            if not roots[old_abs]:
                del roots[old_abs]
            if not roots:
                del login_index[old_login]
        if abstract_id is None:
            by_crm.pop(crm_id, None)
            return
        by_crm[crm_id] = (login, abstract_id)
        if login:
            login_index[login][abstract_id] += 1

    def bind_customer(self, crm_id, login, abstract_id):
        self._rebind(self.customer_login_by_crm, self.customer_logins, crm_id, login, abstract_id)
        self.customer_by_crm[crm_id] = abstract_id

    def drop_customer(self, crm_id):
        if crm_id is None:
            return
        self._rebind(self.customer_login_by_crm, self.customer_logins, crm_id, None, None)
        self.customer_by_crm.pop(crm_id, None)
        self.accepted_customers.pop(crm_id, None)

    def bind_lead(self, crm_id, login, abstract_id):
        self._rebind(self.lead_login_by_crm, self.lead_logins, crm_id, login, abstract_id)
        self.lead_by_crm[crm_id] = abstract_id

    def drop_lead(self, crm_id):
        if crm_id is None:
            return
        self._rebind(self.lead_login_by_crm, self.lead_logins, crm_id, None, None)
        self.lead_by_crm.pop(crm_id, None)
        self.accepted_leads.pop(crm_id, None)

    def index_customer(self, crm_id, login, name, abstract_id):
        if marked_for_deletion(name):
            return
        self.find(abstract_id)
        self.bind_customer(crm_id, login, abstract_id)

    def index_lead(self, crm_id, login, name, abstract_id):
        if marked_for_deletion(name):
            return
        self.find(abstract_id)
        self.bind_lead(crm_id, login, abstract_id)

    def _add_embeds(self, abstract_id, entity):
        for raw in getattr(entity, "contacts", None) or []:
            item = embed_document(raw, "contact")
            if not item:
                continue
            key = item['general'].get('id')
            if key is None:
                key = ("contact", id(raw))
            self.contacts[abstract_id][key] = item
        for raw in getattr(entity, "companies", None) or []:
            item = embed_document(raw, "company")
            if not item:
                continue
            key = item['general'].get('id')
            if key is None:
                key = ("company", id(raw))
            self.companies[abstract_id][key] = item

    def consume_customer(self, customer: Customer):
        login = customer.get_field("Логин")
        if not login or marked_for_deletion(customer.name):
            self.drop_customer(customer.id)
            return None

        if customer.id in self.customer_by_crm:
            abstract_id = self.find(self.customer_by_crm[customer.id])
        else:
            # Больше одной сохранённой сделки с этим логином — совпадение не берём.
            roots = self.roots_with_login(self.lead_logins, login)
            if len(roots) == 1:
                abstract_id = next(iter(roots))
            else:
                abstract_id = self.new_root()

        self.bind_customer(customer.id, login, abstract_id)
        document = customer_document(customer)
        self.accepted_customers[customer.id] = {
            "abstract_id": abstract_id,
            "login": login,
            "name": customer.name,
            "general": document["general"],
            "api_value": document["api_value"],
        }
        self._add_embeds(abstract_id, customer)
        return abstract_id

    def consume_lead(self, lead: Lead):
        if marked_for_deletion(lead.name):
            self.drop_lead(lead.id)
            return None

        login = lead.get_field("Логин")
        by_lead = self.lead_by_crm.get(lead.id)
        if by_lead is not None:
            by_lead = self.find(by_lead)

        by_customer = None
        if login:
            roots = self.roots_with_login(self.customer_logins, login)
            if len(roots) == 1:
                by_customer = next(iter(roots))

        if by_lead is not None and by_customer is not None and by_lead != by_customer:
            self.merge_loser_into_winner(by_customer, by_lead)
            abstract_id = self.find(by_lead)
        elif by_lead is not None:
            abstract_id = by_lead
        elif by_customer is not None:
            abstract_id = by_customer
        else:
            abstract_id = self.new_root()

        self.bind_lead(lead.id, login, abstract_id)
        document = lead_document(lead)
        self.accepted_leads[lead.id] = {
            "abstract_id": abstract_id,
            "login": login,
            "name": lead.name,
            "general": document["general"],
            "api_value": document["api_value"],
        }
        self._add_embeds(abstract_id, lead)
        return abstract_id

    def resolved_root(self, abstract_id: int) -> int:
        return self.find(abstract_id)

    def folded_embeds(self):
        contacts = defaultdict(dict)
        companies = defaultdict(dict)
        for abstract_id, items in self.contacts.items():
            contacts[self.find(abstract_id)].update(items)
        for abstract_id, items in self.companies.items():
            companies[self.find(abstract_id)].update(items)
        return contacts, companies

    def kept_roots(self) -> set:
        roots = set()
        for payload in self.accepted_customers.values():
            roots.add(self.find(payload["abstract_id"]))
        for payload in self.accepted_leads.values():
            roots.add(self.find(payload["abstract_id"]))
        return roots


def load_existing(session, graph: SyncGraph):
    customer_rows = session.execute(
        select(AmoCustomer.crm_id, AmoCustomer.login, AmoCustomer.name, AmoCustomer.abstract_customer_id)
    )
    for crm_id, login, name, abstract_id in customer_rows:
        graph.index_customer(crm_id, login, name, abstract_id)

    lead_rows = session.execute(
        select(AmoLead.crm_id, AmoLead.login, AmoLead.name, AmoLead.abstract_customer_id)
    )
    for crm_id, login, name, abstract_id in lead_rows:
        graph.index_lead(crm_id, login, name, abstract_id)


def _embed_list(items: dict) -> list:
    return list(items.values())


def _merge_embeds(existing, fresh: dict) -> list:
    merged = {}
    for item in existing or []:
        if not isinstance(item, dict):
            continue
        general = item.get("general") or {}
        if marked_for_deletion(general.get("name")):
            continue
        key = general.get("id")
        if key is None:
            key = ("existing", id(item))
        merged[key] = item
    merged.update(fresh)
    return list(merged.values())


def _insert_roots(session, rows: Iterable[dict]) -> List[int]:
    # Pass rows at execution time so SQLAlchemy can batch the insert and
    # correlate returned IDs with the input order.
    stmt = pg_insert(AbstractCustomer.__table__).returning(
        AbstractCustomer.id,
        sort_by_parameter_order=True,
    )
    inserted = []
    for batch in _batches(rows):
        inserted.extend(session.scalars(stmt, batch).all())
    return inserted


def _upsert(session, model, rows: Iterable[dict]):
    for batch in _batches(rows):
        stmt = pg_insert(model).values(batch)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[model.crm_id],
            set_={
                "abstract_customer_id": excluded.abstract_customer_id,
                "login": excluded.login,
                "name": excluded.name,
                "general": excluded.general,
                "api_value": excluded.api_value,
                "last_seen_run": excluded.last_seen_run,
            },
        )
        session.execute(stmt)


def _update_roots(session, rows: Iterable[tuple], run_id: int):
    root = AbstractCustomer.__table__
    for batch in _batches(rows):
        incoming = values(
            column('id', root.c.id.type),
            column('amo_contacts', root.c.amo_contacts.type),
            column('amo_companies', root.c.amo_companies.type),
            name='incoming',
        ).data(batch)
        session.execute(
            update(root)
            .where(root.c.id == incoming.c.id)
            .values(
                amo_contacts=incoming.c.amo_contacts,
                amo_companies=incoming.c.amo_companies,
                last_seen_run=run_id,
            )
        )


def _entity_rows(graph, accepted, id_map, run_id):
    for crm_id, payload in accepted.items():
        yield {
            "abstract_customer_id": id_map[graph.find(payload["abstract_id"])],
            "crm_id": crm_id,
            "login": payload["login"],
            "name": payload["name"],
            "general": payload["general"],
            "api_value": payload["api_value"],
            "last_seen_run": run_id,
        }


def persist_sync(session, graph: SyncGraph, is_full: bool):
    """Пишет граф одной транзакцией. Удаление только после полного прохода."""
    current = session.scalar(select(func.max(AbstractCustomer.last_seen_run)))
    run_id = (current or 0) + 1
    contacts, companies = graph.folded_embeds()
    roots = graph.kept_roots()
    id_map = {root: root for root in roots if root > 0}

    new_roots = [root for root in roots if root < 0]
    if new_roots:
        payloads = (
            {
                "amo_contacts": _embed_list(contacts.get(root, {})),
                "amo_companies": _embed_list(companies.get(root, {})),
                "last_seen_run": run_id,
            }
            for root in new_roots
        )
        new_ids = _insert_roots(session, payloads)
        if len(new_ids) != len(new_roots):
            raise RuntimeError("Postgres вернул не все id новых покупателей")
        id_map.update(zip(new_roots, new_ids))

    existing_roots = [root for root in roots if root > 0]
    if existing_roots and not is_full:
        stored = session.execute(
            select(AbstractCustomer.id, AbstractCustomer.amo_contacts, AbstractCustomer.amo_companies)
            .where(AbstractCustomer.id.in_(existing_roots))
        )
        stored_map = {row.id: row for row in stored}
    else:
        stored_map = {}

    def root_updates():
        for root in existing_roots:
            fresh_contacts = contacts.get(root, {})
            fresh_companies = companies.get(root, {})
            if is_full:
                yield root, _embed_list(fresh_contacts), _embed_list(fresh_companies)
            else:
                row = stored_map.get(root)
                yield (
                    root,
                    _merge_embeds(getattr(row, "amo_contacts", None), fresh_contacts),
                    _merge_embeds(getattr(row, "amo_companies", None), fresh_companies),
                )

    _update_roots(session, root_updates(), run_id)
    _upsert(session, AmoCustomer, _entity_rows(graph, graph.accepted_customers, id_map, run_id))
    _upsert(session, AmoLead, _entity_rows(graph, graph.accepted_leads, id_map, run_id))

    if not is_full:
        return

    session.execute(delete(AmoCustomer).where(AmoCustomer.last_seen_run.is_distinct_from(run_id)))
    session.execute(delete(AmoLead).where(AmoLead.last_seen_run.is_distinct_from(run_id)))
    session.execute(
        delete(AbstractCustomer).where(
            ~exists().where(AmoCustomer.abstract_customer_id == AbstractCustomer.id),
            ~exists().where(AmoLead.abstract_customer_id == AbstractCustomer.id),
        )
    )
