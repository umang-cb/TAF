'''
@author: Umang

Assumptions for test -
1) At max 2 cluster are present.
2) KV, N1QL and Index services should be present on same node.
3) While rebalanc-in, rebalance in-out or swap rebalance KV node, 
the incoming node should have KV, N1QL and Index servies.
4) There are no other services present on node with cbas service.
5) Initial setup (This can change based on parameter passed)-
Local cluster -
1 node - KV, N1QL and Index
1 node - KV, and Index
2 nodes - cbas
Remote cluster -
1 node - KV, N1QL and Index
1 node - KV, and Index
'''

import threading
import time
from math import ceil
import random

from com.couchbase.client.java import *
from com.couchbase.client.java.json import *
from com.couchbase.client.java.query import *

from collections_helper.collections_spec_constants import MetaConstants, MetaCrudParams
from membase.api.rest_client import RestConnection
from TestInput import TestInputSingleton
from BucketLib.BucketOperations import BucketHelper
from remote.remote_util import RemoteMachineShellConnection
from error_simulation.cb_error import CouchbaseError

from sdk_exceptions import SDKException

from basetestcase import BaseTestCase
from cluster_utils.cluster_ready_functions import ClusterUtils
from bucket_utils.bucket_ready_functions import BucketUtils
from testconstants import FTS_QUOTA, CBAS_QUOTA, INDEX_QUOTA, MIN_KV_QUOTA
from cbas_utils.cbas_utils_v2 import CbasUtil


