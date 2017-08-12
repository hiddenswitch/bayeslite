# -*- coding: utf-8 -*-

#   Copyright (c) 2010-2017, MIT Probabilistic Computing Project
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""The Loom Metamodel serves as an interface between BayesDB
and the Loom crosscat backend: `https://github.com/posterior/loom`.

Crosscat is a fully Bayesian nonparametric method for analyzing
heterogeneous, high-dimensional data, described at
`<http://probcomp.csail.mit.edu/crosscat/>`__.

This module implements the :class:`bayeslite.IBayesDBMetamodel`
interface for the Loom Model.
"""
import collections
import csv
import datetime
import gzip
import json
import itertools
import os
import os.path
import tempfile
import time

from StringIO import StringIO
from collections import Counter

import bayeslite.core as core
import bayeslite.metamodel as metamodel
import bayeslite.util as util

from bayeslite.metamodel import bayesdb_metamodel_version
from bayeslite.sqlite3_util import sqlite3_quote_name
from bayeslite.util import casefold
from cgpm.mixtures.view import View
from cgpm.utils.parallel_map import parallel_map
from distributions.io.stream import open_compressed

# TODO should we use "generator" or "metamodel" in the name of

# "bayesdb_loom_generator"

LOOM_SCHEMA_1 = '''
INSERT INTO bayesdb_metamodel (name, version)
    VALUES (?, 1);

CREATE TABLE bayesdb_loom_generator (
    generator_id    INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    name            VARCHAR(64) NOT NULL,
    project_path    VARCHAR(64) NOT NULL,
    PRIMARY KEY(generator_id)
);

CREATE TABLE bayesdb_loom_generator_model_info (
    generator_id    INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    num_models      INTEGER NOT NULL,
    PRIMARY KEY(generator_id)
);

CREATE TABLE bayesdb_loom_string_encoding (
    generator_id    INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    colno           INTEGER NOT NULL,
    string_form     VARCHAR(64) NOT NULL,
    integer_form    INTEGER NOT NULL,
    PRIMARY KEY(generator_id, colno, integer_form)
);

CREATE TABLE bayesdb_loom_column_ordering (
    generator_id    INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    colno           INTEGER NOT NULL,
    rank            INTEGER NOT NULL,
    PRIMARY KEY(generator_id, colno)
);

CREATE TABLE bayesdb_loom_column_kind_partition (
    generator_id    INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    modelno         INTEGER NOT NULL,
    colno           INTEGER NOT NULL,
    kind_id         INTEGER NOT NULL,
    PRIMARY KEY(generator_id, modelno, colno)
);

CREATE TABLE bayesdb_loom_row_kind_partition (
    generator_id    INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    modelno         INTEGER NOT NULL,
    rowid           INTEGER NOT NULL,
    kind_id         INTEGER NOT NULL,
    partition_id    INTEGER NOT NULL,
    PRIMARY KEY(generator_id, modelno, rowid, kind_id)
);
'''

CSV_DELIMITER = ','

# TODO fill out
# TODO optimize number of bdb calls
STATTYPE_TO_LOOMTYPE = {
    'unboundedcategorical': 'dpd',
    'counts': 'gp',
    'boolean': 'bb',
    'categorical': 'dd',
    'cyclic': 'nich',
    'numerical': 'nich',
    'nominal': 'dd'
}


class _Loom():
    def __init__(self, loom_store_path):
        self.loom_store_path = loom_store_path

    def __enter__(self):
        import sys
        # Set the loom store path
        if 'loom.store' in sys.modules:
            self.previous_os_env = sys.modules['loom.store'].STORE
        elif 'LOOM_STORE' in os.environ:
            self.previous_os_env = os.environ['LOOM_STORE']
        else:
            self.previous_os_env = None

        os.environ['LOOM_STORE'] = self.loom_store_path

        if 'loom.store' in sys.modules:
            self.store = reload(sys.modules['loom.store'])
            self.tasks = reload(sys.modules['loom.tasks'])
            self.query = reload(sys.modules['loom.query'])
            self.preql = reload(sys.modules['loom.preql'])
            self.cFormat = reload(sys.modules['loom.cFormat'])
            self.schema_pb2 = reload(sys.modules['loom.schema_pb2'])
        else:
            self.store = __import__('loom.store').store
            self.tasks = __import__('loom.tasks').tasks
            self.query = __import__('loom.query').query
            self.preql = __import__('loom.preql').preql
            self.cFormat = __import__('loom.cFormat').cFormat
            self.schema_pb2 = __import__('loom.schema_pb2').schema_pb2
        assert sys.modules['loom.store'].STORE == self.loom_store_path
        return self

    def __exit__(self, type, value, traceback):
        import sys
        if self.previous_os_env is not None:
            os.environ['LOOM_STORE'] = self.previous_os_env


class LoomMetamodel(metamodel.IBayesDBMetamodel):
    """Loom metamodel for BayesDB.

    The metamodel is named ``loom`` in BQL::

        CREATE GENERATOR t_nig FOR t USING loom

    Internally, the Loom metamodel add SQL tables to the
    database with names that begin with ``bayesdb_loom``.
    """

    def __init__(self, loom_prefix=None,
            loom_store_path=None):
        """Initialize the loom metamodel

        `loom_store_path` is the absolute path at which loom
        stores its data files
        `loom_prefix` is the prefix of the loom project name. If none,
        a timestamp will be used.
        """
        self.loom_prefix = loom_prefix
        self.loom_store_path = loom_store_path
        if self.loom_store_path is None:
            self.loom_store_path = os.path.join(os.getcwd(), 'loomstore')
            if not os.path.isdir(self.loom_store_path):
                os.makedirs(self.loom_store_path)

        # The cache is a dictionary whose keys are bayeslite.BayesDB objects,
        # and whose values are dictionaries (one cache per bdb). We need
        # self._cache to have separate caches for each bdb because the same
        # instance of LoomMetamodel may be used across multiple bdb instances.
        self._cache = dict()
        self._loom = _Loom(self.loom_store_path)

    def name(self):
        return 'loom'

    def register(self, bdb):
        with bdb.savepoint():
            version = bayesdb_metamodel_version(bdb, self.name())
            if version is None:
                bdb.sql_execute(LOOM_SCHEMA_1, (self.name(),))
                version = 1

    def create_generator(self, bdb, generator_id, schema, **kwargs):
        population_id = core.bayesdb_generator_population(bdb, generator_id)
        table = core.bayesdb_population_table(bdb, population_id)

        # Store generator info in bdb
        name = self._generate_name(bdb, generator_id)
        bdb.sql_execute('''
            INSERT INTO bayesdb_loom_generator
            (generator_id, name, project_path)
            VALUES (?, ?, ?)
        ''', (generator_id, name, os.path.join(self.loom_store_path, name)))

        # Collect data from into list form
        headers = []
        data = []
        for colno in core.bayesdb_variable_numbers(bdb, population_id, None):
            column_name = core.bayesdb_variable_name(bdb, population_id, colno)
            headers.append(column_name)

            qt = sqlite3_quote_name(table)
            qcn = sqlite3_quote_name(column_name)

            gather_data_sql = '''
                SELECT %s FROM %s
            ''' % (qcn, qt)
            cursor = bdb.sql_execute(gather_data_sql)
            data.append([item for (item,) in cursor])
        data = [list(i) for i in zip(*data)]

        # Ingest data into loom
        schema_file = self._data_to_schema(bdb, population_id, data)
        csv_file = self._data_to_csv(bdb, population_id, headers, data)

        with _Loom(self.loom_store_path) as loom:
            loom.tasks.ingest(
                self._get_name(bdb, generator_id),
                rows_csv=csv_file.name, schema=schema_file.name)

        # Store encoding info in bdb
        self._store_encoding_info(bdb, generator_id)

    def _store_encoding_info(self, bdb, generator_id):
        encoding_path = os.path.join(self._get_loom_project_path(
                bdb, generator_id), 'ingest', 'encoding.json.gz')
        assert os.path.isfile(encoding_path)
        with gzip.open(encoding_path) as encoding_file:
            encoding = json.loads(encoding_file.read().decode('ascii'))

        population_id = core.bayesdb_generator_population(bdb, generator_id)
        table = core.bayesdb_population_table(bdb, population_id)

        # Store string encoding
        insert_string_encoding = '''
            INSERT INTO bayesdb_loom_string_encoding
                (generator_id, colno, string_form, integer_form)
                VALUES (:generator_id, :colno, :string_form, :integer_form)
        '''
        for col in encoding:
            if 'symbols' in col:
                colno = core.bayesdb_table_column_number(bdb,
                    table, str(col['name']))
                for (string_form, integer_form) in col['symbols'].iteritems():
                    bdb.sql_execute(insert_string_encoding, {
                        'generator_id': generator_id,
                        'colno': colno,
                        'string_form': string_form,
                        'integer_form': integer_form
                    })

        # Store ordering of columns
        insert_order_sql = '''
            INSERT INTO bayesdb_loom_column_ordering
                (generator_id, colno, rank)
                VALUES (:generator_id, :colno, :rank)
        '''
        for col_index in range(len(encoding)):
            colno = core.bayesdb_table_column_number(bdb,
                table, str(encoding[col_index]['name']))
            bdb.sql_execute(insert_order_sql, {
                'generator_id': generator_id,
                'colno': colno,
                'rank': col_index
            })

    def _data_to_csv(self, bdb, population_id, headers, data):
        with tempfile.NamedTemporaryFile(delete=False) as csv_file:
            csv_writer = csv.writer(csv_file, delimiter=CSV_DELIMITER)
            csv_writer.writerow(headers)
            for row in data:
                processed_row = []
                for elem in row:
                    if elem is None:
                        processed_row.append("")
                    elif isinstance(elem, unicode):
                        processed_row.append(elem.encode("ascii", "ignore"))
                    else:
                        processed_row.append(elem)

                csv_writer.writerow(processed_row)
        return csv_file

    def _data_to_schema(self, bdb, population_id, data):
        json_dict = {}
        for colno in core.bayesdb_variable_numbers(bdb,
                population_id, None):
            column_name = core.bayesdb_variable_name(bdb,
                population_id, colno)
            stattype = core.bayesdb_variable_stattype(bdb,
                population_id, colno)
            json_dict[column_name] = STATTYPE_TO_LOOMTYPE[stattype]

        with tempfile.NamedTemporaryFile(delete=False) as schema_file:
            schema_file.write(json.dumps(json_dict))

        return schema_file

    def _generate_name(self, bdb, generator_id):
        # TODO expose name overriding in iventure
        # since jupyter kernals will naturally be stopped
        # so loading back up a large inference should be
        # supported functionality
        return '%s_%s' % (datetime.datetime.fromtimestamp(time.time())
            .strftime('%Y%m%d-%H%M%S.%f') if self.loom_prefix is None else
            self.loom_prefix,
            core.bayesdb_generator_name(bdb, generator_id))

    def _get_name(self, bdb, generator_id):
        return util.cursor_value(bdb.sql_execute('''
            SELECT name FROM bayesdb_loom_generator WHERE
            generator_id=?;
        ''', (generator_id,)))

    def _get_loom_project_path(self, bdb, generator_id):
        return util.cursor_value(bdb.sql_execute('''
            SELECT project_path
            FROM bayesdb_loom_generator WHERE
            generator_id=?;
        ''', (generator_id,)))

    def initialize_models(self, bdb, generator_id, modelnos):
        bdb.sql_execute('''
            DELETE FROM bayesdb_loom_generator_model_info
            WHERE generator_id = ?
        ''', (generator_id,))
        bdb.sql_execute('''
            INSERT INTO bayesdb_loom_generator_model_info
            (generator_id, num_models)
            VALUES (?, ?)
        ''', (generator_id, len(modelnos)))

    def _get_num_models(self, bdb, generator_id):
        return util.cursor_value(bdb.sql_execute('''
            SELECT num_models FROM bayesdb_loom_generator_model_info
            WHERE generator_id = ?;
        ''', (generator_id,)))

    def drop_generator(self, bdb, generator_id):
        self._del_cache_entry(bdb, generator_id, None)

        with bdb.savepoint():
            self.drop_models(bdb, generator_id)
            bdb.sql_execute('''
                DELETE FROM bayesdb_loom_generator
                WHERE generator_id = ?
            ''', (generator_id,))
            bdb.sql_execute('''
                DELETE FROM bayesdb_loom_generator_model_info
                WHERE generator_id = ?
            ''', (generator_id,))
            bdb.sql_execute('''
                DELETE FROM bayesdb_loom_string_encoding
                WHERE generator_id = ?
            ''', (generator_id,))
            bdb.sql_execute('''
                DELETE FROM bayesdb_loom_column_ordering
                WHERE generator_id = ?
            ''', (generator_id,))

    def drop_models(self, bdb, generator_id, modelnos=None):
        with bdb.savepoint():
            if modelnos is None:
                bdb.sql_execute('''
                    DELETE FROM bayesdb_loom_column_kind_partition
                    WHERE generator_id = ?;
                ''', (generator_id,))
                bdb.sql_execute('''
                    DELETE FROM bayesdb_loom_row_kind_partition
                    WHERE generator_id = ?;
                ''', (generator_id,))
                self._del_cache_entry(bdb, generator_id, 'q_server')
                self._del_cache_entry(bdb, generator_id, 'preql_server')
            else:
                for modelno in modelnos:
                    bdb.sql_execute('''
                        DELETE FROM bayesdb_loom_column_kind_partition
                        WHERE generator_id = ? and modelno = ?;
                    ''', (generator_id, modelno))
                    bdb.sql_execute('''
                        DELETE FROM bayesdb_loom_row_kind_partition
                        WHERE generator_id = ? and modelno = ?;
                    ''', (generator_id, modelno))

    def analyze_models(self, bdb, generator_id, modelnos=None, iterations=1,
            max_seconds=None, ckpt_iterations=None, ckpt_seconds=None,
            program=None):

        self.drop_models(bdb, generator_id, modelnos=modelnos)

        name = self._get_name(bdb, generator_id)
        num_models = (self._get_num_models(bdb, generator_id)
            if modelnos is None else len(modelnos))

        # TODO implement extra passes by appending
        # `config={"schedule": {"extra_passes": 1000}`
        with _Loom(self.loom_store_path) as loom:
            loom.tasks.infer(name, sample_count=num_models)
            self._store_kind_partition(bdb, generator_id, modelnos)

        self._load_inferences(bdb, generator_id)

    def _load_inferences(self, bdb, generator_id):
        with _Loom(self.loom_store_path) as loom:
            self._set_cache_entry(bdb, generator_id, 'q_server',
                                  loom.query.get_server(self._get_loom_project_path(bdb, generator_id)))
            self._set_cache_entry(bdb, generator_id, 'preql_server',
                                  loom.tasks.query(self._get_name(bdb, generator_id)))

    def _store_kind_partition(self, bdb, generator_id, modelnos):
        population_id = core.bayesdb_generator_population(bdb, generator_id)
        if modelnos is None:
            modelnos = range(self._get_num_models(bdb, generator_id))
        for modelno in modelnos:
            column_partition = self._retrieve_column_partition(bdb,
                generator_id, modelno)

            column_query = '''
                INSERT INTO bayesdb_loom_column_kind_partition
                (generator_id, modelno, colno, kind_id)
                VALUES
            '''
            for colno in core.bayesdb_variable_numbers(bdb,
                    population_id, None):
                loom_rank = self._get_loom_rank(bdb, generator_id, colno)
                kind_id = column_partition[loom_rank]
                column_query += ' (%d, %d, %d, %d),' % (
                    generator_id, modelno, colno, kind_id)
            bdb.sql_execute(column_query[:-1])

            row_partition = self._retrieve_row_partition(bdb,
                generator_id, modelno)
            row_query = '''
                INSERT INTO bayesdb_loom_row_kind_partition
                (generator_id, modelno,
                rowid, kind_id, partition_id)
                VALUES
            '''
            for kind_id in row_partition.keys():
                for rowid, partition_id in zip(
                        range(1, len(row_partition[kind_id])+1),
                        row_partition[kind_id]):
                    row_query += ' (%d, %d, %d, %d, %d),' % (
                        generator_id, modelno, rowid,
                        kind_id, partition_id)
            bdb.sql_execute(row_query[:-1])

    def _retrieve_column_partition(self, bdb, generator_id, modelno):
        """Return column partition from a CrossCat model.

        The returned structure is of the form `cgpm.crosscat.state.State.Zv`.
        """
        cross_cat = self._get_cross_cat(bdb, generator_id, modelno)
        return dict(itertools.chain.from_iterable([
            [(loom_rank, k) for loom_rank in kind.featureids]
            for k, kind in enumerate(cross_cat.kinds)
        ]))

    def _retrieve_row_partition(self, bdb, generator_id, modelno):
        """Return row partition from a CrossCat model.

        The returned structure is of the form `cgpm.crosscat.state.State.Zv`.
        """
        cross_cat = self._get_cross_cat(bdb, generator_id, modelno)
        num_kinds = len(cross_cat.kinds)
        assign_in = os.path.join(
            self._get_loom_project_path(bdb, generator_id),
            'samples', 'sample.%d' % (modelno,), 'assign.pbs.gz')
        with _Loom(self.loom_store_path) as loom:
            assignments = {
                a.rowid: [a.groupids(k) for k in xrange(num_kinds)]
                for a in loom.cFormat.assignment_stream_load(assign_in)
            }
        rowids = sorted(assignments)
        return {
            k: [assignments[rowid][k] for rowid in rowids]
            for k in xrange(num_kinds)
        }

    def _get_cross_cat(self, bdb, generator_id, modelno):
        """Return the loom CrossCat structure whose id is `modelno`."""
        model_in = os.path.join(
            self._get_loom_project_path(bdb, generator_id),
            'samples', 'sample.%d' % (modelno,), 'model.pb.gz')
        with _Loom(self.loom_store_path) as loom:
            cross_cat = loom.schema_pb2.CrossCat()
        with open_compressed(model_in, 'rb') as f:
            cross_cat.ParseFromString(f.read())
        return cross_cat

    def column_dependence_probability(self,
            bdb, generator_id, modelnos, colno0, colno1):
        hit_list = []
        if modelnos is None:
            modelnos = range(self._get_num_models(bdb, generator_id))
        for modelno in modelnos:
            dependent = self._get_kind_id(
                bdb,
                generator_id,
                modelno,
                colno0
            ) == self._get_kind_id(
                bdb,
                generator_id,
                modelno,
                colno1
            )
            hit_list.append(1 if dependent else 0)

        return sum(hit_list)/float(len(hit_list))

    def _get_kind_id(self, bdb, generator_id, modelno, colno):
        return util.cursor_value(bdb.sql_execute('''
            SELECT kind_id FROM bayesdb_loom_column_kind_partition
            WHERE generator_id = ? and
            modelno = ? and
            colno = ?;
        ''', (generator_id, modelno, colno,)))

    def _get_partition_id(self, bdb, generator_id, modelno, kind_id, rowid):
        return util.cursor_value(bdb.sql_execute('''
            SELECT partition_id FROM bayesdb_loom_row_kind_partition
            WHERE generator_id = ? and
            modelno = ? and
            kind_id = ? and
            rowid = ?;
        ''', (generator_id, modelno, kind_id, rowid)))

    def column_mutual_information(self, bdb, generator_id, modelnos, colnos0,
            colnos1, constraints, numsamples):
        population_id = core.bayesdb_generator_population(bdb, generator_id)
        colnames0 = [str(core.bayesdb_variable_name(bdb, population_id, colno))
            for colno in colnos0]
        colnames1 = [str(core.bayesdb_variable_name(bdb, population_id, colno))
            for colno in colnos1]

        server = self._retrieve_server(bdb, generator_id)
        target_set = server._cols_to_mask(server.encode_set(colnames0))
        query_set = server._cols_to_mask(server.encode_set(colnames1))
        with _Loom(self.loom_store_path) as loom:
            mi = server._query_server.mutual_information(
                target_set,
                query_set,
                entropys=None,
                sample_count=loom.preql.SAMPLE_COUNT)
        return mi

    def row_similarity(self, bdb, generator_id, modelnos, rowid, target_rowid,
            colnos):
        # TODO don't ignore the context
        population_id = core.bayesdb_generator_population(bdb, generator_id)
        _, target_row = zip(*self._reorder_row(bdb, generator_id,
            core.bayesdb_population_row_values(bdb,
                population_id, target_rowid)))
        _, row = zip(*self._reorder_row(bdb, generator_id,
            core.bayesdb_population_row_values(bdb, population_id, rowid)))

        # TODO: cache server
        # Run simlarity query
        server = self._retrieve_server(bdb, generator_id)
        output = server.similar([target_row], rows2=[row])
        return float(output)

    def _retrieve_server(self, bdb, generator_id):
        server = self._get_cache_entry(bdb, generator_id, 'preql_server')
        if server is None:
            self._load_inferences(bdb, generator_id)
            server = self._get_cache_entry(bdb, generator_id, 'preql_server')
        return server

    def _reorder_row(self, bdb, generator_id, row, dense=True):
        """Reorder a row of columns according to loom's column order

        Row should be a list of (colno, value) tuples

        Returns a list of (colno, value) tuples in the proper order.
        """
        ordered_column_labels = self._get_ordered_column_labels(bdb,
            generator_id)
        ordererd_column_dict = collections.OrderedDict(
            [(a, None) for a in ordered_column_labels])

        population_id = core.bayesdb_generator_population(bdb, generator_id)
        # TODO fix bug - colnos are not dense
        for (colno, value) in zip(range(len(row)), row):
            column_name = core.bayesdb_variable_name(bdb, population_id, colno)
            ordererd_column_dict[column_name] = str(value)

        if dense is False:
            return [(colno, value)
                for (colno, value) in ordererd_column_dict.iteritems()
                if value is not None]

        return ordererd_column_dict.iteritems()

    def predictive_relevance(self, bdb, generator_id, modelnos, rowid_target,
            rowid_queries, hypotheticals, colno):
        if modelnos is None:
            modelnos = range(self._get_num_models(bdb, generator_id))

        hitSums = [0 for _ in rowid_queries]
        for modelno in modelnos:
            kind_id_context = self._get_kind_id(bdb,
                generator_id, modelno, colno)
            partition_id_target = self._get_partition_id(bdb,
                generator_id, modelno, kind_id_context, rowid_target)
            for query_index in range(len(rowid_queries)):
                partition_id_query = self._get_partition_id(bdb, generator_id,
                    modelno, kind_id_context, rowid_queries[query_index])
                if partition_id_target == partition_id_query:
                    hitSums[query_index] += 1
        return [xsum/float(len(modelnos)) for xsum in hitSums]

    def predict_confidence(self, bdb, generator_id, modelnos, rowid, colno,
            numsamples=None):
        if not numsamples:
            numsamples = 2
        assert numsamples > 0

        def _impute_categorical(sample):
            counts = Counter(s[0] for s in sample)
            mode_count = max(counts[v] for v in counts)
            pred = iter(v for v in counts if counts[v] == mode_count).next()
            conf = float(mode_count) / numsamples
            return pred, conf

        def _impute_numerical(sample):
            pred = sum(s[0] for s in sample) / float(len(sample))
            conf = 0
            return pred, conf

        def _is_categorical(stattype):
            return casefold(stattype) in ['categorical', 'nominal', 'unboundedcategorical']

        # Retrieve the samples. Specifying `rowid` ensures that relevant
        # constraints are retrieved by `simulate`, so provide empty constraints.
        sample = self.simulate_joint(
            bdb, generator_id, modelnos, rowid, [colno], [], numsamples)

        # Determine the imputation strategy (mode or mean).
        stattype = core.bayesdb_variable_stattype(
            bdb, core.bayesdb_generator_population(bdb, generator_id), colno)
        if _is_categorical(stattype):
            return _impute_categorical(sample)
        else:
            return _impute_numerical(sample)

    def simulate_joint(self, bdb, generator_id, modelnos, rowid, targets,
            constraints, num_samples=1, accuracy=None):
        if rowid != core.bayesdb_generator_fresh_row_id(bdb, generator_id):
            row_values = [str(a) if isinstance(a, unicode) else a
                for a in
                core.bayesdb_generator_row_values(bdb, generator_id, rowid)]

            row = [entry for entry in
                zip(range(len(row_values)), row_values)
                if entry[1] is not None]

            constraints_colnos, _ = ([], []) if len(
                constraints) == 0 else zip(*constraints)
            row_colnos, _ = zip(*row)
            if any([colno in constraints_colnos for colno in row_colnos]):
                raise ValueError('''Conflict between
                    constraints and target row in simulate''')

            constraints += row

        row = {}
        target_no_to_name = {}
        for colno in targets:
            name = core.bayesdb_generator_column_name(bdb,
                generator_id, colno)
            target_no_to_name[colno] = name

            row[name] = ''
        for (colno, value) in constraints:
            row[core.bayesdb_generator_column_name(bdb,
                generator_id, colno)] = value

        csv_headers, csv_values = zip(*row.iteritems())

        # TODO cache
        # Perform predict query with some boiler plate
        # to make loom using StringIO() and an iterable instead of disk
        server = self._retrieve_server(bdb, generator_id)

        # Loom only uses lowercased headers
        # TODO race condition if bayesdb is case sensitive
        # could have duplicate header
        lower_to_upper = {str(a).lower(): str(a) for a in csv_headers}
        csv_headers = lower_to_upper.keys()
        csv_values = [str(a) for a in csv_values]

        outfile = StringIO()
        with _Loom(self.loom_store_path) as loom:
            writer = loom.preql.CsvWriter(outfile, returns=outfile.getvalue)
        reader = iter([csv_headers]+[csv_values])
        server._predict(reader, num_samples, writer, False)
        output = writer.result()

        # Parse output
        returned_headers = [lower_to_upper[a] for a in
                output.strip().split('\r\n')[0].split(CSV_DELIMITER)]
        loom_output = [zip(returned_headers, a.split(CSV_DELIMITER))
            for a in output.strip().split('\r\n')[1:]]
        population_id = core.bayesdb_generator_population(bdb,
            generator_id)
        return_list = []
        for row in loom_output:
            return_list.append([])
            row_dict = dict(row)

            for colno in targets:
                colname = target_no_to_name[colno]
                value = row_dict[colname]
                stattype = core.bayesdb_variable_stattype(
                    bdb, population_id, colno)
                # TODO dont use private
                if core._STATTYPE_TO_AFFINITY[stattype] == 'real':
                    return_list[-1].append(float(value))
                else:
                    return_list[-1].append(value)

        return return_list

    def logpdf_joint(self, bdb, generator_id, modelnos, rowid, targets,
            constraints):
        # TODO optimize bdb calls
        ordered_column_labels = self._get_ordered_column_labels(bdb,
            generator_id)

        and_case = collections.OrderedDict([(a, None)
            for a in ordered_column_labels])
        conditional_case = collections.OrderedDict([(a, None)
            for a in ordered_column_labels])

        population_id = core.bayesdb_generator_population(bdb, generator_id)
        for (colno, value) in targets:
            column_name = core.bayesdb_variable_name(bdb, population_id, colno)

            and_case[column_name] = self._convert_to_proper_stattype(bdb,
                generator_id, colno, value)
            conditional_case[column_name] = None
        for (colno, value) in constraints:
            column_name = core.bayesdb_variable_name(bdb,
                population_id, colno)
            processed_value = self._convert_to_proper_stattype(bdb,
                generator_id, colno, value)

            and_case[column_name] = processed_value
            conditional_case[column_name] = processed_value

        and_case = and_case.values()
        conditional_case = conditional_case.values()

        # TODO cache
        q_server = self._get_cache_entry(bdb, generator_id, 'q_server')
        and_score = q_server.score(and_case)
        conditional_score = q_server.score(conditional_case)
        return and_score - conditional_score

    def _convert_to_proper_stattype(self, bdb, generator_id, colno, value):
        """
        Convert a value from whats given by the logpdf_joint
        method parameters, to what loom can handle.
        Ex. from an integer to real or from a string to an integer
        """
        if value is None:
            return value

        population_id = core.bayesdb_generator_population(bdb,
            generator_id)
        stattype = core.bayesdb_variable_stattype(
            bdb, population_id, colno)

        if core._STATTYPE_TO_AFFINITY[stattype] == 'real':
            return float(value)

        # Lookup the string encoding
        if core._STATTYPE_TO_AFFINITY[stattype] == 'text':
            return self._get_integer_form(bdb, generator_id, colno, value)

        return value

    def _get_integer_form(self, bdb, generator_id, colno, string_form):
        return util.cursor_value(bdb.sql_execute('''
            SELECT integer_form FROM bayesdb_loom_string_encoding
            WHERE generator_id = ? and
            colno = ? and
            string_form = ?;
        ''', (generator_id, colno, string_form,)))

    def _get_ordered_column_labels(self, bdb, generator_id):
        population_id = core.bayesdb_generator_population(bdb, generator_id)
        return [core.bayesdb_variable_name(bdb, population_id, colno)
            for colno in self._get_order(bdb, generator_id)]

    def _get_loom_rank(self, bdb, generator_id, colno):
        return util.cursor_value(bdb.sql_execute('''
            SELECT rank FROM bayesdb_loom_column_ordering
            WHERE generator_id = ? and
            colno = ?
        ''', (generator_id, colno,)))

    def _get_order(self, bdb, generator_id):
        """Get the ordering of the columns according to loom"""
        cursor = bdb.sql_execute('''
            SELECT colno FROM bayesdb_loom_column_ordering
            WHERE generator_id = ?
            ORDER BY rank ASC
        ''', (generator_id,))
        return [colno for (colno,) in cursor]

    def populate_cgpm_engine(self, bdb, generator_id, engine):
        # Update the engine and save the engine.
        args = [
            (bdb, generator_id, engine.states[i], i)
            for i in xrange(engine.num_states())
        ]
        engine.states = parallel_map(self._update_state_mp, args)

        # Transition the non-structural parameters.
        num_transitions = int(len(engine.states[0].outputs)**.5)
        engine.transition(
            N=num_transitions,
            kernels=['column_hypers', 'column_params', 'alpha', 'view_alphas']
        )

    def _update_state_mp(self, args):
        return self._update_state(*args)

    def _update_state(self, bdb, generator_id, state, modelno):
        population_id = core.bayesdb_generator_population(bdb, generator_id)
        column_partition = self._retrieve_column_partition(bdb,
            generator_id, modelno)
        column_partition = {
            colno: column_partition[
                self._get_loom_rank(bdb, generator_id, colno)]
            for colno in
            core.bayesdb_variable_numbers(bdb, population_id, None)}

        row_partition = self._retrieve_row_partition(bdb,
            generator_id, modelno)

        starting_id = max(state.views) + 1
        for view_index in range(len(row_partition)):
            view_id = starting_id + view_index
            view = View(
                state.X,
                outputs=[state.crp_id_view + view_id],
                Zr=row_partition[view_index],
                rng=state.rng
            )
            state._append_view(view, view_id)

        for c in state.outputs:
            v_current = state.Zv(c)
            v_new = column_partition[c] + starting_id
            state._migrate_dim(v_current,
                    v_new, state.dim_for(c), reassign=True)

        state._check_partitions()

        return state

    def _retrieve_cache(self, bdb,):
        if bdb in self._cache:
            return self._cache[bdb]
        self._cache[bdb] = dict()
        return self._cache[bdb]

    def _set_cache_entry(self, bdb, generator_id, key, value):
        cache = self._retrieve_cache(bdb)
        if generator_id not in cache:
            cache[generator_id] = dict()
        cache[generator_id][key] = value

    def _get_cache_entry(self, bdb, generator_id, key):
        # Returns None if the generator_id or key do not exist.
        cache = self._retrieve_cache(bdb)
        if generator_id not in cache:
            return None
        if key not in cache[generator_id]:
            return None
        return cache[generator_id][key]

    def _del_cache_entry(self, bdb, generator_id, key):
        # If key is None, wipes bdb[generator_id] in its entirety.
        cache = self._retrieve_cache(bdb)
        if generator_id in cache:
            if key is None:
                del cache[generator_id]
            elif key in cache[generator_id]:
                del cache[generator_id][key]
