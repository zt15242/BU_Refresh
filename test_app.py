import unittest
from unittest.mock import patch, MagicMock
from app import app
import json
import xml.etree.ElementTree as ET

class TestSOAPLogin(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    @patch('requests.post')
    def test_login_success(self, mock_post):
        # Mock successful response from Salesforce
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns="urn:partner.soap.sforce.com" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
   <soapenv:Body>
      <loginResponse>
         <result>
            <metadataServerUrl>https://test.sfcrmproducts.cn/services/Soap/m/58.0/00D90000000yabc</metadataServerUrl>
            <passwordExpired>false</passwordExpired>
            <sandbox>true</sandbox>
            <serverUrl>https://test.sfcrmproducts.cn/services/Soap/u/58.0/00D90000000yabc</serverUrl>
            <sessionId>mock_session_id_123456</sessionId>
            <userId>00590000000xabc</userId>
            <userInfo>
               <userFullName>测试用户</userFullName>
               <userName>test@test.com</userName>
               <userEmail>test@test.com</userEmail>
               <organizationName>测试组织</organizationName>
               <organizationId>00D90000000yabc</organizationId>
               <userId>00590000000xabc</userId>
               <userTimeZone>Asia/Shanghai</userTimeZone>
               <userLanguage>zh_CN</userLanguage>
            </userInfo>
         </result>
      </loginResponse>
   </soapenv:Body>
</soapenv:Envelope>""".encode('utf-8')
        mock_post.return_value = mock_response

        payload = {
            "env": "sandbox",
            "username": "test@test.com",
            "password": "password123",
            "security_token": "token123"
        }

        response = self.app.post('/api/login', 
                                 data=json.dumps(payload),
                                 content_type='application/json')
        
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['sessionId'], 'mock_session_id_123456')
        self.assertEqual(data['serverUrl'], 'https://test.sfcrmproducts.cn/services/Soap/u/58.0/00D90000000yabc')
        self.assertEqual(data['userInfo']['userFullName'], '测试用户')

    @patch('requests.post')
    def test_login_failure_soap_fault(self, mock_post):
        # Mock SOAP Fault response from Salesforce
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.content = b"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:sf="urn:fault.partner.soap.sforce.com" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
   <soapenv:Body>
      <soapenv:Fault>
         <faultcode>sf:INVALID_LOGIN</faultcode>
         <faultstring>INVALID_LOGIN: Invalid username, password, security token; or user locked out.</faultstring>
         <detail>
            <sf:LoginFault xsi:type="sf:LoginFault">
               <sf:exceptionCode>INVALID_LOGIN</sf:exceptionCode>
               <sf:exceptionMessage>Invalid username, password, security token; or user locked out.</sf:exceptionMessage>
            </sf:LoginFault>
         </detail>
      </soapenv:Fault>
   </soapenv:Body>
</soapenv:Envelope>"""
        mock_post.return_value = mock_response

        payload = {
            "env": "production",
            "username": "wrong@test.com",
            "password": "wrongpassword"
        }

        response = self.app.post('/api/login', 
                                 data=json.dumps(payload),
                                 content_type='application/json')
        
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertFalse(data['success'])
        self.assertIn("INVALID_LOGIN", data['error'])

    def test_invalid_parameters(self):
        # Test validation on invalid/missing params
        response = self.app.post('/api/login', 
                                 data=json.dumps({"env": "invalid_env"}),
                                 content_type='application/json')
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertFalse(data['success'])

class TestSFDCRefreshUtility(unittest.TestCase):
    def test_process_sql_template(self):
        from app import process_sql_template
        # Test semicolon splitting (picklist format)
        template1 = "SELECT id FROM Account WHERE billing_city__c in ({$work_location__c})"
        record1 = {"work_location__c": "北京;上海;广州"}
        resolved1 = process_sql_template(template1, record1)
        self.assertEqual(resolved1, "SELECT id FROM Account WHERE billing_city__c in ('北京','上海','广州')")

        # Test single string value
        template2 = "SELECT id FROM User WHERE Region__c = {$Region__c}"
        record2 = {"region__c": "North"} # test case-insensitive matching
        resolved2 = process_sql_template(template2, record2)
        self.assertEqual(resolved2, "SELECT id FROM User WHERE Region__c = 'North'")

        # Test empty value
        template3 = "SELECT id FROM User WHERE Region__c = {$Region__c}"
        record3 = {}
        resolved3 = process_sql_template(template3, record3)
        self.assertEqual(resolved3, "SELECT id FROM User WHERE Region__c = ''")

    @patch('requests.post')
    def test_query_salesforce_count(self, mock_post):
        from app import query_salesforce_count
        # Mock successful SOAP query response returning count
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.content = b"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns="urn:partner.soap.sforce.com" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
   <soapenv:Body>
      <queryResponse>
         <result>
            <done>true</done>
            <queryLocator xsi:nil="true"/>
            <size>42</size>
         </result>
      </queryResponse>
   </soapenv:Body>
</soapenv:Envelope>"""
        mock_post.return_value = mock_res
        
        count = query_salesforce_count("https://test.salesforce.com", "mock_session", "SELECT id FROM User")
        self.assertEqual(count, 42)

    def test_get_backup_data_not_found(self):
        # Test endpoint returns 404 when no backup matches parameters
        client = app.test_client()
        response = client.get('/api/backup/data?bu_config_id=nonexistent&subtask_key=user')
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.data)
        self.assertFalse(data['success'])

    def test_get_subtask_counts_no_config(self):
        client = app.test_client()
        response = client.post('/api/subtask-counts',
                               data=json.dumps({}),
                               content_type='application/json')
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertFalse(data['success'])

    def test_terminal_logs_api(self):
        client = app.test_client()
        # Save a log
        resp = client.post('/api/logs',
                           data=json.dumps({"bu_config_id": "test_id", "log_type": "info", "message": "Test log message"}),
                           content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        
        # Get logs
        resp = client.get('/api/logs/test_id')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['logs']), 1)
        self.assertEqual(data['logs'][0]['message'], "Test log message")
        
        # Delete logs
        resp = client.delete('/api/logs/test_id')
        self.assertEqual(resp.status_code, 200)
        
        # Get logs again (should be empty)
        resp = client.get('/api/logs/test_id')
        data = json.loads(resp.data)
        self.assertEqual(len(data['logs']), 0)

    def test_db_browser_api(self):
        client = app.test_client()
        # Get tables
        resp = client.get('/api/db/tables')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['success'])
        self.assertIn('bu_configs', data['tables'])
        self.assertIn('subtasks', data['tables'])
        
        # Get table data
        resp = client.get('/api/db/table-data?table=bu_configs')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['success'])
        self.assertIn('columns', data)
        self.assertIn('records', data)
        
        # Get invalid table (SQL injection check)
        resp = client.get('/api/db/table-data?table=invalid_table_name')
        self.assertEqual(resp.status_code, 400)

    def test_subtask_update_and_retry_api(self):
        client = app.test_client()
        import sqlite3
        from app import DATABASE_PATH
        
        # Setup mock configuration and subtask in DB
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO bu_configs (id, name) VALUES ('test_bu_id', 'TEST-BU-01')")
        cursor.execute("""
            INSERT OR REPLACE INTO subtasks (bu_config_id, key, name, count, execute, backup, run_state, backup_state, object_api_name, field_name, sql)
            VALUES ('test_bu_id', 'test_key', '测试子任务', '5条', 1, 1, 'ready', 'ready', 'Account', 'test_field', 'SELECT Id FROM Account')
        """)
        # Insert test backup records
        for i in range(5):
            cursor.execute("""
                INSERT INTO backup_records (bu_config_id, subtask_key, record_id, record_name, raw_data, sync_status)
                VALUES ('test_bu_id', 'test_key', ?, ?, '{}', 'pending')
            """, (f"rec_id_{i}", f"rec_name_{i}"))
        conn.commit()
        conn.close()
        
        # Call subtask update
        resp = client.post('/api/subtask/update',
                           data=json.dumps({"bu_config_id": "test_bu_id", "subtask_key": "test_key"}),
                           content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['success'])
        
        # One of them should fail based on the idx % 10 == 3 (index 3 out of 5)
        self.assertEqual(data['successCount'], 4)
        self.assertEqual(data['failCount'], 1)
        
        # Call retry
        resp = client.post('/api/subtask/retry',
                           data=json.dumps({"bu_config_id": "test_bu_id", "subtask_key": "test_key"}),
                           content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['successCount'], 5)
        self.assertEqual(data['failCount'], 0)
        self.assertEqual(data['retriedCount'], 1)
        
        # Clean up database
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bu_configs WHERE id = 'test_bu_id'")
        cursor.execute("DELETE FROM subtasks WHERE bu_config_id = 'test_bu_id'")
        cursor.execute("DELETE FROM backup_records WHERE bu_config_id = 'test_bu_id'")
        conn.commit()
        conn.close()

    @patch('requests.post')
    def test_subtask_real_update_batch_limit(self, mock_post):
        client = app.test_client()
        import sqlite3
        from app import DATABASE_PATH
        
        # Cleanup any leftover test data first
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bu_configs WHERE id = 'real_test_bu_id'")
        cursor.execute("DELETE FROM subtasks WHERE bu_config_id = 'real_test_bu_id'")
        cursor.execute("DELETE FROM backup_records WHERE bu_config_id = 'real_test_bu_id'")
        conn.commit()
        
        # Setup mock configuration and subtask in DB
        cursor.execute("INSERT OR REPLACE INTO bu_configs (id, name) VALUES ('real_test_bu_id', 'TEST-BU-02')")
        cursor.execute("""
            INSERT OR REPLACE INTO subtasks (bu_config_id, key, name, count, execute, backup, run_state, backup_state, object_api_name, field_name, sql)
            VALUES ('real_test_bu_id', 'user', '用户', '10条', 1, 1, 'ready', 'ready', 'User', 'user_sql__c', 'SELECT Id FROM User')
        """)
        # Insert 10 test backup records
        for i in range(10):
            raw_data = json.dumps({
                "Id": f"rec_id_{i}",
                "Name": f"User {i}",
                "BU__c": "OldBU",
                "User_BU__c": "NewBU",
                "BU_Province_ID__c": "OldID",
                "BU_Province_Text__c": "OldText",
                "Community__c": "OldComm",
                "ProvinceBU__c": "OldProvBU",
                "Region__c": "North"
            })
            cursor.execute("""
                INSERT INTO backup_records (bu_config_id, subtask_key, record_id, record_name, raw_data, sync_status)
                VALUES ('real_test_bu_id', 'user', ?, ?, ?, 'pending')
            """, (f"rec_id_{i}", f"rec_name_{i}", raw_data))
        conn.commit()
        conn.close()
        
        # Mock successful SOAP update response
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.content = b"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns="urn:partner.soap.sforce.com">
   <soapenv:Body>
      <updateResponse>
         <result>
            <id>mock_id</id>
            <success>true</success>
         </result>
      </updateResponse>
   </soapenv:Body>
</soapenv:Envelope>"""
        mock_post.return_value = mock_res
        
        # Call subtask update
        resp = client.post('/api/subtask/update',
                           data=json.dumps({
                               "bu_config_id": "real_test_bu_id", 
                               "subtask_key": "user",
                               "sessionId": "mock_session_id",
                               "serverUrl": "https://test.salesforce.com"
                           }),
                           content_type='application/json')
        
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['success'])
        
        # All 10 records should succeed
        self.assertEqual(data['successCount'], 10)
        self.assertEqual(data['failCount'], 0)
        
        # Verify call counts: mock_post should be called exactly 10 times
        self.assertEqual(mock_post.call_count, 10)
        
        # Check payload in the first call
        first_call_args, first_call_kwargs = mock_post.call_args_list[0]
        payload_data = first_call_kwargs['data'].decode('utf-8')
        
        # Verify 5 fields to nullify are present in <fieldsToNull>
        self.assertIn("<sf:fieldsToNull>BU_Province_ID__c</sf:fieldsToNull>", payload_data)
        self.assertIn("<sf:fieldsToNull>BU_Province_Text__c</sf:fieldsToNull>", payload_data)
        self.assertIn("<sf:fieldsToNull>BU__c</sf:fieldsToNull>", payload_data)
        self.assertIn("<sf:fieldsToNull>Community__c</sf:fieldsToNull>", payload_data)
        self.assertIn("<sf:fieldsToNull>ProvinceBU__c</sf:fieldsToNull>", payload_data)
        
        # Verify other fields are sent as values
        self.assertIn("<sf:User_BU__c>NewBU</sf:User_BU__c>", payload_data)
        self.assertIn("<sf:Region__c>North</sf:Region__c>", payload_data)
        
        # Verify the backup raw_data in SQLite remained unmodified (not cleared)
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT raw_data FROM backup_records WHERE bu_config_id = 'real_test_bu_id' AND subtask_key = 'user'")
        rows = cursor.fetchall()
        for r in rows:
            record_dict = json.loads(r[0])
            self.assertEqual(record_dict["BU__c"], "OldBU")
            self.assertEqual(record_dict["BU_Province_ID__c"], "OldID")
            self.assertEqual(record_dict["BU_Province_Text__c"], "OldText")
            self.assertEqual(record_dict["Community__c"], "OldComm")
            self.assertEqual(record_dict["ProvinceBU__c"], "OldProvBU")
        
        # Clean up database
        cursor.execute("DELETE FROM bu_configs WHERE id = 'real_test_bu_id'")
        cursor.execute("DELETE FROM subtasks WHERE bu_config_id = 'real_test_bu_id'")
        cursor.execute("DELETE FROM backup_records WHERE bu_config_id = 'real_test_bu_id'")
        conn.commit()
        conn.close()

