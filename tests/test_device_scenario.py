import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from fastapi import HTTPException
from app.device_scenario import CreateDevice, DeleteDevice, register


class Routes:
    def __init__(self):
        self.handlers = {}

    def get(self, path):
        return lambda fn: self.handlers.setdefault(path, fn)

    post = get


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.db'
        self.routes = Routes()
        self.connections = []
        self.target = dict(status='ready', version='v1', base_url='https://mock.invalid', shop_id=243, store_id=330)
        with self.db() as conn:
            conn.execute('CREATE TABLE environments (id INTEGER PRIMARY KEY, name TEXT, base_url TEXT, headers TEXT, variables TEXT)')
            conn.execute('INSERT INTO environments VALUES(1,?,?,?,?)', ('test', 'https://mock.invalid', '{}', '{}'))
        register(self.routes, self.db, self.decode, {1: {'token': 'test-only'}}, lambda value, _: value, None,
                 venue_target=lambda _: self.target)
        self.calls = []
        self.responses = []
        real_client = httpx.AsyncClient
        self.patcher = patch('app.device_scenario.httpx.AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(self.handler), **kw))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for conn in self.connections:
            conn.close()
        self.tmp.cleanup()

    def db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def decode(self, row):
        return {**dict(row), 'headers': {}, 'variables': {}}

    def handler(self, request):
        self.calls.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    def create(self):
        return asyncio.run(self.routes.handlers['/api/scenarios/create-device/run'](CreateDevice(
            environment_id=1, venue_version='v1', device_name='test', device_sn='TEST-UNIQUE', allow_write=True)))

    def delete(self, run_id):
        return asyncio.run(self.routes.handlers['/api/scenarios/delete-device/run'](DeleteDevice(
            environment_id=1, run_id=run_id, allow_write=True)))

    def test_form_association_then_delete_returned_id(self):
        self.responses = [{'code': 200, 'data': {'device': {'id': 10090}}}, {'code': 200}]
        report = self.create()
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(len(self.calls), 1)
        self.assertIn('application/x-www-form-urlencoded', self.calls[0].headers['content-type'])
        self.assertEqual(parse_qs(self.calls[0].content.decode()), {
            'device_name': ['test'], 'device_sn': ['TEST-UNIQUE'], 'shop_id': ['243'], 'store_id': ['330'], 'is_aliyun': ['false']})
        deleted = self.delete(report['run_id'])
        self.assertEqual(deleted['status'], 'deleted')
        self.assertEqual(json.loads(self.calls[1].content), {'ids': [10090]})
        with self.assertRaises(HTTPException):
            self.delete(report['run_id'])
        self.assertEqual(len(self.calls), 2)

    def test_failed_create_never_allows_deletion(self):
        for content in [{'code': 500, 'data': {'device': {'id': 10089}}}, {'code': 200, 'data': {}}, {'code': 200, 'data': {'device': {'id': True}}}]:
            self.responses = [content]
            report = self.create()
            self.assertEqual(report['status'], 'check_required')
            with self.assertRaises(HTTPException):
                self.delete(report['run_id'])
        self.assertTrue(all(r.method == 'POST' for r in self.calls))

    def test_changed_venue_rejected_before_request(self):
        self.target['version'] = 'v2'
        with self.assertRaises(HTTPException):
            self.create()
        self.assertEqual(self.calls, [])

    def test_changed_environment_rejected_before_delete(self):
        self.responses = [{'code': 200, 'data': {'device': {'id': 77}}}]
        report = self.create()
        with self.db() as conn:
            conn.execute("UPDATE environments SET base_url='https://other.invalid'")
        with self.assertRaises(HTTPException):
            self.delete(report['run_id'])
        self.assertEqual(len(self.calls), 1)

    def test_delete_failure_retains_id_and_blocks_retry(self):
        self.responses = [{'code': 200, 'data': {'device': {'id': 88}}}, httpx.ReadTimeout('test')]
        report = self.delete(self.create()['run_id'])
        self.assertEqual(report['status'], 'check_required')
        self.assertEqual(report['device_id'], 88)
        with self.assertRaises(HTTPException):
            self.delete(report['run_id'])


if __name__ == '__main__':
    unittest.main()
