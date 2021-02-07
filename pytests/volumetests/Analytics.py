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

import time
from math import ceil
import random

#from com.couchbase.client.java import *
#from com.couchbase.client.java.json import *
#from com.couchbase.client.java.query import *

from collections_helper.collections_spec_constants import MetaConstants, MetaCrudParams
from membase.api.rest_client import RestConnection
from TestInput import TestInputSingleton
from BucketLib.BucketOperations import BucketHelper
from BucketLib.bucket import Bucket
from remote.remote_util import RemoteMachineShellConnection
from error_simulation.cb_error import CouchbaseError

from sdk_exceptions import SDKException

from basetestcase import BaseTestCase
from cluster_utils.cluster_ready_functions import ClusterUtils
from bucket_utils.bucket_ready_functions import BucketUtils
from cbas_utils.cbas_utils_v2 import CbasUtil, CBASRebalanceUtil
import traceback
from java.lang import Exception as Java_base_exception


class volume(BaseTestCase):
    """
    Required test parameters
    :testparams test_type: (str) accepted value steady_state, rebalance and service_crash 
    :testparams iterations: (int) no. of times the test has to be repeated.
    :testparams nodes_init: (int) no. of nodes in local cluster including master.
    :testparams services_init: (str) services to be used while initializing the clusters. 
    The services passed here will be used for all the clusters. Ex. - kv:n1ql:index
    :testparams local_init_services: (str) services to be used while adding nodes to the local cluster. 
    Ex. - "kv:n1ql-cbas", this means the first node being added (excluding master) will have kv and n1ql service and
    second node being added will have cbas service.
    :testparams remote_init_nodes: (int) no. of nodes in remote cluster including master.
    :testparams remote_init_services: (str) services to be used while adding nodes to the remote cluster.
    For example see local_init_services.
    :testparams bucket_spec: (str) bucket spec to be used to create buckets, scopes and collections.
    :testparams data_load_spec: (str) data load spec to be used to load data
    :testparams cbas_spec: (str) cbas spec to be used to create CBAS infra
    :testparams vbucket_check: (boolean) to perform vbucket check or not
    :testparams contains_ephemeral: (boolean) whether ephemeral buckets need to be created or not
    :testparams data_load_stage: (str) accepted values - before or during
    :testparams doc_and_collection_ttl: (boolean)
    :testparams skip_validations: (boolean)
    :testparams run_parallel_cbas_query: (boolean) start running cbas queries on a seperate thread in parallel
    :testparams run_parallel_kv_query: (boolean) start running KV queries on a seperate thread in parallel
    
    Test Scaling parameters
    :testparams override_spec_params: (str) ';' seperated bucket_spec and data_load_spec properties to be updated.
    :testparams remove_default_collection: (boolean) remove default collections from buckets or not.
    :testparams replicas: (int) no of replicas of bucket
    :testparams enable_flush: (boolean) to enable flush on all the buckets.
    :testparams num_buckets: (int) no. of buckets to be created.
    :testparams bucket_size: (int) size of bucket in MB. Min bucket size is 100.
    :testparams num_scopes: (int) no. of scopes per bucket
    :testparams num_collections: (int) no. of collections per scope
    :testparams num_items: (int) no. of items per collection
    :testparams durability: (str)
    :testparams sdk_timeout: (int)
    :testparams doc_size: (int) size of each doc.
    :testparams no_of_dataverses: (int) no. of dataverses to be created
    :testparams no_of_datasets_per_dataverse: (int) no. of datasets per dataverse
    :testparams no_of_links: (int) no. of remote links
    :testparams no_of_synonyms: (int) no. of synonyms
    :testparams no_of_indexes: (int) no. of indexes on datasets
    :testparams 
    :testparams 
    """
    
    def setUp(self):
        self.input = TestInputSingleton.input
        self.input.test_params.update({"default_bucket": False})
        super(volume, self).setUp()
        
        self.test_type = self.input.param("test_type", "steady_state")
        self.iterations = self.input.param("iterations", 2)
        self.vbucket_check = self.input.param("vbucket_check", True)
        
        self.bucket_spec = self.input.param("bucket_spec", "volume_templates.buckets_for_volume_test")
        self.data_load_spec = self.input.param("data_load_spec", "volume_test_load_for_volume_test")
        self.cbas_spec = self.input.param("cbas_spec", "volume")
        self.bucket_size = self.input.param("bucket_size", 0)
        
        self.contains_ephemeral = self.input.param("contains_ephemeral", True)

        # the stage at which CRUD for collection level/ document level take place.
        # "before" - start and finish before rebalance/failover starts at each step
        # "during" - during rebalance/failover at each step
        self.data_load_stage = self.input.param("data_load_stage", "during")

        self.doc_and_collection_ttl = self.input.param("doc_and_collection_ttl", False)  # For using doc_ttl + coll_ttl
        self.skip_validations = self.input.param("skip_validations", True)
        
        # Assuming that only 2 clusters are used
        self.local_cluster = None
        self.remote_cluster = None
        CBASRebalanceUtil.available_servers = self.servers[:]
        CBASRebalanceUtil.exclude_nodes = list()
        
        # Adding nodes in clusters, creating indexes, loading data in collections and creating cbas infra
        for cluster in self.get_clusters():
            cluster.nodes_in_cluster = [cluster.master]
            cluster.cluster_util = ClusterUtils(cluster, self.task_manager)
            cluster.bucket_util = BucketUtils(cluster,
                                              cluster.cluster_util,
                                              self.task)
            cluster.rest = RestConnection(cluster.master)
            cluster.cbas_nodes = list()
            cluster.bucket_helper_obj = BucketHelper(cluster.master)
            
            for node in CBASRebalanceUtil.available_servers:
                if node.ip == cluster.master.ip:
                    CBASRebalanceUtil.available_servers.remove(node)
                    break
            CBASRebalanceUtil.exclude_nodes.append(cluster.master)
                    
            def get_init_services(services_to_use):
                services = list()
                for service in services_to_use.split("-"):
                    services.append(service.replace(":", ","))
                return services if len(services) > 0 else None
            
            if not self.local_cluster:
                self.local_cluster = cluster
                services_to_use = get_init_services(self.input.param("local_init_services", "cbas"))
                init_nodes = self.nodes_init
            else:
                self.remote_cluster = cluster
                services_to_use = get_init_services(self.input.param("remote_init_services", "kv:index"))
                init_nodes = self.input.param("remote_init_nodes", 1)
            
            # reduce init node by 1 as 1 node will be used while initializing cluster
            init_nodes -= 1
            
            for i in range(0, init_nodes):
                node_to_initialize = CBASRebalanceUtil.available_servers.pop(-1)
                services = services_to_use.pop(0)
                services = services.split(",")
                
                node_rest = RestConnection(node_to_initialize)
                info = node_rest.get_nodes_self()
                total_free_memory = int(info.mcdMemoryReserved)
                
                if "kv" in services:
                    self.set_memory_quota(cluster, False, total_free_memory)
                if "cbas" in services:
                    self.set_memory_quota(cluster, True, total_free_memory)
                    cluster.cbas_nodes.append(node_to_initialize)
                
                cluster.cluster_util.add_node(node_to_initialize,services,rebalance=False)
                cluster.nodes_in_cluster.append(node_to_initialize)
                do_rebalance = True
        
            if do_rebalance:
                operation = self.task.async_rebalance(cluster.nodes_in_cluster, [], [])
                self.task.jython_task_manager.get_task_result(operation)
                if not operation.result:
                    self.log.error("Failed while adding nodes to cluster during setup")
                    self.tearDown()
            
            try:
                self.collectionSetUp(cluster, cluster.bucket_util, False)
            except Java_base_exception as exception:
                    self.handle_collection_setup_exception(exception)
            except Exception as exception:
                self.handle_collection_setup_exception(exception)
            
            cluster.bucket_util._expiry_pager(val=5)
        
        CBASRebalanceUtil.exclude_nodes.append(self.local_cluster.cbas_nodes[0])
        self.local_cluster.cbas_util = CbasUtil(self.local_cluster.master, self.local_cluster.cbas_nodes[0], self.task)
        self.local_cluster.rebalance_util = CBASRebalanceUtil(
            self.local_cluster, self.local_cluster.cluster_util, self.local_cluster.bucket_util, 
            self.task, self.local_cluster.rest, vbucket_check=self.vbucket_check, 
            cbas_util=self.local_cluster.cbas_util)
        
        cbas_spec = self.local_cluster.cbas_util.get_cbas_spec(self.cbas_spec)
        update_spec = {
            "no_of_dataverses":self.input.param('no_of_dataverses', 1),
            "no_of_datasets_per_dataverse":self.input.param('no_of_datasets_per_dataverse', 1),
            "no_of_synonyms":self.input.param('no_of_synonyms', 1),
            "no_of_indexes":self.input.param('no_of_indexes', 1),
            "no_of_links":self.input.param('no_of_links', 1)}
        self.local_cluster.cbas_util.update_cbas_spec(cbas_spec, update_spec)
        
        if self.remote_cluster:
            self.remote_cluster.rebalance_util = CBASRebalanceUtil(
                self.remote_cluster, self.remote_cluster.cluster_util, 
                self.remote_cluster.bucket_util, self.task, self.remote_cluster.rest, 
                vbucket_check=self.vbucket_check, cbas_util=None)
            link_properties = list()
            for server in self.remote_cluster.nodes_in_cluster:
                for encryption in ['none', 'half']:
                    link_properties.append(
                        {"type" : "couchbase", "hostname" : server.ip, 
                         "username" : server.rest_username, "password" : server.rest_password, 
                         "encryption":encryption})
            cbas_spec["link"]["properties"] = link_properties
            if not self.local_cluster.cbas_util.create_cbas_infra_from_spec(
                cbas_spec, self.local_cluster.bucket_util, self.remote_cluster.bucket_util,
                wait_for_ingestion=False):
                self.fail("Error while creating infra from CBAS spec")
        else:
            if not self.local_cluster.cbas_util.create_cbas_infra_from_spec(
                cbas_spec, self.local_cluster.bucket_util,wait_for_ingestion=False):
                self.fail("Error while creating infra from CBAS spec")
        
        # start parallel query execution on KV and CBAS
        for cluster in self.get_clusters():
            if cluster.rebalance_util.cbas_util:
                cluster.rebalance_util.run_parallel_cbas_query = self.input.param(
                    "run_parallel_cbas_query", False)
            cluster.rebalance_util.run_parallel_kv_query = self.input.param(
                    "run_parallel_kv_query", False)
            cluster.rebalance_util.start_parallel_queries()

    def tearDown(self):
        # Do not call the base class's teardown, as we want to keep the cluster intact after the volume run
        for cluster in self.get_clusters():
            cluster.rebalance_util.stop_parallel_queries()
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
            kv_quota = info.mcdMemoryAllocated
            cluster.rest.set_service_memoryQuota(service="memoryQuota", memoryQuota=kv_quota)
    
    # This code will be removed once cbas_base is refactored
    def handle_collection_setup_exception(self, exception_obj):
        if self.sdk_client_pool is not None:
            self.sdk_client_pool.shutdown()
        traceback.print_exc()
        raise exception_obj
    
    # This code will be removed once cbas_base is refactored
    def collectionSetUp(self, cluster, bucket_util, load_data=True):
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
        self.over_ride_bucket_template_params(buckets_spec,cluster)
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
        if load_data:
            self.reload_data_into_buckets(cluster)
    
    # This code will be removed once cbas_base is refactored
    def over_ride_bucket_template_params(self, bucket_spec, cluster):
        for over_ride_param in self.over_ride_spec_params:
            if over_ride_param == "replicas":
                bucket_spec[Bucket.replicaNumber] = self.num_replicas
            elif over_ride_param == "remove_default_collection":
                bucket_spec[MetaConstants.REMOVE_DEFAULT_COLLECTION] = \
                    self.remove_default_collection
            elif over_ride_param == "enable_flush":
                if self.input.param("enable_flush", False):
                    bucket_spec[Bucket.flushEnabled] = Bucket.FlushBucket.ENABLED
                else:
                    bucket_spec[Bucket.flushEnabled] = Bucket.FlushBucket.DISABLED
            elif over_ride_param == "num_buckets":
                bucket_spec[MetaConstants.NUM_BUCKETS] = int(
                    self.input.param("num_buckets", 1))
            elif over_ride_param == "bucket_size":
                if self.bucket_size < 100:
                    cluster_info = cluster.rest.get_nodes_self()
                    kv_quota = cluster_info.__getattribute__("memoryQuota")
                    self.bucket_size = kv_quota // bucket_spec[MetaConstants.NUM_BUCKETS]
                bucket_spec[Bucket.ramQuotaMB] = self.bucket_size
            elif over_ride_param == "num_scopes":
                bucket_spec[MetaConstants.NUM_SCOPES_PER_BUCKET] = int(
                    self.input.param("num_scopes", 1))
            elif over_ride_param == "num_collections":
                bucket_spec[MetaConstants.NUM_COLLECTIONS_PER_SCOPE] = int(
                    self.input.param("num_collections", 1))
            elif over_ride_param == "num_items":
                bucket_spec[MetaConstants.NUM_ITEMS_PER_COLLECTION] = \
                    self.num_items
    
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

    # Stopping and restarting the memcached process
    def stop_process(self):
        for cluster in self.get_clusters():
            cluster_kv_nodes = cluster.cluster_util.get_nodes_from_services_map(
                service_type="kv", get_all_nodes=True, servers=cluster.servers, master=cluster.master)
            try:
                cluster_kv_nodes.remove(cluster.master)
            except:
                pass
            remote = RemoteMachineShellConnection(random.choice(cluster_kv_nodes))
            error_sim = CouchbaseError(self.log, remote)
            error_to_simulate = "stop_memcached"
            # Induce the error condition
            error_sim.create(error_to_simulate)
            self.sleep(20, "Wait before reverting the error condition")
            # Revert the simulated error condition and close the ssh session
            error_sim.revert(error_to_simulate)
            remote.disconnect()
    
    #used
    def rebalance(self, cluster, kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=0):
        
        if kv_nodes_out > 0:
            cluster_kv_nodes = cluster.cluster_util.get_nodes_from_services_map(
                service_type="kv", get_all_nodes=True, servers=cluster.servers, master=cluster.master)
        else:
            cluster_kv_nodes = []
        
        if cbas_nodes_out > 0:
            cluster_cbas_nodes = cluster.cluster_util.get_nodes_from_services_map(
                service_type="cbas", get_all_nodes=True, servers=cluster.servers, master=cluster.master)
        else:
            cluster_cbas_nodes = []
        
        for node in self.exclude_nodes:
            try:
                cluster_kv_nodes.remove(node)
            except:
                pass
            try:
                cluster_cbas_nodes.remove(node)
            except:
                pass
            
        servs_in = random.sample(self.available_servers, kv_nodes_in+cbas_nodes_in)
        servs_out = random.sample(cluster_kv_nodes, kv_nodes_out) + random.sample(cluster_cbas_nodes, cbas_nodes_out)
            

        if kv_nodes_in == kv_nodes_out:
            self.vbucket_check = False

        services = list()
        if kv_nodes_in > 0:
            services += ["kv"] * kv_nodes_in
        if cbas_nodes_in > 0:
            services += ["cbas"] * cbas_nodes_in

        rebalance_task = self.task.async_rebalance(
            cluster.servers, servs_in, servs_out, check_vbucket_shuffling=self.vbucket_check,
            retry_get_process_num=200, services=services)

        self.available_servers = [servs for servs in self.available_servers if servs not in servs_in]
        self.available_servers += servs_out

        cluster.servers.extend(servs_in)
        cluster.servers = list(set(cluster.servers) - set(servs_out))
        return rebalance_task
    
    #used
    def wait_for_rebalance_to_complete(self, tasks):
        for task in tasks:
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
    
    #used
    def data_load_collection(self, async_load=True, skip_read_success_results=True):
        tasks = dict()
        for cluster in self.get_clusters():
            doc_loading_spec = \
                cluster.bucket_util.get_crud_template_from_package(self.data_load_spec)
            self.set_retry_exceptions(doc_loading_spec)
            doc_loading_spec[MetaCrudParams.DURABILITY_LEVEL] = self.durability_level
            doc_loading_spec[MetaCrudParams.SKIP_READ_SUCCESS_RESULTS] = skip_read_success_results
            tasks[cluster] = cluster.bucket_util.run_scenario_from_spec(
                self.task, cluster, cluster.bucket_util.buckets, doc_loading_spec, 
                mutation_num=0, async_load=async_load)
        if not async_load:
            result = True
            for task in tasks.values():
                result = result and task.result
            return result
        return tasks
    
    #used
    def reload_data_into_buckets(self,cluster):
        """
        Initial data load happens in collections_base. But this method loads
        data again when buckets have been flushed during volume test
        """
        doc_loading_spec = \
            cluster.bucket_util.get_crud_template_from_package(
                self.data_load_spec)
        doc_loading_task = \
            cluster.bucket_util.run_scenario_from_spec(
                self.task,
                cluster,
                cluster.bucket_util.buckets,
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
        cluster.bucket_util._wait_for_stats_all_buckets()
        if self.bucket_spec not in ttl_buckets:
            cluster.bucket_util.validate_docs_per_collections_all_buckets()

        # Prints bucket stats after doc_ops
        cluster.bucket_util.print_bucket_stats()
    
    #used
    def wait_for_async_data_load_to_complete(self, tasks):
        for cluster,task in tasks.iteritems():
            self.task.jython_task_manager.get_task_result(task)
            if not self.skip_validations:
                cluster.bucket_util.validate_doc_loading_results(task)
                if task.result is False:
                    self.fail("Doc_loading failed")
    
    #used
    def data_validation_collection(self):
        for cluster in self.get_clusters(): 
            retry_count = 0
            while retry_count < 10:
                try:
                    cluster.bucket_util._wait_for_stats_all_buckets()
                except:
                    retry_count = retry_count + 1
                    self.log.info("ep-queue hasn't drained yet. Retry count: {0}".format(retry_count))
                else:
                    break
            if retry_count == 10:
                self.log.info("Attempting last retry for ep-queue to drain")
                cluster.bucket_util._wait_for_stats_all_buckets()
            if self.doc_and_collection_ttl:
                cluster.bucket_util._expiry_pager(val=5)
                self.sleep(400, "wait for doc/collection maxttl to finish")
                items = 0
                cluster.bucket_util._wait_for_stats_all_buckets()
                for bucket in cluster.bucket_util.buckets:
                    items = items + cluster.bucket_helper_obj.get_active_key_count(bucket)
                if items != 0:
                    self.fail("doc count!=0, TTL + rebalance failed")
            else:
                if not self.skip_validations:
                    cluster.bucket_util.validate_docs_per_collections_all_buckets()
                else:
                    pass

    def wait_for_failover_or_assert(self, cluster_rest, expected_failover_count, timeout=7200):
        # Timeout is kept large for graceful failover
        time_start = time.time()
        time_max_end = time_start + timeout
        actual_failover_count = 0
        while time.time() < time_max_end:
            actual_failover_count = self.get_failover_count(cluster_rest)
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

    def get_failover_count(self, cluster_rest):
        cluster_status = cluster_rest.cluster_status()
        failover_count = 0
        # check for inactiveFailed
        for node in cluster_status['nodes']:
            if node['clusterMembership'] == "inactiveFailed":
                failover_count += 1
        return failover_count
    
    #used
    def validate_docs_in_datasets(self):
        result = self.local_cluster.cbas_util.validate_docs_in_all_datasets()
        self.assertTrue(result, "Error while validating doc count in datasets")        

    def test_volume_taf(self):
        self.loop = 0
        self.log.info("Finished steps 1-7 successfully in setup")
        
        while self.loop < self.iterations:
            
            def all_rebalance(kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=0):
                tasks = list()
                tasks.append(self.rebalance(
                    cluster=self.local_cluster, kv_nodes_in=kv_nodes_in, kv_nodes_out=kv_nodes_out, 
                    cbas_nodes_in=cbas_nodes_in, cbas_nodes_out=cbas_nodes_out))
                if self.remote_cluster:
                    tasks.append(self.rebalance(
                        cluster=self.remote_cluster, kv_nodes_in=kv_nodes_in, kv_nodes_out=kv_nodes_out, 
                        cbas_nodes_in=0, cbas_nodes_out=0))
                return tasks
            
            def print_all_cluster_bucket_stats():
                for cluster in self.get_clusters():
                    cluster.bucket_util.print_bucket_stats()
            
            def change_bucket_replica(replicaNumber):
                for cluster in self.get_clusters(): 
                    for i in range(len(cluster.bucket_util.buckets)):
                        cluster.bucket_helper_obj.change_bucket_props(
                            cluster.bucket_util.buckets[i], replicaNumber=replicaNumber)
            #########################################################################################################################
            if self.test_type == "steady_state":
                for cluster in self.get_clusters():
                    self.reload_data_into_buckets(cluster)
                    cluster.cluster_util.print_cluster_stats()
                if self.remote_cluster:
                    self.assertTrue(
                        self.local_cluster.cbas_util.validate_docs_in_all_datasets(
                            self.local_cluster.bucket_util, self.remote_cluster.bucket_util),
                        "Error while validating doc count in datasets")
                else:
                    self.assertTrue(
                        self.local_cluster.cbas_util.validate_docs_in_all_datasets(
                            self.local_cluster.bucket_util, None),
                        "Error while validating doc count in datasets")
            else:
                #########################################################################################################################
                self.log.info("Step 8: Rebalance in data node on both Local and Remote cluster with Loading of docs")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=1, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=0)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 9: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Step 10: Rebalance in CBAS node on Local cluster with Loading of docs on KV")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=1, cbas_nodes_out=0)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 11: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Step 12: Rebalance out CBAS node on Local cluster and data and on both Local and Remote cluster with Loading of docs")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=1)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 13: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Step 14: Rebalance In CBAS node on Local cluster and data and on both Local and Remote cluster with Loading of docs")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=1, kv_nodes_out=0, cbas_nodes_in=1, cbas_nodes_out=0)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 15: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Step 16: Rebalance Out data node on both Local and Remote cluster with Loading of docs")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=0)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 17: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Step 18: Rebalance Out CBAS node on Local cluster with Loading of docs on KV")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=1)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 19: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Step 20: Rebalance In-Out data node on both Local and Remote cluster with Loading of docs")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=2, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=0)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 21: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Step 22: Rebalance In-Out CBAS node on Local cluster with Loading of docs on KV")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=2, cbas_nodes_out=1)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 23: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Rebalance out extra node that was rebalanced-in in last step")
                self.log.info("Step 24: Rebalance out CBAS node on Local cluster and data and on both Local and Remote cluster with Loading of docs")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=1)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 25: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Step 26: Rebalance In-Out cbas node and data node on both Local cluster and only data node on Remote cluster with Loading of docs")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=2, kv_nodes_out=1, cbas_nodes_in=2, cbas_nodes_out=1)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 27: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #######################################################################################################################
                self.log.info("Rebalance out extra node that was rebalanced-in in last step")
                self.log.info("Step 28: Rebalance out CBAS node on Local cluster and data and on both Local and Remote cluster with Loading of docs")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=1)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 29: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                #########################################################################################################################
                self.log.info("Step 30: Swap Rebalance KV node on Local and Remote cluster with Loading of docs on KV")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=1, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=0)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 31: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                ########################################################################################################################
                self.log.info("Step 32: Swap Rebalance CBAS node on Local cluster with Loading of docs on KV")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=1, cbas_nodes_out=1)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 33: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                ########################################################################################################################
                self.log.info("Step 34: Swap Rebalance KV and CBAS node on Local Cluster and KV node on Remote cluster with Loading of docs on KV")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                rebalance_task = all_rebalance(kv_nodes_in=1, kv_nodes_out=1, cbas_nodes_in=1, cbas_nodes_out=1)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 35: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                ########################################################################################################################
                self.log.info("Step 36: Updating the bucket replica to 2 on Local and Remote cluster")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                change_bucket_replica(replicaNumber=2)
                rebalance_task = all_rebalance(kv_nodes_in=1, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=0)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 37: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
                ########################################################################################################################
                if self.contains_ephemeral:
                    self.log.info("No Memcached kill for ephemeral bucket")
                else:
                    self.log.info("Step 38: Stopping and restarting memcached process")
                    if self.data_load_stage == "before":
                        task = self.data_load_collection(async_load=False)
                        if task.result is False:
                            self.fail("Doc loading failed")
                    rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=0)
                    if self.data_load_stage == "during":
                        task = self.data_load_collection()
                    self.wait_for_rebalance_to_complete(rebalance_task)
                    self.stop_process()
                    if self.data_load_stage == "during":
                        self.wait_for_async_data_load_to_complete(task)
                    self.data_validation_collection()
                    print_all_cluster_bucket_stats()
                    self.log.info("Step 39: Validating doc count in datasets.")
                    self.validate_docs_in_datasets()
                ########################################################################################################################
                step_count = 39
                for failover in ["Graceful", "Hard"]:
                    for action in ["RebalanceOut", "FullRecovery", "DeltaRecovery"]:
                        for service_type in ["kv", "cbas", "kv-cbas"]:
                            if (service_type in ["cbas","kv-cbas"]) and (failover == "Graceful" or action == "DeltaRecovery"):
                                continue
                            else:
                                step_count = step_count + 1
                                self.log.info(
                                    "Step {0}: {1} Failover a node and {2} that node with data load in parallel".format(step_count,
                                                                                                                        failover,
                                                                                                                        action))
                                if self.data_load_stage == "before":
                                    task = self.data_load_collection(async_load=False)
                                    if task.result is False:
                                        self.fail("Doc loading failed")
                                
                                if self.data_load_stage == "during":
                                    reset_flag = False
                                    if (not self.durability_level) and failover == "Hard" and "kv" in service_type:
                                        # Force a durability level to prevent data loss during hard failover
                                        self.log.info("Forcing durability level: MAJORITY")
                                        self.durability_level = "MAJORITY"
                                        reset_flag = True
                                    task = self.data_load_collection()
                                    if reset_flag:
                                        self.durability_level = ""
                                
                                for cluster in self.get_clusters():
                                    if "kv" in service_type:
                                        cluster_kv_nodes = cluster.cluster_util.get_nodes_from_services_map(
                                            service_type="kv", get_all_nodes=True, servers=cluster.servers, master=cluster.master)
                                    else:
                                        cluster_kv_nodes = []
                                    
                                    if "cbas" in service_type:
                                        cluster_cbas_nodes = cluster.cluster_util.get_nodes_from_services_map(
                                            service_type="cbas", get_all_nodes=True, servers=cluster.servers, master=cluster.master)
                                    else:
                                        cluster_cbas_nodes = []
                                    
                                    for node in self.exclude_nodes:
                                        try:
                                            cluster_kv_nodes.remove(node)
                                        except:
                                            pass
                                        try:
                                            cluster_cbas_nodes.remove(node)
                                        except:
                                            pass
                                    
                                    failover_count = 0
                                    failover_nodes = []
                                    cluster.success_kv_failed_over = False
                                    cluster.success_cbas_failed_over = False
                                    # Mark Node for failover
                                    if failover == "Graceful":
                                        cluster.success_kv_failed_over = cluster.rest.fail_over(cluster_kv_nodes[0], graceful=True)
                                        failover_count += 1
                                        failover_nodes.append(cluster_kv_nodes[0])
                                    else:
                                        if "kv" in service_type:
                                            cluster.success_kv_failed_over = cluster.rest.fail_over(cluster_kv_nodes[0], graceful=True)
                                            failover_count += 1
                                            failover_nodes.append(cluster_kv_nodes[0])
                                        if "cbas" in service_type and cluster_cbas_nodes:
                                            cluster.success_cbas_failed_over = cluster.rest.fail_over(cluster_cbas_nodes[0], graceful=True)
                                            failover_count += 1
                                            failover_nodes.append(cluster_cbas_nodes[0])
                                    self.sleep(300)
                                    self.wait_for_failover_or_assert(cluster.rest, failover_count)
    
                                    # Perform the action
                                    if action == "RebalanceOut":
                                        nodes = cluster.rest.node_statuses()
                                        cluster.rest.rebalance(
                                            otpNodes=[node.id for node in nodes], ejectedNodes=[node.ip for node in failover_nodes])
                                        # self.sleep(600)
                                        self.assertTrue(cluster.rest.monitorRebalance(stop_if_loop=False), msg="Rebalance failed")
                                        cluster.servers = list(set(cluster.servers) - set(failover_nodes))
                                        self.available_servers += failover_nodes
                                        self.sleep(10)
                                    else:
                                        if action == "FullRecovery":
                                            if cluster.success_kv_failed_over:
                                                cluster.rest.set_recovery_type(otpNode=cluster_kv_nodes[0].id, recoveryType="full")
                                            if cluster.success_cbas_failed_over:
                                                cluster.rest.set_recovery_type(otpNode=cluster_cbas_nodes[0].id, recoveryType="full")
                                        elif action == "DeltaRecovery":
                                            if cluster.success_kv_failed_over:
                                                cluster.rest.set_recovery_type(otpNode=cluster_kv_nodes[0].id, recoveryType="delta")
                
                                        rebalance_task = self.task.async_rebalance(
                                            cluster.servers, [], [], retry_get_process_num=200)
                                        self.wait_for_rebalance_to_complete([rebalance_task])
                                        self.sleep(10)
    
                                if self.data_load_stage == "during":
                                    self.wait_for_async_data_load_to_complete(task)
                                self.data_validation_collection()
    
                                # Bring back the rebalance out node back to cluster for further steps
                                if action == "RebalanceOut":
                                    self.sleep(120)
                                    kv_nodes_in = 0
                                    cbas_nodes_in = 0
                                    if service_type in ["kv", "kv-cbas"]:
                                        kv_nodes_in = 1
                                    if service_type in ["cbas", "kv-cbas"]:
                                        cbas_nodes_in = 1
                                    rebalance_task = all_rebalance(
                                        kv_nodes_in=kv_nodes_in, kv_nodes_out=0, cbas_nodes_in=cbas_nodes_in, cbas_nodes_out=0)
                                    self.wait_for_rebalance_to_complete(rebalance_task)
                                print_all_cluster_bucket_stats()
                                step_count = step_count + 1
                                self.log.info("Step {0}: Validating doc count in datasets.".format(step_count))
                                self.validate_docs_in_datasets()
                ########################################################################################################################
                self.log.info("Step 60: Updating the bucket replica to 1 on Local and Remote cluster")
                if self.data_load_stage == "before":
                    task_result = self.data_load_collection(async_load=False)
                    if task_result is False:
                        self.fail("Doc loading failed")
                change_bucket_replica(replicaNumber=1)
                rebalance_task = all_rebalance(kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=0)
                if self.data_load_stage == "during":
                    task = self.data_load_collection()
                self.wait_for_rebalance_to_complete(rebalance_task)
                if self.data_load_stage == "during":
                    self.wait_for_async_data_load_to_complete(task)
                self.data_validation_collection()
                print_all_cluster_bucket_stats()
                self.log.info("Step 61: Validating doc count in datasets.")
                self.validate_docs_in_datasets()
            ########################################################################################################################
            self.log.info("Step 62: Flush bucket(s) and start the entire process again")
            self.loop += 1
            if self.loop < self.iterations:
                cluster_init_dict = {self.local_cluster:self.nodes_init}
                if self.remote_cluster:
                    cluster_init_dict[self.remote_cluster] = self.input.param("remote_init_nodes", 1)
                # Flush buckets(s)
                for cluster, init_node in cluster_init_dict.iteritems(): 
                    cluster.bucket_util.flush_all_buckets(cluster.master, skip_resetting_num_items=True)
                    self.sleep(10)
                    if len(cluster.nodes_in_cluster) > init_node:
                        nodes_cluster = cluster.nodes_in_cluster
                        nodes_cluster.remove(cluster.master)
                        servs_out = random.sample(nodes_cluster,
                                                  int(len(cluster.nodes_in_cluster) - init_node))
                        rebalance_task = self.task.async_rebalance(
                            cluster.nodes_in_cluster, [], servs_out, retry_get_process_num=200)
                        cluster.rebalance_util.wait_for_rebalance_task_to_complete(rebalance_task)
                        CBASRebalanceUtil.available_servers += servs_out
                        cluster.nodes_in_cluster = list(set(cluster.nodes_in_cluster) - set(servs_out))
            else:
                self.log.info("Volume Test Run Complete")
        ############################################################################################################################
