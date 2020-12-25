from airflow.models import BaseOperator
from airflow.hooks.base_hook import BaseHook
from airflow.utils.decorators import apply_defaults

from ewah.dwhooks import get_dwhook
from ewah.constants import EWAHConstants as EC
from ewah.ewah_utils.ssh_tunnel import start_ssh_tunnel
from ewah.ewah_utils.airflow_utils import airflow_datetime_adjustments as ada

from datetime import datetime, timedelta
import time
import copy
import hashlib

class EWAHBaseOperator(BaseOperator):
    """Extension of airflow's native Base Operator.

    EWAH operators always work in the same way: They extract raw data from a
    source and load it into a relational database aka data warehouse aka DWH.
    Transformations only happen insofar as they are required to bring data
    into a relational format.

    *How to use*
    The child class extracts data from the source. At the end of the child
    class' execute() function, it calls self.update(). Optional: The child can
    also repeatedly call the self.update() class for chunking.

    *Arguments of self.update():*
    Data is a list of dictionaries, where each list element will become
    one row of data and the contained dict is fieldname:value. Value can be
    None, or a fieldname can be missing in a particular dictionary, in which
    case the value is assumed to be None.

    columns_definition may be supplied at the self.update() function call,
    at initialization, or not at all, depending on the usecase. The use order is
    - If available, take the columns_definition from the self.update() call
    - If None, check if columns_definition was supplied to the operator at init
    - If neither, create a columns_definition on the fly using (all) the data

    columns_definition is a dictionary with fieldname:properties. Properties
    is None or a dictionary of option:value where option can be one of the
    following:
    - EC.QBC_FIELD_TYPE -> String: Field type (default text)
    - EC.QBC_FIELD_PK -> Boolean: Is this field the primary key? (default False)
    - EC.QBC_FIELD_NN -> Boolean: Not Null constraint for this field? (False)
    - EC.QBC_FIELD_UQ -> Boolean: Unique constraint for this field? (False)

    Note that the value of EC.QBC_FIELD_TYPE is DWH-engine specific!

    columns_definition is case sensitive.

    Implemented DWH engines:
    - PostgreSQL
    - Snowflake

    Next up:
    - BigQuery
    - Redshift
    """

    # in child classes, don't overwrite this but add values within __init__!
    template_fields = (
        'load_data_from',
        'load_data_until',
        'reload_data_from',
    )

    # Child class must update or overwrite these values
    # A missing element is interpreted as False
    _ACCEPTED_LOAD_STRATEGIES = {
        EC.LS_FULL_REFRESH: False,
        EC.LS_INCREMENTAL: False,
        EC.LS_APPENDING: False,
    }

    _REQUIRES_COLUMNS_DEFINITION = False # raise error if true an none supplied

    _INDEX_QUERY = '''
        CREATE INDEX IF NOT EXISTS {0}
        ON "{1}"."{2}" ({3})
    '''

    upload_call_count = 0

    _metadata = {} # to be updated by operator, if applicable

    def __init__(self,
        source_conn_id,
        dwh_engine,
        dwh_conn_id,
        load_strategy,
        target_table_name,
        target_schema_name,
        target_schema_suffix='_next',
        target_database_name=None, # Only for Snowflake
        load_data_from=None, # defaults to execution_date if incremental
        reload_data_from=None, # used preferentially if loading table anew
        load_data_from_relative=None, # optional timedelta
        load_data_until=None, # defaults to next_execution_date if incremental
        load_data_until_relative=None, # optional timedelta
        columns_definition=None,
        update_on_columns=None,
        primary_key_column_name=None,
        clean_data_before_upload=True,
        exclude_columns=[], # list of columns to exclude, if no
        # columns_definition was supplied (e.g. for select * with sql)
        index_columns=[], # list of columns to create an index on. can be
        # an expression, must be quoted in list if quoting is required.
        source_ssh_tunnel_conn_id=None, # create SSH tunnel if set; uses host
        # and port from source_conn_id as remote host and port
        target_ssh_tunnel_conn_id=None, # see source_ssh_tunnel_conn_id
        hash_columns=None, # str or list of str - columns to hash pre-upload
        hashlib_func_name='sha256', # specify hashlib hashing function
        wait_for_seconds=0, # seconds past next_execution_date to wait until
        # wait_for_seconds only applies for incremental loads
    *args, **kwargs):
        super().__init__(*args, **kwargs)

        _msg = 'param "wait_for_seconds" must be a nonnegative integer!'
        assert isinstance(wait_for_seconds, int) and wait_for_seconds >= 0, _msg
        _msg = 'load_strategy {0} not accepted for this operator!'.format(
            load_strategy,
        )
        assert self._ACCEPTED_LOAD_STRATEGIES.get(load_strategy), _msg

        if hash_columns and not clean_data_before_upload:
            _msg = 'column hashing is only possible with data cleaning!'
            raise Exception(_msg)
        elif isinstance(hash_columns, str):
            hash_columns = [hash_columns]
        if hashlib_func_name:
            _msg = 'Invalid hashing function: hashlib.{0}()'
            _msg = _msg.format(hashlib_func_name)
            assert hasattr(hashlib, hashlib_func_name), _msg

        if columns_definition and exclude_columns:
            raise Exception('Must not supply both columns_definition and ' \
                + 'exclude_columns!')

        if not dwh_engine or not dwh_engine in EC.DWH_ENGINES:
            _msg = 'Invalid DWH Engine: {0}\n\nAccepted Engines:\n\t{1}'.format(
                str(dwh_engine),
                '\n\t'.join(EC.DWH_ENGINES),
            )
            raise Exception(_msg)

        if index_columns and not dwh_engine == EC.DWH_ENGINE_POSTGRES:
            raise Exception('Indices are only allowed for PostgreSQL DWHs!')

        if dwh_engine == EC.DWH_ENGINE_SNOWFLAKE:
            if not target_database_name:
                conn_db_name = BaseHook.get_connection(dwh_conn_id)
                conn_db_name = conn_db_name.extra_dejson.get('database')
                if conn_db_name:
                    target_database_name = conn_db_name
                else:
                    raise Exception('If using DWH Engine {0}, must provide {1}!'
                        .format(
                            dwh_engine,
                            '"target_database_name" to specify the Database',
                        )
                    )
        else:
            if target_database_name:
                raise Exception('Received argument for "target_database_name"!')

        if self._REQUIRES_COLUMNS_DEFINITION:
            if not columns_definition:
                raise Exception('This operator requires the argument ' \
                    + 'columns_definition!')

        if primary_key_column_name and update_on_columns:
            raise Exception('Cannot supply BOTH primary_key_column_name AND' + \
                ' update_on_columns!')

        if not load_strategy in (EC.LS_APPENDING, EC.LS_FULL_REFRESH):
            # Required settings for incremental loads
            # Update condition for new load strategies as required
            if not (
                update_on_columns
                or primary_key_column_name
                or (columns_definition and (0 < sum([
                        bool(columns_definition[col].get(
                            EC.QBC_FIELD_PK
                        )) for col in list(columns_definition.keys())
                    ])))
                ):
                raise Exception("If this is incremental loading of a table, "
                    + "one of the following is required:"
                    + "\n- List of columns to update on (update_on_columns)"
                    + "\n- Name of the primary key (primary_key_column_name)"
                    + "\n- Column definition (columns_definition) that includes"
                    + " the primary key(s)"
                )

        _msg = 'load_data_from_relative and load_data_until_relative must be'
        _msg += ' timedelta if supplied!'
        assert isinstance(load_data_from_relative, (type(None), timedelta)), _msg
        assert isinstance(load_data_until_relative, (type(None), timedelta)), _msg

        self.source_conn_id = source_conn_id
        self.dwh_engine = dwh_engine
        self.dwh_conn_id = dwh_conn_id
        self.load_strategy = load_strategy
        self.target_table_name = target_table_name
        self.target_schema_name = target_schema_name
        self.target_schema_suffix = target_schema_suffix
        self.target_database_name = target_database_name
        self.load_data_from = load_data_from
        self.reload_data_from = reload_data_from
        self.load_data_from_relative = load_data_from_relative
        self.load_data_until = load_data_until
        self.load_data_until_relative = load_data_until_relative
        self.columns_definition = columns_definition
        if (not update_on_columns) and primary_key_column_name:
            if type(primary_key_column_name) == str:
                update_on_columns = [primary_key_column_name]
            elif type(primary_key_column_name) in (list, tuple):
                update_on_columns = primary_key_column_name
        self.update_on_columns = update_on_columns
        self.clean_data_before_upload = clean_data_before_upload
        self.primary_key_column_name = primary_key_column_name # may be used ...
        #   ... by a child class at execution!
        self.exclude_columns = exclude_columns
        self.index_columns = index_columns
        self.source_ssh_tunnel_conn_id = source_ssh_tunnel_conn_id
        self.target_ssh_tunnel_conn_id = target_ssh_tunnel_conn_id
        self.hash_columns = hash_columns
        self.hashlib_func_name = hashlib_func_name
        self.wait_for_seconds = wait_for_seconds

        self.hook = get_dwhook(self.dwh_engine)

        _msg = 'DWH hook does not support load strategy {0}!'.format(
            load_strategy,
        )
        assert self.hook._ACCEPTED_LOAD_STRATEGIES.get(load_strategy), _msg

    def execute(self, context):
        """ Why this method is defined here:
            When executing a task, airflow calls this method. Generally, this
            method contains the "business logic" of the individual operator.
            However, EWAH may want to do some actions for all operators. Thus,
            the child operators shall have an ewah_execute() function which is
            called by this general execute() method.
        """

        def close_ssh_tunnels():
            # close SSH tunnels if they exist
            if hasattr(self, 'source_ssh_tunnel_forwarder'):
                self.source_ssh_tunnel_forwarder.stop()
                del self.source_ssh_tunnel_forwarder
            if hasattr(self, 'target_ssh_tunnel_forwarder'):
                self.target_ssh_tunnel_forwarder.stop()
                del self.target_ssh_tunnel_forwarder

        # required for metadata in data upload
        self._execution_time = datetime.now()
        self._context = context


        # the upload hook is used in the self.upload_data() function
        # which is called by the child's ewah_execute function whenever there is
        # data to upload. If applicable: start SSH tunnel first!
        if self.target_ssh_tunnel_conn_id:
            self.log.info('Opening SSH tunnel to target...')
            self.target_ssh_tunnel_forwarder, self.dwh_conn = start_ssh_tunnel(
                ssh_conn_id=self.target_ssh_tunnel_conn_id,
                remote_conn_id=self.dwh_conn_id,
            )
        else:
            self.dwh_conn = BaseHook.get_connection(self.dwh_conn_id)
        self.upload_hook = self.hook(self.dwh_conn)

        # open SSH tunnel for the data source connection, if applicable
        if self.source_ssh_tunnel_conn_id:
            #if self.target_ssh_tunnel_conn_id:
            #    local_port = self.dwh_conn.port + 1
            #else:
            #    local_port = 0 # random assignment
            self.log.info('Opening SSH tunnel to source...')
            self.source_ssh_tunnel_forwarder, self.source_conn=start_ssh_tunnel(
                ssh_conn_id=self.source_ssh_tunnel_conn_id,
                remote_conn_id=self.source_conn_id,
            )
        elif self.source_conn_id:
            # resolve conn id here & delete the object to avoid usage elsewhere
            self.source_conn = BaseHook.get_connection(self.source_conn_id)
        del self.source_conn_id


        # set load_data_from and load_data_until as required
        if self.load_strategy == EC.LS_INCREMENTAL:
            _tdz = timedelta(days=0) # aka timedelta zero

            if self.test_if_target_table_exists():
                if not self.load_data_from:
                    self.load_data_from = context['execution_date']
                self.load_data_from = ada(self.load_data_from)
                self.load_data_from -= self.load_data_from_relative or _tdz
            else:
                # Load data from scratch!
                if self.reload_data_from:
                    self.load_data_from = ada(self.reload_data_from)
                else:
                    self.load_data_from = ada(context['dag'].start_date)

            if not self.load_data_until:
                self.load_data_until = context['next_execution_date']
            self.load_data_until = ada(self.load_data_until)
            self.load_data_until += self.load_data_until_relative or _tdz


        elif self.load_strategy == EC.LS_FULL_REFRESH:
            # Values may still be set as static values
            self.load_data_from = self.reload_data_from or self.load_data_from
            self.load_data_from = ada(self.load_data_from)
            self.load_data_until = ada(self.load_data_until)

        else:
            _msg = 'Must define load_data_from etc. behavior for load strategy!'
            raise Exception(_msg)


        # Have an option to wait until a short period (e.g. 2 minutes) past
        # the incremental loading range timeframe to ensure that all data is
        # loaded, useful e.g. if APIs lag or if server timestamps are not
        # perfectly accurate.
        if self.wait_for_seconds and self.load_strategy == EC.LS_INCREMENTAL:
            wait_until = context.get('next_execution_date')
            if wait_until:
                wait_until += timedelta(seconds=self.wait_for_seconds)
                self.log.info('Awaiting execution until {0}...'.format(
                    str(wait_until),
                ))
            while wait_until and datetime.now() < wait_until:
                # Only sleep a maximum of 5s at a time
                wait_for_timedelta = datetime.now() - wait_until
                time.sleep(min(wait_for_timedelta.total_seconds(), 5))

        try:
            # execute operator
            result = self.ewah_execute(context)

            # if PostgreSQL and arg given: create indices
            for column in self.index_columns:
                # Use hashlib to create a unique 63 character string as index
                # name to avoid breaching index name length limits & accidental
                # duplicates / missing indices due to name truncation leading to
                # identical index names.
                self.hook.execute(self._INDEX_QUERY.format(
                    '__ewah_' + hashlib.blake2b(
                        (self.target_schema_name
                            + self.target_schema_suffix
                            + '.'
                            + self.target_table_name
                            + '.'
                            + column
                        ).encode(),
                        digest_size=28,
                    ).hexdigest(),
                    self.target_schema_name + self.target_schema_suffix,
                    self.target_table_name,
                    column,
                ))

            # commit only at the end, so that no data may be committed before an
            # error occurs.
            self.log.info('Now committing changes!')
            self.upload_hook.commit()
            self.upload_hook.close()
        except:
            # close SSH tunnels on failure before raising the error
            close_ssh_tunnels()
            raise

        # everything worked, now close SSH tunnels
        close_ssh_tunnels()

        return result

    def test_if_target_table_exists(self):
        hook = self.hook(self.dwh_conn)
        if self.dwh_engine == EC.DWH_ENGINE_POSTGRES:
            result = hook.test_if_table_exists(
                table_name=self.target_table_name,
                schema_name=self.target_schema_name + self.target_schema_suffix,
            )
            hook.close()
            return result
        elif self.dwh_engine == EC.DWH_ENGINE_SNOWFLAKE:
            result = hook.test_if_table_exists(
                table_name=self.target_table_name,
                schema_name=self.target_schema_name + self.target_schema_suffix,
                database_name=self.target_database_name,
            )
            hook.close()
            return result
        raise Exception('Function not implemented for DWH {0}!'.format(
            dwh_engine
        ))

    def _create_columns_definition(self, data):
        "Create a columns_definition from data (list of dicts)."
        inconsistent_data_type = EC.QBC_TYPE_MAPPING[self.dwh_engine].get(
            EC.QBC_TYPE_MAPPING_INCONSISTENT
        )
        def get_field_type(value):
            return EC.QBC_TYPE_MAPPING[self.dwh_engine].get(
                type(value)
            ) or inconsistent_data_type

        result = {}
        for datum in data:
            for field in datum.keys():
                if field in self.exclude_columns:
                    datum[field] = None
                elif field in (self.hash_columns or []) \
                    and not result.get(field):
                     # Type is appropriate string type & QBC_FIELD_HASH is true
                     result.update({field:{
                        EC.QBC_FIELD_TYPE: get_field_type('str'),
                        EC.QBC_FIELD_HASH: True,
                     }})
                elif not (result.get(field, {}).get(EC.QBC_FIELD_TYPE) \
                    == inconsistent_data_type) and (not datum[field] is None):
                    if result.get(field):
                        # column has been added in a previous iteration.
                        # If not default column: check if new and old column
                        #   type identification agree.
                        if not (result[field][EC.QBC_FIELD_TYPE] \
                            == get_field_type(datum[field])):
                            self.log.info(
                                'WARNING! Data types are inconsistent.'
                                + ' Affected column: {0}'.format(field)
                            )
                            result[field][EC.QBC_FIELD_TYPE] = \
                                inconsistent_data_type

                    else:
                        # First iteration with this column. Add to result.
                        result.update({field:{
                            EC.QBC_FIELD_TYPE: get_field_type(datum[field])
                        }})
        return result

    def upload_data(self, data=None, columns_definition=None):
        """Upload data, no matter the source. Call this functions in the child
            operator whenever data is available for upload, as often as needed.
        """
        if not data:
            self.log.info('No data to upload!')
            return
        self.upload_call_count += 1
        self.log.info('Chunk {1}: Uploading {0} rows of data.'.format(
            str(len(data)),
            str(self.upload_call_count),
        ))

        self.log.info('Adding metadata...')
        metadata = copy.deepcopy(self._metadata) # from individual operator
        # for all operators alike
        metadata.update({
            '_ewah_executed_at': self._execution_time,
            '_ewah_execution_chunk': self.upload_call_count,
            '_ewah_dag_id': self._context['dag'].dag_id,
            '_ewah_dag_run_id': self._context['run_id'],
            '_ewah_dag_run_execution_date': self._context['execution_date'],
            '_ewah_dag_run_next_execution_date': self._context['next_execution_date'],
        })
        for datum in data:
            datum.update(metadata)

        columns_definition = columns_definition or self.columns_definition
        if not columns_definition:
            self.log.info('Creating table schema on the fly based on data.')
            # Note: This is also where metadata is added, if applicable
            columns_definition = self._create_columns_definition(data)

        if self.update_on_columns:
            pk_list = self.update_on_columns # is a list already
        elif self.primary_key_column_name:
            pk_list = [self.primary_key_column_name]
        else:
            pk_list = []

        if pk_list:
            for pk_name in pk_list:
                if not pk_name in columns_definition.keys():
                    raise Exception(('Column {0} does not exist but is ' + \
                        'expected!').format(pk_name))
                columns_definition[pk_name][EC.QBC_FIELD_PK] = True

        hook = self.upload_hook

        if (self.load_strategy == EC.LS_INCREMENTAL) \
            or (self.upload_call_count > 1):
            self.log.info('Checking for, and applying schema changes.')
            _new_schema_name = self.target_schema_name+self.target_schema_suffix
            new_cols, del_cols = hook.detect_and_apply_schema_changes(
                new_schema_name=_new_schema_name,
                new_table_name=self.target_table_name,
                new_columns_dictionary=columns_definition,
                # When introducing a feature utilizing this, remember to
                #  consider multiple runs within the same execution
                drop_missing_columns=False and self.upload_call_count==1,
                database=self.target_database_name,
                commit=False, # Commit only when / after uploading data
            )
            self.log.info('Added fields:\n\t{0}\nDeleted fields:\n\t{1}'.format(
                '\n\t'.join(new_cols) or '\n',
                '\n\t'.join(del_cols) or '\n',
            ))

        self.log.info('Uploading data now.')
        hook.create_or_update_table(
            data=data,
            columns_definition=columns_definition,
            table_name=self.target_table_name,
            schema_name=self.target_schema_name,
            schema_suffix=self.target_schema_suffix,
            database_name=self.target_database_name,
            drop_and_replace=(self.load_strategy == EC.LS_FULL_REFRESH) and \
                (self.upload_call_count == 1), # In case of chunking of uploads
            update_on_columns=self.update_on_columns,
            commit=False, # See note below for reason
            logging_function=self.log.info,
            clean_data_before_upload=self.clean_data_before_upload,
            hash_columns=self.hash_columns,
            hashlib_func_name=self.hashlib_func_name,
        )
        """ Note on committing changes:
            The hook used for data uploading is created at the beginning of the
            execute function and automatically committed and closed at the end.
            DO NOT commit in this function, as multiple uploads may be required,
            and any intermediate commit may be subsequently followed by an
            error, which would then result in incomplete data committed.
        """

class EWAHEmptyOperator(EWAHBaseOperator):
    _ACCEPTED_LOAD_STRATEGIES = {
        EC.LS_FULL_REFRESH: True,
        EC.LS_INCREMENTAL: True,
        EC.LS_APPENDING: True,
    }
    def __init__(self, *args, **kwargs):
        raise Exception('Failed to load operator! Probably missing' \
            + ' requirements for the operator in question.\n\nSupplied args:' \
            + '\n\t' + '\n\t'.join(args) + '\n\nSupplied kwargs:\n\t' \
            + '\n\t'.join(['{0}: {1}'.format(k, v) for k, v in kwargs.items()])
        )
