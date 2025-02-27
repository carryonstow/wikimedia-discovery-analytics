"""Ship mediwaiki revision predictions to elasticsearch

mediawiki/revision/score events are constantly being generated by real
time edits. This DAG waits for events to arrive in hdfs, processes those
events into a set of updates to push to the cirrussearch clusters, and
writes them to a staging area to be shipped by the transfer_to_es dag's.

Primary input to this dag is the revision score events, processed
hourly.  To support the process the dag also fetches two secondary
inputs.  It fetches up-to-date thresholds from ORES public apis on a
daily basis, so the thresholding stays in step with any changes on their
side without any change here. It also fetches the wikibase_item page
property for all known pages from the mariadb replicas on a weekly basis
to facilitate propagation of predictions.

Once inputs are collected the primary transformation is applied in
prepare_mw_rev_score.py. This applies the collected thresholds to the
predictions, and propagates those predictions out using wikibase_item.
The predictions are finally formatted appropriately for elasticsearch
ingestion and stored in a staging table for the transfer_to_es_weekly
DAG to pick up.
"""
from datetime import datetime
import os
from typing import List, Optional

from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.python_operator import PythonOperator
from airflow.operators.hive_operator import HiveOperator
from airflow.sensors.external_task_sensor import ExternalTaskSensor
from airflow.sensors.named_hive_partition_sensor import NamedHivePartitionSensor

from wmf_airflow import DAG
from wmf_airflow.hdfs_cli import HdfsCliHook
from wmf_airflow.skein import SkeinOperator
from wmf_airflow.spark_submit import SparkSubmitOperator
from wmf_airflow.template import (
    HTTPS_PROXY, IVY_SETTINGS_PATH, MARIADB_CREDENTIALS_PATH,
    MEDIAWIKI_CONFIG_PATH, REPO_PATH, YMD_PARTITION,
    YMDH_PARTITION, DagConf, eventgate_partitions)
from wmf_airflow.transfer_to_es import convert_and_upload


dag_conf = DagConf('ores_predictions_conf')

INPUT_TABLE = dag_conf('table_mw_rev_score')
WIKIBASE_ITEM_TABLE = dag_conf('table_wikibase_item')


def mw_sql_to_hive(
    task_id: str,
    sql_query: str,
    output_partition: str,
    mysql_defaults_path: str = MARIADB_CREDENTIALS_PATH,
    mediawiki_config_repo: str = MEDIAWIKI_CONFIG_PATH,
) -> SparkSubmitOperator:
    # The set of wikis to collect from, and where to find the databases
    # for those wikis, is detected through the dblist files.
    dblists = ['s{}.dblist'.format(i) for i in range(1, 9)]
    # Local paths to dblists, so we can ship to executor
    local_dblists = [os.path.join(mediawiki_config_repo, 'dblists', dblist) for dblist in dblists]

    # mysql defaults file to source username / password from. The '#'
    # tells spark to rename to the suffix when placing in working directory.
    mysql_defaults_path += '#mysql.cnf'

    return SparkSubmitOperator(
        task_id=task_id,
        # Custom environment provides dnspython dependency. The environment must come
        # from hdfs, because it has to be built on an older version of debian than runs
        # on the airflow instance.
        name='airflow: ores: ' + task_id,
        archives='{{ wmf_conf.venv_path }}/mw_sql_to_hive.venv.zip#venv',
        py_files=REPO_PATH + '/spark/wmf_spark.py',
        # jdbc connector for talking to analytics replicas
        packages='mysql:mysql-connector-java:8.0.19',
        spark_submit_env_vars={
            # Must be explicitly provided for spark-env.sh. Note that these will not actually
            # be used by spark, it will take the override from spark.pyspark.python. This is
            # necessary to con spark-env.sh into being happy.
            'PYSPARK_PYTHON': 'python3.7',
        },
        conf={
            # Delegate retrys to airflow
            'spark.yarn.maxAppAttempts': '1',
            # Use the venv shipped in archives.
            'spark.pyspark.python': 'venv/bin/python3.7',
            # Fetch jars specified in packages from archiva
            'spark.jars.ivySettings': IVY_SETTINGS_PATH,
            # By default ivy will use $HOME/.ivy2, but system users dont have a home
            'spark.jars.ivy': '/tmp/airflow_ivy2',
            # Limit parallelism so we don't try and query 900 databases all at once
            'spark.dynamicAllocation.maxExecutors': '20',
            # Don't know exactly where it's used, but we need extra memory or we get
            # high gc and timeouts or yarn killing executors.
            'spark.executor.memoryOverhead': '1g',
            'spark.executor.memory': '4g',
        },
        files=','.join([mysql_defaults_path] + local_dblists),
        application=REPO_PATH + '/spark/mw_sql_to_hive.py',
        application_args=[
            '--mysql-defaults-file', 'mysql.cnf',
            '--dblists', ','.join(dblists),
            '--query', sql_query,
            '--output-partition', output_partition,
        ],
    )


