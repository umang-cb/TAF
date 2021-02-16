from threading import Thread
import threading
import random
from java.util.concurrent import Callable
from java.util.concurrent import Executors, TimeUnit
from pytests.cbas.cbas_base import CBASBaseTest
from lib.membase.api.rest_client import RestConnection, RestHelper
from TestInput import TestInputSingleton
from bucket_utils.bucket_ready_functions import bucket_utils
from bucket_utils.Bucket import Bucket as buck
import time
from pytests.cbas.cbas_utils import cbas_utils


class QueryRunner(Callable):
    def __init__(self, query, num_queries, cbas_util):
        self.num_queries = num_queries
        self.started = None
        self.completed = None
        self.loaded = 0
        self.thread_used = None
        self.exception = None
        self.cbas_util = cbas_util
        self.statement = query
 
    def __str__(self):
        if self.exception:
            return "[%s] %s download error %s in %.2fs" % \
                (self.thread_used, self.num_queries, self.exception,
                 self.completed - self.started, ) #, self.result)
        elif self.completed:
            print "Time: %s"%str(time.strftime("%H:%M:%S", time.gmtime(time.time())))
            return "[%s] %s items loaded in %.2fs" % \
                (self.thread_used, self.loaded,
                 self.completed - self.started, ) #, self.result)
        elif self.started:
            return "[%s] %s started at %s" % \
                (self.thread_used, self.num_queries, self.started)
        else:
            return "[%s] %s not yet scheduled" % \
                (self.thread_used, self.num_queries)
 
    def call(self):
        self.thread_used = threading.currentThread().getName()
        self.started = time.time()
        try:
            self.cbas_util._run_concurrent_queries(self.statement, None, self.num_queries,batch_size=50)
            self.loaded += 1
        except Exception, ex:
            self.exception = ex
        self.completed = time.time()
        return self

def shutdown_and_await_termination(pool, timeout):
    pool.shutdown()
    try:
        if not pool.awaitTermination(timeout, TimeUnit.SECONDS):
            pool.shutdownNow()
            if (not pool.awaitTermination(timeout, TimeUnit.SECONDS)):
                print >> sys.stderr, "Pool did not terminate"
    except InterruptedException, ex:
        # (Re-)Cancel if current thread also interrupted
        pool.shutdownNow()
        # Preserve interrupt status
        Thread.currentThread().interrupt()
 
