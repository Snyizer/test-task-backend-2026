import logging

from abstract_customer import SyncGraph, load_existing, persist_sync
from amocrm import COMPANIES, CONTACTS, Customer, Lead, amocrm_get
from models import db

logger = logging.getLogger(__name__)


def collect_leads_customers(is_full=True):
    """Полная синхронизация покупателей и сделок из CRM.

    Покупатель без логина пропускается. Покупатель ищется по id CRM,
    иначе по логину уже сохранённой сделки, если такая сделка одна.
    Сделка ищется по id CRM, иначе по логину покупателя, если он один.
    Два разных корня сливаются в корень сделки. Чистка запускается
    только после успешного полного прохода.
    """
    session = db.session
    graph = SyncGraph()
    try:
        load_existing(session, graph)
        customers = amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}")
        logger.info("Collected customers %s", is_full)
        for customer in customers:
            if is_full is False and customer.status_id == 111349:
                continue
            graph.consume_customer(customer)

        amo_leads = amocrm_get(Lead, _with=f"{CONTACTS}")
        logger.info("Collected leads %s", is_full)
        for amo_lead in amo_leads:
            if is_full is False and amo_lead.status_id == 143:
                continue
            graph.consume_lead(amo_lead)

        persist_sync(session, graph, is_full=is_full)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Сбор прерван, удаление не выполняется")
        raise