def thresholds_path(model: str) -> str:
    return dag_conf('thresholds_prefix') + '/' + model + '_{{ ds_nodash }}.json'


def yesterday_thresholds_path(model: str) -> str:
    # Due to how airflow schedules tasks at the end of a period, to get the daily thresholds
    # for today we use yesterdays date.
    yesterday = "{{ macros.ds_format(macros.ds_add(ds, -1), '%Y-%m-%d', '%Y%m%d') }}"
    return dag_conf('thresholds_prefix') + '/' + model + '_' + yesterday + '.json'


def fetch_thresholds(model: str):
    # Fetch per-topic thresholds from ORES to use when deciding which
    # predictions to discard as not-confident enough.
    return SkeinOperator(
        task_id='fetch_{}_prediction_thresholds'.format(model),
        application=REPO_PATH + '/spark/fetch_ores_thresholds.py',
        application_args=[
            '--model', model,
            '--output-path', 'thresholds.json',
        ],
        output_files={
            'thresholds.json': thresholds_path(model),
        },
        # ORES is not available from the analytics network, we need to
        # proxy to the outside world.
        env={
            'HTTPS_PROXY': HTTPS_PROXY,
        })


def bulk_partition_spec(model: str, wiki: Optional[str]):
    # Worth noting that the actual partitioning also includes the namespace,
    # but we ignore it. The spark integration will still correctly store
    # the underlying data partitioned by namespaces since the script includes
    # the page_namespace column for us.
    if wiki is None:
        # Reads are all wikis at once
        tmpl = '{table}/model={model}/{ymd}'
    else:
        # Writes are per-wiki
        tmpl = '{table}/wikiid={wiki}/model={model}/{ymd}'
    return tmpl.format(
        table='{{ dag_conf.table_scores_export }}',
        wiki=wiki,
        model=model,
        ymd=YMD_PARTITION)


def bulk_ingest(
    wiki: str,
    model: str,
    namespace: int,
    error_threshold: float,
):
    return SparkSubmitOperator(
        task_id='ores_bulk_ingest_{}_for_{}_ns_{}'.format(model, wiki, namespace),
        name='airflow: ores_bulk_ingest {} for {}'.format(model, wiki),
        archives='{{ wmf_conf.venv_path }}/ores_bulk_ingest.venv.zip#venv',
        conf={
            'spark.yarn.maxAppAttempts': '1',
            'spark.dynamicAllocation.maxExecutors': '20',
            # Use the venv shipped in archives.
            'spark.pyspark.python': 'venv/bin/python3.7',
        },
        spark_submit_env_vars={
            'PYSPARK_PYTHON': 'python3.7',
        },
        files=yesterday_thresholds_path(model) + '#thresholds.json',
        py_files=REPO_PATH + '/spark/wmf_spark.py',
        env_vars={
            'HTTPS_PROXY': HTTPS_PROXY
        },
        application=REPO_PATH + '/spark/ores_bulk_ingest.py',
        application_args=[
            '--mediawiki-dbname', wiki,
            '--output-partition', bulk_partition_spec(model, wiki),
            '--ores-model', model,
            '--error-threshold', str(error_threshold),
            '--namespace', str(namespace),
        ]
    )


