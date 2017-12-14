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

from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import threadgroup
from oslo_utils import timeutils

from qinling import context
from qinling.db import api as db_api
from qinling.db.sqlalchemy import models
from qinling import rpc
from qinling import status
from qinling.utils import constants
from qinling.utils import etcd_util
from qinling.utils import executions
from qinling.utils import jobs
from qinling.utils.openstack import keystone as keystone_utils

LOG = logging.getLogger(__name__)
CONF = cfg.CONF
_periodic_tasks = {}


def handle_function_service_expiration(ctx, engine_client, orchestrator):
    context.set_ctx(ctx)

    delta = timedelta(seconds=CONF.engine.function_service_expiration)
    expiry_time = datetime.utcnow() - delta

    results = db_api.get_functions(
        sort_keys=['updated_at'],
        insecure=True,
        updated_at={'lte': expiry_time}
    )
    if len(results) == 0:
        return

    for func_db in results:
        if not etcd_util.get_service_url(func_db.id):
            continue

        LOG.info(
            'Deleting service mapping and workers for function %s',
            func_db.id
        )

        # Delete resources related to the function
        engine_client.delete_function(func_db.id)

        # Delete etcd keys
        etcd_util.delete_function(func_db.id)


def handle_job(engine_client):
    """Execute job task with no db transactions."""
    for job in db_api.get_next_jobs(timeutils.utcnow() + timedelta(seconds=3)):
        job_id = job.id
        func_id = job.function_id
        LOG.debug("Processing job: %s, function: %s", job_id, func_id)

        func_db = db_api.get_function(func_id, insecure=True)
        trust_id = func_db.trust_id

        try:
            # Setup context before schedule job.
            ctx = keystone_utils.create_trust_context(
                trust_id, job.project_id
            )
            context.set_ctx(ctx)

            if (job.count is not None and job.count > 0):
                job.count -= 1

            # Job delete/update is done using UPDATE ... FROM ... WHERE
            # non-locking clause.
            if job.count == 0:
                modified = db_api.conditional_update(
                    models.Job,
                    {
                        'status': status.DONE,
                        'count': 0
                    },
                    {
                        'id': job_id,
                        'status': status.RUNNING
                    },
                    insecure=True,
                )
            else:
                next_time = jobs.get_next_execution_time(
                    job.pattern,
                    job.next_execution_time
                )

                modified = db_api.conditional_update(
                    models.Job,
                    {
                        'next_execution_time': next_time,
                        'count': job.count
                    },
                    {
                        'id': job_id,
                        'next_execution_time': job.next_execution_time
                    },
                    insecure=True,
                )

            if not modified:
                LOG.warning(
                    'Job %s has been already handled by another periodic '
                    'task.', job_id
                )
                continue

            LOG.debug(
                "Starting to execute function %s by job %s", func_id, job_id
            )

            params = {
                'function_id': func_id,
                'input': job.function_input,
                'sync': False,
                'description': constants.EXECUTION_BY_JOB % job_id
            }
            executions.create_execution(engine_client, params)
        except Exception:
            LOG.exception("Failed to process job %s", job_id)
        finally:
            context.set_ctx(None)


def start_function_mapping_handler(orchestrator):
    tg = threadgroup.ThreadGroup(1)
    engine_client = rpc.get_engine_client()

    tg.add_timer(
        300,
        handle_function_service_expiration,
        ctx=context.Context(),
        engine_client=engine_client,
        orchestrator=orchestrator
    )
    _periodic_tasks[constants.PERIODIC_FUNC_MAPPING_HANDLER] = tg

    LOG.info('Function mapping handler started.')


def start_job_handler():
    tg = threadgroup.ThreadGroup(1)
    engine_client = rpc.get_engine_client()

    tg.add_timer(
        3,
        handle_job,
        engine_client=engine_client,
    )
    _periodic_tasks[constants.PERIODIC_JOB_HANDLER] = tg

    LOG.info('Job handler started.')


def stop(task=None):
    if not task:
        for name, tg in _periodic_tasks.items():
            LOG.info('Stopping periodic task: %s', name)
            tg.stop()
            del _periodic_tasks[name]
    else:
        tg = _periodic_tasks.get(task)
        if tg:
            LOG.info('Stopping periodic task: %s', task)
            tg.stop()
            del _periodic_tasks[task]
