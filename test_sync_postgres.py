import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import Flask
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateSchema, DropSchema

from amocrm import AmoCrmError, Customer, Lead
from main import collect_leads_customers
from models import AbstractCustomer, AmoCustomer, AmoLead, db
from test_sync_rules import _customer, _lead


TEST_DATABASE_URL = os.getenv('TEST_DATABASE_URL')


@unittest.skipUnless(TEST_DATABASE_URL, 'Set TEST_DATABASE_URL to run PostgreSQL tests')
class PostgresSyncTest(unittest.TestCase):
    def setUp(self):
        # Use a fresh schema; never clear existing tables in the supplied database.
        schema = f'test_crm_sync_{uuid4().hex}'
        engine = create_engine(TEST_DATABASE_URL)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        self.addCleanup(self.drop_schema, engine, schema)

        app = Flask(__name__)
        app.config.update(
            SQLALCHEMY_DATABASE_URI=TEST_DATABASE_URL,
            SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'options': f'-csearch_path={schema}'}},
        )
        db.init_app(app)
        context = app.app_context()
        context.push()
        self.addCleanup(db.engine.dispose)
        self.addCleanup(context.pop)
        db.create_all()

    @staticmethod
    def drop_schema(engine, schema):
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))

    def sync(self, customers, leads):
        def fake_amocrm_get(entity, **kwargs):
            yield from {Customer: customers, Lead: leads}[entity]

        with patch('main.amocrm_get', side_effect=fake_amocrm_get):
            collect_leads_customers()

    def snapshot(self):
        return [
            db.session.execute(select(table).order_by(table.c.id)).all()
            for table in (AbstractCustomer.__table__, AmoCustomer.__table__, AmoLead.__table__)
        ]

    def observe_sql(self, callback):
        engine = db.engine
        event.listen(engine, 'before_cursor_execute', callback)
        self.addCleanup(event.remove, engine, 'before_cursor_execute', callback)

    def test_full_sync_merges_groups_and_removes_stale_data(self):
        self.sync(
            [
                _customer(1, 'alice', contacts=[{'id': 101}]),
                _customer(2, 'bob', contacts=[{'id': 202}]),
                _customer(3, 'obsolete'),
            ],
            [_lead(10, 'old'), _lead(11, 'bob')],
        )
        customers = {row.crm_id: row.abstract_customer_id for row in db.session.scalars(select(AmoCustomer))}
        leads = {row.crm_id: row.abstract_customer_id for row in db.session.scalars(select(AmoLead))}
        self.assertNotEqual(customers[1], leads[10])
        self.assertEqual(customers[2], leads[11])
        self.assertEqual(db.session.get(AbstractCustomer, customers[1]).amo_contacts[0]['general']['id'], 101)
        self.assertEqual(db.session.get(AbstractCustomer, customers[2]).amo_contacts[0]['general']['id'], 202)
        db.session.get(AbstractCustomer, leads[10]).send_methods = [{'type': 'mail'}]
        db.session.commit()

        self.sync(
            [
                _customer(1, 'alice', contacts=[{'id': 303}, {'id': 404, 'name': 'УДАЛИТЬ контакт'}]),
                _customer(2, 'bob', name='УДАЛИТЬ покупателя'),
            ],
            [_lead(10, 'alice', name='Сделка обновлена', contacts=[{'id': 505}])],
        )

        customer = db.session.scalars(select(AmoCustomer)).one()
        lead = db.session.scalars(select(AmoLead)).one()
        group = db.session.scalars(select(AbstractCustomer)).one()
        self.assertEqual(customer.crm_id, 1)
        self.assertEqual(lead.crm_id, 10)
        self.assertEqual(lead.name, 'Сделка обновлена')
        self.assertEqual(customer.abstract_customer_id, leads[10])
        self.assertEqual(lead.abstract_customer_id, group.id)
        self.assertEqual(group.send_methods, [{'type': 'mail'}])
        self.assertEqual({item['general']['id'] for item in group.amo_contacts}, {303, 505})
        self.assertIsNotNone(group.updated_at)

    def test_interrupted_crm_does_not_write_or_delete(self):
        self.sync([_customer(1, 'keep')], [_lead(10, 'keep')])
        before = self.snapshot()
        statements = []

        def record(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.split()[0].upper())

        def interrupted():
            yield _lead(20, 'new')
            raise AmoCrmError('connection lost')

        self.observe_sql(record)
        with self.assertLogs('main', level='ERROR'), self.assertRaises(AmoCrmError):
            self.sync([_customer(2, 'new')], interrupted())
        self.assertEqual(statements, ['SELECT', 'SELECT'])
        self.assertEqual(self.snapshot(), before)

    def test_error_during_cleanup_rolls_back_preceding_writes(self):
        self.sync([_customer(1, 'keep')], [_lead(10, 'keep')])
        before = self.snapshot()

        def fail_at_cleanup(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith('DELETE FROM amo_lead'):
                connection.exec_driver_sql('SELECT 1 / 0')

        self.observe_sql(fail_at_cleanup)
        with self.assertLogs('main', level='ERROR'), self.assertRaises(DBAPIError):
            self.sync([_customer(2, 'new')], [_lead(20, 'new')])
        self.assertEqual(self.snapshot(), before)

    def test_repeat_run_updates_3500_groups_in_two_statements(self):
        def customers():
            for i in range(1, 1001):
                customer = _customer(i, f'customer-{i}')
                customer.set_field('Sample', 'x' * 1024, field_id=10)
                yield customer

        def leads():
            for i in range(1, 2501):
                lead = _lead(i, None)
                lead.set_field('Sample', 'x' * 1024, field_id=10)
                yield lead

        self.sync(customers(), leads())
        updates = []

        def record(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith('UPDATE abstract_customer'):
                updates.append((statement, executemany))

        self.observe_sql(record)
        self.sync(customers(), leads())
        self.assertEqual(len(updates), 2)
        self.assertTrue(all('FROM (VALUES' in sql and not many for sql, many in updates))
        self.assertEqual(len(db.session.scalars(select(AbstractCustomer.id)).all()), 3500)
