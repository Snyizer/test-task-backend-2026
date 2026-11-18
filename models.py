from enum import Enum

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, func, MetaData
from sqlalchemy.dialects.postgresql import JSONB

naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}
db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))


class AbcCustomerEntities(Enum):
    AmoCustomer = 'amo_customers'
    AmoLead = 'amo_leads'
    AmoCompany = 'amo_companies'
    AmoContact = 'amo_contacts'
    SendMethods = 'send_methods'


class AbstractCustomer(db.Model):
    """Корень покупателя.

    Покупатели CRM и сделки лежат в amo_customer / amo_lead.
    Элемент по-прежнему имеет вид {"general": {...}, "api_value": {...}}.
    Контакты, компании и send_methods остаются JSONB-массивами того же вида.
    """

    __tablename__ = "abstract_customer"
    id = Column(db.BigInteger(), nullable=False, primary_key=True)
    amo_companies = Column(JSONB)
    amo_contacts = Column(JSONB)
    send_methods = Column(JSONB)
    last_seen_run = Column(db.BigInteger)
    created_at = Column(db.DateTime, server_default=func.now())
    updated_at = Column(db.DateTime, onupdate=func.now())

    __table_args__ = (
        db.Index("ix_abstract_customer_last_seen_run", "last_seen_run"),
    )


class AmoCustomer(db.Model):
    """Покупатель CRM. crm_id, login и name вынесены под точный поиск."""

    __tablename__ = "amo_customer"
    id = Column(db.BigInteger(), nullable=False, primary_key=True)
    abstract_customer_id = Column(
        db.BigInteger(),
        db.ForeignKey("abstract_customer.id"),
        nullable=False,
    )
    crm_id = Column(db.BigInteger(), nullable=False, unique=True)
    login = Column(db.String(512))
    name = Column(db.String(512))
    general = Column(JSONB, nullable=False)
    api_value = Column(JSONB)
    last_seen_run = Column(db.BigInteger)

    __table_args__ = (
        db.Index("ix_amo_customer_login", "login"),
        db.Index("ix_amo_customer_name", "name"),
        db.Index("ix_amo_customer_abstract_customer_id", "abstract_customer_id"),
        db.Index("ix_amo_customer_last_seen_run", "last_seen_run"),
    )


class AmoLead(db.Model):
    """Сделка CRM. Связь со покупателем — общий abstract_customer_id."""

    __tablename__ = "amo_lead"
    id = Column(db.BigInteger(), nullable=False, primary_key=True)
    abstract_customer_id = Column(
        db.BigInteger(),
        db.ForeignKey("abstract_customer.id"),
        nullable=False,
    )
    crm_id = Column(db.BigInteger(), nullable=False, unique=True)
    login = Column(db.String(512))
    name = Column(db.String(512))
    general = Column(JSONB, nullable=False)
    api_value = Column(JSONB)
    last_seen_run = Column(db.BigInteger)

    __table_args__ = (
        db.Index("ix_amo_lead_login", "login"),
        db.Index("ix_amo_lead_name", "name"),
        db.Index("ix_amo_lead_abstract_customer_id", "abstract_customer_id"),
        db.Index("ix_amo_lead_last_seen_run", "last_seen_run"),
    )
