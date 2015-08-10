# -*- coding: utf-8 -*-
# Copyright 2014, 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging

from synapse.api.errors import StoreError
from synapse.util.async import ObservableDeferred
from synapse.util import unwrapFirstError
from synapse.util.logutils import log_function
from synapse.util.logcontext import preserve_context_over_fn, LoggingContext
from synapse.util.lrucache import LruCache
from synapse.util.dictionary_cache import DictionaryCache
import synapse.metrics

from util.id_generators import IdGenerator, StreamIdGenerator

from twisted.internet import defer

from collections import namedtuple, OrderedDict

import functools
import inspect
import sys
import time
import threading

DEBUG_CACHES = False

logger = logging.getLogger(__name__)

sql_logger = logging.getLogger("synapse.storage.SQL")
transaction_logger = logging.getLogger("synapse.storage.txn")
perf_logger = logging.getLogger("synapse.storage.TIME")


metrics = synapse.metrics.get_metrics_for("synapse.storage")

sql_scheduling_timer = metrics.register_distribution("schedule_time")

sql_query_timer = metrics.register_distribution("query_time", labels=["verb"])
sql_txn_timer = metrics.register_distribution("transaction_time", labels=["desc"])

caches_by_name = {}
cache_counter = metrics.register_cache(
    "cache",
    lambda: {(name,): len(caches_by_name[name]) for name in caches_by_name.keys()},
    labels=["name"],
)


_CacheSentinel = object()


class Cache(object):

    def __init__(self, name, max_entries=1000, keylen=1, lru=True):
        if lru:
            self.cache = LruCache(max_size=max_entries)
            self.max_entries = None
        else:
            self.cache = OrderedDict()
            self.max_entries = max_entries

        self.name = name
        self.keylen = keylen
        self.sequence = 0
        self.thread = None
        caches_by_name[name] = self.cache

    def check_thread(self):
        expected_thread = self.thread
        if expected_thread is None:
            self.thread = threading.current_thread()
        else:
            if expected_thread is not threading.current_thread():
                raise ValueError(
                    "Cache objects can only be accessed from the main thread"
                )

    def get(self, key, default=_CacheSentinel):
        val = self.cache.get(key, _CacheSentinel)
        if val is not _CacheSentinel:
            cache_counter.inc_hits(self.name)
            return val

        cache_counter.inc_misses(self.name)

        if default is _CacheSentinel:
            raise KeyError()
        else:
            return default

    def update(self, sequence, key, value):
        self.check_thread()
        if self.sequence == sequence:
            # Only update the cache if the caches sequence number matches the
            # number that the cache had before the SELECT was started (SYN-369)
            self.prefill(key, value)

    def prefill(self, key, value):
        if self.max_entries is not None:
            while len(self.cache) >= self.max_entries:
                self.cache.popitem(last=False)

        self.cache[key] = value

    def invalidate(self, key):
        self.check_thread()
        if not isinstance(key, tuple):
            raise ValueError("keyargs must be a tuple.")

        # Increment the sequence number so that any SELECT statements that
        # raced with the INSERT don't update the cache (SYN-369)
        self.sequence += 1
        self.cache.pop(key, None)

    def invalidate_all(self):
        self.check_thread()
        self.sequence += 1
        self.cache.clear()


