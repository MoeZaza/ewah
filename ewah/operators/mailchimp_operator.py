from ewah.operators.base_operator import EWAHBaseOperator
from ewah.constants import EWAHConstants as EC

from airflow.hooks.base_hook import BaseHook

import mailchimp3

class EWAHMailchimpOperator(EWAHBaseOperator):

    _IS_INCREMENTAL = False
    _IS_FULL_REFRESH = True

    _REQUIRES_COLUMNS_DEFINITION = False

    def __init__(self, resource=None, *args, **kwargs):
        # API docs: https://mailchimp.com/developer/api/marketing/

        # use target table name as resource if none is given (-> easier config)
        resource = resource or kwargs.get('target_table_name')

        # validate resource name
        if not hasattr(mailchimp3.entities, resource):
            # see here for acceptable resources: https://github.com/VingtCinq/python-mailchimp/blob/master/mailchimp3/__init__.py
            raise Exception('Invalid resource {0}!'.format(resource))

        self.resource = resource
        super().__init__(*args, **kwargs)

    def ewah_execute(self, context):
        # Initiate Mailchimp client
        self.log.info('Connecting to Mailchimp...')
        conn = BaseHook.get_connection(self.source_conn_id)
        mailchimp_cli = mailchimp3.MailChimp(
            mc_user=conn.login,
            mc_api=conn.password,
        )

        # get data!
        self.log.info('Loading data from Mailchimp...')
        resource = self.resource
        data = getattr(mailchimp_cli, resource).all(get_all=True)[resource]

        # upload data
        self.upload_data(data)