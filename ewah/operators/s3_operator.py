from ewah.operators.base_operator import EWAHBaseOperator
from ewah.ewah_utils.airflow_utils import airflow_datetime_adjustments
from ewah.constants import EWAHConstants as EC

from airflow.hooks.S3_hook import S3Hook

from datetime import datetime, timedelta

import json
import csv

class EWAHS3Operator(EWAHBaseOperator):
    """Only implemented for JSON and CSV files from S3 right now!"""

    template_fields = ('data_from', 'data_until')

    _IS_INCREMENTAL = True
    _IS_FULL_REFRESH = True

    _IMPLEMENTED_FORMATS = [
        'JSON',
        'AWS_FIREHOSE_JSON',
        'CSV',
    ]

    def __init__(self,
        bucket_name,
        file_format,
        key_name=None, # if not specified, uses data_from and data_until
        data_from=None,
        data_until=None,
        csv_format_options={},
    *args, **kwargs):

        if not file_format in self._IMPLEMENTED_FORMATS:
            raise Exception('File format not implemented! Available formats:' \
                + ' {0}'.format(str(self._IMPLEMENTED_FORMATS)))

        if key_name:
            if data_from or data_until:
                raise Exception('Both key_name and either data_from or data_' \
                    + 'until specified! Cannot have both specified.')

        if not file_format == 'CSV' and csv_format_options:
            raise Exception('csv_format_options is only valid for CSV files!')

        super().__init__(*args, **kwargs)

        #if not self.drop_and_replace:
        #    if not (data_from and data_until):
        #        raise Exception('For incremental loading, you must specify ' \
        #            + 'both data_from and data_until')
        # if incrementally loading with data_from and data_until: default
        #   to execution dates!

        self.bucket_name = bucket_name
        self.key_name = key_name
        self.data_from = data_from
        self.data_until = data_until
        self.file_format = file_format
        self.csv_format_options = csv_format_options

    def execute(self, context):
        if not self.drop_and_replace and not self.key_name:
            self.data_from = self.data_from or context['execution_date']
            self.data_until = self.data_until or context['next_execution_date']

        if self.file_format == 'JSON':
            return self.execute_json(
                context=context,
                f_get_data=lambda hook, key, bucket: json.loads(
                    hook.read_key(key, bucket)
                ),
            )
        elif self.file_format == 'AWS_FIREHOSE_JSON':
            return self.execute_json(
                context=context,
                f_get_data=lambda hook, key, bucket: json.loads(
                    '['+hook.read_key(key, bucket).replace('}{', '},{')+']'
                ),
            )
        elif self.file_format == 'CSV':
            return self.execute_csv(
                context=context,
            )
        else:
            raise Exception('File format not implemented!')

    def execute_csv(self, context):
        self.data_from = airflow_datetime_adjustments(self.data_from)
        self.data_until = airflow_datetime_adjustments(self.data_until)
        hook = S3Hook(self.source_conn_id)
        if self.key_name:
            data = hook.read_key(self.key_name, self.bucket_name)
        else:
            bucket = hook.get_bucket(self.bucket_name)
            for obj in bucket.objects.all():
                # skip all files outside of loading scope
                if self.data_from and obj.last_modified < self.data_from:
                    continue
                if self.data_until and obj.last_modified >= self.data_until:
                    continue

                self.log.info('Loading data from file {0}'.format(obj.key))
                raw_data = hook.read_key(obj.key, self.bucket_name).splitlines()
                reader = csv.DictReader(raw_data, **self.csv_format_options)
                data = list(reader)
                self.upload_data(data=data)

    def execute_json(self, context, f_get_data):
        self.data_from = airflow_datetime_adjustments(self.data_from)
        self.data_until = airflow_datetime_adjustments(self.data_until)

        hook = S3Hook(self.source_conn_id)

        if self.key_name:
            data = f_get_data(
                hook=hook,
                key=self.key_name,
                bucket=self.bucket_name,
            )
        else:
            data = []
            bucket = hook.get_bucket(self.bucket_name)
            for obj in bucket.objects.all():
                if self.data_from and obj.last_modified < self.data_from:
                    continue
                if self.data_until and obj.last_modified >= self.data_until:
                    continue

                self.log.info('Loading data from file {0}'.format(
                    obj.key,
                ))
                data += f_get_data(
                    hook=hook,
                    key=obj.key,
                    bucket=self.bucket_name,
                )

        print(self.columns_definition)
        self.upload_data(data=data)