class CacheDescriptor(object):
    """ A method decorator that applies a memoizing cache around the function.

    This caches deferreds, rather than the results themselves. Deferreds that
    fail are removed from the cache.

    The function is presumed to take zero or more arguments, which are used in
    a tuple as the key for the cache. Hits are served directly from the cache;
    misses use the function body to generate the value.

    The wrapped function has an additional member, a callable called
    "invalidate". This can be used to remove individual entries from the cache.

    The wrapped function has another additional callable, called "prefill",
    which can be used to insert values into the cache specifically, without
    calling the calculation function.
    """
    def __init__(self, orig, max_entries=1000, num_args=1, lru=True,
                 inlineCallbacks=False):
        self.orig = orig

        if inlineCallbacks:
            self.function_to_call = defer.inlineCallbacks(orig)
        else:
            self.function_to_call = orig

        self.max_entries = max_entries
        self.num_args = num_args
        self.lru = lru

        self.arg_names = inspect.getargspec(orig).args[1:num_args+1]

        if len(self.arg_names) < self.num_args:
            raise Exception(
                "Not enough explicit positional arguments to key off of for %r."
                " (@cached cannot key off of *args or **kwars)"
                % (orig.__name__,)
            )

        self.cache = Cache(
            name=self.orig.__name__,
            max_entries=self.max_entries,
            keylen=self.num_args,
            lru=self.lru,
        )

    def __get__(self, obj, objtype=None):

        @functools.wraps(self.orig)
        def wrapped(*args, **kwargs):
            arg_dict = inspect.getcallargs(self.orig, obj, *args, **kwargs)
            cache_key = tuple(arg_dict[arg_nm] for arg_nm in self.arg_names)
            try:
                cached_result_d = self.cache.get(cache_key)

                observer = cached_result_d.observe()
                if DEBUG_CACHES:
                    @defer.inlineCallbacks
                    def check_result(cached_result):
                        actual_result = yield self.function_to_call(obj, *args, **kwargs)
                        if actual_result != cached_result:
                            logger.error(
                                "Stale cache entry %s%r: cached: %r, actual %r",
                                self.orig.__name__, cache_key,
                                cached_result, actual_result,
                            )
                            raise ValueError("Stale cache entry")
                        defer.returnValue(cached_result)
                    observer.addCallback(check_result)

                return observer
            except KeyError:
                # Get the sequence number of the cache before reading from the
                # database so that we can tell if the cache is invalidated
                # while the SELECT is executing (SYN-369)
                sequence = self.cache.sequence

                ret = defer.maybeDeferred(
                    self.function_to_call,
                    obj, *args, **kwargs
                )

                def onErr(f):
                    self.cache.invalidate(cache_key)
                    return f

                ret.addErrback(onErr)

                ret = ObservableDeferred(ret, consumeErrors=True)
                self.cache.update(sequence, cache_key, ret)

                return ret.observe()

        wrapped.invalidate = self.cache.invalidate
        wrapped.invalidate_all = self.cache.invalidate_all
        wrapped.prefill = self.cache.prefill

        obj.__dict__[self.orig.__name__] = wrapped

        return wrapped


class CacheListDescriptor(object):
    """Wraps an existing cache to support bulk fetching of keys.

    Given a list of keys it looks in the cache to find any hits, then passes
    the list of missing keys to the wrapped fucntion.
    """

    def __init__(self, orig, cache, list_name, num_args=1, inlineCallbacks=False):
        self.orig = orig

        if inlineCallbacks:
            self.function_to_call = defer.inlineCallbacks(orig)
        else:
            self.function_to_call = orig

        self.num_args = num_args
        self.list_name = list_name

        self.arg_names = inspect.getargspec(orig).args[1:num_args+1]
        self.list_pos = self.arg_names.index(self.list_name)

        self.cache = cache

        self.sentinel = object()

        if len(self.arg_names) < self.num_args:
            raise Exception(
                "Not enough explicit positional arguments to key off of for %r."
                " (@cached cannot key off of *args or **kwars)"
                % (orig.__name__,)
            )

        if self.list_name not in self.arg_names:
            raise Exception(
                "Couldn't see arguments %r for %r."
                % (self.list_name, cache.name,)
            )

    def __get__(self, obj, objtype=None):

        @functools.wraps(self.orig)
        def wrapped(*args, **kwargs):
            arg_dict = inspect.getcallargs(self.orig, obj, *args, **kwargs)
            keyargs = [arg_dict[arg_nm] for arg_nm in self.arg_names]
            list_args = arg_dict[self.list_name]

            cached = {}
            missing = []
            for arg in list_args:
                key = list(keyargs)
                key[self.list_pos] = arg

                try:
                    res = self.cache.get(tuple(key)).observe()
                    res.addCallback(lambda r, arg: (arg, r), arg)
                    cached[arg] = res
                except KeyError:
                    missing.append(arg)

            if missing:
                sequence = self.cache.sequence
                args_to_call = dict(arg_dict)
                args_to_call[self.list_name] = missing

                ret_d = defer.maybeDeferred(
                    self.function_to_call,
                    **args_to_call
                )

                ret_d = ObservableDeferred(ret_d)

                for arg in missing:
                    observer = ret_d.observe()
                    observer.addCallback(lambda r, arg: r[arg], arg)

                    observer = ObservableDeferred(observer)

                    key = list(keyargs)
                    key[self.list_pos] = arg
                    self.cache.update(sequence, tuple(key), observer)

                    def invalidate(f, key):
                        self.cache.invalidate(key)
                        return f
                    observer.addErrback(invalidate, tuple(key))

                    res = observer.observe()
                    res.addCallback(lambda r, arg: (arg, r), arg)

                    cached[arg] = res

            return defer.gatherResults(
                cached.values(),
                consumeErrors=True,
            ).addErrback(unwrapFirstError).addCallback(lambda res: dict(res))

        obj.__dict__[self.orig.__name__] = wrapped

        return wrapped