class analytics_high_doc_ops(CBASBaseTest):
    def setUp(self, add_defualt_cbas_node=True):
        self.input = TestInputSingleton.input
        self.input.test_params.update({"default_bucket":False})
        CBASBaseTest.setUp(self, add_defualt_cbas_node=add_defualt_cbas_node)
    
    def create_required_buckets(self):
        self.log.info("Get the available memory quota")
        bucket_util = bucket_utils(self.cluster.master)
        self.info = bucket_util.rest.get_nodes_self()
        threadhold_memory = 1024
        total_memory_in_mb = self.info.memoryFree / 1024 ** 2
        total_available_memory_in_mb = total_memory_in_mb
        active_service = self.info.services

        if "index" in active_service:
            total_available_memory_in_mb -= self.info.indexMemoryQuota
        if "fts" in active_service:
            total_available_memory_in_mb -= self.info.ftsMemoryQuota
        if "cbas" in active_service:
            total_available_memory_in_mb -= self.info.cbasMemoryQuota
        if "eventing" in active_service:
            total_available_memory_in_mb -= self.info.eventingMemoryQuota
        
        print(total_memory_in_mb)
        available_memory =  total_available_memory_in_mb - threadhold_memory
        self.rest.set_service_memoryQuota(service='memoryQuota', memoryQuota=available_memory)
        self.rest.set_service_memoryQuota(service='cbasMemoryQuota', memoryQuota=available_memory-1024)
        self.rest.set_service_memoryQuota(service='indexMemoryQuota', memoryQuota=available_memory-1024)

        self.log.info("Create CB buckets")
            
        self.create_bucket(self.cluster.master, "GleambookUsers",bucket_ram=available_memory, replica=0)
        self.sleep(30, "wait for bucket warmup to complete.")
        result = RestConnection(self.query_node).query_tool("CREATE PRIMARY INDEX idx_GleambookUsers ON GleambookUsers;")
        self.sleep(10, "wait for index creation.")
        self.assertTrue(result['status'] == "success")

    def setup_cbas(self):
        self.cbas_util = cbas_utils(self.cluster.master, self.cluster.cbas_nodes[0])
        self.cbas_util.createConn("GleambookUsers")
        self.cleanup_cbas()

        # Create dataset on the CBAS bucket
        self.cbas_util.create_dataset_on_bucket(cbas_bucket_name="GleambookUsers",
                                                cbas_dataset_name="GleambookUsers_ds")
        # Connect to Bucket
        self.connect_cbas_buckets()
        
    def connect_cbas_buckets(self):        
        # Connect to Bucket
        self.cbas_util.connect_to_bucket(cbas_bucket_name='GleambookUsers',
                               cb_bucket_password=self.cb_bucket_password)
        
    def disconnect_cbas_buckets(self):
        
        result = self.cbas_util.disconnect_from_bucket(cbas_bucket_name="GleambookUsers")
        self.assertTrue(result, "Disconnect GleambookUsers bucket failed")

    def create_cbas_indexes(self):
        #create Indexes
        status, metrics, errors, results, _ = self.cbas_util.execute_statement_on_cbas_util(
            "CREATE INDEX usrSinceIdx ON `GleambookUsers_ds`(user_since: string);",timeout=3600,analytics_timeout=3600)
        self.assertTrue(status == "success", "Create Index query failed")

            
    def validate_items_count(self):
        items_GleambookUsers = RestConnection(self.query_node).query_tool('select count(*) from GleambookUsers')['results'][0]['$1']
        self.log.info("Items in CB GleanBookUsers bucket: %s"%items_GleambookUsers)
        
        self.sleep(60)
        result = False
        tries = 100
        while tries>0:
            try:
                items_GleambookUsers = RestConnection(self.query_node).query_tool('select count(*) from GleambookUsers')['results'][0]['$1']
                self.log.info("Items in CB GleanBookUsers bucket: %s"%items_GleambookUsers)
                result = self.cbas_util.validate_cbas_dataset_items_count("GleambookUsers_ds",items_GleambookUsers, num_tries=100,timeout=600,analytics_timeout=600)
                break
            except:
                pass
            tries -= 1
            self.sleep(60, "CBAS count query is throwing exceptions. Wait for 60s and try again.")
            
        self.assertTrue(result,"No. of items in GleambookUsers dataset do not match that in the CB bucket")
        
    def test_analytics_volume(self):
        queries = ['SELECT VALUE u FROM `GleambookUsers_ds` u WHERE u.user_since >= "2010-09-13T16-48-15" AND u.user_since < "2010-10-13T16-48-15" AND (SOME e IN u.employment SATISFIES e.end_date IS UNKNOWN) LIMIT 100;',
           'SELECT VALUE u FROM `GleambookUsers_ds` u WHERE u.user_since >= "2010-11-13T16-48-15" AND u.user_since < "2010-12-13T16-48-15" limit 1;',
           ]
        nodes_in_cluster= [self.servers[0],self.cbas_node]
        print "Start Time: %s"%str(time.strftime("%H:%M:%S", time.gmtime(time.time())))
        
        ########################################################################################################################
        self.log.info("Step 1: Start the test with 2 KV and 2 CBAS nodes")

        self.log.info("Add a N1QL/Index nodes")
        self.query_node = self.servers[1]
        rest = RestConnection(self.query_node)
        rest.set_data_path(data_path=self.query_node.data_path,index_path=self.query_node.index_path,cbas_path=self.query_node.cbas_path)
        result = self.cluster_util.add_node(self.query_node, rebalance=False)
        self.assertTrue(result, msg="Failed to add N1QL/Index node.")
        self.log.info("Add a KV nodes")
        rest = RestConnection(self.cluster.kv_nodes[1])
        rest.set_data_path(data_path=self.cluster.kv_nodes[1].data_path,index_path=self.cluster.kv_nodes[1].index_path,cbas_path=self.cluster.kv_nodes[1].cbas_path)
        result = self.cluster_util.add_node(self.cluster.kv_nodes[1], services=["kv"], rebalance=False)
        self.assertTrue(result, msg="Failed to add KV node.")

        self.log.info("Add one more KV node")
        rest = RestConnection(self.cluster.kv_nodes[3])
        rest.set_data_path(data_path=self.cluster.kv_nodes[3].data_path,index_path=self.cluster.kv_nodes[3].index_path,cbas_path=self.cluster.kv_nodes[3].cbas_path)
        result = self.cluster_util.add_node(self.cluster.kv_nodes[3], services=["kv"], rebalance=False)
        self.assertTrue(result, msg="Failed to add KV node.")

        self.log.info("Add one more KV node")
        rest = RestConnection(self.cluster.kv_nodes[4])
        rest.set_data_path(data_path=self.cluster.kv_nodes[4].data_path,index_path=self.cluster.kv_nodes[4].index_path,cbas_path=self.cluster.kv_nodes[4].cbas_path)
        result = self.cluster_util.add_node(self.cluster.kv_nodes[4], services=["kv"], rebalance=False)
        self.assertTrue(result, msg="Failed to add KV node.")
                 
        self.log.info("Add a CBAS nodes")
        result = self.cluster_util.add_node(self.cluster.cbas_nodes[0], services=["cbas"], rebalance=True)
        self.assertTrue(result, msg="Failed to add CBAS node.")
         
        nodes_in_cluster = nodes_in_cluster + [self.query_node, self.cluster.kv_nodes[1], self.cluster.kv_nodes[3], self.cluster.kv_nodes[4], self.cluster.cbas_nodes[0]]
        ########################################################################################################################
        self.log.info("Step 2: Create Couchbase buckets.")
        self.create_required_buckets()
        
        ########################################################################################################################
        self.log.info("Step 3: Create 10M docs average of 1k docs for 8 couchbase buckets.")
        
        GleambookUsers = buck(name="GleambookUsers", authType=None, saslPassword=None,
                            num_replicas=self.num_replicas,
                            bucket_size=self.bucket_size,
                            eviction_policy='noEviction', lww=self.lww)
        
        items_start_from = 0
        total_num_items = self.input.param("num_items",1000000)
        num_query = self.input.param("num_query",240)

        self.use_replica_to = False
        self.rate_limit = self.input.param('rate_limit', '100000')
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()
        load_thread.join()
        
        updates_from = items_start_from
        deletes_from = items_start_from + total_num_items/10
        items_start_from += total_num_items
        ########################################################################################################################
        self.log.info("Step 4: Create 8 analytics buckets and 8 datasets and connect.")
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()
        
        items_start_from += total_num_items
        self.setup_cbas()
        load_thread.join()
                 
        ########################################################################################################################
        self.log.info("Step 5: Wait for ingestion to complete.")
        self.sleep(10,"Wait for the ingestion to complete")
         
        ########################################################################################################################
        self.log.info("Step 6: Verify the items count.")
        self.validate_items_count()
        
        ########################################################################################################################
        self.log.info("Step 7: Disconnect CBAS bucket and create secondary indexes.")
        self.disconnect_cbas_buckets()
        self.create_cbas_indexes()
         
        ########################################################################################################################
        self.log.info("Step 8: Delete 1M docs. Update 1M docs.")
        pool = Executors.newFixedThreadPool(5)
        num_items = self.input.param("num_items",5000)
        executors=[]
        query_executors = 1
        num_executors = query_executors

        upsert_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10,10000,4, updates_from,1, 0))
        delete_thread = Thread(target=self.delete_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10, self.rate_limit, 10000, 2, deletes_from,1))
        delete_thread.start()
        upsert_thread.start()
        
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        
        delete_thread.join()
        upsert_thread.join()
        
        ########################################################################################################################
        self.log.info("Step 9: Connect cbas buckets.")
        self.connect_cbas_buckets()
        self.sleep(10,"Wait for the ingestion to complete")
         
        ########################################################################################################################
        self.log.info("Step 10: Verify the items count.")
        self.validate_items_count()
         
        ########################################################################################################################
        self.log.info("Step 12: When 11 is in progress do a KV Rebalance in of 1 nodes.")
        rest = RestConnection(self.cluster.kv_nodes[2])
        rest.set_data_path(data_path=self.cluster.kv_nodes[2].data_path,index_path=self.cluster.kv_nodes[2].index_path,cbas_path=self.cluster.kv_nodes[2].cbas_path)
        rebalance = self.cluster.async_rebalance(nodes_in_cluster, [self.cluster.kv_nodes[2]], [])
        nodes_in_cluster += [self.cluster.kv_nodes[2]]
        ########################################################################################################################
        self.log.info("Step 11: Create 10M docs.")
        pool = Executors.newFixedThreadPool(5)
        total_num_items = self.input.param("num_items",1000000)
        num_query = self.input.param("num_query",240)

        self.use_replica_to = False
        self.rate_limit = self.input.param('rate_limit', '100000')
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()

        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        load_thread.join()
        
        updates_from = items_start_from
        deletes_from = items_start_from + total_num_items/10
        items_start_from += total_num_items
                 
        ########################################################################################################################
        self.log.info("Step 13: Wait for rebalance to complete.")
        
        reached = RestHelper(self.rest).rebalance_reached(wait_step=120)
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        self.sleep(20)
         
        ########################################################################################################################
        self.log.info("Step 14: Verify the items count.")
        self.validate_items_count()
         
        ########################################################################################################################
        self.log.info("Step 15: Delete 1M docs. Update 1M docs.")
        pool = Executors.newFixedThreadPool(5)
        num_items = self.input.param("num_items",5000)
        executors=[]
        query_executors = 1
        num_executors = query_executors

        upsert_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10,10000,4, updates_from,1, 0))
        delete_thread = Thread(target=self.delete_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10, self.rate_limit, 10000, 2, deletes_from,1))
        delete_thread.start()
        upsert_thread.start()
        
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        
        delete_thread.join()
        upsert_thread.join()
        ########################################################################################################################
        self.log.info("Step 16: Verify Results that 1M docs gets deleted from analytics datasets.")
        self.validate_items_count()
         
        ########################################################################################################################
        self.log.info("Step 17: Disconnect CBAS buckets.")
        self.disconnect_cbas_buckets()
         
        ########################################################################################################################
        self.log.info("Step 18: Create 10M docs.")
        pool = Executors.newFixedThreadPool(5)
        total_num_items = self.input.param("num_items",1000000)
        num_query = self.input.param("num_query",240)

        self.use_replica_to = False
        self.rate_limit = self.input.param('rate_limit', '100000')
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()

        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        load_thread.join()
 
        updates_from = items_start_from
        deletes_from = items_start_from + total_num_items/10
        items_start_from += total_num_items
         
        ########################################################################################################################
        self.log.info("Step 19: Multiple Connect/Disconnect CBAS buckets during ingestion in step 18.")
        self.connect_cbas_buckets()
        self.sleep(5)
        self.disconnect_cbas_buckets()
        self.connect_cbas_buckets()
        self.sleep(5)
        self.disconnect_cbas_buckets()
        self.connect_cbas_buckets()
        self.sleep(5)
        self.disconnect_cbas_buckets()
        self.connect_cbas_buckets()
        ########################################################################################################################
        self.log.info("Step 20: Verify the docs count.")
        self.validate_items_count()
         
        ########################################################################################################################
        self.log.info("Step 21: Run 500 complex queries concurrently and verify the results.")
        pool = Executors.newFixedThreadPool(5)
        num_query = self.input.param("num_query",500)
        executors=[]
        num_executors = 5
        query_executors = num_executors
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
         
        self.log.info("Step 22: When 21 is in progress do a KV Rebalance out of 2 nodes.")
        rebalance = self.cluster.async_rebalance(nodes_in_cluster, [], self.cluster.kv_nodes[1:2])
        nodes_in_cluster = [node for node in nodes_in_cluster if node not in self.cluster.kv_nodes[1:2]]
         
        futures = pool.invokeAll(executors)
        self.log.info("Step 23: Wait for rebalance.")
        
        reached = RestHelper(self.rest).rebalance_reached(wait_step=120)
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        self.sleep(20)
         
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
         
        ########################################################################################################################
        self.log.info("Step 24: Create 10M docs.")
        pool = Executors.newFixedThreadPool(5)
        executors=[]
        num_executors = 2
        query_executors = num_executors

        total_num_items = self.input.param("num_items",1000000)
        num_query = self.input.param("num_query",240)
        
        self.use_replica_to = False
        self.rate_limit = self.input.param('rate_limit', '100000')
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
         
        self.log.info("Step 26: Run 500 complex queries concurrently and verify the results.")
        executors.append(QueryRunner(random.choice(queries),500,self.cbas_util))
         
         
        ##################################################### NEED TO BE UPDATED ##################################################################
        self.log.info("Step 25: When 24 is in progress do a CBAS Rebalance in of 2 nodes.")
        for node in self.cluster.cbas_nodes[2:]:
            rest = RestConnection(node)
            rest.set_data_path(data_path=node.data_path,index_path=node.index_path,cbas_path=node.cbas_path)
        rebalance = self.cluster.async_rebalance(nodes_in_cluster, self.cluster.cbas_nodes[1:],[],services=["cbas","cbas"])
        nodes_in_cluster = nodes_in_cluster + self.cluster.cbas_nodes[1:]
        futures = pool.invokeAll(executors)
        
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        load_thread.join()
        
        updates_from = items_start_from
        deletes_from = items_start_from + total_num_items/10
        items_start_from += total_num_items
 
        self.log.info("Step 27: Wait for rebalance to complete.")
        
        reached = RestHelper(self.rest).rebalance_reached(wait_step=120)
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
         
        ########################################################################################################################
        self.log.info("Step 28: Verify the docs count.")
        self.validate_items_count()
         
        ########################################################################################################################
        self.log.info("Step 29: Delete 1M docs. Update 1M docs.")
        pool = Executors.newFixedThreadPool(5)
        num_items = self.input.param("num_items",5000)
        executors=[]
        query_executors = 1
        num_executors = query_executors

        upsert_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10,10000,4, updates_from,1, 0))
        delete_thread = Thread(target=self.delete_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10, self.rate_limit, 10000, 2, deletes_from,1))
        delete_thread.start()
        upsert_thread.start()
        
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        
        delete_thread.join()
        upsert_thread.join()
        ########################################################################################################################
        self.log.info("Step 30: Verify the docs count.")
        self.validate_items_count()
 
        ########################################################################################################################
        self.log.info("Step 31: Create 10M docs.")
        pool = Executors.newFixedThreadPool(5)
        total_num_items = self.input.param("num_items",1000000)
        num_query = self.input.param("num_query",240)

        self.use_replica_to = False
        self.rate_limit = self.input.param('rate_limit', '100000')
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()

        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
 
        ###################################################### NEED TO BE UPDATED ##################################################################
        self.log.info("Step 32: When 31 is in progress do a CBAS Rebalance out of 1 nodes.")
        rebalance = self.cluster.async_rebalance(nodes_in_cluster, [], self.cluster.cbas_nodes[-1:])
        nodes_in_cluster = [node for node in nodes_in_cluster if node not in self.cluster.cbas_nodes[-1:]]
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        load_thread.join()
 
        updates_from = items_start_from
        deletes_from = items_start_from + total_num_items/10
        items_start_from += total_num_items
        #######################################################################################################################
        self.log.info("Step 33: Wait for rebalance to complete.")
        
        reached = RestHelper(self.rest).rebalance_reached(wait_step=120)
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        self.sleep(20)
 
        ########################################################################################################################
        self.log.info("Step 34: Verify the docs count.")
        self.validate_items_count()
         
        ########################################################################################################################
        self.log.info("Step 35: Delete 1M docs. Update 1M docs.")
        pool = Executors.newFixedThreadPool(5)
        num_items = self.input.param("num_items",5000)
        executors=[]
        query_executors = 1
        num_executors = query_executors

        upsert_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10,10000,4, updates_from,1, 0))
        delete_thread = Thread(target=self.delete_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10, self.rate_limit, 10000, 2, deletes_from,1))
        delete_thread.start()
        upsert_thread.start()
        
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        
        delete_thread.join()
        upsert_thread.join()
        ########################################################################################################################
        self.log.info("Step 36: Verify the docs count.")
        self.validate_items_count()
         
        ########################################################################################################################
        self.log.info("Step 37: Create 10M docs.")
        pool = Executors.newFixedThreadPool(5)
        total_num_items = self.input.param("num_items",1000000)
        num_query = self.input.param("num_query",240)

        self.use_replica_to = False
        self.rate_limit = self.input.param('rate_limit', '100000')
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()

        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
         
        ###################################################### NEED TO BE UPDATED ##################################################################
        self.log.info("Step 38: When 37 is in progress do a CBAS CC SWAP Rebalance of 2 nodes.")
        
        for node in self.cluster.cbas_nodes[-1:]:
            rest = RestConnection(node)
            rest.set_data_path(data_path=node.data_path,index_path=node.index_path,cbas_path=node.cbas_path)
        rebalance = self.cluster.async_rebalance(nodes_in_cluster,self.cluster.cbas_nodes[-1:], [self.cbas_node],services=["cbas"],check_vbucket_shuffling=False)
        nodes_in_cluster += self.cluster.cbas_nodes[-1:]
        nodes_in_cluster.remove(self.cbas_node)
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        load_thread.join()
        
        ########################################################################################################################
        self.log.info("Step 39: Wait for rebalance to complete.")
        
        reached = RestHelper(self.rest).rebalance_reached(wait_step=120)
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        self.sleep(20)
         
        updates_from = items_start_from
        deletes_from = items_start_from + total_num_items/10
        items_start_from += total_num_items
         
        ########################################################################################################################
        self.log.info("Step 40: Verify the docs count.")
        self.validate_items_count()
 
        ########################################################################################################################
        self.log.info("Step 41: Delete 1M docs. Update 1M docs.")
        pool = Executors.newFixedThreadPool(5)
        num_items = self.input.param("num_items",5000)
        executors=[]
        query_executors = 1
        num_executors = query_executors

        upsert_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10,10000,4, updates_from,1, 0))
        delete_thread = Thread(target=self.delete_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10, self.rate_limit, 10000, 2, deletes_from,1))
        delete_thread.start()
        upsert_thread.start()
        
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        
        delete_thread.join()
        upsert_thread.join()
        ########################################################################################################################
        self.log.info("Step 42: Verify the docs count.")
        self.validate_items_count() 
         
        ########################################################################################################################
        self.log.info("Step 43: Create 10M docs.")
        pool = Executors.newFixedThreadPool(5)
        total_num_items = self.input.param("num_items",1000000)
        num_query = self.input.param("num_query",240)

        self.use_replica_to = False
        self.rate_limit = self.input.param('rate_limit', '100000')
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()

        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
         
        ###################################################### NEED TO BE UPDATED ##################################################################
        self.log.info("Step 44: When 43 is in progress do a KV+CBAS Rebalance IN.")
        rest = RestConnection(self.cbas_node)
        rest.set_data_path(data_path=self.cbas_node.data_path,index_path=self.cbas_node.index_path,cbas_path=self.cbas_node.cbas_path)
        rebalance = self.cluster.async_rebalance(nodes_in_cluster, [self.cbas_node], [],services=["cbas"])
        nodes_in_cluster += [self.cbas_node]
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        rest = RestConnection(self.cluster.kv_nodes[1])
        rest.set_data_path(data_path=self.cluster.kv_nodes[1].data_path,index_path=self.cluster.kv_nodes[1].index_path,cbas_path=self.cluster.kv_nodes[1].cbas_path)
        rebalance = self.cluster.async_rebalance(nodes_in_cluster, [self.cluster.kv_nodes[1]], [])
        nodes_in_cluster += [self.cluster.kv_nodes[1]]
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        load_thread.join()
        ########################################################################################################################
        self.log.info("Step 45: Wait for rebalance to complete.")
        
        reached = RestHelper(self.rest).rebalance_reached(wait_step=120)
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        self.sleep(20)
         
        updates_from = items_start_from
        deletes_from = items_start_from + total_num_items/10
        items_start_from += total_num_items        
         
        ########################################################################################################################
        self.log.info("Step 46: Verify the docs count.")
        self.validate_items_count() 
         
        ########################################################################################################################
        self.log.info("Step 47: Delete 1M docs. Update 1M docs.")
        pool = Executors.newFixedThreadPool(5)
        num_items = self.input.param("num_items",5000)
        executors=[]
        query_executors = 1
        num_executors = query_executors

        upsert_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10,10000,4, updates_from,1, 0))
        delete_thread = Thread(target=self.delete_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10, self.rate_limit, 10000, 2, deletes_from,1))
        delete_thread.start()
        upsert_thread.start()
        
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        
        delete_thread.join()
        upsert_thread.join()
        ########################################################################################################################
        self.log.info("Step 48: Verify the docs count.")
        self.validate_items_count() 
 
        ########################################################################################################################
        self.log.info("Step 49: Create 10M docs.")
        pool = Executors.newFixedThreadPool(5)
        total_num_items = self.input.param("num_items",1000000)
        num_query = self.input.param("num_query",240)

        self.use_replica_to = False
        self.rate_limit = self.input.param('rate_limit', '100000')
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()

        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
         
        ########################################################################################################################
        self.log.info("Step 50: When 49 is in progress do a CBAS Rebalance OUT.")
        rest = RestConnection(self.cluster.kv_nodes[2])