def extract_predictions(
    model: str,
    input_kind: str,
    output_table: str,
    source: str,
    propagate_from_wiki: Optional[str],
):
    if propagate_from_wiki is None:
        propagate_args: List[str] = []
    else:
        propagate_args = [
            '--wikibase-item-partition',
            WIKIBASE_ITEM_TABLE
            + '/date={{ macros.hive.max_partition(dag_conf.table_wikibase_item).decode("utf8") }}',
            '--propagate-from', propagate_from_wiki,
        ]

    if input_kind == 'mediawiki_revision_score':
        input_partition = INPUT_TABLE + '/@{{ ds }}/{{ macros.ds_add(ds, 7) }}'
    elif input_kind == 'ores_bulk_ingest':
        input_partition = bulk_partition_spec(model, None)
    else:
        raise ValueError('Unknown input_kind: ' + input_kind)

    output_partition = '{table}/{ymdh}/source={source}'.format(
        table=output_table,
        ymdh=YMDH_PARTITION,
        source=source)

    # Extract the data from mediawiki event logs and put into
    # a format suitable for shipping to elasticsearch.
    return SparkSubmitOperator(
        task_id='extract_{}_predictions'.format(model),
        conf={
            # Delegate retrys to airflow
            'spark.yarn.maxAppAttempts': '1',
            'spark.dynamicAllocation.maxExecutors': '20',
        },
        spark_submit_env_vars={
            'PYSPARK_PYTHON': 'python3.7',
        },
        files=yesterday_thresholds_path(model) + '#thresholds.json',
        py_files=REPO_PATH + '/spark/wmf_spark.py',
        application=REPO_PATH + '/spark/prepare_mw_rev_score.py',
        application_args=propagate_args + [
            '--input-partition', input_partition,
            '--input-kind', input_kind,
            '--output-partition', output_partition,
            '--thresholds-path', 'thresholds.json',
            '--prediction', model,
        ],
    )


# Manually triggered dag to initialize deployment
with DAG(
    'ores_predictions_v4_init',
    default_args={
        # Start any time after being deployed and enabled
        'start_date': datetime(2021, 1, 1),
    },
    schedule_interval='@once',
    user_defined_macros={
        'dag_conf': dag_conf.macro,
        'col_wikiid': "`wikiid` string COMMENT 'MediaWiki database name'",
        'col_page_id': "`page_id` int COMMENT 'MediaWiki page_id'",
        'col_page_namespace': "`page_namespace` int"
                              " COMMENT 'MediaWiki namespace page_id belongs to'",
        'col_hour': "`hour` int COMMENT 'Hour collection starts at'",
        'col_source': "`source` string COMMENT 'Name of process staging this partition'",
        'cols_ymd': """
            `year` int COMMENT 'Year collection starts at',
            `month` int COMMENT 'Month collection starts at',
            `day` int COMMENT 'Day collection starts at'""",
    },
) as init_dag:
    complete = DummyOperator(task_id='complete')

    HiveOperator(
        task_id='create_tables',
        hql="""
            CREATE TABLE IF NOT EXISTS {{ dag_conf.table_articletopic }} (
                {{ col_wikiid }},
                {{ col_page_id }},
                {{ col_page_namespace }},
                `articletopic` array<string> COMMENT 'ores articletopic predictions formatted as name|int_score for elasticsearch ingestion'
            )
            PARTITIONED BY (
                {{ col_source }},
                {{ cols_ymd }},
                {{ col_hour }}
            )
            STORED AS PARQUET
            LOCATION '{{ wmf_conf.data_path }}/{{ dag_conf.rel_path_articletopic }}'
            ;

            CREATE TABLE IF NOT EXISTS {{ dag_conf.table_drafttopic }} (
                {{ col_wikiid }},
                {{ col_page_id }},
                {{ col_page_namespace }},
                `drafttopic` array<string> COMMENT 'ores draftopic predictions formatted as name|int_score for elasticsearch ingestion'
            )
            PARTITIONED BY (
                {{ col_source }},
                {{ cols_ymd }},
                {{ col_hour }}
            )
            STORED AS PARQUET
            LOCATION '{{ wmf_conf.data_path }}/{{ dag_conf.rel_path_drafttopic }}'
            ;

            ALTER TABLE {{ dag_conf.table_scores_export }} RENAME TO {{ dag_conf.table_scores_export }}_old;
            CREATE TABLE IF NOT EXISTS {{ dag_conf.table_scores_export }} (
                {{ col_page_id }},
                `probability` map<string,float> COMMENT 'predicted classification as key, confidence as value'
            )
            PARTITIONED BY (
                {{ col_wikiid }},
                `model` string COMMENT 'ORES model that produced predictions',
                {{ col_page_namespace }},
                {{ cols_ymd }}
            )
            STORED AS PARQUET
            LOCATION '{{ wmf_conf.data_path }}/{{ dag_conf.rel_path_scores_export }}'
            ;

            CREATE TABLE IF NOT EXISTS {{ dag_conf.table_wikibase_item }} (
                {{ col_wikiid }},
                {{ col_page_id }},
                {{ col_page_namespace }},
                `wikibase_item` string COMMENT 'wikibase_item page property from mediawiki database'
            )
            PARTITIONED BY (
                `date` string COMMENT 'airflow execution_date of populating task'
            )
            STORED AS PARQUET
            LOCATION '{{ wmf_conf.data_path }}/{{ dag_conf.rel_path_wikibase_item }}'
        """  # noqa
    ) >> complete

    # Ensure the location we want to write thresholds to exists.
    PythonOperator(
        task_id='create_threshold_dir',
        python_callable=HdfsCliHook.mkdir,
        op_kwargs={
            'path': dag_conf('thresholds_prefix'),
            'parents': True
        },
        provide_context=False
    ) >> complete


