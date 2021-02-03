'''
@author: Umang
TO-DO : Extend this to support remote cluster rebalance operations also, once cbas_base refactoring is done
'''

from TestInput import TestInputSingleton
from cbas.cbas_base import CBASBaseTest
from collections_helper.collections_spec_constants import MetaConstants, MetaCrudParams
import random, time, json
from BucketLib.BucketOperations import BucketHelper
import threading
from CbasLib.CBASOperations import CBASHelper
from sdk_exceptions import SDKException

class CBASRebalance(CBASBaseTest):
    
    def setUp(self):
        self.input = TestInputSingleton.input
        if "bucket_spec" not in self.input.test_params:
            self.input.test_params.update({"bucket_spec": "analytics.default"})
        if "set_cbas_memory_from_available_free_memory" not in self.input.test_params:
            self.input.test_params.update({"set_cbas_memory_from_available_free_memory": True})
        super(CBASRebalance, self).setUp()
        
        self.data_load_stage = self.input.param("data_load_stage", "during")
        self.run_parallel_kv_query = self.input.param("run_kv_queries", False)
        self.run_parallel_cbas_query = self.input.param("run_cbas_queries", False)
        self.vbucket_check = self.input.param("vbucket_check", True)
        self.query_interval = self.input.param("query_interval", 3)
        self.no_of_parallel_queries = self.input.param("no_of_parallel_queries", 1)
        
        self.available_servers = list()
        self.available_servers.extend(self.cluster.servers)
        self.exclude_nodes = [self.cluster.master, self.cbas_node]
        self.cluster.nodes_in_cluster = [self.cluster.master, self.cbas_node]
        
        for node in self.exclude_nodes:
            try:
                self.available_servers.remove(node)
            except:
                pass
        
        init_kv_nodes = self.input.param("init_kv_nodes", 1) - 1
        init_cbas_nodes = self.input.param("init_cbas_nodes", 1) - 1
        
        while init_kv_nodes > 0 or init_cbas_nodes > 0:
            node_to_initialize = self.available_servers.pop(-1)
            if init_kv_nodes > 0:
                services = ["kv"]
                init_kv_nodes -= 1
            else:
                services = ["cbas"]
                init_cbas_nodes -= 1
            node_to_initialize.services = services
            self.cluster_util.add_node(node_to_initialize,services,rebalance=False)
            self.cluster.nodes_in_cluster.append(node_to_initialize)
        
        operation = self.task.async_rebalance(self.cluster.nodes_in_cluster, [], [])
        if not self.wait_for_task_to_complete(operation):
            self.log.error("Failed while adding nodes to cluster during setup")
            self.tearDown()
        
        self.cbas_spec = self.cbas_util_v2.get_cbas_spec(self.cbas_spec_name)
        update_spec = {
            "no_of_dataverses":self.input.param('no_of_dv', 1),
            "no_of_datasets_per_dataverse":self.input.param('ds_per_dv', 1),
            "no_of_synonyms":self.input.param('no_of_synonyms', 1),
            "no_of_indexes":self.input.param('no_of_indexes', 1),
            "max_thread_count":self.input.param('no_of_threads', 1)}
        self.cbas_util_v2.update_cbas_spec(self.cbas_spec, update_spec)
        if not self.cbas_util_v2.create_cbas_infra_from_spec(self.cbas_spec, self.bucket_util):
            self.fail("Error while creating infra from CBAS spec")
        
        self.bucket_helper_obj = BucketHelper(self.cluster.master)
        
        if self.run_parallel_cbas_query:
            self.run_cbas_queries()
        
        if self.run_parallel_kv_query:
            self.run_n1ql_query()

        self.log.info("================================================================")
        self.log.info("SETUP has finished")
        self.log.info("================================================================")

    def tearDown(self):
        
        self.log.info("================================================================")
        self.log.info("TEARDOWN has started")
        self.log.info("================================================================")
        
        if self.run_parallel_cbas_query:
            self.log.info("Stopping CBAS queries")
            self.run_parallel_cbas_query = False
            self.cbas_query_thread.join()
        
        if self.run_parallel_kv_query:
            self.log.info("Stopping N1QL queries")
            self.run_parallel_kv_query = False
            self.n1ql_query_thread.join()
        
        super(CBASRebalance, self).tearDown()
        
        self.log.info("================================================================")
        self.log.info("Teardown has finished")
        self.log.info("================================================================")
    
    def run_cbas_queries(self):
        self.log.info("Starting CBAS queries")
        def inner_func():
            datasets = self.cbas_util_v2.list_all_dataset_objs()
            while self.run_parallel_cbas_query:
                total_items, mutated_items = self.cbas_util_v2.get_num_items_in_cbas_dataset(
                    (random.choice(datasets)).full_name, timeout=300, analytics_timeout=300)
                if total_items < 0 or mutated_items < 0:
                    self.log.warn("CBAS Query failed")
                time.sleep(self.query_interval)
        self.cbas_query_thread = threading.Thread(target=inner_func, name="cbas_query")
        self.cbas_query_thread.start()
    
    def run_n1ql_query(self):
        """
        Runs select queries in a loop in a separate thread until the thread is asked for to join
        """
        self.log.info("Starting N1QL queries: {0}".format(self.run_parallel_kv_query))
        collection_list = list()
        for bucket in self.bucket_util.buckets:
            status, content = self.bucket_helper_obj.list_collections(bucket.name)
            if status:
                content = json.loads(content)
                for scope in content["scopes"]:
                    for collection in scope["collections"]:
                        collection_list.append(CBASHelper.format_name(bucket.name, scope["name"], collection["name"]))
        def inner_func():
            while self.run_parallel_kv_query:
                for collection in collection_list:
                    query = "select count(*) from {0}".format(collection)
                    result = self.rest.query_tool(query, timeout=600)
                    if result['status'] != "success":
                        self.log.warn("Query failed: {0}".format(query))
                    time.sleep(self.query_interval)
        self.n1ql_query_thread = threading.Thread(target=inner_func,name="N1QL_query")
        self.n1ql_query_thread.start()
    
    def wait_for_task_to_complete(self, task):
        self.task.jython_task_manager.get_task_result(task)
        return task.result
    
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
        doc_loading_spec = self.bucket_util.get_crud_template_from_package(self.doc_spec_name)
        self.set_retry_exceptions(doc_loading_spec)
        doc_loading_spec[MetaCrudParams.DURABILITY_LEVEL] = self.durability_level
        doc_loading_spec[MetaCrudParams.SKIP_READ_SUCCESS_RESULTS] = skip_read_success_results
        task = self.bucket_util.run_scenario_from_spec(
            self.task, self.cluster, self.bucket_util.buckets, 
            doc_loading_spec, mutation_num=0, async_load=async_load)
        if not async_load:
            return self.wait_for_task_to_complete(task)
        return task
    
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
    
    def rebalance(self, kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=0):
        
        if kv_nodes_out > 0:
            cluster_kv_nodes = self.cluster_util.get_nodes_from_services_map(
                service_type="kv", get_all_nodes=True, servers=self.cluster.nodes_in_cluster, master=self.cluster.master)
        else:
            cluster_kv_nodes = []
        
        if cbas_nodes_out > 0:
            cluster_cbas_nodes = self.cluster_util.get_nodes_from_services_map(
                service_type="cbas", get_all_nodes=True, servers=self.cluster.nodes_in_cluster, master=self.cluster.master)
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
            self.cluster.nodes_in_cluster, servs_in, servs_out, check_vbucket_shuffling=self.vbucket_check,
            retry_get_process_num=200, services=services)

        self.available_servers = [servs for servs in self.available_servers if servs not in servs_in]
        self.available_servers += servs_out

        self.cluster.nodes_in_cluster.extend(servs_in)
        self.cluster.nodes_in_cluster = list(set(self.cluster.nodes_in_cluster) - set(servs_out))
        return rebalance_task
    
    def load_collections_with_rebalance(self, rebalance_operation, kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=0):
        
        self.log.info("Doing collection data load {0} {1}".format(self.data_load_stage, rebalance_operation))
        
        if rebalance_operation in ["rebalance_out", "rebalance_in_out"]:
            # Rebalance in nodes before rebalancing out
            if len(self.cluster.nodes_in_cluster) <= 2:
                if kv_nodes_out > 0:
                    kv_nodes_in = 1
                if cbas_nodes_out > 0:
                    cbas_nodes_in = 1
                rebalance_task = self.rebalance(kv_nodes_in=kv_nodes_in, kv_nodes_out=0, 
                                                cbas_nodes_in=cbas_nodes_in, cbas_nodes_out=0)
                if not self.wait_for_task_to_complete(rebalance_task):
                    self.fail("Pre-Rebalance failed")
            
        
        if self.data_load_stage == "before":
            if not self.data_load_collection(async_load=False):
                self.fail("Doc loading failed")
        
        rebalance_task = self.rebalance(kv_nodes_in=kv_nodes_in, kv_nodes_out=kv_nodes_out, 
                                        cbas_nodes_in=cbas_nodes_in, cbas_nodes_out=cbas_nodes_out)
        
        if self.data_load_stage == "during":
            task = self.data_load_collection()
            
        if not self.wait_for_task_to_complete(rebalance_task):
            self.fail("Rebalance failed")
        
        if self.data_load_stage == "during":
            if not self.wait_for_task_to_complete(task):
                self.fail("Doc loading failed")
        
        self.data_validation_collection()
        
        if self.data_load_stage == "after":
            if not self.data_load_collection(async_load=False):
                self.fail("Doc loading failed")
            self.data_validation_collection()
        
        self.bucket_util.print_bucket_stats()
        if not self.cbas_util_v2.validate_docs_in_all_datasets(self.bucket_util):
            self.fail("Doc count mismatch between KV and CBAS")
    
    def get_failover_count(self, cluster_rest):
        cluster_status = cluster_rest.cluster_status()
        failover_count = 0
        # check for inactiveFailed
        for node in cluster_status['nodes']:
            if node['clusterMembership'] == "inactiveFailed":
                failover_count += 1
        return failover_count
    
    def wait_for_failover_or_assert(self, expected_failover_count, timeout=7200):
        # Timeout is kept large for graceful failover
        time_start = time.time()
        time_max_end = time_start + timeout
        actual_failover_count = 0
        while time.time() < time_max_end:
            actual_failover_count = self.get_failover_count(self.rest)
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
    
    def load_collections_with_failover(self, failover_type="Hard", action="RebalanceOut", service_type="cbas"):
        self.log.info("{0} Failover a node and {1} that node with data load in parallel".format(
            failover_type, action))
        
        if self.data_load_stage == "before":
            if not self.data_load_collection(async_load=False):
                self.fail("Doc loading failed")
                            
        if self.data_load_stage == "during":
            reset_flag = False
            if (not self.durability_level) and failover_type == "Hard" and "kv" in service_type:
                # Force a durability level to prevent data loss during hard failover
                self.log.info("Forcing durability level: MAJORITY")
                self.durability_level = "MAJORITY"
                reset_flag = True
            task = self.data_load_collection()
            if reset_flag:
                self.durability_level = ""
        
        if "kv" in service_type:
            cluster_kv_nodes = self.cluster_util.get_nodes_from_services_map(
                service_type="kv", get_all_nodes=True, servers=self.cluster.nodes_in_cluster, master=self.cluster.master)
        else:
            cluster_kv_nodes = []
        
        if "cbas" in service_type:
            cluster_cbas_nodes = self.cluster_util.get_nodes_from_services_map(
                service_type="cbas", get_all_nodes=True, servers=self.cluster.nodes_in_cluster, master=self.cluster.master)
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
        kv_failover_nodes = []
        cbas_failover_nodes = []
        self.success_kv_failed_over = False
        self.success_cbas_failed_over = False
        
        # Mark Node for failover
        if failover_type == "Graceful":
            chosen = self.cluster_util.pick_nodes(
                self.cluster.master, howmany=1, target_node= cluster_kv_nodes[0], exclude_nodes=self.exclude_nodes)
            self.success_kv_failed_over = self.rest.fail_over(chosen[0].id, graceful=True)
            failover_count += 1
            kv_failover_nodes.extend(chosen)
        else:
            if "kv" in service_type:
                chosen = self.cluster_util.pick_nodes(
                    self.cluster.master, howmany=1, target_node= cluster_kv_nodes[0], exclude_nodes=self.exclude_nodes)
                self.success_kv_failed_over = self.rest.fail_over(chosen[0].id, graceful=True)
                failover_count += 1
                kv_failover_nodes.extend(chosen)
            if "cbas" in service_type and cluster_cbas_nodes:
                chosen = self.cluster_util.pick_nodes(
                    self.cluster.master, howmany=1, target_node= cluster_cbas_nodes[0], exclude_nodes=self.exclude_nodes)
                self.success_cbas_failed_over = self.rest.fail_over(chosen[0].id, graceful=True)
                failover_count += 1
                cbas_failover_nodes.extend(chosen)
        self.sleep(300)
        self.wait_for_failover_or_assert(failover_count)

        # Perform the action
        if action == "RebalanceOut":
            nodes = self.rest.node_statuses()
            self.rest.rebalance(
                otpNodes=[node.id for node in nodes], ejectedNodes=[node.id for node in (kv_failover_nodes + cbas_failover_nodes)])
            # self.sleep(600)
            self.assertTrue(self.rest.monitorRebalance(stop_if_loop=False), msg="Rebalance failed")
            servs_out = [node for node in self.cluster.nodes_in_cluster for fail_node in (kv_failover_nodes + cbas_failover_nodes) if node.ip == fail_node.ip]
            self.cluster.nodes_in_cluster = list(set(self.cluster.nodes_in_cluster) - set(servs_out))
            self.available_servers += servs_out
            self.sleep(10)
        else:
            if action == "FullRecovery":
                if self.success_kv_failed_over:
                    self.rest.set_recovery_type(otpNode=kv_failover_nodes[0].id, recoveryType="full")
                if self.success_cbas_failed_over:
                    self.rest.set_recovery_type(otpNode=cbas_failover_nodes[0].id, recoveryType="full")
            elif action == "DeltaRecovery":
                if self.success_kv_failed_over:
                    self.rest.set_recovery_type(otpNode=kv_failover_nodes[0].id, recoveryType="delta")

            rebalance_task = self.task.async_rebalance(
                self.cluster.nodes_in_cluster, [], [], retry_get_process_num=200)
            self.wait_for_task_to_complete(rebalance_task)
            self.sleep(10)
        
        if self.data_load_stage == "during":
            if not self.wait_for_task_to_complete(task):
                self.fail("Doc loading failed")
        
        self.data_validation_collection()
        
        if self.data_load_stage == "after":
            if not self.data_load_collection(async_load=False):
                self.fail("Doc loading failed")
            self.data_validation_collection()
        
        self.bucket_util.print_bucket_stats()
        if not self.cbas_util_v2.validate_docs_in_all_datasets(self.bucket_util):
            self.fail("Doc count mismatch between KV and CBAS")

    def test_cbas_with_kv_rebalance_in(self):
        self.load_collections_with_rebalance(
            rebalance_operation="rebalance_in", kv_nodes_in=1, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=0)
    
    def test_cbas_with_cbas_rebalance_in(self):
        self.load_collections_with_rebalance(
            rebalance_operation="rebalance_in", kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=1, cbas_nodes_out=0)
    
    def test_cbas_with_kv_cbas_rebalance_in(self):
        self.load_collections_with_rebalance(
            rebalance_operation="rebalance_in", kv_nodes_in=1, kv_nodes_out=0, cbas_nodes_in=1, cbas_nodes_out=0)
    
    def test_cbas_with_kv_rebalance_out(self):
        self.load_collections_with_rebalance(
            rebalance_operation="rebalance_out", kv_nodes_in=0, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=0)
    
    def test_cbas_with_cbas_rebalance_out(self):
        self.load_collections_with_rebalance(
            rebalance_operation="rebalance_out", kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=0, cbas_nodes_out=1)
    
    def test_cbas_with_kv_cbas_rebalance_out(self):
        self.load_collections_with_rebalance(
            rebalance_operation="rebalance_out", kv_nodes_in=0, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=1)
        
    def test_cbas_with_kv_swap_rebalance(self):
        self.load_collections_with_rebalance(
            rebalance_operation="swap_rebalance", kv_nodes_in=1, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=0)
    
    def test_cbas_with_cbas_swap_rebalance(self):
        self.load_collections_with_rebalance(
            rebalance_operation="swap_rebalance", kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=1, cbas_nodes_out=1)
    
    def test_cbas_with_kv_cbas_swap_rebalance(self):
        self.load_collections_with_rebalance(
            rebalance_operation="swap_rebalance", kv_nodes_in=1, kv_nodes_out=1, cbas_nodes_in=1, cbas_nodes_out=1)    
        
    def test_cbas_with_kv_rebalance_in_out(self):
        self.load_collections_with_rebalance(
            rebalance_operation="rebalance_in_out", kv_nodes_in=2, kv_nodes_out=1, cbas_nodes_in=0, cbas_nodes_out=0)
    
    def test_cbas_with_cbas_rebalance_in_out(self):
        self.load_collections_with_rebalance(
            rebalance_operation="rebalance_in_out", kv_nodes_in=0, kv_nodes_out=0, cbas_nodes_in=2, cbas_nodes_out=1)
    
    def test_cbas_with_kv_cbas_rebalance_in_out(self):
        self.load_collections_with_rebalance(
            rebalance_operation="rebalance_in_out", kv_nodes_in=2, kv_nodes_out=1, cbas_nodes_in=2, cbas_nodes_out=1)    
        
    def test_cbas_with_kv_graceful_failover_rebalance_out(self):
        self.load_collections_with_failover(failover_type="Graceful", action="RebalanceOut", service_type="kv")
    
    def test_cbas_with_kv_graceful_failover_full_recovery(self):
        self.load_collections_with_failover(failover_type="Graceful", action="FullRecovery", service_type="kv")
    
    def test_cbas_with_kv_graceful_failover_delta_recovery(self):
        self.load_collections_with_failover(failover_type="Graceful", action="DeltaRecovery", service_type="kv")
    
    def test_cbas_with_kv_hard_failover_rebalance_out(self):
        self.load_collections_with_failover(failover_type="Hard", action="RebalanceOut", service_type="kv")
    
    def test_cbas_with_cbas_hard_failover_rebalance_out(self):
        self.load_collections_with_failover(failover_type="Hard", action="RebalanceOut", service_type="cbas")
    
    def test_cbas_with_kv_cbas_hard_failover_rebalance_out(self):
        self.load_collections_with_failover(failover_type="Hard", action="RebalanceOut", service_type="kv-cbas")
        
    def test_cbas_with_kv_hard_failover_full_recovery(self):
        self.load_collections_with_failover(failover_type="Hard", action="FullRecovery", service_type="kv")
    
    def test_cbas_with_cbas_hard_failover_full_recovery(self):
        self.load_collections_with_failover(failover_type="Hard", action="FullRecovery", service_type="cbas")
    
    def test_cbas_with_kv_cbas_hard_failover_full_recovery(self):
        self.load_collections_with_failover(failover_type="Hard", action="FullRecovery", service_type="kv-cbas")
    
    def test_cbas_with_kv_hard_failover_delta_recovery(self):
        self.load_collections_with_failover(failover_type="Hard", action="DeltaRecovery", service_type="kv")