class TestSFDCRequestLogger(unittest.TestCase):
    def test_log_sfdc_request_and_sanitize(self):
        import sqlite3
        import time
        from app import DATABASE_PATH, log_sfdc_request
        
        # Test clean up first
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sfdc_request_logs")
        conn.commit()
        
        # Inputs containing sensitive details
        url = "https://test.salesforce.com/services/Soap/u/58.0"
        method = "POST"
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "Authorization": "Bearer super_secret_token",
            "sessionId": "sensitive_session_id"
        }
        
        # XML payload containing sensitive password and sessionId
        body = """<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope>
  <env:Header>
    <SessionHeader>
      <sessionId>my_secret_session_id</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <login>
      <username>test@test.com</username>
      <password>my_secret_password</password>
    </login>
  </env:Body>
</env:Envelope>"""
        
        response_body = """{
            "access_token": "token_val",
            "client_secret": "secret_val",
            "username": "test@test.com"
        }"""
        
        log_sfdc_request(url, method, headers, body, 200, response_body)
        
        # Wait for async worker to process the log
        time.sleep(0.5)
        
        # Fetch the log row
        cursor.execute("SELECT * FROM sfdc_request_logs")
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 1)
        
        row = rows[0]
        # Columns: id, timestamp, request_url, request_method, request_headers, request_body, response_status, response_body, error_message
        logged_headers = json.loads(row[4])
        logged_body = row[5]
        logged_response = row[7]
        
        # Check that headers are sanitized
        self.assertEqual(logged_headers.get("Authorization"), "******")
        self.assertEqual(logged_headers.get("sessionId"), "******")
        
        # Check that XML body is sanitized
        self.assertIn("<sessionId>******</sessionId>", logged_body)
        self.assertIn("<password>******</password>", logged_body)
        self.assertNotIn("my_secret_session_id", logged_body)
        self.assertNotIn("my_secret_password", logged_body)
        
        # Check that JSON response body is sanitized
        self.assertIn('"access_token": "******"', logged_response)
        self.assertIn('"client_secret": "******"', logged_response)
        self.assertNotIn("token_val", logged_response)
        self.assertNotIn("secret_val", logged_response)
        
        # Verify username is not sanitized
        self.assertIn("test@test.com", logged_response)
        self.assertIn("test@test.com", logged_body)
        
        # Clean up database
        cursor.execute("DELETE FROM sfdc_request_logs")
        conn.commit()
        conn.close()

if __name__ == '__main__':
    unittest.main()