def cached(max_entries=1000, num_args=1, lru=True):
    return lambda orig: CacheDescriptor(
        orig,
        max_entries=max_entries,
        num_args=num_args,
        lru=lru
    )


def cachedInlineCallbacks(max_entries=1000, num_args=1, lru=False):
    return lambda orig: CacheDescriptor(
        orig,
        max_entries=max_entries,
        num_args=num_args,
        lru=lru,
        inlineCallbacks=True,
    )


def cachedList(cache, list_name, num_args=1, inlineCallbacks=False):
    return lambda orig: CacheListDescriptor(
        orig,
        cache=cache,
        list_name=list_name,
        num_args=num_args,
        inlineCallbacks=inlineCallbacks,
    )


class LoggingTransaction(object):
    """An object that almost-transparently proxies for the 'txn' object
    passed to the constructor. Adds logging and metrics to the .execute()
    method."""
    __slots__ = ["txn", "name", "database_engine", "after_callbacks"]

    def __init__(self, txn, name, database_engine, after_callbacks):
        object.__setattr__(self, "txn", txn)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "database_engine", database_engine)
        object.__setattr__(self, "after_callbacks", after_callbacks)

    def call_after(self, callback, *args):
        """Call the given callback on the main twisted thread after the
        transaction has finished. Used to invalidate the caches on the
        correct thread.
        """
        self.after_callbacks.append((callback, args))

    def __getattr__(self, name):
        return getattr(self.txn, name)

    def __setattr__(self, name, value):
        setattr(self.txn, name, value)

    def execute(self, sql, *args):
        self._do_execute(self.txn.execute, sql, *args)

    def executemany(self, sql, *args):
        self._do_execute(self.txn.executemany, sql, *args)

    def _do_execute(self, func, sql, *args):
        # TODO(paul): Maybe use 'info' and 'debug' for values?
        sql_logger.debug("[SQL] {%s} %s", self.name, sql)

        sql = self.database_engine.convert_param_style(sql)

        if args:
            try:
                sql_logger.debug(
                    "[SQL values] {%s} %r",
                    self.name, args[0]
                )
            except:
                # Don't let logging failures stop SQL from working
                pass

        start = time.time() * 1000

        try:
            return func(
                sql, *args
            )
        except Exception as e:
            logger.debug("[SQL FAIL] {%s} %s", self.name, e)
            raise
        finally:
            msecs = (time.time() * 1000) - start
            sql_logger.debug("[SQL time] {%s} %f", self.name, msecs)
            sql_query_timer.inc_by(msecs, sql.split()[0])


class PerformanceCounters(object):
    def __init__(self):
        self.current_counters = {}
        self.previous_counters = {}

    def update(self, key, start_time, end_time=None):
        if end_time is None:
            end_time = time.time() * 1000
        duration = end_time - start_time
        count, cum_time = self.current_counters.get(key, (0, 0))
        count += 1
        cum_time += duration
        self.current_counters[key] = (count, cum_time)
        return end_time

    def interval(self, interval_duration, limit=3):
        counters = []
        for name, (count, cum_time) in self.current_counters.items():
            prev_count, prev_time = self.previous_counters.get(name, (0, 0))
            counters.append((
                (cum_time - prev_time) / interval_duration,
                count - prev_count,
                name
            ))

        self.previous_counters = dict(self.current_counters)

        counters.sort(reverse=True)

        top_n_counters = ", ".join(
            "%s(%d): %.3f%%" % (name, count, 100 * ratio)
            for ratio, count, name in counters[:limit]
        )

        return top_n_counters


