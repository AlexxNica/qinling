# Copyright 2017 Catalyst IT Limited
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
from datetime import datetime
from datetime import timedelta
import time

import mock
from oslo_config import cfg

from qinling import context
from qinling.db import api as db_api
from qinling.engine import default_engine
from qinling.services import periodics
from qinling import status
from qinling.tests.unit import base

CONF = cfg.CONF


class TestPeriodics(base.DbTestCase):
    TEST_CASE_NAME = 'TestPeriodics'

    def setUp(self):
        super(TestPeriodics, self).setUp()
        self.override_config('auth_enable', False, group='pecan')

    @mock.patch('qinling.utils.etcd_util.delete_function')
    @mock.patch('qinling.utils.etcd_util.get_service_url')
    def test_function_service_expiration_handler(self, mock_etcd_url,
                                                 mock_etcd_delete):
        db_func = self.create_function(
            runtime_id=None, prefix=self.TEST_CASE_NAME
        )
        function_id = db_func.id
        # Update function to simulate function execution
        db_api.update_function(function_id, {'count': 1})
        time.sleep(1.5)

        mock_k8s = mock.Mock()
        mock_etcd_url.return_value = 'http://localhost:37718'
        self.override_config('function_service_expiration', 1, 'engine')
        engine = default_engine.DefaultEngine(mock_k8s, CONF.qinling_endpoint)
        periodics.handle_function_service_expiration(self.ctx, engine)

        self.assertEqual(1, mock_k8s.delete_function.call_count)
        args, kwargs = mock_k8s.delete_function.call_args
        self.assertIn(function_id, args)
        mock_etcd_delete.assert_called_once_with(function_id)

    @mock.patch('qinling.utils.jobs.get_next_execution_time')
    def test_job_handler(self, mock_get_next):
        db_func = self.create_function(
            runtime_id=None, prefix=self.TEST_CASE_NAME
        )
        function_id = db_func.id

        self.assertEqual(0, db_func.count)

        now = datetime.utcnow()
        db_job = self.create_job(
            function_id,
            self.TEST_CASE_NAME,
            status=status.RUNNING,
            next_execution_time=now,
            count=2
        )
        job_id = db_job.id

        e_client = mock.Mock()
        mock_get_next.return_value = now + timedelta(seconds=1)
        periodics.handle_job(e_client)
        context.set_ctx(self.ctx)

        db_job = db_api.get_job(job_id)
        self.assertEqual(1, db_job.count)
        db_func = db_api.get_function(function_id)
        self.assertEqual(1, db_func.count)
        db_execs = db_api.get_executions(function_id=function_id)
        self.assertEqual(1, len(db_execs))

        periodics.handle_job(e_client)
        context.set_ctx(self.ctx)

        db_job = db_api.get_job(job_id)
        self.assertEqual(0, db_job.count)
        self.assertEqual(status.DONE, db_job.status)
        db_func = db_api.get_function(function_id)
        self.assertEqual(2, db_func.count)
        db_execs = db_api.get_executions(function_id=function_id)
        self.assertEqual(2, len(db_execs))