class volume(BaseTestCase):
    # will add the __init__ functions after the test has been stabilised
    def setUp(self):
        self.input = TestInputSingleton.input
        self.input.test_params.update({"default_bucket": False})
        super(volume, self).setUp()
        
        self.iterations = self.input.param("iterations", 2)
        self.vbucket_check = self.input.param("vbucket_check", True)
        
        self.bucket_spec = self.input.param("bucket_spec", "volume_templates.buckets_for_volume_test")
        self.data_load_spec = self.input.param("data_load_spec", "volume_test_load_for_volume_test")
        self.cbas_spec = self.input.param("cbas_spec", "volume")
        
        self.contains_ephemeral = self.input.param("contains_ephemeral", True)

        # the stage at which CRUD for collection level/ document level take place.
        # "before" - start and finish before rebalance/failover starts at each step
        # "during" - during rebalance/failover at each step
        self.data_load_stage = self.input.param("data_load_stage", "during")

        self.doc_and_collection_ttl = self.input.param("doc_and_collection_ttl", False)  # For using doc_ttl + coll_ttl
        self.skip_validations = self.input.param("skip_validations", True)

        # Services to be added on rebalance-in nodes during the volume test
        self.services_for_rebalance_in = self.input.param("services_for_rebalance_in", None)
        
        # Assuming that only 2 clusters are used
        self.local_cluster = None
        self.remote_cluster = None
        self.available_servers = self.servers[:]
        self.exclude_nodes = list()
        self.number_of_indexes = self.input.param("number_of_indexes", 0)
        
        # Adding nodes in clusters, creating indexes, loading data in collections and creating cbas infra
        for cluster in self.get_clusters():
            cluster.cluster_util = ClusterUtils(cluster, self.task_manager)
            cluster.bucket_util = BucketUtils(cluster,
                                              cluster.cluster_util,
                                              self.task)
            cluster.rest = RestConnection(cluster.master)
            cluster.cbas_nodes = list()
            
            for node in self.available_servers:
                if node.ip == cluster.master.ip:
                    self.available_servers.remove(node)
                    break
            self.exclude_nodes.append(cluster.master)
                    
            def get_init_services(services_to_use):
                services = list()
                for service in services_to_use.split("-"):
                    services.append(service.replace(":", ","))
                return services[1:] if len(services) > 1 else None
            
            if not self.local_cluster:
                self.local_cluster = cluster
                if self.services_init:
                    services_to_use = get_init_services(self.services_init)
                else:
                    services_to_use = get_init_services("kv-cbas")
                init_nodes = self.nodes_init
            else:
                self.remote_cluster = cluster
                services_to_use = get_init_services(self.input.param("remote_init_services", "kv:n1ql:index-kv:index"))
                init_nodes = self.input.param("remote_init_nodes", 1)
            
            # reduce init node by 1 as 1 node will be used while initializing cluster
            init_nodes -= 1
            
            for i in range(0, init_nodes):
                node_to_initialize = self.available_servers.pop(-1)
                services = services_to_use.pop(0)
                services = services.split(",")
                
                node_rest = RestConnection(node_to_initialize)
                info = node_rest.get_nodes_self()
                total_free_memory = int((info.memoryFree // 1024 ** 2))
                
                if "kv" in services:
                    self.set_memory_quota(cluster, False, total_free_memory)
                if "cbas" in services:
                    self.set_memory_quota(cluster, True, total_free_memory)
                    cluster.cbas_nodes.append(node_to_initialize)
                
                cluster.cluster_util.add_node(node_to_initialize,services)
                cluster.servers.append(node_to_initialize)
            
            self.collectionSetUp(cluster, cluster.bucket_util, cluster.cluster_util)
            cluster.bucket_util._expiry_pager(val=5)
            
            # Initialize parameters for index querying
            cluster.n1ql_nodes = None
            cluster.flush_buckets_before_indexes_creation = False
            if self.number_of_indexes > 0:
                cluster.flush_buckets_before_indexes_creation = self.input.param(
                    "flush_buckets_before_indexes_creation", True)
                if cluster.flush_buckets_before_indexes_creation:
                    cluster.bucket_util.flush_all_buckets(cluster.master, skip_resetting_num_items=True)
                self.set_memory_quota(cluster)
                cluster.n1ql_nodes = cluster.cluster_util.get_nodes_from_services_map(
                    service_type="n1ql", get_all_nodes=True, servers=cluster.servers,
                    master=cluster.master)
                cluster.n1ql_rest_connections = list()
                for n1ql_node in cluster.n1ql_nodes:
                    cluster.n1ql_rest_connections.append(RestConnection(n1ql_node))
                    if n1ql_node not in self.exclude_nodes:
                        self.exclude_nodes.append(n1ql_node)
                cluster.n1ql_turn_counter = 0  # To distribute the turn of using n1ql nodes for query. Start with first node
                indexes_to_build = self.create_indexes_and_initialize_queries(cluster)
                self.build_deferred_indexes(cluster, indexes_to_build)
            cluster.query_thread_flag = False
            cluster.query_thread = None
        
        self.local_cluster.cbas_util = CbasUtil(self.local_cluster.master, self.local_cluster.cbas_nodes[0])
        self.exclude_nodes.append(self.local_cluster.cbas_nodes[0])
        cbas_spec = self.local_cluster.cbas_util.get_cbas_spec(self.cbas_spec)
        
        if self.remote_cluster:
            link_properties = list()
            for server in self.remote_cluster.servers:
                for encryption in ['none', 'half']:
                    link_properties.append(
                        {"type" : "couchbase", "hostname" : server.ip, 
                         "username" : server.rest_username, "password" : server.rest_password, 
                         "encryption":encryption})
            cbas_spec["link"]["properties"] = link_properties
            if not self.local_cluster.cbas_util.create_cbas_infra_from_spec(
                cbas_spec, self.local_cluster.bucket_util, self.remote_cluster.bucket_util):
                self.fail("Error while creating infra from CBAS spec")
        else:
            if not self.local_cluster.cbas_util.create_cbas_infra_from_spec(
                cbas_spec, self.local_cluster.bucket_util):
                self.fail("Error while creating infra from CBAS spec")

    def tearDown(self):
        # Do not call the base class's teardown, as we want to keep the cluster intact after the volume run
        # Do not call the base class's teardown, as we want to keep the cluster intact after the volume run
        for cluster in self.get_clusters():
            if cluster.query_thread:
                cluster.query_thread_flag = False
                cluster.query_thread.join()
                cluster.query_thread = None
            self.log.info("Printing bucket stats before teardown")
            cluster.bucket_util.print_bucket_stats()
        if self.collect_pcaps:
            self.start_fetch_pcaps()
        result = self.check_coredump_exist(self.servers, force_collect=True)
        if not self.crash_warning:
            self.assertFalse(result, msg="Cb_log file validation failed")
        if self.crash_warning and result:
            self.log.warn("CRASH | CRITICAL | WARN messages found in cb_logs")
    
    def set_memory_quota(self, cluster, cbas=False, memory=0):
        """
        To set memory quota of KV and index services before starting step 5 of volume test
        """
        if cbas:
            cluster.rest.set_service_memoryQuota(service="cbasMemoryQuota", memoryQuota=memory)
        else:
            info = cluster.rest.get_nodes_self()
            index_quota = 5000
            kv_quota = info.mcdMemoryAllocated - index_quota
            while kv_quota < info.memoryQuota:
                index_quota /= 2
                kv_quota = info.mcdMemoryAllocated - index_quota
            cluster.rest.set_service_memoryQuota(service="memoryQuota", memoryQuota=kv_quota)
            cluster.rest.set_service_memoryQuota(service="indexMemoryQuota", memoryQuota=index_quota)
    
    # This code will be removed once cbas_base is refactored
    def collectionSetUp(self, cluster, bucket_util, cluster_util):
        """
        Setup the buckets, scopes and collecitons based on the spec passed.
        """
        self.over_ride_spec_params = self.input.param(
            "override_spec_params", "").split(";")
        self.remove_default_collection = self.input.param(
            "remove_default_collection", False)

        # Create bucket(s) and add rbac user
        bucket_util.add_rbac_user()
        buckets_spec = bucket_util.get_bucket_template_from_package(
            self.bucket_spec)
        doc_loading_spec = \
            bucket_util.get_crud_template_from_package(self.data_load_spec)

        # Process params to over_ride values if required
        self.over_ride_bucket_template_params(buckets_spec)
        self.over_ride_doc_loading_template_params(doc_loading_spec)

        # MB-38438, adding CollectionNotFoundException in retry exception
        doc_loading_spec[MetaCrudParams.RETRY_EXCEPTIONS].append(
            SDKException.CollectionNotFoundException)

        bucket_util.create_buckets_using_json_data(buckets_spec)
        bucket_util.wait_for_collection_creation_to_complete()

        # Prints bucket stats before doc_ops
        bucket_util.print_bucket_stats()

        # Init sdk_client_pool if not initialized before
        if self.sdk_client_pool is None:
            self.init_sdk_pool_object()

        # Create clients in SDK client pool
        if self.sdk_client_pool:
            self.log.info("Creating required SDK clients for client_pool")
            bucket_count = len(bucket_util.buckets)
            max_clients = self.task_manager.number_of_threads
            clients_per_bucket = int(ceil(max_clients / bucket_count))
            for bucket in bucket_util.buckets:
                self.sdk_client_pool.create_clients(
                    bucket,
                    [cluster.master],
                    clients_per_bucket,
                    compression_settings=self.sdk_compression)

        # TODO: remove this once the bug is fixed
        # self.sleep(120, "MB-38497")
        self.sleep(10, "MB-38497")

        doc_loading_task = \
            bucket_util.run_scenario_from_spec(
                self.task,
                cluster,
                bucket_util.buckets,
                doc_loading_spec,
                mutation_num=0,
                batch_size=self.batch_size)
        if doc_loading_task.result is False:
            self.fail("Initial doc_loading failed")

        cluster_util.print_cluster_stats()

        ttl_buckets = [
            "multi_bucket.buckets_for_rebalance_tests_with_ttl",
            "multi_bucket.buckets_all_membase_for_rebalance_tests_with_ttl",
            "multi_bucket.buckets_for_volume_tests_with_ttl"]

        # Verify initial doc load count
        bucket_util._wait_for_stats_all_buckets()
        if self.bucket_spec not in ttl_buckets:
            bucket_util.validate_docs_per_collections_all_buckets()

        # Prints bucket stats after doc_ops
        bucket_util.print_bucket_stats()
    
    # This code will be removed once cbas_base is refactored
    def over_ride_bucket_template_params(self, bucket_spec):
        for over_ride_param in self.over_ride_spec_params:
            if over_ride_param == "replicas":
                bucket_spec[Bucket.replicaNumber] = self.num_replicas
            elif over_ride_param == "bucket_size":
                bucket_spec[Bucket.ramQuotaMB] = self.bucket_size
            elif over_ride_param == "num_items":
                bucket_spec[MetaConstants.NUM_ITEMS_PER_COLLECTION] = \
                    self.num_items
            elif over_ride_param == "remove_default_collection":
                bucket_spec[MetaConstants.REMOVE_DEFAULT_COLLECTION] = \
                    self.remove_default_collection
            elif over_ride_param == "enable_flush":
                if self.input.param("enable_flush", False):
                    bucket_spec[Bucket.flushEnabled] = Bucket.FlushBucket.ENABLED
                else:
                    bucket_spec[Bucket.flushEnabled] = Bucket.FlushBucket.DISABLED
            elif over_ride_param == "num_collections":
                bucket_spec[MetaConstants.NUM_COLLECTIONS_PER_SCOPE] = int(
                    self.input.param("num_collections", 1))
    
    # This code will be removed once cbas_base is refactored
    def over_ride_doc_loading_template_params(self, target_spec):
        for over_ride_param in self.over_ride_spec_params:
            if over_ride_param == "durability":
                target_spec[MetaCrudParams.DURABILITY_LEVEL] = \
                    self.durability_level
            elif over_ride_param == "sdk_timeout":
                target_spec[MetaCrudParams.SDK_TIMEOUT] = self.sdk_timeout
            elif over_ride_param == "doc_size":
                target_spec[MetaCrudParams.DocCrud.DOC_SIZE] = self.doc_size

    def run_cbq_query(self, cluster, query):
        """
        To run cbq queries
        Note: Do not run this in parallel
        """
        result = cluster.n1ql_rest_connections[cluster.n1ql_turn_counter].query_tool(query, timeout=1300)
        cluster.n1ql_turn_counter = (cluster.n1ql_turn_counter + 1) % len(cluster.n1ql_nodes)
        return result

    def wait_for_indexes_to_go_online(self, cluster, gsi_index_names, timeout=300):
        """
        Wait for indexes to go online after building the deferred indexes
        """
        self.log.info("Waiting for indexes to go online")
        start_time = time.time()
        stop_time = start_time + timeout
        for gsi_index_name in gsi_index_names:
            while True:
                check_state_query = "SELECT state FROM system:indexes WHERE name='%s'" % gsi_index_name
                result = self.run_cbq_query(cluster, check_state_query)
                if result['results'][0]['state'] == "online":
                    break
                if time.time() > stop_time:
                    self.fail("Index availability timeout of index: {0}".format(gsi_index_name))

    def build_deferred_indexes(self, cluster, indexes_to_build):
        """
        Build secondary indexes that were deferred
        """
        self.log.info("Building indexes")
        for bucket, bucket_data in indexes_to_build.items():
            for scope, collection_data in bucket_data.items():
                for collection, gsi_index_names in collection_data.items():
                    build_query = "BUILD INDEX on `%s`.`%s`.`%s`(`%s`) " \
                                  "USING GSI" \
                                  % (bucket, scope, collection, ",".join(gsi_index_names))
                    result = self.run_cbq_query(cluster, build_query)
                    self.assertTrue(result['status'] == "success", "Build query %s failed." % build_query)
                    self.wait_for_indexes_to_go_online(cluster, gsi_index_names)

        query = "select state from system:indexes where state='deferred'"
        result = self.run_cbq_query(cluster, query)
        self.log.info("deferred indexes remaining: {0}".format(len(result['results'])))
        query = "select state from system:indexes where state='online'"
        result = self.run_cbq_query(cluster, query)
        self.log.info("online indexes count: {0}".format(len(result['results'])))
        self.sleep(600, "Wait after building indexes")

    def create_indexes_and_initialize_queries(self, cluster):
        """
        Create gsi indexes on collections - according to number_of_indexes, and
        Initialize select queries on collections that will be run later
        """
        self.log.info("Creating indexes with defer build")
        cluster.select_queries = list()
        indexes_to_build = dict()
        count = 0
        # ToDO create indexes on ephemeral buckets too using MOI storage
        couchbase_buckets = [bucket for bucket in cluster.bucket_util.buckets if bucket.bucketType == "couchbase"]
        for bucket in couchbase_buckets:
            indexes_to_build[bucket.name] = dict()
            for _, scope in bucket.scopes.items():
                indexes_to_build[bucket.name][scope.name] = dict()
                for _, collection in scope.collections.items():
                    gsi_index_name = "gsi-" + str(count)
                    create_index_query = "CREATE INDEX `%s` " \
                                         "ON `%s`.`%s`.`%s`(`age`)" \
                                         "WITH { 'defer_build': true, 'num_replica': 2 }" \
                                         % (gsi_index_name, bucket.name, scope.name, collection.name)
                    result = self.run_cbq_query(cluster, create_index_query)
                    # self.assertTrue(result['status'] == "success", "Defer build Query %s failed." % create_index_query)

                    if collection.name not in indexes_to_build[bucket.name][scope.name]:
                        indexes_to_build[bucket.name][scope.name][collection.name] = list()
                    indexes_to_build[bucket.name][scope.name][collection.name].append(gsi_index_name)

                    select_query = "select age from `%s`.`%s`.`%s` where age > 1 limit 1" % (
                        bucket.name, scope.name, collection.name)
                    cluster.select_queries.append(select_query)

                    count = count + 1
                    if count >= self.number_of_indexes:
                        return indexes_to_build
        return indexes_to_build

    def run_select_query(self, cluster):
        """
        Runs select queries in a loop in a separate thread until the thread is asked for to join
        """
        self.log.info("Starting select queries: {0}".format(cluster.query_thread_flag))
        while cluster.query_thread_flag:
            for select_query in cluster.select_queries:
                result = self.run_cbq_query(cluster, select_query)
                if result['status'] != "success":
                    self.log.warn("Query failed: {0}".format(select_query))
                time.sleep(3)
        self.log.info("Stopping select queries")

    # Stopping and restarting the memcached process
    def stop_process(self):
        target_node = self.servers[2]
        remote = RemoteMachineShellConnection(target_node)
        error_sim = CouchbaseError(self.log, remote)
        error_to_simulate = "stop_memcached"
        # Induce the error condition
        error_sim.create(error_to_simulate)
        self.sleep(20, "Wait before reverting the error condition")
        # Revert the simulated error condition and close the ssh session
        error_sim.revert(error_to_simulate)
        remote.disconnect()

    def rebalance(self, nodes_in=0, nodes_out=0):
        servs_in = random.sample(self.available_servers, nodes_in)

        self.nodes_cluster = self.cluster.nodes_in_cluster[:]
        for node in self.exclude_nodes:
            self.nodes_cluster.remove(node)
        servs_out = random.sample(self.nodes_cluster, nodes_out)

        if nodes_in == nodes_out:
            self.vbucket_check = False

        services = None
        if self.services_for_rebalance_in and nodes_in > 0:
            services = list()
            services.append(self.services_for_rebalance_in.replace(":", ","))
            services = services * nodes_in
            services = services * nodes_in

        rebalance_task = self.task.async_rebalance(
            self.cluster.servers[:self.nodes_init], servs_in, servs_out, check_vbucket_shuffling=self.vbucket_check,
            retry_get_process_num=200, services=services)

        self.available_servers = [servs for servs in self.available_servers if servs not in servs_in]
        self.available_servers += servs_out

        self.cluster.nodes_in_cluster.extend(servs_in)
        self.cluster.nodes_in_cluster = list(set(self.cluster.nodes_in_cluster) - set(servs_out))
        return rebalance_task

    def wait_for_rebalance_to_complete(self, task, wait_step=120):
        self.task.jython_task_manager.get_task_result(task)
        self.assertTrue(task.result, "Rebalance Failed")

    def set_retry_exceptions(self, doc_loading_spec):
        """
        Exceptions for which mutations need to be retried during
        topology changes
        """
        retry_exceptions = list()
        retry_exceptions.append(SDKException.AmbiguousTimeoutException)
        retry_exceptions.append(SDKException.TimeoutException)
        retry_exceptions.append(SDKException.RequestCanceledException)
        if self.durability_level:
            retry_exceptions.append(SDKException.DurabilityAmbiguousException)
            retry_exceptions.append(SDKException.DurabilityImpossibleException)
        doc_loading_spec[MetaCrudParams.RETRY_EXCEPTIONS] = retry_exceptions

    def data_load_collection(self, async_load=True, skip_read_success_results=True):
        doc_loading_spec = \
            self.bucket_util.get_crud_template_from_package(self.data_load_spec)
        self.set_retry_exceptions(doc_loading_spec)
        doc_loading_spec[MetaCrudParams.DURABILITY_LEVEL] = self.durability_level
        doc_loading_spec[MetaCrudParams.SKIP_READ_SUCCESS_RESULTS] = skip_read_success_results
        task = self.bucket_util.run_scenario_from_spec(self.task,
                                                       self.cluster,
                                                       self.bucket_util.buckets,
                                                       doc_loading_spec,
                                                       mutation_num=0,
                                                       async_load=async_load)
        return task

    def reload_data_into_buckets(self):
        """
        Initial data load happens in collections_base. But this method loads
        data again when buckets have been flushed during volume test
        """
        doc_loading_spec = \
            self.bucket_util.get_crud_template_from_package(
                self.data_spec_name)
        doc_loading_task = \
            self.bucket_util.run_scenario_from_spec(
                self.task,
                self.cluster,
                self.bucket_util.buckets,
                doc_loading_spec,
                mutation_num=0,
                batch_size=self.batch_size)
        if doc_loading_task.result is False:
            self.fail("Initial reloading failed")
        ttl_buckets = [
            "multi_bucket.buckets_for_rebalance_tests_with_ttl",
            "multi_bucket.buckets_all_membase_for_rebalance_tests_with_ttl",
            "volume_templates.buckets_for_volume_tests_with_ttl"]

        # Verify initial doc load count
        self.bucket_util._wait_for_stats_all_buckets()
        if self.spec_name not in ttl_buckets:
            self.bucket_util.validate_docs_per_collections_all_buckets()

        # Prints bucket stats after doc_ops
        self.bucket_util.print_bucket_stats()

    def wait_for_async_data_load_to_complete(self, task):
        self.task.jython_task_manager.get_task_result(task)
        if not self.skip_validations:
            self.bucket_util.validate_doc_loading_results(task)
            if task.result is False:
                self.fail("Doc_loading failed")

    def data_validation_collection(self):
        retry_count = 0
        while retry_count < 10:
            try:
                self.bucket_util._wait_for_stats_all_buckets()
            except:
                retry_count = retry_count + 1
                self.log.info("ep-queue hasn't drained yet. Retry count: {0}".format(retry_count))
            else:
                break
        if retry_count == 10:
            self.log.info("Attempting last retry for ep-queue to drain")
            self.bucket_util._wait_for_stats_all_buckets()
        if self.doc_and_collection_ttl:
            self.bucket_util._expiry_pager(val=5)
            self.sleep(400, "wait for doc/collection maxttl to finish")
            items = 0
            self.bucket_util._wait_for_stats_all_buckets()
            for bucket in self.bucket_util.buckets:
                items = items + self.bucket_helper_obj.get_active_key_count(bucket)
            if items != 0:
                self.fail("doc count!=0, TTL + rebalance failed")
        else:
            if not self.skip_validations:
                self.bucket_util.validate_docs_per_collections_all_buckets()
            else:
                pass

    def wait_for_failover_or_assert(self, expected_failover_count, timeout=7200):
        # Timeout is kept large for graceful failover
        time_start = time.time()
        time_max_end = time_start + timeout
        actual_failover_count = 0
        while time.time() < time_max_end:
            actual_failover_count = self.get_failover_count()
            if actual_failover_count == expected_failover_count:
                break
            time.sleep(20)
        time_end = time.time()
        if actual_failover_count != expected_failover_count:
            self.log.info(self.rest.print_UI_logs())
        self.assertTrue(actual_failover_count == expected_failover_count,
                        "{0} nodes failed over, expected : {1}"
                        .format(actual_failover_count,
                                expected_failover_count))
        self.log.info("{0} nodes failed over as expected in {1} seconds"
                      .format(actual_failover_count, time_end - time_start))

    def get_failover_count(self):
        rest = RestConnection(self.cluster.master)
        cluster_status = rest.cluster_status()
        failover_count = 0
        # check for inactiveFailed
        for node in cluster_status['nodes']:
            if node['clusterMembership'] == "inactiveFailed":
                failover_count += 1
        return failover_count

    def test_volume_taf(self):
        self.loop = 0
        # self.cluster_utils.set_metadata_purge_interval()
        if self.number_of_indexes > 0:
            self.query_thread = threading.Thread(target=self.run_select_query)
            self.query_thread_flag = True
            self.query_thread.start()
        self.log.info("Finished steps 1-4 successfully in setup")
        while self.loop < self.iterations:
            if self.loop > 0 or self.flush_buckets_before_indexes_creation:
                self.log.info("Reloading items to buckets")
                self.reload_data_into_buckets()
            #########################################################################################################################
            self.log.info("Step 5: Rebalance in with Loading of docs")
            if self.data_load_stage == "before":
                task = self.data_load_collection(async_load=False)
                if task.result is False:
                    self.fail("Doc loading failed")
            rebalance_task = self.rebalance(nodes_in=1, nodes_out=0)
            if self.data_load_stage == "during":
                task = self.data_load_collection()
            self.wait_for_rebalance_to_complete(rebalance_task)
            if self.data_load_stage == "during":
                self.wait_for_async_data_load_to_complete(task)
            self.data_validation_collection()
            self.bucket_util.print_bucket_stats()
            #########################################################################################################################
            self.log.info("Step 6: Rebalance Out with Loading of docs")
            if self.data_load_stage == "before":
                task = self.data_load_collection(async_load=False)
                if task.result is False:
                    self.fail("Doc loading failed")
            rebalance_task = self.rebalance(nodes_in=0, nodes_out=1)
            if self.data_load_stage == "during":
                task = self.data_load_collection()
            self.wait_for_rebalance_to_complete(rebalance_task)
            if self.data_load_stage == "during":
                self.wait_for_async_data_load_to_complete(task)
            self.data_validation_collection()
            self.bucket_util.print_bucket_stats()
            #######################################################################################################################
            self.log.info("Step 7: Rebalance In_Out with Loading of docs")
            if self.data_load_stage == "before":
                task = self.data_load_collection(async_load=False)
                if task.result is False:
                    self.fail("Doc loading failed")
            rebalance_task = self.rebalance(nodes_in=2, nodes_out=1)
            if self.data_load_stage == "during":
                task = self.data_load_collection()
            self.wait_for_rebalance_to_complete(rebalance_task)
            if self.data_load_stage == "during":
                self.wait_for_async_data_load_to_complete(task)
            self.data_validation_collection()
            self.bucket_util.print_bucket_stats()
            ########################################################################################################################
            self.log.info("Step 8: Swap with Loading of docs")
            if self.data_load_stage == "before":
                task = self.data_load_collection(async_load=False)
                if task.result is False:
                    self.fail("Doc loading failed")
            rebalance_task = self.rebalance(nodes_in=1, nodes_out=1)
            if self.data_load_stage == "during":
                task = self.data_load_collection()
            self.wait_for_rebalance_to_complete(rebalance_task)
            if self.data_load_stage == "during":
                self.wait_for_async_data_load_to_complete(task)
            self.data_validation_collection()
            self.tasks = []
            self.bucket_util.print_bucket_stats()
            ########################################################################################################################
            self.log.info("Step 9: Updating the bucket replica to 2")
            if self.data_load_stage == "before":
                task = self.data_load_collection(async_load=False)
                if task.result is False:
                    self.fail("Doc loading failed")
            bucket_helper = BucketHelper(self.cluster.master)
            for i in range(len(self.bucket_util.buckets)):
                bucket_helper.change_bucket_props(
                    self.bucket_util.buckets[i], replicaNumber=2)
            rebalance_task = self.rebalance(nodes_in=1, nodes_out=0)
            if self.data_load_stage == "during":
                task = self.data_load_collection()
            self.wait_for_rebalance_to_complete(rebalance_task)
            if self.data_load_stage == "during":
                self.wait_for_async_data_load_to_complete(task)
            self.data_validation_collection()
            self.bucket_util.print_bucket_stats()
            ########################################################################################################################
            if self.contains_ephemeral:
                self.log.info("No Memcached kill for ephemeral bucket")
            else:
                self.log.info("Step 10: Stopping and restarting memcached process")
                if self.data_load_stage == "before":
                    task = self.data_load_collection(async_load=False)
                    if task.result is False:
                        self.fail("Doc loading failed")
                rebalance_task = self.task.async_rebalance(self.cluster.servers, [], [], retry_get_process_num=200)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                self.stop_process()
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                self.bucket_util.print_bucket_stats()
            ########################################################################################################################
            step_count = 10
            for failover in ["Graceful", "Hard"]:
                for action in ["RebalanceOut", "FullRecovery", "DeltaRecovery"]:
                    step_count = step_count + 1
                    self.log.info(
                        "Step {0}: {1} Failover a node and {2} that node with data load in parallel".format(step_count,
                                                                                                            failover,
                                                                                                            action))
                    if self.data_load_stage == "before":
                        task = self.data_load_collection(async_load=False)
                        if task.result is False:
                            self.fail("Doc loading failed")

                    self.std_vbucket_dist = self.input.param("std_vbucket_dist", None)
                    std = self.std_vbucket_dist or 1.0

                    kv_nodes = self.cluster_util.get_kv_nodes()
                    self.log.info("Collecting pre_failover_stats. KV nodes are {0}".format(kv_nodes))
                    prev_failover_stats = self.bucket_util.get_failovers_logs(kv_nodes,
                                                                              self.bucket_util.buckets)
                    prev_vbucket_stats = self.bucket_util.get_vbucket_seqnos(kv_nodes,
                                                                             self.bucket_util.buckets)
                    self.sleep(10)

                    disk_replica_dataset, disk_active_dataset = self.bucket_util.get_and_compare_active_replica_data_set_all(
                        kv_nodes, self.bucket_util.buckets, path=None)

                    self.rest = RestConnection(self.cluster.master)
                    self.nodes = self.cluster_util.get_nodes(self.cluster.master)
                    self.chosen = self.cluster_util.pick_nodes(self.cluster.master, howmany=1,
                                                               exclude_nodes=self.exclude_nodes)

                    if self.data_load_stage == "during":
                        reset_flag = False
                        if (not self.durability_level) and failover == "Hard":
                            # Force a durability level to prevent data loss during hard failover
                            self.log.info("Forcing durability level: MAJORITY")
                            self.durability_level = "MAJORITY"
                            reset_flag = True
                        task = self.data_load_collection()
                        if reset_flag:
                            self.durability_level = ""

                    # Mark Node for failover
                    if failover == "Graceful":
                        self.success_failed_over = self.rest.fail_over(self.chosen[0].id, graceful=True)
                    else:
                        self.success_failed_over = self.rest.fail_over(self.chosen[0].id, graceful=False)

                    self.sleep(300)
                    self.wait_for_failover_or_assert(1)

                    # Perform the action
                    if action == "RebalanceOut":
                        self.nodes = self.rest.node_statuses()
                        self.rest.rebalance(otpNodes=[node.id for node in self.nodes], ejectedNodes=[self.chosen[0].id])
                        # self.sleep(600)
                        self.assertTrue(self.rest.monitorRebalance(stop_if_loop=False), msg="Rebalance failed")
                        servs_out = [node for node in self.cluster.servers if node.ip == self.chosen[0].ip]
                        self.cluster.nodes_in_cluster = list(set(self.cluster.nodes_in_cluster) - set(servs_out))
                        self.available_servers += servs_out
                        self.sleep(10)
                    else:
                        if action == "FullRecovery":
                            if self.success_failed_over:
                                self.rest.set_recovery_type(otpNode=self.chosen[0].id, recoveryType="full")
                        elif action == "DeltaRecovery":
                            if self.success_failed_over:
                                self.rest.set_recovery_type(otpNode=self.chosen[0].id, recoveryType="delta")

                        rebalance_task = self.task.async_rebalance(
                            self.cluster.servers[:self.nodes_init], [], [], retry_get_process_num=200)
                        self.wait_for_rebalance_to_complete(rebalance_task)
                        self.sleep(10)

                    if self.data_load_stage == "during":
                        self.wait_for_async_data_load_to_complete(task)
                    self.data_validation_collection()

                    kv_nodes = self.cluster_util.get_kv_nodes()
                    self.log.info("Collecting post_failover_stats. KV nodes are {0}".format(kv_nodes))
                    self.bucket_util.compare_failovers_logs(prev_failover_stats, kv_nodes,
                                                            self.bucket_util.buckets)
                    self.sleep(10)

                    self.bucket_util.data_analysis_active_replica_all(
                        disk_active_dataset, disk_replica_dataset,
                        kv_nodes,
                        self.bucket_util.buckets, path=None)
                    self.bucket_util.vb_distribution_analysis(
                        servers=kv_nodes, buckets=self.bucket_util.buckets,
                        num_replicas=2,
                        std=std, total_vbuckets=self.cluster_util.vbuckets)
                    self.sleep(10)
                    self.tasks = []
                    # Bring back the rebalance out node back to cluster for further steps
                    if action == "RebalanceOut":
                        self.sleep(120)
                        rebalance_task = self.rebalance(nodes_in=1, nodes_out=0)
                        # self.sleep(600)
                        self.wait_for_rebalance_to_complete(rebalance_task)
                    self.bucket_util.print_bucket_stats()
            ########################################################################################################################
            self.log.info("Step 17: Updating the bucket replica to 1")
            if self.data_load_stage == "before":
                task = self.data_load_collection(async_load=False)
                if task.result is False:
                    self.fail("Doc loading failed")
            bucket_helper = BucketHelper(self.cluster.master)
            for i in range(len(self.bucket_util.buckets)):
                bucket_helper.change_bucket_props(
                    self.bucket_util.buckets[i], replicaNumber=1)
            rebalance_task = self.task.async_rebalance(self.cluster.servers, [], [], retry_get_process_num=200)
            if self.data_load_stage == "during":
                task = self.data_load_collection()
            self.wait_for_rebalance_to_complete(rebalance_task)
            if self.data_load_stage == "during":
                self.wait_for_async_data_load_to_complete(task)
            self.data_validation_collection()
            self.tasks = []
            self.bucket_util.print_bucket_stats()
            ########################################################################################################################
            self.log.info("Step 18: Flush bucket(s) and start the entire process again")
            self.loop += 1
            if self.loop < self.iterations:
                # Flush buckets(s)
                self.bucket_util.flush_all_buckets(self.cluster.master, skip_resetting_num_items=True)
                self.sleep(10)
                if len(self.cluster.nodes_in_cluster) > self.nodes_init:
                    self.nodes_cluster = self.cluster.nodes_in_cluster[:]
                    self.nodes_cluster.remove(self.cluster.master)
                    servs_out = random.sample(self.nodes_cluster,
                                              int(len(self.cluster.nodes_in_cluster) - self.nodes_init))
                    rebalance_task = self.task.async_rebalance(
                        self.cluster.servers[:self.nodes_init], [], servs_out, retry_get_process_num=200)
                    self.wait_for_rebalance_to_complete(rebalance_task)
                    self.available_servers += servs_out
                    self.cluster.nodes_in_cluster = list(set(self.cluster.nodes_in_cluster) - set(servs_out))
            else:
                if self.number_of_indexes > 0:
                    self.query_thread_flag = False
                    self.query_thread.join()
                    self.query_thread = None
                self.log.info("Volume Test Run Complete")
        ############################################################################################################################
