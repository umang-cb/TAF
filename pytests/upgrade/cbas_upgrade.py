'''
Created on 04-Mar-2021

@author: Umang

Very important note - 
Number of datasets in <= 6.5.0 should not be more than 8.
'''

from math import ceil

from BucketLib.bucket import Bucket
from Cb_constants import DocLoading, CbServer
from collections_helper.collections_spec_constants import MetaConstants, \
    MetaCrudParams
from couchbase_helper.documentgenerator import doc_generator
from sdk_exceptions import SDKException
from upgrade.upgrade_base import UpgradeBase
from cbas_utils.cbas_utils_v2 import CbasUtil
from membase.api.rest_client import RestConnection
from BucketLib.BucketOperations import BucketHelper


class UpgradeTests(UpgradeBase):

    def setUp(self):
        super(UpgradeTests, self).setUp()
        cluster_cbas_nodes = self.cluster_util.get_nodes_from_services_map(
            service_type="cbas", get_all_nodes=True,
            servers=self.cluster.nodes_in_cluster,
            master=self.cluster.master)
        self.cbas_util = CbasUtil(
            self.cluster.master, cluster_cbas_nodes[0], self.task)
        self.cbas_spec_name = self.input.param("cbas_spec", "local_datasets")
        self.pre_upgrade_setup()

    def tearDown(self):
        super(UpgradeTests, self).tearDown()

    def pre_upgrade_setup(self):
        update_spec = {
            "dataverse": {
                "no_of_dataverses": 2,
                "no_of_datasets_per_dataverse": 4,
                "no_of_synonyms": 0,
                "no_of_indexes": 3,
                "max_thread_count": self.input.param('no_of_threads', 10),
                "cardinality": 1,
                "creation_method": "dataverse"
            },
            "dataset": {
                "creation_methods": ["cbas_dataset"],
                "bucket_cardinality": 1
            },
            "index": {
                "creation_method": "index"
            }
        }
        if not self.cbas_setup(update_spec):
            self.fail("Pre Upgrade CBAS setup failed")

    def cbas_setup(self, update_spec, connect_local_link=True):
        if self.cbas_spec_name:
            self.cbas_spec = self.cbas_util.get_cbas_spec(
                self.cbas_spec_name)
            for spec_name in update_spec:
                self.cbas_util.update_cbas_spec(
                    self.cbas_spec, update_spec[spec_name], spec_name)
            cbas_infra_result = self.cbas_util.create_cbas_infra_from_spec(
                self.cbas_spec, self.bucket_util, wait_for_ingestion=False)
            if not cbas_infra_result[0]:
                self.log.error(
                    "Error while creating infra from CBAS spec -- {0}".format(
                        cbas_infra_result[1]))
                return False
        
        if connect_local_link:
            for dataverse in self.cbas_util.dataverses:
                if not self.cbas_util.connect_link(".".join([dataverse,"Local"])):
                    self.log.error(
                        "Failed to connect Local link for dataverse - {0}".format(
                            dataverse))
                    return False
        if not self.cbas_util.wait_for_ingestion_all_datasets(
                self.bucket_util):
            self.log.error("Data ingestion did not happen in the datasets")
            return False
        return True

    def post_upgrade_validation(self):
        # rebalance once again to activate CBAS service
        rest = RestConnection(self.cluster.master)
        otp_nodes = [node.id for node in rest.node_statuses()]
        rest.rebalance(otpNodes=otp_nodes, ejectedNodes=[])
        rebalance_passed = rest.monitorRebalance()
        if not rebalance_passed:
            self.log_failure("Rebalance operation Failed")
            return False
        
        # Update RAM quota allocated to buckets created before upgrade
        cluster_info = rest.get_nodes_self()
        kv_quota = cluster_info.__getattribute__("memoryQuota")
        bucket_size = kv_quota // (self.input.param("num_buckets", 1) + 1)
        for bucket in self.bucket_util.buckets:
            self.bucket_util.update_bucket_property(bucket,bucket_size)
        
        validation_results = {}
        cluster_cbas_nodes = self.cluster_util.get_nodes_from_services_map(
            service_type="cbas", get_all_nodes=True,
            servers=self.cluster.nodes_in_cluster,
            master=self.cluster.master)
        pre_upgrade_cbas_entities = self.cbas_util.dataverses
        self.cbas_util = CbasUtil(
            self.cluster.master, cluster_cbas_nodes[0], self.task)
        self.cbas_util.dataverses = pre_upgrade_cbas_entities

        self.log.info("Validating pre upgrade cbas infra")
        results = list()
        for dataverse in self.cbas_util.dataverses:
            results.append(
                self.cbas_util.validate_dataverse_in_metadata(dataverse))
        for dataset in self.cbas_util.list_all_dataset_objs(
                dataset_source="internal"):
            results.append(
                self.cbas_util.validate_dataset_in_metadata(
                    dataset_name=dataset.name,
                    dataverse_name=dataset.dataverse_name))
            results.append(
                self.cbas_util.validate_cbas_dataset_items_count(
                    dataset_name=dataset.full_name,
                    expected_count=dataset.num_of_items))
        for index in self.cbas_util.list_all_index_objs():
            results.append(
                self.cbas_util.verify_index_created(
                    index_name=index.name, dataset_name=index.dataset_name,
                    indexed_fields=index.indexed_fields))
            results.append(
                self.cbas_util.verify_index_used(
                    statement="SELECT VALUE v FROM {0} v WHERE age > 2".format(
                        index.full_dataset_name),
                    index_used=True, index_name=index.name))
        validation_results["pre_upgrade"] = all(results)

        self.log.info("Loading docs in default collection of existing buckets")
        for bucket in self.bucket_util.buckets:
            gen_load = doc_generator(
                self.key, self.num_items, self.num_items*2,
                randomize_doc_size=True, randomize_value=True, randomize=True)
            async_load_task = self.task.async_load_gen_docs(
                self.cluster, bucket, gen_load,
                DocLoading.Bucket.DocOps.CREATE,
                active_resident_threshold=self.active_resident_threshold,
                timeout_secs=self.sdk_timeout,
                process_concurrency=8,
                batch_size=500,
                sdk_client_pool=self.sdk_client_pool)
            self.task_manager.get_task_result(async_load_task)

            # Update num_items in case of DGM run
            if self.active_resident_threshold != 100:
                self.num_items = async_load_task.doc_index

            bucket.scopes[CbServer.default_scope].collections[
                CbServer.default_collection].num_items = self.num_items * 2

            # Verify doc load count
            self.bucket_util._wait_for_stats_all_buckets()
            self.sleep(30, "Wait for num_items to get reflected")
            current_items = self.bucket_util.get_bucket_current_item_count(
                self.cluster, bucket)
            if current_items == self.num_items * 2:
                validation_results["post_upgrade_data_load"] = True
            else:
                self.log.error(
                    "Mismatch in doc_count. Actual: %s, Expected: %s"
                    % (current_items, self.num_items*2))
                validation_results["post_upgrade_data_load"] = False
        self.bucket_util.print_bucket_stats()
        if not self.cbas_util.wait_for_ingestion_all_datasets(self.bucket_util):
            validation_results["post_upgrade_data_load"] = False
            self.log.error("Data ingestion did not happen in the datasets")
        else:
            validation_results["post_upgrade_data_load"] = True
        
        self.log.info("Deleting all the data from default collection of buckets created before upgrade")
        for bucket in self.bucket_util.buckets:
            gen_load = doc_generator(
                self.key, 0, self.num_items*2,
                randomize_doc_size=True, randomize_value=True, randomize=True)
            async_load_task = self.task.async_load_gen_docs(
                self.cluster, bucket, gen_load,
                DocLoading.Bucket.DocOps.DELETE,
                active_resident_threshold=self.active_resident_threshold,
                timeout_secs=self.sdk_timeout,
                process_concurrency=8,
                batch_size=500,
                sdk_client_pool=self.sdk_client_pool)
            self.task_manager.get_task_result(async_load_task)
            
            # Verify doc load count
            self.bucket_util._wait_for_stats_all_buckets()
            while True:
                current_items = self.bucket_util.get_bucket_current_item_count(
                    self.cluster, bucket)
                if current_items == 0:
                    break
                else:
                    self.sleep(30, "Wait for num_items to get reflected")
        
        bucket.scopes[CbServer.default_scope].collections[
                CbServer.default_collection].num_items = self.num_items

        self.log.info("Creating scopes and collections in existing bucket")
        scope_spec={"name":self.cbas_util.generate_name()}
        self.bucket_util.create_scope_object(bucket, scope_spec)
        collection_spec={"name":self.cbas_util.generate_name(),
                         "num_items":self.num_items}
        self.bucket_util.create_collection_object(
            bucket, scope_spec["name"], collection_spec)
        bucket_helper = BucketHelper(self.cluster.master)
        
        status, content = bucket_helper.create_scope(self.bucket.name, scope_spec["name"])
        if status is False:
            self.fail(
                "Create scope failed for %s:%s, Reason - %s" % (
                    self.bucket.name, scope_spec["name"], content))
        self.bucket.stats.increment_manifest_uid()
        status, content = bucket_helper.create_collection(
            self.bucket.name, scope_spec["name"], collection_spec)
        if status is False:
            self.fail("Create collection failed for %s:%s:%s, Reason - %s"
                      % (self.bucket.name, scope_spec["name"], collection_spec["name"], content))
        self.bucket.stats.increment_manifest_uid()
        
        self.log.info("Creating new buckets with scopes and collections")
        self.bucket_spec_name = self.input.param(
            "bucket_spec", "analytics.default")
        self.buckets_spec = self.bucket_util.get_bucket_template_from_package(
            self.bucket_spec_name)
        self.doc_spec_name = self.input.param("doc_spec", "initial_load")
        self.collectionSetUp(True, self.buckets_spec)

        self.log.info("Create CBAS infra post upgrade and check for data "
                      "ingestion")
        update_spec = {
            "dataverse": {
                "no_of_dataverses": self.input.param('no_of_dv', 2),
                "no_of_datasets_per_dataverse": self.input.param('ds_per_dv',
                                                                 4),
                "no_of_synonyms": self.input.param('no_of_synonym', 2),
                "no_of_indexes": self.input.param('no_of_index', 3),
                "max_thread_count": self.input.param('no_of_threads', 10),
            }
        }
        if self.cbas_setup(update_spec, False):
            validation_results["post_upgrade_cbas_infra"] = True
        else:
            validation_results["post_upgrade_cbas_infra"] = False

        self.log.info("Delete the bucket created before upgrade")
        if self.bucket_util.delete_bucket(
                self.cluster.master, self.bucket, wait_for_bucket_deletion=True):
            validation_results["bucket_delete"] = True
        else:
            validation_results["bucket_delete"] = False

        if validation_results["bucket_delete"]:
            self.log.info("Check all datasets created on the deleted bucket "
                          "are empty")
            results = []
            for dataset in self.cbas_util.list_all_dataset_objs(
                    dataset_source="internal"):
                if dataset.kv_bucket.name == self.bucket.name:
                    if self.cbas_util.wait_for_ingestion_complete(
                            [dataset.full_name], 0, timeout=300):
                        results.append(True)
                    else:
                        results.append(False)
            validation_results["bucket_delete"] = all(results)

        return validation_results

    def collectionSetUp(self, load_data=True, buckets_spec=None,
                        doc_loading_spec=None):
        """
        Setup the buckets, scopes and collecitons based on the spec passed.
        """
        self.over_ride_spec_params = self.input.param(
            "override_spec_params", "").split(";")
        self.remove_default_collection = self.input.param(
            "remove_default_collection", False)

        if not buckets_spec:
            buckets_spec = self.bucket_util.get_bucket_template_from_package(
                self.bucket_spec_name)

        # Process params to over_ride values if required
        self.over_ride_bucket_template_params(buckets_spec)

        self.bucket_util.create_buckets_using_json_data(
            buckets_spec, ignore_existing_buckets=True)
        self.bucket_util.wait_for_collection_creation_to_complete()

        # Prints bucket stats before doc_ops
        self.bucket_util.print_bucket_stats()

        # Init sdk_client_pool if not initialized before
        if self.sdk_client_pool is None:
            self.init_sdk_pool_object()

        # Create clients in SDK client pool
        if self.sdk_client_pool:
            self.log.info("Creating required SDK clients for client_pool")
            bucket_count = len(self.bucket_util.buckets)
            max_clients = self.task_manager.number_of_threads
            clients_per_bucket = int(ceil(max_clients / bucket_count))
            for bucket in self.bucket_util.buckets:
                self.sdk_client_pool.create_clients(
                    bucket, [self.cluster.master], clients_per_bucket,
                    compression_settings=self.sdk_compression)

        # TODO: remove this once the bug is fixed
        # self.sleep(120, "MB-38497")
        self.sleep(10, "MB-38497")
        self.cluster_util.print_cluster_stats()
        if load_data:
            self.load_data_into_buckets(doc_loading_spec)

    def load_data_into_buckets(self, doc_loading_spec=None):
        """
        Loads data into buckets using the data spec
        """
        if not doc_loading_spec:
            doc_loading_spec = self.bucket_util.get_crud_template_from_package(
                self.doc_spec_name)
        self.over_ride_doc_loading_template_params(doc_loading_spec)
        # MB-38438, adding CollectionNotFoundException in retry exception
        doc_loading_spec[MetaCrudParams.RETRY_EXCEPTIONS].append(
            SDKException.CollectionNotFoundException)
        doc_loading_task = self.bucket_util.run_scenario_from_spec(
            self.task, self.cluster, self.bucket_util.buckets,
            doc_loading_spec, mutation_num=0, batch_size=self.batch_size)
        if doc_loading_task.result is False:
            self.fail("Initial reloading failed")
        ttl_buckets = [
            "multi_bucket.buckets_for_rebalance_tests_with_ttl",
            "multi_bucket.buckets_all_membase_for_rebalance_tests_with_ttl",
            "volume_templates.buckets_for_volume_tests_with_ttl"]
        # Verify initial doc load count
        self.bucket_util._wait_for_stats_all_buckets()
         
        if self.bucket_spec_name not in ttl_buckets:
            self.bucket_util.validate_docs_per_collections_all_buckets()

    def over_ride_bucket_template_params(self, bucket_spec):
        for over_ride_param in self.over_ride_spec_params:
            if over_ride_param == "replicas":
                bucket_spec[Bucket.replicaNumber] = self.num_replicas
            elif over_ride_param == "remove_default_collection":
                bucket_spec[MetaConstants.REMOVE_DEFAULT_COLLECTION] = \
                    self.remove_default_collection
            elif over_ride_param == "enable_flush":
                if self.input.param("enable_flush", False):
                    bucket_spec[
                        Bucket.flushEnabled] = Bucket.FlushBucket.ENABLED
                else:
                    bucket_spec[
                        Bucket.flushEnabled] = Bucket.FlushBucket.DISABLED
            elif over_ride_param == "num_buckets":
                bucket_spec[MetaConstants.NUM_BUCKETS] = int(
                    self.input.param("num_buckets", 1))
            elif over_ride_param == "bucket_size":
                if self.bucket_size == "auto":
                    cluster_info = self.rest.get_nodes_self()
                    kv_quota = cluster_info.__getattribute__("memoryQuota")
                    self.bucket_size = kv_quota // (bucket_spec[
                        MetaConstants.NUM_BUCKETS] + 1)
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

    def over_ride_doc_loading_template_params(self, target_spec):
        for over_ride_param in self.over_ride_spec_params:
            if over_ride_param == "durability":
                target_spec[MetaCrudParams.DURABILITY_LEVEL] = \
                    self.durability_level
            elif over_ride_param == "sdk_timeout":
                target_spec[MetaCrudParams.SDK_TIMEOUT] = self.sdk_timeout
            elif over_ride_param == "doc_size":
                target_spec[MetaCrudParams.DocCrud.DOC_SIZE] = self.doc_size

    def test_upgrade(self):
        self.log.info("Upgrading cluster nodes to target version")
        node_to_upgrade = self.fetch_node_to_upgrade()
        while node_to_upgrade is not None:
            self.log.info("Selected node for upgrade: %s"
                          % node_to_upgrade.ip)
            self.upgrade_function[self.upgrade_type](node_to_upgrade,
                                                     self.upgrade_version)
            self.cluster_util.print_cluster_stats()
            node_to_upgrade = self.fetch_node_to_upgrade()
        if not all(self.post_upgrade_validation().values()):
            self.fail("Post upgrade scenarios failed")
    
    def test_upgrade_with_failover(self):
        self.log.info("Upgrading cluster nodes to target version")
        node_to_upgrade = self.fetch_node_to_upgrade()
        while node_to_upgrade is not None:
            self.log.info("Selected node for upgrade: %s"
                          % node_to_upgrade.ip)
            if "cbas" in node_to_upgrade.services:
                self.upgrade_function["failover_full_recovery"](
                    node_to_upgrade, self.upgrade_version)
            else:
                self.upgrade_function[self.upgrade_type](
                    node_to_upgrade, self.upgrade_version)
            self.cluster_util.print_cluster_stats()
            node_to_upgrade = self.fetch_node_to_upgrade()
        if not all(self.post_upgrade_validation().values()):
            self.fail("Post upgrade scenarios failed")