class SQLBaseStore(object):
    _TXN_ID = 0

    def __init__(self, hs):
        self.hs = hs
        self._db_pool = hs.get_db_pool()
        self._clock = hs.get_clock()

        self._previous_txn_total_time = 0
        self._current_txn_total_time = 0
        self._previous_loop_ts = 0

        # TODO(paul): These can eventually be removed once the metrics code
        #   is running in mainline, and we have some nice monitoring frontends
        #   to watch it
        self._txn_perf_counters = PerformanceCounters()
        self._get_event_counters = PerformanceCounters()

        self._get_event_cache = Cache("*getEvent*", keylen=3, lru=True,
                                      max_entries=hs.config.event_cache_size)

        self._state_group_cache = DictionaryCache("*stateGroupCache*", 100000)

        self._event_fetch_lock = threading.Condition()
        self._event_fetch_list = []
        self._event_fetch_ongoing = 0

        self._pending_ds = []

        self.database_engine = hs.database_engine

        self._stream_id_gen = StreamIdGenerator("events", "stream_ordering")
        self._transaction_id_gen = IdGenerator("sent_transactions", "id", self)
        self._state_groups_id_gen = IdGenerator("state_groups", "id", self)
        self._access_tokens_id_gen = IdGenerator("access_tokens", "id", self)
        self._pushers_id_gen = IdGenerator("pushers", "id", self)
        self._push_rule_id_gen = IdGenerator("push_rules", "id", self)
        self._push_rules_enable_id_gen = IdGenerator("push_rules_enable", "id", self)
        self._receipts_id_gen = StreamIdGenerator("receipts_linearized", "stream_id")

    def start_profiling(self):
        self._previous_loop_ts = self._clock.time_msec()

        def loop():
            curr = self._current_txn_total_time
            prev = self._previous_txn_total_time
            self._previous_txn_total_time = curr

            time_now = self._clock.time_msec()
            time_then = self._previous_loop_ts
            self._previous_loop_ts = time_now

            ratio = (curr - prev)/(time_now - time_then)

            top_three_counters = self._txn_perf_counters.interval(
                time_now - time_then, limit=3
            )

            top_3_event_counters = self._get_event_counters.interval(
                time_now - time_then, limit=3
            )

            perf_logger.info(
                "Total database time: %.3f%% {%s} {%s}",
                ratio * 100, top_three_counters, top_3_event_counters
            )

        self._clock.looping_call(loop, 10000)

    def _new_transaction(self, conn, desc, after_callbacks, func, *args, **kwargs):
        start = time.time() * 1000
        txn_id = self._TXN_ID

        # We don't really need these to be unique, so lets stop it from
        # growing really large.
        self._TXN_ID = (self._TXN_ID + 1) % (sys.maxint - 1)

        name = "%s-%x" % (desc, txn_id, )

        transaction_logger.debug("[TXN START] {%s}", name)

        try:
            i = 0
            N = 5
            while True:
                try:
                    txn = conn.cursor()
                    txn = LoggingTransaction(
                        txn, name, self.database_engine, after_callbacks
                    )
                    r = func(txn, *args, **kwargs)
                    conn.commit()
                    return r
                except self.database_engine.module.OperationalError as e:
                    # This can happen if the database disappears mid
                    # transaction.
                    logger.warn(
                        "[TXN OPERROR] {%s} %s %d/%d",
                        name, e, i, N
                    )
                    if i < N:
                        i += 1
                        try:
                            conn.rollback()
                        except self.database_engine.module.Error as e1:
                            logger.warn(
                                "[TXN EROLL] {%s} %s",
                                name, e1,
                            )
                        continue
                    raise
                except self.database_engine.module.DatabaseError as e:
                    if self.database_engine.is_deadlock(e):
                        logger.warn("[TXN DEADLOCK] {%s} %d/%d", name, i, N)
                        if i < N:
                            i += 1
                            try:
                                conn.rollback()
                            except self.database_engine.module.Error as e1:
                                logger.warn(
                                    "[TXN EROLL] {%s} %s",
                                    name, e1,
                                )
                            continue
                    raise
        except Exception as e:
            logger.debug("[TXN FAIL] {%s} %s", name, e)
            raise
        finally:
            end = time.time() * 1000
            duration = end - start

            transaction_logger.debug("[TXN END] {%s} %f", name, duration)

            self._current_txn_total_time += duration
            self._txn_perf_counters.update(desc, start, end)
            sql_txn_timer.inc_by(duration, desc)

    @defer.inlineCallbacks
    def runInteraction(self, desc, func, *args, **kwargs):
        """Wraps the .runInteraction() method on the underlying db_pool."""
        current_context = LoggingContext.current_context()

        start_time = time.time() * 1000

        after_callbacks = []

        def inner_func(conn, *args, **kwargs):
            with LoggingContext("runInteraction") as context:
                sql_scheduling_timer.inc_by(time.time() * 1000 - start_time)

                if self.database_engine.is_connection_closed(conn):
                    logger.debug("Reconnecting closed database connection")
                    conn.reconnect()

                current_context.copy_to(context)
                return self._new_transaction(
                    conn, desc, after_callbacks, func, *args, **kwargs
                )

        result = yield preserve_context_over_fn(
            self._db_pool.runWithConnection,
            inner_func, *args, **kwargs
        )

        for after_callback, after_args in after_callbacks:
            after_callback(*after_args)
        defer.returnValue(result)

    @defer.inlineCallbacks
    def runWithConnection(self, func, *args, **kwargs):
        """Wraps the .runInteraction() method on the underlying db_pool."""
        current_context = LoggingContext.current_context()

        start_time = time.time() * 1000

        def inner_func(conn, *args, **kwargs):
            with LoggingContext("runWithConnection") as context:
                sql_scheduling_timer.inc_by(time.time() * 1000 - start_time)

                if self.database_engine.is_connection_closed(conn):
                    logger.debug("Reconnecting closed database connection")
                    conn.reconnect()

                current_context.copy_to(context)

                return func(conn, *args, **kwargs)

        result = yield preserve_context_over_fn(
            self._db_pool.runWithConnection,
            inner_func, *args, **kwargs
        )

        defer.returnValue(result)

    def cursor_to_dict(self, cursor):
        """Converts a SQL cursor into an list of dicts.

        Args:
            cursor : The DBAPI cursor which has executed a query.
        Returns:
            A list of dicts where the key is the column header.
        """
        col_headers = list(column[0] for column in cursor.description)
        results = list(
            dict(zip(col_headers, row)) for row in cursor.fetchall()
        )
        return results

    def _execute(self, desc, decoder, query, *args):
        """Runs a single query for a result set.

        Args:
            decoder - The function which can resolve the cursor results to
                something meaningful.
            query - The query string to execute
            *args - Query args.
        Returns:
            The result of decoder(results)
        """
        def interaction(txn):
            txn.execute(query, args)
            if decoder:
                return decoder(txn)
            else:
                return txn.fetchall()

        return self.runInteraction(desc, interaction)

    def _execute_and_decode(self, desc, query, *args):
        return self._execute(desc, self.cursor_to_dict, query, *args)

    # "Simple" SQL API methods that operate on a single table with no JOINs,
    # no complex WHERE clauses, just a dict of values for columns.

    @defer.inlineCallbacks
    def _simple_insert(self, table, values, or_ignore=False,
                       desc="_simple_insert"):
        """Executes an INSERT query on the named table.

        Args:
            table : string giving the table name
            values : dict of new column names and values for them
        """
        try:
            yield self.runInteraction(
                desc,
                self._simple_insert_txn, table, values,
            )
        except self.database_engine.module.IntegrityError:
            # We have to do or_ignore flag at this layer, since we can't reuse
            # a cursor after we receive an error from the db.
            if not or_ignore:
                raise

    @log_function
    def _simple_insert_txn(self, txn, table, values):
        keys, vals = zip(*values.items())

        sql = "INSERT INTO %s (%s) VALUES(%s)" % (
            table,
            ", ".join(k for k in keys),
            ", ".join("?" for _ in keys)
        )

        txn.execute(sql, vals)

    def _simple_insert_many_txn(self, txn, table, values):
        if not values:
            return

        # This is a *slight* abomination to get a list of tuples of key names
        # and a list of tuples of value names.
        #
        # i.e. [{"a": 1, "b": 2}, {"c": 3, "d": 4}]
        #         => [("a", "b",), ("c", "d",)] and [(1, 2,), (3, 4,)]
        #
        # The sort is to ensure that we don't rely on dictionary iteration
        # order.
        keys, vals = zip(*[
            zip(
                *(sorted(i.items(), key=lambda kv: kv[0]))
            )
            for i in values
            if i
        ])

        for k in keys:
            if k != keys[0]:
                raise RuntimeError(
                    "All items must have the same keys"
                )

        sql = "INSERT INTO %s (%s) VALUES(%s)" % (
            table,
            ", ".join(k for k in keys[0]),
            ", ".join("?" for _ in keys[0])
        )

        txn.executemany(sql, vals)

    def _simple_upsert(self, table, keyvalues, values,
                       insertion_values={}, desc="_simple_upsert", lock=True):
        """
        Args:
            table (str): The table to upsert into
            keyvalues (dict): The unique key tables and their new values
            values (dict): The nonunique columns and their new values
            insertion_values (dict): key/values to use when inserting
        Returns: A deferred
        """
        return self.runInteraction(
            desc,
            self._simple_upsert_txn, table, keyvalues, values, insertion_values,
            lock
        )

    def _simple_upsert_txn(self, txn, table, keyvalues, values, insertion_values={},
                           lock=True):
        # We need to lock the table :(, unless we're *really* careful
        if lock:
            self.database_engine.lock_table(txn, table)

        # Try to update
        sql = "UPDATE %s SET %s WHERE %s" % (
            table,
            ", ".join("%s = ?" % (k,) for k in values),
            " AND ".join("%s = ?" % (k,) for k in keyvalues)
        )
        sqlargs = values.values() + keyvalues.values()
        logger.debug(
            "[SQL] %s Args=%s",
            sql, sqlargs,
        )

        txn.execute(sql, sqlargs)
        if txn.rowcount == 0:
            # We didn't update and rows so insert a new one
            allvalues = {}
            allvalues.update(keyvalues)
            allvalues.update(values)
            allvalues.update(insertion_values)

            sql = "INSERT INTO %s (%s) VALUES (%s)" % (
                table,
                ", ".join(k for k in allvalues),
                ", ".join("?" for _ in allvalues)
            )
            logger.debug(
                "[SQL] %s Args=%s",
                sql, keyvalues.values(),
            )
            txn.execute(sql, allvalues.values())

    def _simple_select_one(self, table, keyvalues, retcols,
                           allow_none=False, desc="_simple_select_one"):
        """Executes a SELECT query on the named table, which is expected to
        return a single row, returning a single column from it.

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the row with
            retcols : list of strings giving the names of the columns to return

            allow_none : If true, return None instead of failing if the SELECT
              statement returns no rows
        """
        return self.runInteraction(
            desc,
            self._simple_select_one_txn,
            table, keyvalues, retcols, allow_none,
        )

    def _simple_select_one_onecol(self, table, keyvalues, retcol,
                                  allow_none=False,
                                  desc="_simple_select_one_onecol"):
        """Executes a SELECT query on the named table, which is expected to
        return a single row, returning a single column from it."

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the row with
            retcol : string giving the name of the column to return
        """
        return self.runInteraction(
            desc,
            self._simple_select_one_onecol_txn,
            table, keyvalues, retcol, allow_none=allow_none,
        )

    def _simple_select_one_onecol_txn(self, txn, table, keyvalues, retcol,
                                      allow_none=False):
        ret = self._simple_select_onecol_txn(
            txn,
            table=table,
            keyvalues=keyvalues,
            retcol=retcol,
        )

        if ret:
            return ret[0]
        else:
            if allow_none:
                return None
            else:
                raise StoreError(404, "No row found")

    def _simple_select_onecol_txn(self, txn, table, keyvalues, retcol):
        sql = (
            "SELECT %(retcol)s FROM %(table)s WHERE %(where)s"
        ) % {
            "retcol": retcol,
            "table": table,
            "where": " AND ".join("%s = ?" % k for k in keyvalues.keys()),
        }

        txn.execute(sql, keyvalues.values())

        return [r[0] for r in txn.fetchall()]

    def _simple_select_onecol(self, table, keyvalues, retcol,
                              desc="_simple_select_onecol"):
        """Executes a SELECT query on the named table, which returns a list
        comprising of the values of the named column from the selected rows.

        Args:
            table (str): table name
            keyvalues (dict): column names and values to select the rows with
            retcol (str): column whos value we wish to retrieve.

        Returns:
            Deferred: Results in a list
        """
        return self.runInteraction(
            desc,
            self._simple_select_onecol_txn,
            table, keyvalues, retcol
        )

    def _simple_select_list(self, table, keyvalues, retcols,
                            desc="_simple_select_list"):
        """Executes a SELECT query on the named table, which may return zero or
        more rows, returning the result as a list of dicts.

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the rows with,
            or None to not apply a WHERE clause.
            retcols : list of strings giving the names of the columns to return
        """
        return self.runInteraction(
            desc,
            self._simple_select_list_txn,
            table, keyvalues, retcols
        )

    def _simple_select_list_txn(self, txn, table, keyvalues, retcols):
        """Executes a SELECT query on the named table, which may return zero or
        more rows, returning the result as a list of dicts.

        Args:
            txn : Transaction object
            table : string giving the table name
            keyvalues : dict of column names and values to select the rows with
            retcols : list of strings giving the names of the columns to return
        """
        if keyvalues:
            sql = "SELECT %s FROM %s WHERE %s" % (
                ", ".join(retcols),
                table,
                " AND ".join("%s = ?" % (k, ) for k in keyvalues)
            )
            txn.execute(sql, keyvalues.values())
        else:
            sql = "SELECT %s FROM %s" % (
                ", ".join(retcols),
                table
            )
            txn.execute(sql)

        return self.cursor_to_dict(txn)

    def _simple_update_one(self, table, keyvalues, updatevalues,
                           desc="_simple_update_one"):
        """Executes an UPDATE query on the named table, setting new values for
        columns in a row matching the key values.

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the row with
            updatevalues : dict giving column names and values to update
            retcols : optional list of column names to return

        If present, retcols gives a list of column names on which to perform
        a SELECT statement *before* performing the UPDATE statement. The values
        of these will be returned in a dict.

        These are performed within the same transaction, allowing an atomic
        get-and-set.  This can be used to implement compare-and-set by putting
        the update column in the 'keyvalues' dict as well.
        """
        return self.runInteraction(
            desc,
            self._simple_update_one_txn,
            table, keyvalues, updatevalues,
        )

    def _simple_update_one_txn(self, txn, table, keyvalues, updatevalues):
        update_sql = "UPDATE %s SET %s WHERE %s" % (
            table,
            ", ".join("%s = ?" % (k,) for k in updatevalues),
            " AND ".join("%s = ?" % (k,) for k in keyvalues)
        )

        txn.execute(
            update_sql,
            updatevalues.values() + keyvalues.values()
        )

        if txn.rowcount == 0:
            raise StoreError(404, "No row found")
        if txn.rowcount > 1:
            raise StoreError(500, "More than one row matched")

    def _simple_select_one_txn(self, txn, table, keyvalues, retcols,
                               allow_none=False):
        select_sql = "SELECT %s FROM %s WHERE %s" % (
            ", ".join(retcols),
            table,
            " AND ".join("%s = ?" % (k,) for k in keyvalues)
        )

        txn.execute(select_sql, keyvalues.values())

        row = txn.fetchone()
        if not row:
            if allow_none:
                return None
            raise StoreError(404, "No row found")
        if txn.rowcount > 1:
            raise StoreError(500, "More than one row matched")

        return dict(zip(retcols, row))

    def _simple_selectupdate_one(self, table, keyvalues, updatevalues=None,
                                 retcols=None, allow_none=False,
                                 desc="_simple_selectupdate_one"):
        """ Combined SELECT then UPDATE."""
        def func(txn):
            ret = None
            if retcols:
                ret = self._simple_select_one_txn(
                    txn,
                    table=table,
                    keyvalues=keyvalues,
                    retcols=retcols,
                    allow_none=allow_none,
                )

            if updatevalues:
                self._simple_update_one_txn(
                    txn,
                    table=table,
                    keyvalues=keyvalues,
                    updatevalues=updatevalues,
                )

                # if txn.rowcount == 0:
                #     raise StoreError(404, "No row found")
                if txn.rowcount > 1:
                    raise StoreError(500, "More than one row matched")

            return ret
        return self.runInteraction(desc, func)

    def _simple_delete_one(self, table, keyvalues, desc="_simple_delete_one"):
        """Executes a DELETE query on the named table, expecting to delete a
        single row.

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the row with
        """
        sql = "DELETE FROM %s WHERE %s" % (
            table,
            " AND ".join("%s = ?" % (k, ) for k in keyvalues)
        )

        def func(txn):
            txn.execute(sql, keyvalues.values())
            if txn.rowcount == 0:
                raise StoreError(404, "No row found")
            if txn.rowcount > 1:
                raise StoreError(500, "more than one row matched")
        return self.runInteraction(desc, func)

    def _simple_delete(self, table, keyvalues, desc="_simple_delete"):
        """Executes a DELETE query on the named table.

        Args:
            table : string giving the table name
            keyvalues : dict of column names and values to select the row with
        """

        return self.runInteraction(desc, self._simple_delete_txn)

    def _simple_delete_txn(self, txn, table, keyvalues):
        sql = "DELETE FROM %s WHERE %s" % (
            table,
            " AND ".join("%s = ?" % (k, ) for k in keyvalues)
        )

        return txn.execute(sql, keyvalues.values())

    def _simple_max_id(self, table):
        """Executes a SELECT query on the named table, expecting to return the
        max value for the column "id".

        Args:
            table : string giving the table name
        """
        sql = "SELECT MAX(id) AS id FROM %s" % table

        def func(txn):
            txn.execute(sql)
            max_id = self.cursor_to_dict(txn)[0]["id"]
            if max_id is None:
                return 0
            return max_id

        return self.runInteraction("_simple_max_id", func)

    def get_next_stream_id(self):
        with self._next_stream_id_lock:
            i = self._next_stream_id
            self._next_stream_id += 1
            return i