#         rest.set_data_path(data_path=self.cluster.kv_nodes[2].data_path,index_path=self.cluster.kv_nodes[2].index_path,cbas_path=self.cluster.kv_nodes[2].cbas_path)
        rebalance = self.cluster.async_rebalance(nodes_in_cluster, [], self.cluster.cbas_nodes[-1:])
#         
        nodes_in_cluster = [node for node in nodes_in_cluster if node not in self.cluster.cbas_nodes[-1:]]
#         nodes_in_cluster.remove(self.cluster.kv_nodes[1])
        
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        load_thread.join()
        ########################################################################################################################
        self.log.info("Step 51: Wait for rebalance to complete.")
        
        reached = RestHelper(self.rest).rebalance_reached(wait_step=120)
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        self.sleep(20)
        updates_from = items_start_from
        deletes_from = items_start_from + total_num_items/10
        items_start_from += total_num_items  
 
        ########################################################################################################################
        self.log.info("Step 52: Verify the docs count.")
        self.validate_items_count() 
 
        ########################################################################################################################
        self.log.info("Step 53: Delete 1M docs. Update 1M docs.")
        pool = Executors.newFixedThreadPool(5)
        num_items = self.input.param("num_items",5000)
        executors=[]
        query_executors = 1
        num_executors = query_executors

        upsert_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10,10000,4, updates_from,1, 0))
        delete_thread = Thread(target=self.delete_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10, self.rate_limit, 10000, 2, deletes_from,1))
        delete_thread.start()
        upsert_thread.start()
        
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        
        delete_thread.join()
        upsert_thread.join()
        ########################################################################################################################
        self.log.info("Step 54: Verify the docs count.")
        self.validate_items_count() 
         
         
        ########################################################################################################################
        self.log.info("Step 55: Create 10M docs.")
        pool = Executors.newFixedThreadPool(5)
        total_num_items = self.input.param("num_items",1000000)
        num_query = self.input.param("num_query",240)

        self.use_replica_to = False
        self.rate_limit = self.input.param('rate_limit', '100000')
        load_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_load",
                                 args=(self.cluster.master, GleambookUsers, total_num_items,50,4, items_start_from,2, 0))
        self.log.info('starting the load thread...')
        load_thread.start()

        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
         
        ########################################################################################################################
        self.log.info("Step 56: When 55 is in progress do a CBAS Rebalance IN.")
        for node in self.cluster.cbas_nodes[-1:]:
            rest = RestConnection(node)
            rest.set_data_path(data_path=node.data_path,index_path=node.index_path,cbas_path=node.cbas_path)
        rest = RestConnection(self.cluster.cbas_nodes[-1])
        rest.set_data_path(data_path=self.cluster.cbas_nodes[-1].data_path,index_path=self.cluster.cbas_nodes[-1].index_path,cbas_path=self.cluster.cbas_nodes[-1].cbas_path)
        rebalance = self.cluster.async_rebalance(nodes_in_cluster, self.cluster.cbas_nodes[-1:], [], services=["cbas"])
        nodes_in_cluster += self.cluster.cbas_nodes[-1:]
         
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        load_thread.join()
        ########################################################################################################################
        self.log.info("Step 57: Wait for rebalance to complete.")
        
        reached = RestHelper(self.rest).rebalance_reached(wait_step=120)
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        self.sleep(20)
         
        updates_from = items_start_from
        deletes_from = items_start_from + total_num_items/10
        items_start_from += total_num_items  
 
        ########################################################################################################################
        self.log.info("Step 58: Verify the docs count.")
        self.validate_items_count() 
         
        ########################################################################################################################
        self.log.info("Step 59: Delete 1M docs. Update 1M docs.")
        pool = Executors.newFixedThreadPool(5)
        num_items = self.input.param("num_items",5000)
        executors=[]
        query_executors = 1
        num_executors = query_executors

        upsert_thread = Thread(target=self.load_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10,10000,4, updates_from,1, 0))
        delete_thread = Thread(target=self.delete_buckets_with_high_ops,
                                 name="high_ops_delete",
                                 args=(self.cluster.master, GleambookUsers, num_items/10, self.rate_limit, 10000, 2, deletes_from,1))
        delete_thread.start()
        upsert_thread.start()
        
        for i in xrange(query_executors):
            executors.append(QueryRunner(random.choice(queries),num_query,self.cbas_util))
        futures = pool.invokeAll(executors)
        for future in futures:
            print future.get(num_executors, TimeUnit.SECONDS)
        print "Executors completed!!"
        shutdown_and_await_termination(pool, num_executors)
        
        delete_thread.join()
        upsert_thread.join()
        ########################################################################################################################
        self.log.info("Step 60: Verify the docs count.")
        self.validate_items_count() 
                 
 
        print "End Time: %s"%str(time.strftime("%H:%M:%S", time.gmtime(time.time())))