# This doesn't change very quickly, and is quite a large dataset. Only pull
# from the replicas once a week.
with DAG(
    'ores_predictions_wbitem',
    default_args={
        'start_date': datetime(2021, 1, 1),
    },
    schedule_interval='0 0 * * 0',
    max_active_runs=1,
    # Nothing references exact date=, they use hive.max_partition. If
    # we miss a week there is no benefit to putting new data in a
    # previously dated partition.
    catchup=False,
) as dag_wbitem:
    mw_sql_to_hive(
        task_id='extract_wikibase_item',
        output_partition=WIKIBASE_ITEM_TABLE + "/date={{ ds_nodash }}",
        sql_query="""
            SELECT pp_page as page_id, page_namespace, pp_value as wikibase_item
            FROM page_props
            JOIN page ON page_id = pp_page
            WHERE pp_propname="wikibase_item"
        """
    ) >> DummyOperator(task_id='complete')


with DAG(
    'ores_predictions_daily',
    default_args={
        # Must start 1 day before ores_predictions_hourly, as that
        # means the job runs at the beginning of the day hourly starts.
        'start_date': datetime(2021, 1, 23),
    },
    schedule_interval='@daily',
    max_active_runs=1,
    catchup=True,
) as daily_dag:
    complete = DummyOperator(task_id='complete')
    fetch_thresholds('articletopic') >> complete
    fetch_thresholds('drafttopic') >> complete