class _RollbackButIsFineException(Exception):
    """ This exception is used to rollback a transaction without implying
    something went wrong.
    """
    pass


class Table(object):
    """ A base class used to store information about a particular table.
    """

    table_name = None
    """ str: The name of the table """

    fields = None
    """ list: The field names """

    EntryType = None
    """ Type: A tuple type used to decode the results """

    _select_where_clause = "SELECT %s FROM %s WHERE %s"
    _select_clause = "SELECT %s FROM %s"
    _insert_clause = "REPLACE INTO %s (%s) VALUES (%s)"

    @classmethod
    def select_statement(cls, where_clause=None):
        """
        Args:
            where_clause (str): The WHERE clause to use.

        Returns:
            str: An SQL statement to select rows from the table with the given
            WHERE clause.
        """
        if where_clause:
            return cls._select_where_clause % (
                ", ".join(cls.fields),
                cls.table_name,
                where_clause
            )
        else:
            return cls._select_clause % (
                ", ".join(cls.fields),
                cls.table_name,
            )

    @classmethod
    def insert_statement(cls):
        return cls._insert_clause % (
            cls.table_name,
            ", ".join(cls.fields),
            ", ".join(["?"] * len(cls.fields)),
        )

    @classmethod
    def decode_single_result(cls, results):
        """ Given an iterable of tuples, return a single instance of
            `EntryType` or None if the iterable is empty
        Args:
            results (list): The results list to convert to `EntryType`
        Returns:
            EntryType: An instance of `EntryType`
        """
        results = list(results)
        if results:
            return cls.EntryType(*results[0])
        else:
            return None

    @classmethod
    def decode_results(cls, results):
        """ Given an iterable of tuples, return a list of `EntryType`
        Args:
            results (list): The results list to convert to `EntryType`

        Returns:
            list: A list of `EntryType`
        """
        return [cls.EntryType(*row) for row in results]

    @classmethod
    def get_fields_string(cls, prefix=None):
        if prefix:
            to_join = ("%s.%s" % (prefix, f) for f in cls.fields)
        else:
            to_join = cls.fields

        return ", ".join(to_join)


class JoinHelper(object):
    """ Used to help do joins on tables by looking at the tables' fields and
    creating a list of unique fields to use with SELECTs and a namedtuple
    to dump the results into.

    Attributes:
        tables (list): List of `Table` classes
        EntryType (type)
    """

    def __init__(self, *tables):
        self.tables = tables

        res = []
        for table in self.tables:
            res += [f for f in table.fields if f not in res]

        self.EntryType = namedtuple("JoinHelperEntry", res)

    def get_fields(self, **prefixes):
        """Get a string representing a list of fields for use in SELECT
        statements with the given prefixes applied to each.

        For example::

            JoinHelper(PdusTable, StateTable).get_fields(
                PdusTable="pdus",
                StateTable="state"
            )
        """
        res = []
        for field in self.EntryType._fields:
            for table in self.tables:
                if field in table.fields:
                    res.append("%s.%s" % (prefixes[table.__name__], field))
                    break

        return ", ".join(res)

    def decode_results(self, rows):
        return [self.EntryType(*row) for row in rows]
