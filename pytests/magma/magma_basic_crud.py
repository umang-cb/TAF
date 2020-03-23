from couchbase_helper.documentgenerator import doc_generator
from magma_base import MagmaBaseTest
from remote.remote_util import RemoteMachineShellConnection


class BasicCrudTests(MagmaBaseTest):
    def setUp(self):
        super(BasicCrudTests, self).setUp()

    def tearDown(self):
        super(BasicCrudTests, self).tearDown()

    def test_basic_create_read(self):
        """
        Write and Read docs parallely , While reading we are using
        old doc generator (self.gen_create)
        using which we already created docs in magam_base
        for writing we are creating a new doc generator.
        Befor we start read, killing memcached to make sure,
        all reads happen from magma/storage
        """
        self.log.info("Loading and Reading docs parallel")
        count = 0
        init_items = self.num_items
        while count < 4:
            for node in self.cluster.nodes_in_cluster:
                shell = RemoteMachineShellConnection(node)
                shell.kill_memcached()
                shell.disconnect()
            self.doc_ops = "create:read"
            start = self.num_items
            end = self.num_items+init_items
            start_read = self.num_items
            end_read = self.num_items+init_items
            if self.rev_write:
                start = -int(self.num_items+init_items - 1)
                end = -int(self.num_items - 1)
            if self.rev_read:
                start_read = -int(self.num_items+init_items - 1)
                end_read = -int(self.num_items - 1)
            self.gen_create = doc_generator(
                self.key, start, end,
                doc_size=self.doc_size,
                doc_type=self.doc_type,
                target_vbucket=self.target_vbucket,
                vbuckets=self.cluster_util.vbuckets,
                key_size=self.key_size,
                randomize_doc_size=self.randomize_doc_size,
                randomize_value=self.randomize_value,
                mix_key_size=self.mix_key_size)
            _ = self.loadgen_docs(self.retry_exceptions,
                                  self.ignore_exceptions,
                                  _sync=True)
            self.log.info("Verifying doc counts after create doc_ops")
            self.bucket_util._wait_for_stats_all_buckets()
            self.bucket_util.verify_stats_all_buckets(self.num_items)
            self.gen_read = doc_generator(
                self.key, start_read, end_read,
                doc_size=self.doc_size,
                doc_type=self.doc_type,
                target_vbucket=self.target_vbucket,
                vbuckets=self.cluster_util.vbuckets,
                key_size=self.key_size,
                randomize_doc_size=self.randomize_doc_size,
                randomize_value=self.randomize_value,
                mix_key_size=self.mix_key_size)
            count += 1
        self.log.info("====test_read_create_docs_parallelly ends====")

    def test_update_multi(self):
        """
        Write and Read docs parallely , While reading we are using
        old doc generator (self.gen_create)
        using which we already created docs in magam_base
        for writing we are creating a new doc generator.
        Befor we start read, killing memcached to make sure,
        all reads happen from magma/storage
        """
        self.log.info("Updating half the docs multiple times")
        count = 0
        while count < 10:
            for node in self.cluster.nodes_in_cluster:
                shell = RemoteMachineShellConnection(node)
                shell.kill_memcached()
                shell.disconnect()
                self.assertTrue(self.bucket_util._wait_warmup_completed(
                                [self.cluster_util.cluster.master],
                                self.bucket_util.buckets[0],
                                wait_time=self.wait_timeout * 10))
            self.doc_ops = "update"
            start = 0
            end = self.num_items//2
            if self.rev_update:
                start = -int(self.num_items//2 - 1)
                end = 1
            self.gen_update = doc_generator(self.key, start, end,
                                            doc_size=self.doc_size,
                                            doc_type=self.doc_type,
                                            target_vbucket=self.target_vbucket,
                                            vbuckets=self.cluster_util.vbuckets,
                                            key_size=self.key_size,
                                            mutate=count+1,
                                            randomize_doc_size=self.randomize_doc_size,
                                            randomize_value=self.randomize_value,
                                            mix_key_size=self.mix_key_size)
            _ = self.loadgen_docs(self.retry_exceptions,
                                  self.ignore_exceptions,
                                  _sync=True)
            self.log.info("Waiting for ep-queues to get drained")
            self.bucket_util._wait_for_stats_all_buckets()
            data_validation = self.task.async_validate_docs(
                self.cluster, self.bucket_util.buckets[0],
                self.gen_update, "update", 0,
                batch_size=self.batch_size,
                process_concurrency=self.process_concurrency,
                pause_secs=5, timeout_secs=self.sdk_timeout)
            self.task.jython_task_manager.get_task_result(data_validation)
            disk_usage = self.get_disk_usage(self.bucket_util.get_all_buckets()[0], self.servers)
            self.log.info("disk usage after update count {} is {}".format(count + 1, disk_usage))
            self.assertIs(
                disk_usage > 4 * self.disk_usage, False,
                "Disk Usage {} After Update Count {} exceeds Actual \
                disk usage {} by four times"
                .format(disk_usage, count, self.disk_usage)
                )
            count += 1
        self.log.info("====test_read_create_docs_parallelly ends====")
