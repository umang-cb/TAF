import datetime

from TestInput import TestInputSingleton
from cbas.cbas_base import CBASBaseTest


class CBASAsyncResultDeliveryTests(CBASBaseTest):
    def setUp(self):
        self.input = TestInputSingleton.input
        if "cb_bucket_name" not in self.input.test_params:
            self.input.test_params.update({"default_bucket": False})

        super(CBASAsyncResultDeliveryTests, self).setUp()
        self.validate_error = False
        if self.expected_error:
            self.validate_error = True

    def tearDown(self):
        super(CBASAsyncResultDeliveryTests, self).tearDown()

    def setupForTest(self):
        self.bucket_util.load_sample_bucket(self.sample_bucket)
        self.cbas_util.createConn(self.sample_bucket.name)

        # Create dataset on the CBAS bucket
        self.cbas_util.create_dataset_on_bucket(
            cbas_bucket_name=self.cb_bucket_name,
            cbas_dataset_name=self.cbas_dataset_name)

        # Connect to Bucket
        self.cbas_util.connect_to_bucket(
            cbas_bucket_name=self.cbas_bucket_name,
            cb_bucket_password=self.cb_bucket_password)

        # Allow ingestion to complete
        self.cbas_util.wait_for_ingestion_complete(
            [self.cbas_dataset_name],
            self.sample_bucket.stats.expected_item_count, 300)

    def test_mode_timeout(self):
        self.setupForTest()
        client_context_id = "abc"
        statement = "select sleep(count(*),200000) from {0} where mutated=0;".\
            format(self.cbas_dataset_name)
        import time
        start_time = time.time()
        end_time = start_time
        diff = end_time - start_time
        try:
            _, _, _, _, _ = self.cbas_util.execute_statement_on_cbas_util(
                statement, mode=self.mode, client_context_id=client_context_id,
                timeout=75, analytics_timeout=120)
        except Exception as e:
            end_time = time.time()
            diff = end_time - start_time
        self.assertTrue(diff > 70 and diff < 90, "Request took %s seconds to \
        timeout" % diff)
        status = self.cbas_util.delete_request(client_context_id)
        self.assertTrue(str(status) != "200",
                        "Request is still running on the server even client \
                        is timed out.")

    def test_mode(self):
        self.setupForTest()

        statement = "select * from {0} where city=\"Chicago\";".format(
            self.cbas_dataset_name)
        status, _, _, results, response_handle = self.cbas_util.\
            execute_statement_on_cbas_util(statement, mode=self.mode)

        if self.mode == 'async' or self.mode == 'deferred':
            if results:
                self.log.info("Results in Response : {0}".format(results))
                self.fail("Results returned when mode is async/deferred")
        else:
            if response_handle:
                self.log.info("Handle in Response : {0}".format(response_handle))
                self.fail("Handle returned when mode is not async/deferred")

        if response_handle:
            if self.mode == 'async':
                # Retrieve status first. Then, from status URI,
                # get the handle to retrieve results
                # Wait for results to be available at the Status URI
                status, result_handle = self.cbas_util.\
                    retrieve_request_status_using_handle(self.cluster.master,
                                                         response_handle)

                while (status.lower() != "success"):
                    self.sleep(5)
                    status, result_handle = self.cbas_util.\
                        retrieve_request_status_using_handle(self.cluster.master,
                                                             response_handle)

                results = self.cbas_util.retrieve_result_using_handle(
                    self.cluster.master, result_handle)

            if self.mode == "deferred":
                # Retrieve results directly from this handle.
                results = self.cbas_util.retrieve_result_using_handle(
                    self.cluster.master, response_handle)

            # Execute the same query without passing the mode param (legacy mode)
            _, _, _, immediate_results, _ = self.cbas_util.execute_statement_on_cbas_util(
                statement)

            # Validate if the results with mode and without mode are the same
            if not (sorted(results) == sorted(immediate_results)):
                self.log.info("Results with mode = %s : %s"%(self.mode, results))
                self.log.info("Results with legacy mode : %s"%immediate_results)

                self.fail("Results not correct")
        else:
            if self.mode == 'async' or self.mode == 'deferred':
                self.fail("No handle returned with mode=async/deferred")

    def test_mode_reuse_handle(self):
        self.setupForTest()

        # Execute statement and get a handle
        statement = "select * from {0} where city=\"Chicago\";".format(
            self.cbas_dataset_name)
        status, metrics, errors, results, handle = self.cbas_util.execute_statement_on_cbas_util(
            statement, mode=self.mode)

        # Fetch result using the same handle twice
        if handle:
            response1 = self.cbas_util.retrieve_result_using_handle(self.cluster.master, handle)
            response2 = self.cbas_util.retrieve_result_using_handle(self.cluster.master, handle)

            # Validate results can not be fetched more than once using the same handle
            if response2:
                self.fail("able to retrieve results from a used handle")

        else:
            if self.mode == 'async' or self.mode == 'deferred':
                self.fail("No handle returned with mode=async/deferred")

    def test_mode_invalid_handle(self):
        self.setupForTest()

        handle = "http://{0}:8095/analytics/service/result/999-0".format(self.cbas_node.ip)

        response = self.cbas_util.retrieve_result_using_handle(self.cluster.master, handle)

        if response:
            self.fail("No error when using an invalid handle")

    def test_async_mode(self):
        delay = 20000
        self.cbas_util.createConn(self.cb_bucket_name)

        # Create dataset on the CBAS bucket
        self.cbas_util.create_dataset_on_bucket(cbas_bucket_name=self.cb_bucket_name,
                                      cbas_dataset_name=self.cbas_dataset_name)

        # Connect to Bucket
        self.cbas_util.connect_to_bucket(cbas_bucket_name=self.cbas_bucket_name,
                               cb_bucket_password=self.cb_bucket_password)

        # Load CB bucket
        self.perform_doc_ops_in_all_cb_buckets("create", 0,
                                               self.num_items)
        self.bucket_util.verify_stats_all_buckets(self.num_items)

        # Wait while ingestion is completed
        total_items, _ = self.cbas_util.get_num_items_in_cbas_dataset(
            self.cbas_dataset_name)

        timeout = 300
        while (timeout > 0):
            if self.num_items == total_items:
                break
            else:
                self.sleep(5)
                total_items, _ = self.cbas_util.get_num_items_in_cbas_dataset(self.cbas_dataset_name)
                timeout -= 5
                
        # Execute query (with sleep induced) and use the handle immediately to fetch the results
        statement = "select sleep(count(*),{0}) from {1} where mutated=0;".format(
            delay, self.cbas_dataset_name)

        status, metrics, errors, results, handle = self.cbas_util.execute_statement_on_cbas_util(
            statement, mode=self.mode)
        async_mode_execution_time = self.cbas_util.convert_execution_time_into_ms(
            metrics["executionTime"])
        self.log.info("Execution time in async mode = %s",
                      async_mode_execution_time)

        # Validate if the status is 'started'
        self.log.info("Status = %s", status)
        if status != "running":
            self.fail("Status is not 'running'")

        # Validate if results key is not present in response
        if results:
            self.fail("Results is returned in the response")

        if handle:
            # Retrive results from handle and compute elapsed time
            a = datetime.datetime.now()
            status, result_handle = self.cbas_util.retrieve_request_status_using_handle(
                self.cluster.master, handle)
            while (status.lower() != "success"):
                self.sleep(5)
                status, result_handle = self.cbas_util.retrieve_request_status_using_handle(
                    self.cluster.master, handle)

            response = self.cbas_util.retrieve_result_using_handle(self.cluster.master, result_handle)
            b = datetime.datetime.now()
            c = b - a
            elapsedTime = c.total_seconds() * 1000
            self.log.info("Elapsed time = %s ms", elapsedTime)

            # Validate response is available
            if not response:
                self.fail("Did not get the response using the handle")

            # Validate if response is not available before query execution completes
            # Here, delay*0.9 is because assuming we might have lost 10% time in the testcase.
            if elapsedTime < (delay * 0.9):
                self.fail(
                    "Able to fetch result from handle before query execution completed")

    def test_deferred_mode(self):
        self.cbas_util.createConn(self.cb_bucket_name)
        # Create dataset on the CBAS bucket
        self.cbas_util.create_dataset_on_bucket(
            cbas_bucket_name=self.cb_bucket_name,
            cbas_dataset_name=self.cbas_dataset_name)

        # Connect to Bucket
        self.cbas_util.connect_to_bucket(
            cbas_bucket_name=self.cbas_bucket_name,
            cb_bucket_password=self.cb_bucket_password)

        # Load CB bucket
        self.perform_doc_ops_in_all_cb_buckets("create", 0,
                                               self.num_items)
        self.bucket_util.verify_stats_all_buckets(self.num_items)

        # Wait while ingestion is completed
        total_items, _ = self.cbas_util.get_num_items_in_cbas_dataset(
            self.cbas_dataset_name)
        while (self.num_items > total_items):
            self.sleep(5)
            total_items, _ = self.cbas_util.get_num_items_in_cbas_dataset(
                self.cbas_dataset_name)

        statement = "select sleep(count(*),20000) from {0} where mutated=0;".format(
            self.cbas_dataset_name)

        # Execute query (with sleep induced) in async mode and see the execution time
        _, async_metrics, _, _, async_handle = self.cbas_util.execute_statement_on_cbas_util(
            statement, mode="async")
        async_mode_execution_time = self.cbas_util.convert_execution_time_into_ms(
            async_metrics["executionTime"])
        self.log.info("Execution time in async mode = %s ms",
                      async_mode_execution_time)

        # Execute query (with sleep induced) in deferred mode and see the execution time
        status, deferred_metrics, _, results, deferred_handle = self.cbas_util.execute_statement_on_cbas_util(
            statement, mode=self.mode)
        deferred_mode_execution_time = self.cbas_util.convert_execution_time_into_ms(
            deferred_metrics["executionTime"])
        self.log.info("Execution time in deferred mode = %s ms",
                      deferred_mode_execution_time)

        # Validate that execution time in deferred mode > async mode
        if deferred_mode_execution_time <= async_mode_execution_time:
            self.fail(
                "Response in Deferred mode is faster or equal to async mode")

        # Validate status is 'success'
        self.log.info("Status = %s", status)
        if status != "success":
            self.fail("Status is not 'success'")

        # Validate if results key is not present in response
        if results:
            self.fail("Results is returned in the response")

        # Validate if result can be retrieved using the handle
        if deferred_handle:
            response = self.cbas_util.retrieve_result_using_handle(self.cluster.master,
                                                         deferred_handle)
            if not response:
                self.fail("Did not get the response using the handle")

    def test_immediate_mode(self):
        self.cbas_util.createConn(self.cb_bucket_name)

        # Create dataset on the CBAS bucket
        self.cbas_util.create_dataset_on_bucket(cbas_bucket_name=self.cb_bucket_name,
                                      cbas_dataset_name=self.cbas_dataset_name)

        # Connect to Bucket
        self.cbas_util.connect_to_bucket(cbas_bucket_name=self.cbas_bucket_name,
                               cb_bucket_password=self.cb_bucket_password)

        # Load CB bucket
        self.perform_doc_ops_in_all_cb_buckets("create", 0,
                                               self.num_items)
        self.bucket_util.verify_stats_all_buckets(self.num_items)

        # Wait while ingestion is completed
        total_items, _ = self.cbas_util.get_num_items_in_cbas_dataset(
            self.cbas_dataset_name)
        while (self.num_items > total_items):
            self.sleep(5)
            total_items, _ = self.cbas_util.get_num_items_in_cbas_dataset(
                self.cbas_dataset_name)

        statement = "select sleep(count(*),20000) from {0} where mutated=0;".format(
            self.cbas_dataset_name)

        # Execute query (with sleep induced) in immediate mode
        status, metrics, _, results, handle = self.cbas_util.execute_statement_on_cbas_util(
            statement, mode=self.mode)

        # Validate status is 'success'
        self.log.info("Status = %s", status)
        if status != "success":
            self.fail("Status is not 'success'")

        # Validate if results key is present in response
        if not results:
            self.fail("Results is not returned in the response")

        # Validate if handle key is not present in response
        if handle:
            self.fail("Handle returned in response in immediate mode")

    def test_status(self):
        delay = 20000
        self.cbas_util.createConn(self.cb_bucket_name)

        # Create dataset on the CBAS bucket
        self.cbas_util.create_dataset_on_bucket(
            cbas_bucket_name=self.cb_bucket_name,
            cbas_dataset_name=self.cbas_dataset_name)

        # Connect to Bucket
        self.cbas_util.connect_to_bucket(
            cbas_bucket_name=self.cbas_bucket_name,
            cb_bucket_password=self.cb_bucket_password)

        # Load CB bucket
        self.perform_doc_ops_in_all_cb_buckets("create", 0,
                                               self.num_items)
        self.bucket_util.verify_stats_all_buckets(self.num_items)

        if not self.cbas_util.validate_cbas_dataset_items_count(
                self.cbas_dataset_name,
                self.num_items):
            self.fail("No. of items in CBAS dataset do not match that in the CB bucket")
        # Execute query (with sleep induced) and use the handle immediately to fetch the results
        statement = "select sleep(count(*),{0}) from {1} where mutated=0;".\
            format(delay, self.cbas_dataset_name)

        status, _, _, _, handle = self.cbas_util.execute_statement_on_cbas_util(
            statement, mode=self.mode)

        if handle:
            if self.mode == "async":
                # Retrieve status from handle
                status, result_handle = self.cbas_util.retrieve_request_status_using_handle(self.cluster.master,
                                                                   handle)
                if status.lower() != "running":
                    self.fail("Status is not RUNNING")
                else:
                    # Allow the request to be processed, and then check status
                    self.sleep((delay / 1000) + 5)
                    status, result_handle = self.cbas_util.retrieve_request_status_using_handle(
                        self.cluster.master, handle)
                    if status.lower() != "success":
                        self.fail("Status is not SUCCESS")
            elif self.mode == "deferred":
                # Retrieve status from handle
                status = self.cbas_util.retrieve_request_status_using_handle(self.cluster.master,
                                                                   handle)
                if status.lower() != "success":
                    self.fail("Status is not SUCCESS")

    def test_status_with_invalid_handle(self):
        self.setupForTest()

        handle = "http://{0}:8095/analytics/service/status/999-0".format(self.cbas_node.ip)

        # Retrive status from handle
        status, result_handle = self.cbas_util.retrieve_request_status_using_handle(self.cluster.master,
                                                           handle)

        if status:
            self.fail("No error when fetching status for an invalid handle")
