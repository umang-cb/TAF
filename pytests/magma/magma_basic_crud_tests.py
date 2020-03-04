import os
import random

from cb_tools.cbstats import Cbstats
from couchbase_helper.documentgenerator import doc_generator
from magma_base import MagmaBaseTest
from membase.api.rest_client import RestConnection
from memcached.helper.data_helper import MemcachedClientHelper
from remote.remote_util import RemoteMachineShellConnection
from sdk_exceptions import SDKException
import copy


class BasicCrudTests(MagmaBaseTest):
    def setUp(self):
        super(BasicCrudTests, self).setUp()

    def tearDown(self):
        super(BasicCrudTests, self).tearDown()
    
    def test_read_create_docs_parallelly(self):
        """
        Write and Read docs parallely , While reading we are using
        old doc generator (self.gen_create)
        using which we already created docs in magam_base
        for writing we are creating a new doc generator.
        Befor we start read, killing memcached to make sure,
        all reads happen from magma/storage
        """
        for bucket in self.bucket_util.get_all_buckets():
            self.log.info("Loading and Reading docs parallely")
            count = 0
            while count < 3:
                for node in self.cluster.nodes_in_cluster:
                    shell = RemoteMachineShellConnection(node)
                    shell.kill_memcached()
                    shell.disconnect()
                task_info = dict()
                self.doc_ops = "create:read"
                start = self.num_items
                end = self.num_items+self.num_items
                if self.rev_write:
                    start = -int(self.num_items+self.num_items -1)
                    end = -int(self.num_items - 1)
                self.gen_create = doc_generator(self.key, start, end,
                                        doc_size=self.doc_size,
                                        doc_type=self.doc_type,
                                        target_vbucket=self.target_vbucket,
                                        vbuckets=self.cluster_util.vbuckets,
                                        key_size=self.key_size,
                                        randomize_doc_size=self.randomize_doc_size,
                                        randomize_value=self.randomize_value,
                                        mix_key_size=self.mix_key_size)
                task_info = self.loadgen_docs(self.retry_exceptions,
                                                self.ignore_exceptions, _sync=True)
                for task in task_info:
                    self.task.jython_task_manager.get_task_result(task)
                self.log.info("Verifying doc counts after create doc_ops")
                self.bucket_util._wait_for_stats_all_buckets()
                self.bucket_util.verify_stats_all_buckets(self.num_items)
                self.gen_delete = copy.deepcopy(self.gen_create)
                task_info = dict()
                self.doc_ops = "delete"
                task_info = self.loadgen_docs(self.retry_exceptions,
                                                  self.ignore_exceptions)
                for task in task_info:
                    self.task.jython_task_manager.get_task_result(task)
                self.bucket_util.verify_stats_all_buckets(self.num_items)
                count += 1
        self.log.info("====test_read_create_docs_parallelly ends====")
        