with DAG(
    'ores_predictions_hourly',
    default_args={
        'start_date': datetime(2021, 1, 24)
    },
    # Every hour on the hour
    schedule_interval='@hourly',
    # Let the next hour try even if the prior hour is having issues.
    max_active_runs=2,
    catchup=True,
    user_defined_macros={
        'dag_conf': dag_conf.macro,
    },
) as hourly_dag:
    wait_for_thresholds = ExternalTaskSensor(
        task_id='wait_for_thresholds',
        external_dag_id='ores_predictions_daily',
        external_task_id='complete',
        # dt is a pendulum.datetime. We need the task for yesterday, because
        # today's task runs at the *end* of the day.
        execution_date_fn=lambda dt: dt.subtract(days=1).at(hour=0, minute=0, second=0))

    wait_for_hourly_data = NamedHivePartitionSensor(
        task_id='wait_for_hourly_data',
        # We send a failure email once a day when the expected data is not
        # found. Since this is a weekly job we wait up to 4 days for the data
        # to show up before giving up and waiting for next scheduled run.
        timeout=60 * 60 * 6,  # 6 hours
        retries=4,
        email_on_retry=True,
        partition_names=eventgate_partitions(INPUT_TABLE))

    extract_articletopic = extract_predictions(
        model='articletopic',
        input_kind='mediawiki_revision_score',
        output_table=dag_conf('table_articletopic'),
        propagate_from_wiki='enwiki',
        source=hourly_dag.dag_id)

    extract_drafttopic = extract_predictions(
        model='drafttopic',
        input_kind='mediawiki_revision_score',
        output_table=dag_conf('table_drafttopic'),
        propagate_from_wiki=None,
        source=hourly_dag.dag_id)

    # list >> list doesn't get the magic, have to have two invocations
    wait_for_thresholds >> [
        extract_articletopic,
        extract_drafttopic,
    ]
    wait_for_hourly_data >> [
        extract_articletopic,
        extract_drafttopic,
    ] >> DummyOperator(task_id='complete')


with DAG(
    'ores_predictions_bulk_ingest',
    default_args={
        'start_date': datetime(2021, 1, 1)
    },
    # Manually triggered
    schedule_interval=None,
    # This is a full dump and reload of data, running more than one in parallel
    # would be silly.
    max_active_runs=1,
    user_defined_macros={
        'dag_conf': dag_conf.macro,
    },
) as bulk_dag:
    wait_for_thresholds = ExternalTaskSensor(
        task_id='wait_for_thresholds',
        external_dag_id='ores_predictions_daily',
        external_task_id='complete',
        # dt is a pendulum.datetime. We need the task for yesterday, because
        # today's task runs at the *end* of the day.
        execution_date_fn=lambda dt: dt.subtract(days=1).at(hour=0, minute=0, second=0))

    # The ORES api's only allow two connections from a given IP and then start
    # rejecting requests. This means our dump, proxying through a single host,
    # has to run a single task a time. To keep things simple we use a
    # straight-line dag to force sequential execution.
    def bulk_ingest_wikis(last_task, wikis, model, namespaces, error_threshold):
        for wiki in wikis:
            # We run a task per namespace to avoid having a single week long task for enwiki
            # when many namespaces are requested.
            for namespace in namespaces:
                last_task = last_task >> bulk_ingest(wiki, model, namespace, error_threshold)
        return last_task

    last_task = bulk_ingest_wikis(
        wait_for_thresholds,
        wikis=['arwiki', 'cswiki', 'enwiki', 'kowiki', 'testwiki', 'viwiki'],
        model='articletopic',
        namespaces=[0],
        error_threshold=0.001)

    last_task = bulk_ingest_wikis(
        last_task,
        wikis=['enwiki'],
        model='drafttopic',
        # TODO: Unclear what the proper set of namespaces is. This is the set of namespaces
        # seen in drafttopic events for january 2021.
        namespaces=[0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 14, 15, 100, 118, 119, 711, 828],
        # Drafttopic visits most pages on the wiki, errors are much more
        # common outside the content namespaces. Increase error_threshold
        # to help ensure it finishes eventually.
        error_threshold=0.002)

    extract = [
        extract_predictions(
            model='articletopic',
            input_kind='ores_bulk_ingest',
            output_table=dag_conf('table_articletopic'),
            propagate_from_wiki='enwiki',
            source=bulk_dag.dag_id,
        ),
        extract_predictions(
            model='drafttopic',
            input_kind='ores_bulk_ingest',
            output_table=dag_conf('table_drafttopic'),
            propagate_from_wiki=None,
            source=bulk_dag.dag_id,
        ),
    ]

    convert, upload = convert_and_upload(
        'ores_bulk_ingest',
        'freq=bulk')

    last_task >> extract >> convert >> upload >> DummyOperator(task_id='complete')
