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

if __name__ == '__main__':
    unittest.main()
