"""
Process the events generated by the SearchSatisfaction eventlogging.

Enriches SearchSatisfaction events with did-you-mean related metrics.
Per-query data with session information is written out to a hive table, and the
per-query data is bucketized, aggregated, and shipped to druid for interactive
querying.
"""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.python_operator import PythonOperator
from airflow.utils.trigger_rule import TriggerRule

import jinja2
from wmf_airflow.hdfs_cli import HdfsCliHook
from wmf_airflow.hdfs_to_druid import HdfsToDruidOperator
from wmf_airflow.hive_partition_range_sensor import HivePartitionRangeSensor
from wmf_airflow.spark_submit import SparkSubmitOperator
from wmf_airflow.template import MEDIAWIKI_ACTIVE_DC, REPO_PATH, YMD_PARTITION, DagConf


dag_conf = DagConf('search_satisfaction_conf')

# Input data
TABLE_SEARCH_EVENTS = dag_conf('table_search_events')
TABLE_SEARCH_LOGS = dag_conf('table_search_logs')

# Where to store enriched events in hive
TABLE_SEARCH_SATISFACTION = dag_conf('table_search_satisfaction')

# Where to store aggregated enriched events in druid
DRUID_DATASOURCE = dag_conf('druid_datasource')

# Template used to create druid ingestion spec
DRUID_SPEC_TEMPLATE = REPO_PATH + dag_conf('druid_spec_template')

# Base path for temporary files
TEMP_DIR = 'hdfs://analytics-hadoop/tmp/{{ dag.dag_id }}_{{ ds }}'

default_args = {
    'owner': 'discovery-analytics',
    'depends_on_past': False,
    'start_date': datetime(2020, 7, 11),
    'email': ['ebernhardson@wikimedia.org'],
    'email_on_failure': True,
    'email_on_retry': False,
    'retries': 5,
    'retry_delay': timedelta(minutes=5),
    'provide_context': True,
}

with DAG(
    'search_satisfaction_daily',
    default_args=default_args,
    schedule_interval='@daily',
    max_active_runs=3,
    catchup=True,
    template_undefined=jinja2.StrictUndefined,
) as dag:
    # Wait for the events that come from browsers
    wait_for_events = HivePartitionRangeSensor(
        task_id='wait_for_events',
        timeout=int(timedelta(days=1).total_seconds()),
        email_on_retry=True,
        table=TABLE_SEARCH_EVENTS,
        period=timedelta(days=1),
        partition_frequency='hours',
        partition_specs=[
            [
                ('year', None), ('month', None),
                ('day', None), ('hour', None),
            ]
        ])

    # Wait for the logs (events, basically) that come from application severs
    wait_for_logs = HivePartitionRangeSensor(
        task_id='wait_for_logs',
        timeout=int(timedelta(days=1).total_seconds()),
        email_on_retry=True,
        table=TABLE_SEARCH_LOGS,
        period=timedelta(days=1),
        partition_frequency='hours',
        partition_specs=[
            [
                ('datacenter', MEDIAWIKI_ACTIVE_DC),
                ('year', None), ('month', None),
                ('day', None), ('hour', None),
            ]
        ])

    # Aggregate frontend and backend logs into unified per-search logs
    aggregate = SparkSubmitOperator(
        task_id='aggregate',
        conf={
            # Defer retrys to airflow
            'spark.yarn.maxAppAttempts': '1',
            'spark.sql.shuffle.partitions': '20',
            'spark.dynamicAllocation.maxExecutors': '50',
        },
        spark_submit_env_vars={
            'PYSPARK_PYTHON': 'python3.7',
        },
        py_files=REPO_PATH + '/spark/wmf_spark.py',
        application=REPO_PATH + '/spark/generate_daily_search_satisfaction.py',
        application_args=[
            '--cirrus-partition', TABLE_SEARCH_LOGS + '/' + YMD_PARTITION,
            '--satisfaction-partition', TABLE_SEARCH_EVENTS + '/' + YMD_PARTITION,
            '--output-partition', TABLE_SEARCH_SATISFACTION + '/' + YMD_PARTITION
        ])

    # Reduces precision (for example, bucketing the number of total hits)
    # and further aggregate over the reduced precision data. Writes output
    # formatted for druid ingestion.
    prepare_json_for_druid = SparkSubmitOperator(
        task_id='prepare_json_for_druid',
        conf={
            'spark.yarn.maxAppAttempts': '1',
            'spark.dynamicAllocation.maxExecutors': '200',
        },
        spark_submit_env_vars={
            'PYSPARK_PYTHON': 'python3.7',
        },
        py_files=REPO_PATH + '/spark/wmf_spark.py',
        application=REPO_PATH + '/spark/generate_daily_druid_search_satisfaction.py',
        application_args=[
            '--source-partition', TABLE_SEARCH_SATISFACTION + '/' + YMD_PARTITION,
            '--destination-directory', TEMP_DIR,
        ])

    index_into_druid = HdfsToDruidOperator(
        task_id='index_into_druid',
        template_file=DRUID_SPEC_TEMPLATE,
        source_directory=TEMP_DIR,
        loaded_period='{{ ds }}/{{ next_ds }}',
        target_datasource=DRUID_DATASOURCE,
        prod_username='analytics-search')

    cleanup_temp_path = PythonOperator(
        task_id='cleanup_temp_path',
        # All done means we delete the temp path regardless of success
        # or failure, we only require that that parent task is "done".
        trigger_rule=TriggerRule.ALL_DONE,
        python_callable=HdfsCliHook.rm,
        op_args=[TEMP_DIR],
        op_kwargs={'recurse': True, 'force': True},
        provide_context=False)

    complete = DummyOperator(task_id='complete')

    [wait_for_logs, wait_for_events] >> aggregate \
        >> prepare_json_for_druid >> index_into_druid \
        >> cleanup_temp_path >> complete

    # Ensure the 'complete' task success is conditioned on the indexing task.
    # The cleanup_temp_path action runs on success or failure of the parents,
    # so the success of the final task would only represent if that deletion
    # worked. By also attaching it to index_into_druid we tie the operator
    # status's together.
    index_into_druid >> complete
