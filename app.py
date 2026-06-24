import traceback
from flask import Flask, request, jsonify, render_template
import requests
import xml.etree.ElementTree as ET
import html
import os
import datetime
import sqlite3
import json
import csv

import sys
import contextlib
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__, template_folder='templates', static_folder='static')

# Check if running in a PyInstaller bundle
if getattr(sys, 'frozen', False):
    # Use a user-writable folder in the user's home directory for database and backups
    DATA_DIR = os.path.expanduser("~/BU省刷新工具")
    os.makedirs(DATA_DIR, exist_ok=True)
else:
    DATA_DIR = app.root_path

DATABASE_PATH = os.path.join(DATA_DIR, "sfdc_workspace.db")

# Global log queue for async SFDC request logging
LOG_QUEUE = queue.Queue(maxsize=10000)

# Global pause control for subtask updates
# Key: (bu_config_id, subtask_key), Value: {'paused': bool}
PAUSE_CONTROL = {}

# Global backup progress tracking
# Key: (bu_config_id, subtask_key), Value: {'current': int, 'total': int, 'status': str}
BACKUP_PROGRESS = {}

def log_worker():
    """Background thread worker to write SFDC request logs asynchronously"""
    while True:
        try:
            log_entry = LOG_QUEUE.get(timeout=1)
            if log_entry is None:  # Poison pill to stop the worker
                break
                
            try:
                with db_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO sfdc_request_logs (timestamp, request_url, request_method, request_headers, request_body, response_status, response_body, error_message)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, log_entry)
                    conn.commit()
            except Exception as e:
                print(f"Failed to write log entry to database: {str(e)}")
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Log worker error: {str(e)}")

# Start the log worker thread
log_thread = threading.Thread(target=log_worker, daemon=True)
log_thread.start()

@contextlib.contextmanager
def db_conn():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        yield conn
    finally:
        conn.close()

def init_db():
    with db_conn() as conn:
        cursor = conn.cursor()
        
        # Create main configs table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS bu_configs (
            id TEXT PRIMARY KEY,
            name TEXT,
            province TEXT,
            city TEXT,
            currency TEXT,
            owner TEXT,
            created_by_name TEXT,
            created_by_time TEXT,
            modified_by_name TEXT,
            modified_by_time TEXT,
            progress_text TEXT,
            progress_color TEXT,
            work_location__c TEXT,
            BU_Group__c TEXT
        )
        """)
        
        # Create subtasks table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS subtasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bu_config_id TEXT,
            key TEXT,
            name TEXT,
            count TEXT,
            execute INTEGER,
            backup INTEGER,
            run_state TEXT,
            backup_state TEXT,
            object_api_name TEXT,
            field_name TEXT,
            sql TEXT,
            success_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            FOREIGN KEY (bu_config_id) REFERENCES bu_configs(id) ON DELETE CASCADE
        )
        """)
        
        # Create object mappings cache table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS object_mappings (
            field_name TEXT PRIMARY KEY,
            object_name TEXT,
            object_label TEXT
        )
        """)
        
        # Create backup records table for tracking data backup records and sync progress
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bu_config_id TEXT,
            subtask_key TEXT,
            record_id TEXT,
            record_name TEXT,
            raw_data TEXT,
            sync_status TEXT DEFAULT 'pending',
            error_message TEXT,
            backup_file_path TEXT,
            FOREIGN KEY (bu_config_id) REFERENCES bu_configs(id) ON DELETE CASCADE
        )
        """)
        
        # Create terminal logs table for persistent console history
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS terminal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bu_config_id TEXT,
            timestamp TEXT,
            log_type TEXT,
            message TEXT,
            FOREIGN KEY (bu_config_id) REFERENCES bu_configs(id) ON DELETE CASCADE
        )
        """)
    
        # Create sfdc_request_logs table for tracking all requests to SFDC
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sfdc_request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            request_url TEXT,
            request_method TEXT,
            request_headers TEXT,
            request_body TEXT,
            response_status INTEGER,
            response_body TEXT,
            error_message TEXT
        )
        """)
    
        # Ensure success_count and fail_count exist in subtasks table
        cursor.execute("PRAGMA table_info(subtasks)")
        columns = [r[1] for r in cursor.fetchall()]
        if 'success_count' not in columns:
            cursor.execute("ALTER TABLE subtasks ADD COLUMN success_count INTEGER DEFAULT 0")
        if 'fail_count' not in columns:
            cursor.execute("ALTER TABLE subtasks ADD COLUMN fail_count INTEGER DEFAULT 0")
        
        cursor.execute("PRAGMA table_info(bu_configs)")
        bu_columns = [r[1] for r in cursor.fetchall()]
        if 'BU_Group__c' not in bu_columns:
            cursor.execute("ALTER TABLE bu_configs ADD COLUMN BU_Group__c TEXT")
        # No initial fake data seeded, only keep real data
        conn.commit()

init_db()

import re

def sanitize_headers(headers):
    if not headers:
        return headers
    sanitized = {}
    for k, v in dict(headers).items():
        kl = k.lower()
        if kl in ['authorization', 'sessionid']:
            sanitized[k] = "******"
        else:
            sanitized[k] = v
    return sanitized

def sanitize_sensitive_data(text):
    if not isinstance(text, str):
        return text
    
    # 1. Mask XML tags for password, sessionId, etc.
    text = re.sub(
        r'(<(?:[^:>]+:)?password[^>]*>)(.*?)(</?:?(?:[^:>]+:)?password>)',
        r'\1******\3',
        text,
        flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(
        r'(<(?:[^:>]+:)?sessionId[^>]*>)(.*?)(</?:?(?:[^:>]+:)?sessionId>)',
        r'\1******\3',
        text,
        flags=re.IGNORECASE | re.DOTALL
    )
    
    # 2. Mask JSON/Form urlencoded keys
    def mask_urlencoded(match):
        key = match.group(1)
        val = match.group(2)
        if key.lower() in ['client_secret', 'password', 'sessionid', 'access_token', 'security_token', 'code']:
            return f"{key}=******"
        return match.group(0)
    
    text = re.sub(r'([^& =]+)=([^&]*)', mask_urlencoded, text)
    
    def mask_json(match):
        quote1 = match.group(1)
        key = match.group(2)
        quote2 = match.group(3)
        sep = match.group(4)
        quote3 = match.group(5)
        val = match.group(6)
        quote4 = match.group(7)
        if key.lower() in ['client_secret', 'password', 'sessionid', 'access_token', 'security_token', 'code']:
            return f"{quote1}{key}{quote2}{sep}{quote3}******{quote4}"
        return match.group(0)
        
    text = re.sub(
        r'(["\'])([^"\']+)(["\'])(\s*:\s*)(["\'])(.*?)(["\'])',
        mask_json,
        text
    )
    
    return text

def log_sfdc_request(url, method, headers, body, status_code=None, response_body=None, error_message=None):
    try:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        sanitized_headers = sanitize_headers(headers)
        headers_str = ""
        if sanitized_headers:
            try:
                headers_str = json.dumps(sanitized_headers, ensure_ascii=False)
            except Exception:
                headers_str = str(sanitized_headers)
                
        body_str = ""
        if body is not None:
            if isinstance(body, bytes):
                try:
                    body_str = body.decode('utf-8')
                except Exception:
                    body_str = str(body)
            elif isinstance(body, dict):
                try:
                    body_str = json.dumps(body, ensure_ascii=False)
                except Exception:
                    body_str = str(body)
            else:
                body_str = str(body)
        body_str = sanitize_sensitive_data(body_str)
                
        resp_str = ""
        if response_body is not None:
            if isinstance(response_body, bytes):
                try:
                    resp_str = response_body.decode('utf-8')
                except Exception:
                    resp_str = str(response_body)
            else:
                resp_str = str(response_body)
        resp_str = sanitize_sensitive_data(resp_str)

        # Queue the log entry for async writing
        log_entry = (now_str, url, method, headers_str, body_str, status_code, resp_str, error_message)
        try:
            LOG_QUEUE.put_nowait(log_entry)
        except queue.Full:
            print(f"Log queue is full, dropping log entry for {url}")
    except Exception as e:
        print(f"Failed to prepare sfdc request log: {str(e)}")

def process_sql_template(sql_template, record_dict):
    if not sql_template:
        return ""
    
    # Find all placeholders like {$field_name}
    placeholders = re.findall(r'\{\$(.+?)\}', sql_template)
    
    for ph in placeholders:
        # Get field value from record_dict (case-insensitive)
        val = None
        for k, v in record_dict.items():
            if k.lower() == ph.lower():
                val = v
                break
        if val is None:
            val = ""
            
        # Format based on semicolons (Salesforce multi-select picklist splits by semicolon)
        if isinstance(val, str) and ';' in val:
            parts = [p.strip() for p in val.split(';') if p.strip()]
            formatted_val = ",".join([f"'{p}'" for p in parts])
        elif isinstance(val, str) and val:
            # Wrap standard string in quotes for SOQL 'IN' context
            formatted_val = f"'{val}'"
        else:
            formatted_val = "''"
            
        sql_template = sql_template.replace(f"{{${ph}}}", formatted_val)
    return sql_template

def query_salesforce_count(soap_url, session_id, soql):
    if not soap_url or not session_id or not soql:
        return None
        
    count_soql = soql
    # Try to convert to SELECT count() FROM ... to speed up
    match = re.match(r'(?i)\bselect\b\s+(.+?)\s+\bfrom\b', soql)
    if match:
        fields_part = match.group(1)
        if 'count(' not in fields_part.lower():
            count_soql = re.sub(r'(?i)\bselect\b\s+(.+?)\s+\bfrom\b', 'SELECT count() FROM', soql, count=1)
            
    # Remove LIMIT if present
    count_soql = re.sub(r'(?i)\s+limit\s+\d+', '', count_soql)

    body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <query xmlns="urn:partner.soap.sforce.com">
      <queryString>{html.escape(count_soql)}</queryString>
    </query>
  </env:Body>
</env:Envelope>"""
    try:
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": "query"
        }
        res = requests.post(soap_url, data=body.encode('utf-8'), headers=headers, timeout=10)
        log_sfdc_request(soap_url, "POST", headers, body, res.status_code, res.content)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for elem in root.iter():
                if elem.tag.split('}')[-1] == 'size':
                    return int(elem.text)
    except Exception as e:
        log_sfdc_request(soap_url, "POST", headers, body, error_message=str(e))
        print(f"Failed count query for SOQL: {count_soql}, error: {str(e)}")
        
    # Fallback to original query size
    body_orig = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <query xmlns="urn:partner.soap.sforce.com">
      <queryString>{html.escape(soql)}</queryString>
    </query>
  </env:Body>
</env:Envelope>"""
    try:
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": "query"
        }
        res = requests.post(soap_url, data=body_orig.encode('utf-8'), headers=headers, timeout=10)
        log_sfdc_request(soap_url, "POST", headers, body_orig, res.status_code, res.content)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for elem in root.iter():
                if elem.tag.split('}')[-1] == 'size':
                    return int(elem.text)
    except Exception as e:
        log_sfdc_request(soap_url, "POST", headers, body_orig, error_message=str(e))
        print(f"Failed fallback query for count: {str(e)}")
        
    return None

def get_sqlite_records(soap_url=None, session_id=None):
    with db_conn() as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM bu_configs")
        config_rows = cursor.fetchall()
        
        records = []
        for row in config_rows:
            cfg_id = row[0]
            cursor.execute("SELECT * FROM subtasks WHERE bu_config_id = ?", (cfg_id,))
            subtask_rows = cursor.fetchall()
            
            # Temp record dictionary for SQL template processing
            record_dict_temp = {
                "id": cfg_id,
                "name": row[1],
                "province": row[2],
                "city": row[3],
                "currency": row[4],
                "owner": row[5],
                "createdByName": row[6],
                "createdByTime": row[7],
                "modifiedByName": row[8],
                "modifiedByTime": row[9],
                "progressText": row[10],
                "progressColor": row[11],
                "work_location__c": row[12] if len(row) > 12 else "",
                "BU_Group__c": row[13] if len(row) > 13 else ""
            }
            
            subtasks_dict = {}
            for sub in subtask_rows:
                key = sub[2]
                raw_sql = sub[11]
                resolved_sql = process_sql_template(raw_sql, record_dict_temp)
                
                # Use count from database directly as per user request
                count_str = sub[4] # Fallback to seeded count
                if count_str == "计算中..." or count_str == "-":
                    count_str = "备份后显示"
                
                subtasks_dict[key] = {
                    "name": sub[3],
                    "key": key,
                    "count": count_str,
                    "execute": bool(sub[5]),
                    "backup": bool(sub[6]),
                    "runState": sub[7],
                    "backupState": sub[8],
                    "objectApiName": sub[9],
                    "fieldName": sub[10],
                    "sql": resolved_sql,
                    "successCount": sub[12] if len(sub) > 12 else 0,
                    "failCount": sub[13] if len(sub) > 13 else 0
                }
                
            records.append({
                "id": cfg_id,
                "name": row[1],
                "province": row[2],
                "city": row[3],
                "currency": row[4],
                "owner": row[5],
                "createdByName": row[6],
                "createdByTime": row[7],
                "modifiedByName": row[8],
                "modifiedByTime": row[9],
                "progressText": row[10],
                "progressColor": row[11],
                "work_location__c": record_dict_temp["work_location__c"],
                "BU_Group__c": record_dict_temp["BU_Group__c"],
                "subtasks": subtasks_dict
            })
            
        return records

# Environment configuration
ENVIRONMENTS = {
    "sandbox": "https://test.sfcrmproducts.cn",
    "production": "https://login.sfcrmproducts.cn"
}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    env_type = data.get('env')
    username = data.get('username')
    password = data.get('password')
    security_token = data.get('security_token', '')

    if not env_type or env_type not in ENVIRONMENTS:
        return jsonify({"success": False, "error": "请选择有效的登录环境（sandbox 或 production）"}), 400

    if not username or not password:
        return jsonify({"success": False, "error": "用户名和密码不能为空"}), 400

    base_url = ENVIRONMENTS[env_type]
    # Partner SOAP URL
    soap_url = f"{base_url}/services/Soap/u/58.0"

    # Concatenate password and security token if token is provided
    full_password = password
    if security_token:
        full_password = password + security_token

    # Escape XML entities
    escaped_username = html.escape(username)
    escaped_password = html.escape(full_password)

    # SOAP Payload
    soap_body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Body>
    <n1:login xmlns:n1="urn:partner.soap.sforce.com">
      <n1:username>{escaped_username}</n1:username>
      <n1:password>{escaped_password}</n1:password>
    </n1:login>
  </env:Body>
</env:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=UTF-8",
        "SOAPAction": "login"
    }

    try:
        response = requests.post(soap_url, data=soap_body.encode('utf-8'), headers=headers, timeout=30)
        log_sfdc_request(soap_url, "POST", headers, soap_body, response.status_code, response.content)
        
        # Parse XML response
        try:
            root = ET.fromstring(response.content)
        except Exception as parse_err:
            return jsonify({
                "success": False, 
                "error": f"解析服务响应 XML 失败，HTTP 状态码: {response.status_code}. 错误: {str(parse_err)}"
            }), 500

        # Helper to find tag ignoring namespace
        def find_elem(element, tag_name):
            for elem in element.iter():
                if elem.tag.split('}')[-1] == tag_name:
                    return elem
            return None

        # Check for Fault
        fault = find_elem(root, 'Fault')
        if fault is not None:
            faultstring_elem = find_elem(fault, 'faultstring')
            faultstring = faultstring_elem.text if faultstring_elem is not None else "未知 SOAP 错误"
            return jsonify({"success": False, "error": faultstring}), 400

        # Extract login success fields
        session_id_elem = find_elem(root, 'sessionId')
        server_url_elem = find_elem(root, 'serverUrl')
        user_id_elem = find_elem(root, 'userId')

        if session_id_elem is not None and server_url_elem is not None:
            user_info = {}
            user_info_elem = find_elem(root, 'userInfo')
            if user_info_elem is not None:
                for child in user_info_elem:
                    tag = child.tag.split('}')[-1]
                    user_info[tag] = child.text

            return jsonify({
                "success": True,
                "sessionId": session_id_elem.text,
                "serverUrl": server_url_elem.text.split('/services/Soap')[0],  # Extract base URL only
                "userId": user_id_elem.text if user_id_elem is not None else "",
                "userInfo": user_info
            })
        else:
            return jsonify({"success": False, "error": "登录失败：返回的数据中没有 Session ID 或 Server URL"}), 400

    except requests.exceptions.RequestException as req_err:
        log_sfdc_request(soap_url, "POST", headers, soap_body, error_message=str(req_err))
        return jsonify({"success": False, "error": f"网络请求失败: {str(req_err)}"}), 500
    except Exception as e:
        log_sfdc_request(soap_url, "POST", headers, soap_body, error_message=str(e))
        return jsonify({"success": False, "error": f"系统内部错误: {str(e)}"}), 500

import uuid
import urllib.parse

OAUTH_PENDING_REQUESTS = {}

@app.route('/api/session-login', methods=['POST'])
def session_login():
    data = request.json or {}
    session_id = data.get('sessionId')
    server_url = data.get('serverUrl')
    
    if not session_id or not server_url:
        return jsonify({"success": False, "error": "Session ID 和 Server URL 不能为空"}), 400
        
    server_url = server_url.rstrip('/')
    if '/services/Soap/u/' not in server_url:
        soap_url = f"{server_url}/services/Soap/u/58.0"
    else:
        soap_url = server_url
        server_url = server_url.split('/services/Soap/')[0]

    soap_body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <getUserInfo xmlns="urn:partner.soap.sforce.com"/>
  </env:Body>
</env:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=UTF-8",
        "SOAPAction": "getUserInfo"
    }

    try:
        response = requests.post(soap_url, data=soap_body.encode('utf-8'), headers=headers, timeout=20)
        log_sfdc_request(soap_url, "POST", headers, soap_body, response.status_code, response.content)
        try:
            root = ET.fromstring(response.content)
        except Exception as parse_err:
            return jsonify({
                "success": False, 
                "error": f"解析服务响应 XML 失败，HTTP 状态码: {response.status_code}. 错误: {str(parse_err)}"
            }), 500

        def find_elem(element, tag_name):
            for elem in element.iter():
                if elem.tag.split('}')[-1] == tag_name:
                    return elem
            return None

        fault = find_elem(root, 'Fault')
        if fault is not None:
            faultstring_elem = find_elem(fault, 'faultstring')
            faultstring = faultstring_elem.text if faultstring_elem is not None else "未知 SOAP 错误"
            return jsonify({"success": False, "error": faultstring}), 400

        result_elem = find_elem(root, 'result')
        if result_elem is not None:
            user_info = {}
            for child in result_elem:
                tag = child.tag.split('}')[-1]
                user_info[tag] = child.text
            
            user_id = user_info.get('userId', '')
            
            return jsonify({
                "success": True,
                "sessionId": session_id,
                "serverUrl": server_url,
                "userId": user_id,
                "userInfo": {
                    "userId": user_id,
                    "userFullName": user_info.get('userFullName', 'Session 用户'),
                    "userEmail": user_info.get('userEmail', 'session@company.com'),
                    "userName": user_info.get('userName', '')
                }
            })
        else:
            return jsonify({"success": False, "error": "验证 Session ID 失败：未能获取用户信息"}), 400

    except requests.exceptions.RequestException as req_err:
        log_sfdc_request(soap_url, "POST", headers, soap_body, error_message=str(req_err))
        return jsonify({"success": False, "error": f"网络请求失败: {str(req_err)}"}), 500
    except Exception as e:
        log_sfdc_request(soap_url, "POST", headers, soap_body, error_message=str(e))
        return jsonify({"success": False, "error": f"验证 Session ID 失败: {str(e)}"}), 500


@app.route('/api/oauth/url', methods=['POST'])
def oauth_url():
    data = request.json or {}
    login_url = data.get('login_url', '').rstrip('/')
    client_id = data.get('client_id', '').strip()
    client_secret = data.get('client_secret', '').strip()
    
    if not login_url or not client_id:
        return jsonify({"success": False, "error": "登录地址和 Client ID 不能为空"}), 400
        
    state = str(uuid.uuid4())
    redirect_uri = request.host_url.rstrip('/') + '/oauth/callback'
    
    OAUTH_PENDING_REQUESTS[state] = {
        "login_url": login_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri
    }
    
    auth_url = f"{login_url}/services/oauth2/authorize?response_type=code&client_id={client_id}&redirect_uri={urllib.parse.quote(redirect_uri)}&state={state}"
    
    return jsonify({
        "success": True,
        "auth_url": auth_url
    })


@app.route('/oauth/callback')
def oauth_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    error_description = request.args.get('error_description')
    
    if error:
        err_msg = f"OAuth 授权错误: {error} - {error_description}"
        return f"<h3>登录授权失败</h3><p>{err_msg}</p><a href='/'>返回登录页</a>", 400
        
    if not code or not state:
        return "<h3>登录授权失败</h3><p>缺少必要的 authorization code 或 state 参数</p><a href='/'>返回登录页</a>", 400
        
    req_info = OAUTH_PENDING_REQUESTS.pop(state, None)
    if not req_info:
        return "<h3>登录授权失败</h3><p>无效或已过期的 state 会话</p><a href='/'>返回登录页</a>", 400
        
    login_url = req_info["login_url"]
    client_id = req_info["client_id"]
    client_secret = req_info["client_secret"]
    redirect_uri = req_info["redirect_uri"]
    
    token_url = f"{login_url}/services/oauth2/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri
    }
    
    try:
        token_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            res = requests.post(token_url, data=payload, headers=token_headers, timeout=20)
            log_sfdc_request(token_url, "POST", token_headers, payload, res.status_code, res.content)
        except Exception as post_err:
            log_sfdc_request(token_url, "POST", token_headers, payload, error_message=str(post_err))
            raise post_err
            
        if res.status_code != 200:
            err_data = res.json() if res.headers.get('Content-Type', '').startswith('application/json') else {}
            err_msg = err_data.get('error_description', res.text)
            return f"<h3>向 Salesforce 交换 Token 失败</h3><p>{err_msg}</p><a href='/'>返回登录页</a>", 400
            
        token_data = res.json()
        access_token = token_data.get("access_token")
        instance_url = token_data.get("instance_url")
        id_url = token_data.get("id")
        
        userinfo_data = {}
        if id_url:
            user_headers = {"Authorization": f"Bearer {access_token}"}
            try:
                userinfo_res = requests.get(id_url, headers=user_headers, timeout=10)
                log_sfdc_request(id_url, "GET", user_headers, None, userinfo_res.status_code, userinfo_res.content)
            except Exception as get_err:
                log_sfdc_request(id_url, "GET", user_headers, None, error_message=str(get_err))
                raise get_err
                
            if userinfo_res.status_code == 200:
                userinfo_data = userinfo_res.json()
                
        user_fullname = userinfo_data.get("display_name", "OAuth 用户")
        user_email = userinfo_data.get("email", "oauth@company.com")
        username = userinfo_data.get("username", "")
        user_id = userinfo_data.get("user_id", "")
        
        params = {
            "sso_success": "true",
            "sessionId": access_token,
            "serverUrl": instance_url,
            "userId": user_id,
            "userFullName": user_fullname,
            "userEmail": user_email,
            "userName": username
        }
        
        redirect_target = f"/?{urllib.parse.urlencode(params)}"
        return f"""
        <html>
        <head><title>登录成功，正在跳转...</title></head>
        <body>
            <p>登录成功！正在跳转回工作台...</p>
            <script>
                window.location.href = "{redirect_target}";
            </script>
        </body>
        </html>
        """
    except Exception as e:
        return f"<h3>系统内部错误</h3><p>{str(e)}</p><a href='/'>返回登录页</a>", 500

# Backup file tree endpoints
@app.route('/api/backup', methods=['POST'])
def perform_backup():
    data = request.json or {}
    task_name = data.get('task_name') # Object label, e.g. '用户'
    bu_config_id = data.get('bu_config_id')
    subtask_key = data.get('subtask_key')
    sql = data.get('sql')
    session_id = data.get('sessionId')
    server_url = data.get('serverUrl')
    
    if not task_name or not bu_config_id or not subtask_key or not sql:
        return jsonify({"success": False, "error": "缺少必要参数(task_name, bu_config_id, subtask_key, sql)"}), 400
        
    try:
        now = datetime.datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")
        
        with db_conn() as conn:
            cursor = conn.cursor()
            
            # Query master config name for unique folder path
            cursor.execute("SELECT name FROM bu_configs WHERE id = ?", (bu_config_id,))
            cfg_row = cursor.fetchone()
            cfg_name = cfg_row[0] if cfg_row else bu_config_id
            
            # 1. Check if there are already records backed up in SQLite for this bu_config_id and subtask_key
            cursor.execute("SELECT backup_file_path, COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? GROUP BY backup_file_path", 
                           (bu_config_id, subtask_key))
            existing_backup = cursor.fetchone()
            
            if existing_backup:
                # Already exists on disk and SQLite, just return it for resumption!
                filename_val = os.path.basename(existing_backup[0])
                # Restore colons for display
                if "-" in filename_val:
                    parts = filename_val.split('-')
                    if len(parts) >= 6:
                        date_part = "-".join(parts[0:3])
                        time_part = ":".join(parts[3:6])
                        suffix = "-".join(parts[6:])
                        filename_val = f"{date_part}-{time_part}-{suffix}"
                
                # Update the count in subtasks table just in case it was not updated
                cursor.execute("UPDATE subtasks SET count = ? WHERE bu_config_id = ? AND key = ?", 
                               (f"{existing_backup[1]}条", bu_config_id, subtask_key))
                conn.commit()

                return jsonify({
                    "success": True, 
                    "resumed": True,
                    "filePath": existing_backup[0],
                    "filename": filename_val,
                    "count": existing_backup[1],
                    "message": f"检测到未完成的备份，已继续使用断点备份: {existing_backup[0]}"
                })
                
            # 2. Perform query (Salesforce SOAP query or mock query)
            records_to_backup = []
            headers_list = ["Id", "Name", "CreatedDate", "Status"] # default headers
            
            # Initialize backup progress tracking
            progress_key = (bu_config_id, subtask_key)
            BACKUP_PROGRESS[progress_key] = {'current': 0, 'total': 0, 'status': 'starting'}
            
            # Check if we should execute SOAP query on Salesforce
            soap_success = False
            if session_id and server_url:
                soap_url = f"{server_url}/services/Soap/u/58.0"
                query_body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <query xmlns="urn:partner.soap.sforce.com">
      <queryString>{html.escape(sql)}</queryString>
    </query>
  </env:Body>
</env:Envelope>"""
                backup_headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "query"}
                try:
                    res_query = requests.post(soap_url, data=query_body.encode('utf-8'), 
                                              headers=backup_headers, timeout=20)
                    log_sfdc_request(soap_url, "POST", backup_headers, query_body, res_query.status_code, res_query.content)
                    if res_query.status_code == 200:
                        root_query = ET.fromstring(res_query.content)
                        
                        # Parse SOAP records from first batch
                        def parse_records(root):
                            records = []
                            for elem in root.iter():
                                if elem.tag.split('}')[-1] == 'records':
                                    rec_data = {}
                                    for child in elem:
                                        tag = child.tag.split('}')[-1]
                                        if tag == 'type':
                                            continue
                                        if len(list(child)) > 0:
                                            nested_data = {}
                                            for sub_child in child:
                                                sub_tag = sub_child.tag.split('}')[-1]
                                                nested_data[sub_tag] = sub_child.text
                                            rec_data[tag] = nested_data
                                        else:
                                            rec_data[tag] = child.text
                                    records.append(rec_data)
                            return records
                        
                        records_to_backup.extend(parse_records(root_query))
                        BACKUP_PROGRESS[progress_key] = {'current': len(records_to_backup), 'total': 0, 'status': 'fetching'}
                        
                        # Check if there are more records (queryLocator)
                        query_locator = None
                        done = True
                        size = 0
                        for elem in root_query.iter():
                            if elem.tag.split('}')[-1] == 'done':
                                done = (elem.text and elem.text.lower() == 'true')
                                print(f"[DEBUG] Found 'done' element: {elem.text}")
                            elif elem.tag.split('}')[-1] == 'queryLocator':
                                query_locator = elem.text
                                if query_locator:
                                    print(f"[DEBUG] Found 'queryLocator': {elem.text[:50]}...")
                            elif elem.tag.split('}')[-1] == 'size':
                                if elem.text:
                                    size = int(elem.text)
                                    print(f"[DEBUG] Found 'size': {elem.text}")
                        
                        print(f"[DEBUG] Query result: done={done}, has_queryLocator={query_locator is not None}, size={size}, records_count={len(records_to_backup)}")
                        
                        # If not done, use queryMore to fetch remaining records
                        while not done and query_locator:
                            print(f"Fetching more records with queryLocator: {query_locator[:50]}...")
                            query_more_body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <queryMore xmlns="urn:partner.soap.sforce.com">
      <queryLocator>{html.escape(query_locator)}</queryLocator>
    </queryMore>
  </env:Body>
</env:Envelope>"""
                            more_headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "queryMore"}
                            res_more = requests.post(soap_url, data=query_more_body.encode('utf-8'), 
                                                    headers=more_headers, timeout=20)
                            log_sfdc_request(soap_url, "POST", more_headers, query_more_body, res_more.status_code, res_more.content)
                            
                            if res_more.status_code == 200:
                                root_more = ET.fromstring(res_more.content)
                                more_records = parse_records(root_more)
                                records_to_backup.extend(more_records)
                                print(f"Fetched {len(more_records)} more records. Total: {len(records_to_backup)}")
                                BACKUP_PROGRESS[progress_key] = {'current': len(records_to_backup), 'total': 0, 'status': 'fetching'}
                                
                                # Check if done
                                done = True
                                query_locator = None
                                for elem in root_more.iter():
                                    if elem.tag.split('}')[-1] == 'done':
                                        done = (elem.text and elem.text.lower() == 'true')
                                    elif elem.tag.split('}')[-1] == 'queryLocator':
                                        query_locator = elem.text
                            else:
                                print(f"queryMore failed with status {res_more.status_code}")
                                break
                        
                        print(f"Total records fetched: {len(records_to_backup)}")
                        BACKUP_PROGRESS[progress_key] = {'current': len(records_to_backup), 'total': len(records_to_backup), 'status': 'saving'}
                        soap_success = True
                except Exception as e:
                    log_sfdc_request(soap_url, "POST", backup_headers, query_body, error_message=str(e))
                    print(f"Salesforce query failed: {str(e)}, falling back to mock record generation.")
    
            # If SOAP query was not executed or failed, return error as we only support real data now
            if not soap_success:
                return jsonify({"success": False, "error": "查询 Salesforce 备份数据失败，请检查网络或会话生命周期。"}), 400
    
            # 3. Save records to CSV file
            folder_name = f"{date_str}-BU"
            target_dir = os.path.join(DATA_DIR, "olympus", folder_name, cfg_name, task_name)
            os.makedirs(target_dir, exist_ok=True)
            
            disk_filename = f"{date_str}-{time_str.replace(':', '-')}-备份.csv"
            file_path = os.path.join(target_dir, disk_filename)
            relative_path = os.path.relpath(file_path, DATA_DIR).replace("\\", "/")
    
            # Write to CSV
            import csv
            if records_to_backup:
                headers_list = list(records_to_backup[0].keys())
                
            with open(file_path, 'w', encoding='utf-8', newline='') as f_csv:
                writer = csv.DictWriter(f_csv, fieldnames=headers_list)
                writer.writeheader()
                for r in records_to_backup:
                    filtered_r = {k: v for k, v in r.items() if k in headers_list}
                    writer.writerow(filtered_r)
    
            # 4. Insert records into SQLite backup_records table
            for r in records_to_backup:
                rec_id = r.get('Id', r.get('id', ''))
                rec_name = r.get('Name', r.get('name', ''))
                raw_json = json.dumps(r, ensure_ascii=False)
                cursor.execute("""
                INSERT INTO backup_records (bu_config_id, subtask_key, record_id, record_name, raw_data, sync_status, backup_file_path)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """, (bu_config_id, subtask_key, rec_id, rec_name, raw_json, relative_path))
                
            # Update the count in subtasks table based on actual backed up records
            cursor.execute("UPDATE subtasks SET count = ? WHERE bu_config_id = ? AND key = ?", 
                           (f"{len(records_to_backup)}条", bu_config_id, subtask_key))
            conn.commit()
    
            # Clear backup progress
            if progress_key in BACKUP_PROGRESS:
                del BACKUP_PROGRESS[progress_key]
    
            return jsonify({
                "success": True, 
                "resumed": False,
                "filePath": relative_path,
                "filename": f"{date_str}-{time_str}-备份.csv",
                "count": len(records_to_backup)
            })
    except Exception as e:
        # Clear backup progress on error
        progress_key = (data.get('bu_config_id'), data.get('subtask_key'))
        if progress_key in BACKUP_PROGRESS:
            del BACKUP_PROGRESS[progress_key]
        return jsonify({"success": False, "error": f"创建备份文件与入库失败: {str(e)}"}), 500

@app.route('/api/backup/progress', methods=['GET'])
def get_backup_progress():
    """Get backup progress for a specific subtask"""
    bu_config_id = request.args.get('bu_config_id')
    subtask_key = request.args.get('subtask_key')
    
    if not bu_config_id or not subtask_key:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    
    progress_key = (bu_config_id, subtask_key)
    progress = BACKUP_PROGRESS.get(progress_key)
    
    if progress:
        return jsonify({
            "success": True,
            "current": progress['current'],
            "total": progress['total'],
            "status": progress['status']
        })
    else:
        return jsonify({
            "success": True,
            "current": 0,
            "total": 0,
            "status": "idle"
        })

@app.route('/api/backup/data', methods=['GET'])
def get_backup_data():
    bu_config_id = request.args.get('bu_config_id')
    subtask_key = request.args.get('subtask_key')
    file_path = request.args.get('file_path')
    
    records = []
    resolved_file_path = None
    
    if file_path:
        normalized = file_path.replace("\\", "/").strip("/")
        if normalized.startswith("olympus/"):
            resolved_file_path = os.path.join(DATA_DIR, normalized)
        else:
            resolved_file_path = os.path.join(DATA_DIR, "olympus", normalized)
    elif bu_config_id and subtask_key:
        try:
            with db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT backup_file_path FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? ORDER BY id DESC LIMIT 1",
                               (bu_config_id, subtask_key))
                row = cursor.fetchone()
                if row and row[0]:
                    normalized = row[0].replace("\\", "/").strip("/")
                    if normalized.startswith("olympus/"):
                        resolved_file_path = os.path.join(DATA_DIR, normalized)
                    else:
                        resolved_file_path = os.path.join(DATA_DIR, "olympus", normalized)
        except Exception as e:
            print(f"Failed to find backup file path in db: {str(e)}")
            
    # Read from CSV if file exists
    if resolved_file_path and os.path.exists(resolved_file_path):
        try:
            with open(resolved_file_path, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    records.append(dict(row))
            return jsonify({"success": True, "records": records, "source": "csv"})
        except Exception as e:
            print(f"Failed to read CSV file {resolved_file_path}: {str(e)}")
            
    # Fallback: read from SQLite backup_records table
    if bu_config_id and subtask_key:
        try:
            with db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT raw_data FROM backup_records WHERE bu_config_id = ? AND subtask_key = ?",
                               (bu_config_id, subtask_key))
                rows = cursor.fetchall()
                
                for r in rows:
                    try:
                        records.append(json.loads(r[0]))
                    except:
                        pass
                if records:
                    return jsonify({"success": True, "records": records, "source": "db"})
        except Exception as e:
            print(f"Failed to read from backup_records table: {str(e)}")
            
    return jsonify({"success": False, "error": "未找到对应的备份记录或文件已不存在"}), 404

@app.route('/api/backup/tree', methods=['GET'])
def get_backup_tree():
    olympus_path = os.path.join(DATA_DIR, "olympus")
    if not os.path.exists(olympus_path):
        os.makedirs(olympus_path, exist_ok=True)

    def get_directory_tree(path):
        tree = []
        try:
            for entry in os.scandir(path):
                if entry.is_dir():
                    tree.append({
                        "name": entry.name,
                        "type": "directory",
                        "path": os.path.relpath(entry.path, DATA_DIR).replace("\\", "/"),
                        "children": get_directory_tree(entry.path)
                    })
                else:
                    display_name = entry.name
                    if "-备份.csv" in entry.name:
                        # Translate dashes back to colons for display
                        parts = entry.name.split('-')
                        if len(parts) >= 6:
                            date_part = "-".join(parts[0:3])
                            time_part = ":".join(parts[3:6])
                            suffix = "-".join(parts[6:])
                            display_name = f"{date_part}-{time_part}-{suffix}"
                    
                    tree.append({
                        "name": display_name,
                        "realName": entry.name,
                        "type": "file",
                        "path": os.path.relpath(entry.path, DATA_DIR).replace("\\", "/"),
                        "size": f"{os.path.getsize(entry.path) / 1024:.2f} KB"
                    })
        except Exception as e:
            print(f"Error scanning directory {path}: {str(e)}")
        
        tree.sort(key=lambda x: (x['type'] != 'directory', x['name']))
        return tree

    # Return tree with olympus root
    tree_data = [{
        "name": "olympus",
        "type": "directory",
        "path": "olympus",
        "children": get_directory_tree(olympus_path)
    }]
    
    return jsonify(tree_data)

# Safe SOAP describe sobject helper
def describe_sobject_safe(soap_url, session_id, sobject_name):
    body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <describeSObject xmlns="urn:partner.soap.sforce.com">
      <sObjectType>{sobject_name}</sObjectType>
    </describeSObject>
  </env:Body>
</env:Envelope>"""
    try:
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": "describeSObject"
        }
        res = requests.post(soap_url, data=body.encode('utf-8'), headers=headers, timeout=15)
        log_sfdc_request(soap_url, "POST", headers, body, res.status_code, res.content)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            result_elem = None
            for elem in root.iter():
                if elem.tag.split('}')[-1] == 'result':
                    result_elem = elem
                    break
            if result_elem is not None:
                sobject_label = None
                sobject_name_res = None
                for child in result_elem:
                    tag = child.tag.split('}')[-1]
                    if tag == 'label':
                        sobject_label = child.text
                    elif tag == 'name':
                        sobject_name_res = child.text
                if sobject_name_res and sobject_label:
                    return {"name": sobject_name_res, "label": sobject_label}
    except Exception as e:
        log_sfdc_request(soap_url, "POST", headers, body, error_message=str(e))
        print(f"Failed to describe {sobject_name}: {str(e)}")
    return None

# API to fetch actual Salesforce refresh configurations and split into sub-tasks
@app.route('/api/refresh-config', methods=['POST'])
def get_refresh_config():
    data = request.json or {}
    session_id = data.get('sessionId')
    server_url = data.get('serverUrl')
    
    if not session_id or not server_url:
        return jsonify({"success": False, "error": "缺少会话 ID 或服务器 URL"}), 400
        
    soap_url = f"{server_url}/services/Soap/u/58.0"
    
    # 1. Describe BU_Config_Refresh__c
    describe_body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <describeSObject xmlns="urn:partner.soap.sforce.com">
      <sObjectType>BU_Config_Refresh__c</sObjectType>
    </describeSObject>
  </env:Body>
</env:Envelope>"""

    try:
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": "describeSObject"
        }
        try:
            res = requests.post(soap_url, data=describe_body.encode('utf-8'), headers=headers, timeout=20)
            log_sfdc_request(soap_url, "POST", headers, describe_body, res.status_code, res.content)
        except Exception as e:
            log_sfdc_request(soap_url, "POST", headers, describe_body, error_message=str(e))
            raise e
            
        if res.status_code != 200:
            return get_mock_refresh_config_response(soap_url, session_id)

        root = ET.fromstring(res.content)
        
        # Parse fields
        fields = []
        result_elem = None
        for elem in root.iter():
            if elem.tag.split('}')[-1] == 'result':
                result_elem = elem
                break
                
        if result_elem is None:
            return get_mock_refresh_config_response(soap_url, session_id)
            
        for child in result_elem:
            if child.tag.split('}')[-1] == 'fields':
                field_name = None
                field_label = None
                field_type = None
                rel_name = None
                for f_child in child:
                    f_tag = f_child.tag.split('}')[-1]
                    if f_tag == 'name':
                        field_name = f_child.text
                    elif f_tag == 'label':
                        field_label = f_child.text
                    elif f_tag == 'type':
                        field_type = f_child.text
                    elif f_tag == 'relationshipName':
                        rel_name = f_child.text
                if field_name:
                    fields.append({
                        "name": field_name, 
                        "label": field_label or field_name,
                        "type": field_type,
                        "relationshipName": rel_name
                    })

        # 2. Filter SQL fields (e.g. account_sql__c)
        sql_fields = [f for f in fields if 'sql' in f['name'].lower()]
        
        if not sql_fields:
            return get_mock_refresh_config_response(soap_url, session_id)

        # 3. Safe describe target objects for each SQL field to get confirmed API names & Labels (Using local SQLite cache)
        with db_conn() as conn_db:
            cursor_db = conn_db.cursor()
            
            mapped_objects = {}
            for f in sql_fields:
                field_name = f['name']
                
                # Query local DB cache first to avoid expensive describe calls
                cursor_db.execute("SELECT object_name, object_label FROM object_mappings WHERE field_name = ?", (field_name,))
                cache_row = cursor_db.fetchone()
                
                if cache_row:
                    mapped_objects[field_name] = {
                        "objectName": cache_row[0],
                        "objectLabel": cache_row[1],
                        "fieldName": field_name
                    }
                else:
                    prefix = field_name.lower().replace('sql__c', '').replace('_sql__c', '').replace('sql', '').strip('_')
                    
                    candidates = [
                        prefix.capitalize(),
                        f"{prefix}__c",
                        f"{prefix.capitalize()}__c"
                    ]
                    std_mappings = {
                        "user": "User",
                        "account": "Account",
                        "contact": "Contact",
                        "lead": "Lead",
                        "opportunity": "Opportunity",
                        "case": "Case"
                    }
                    if prefix in std_mappings:
                        candidates.insert(0, std_mappings[prefix])
                        
                    confirmed = None
                    for cand in candidates:
                        desc = describe_sobject_safe(soap_url, session_id, cand)
                        if desc:
                            confirmed = desc
                            break
                    
                    if confirmed:
                        # Cache in SQLite database
                        cursor_db.execute("INSERT OR REPLACE INTO object_mappings (field_name, object_name, object_label) VALUES (?, ?, ?)",
                                         (field_name, confirmed['name'], confirmed['label']))
                        conn_db.commit()
                        
                        mapped_objects[field_name] = {
                            "objectName": confirmed['name'],
                            "objectLabel": confirmed['label'],
                            "fieldName": field_name
                        }
                    else:
                        mapped_objects[field_name] = {
                            "objectName": prefix.capitalize(),
                            "objectLabel": f.get('label', prefix.capitalize()),
                            "fieldName": field_name
                        }

        # 4. Construct dynamic SOQL query (Select ALL fields dynamically)
        query_fields = ['Id', 'Name', 'CreatedDate', 'LastModifiedDate', 'OwnerId', 'Owner.Name']
        for f in fields:
            query_fields.append(f['name'])
            # If it is reference field, also query its Name field
            if f.get('type') == 'reference' and f.get('relationshipName'):
                query_fields.append(f"{f['relationshipName']}.Name")
            
        # Check if province/city fields exist
        province_field = None
        city_field = None
        for f in fields:
            fname = f['name'].lower()
            if 'province' in fname or 'state' in fname:
                province_field = f['name']
            if 'city' in fname:
                city_field = f['name']
            
        query_fields = list(set(query_fields))
        soql = f"SELECT {', '.join(query_fields)} FROM BU_Config_Refresh__c LIMIT 100"

        # 5. Execute SOAP Query
        query_body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <query xmlns="urn:partner.soap.sforce.com">
      <queryString>{html.escape(soql)}</queryString>
    </query>
  </env:Body>
</env:Envelope>"""

        query_headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "query"}
        try:
            res_query = requests.post(soap_url, data=query_body.encode('utf-8'), headers=query_headers, timeout=20)
            log_sfdc_request(soap_url, "POST", query_headers, query_body, res_query.status_code, res_query.content)
        except Exception as e:
            log_sfdc_request(soap_url, "POST", query_headers, query_body, error_message=str(e))
            raise e
        
        if res_query.status_code != 200:
            return get_mock_refresh_config_response(soap_url, session_id)
            
        root_query = ET.fromstring(res_query.content)
        
        records = []
        for elem in root_query.iter():
            if elem.tag.split('}')[-1] == 'records':
                rec_data = {}
                for child in elem:
                    tag = child.tag.split('}')[-1]
                    if tag == 'type':
                        continue
                    if len(list(child)) > 0: # nested object like Owner or custom lookups
                        nested_data = {}
                        for sub_child in child:
                            sub_tag = sub_child.tag.split('}')[-1]
                            nested_data[sub_tag] = sub_child.text
                        rec_data[tag] = nested_data
                    else:
                        rec_data[tag] = child.text
                records.append(rec_data)

        if not records:
            return get_mock_refresh_config_response(soap_url, session_id)

        # 6. Map Salesforce records into frontend model structure
        formatted_records = []
        with db_conn() as conn_db:
            cursor_db = conn_db.cursor()
            
            for idx, rec in enumerate(records):
                rec_id = rec.get('Id', '')
                name = rec.get('Name', 'BU-Config')
                
                # Resolve Owner Name from lookup relation
                owner_val = '精琢技术'
                owner_data = rec.get('Owner')
                if isinstance(owner_data, dict):
                    owner_val = owner_data.get('Name', '精琢技术')
                elif isinstance(owner_data, str):
                    owner_val = owner_data
                else:
                    owner_val = rec.get('OwnerId', '精琢技术')
                    
                # Resolve Province display value if it's a lookup field
                province_val = ""
                if province_field:
                    f_meta = next((item for item in fields if item["name"] == province_field), {})
                    if f_meta.get('type') == 'reference' and f_meta.get('relationshipName'):
                        rel_name = f_meta['relationshipName']
                        rel_data = rec.get(rel_name)
                        if isinstance(rel_data, dict):
                            province_val = rel_data.get('Name', "")
                    else:
                        province_val = rec.get(province_field, "")
                    if province_val is None:
                        province_val = ""
    
                # Resolve City display value if it's a lookup field
                city_val = ""
                if city_field:
                    f_meta = next((item for item in fields if item["name"] == city_field), {})
                    if f_meta.get('type') == 'reference' and f_meta.get('relationshipName'):
                        rel_name = f_meta['relationshipName']
                        rel_data = rec.get(rel_name)
                        if isinstance(rel_data, dict):
                            city_val = rel_data.get('Name', "")
                    else:
                        city_val = rec.get(city_field, "")
                    if city_val is None:
                        city_val = ""
    
                created_time = rec.get('CreatedDate', '')
                if created_time:
                    created_time = created_time.replace('T', ' ').split('.')[0]
                else:
                    created_time = '2026/6/17 13:45'
                    
                modified_time = rec.get('LastModifiedDate', '')
                if modified_time:
                    modified_time = modified_time.replace('T', ' ').split('.')[0]
                else:
                    modified_time = '2026/6/17 13:45'
    
                # Build sub-tasks dictionary from the SQL fields (processing placeholders against Salesforce record values)
                subtasks = {}
                for field_name, obj_info in mapped_objects.items():
                    key = obj_info['objectName'].lower().replace('__c', '')
                    sql_value = rec.get(field_name, '')
                    resolved_sql = process_sql_template(sql_value, rec)
                    
                    # Check if this subtask configuration already exists in SQLite
                    cursor_db.execute("SELECT execute, backup, run_state, backup_state, count, success_count, fail_count FROM subtasks WHERE bu_config_id = ? AND key = ?", (rec_id, key))
                    existing_sub = cursor_db.fetchone()
                    
                    if existing_sub:
                        exec_val = existing_sub[0]
                        backup_val = existing_sub[1]
                        run_state_val = existing_sub[2]
                        backup_state_val = existing_sub[3]
                        count_str = existing_sub[4] or "备份后显示"
                        if count_str == "计算中..." or count_str == "-":
                            count_str = "备份后显示"
                        success_count_val = existing_sub[5] if existing_sub[5] is not None else 0
                        fail_count_val = existing_sub[6] if existing_sub[6] is not None else 0
                    else:
                        exec_val = 1
                        backup_val = 1
                        run_state_val = 'ready'
                        backup_state_val = 'ready'
                        count_str = "备份后显示"
                        success_count_val = 0
                        fail_count_val = 0
                    
                    # Force execute: True for user/customer/account
                    is_mandatory = ('user' in key or 'account' in key or 'customer' in key)
                    if is_mandatory:
                        exec_val = 1
                    
                    # Check SQLite backup_records table to see if it actually has backup records
                    cursor_db.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ?", (rec_id, key))
                    has_backup = cursor_db.fetchone()[0] > 0
                    if has_backup:
                        backup_state_val = 'success'
                    
                    # Upsert subtask into SQLite subtasks table
                    cursor_db.execute("""
                    INSERT OR REPLACE INTO subtasks (bu_config_id, key, name, count, execute, backup, run_state, backup_state, object_api_name, field_name, sql, success_count, fail_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (rec_id, key, obj_info['objectLabel'], count_str, exec_val, backup_val, run_state_val, backup_state_val, obj_info['objectName'], field_name, resolved_sql, success_count_val, fail_count_val))
                    
                    subtasks[key] = {
                        "name": obj_info['objectLabel'],
                        "key": key,
                        "count": count_str,
                        "execute": bool(exec_val),
                        "backup": bool(backup_val),
                        "runState": run_state_val,
                        "backupState": backup_state_val,
                        "objectApiName": obj_info['objectName'],
                        "fieldName": field_name,
                        "sql": resolved_sql,
                        "successCount": success_count_val,
                        "failCount": fail_count_val
                    }
    
                progress_text_val = "进行中" if idx == 0 else "待开始"
                progress_color_val = "text-amber-500 bg-amber-500/10 border-amber-500/20" if idx == 0 else "text-slate-500 bg-slate-100 dark:bg-slate-800 border-slate-200 dark:border-slate-700"
                
                # Check if progress_text is already in SQLite (to preserve completion status)
                cursor_db.execute("SELECT progress_text, progress_color FROM bu_configs WHERE id = ?", (rec_id,))
                existing_cfg = cursor_db.fetchone()
                if existing_cfg:
                    progress_text_val = existing_cfg[0]
                    progress_color_val = existing_cfg[1]
                    
                cursor_db.execute("""
                INSERT OR REPLACE INTO bu_configs (id, name, province, city, currency, owner, created_by_name, created_by_time, modified_by_name, modified_by_time, progress_text, progress_color, work_location__c, BU_Group__c)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (rec_id, name, province_val, city_val, "CNY - 中国人民币", owner_val, "精琢技术", created_time, "精琢技术", modified_time, progress_text_val, progress_color_val, rec.get('work_location__c', ''), rec.get('BU_Group__c', '')))
    
                formatted_records.append({
                    "id": rec_id,
                    "name": name,
                    "province": province_val,
                    "city": city_val,
                    "currency": "CNY - 中国人民币",
                    "owner": owner_val,
                    "createdByName": "精琢技术",
                    "createdByTime": created_time,
                    "modifiedByName": "精琢技术",
                    "modifiedByTime": modified_time,
                    "progressText": progress_text_val,
                    "progressColor": progress_color_val,
                    "work_location__c": rec.get('work_location__c', ''),
                    "BU_Group__c": rec.get('BU_Group__c', ''),
                    "subtasks": subtasks
                })
                
            conn_db.commit()
            
        if formatted_records:
            formatted_records[0]["progressText"] = "进行中"
            formatted_records[0]["progressColor"] = "text-amber-500 bg-amber-500/10 border-amber-500/20"

        return jsonify({"success": True, "records": formatted_records})

    except Exception as e:
        print(f"Error in get_refresh_config: {str(e)}")
        return get_mock_refresh_config_response(soap_url, session_id)

# Fallback helper to return mock config if object is not in the org or describe fails
def get_mock_refresh_config_response(soap_url=None, session_id=None):
    try:
        records = get_sqlite_records(soap_url, session_id)
        return jsonify({"success": True, "records": records})
    except Exception as e:
        print(f"Failed to read sqlite database: {str(e)}")
        return jsonify({"success": True, "records": []})

# API to save user switch configurations locally in SQLite
@app.route('/api/save-config', methods=['POST'])
def save_config():
    data = request.json or {}
    bu_config_id = data.get('bu_config_id')
    subtask_key = data.get('subtask_key')
    execute = data.get('execute')
    backup = data.get('backup')
    
    if not bu_config_id or not subtask_key:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
        
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            
            if execute is not None:
                cursor.execute("UPDATE subtasks SET execute = ? WHERE bu_config_id = ? AND key = ?", 
                               (1 if execute else 0, bu_config_id, subtask_key))
            if backup is not None:
                cursor.execute("UPDATE subtasks SET backup = ? WHERE bu_config_id = ? AND key = ?", 
                               (1 if backup else 0, bu_config_id, subtask_key))
                               
            conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": f"保存配置失败: {str(e)}"}), 500

# API to fetch real subtask record counts asynchronously
@app.route('/api/subtask-counts', methods=['POST'])
def get_subtask_counts():
    data = request.json or {}
    bu_config_id = data.get('bu_config_id')
    session_id = data.get('sessionId')
    server_url = data.get('serverUrl')
    
    if not bu_config_id:
        return jsonify({"success": False, "error": "缺少必要参数 bu_config_id"}), 400
        
    soap_url = f"{server_url}/services/Soap/u/58.0" if server_url else None
    
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            
            # Query subtasks of this bu_config_id (including name for log readability)
            cursor.execute("SELECT key, sql, name FROM subtasks WHERE bu_config_id = ?", (bu_config_id,))
            rows = cursor.fetchall()

            # Parallelize the SOAP count queries so total time = single-request time
            # rather than N * single-request time.
            def _fetch_one(row):
                key = row[0]
                resolved_sql = row[1]
                task_name = row[2] or key
                real_count = None
                if soap_url and session_id and resolved_sql:
                    try:
                        real_count = query_salesforce_count(soap_url, session_id, resolved_sql)
                    except Exception as inner_ex:
                        print(f"Count query failed for [{task_name}]: {str(inner_ex)}")
                return key, task_name, resolved_sql, real_count

            results = []
            if rows:
                # Cap workers to avoid hammering Salesforce; 8 is a safe default.
                max_workers = min(8, len(rows))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(_fetch_one, row) for row in rows]
                    for future in as_completed(futures):
                        try:
                            results.append(future.result())
                        except Exception as fx:
                            print(f"Subtask count future failed: {str(fx)}")

            # Now perform DB writes sequentially on the main thread/connection.
            counts = {}
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for key, task_name, resolved_sql, real_count in results:
                if real_count is not None:
                    counts[key] = f"{real_count}条"
                    cursor.execute(
                        "UPDATE subtasks SET count = ? WHERE bu_config_id = ? AND key = ?",
                        (f"{real_count}条", bu_config_id, key)
                    )
                else:
                    counts[key] = "0条"

                # Persist the count query SQL into terminal_logs so users can review what was executed
                try:
                    log_message = f"计算子任务 [{task_name}] 数据量 -> {counts[key]}; SOQL: {resolved_sql or 'N/A'}"
                    cursor.execute("""
                        INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                        VALUES (?, ?, ?, ?)
                    """, (bu_config_id, now_str, 'info', log_message))
                except Exception as log_ex:
                    print(f"Failed to persist count SQL log: {str(log_ex)}")

            conn.commit()
        
        return jsonify({"success": True, "counts": counts})
    except Exception as e:
        print(f"Error fetching subtask counts: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

# API to save log entry
@app.route('/api/logs', methods=['POST'])
def save_terminal_log():
    data = request.json or {}
    bu_config_id = data.get('bu_config_id')
    log_type = data.get('log_type', 'info')
    message = data.get('message')
    
    if not bu_config_id or not message:
        return jsonify({"success": False, "error": "缺少必要参数(bu_config_id, message)"}), 400
        
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                VALUES (?, ?, ?, ?)
            """, (bu_config_id, now_str, log_type, message))
            conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API to retrieve logs for a config
@app.route('/api/logs/<bu_config_id>', methods=['GET'])
def get_terminal_logs(bu_config_id):
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, log_type, message FROM terminal_logs WHERE bu_config_id = ? ORDER BY id ASC", (bu_config_id,))
            rows = cursor.fetchall()
        
        logs = [{"timestamp": r[0], "type": r[1], "message": r[2]} for r in rows]
        return jsonify({"success": True, "logs": logs})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API to delete logs for a config
@app.route('/api/logs/<bu_config_id>', methods=['DELETE'])
def delete_terminal_logs(bu_config_id):
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM terminal_logs WHERE bu_config_id = ?", (bu_config_id,))
            conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API to get all tables in SQLite database
@app.route('/api/db/tables', methods=['GET'])
def get_db_tables():
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            tables = [r[0] for r in cursor.fetchall()]
        return jsonify({"success": True, "tables": tables})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API to get rows and columns for a table in SQLite
@app.route('/api/db/table-data', methods=['GET'])
def get_db_table_data():
    table_name = request.args.get('table')
    if not table_name:
        return jsonify({"success": False, "error": "缺少表名参数"}), 400
        
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            
            # Whitelist validation
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            valid_tables = [r[0] for r in cursor.fetchall()]
            if table_name not in valid_tables:
                return jsonify({"success": False, "error": f"无效的表名: {table_name}"}), 400
                
            # Get schema columns
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = [r[1] for r in cursor.fetchall()]
            
            # Get records
            cursor.execute(f"SELECT * FROM {table_name} LIMIT 1000")
            rows = cursor.fetchall()
        
        records = []
        for row in rows:
            rec = {}
            for idx, col in enumerate(columns):
                rec[col] = row[idx]
            records.append(rec)
            
        return jsonify({
            "success": True, 
            "columns": columns, 
            "records": records
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API to get failed backup records with error messages
@app.route('/api/backup/failed', methods=['GET'])
def get_failed_records():
    bu_config_id = request.args.get('bu_config_id')
    subtask_key = request.args.get('subtask_key')
    
    if not bu_config_id or not subtask_key:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT record_id, record_name, error_message, raw_data 
                FROM backup_records 
                WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'
                ORDER BY id
            """, (bu_config_id, subtask_key))
            rows = cursor.fetchall()
        
        records = []
        for row in rows:
            rec = {
                'Id': row[0],
                'Name': row[1],
                'error_message': row[2] or '未知错误'
            }
            
            # Try to parse raw_data to get additional fields
            if row[3]:
                try:
                    raw_data = json.loads(row[3])
                    # Add some key fields from raw_data if available
                    for key in ['Email', 'Username', 'Phone', 'Department']:
                        if key in raw_data:
                            rec[key] = raw_data[key]
                except:
                    pass
            
            records.append(rec)
        
        return jsonify({
            "success": True,
            "records": records
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API to update simulation progress in SQLite
@app.route('/api/update-progress', methods=['POST'])
def update_progress():
    data = request.json or {}
    bu_config_id = data.get('bu_config_id')
    progress_text = data.get('progressText')
    progress_color = data.get('progressColor')
    
    if not bu_config_id or not progress_text:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
        
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE bu_configs SET progress_text = ?, progress_color = ? WHERE id = ?", 
                           (progress_text, progress_color or '', bu_config_id))
            conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": f"更新进度失败: {str(e)}"}), 500

# Helper function to update BU_Config_Refresh__c.userStatus__c field
def update_bu_config_user_status(soap_url, session_id, bu_config_sf_id, status_value):
    """
    Update userStatus__c field on BU_Config_Refresh__c record
    status_value: '处理中', '更新完成', '部分失败'
    """
    try:
        update_xml = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <update xmlns="urn:partner.soap.sforce.com">
      <sObjects xsi:type="sf:sObject" xmlns:sf="urn:sobject.partner.soap.sforce.com">
        <sf:type>BU_Config_Refresh__c</sf:type>
        <sf:Id>{bu_config_sf_id}</sf:Id>
        <sf:userStatus__c>{html.escape(status_value)}</sf:userStatus__c>
      </sObjects>
    </update>
  </env:Body>
</env:Envelope>"""
        
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": "update",
            "Connection": "close"
        }
        
        import time
        for attempt in range(3):
            try:
                res = requests.post(soap_url, data=update_xml.encode('utf-8'), headers=headers, timeout=30)
                break
            except Exception as e:
                if attempt == 2:
                    raise e
                time.sleep(2)
        log_sfdc_request(soap_url, "POST", headers, update_xml, res.status_code, res.content)
        
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for elem in root.iter():
                if elem.tag.split('}')[-1] == 'success':
                    return elem.text.lower() == 'true'
        return False
    except Exception as e:
        log_sfdc_request(soap_url, "POST", headers, update_xml, error_message=str(e))
        print(f"Failed to update BU_Config_Refresh__c.userStatus__c: {str(e)}")
        return False


# Helper function to update BU_Config_Refresh__c.opportunityStatus__c field
def update_bu_config_opportunity_status(soap_url, session_id, bu_config_sf_id, status_value):
    try:
        update_xml = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <update xmlns="urn:partner.soap.sforce.com">
      <sObjects xsi:type="sf:sObject" xmlns:sf="urn:sobject.partner.soap.sforce.com">
        <sf:type>BU_Config_Refresh__c</sf:type>
        <sf:Id>{bu_config_sf_id}</sf:Id>
        <sf:opportunityStatus__c>{html.escape(status_value)}</sf:opportunityStatus__c>
      </sObjects>
    </update>
  </env:Body>
</env:Envelope>"""
        
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": "update",
            "Connection": "close"
        }
        
        import time
        for attempt in range(3):
            try:
                res = requests.post(soap_url, data=update_xml.encode('utf-8'), headers=headers, timeout=30)
                break
            except Exception as e:
                if attempt == 2:
                    raise e
                time.sleep(2)
        log_sfdc_request(soap_url, "POST", headers, update_xml, res.status_code, res.content)
        
        if res.status_code == 200:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(res.content)
            for elem in root.iter():
                if elem.tag.split('}')[-1] == 'success':
                    return elem.text.lower() == 'true'
        return False
    except Exception as e:
        log_sfdc_request(soap_url, "POST", headers, update_xml, error_message=str(e))
        print(f"Failed to update BU_Config_Refresh__c.opportunityStatus__c: {str(e)}")
        return False

def update_custom_label(server_url, session_id, label_name, label_value):
    try:
        base_url = server_url.split('/services/')[0] if '/services/' in server_url else server_url
        query_url = f"{base_url}/services/data/v58.0/tooling/query/?q=SELECT+Id+FROM+ExternalString+WHERE+Name='{label_name}'"
        headers = {
            "Authorization": f"Bearer {session_id}",
            "Content-Type": "application/json",
            "Connection": "close"
        }
        
        import time
        for attempt in range(3):
            try:
                res = requests.get(query_url, headers=headers, timeout=30)
                break
            except Exception as e:
                if attempt == 2: raise e
                time.sleep(2)
                
        log_sfdc_request(query_url, "GET", headers, None, res.status_code, res.content)
        
        if res.status_code == 200:
            data = res.json()
            if data.get('size', 0) > 0 and data.get('records'):
                label_id = data['records'][0]['Id']
                
                # ExternalString update in Tooling API directly uses the 'Value' field.
                # Metadata envelope and FullName are NOT supported for this object.
                payload = {
                    "Value": str(label_value)
                }
                
                patch_url = f"{base_url}/services/data/v58.0/tooling/sobjects/ExternalString/{label_id}"
                
                for attempt in range(3):
                    try:
                        patch_res = requests.patch(patch_url, json=payload, headers=headers, timeout=30)
                        break
                    except Exception as e:
                        if attempt == 2: raise e
                        time.sleep(2)
                log_sfdc_request(patch_url, "PATCH", headers, payload, patch_res.status_code, patch_res.content)
                
                if patch_res.status_code in (200, 204):
                    print(f"Successfully updated Custom Label {label_name} to {label_value}")
                    # Also log to terminal
                    with db_conn() as conn_log:
                        cursor_log = conn_log.cursor()
                        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        cursor_log.execute("""
                            INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                            VALUES (?, ?, 'success', ?)
                        """, ('SYSTEM', now_str, f"成功更新自定义标签 {label_name} 为 {label_value}"))
                        conn_log.commit()
                    return True
                else:
                    err_txt = patch_res.text
                    print(f"Failed to update Custom Label {label_name}: {err_txt}")
                    with db_conn() as conn_log:
                        cursor_log = conn_log.cursor()
                        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        cursor_log.execute("""
                            INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                            VALUES (?, ?, 'error', ?)
                        """, ('SYSTEM', now_str, f"更新自定义标签 {label_name} 失败: {err_txt}"))
                        conn_log.commit()
                    return False
            else:
                print(f"Custom Label {label_name} not found")
                return False
        else:
            print(f"Failed to query Custom Label {label_name}: {res.text}")
            return False
    except Exception as e:
        print(f"Exception updating Custom Label {label_name}: {str(e)}")
        return False

# Helper function to update BU_Config_Refresh__c.accountStatus__c field
def update_bu_config_account_status(soap_url, session_id, bu_config_sf_id, status_value):
    """
    Update accountStatus__c field on BU_Config_Refresh__c record
    status_value: '处理中', '更新完成', '部分失败'
    """
    try:
        update_xml = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <update xmlns="urn:partner.soap.sforce.com">
      <sObjects xsi:type="sf:sObject" xmlns:sf="urn:sobject.partner.soap.sforce.com">
        <sf:type>BU_Config_Refresh__c</sf:type>
        <sf:Id>{bu_config_sf_id}</sf:Id>
        <sf:accountStatus__c>{html.escape(status_value)}</sf:accountStatus__c>
      </sObjects>
    </update>
  </env:Body>
</env:Envelope>"""
        
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": "update",
            "Connection": "close"
        }
        
        import time
        for attempt in range(3):
            try:
                res = requests.post(soap_url, data=update_xml.encode('utf-8'), headers=headers, timeout=30)
                break
            except Exception as e:
                if attempt == 2:
                    raise e
                time.sleep(2)
        log_sfdc_request(soap_url, "POST", headers, update_xml, res.status_code, res.content)
        
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for elem in root.iter():
                if elem.tag.split('}')[-1] == 'success':
                    return elem.text.lower() == 'true'
        return False
    except Exception as e:
        log_sfdc_request(soap_url, "POST", headers, update_xml, error_message=str(e))
        print(f"Failed to update BU_Config_Refresh__c.accountStatus__c: {str(e)}")
        return False

# Helper function to execute anonymous Apex code via Tooling REST API
def execute_apex_anonymous(server_url, session_id, apex_code):
    """
    Execute anonymous Apex code using Tooling REST API.
    Returns: dict with keys: success, compiled, exceptionMessage, compileProblem, line, column
    """
    import urllib.parse
    import re
    
    # URL encode the Apex code
    encoded_apex = urllib.parse.quote(apex_code)
    
    # Extract the base URL from the server_url (e.g. from https://.../services/Soap/u/58.0/00D...)
    # We just want the protocol and the host part
    match = re.match(r'(https?://[^/]+)', server_url)
    base_url = match.group(1) if match else server_url
    
    # Use GET method for Tooling API executeAnonymous
    tooling_url = f"{base_url}/services/data/v60.0/tooling/executeAnonymous/?anonymousBody={encoded_apex}"
    
    headers = {
        "Authorization": f"Bearer {session_id}"
    }
    
    try:
        res = requests.get(tooling_url, headers=headers, timeout=60)
        # Log without the full query string in URL to save space, put code in body
        log_sfdc_request(tooling_url.split('?')[0], "GET", headers, f"anonymousBody={apex_code}", res.status_code, res.content)
        
        if res.status_code == 200:
            return res.json()
        else:
            return {
                "success": False,
                "compiled": False,
                "exceptionMessage": f"HTTP {res.status_code}: {res.text[:500]}"
            }
    except Exception as e:
        log_sfdc_request(tooling_url.split('?')[0], "GET", headers, f"anonymousBody={apex_code}", error_message=str(e))
        return {
            "success": False,
            "compiled": False,
            "exceptionMessage": str(e)
        }

# API to update subtask records (simulating updating Salesforce records, or doing real updates if needed)
from concurrent.futures import ThreadPoolExecutor, as_completed
def update_sobjects_salesforce_single_batch(soap_url, session_id, object_api_name, batch, subtask_key):
    # Predefined fields to clear for User
    user_clear_fields = {"bu_province_id__c": "BU_Province_ID__c", 
                         "bu_province_text__c": "BU_Province_Text__c", 
                         "bu__c": "BU__c", 
                         "community__c": "Community__c", 
                         "provincebu__c": "ProvinceBU__c",
                         "region__c": "Region__c"}
    
    # Predefined fields to clear for Account (客户)
    account_clear_fields = {"bu__c": "BU__c",
                           "bu_provice__c": "BU_provice__c",
                           "community__c": "Community__c",
                           "region__c": "Region__c"}
    # Predefined fields to clear for Opportunity (询价)
    opportunity_clear_fields = {"bu__c": "BU__c",
                               "bu_province__c": "BU_province__c",
                               "community__c": "Community__c",
                               "region__c": "Region__c"}


    sobjects_xml = []
    for r in batch:
        rec_id = r.get('Id', r.get('id', ''))
        fields_xml = []
        fields_to_null = []
        
        # For Opportunity, map the lastmonth fields before clearing
        if subtask_key == 'opportunity':
            bu_c = ""
            community_c = ""
            region_c = ""
            prov_name = ""
            
            for k_orig, v_orig in r.items():
                kl = k_orig.lower()
                if kl == 'bu__c' and v_orig:
                    bu_c = str(v_orig)
                elif kl == 'community__c' and v_orig:
                    community_c = str(v_orig)
                elif kl == 'region__c' and v_orig:
                    region_c = str(v_orig)
                elif kl == 'bu_province__r' and v_orig:
                    if isinstance(v_orig, dict):
                        prov_name = str(v_orig.get('Name', v_orig.get('name', '')))
                    else:
                        prov_name = str(v_orig)
            
            if prov_name:
                fields_xml.append(f"<sf:BU_province_lastmonth__c>{html.escape(prov_name)}</sf:BU_province_lastmonth__c>")
            else:
                fields_to_null.append("BU_province_lastmonth__c")
                
            if bu_c:
                fields_xml.append(f"<sf:BU_lastmonth__c>{html.escape(bu_c)}</sf:BU_lastmonth__c>")
            else:
                fields_to_null.append("BU_lastmonth__c")
                
            if community_c:
                fields_xml.append(f"<sf:Community_lastmonth__c>{html.escape(community_c)}</sf:Community_lastmonth__c>")
            else:
                fields_to_null.append("Community_lastmonth__c")
                
            if region_c:
                fields_xml.append(f"<sf:Region_lastmonth__c>{html.escape(region_c)}</sf:Region_lastmonth__c>")
            else:
                fields_to_null.append("Region_lastmonth__c")
        
        for k, v in r.items():
            kl = k.lower()
            # Skip system/read-only fields
            if kl in ['id', 'name', 'createddate', 'lastmodifieddate', 'type']:
                continue
            
            # For User: BU_Change__c is force-set to true below, skip original value here
            if subtask_key == 'user' and kl == 'bu_change__c':
                continue
            
            # For Account: Skip Acc_Record_Type__c (we need to read it but not update it)
            if subtask_key == 'account' and kl == 'acc_record_type__c':
                continue
                
            # For Opportunity: Skip Opportunity_Category__c (no processing, no nulling)
            if subtask_key == 'opportunity' and kl == 'opportunity_category__c':
                continue
                
            # Skip the custom mapped lastmonth fields to prevent duplicates
            if subtask_key == 'opportunity' and kl in ['bu_province_lastmonth__c', 'bu_lastmonth__c', 'community_lastmonth__c', 'region_lastmonth__c']:
                continue
            
            # Check if it should be cleared
            if subtask_key == 'user' and kl in user_clear_fields:
                fields_to_null.append(user_clear_fields[kl])
            elif subtask_key == 'account' and kl in account_clear_fields:
                fields_to_null.append(account_clear_fields[kl])
            elif subtask_key == 'opportunity' and kl in opportunity_clear_fields:
                fields_to_null.append(opportunity_clear_fields[kl])
            elif k.endswith('__c'):
                if v is not None and v != '':
                    escaped_val = html.escape(str(v))
                    fields_xml.append(f"<sf:{k}>{escaped_val}</sf:{k}>")
                else:
                    fields_to_null.append(k)
        
        # Explicitly ensure the user fields are included in fieldsToNull for User
        if subtask_key == 'user':
            for kl, orig_name in user_clear_fields.items():
                if orig_name not in fields_to_null:
                    fields_to_null.append(orig_name)
            # Force BU_Change__c to true on User updates (required field)
            fields_xml.append("<sf:BU_Change__c>true</sf:BU_Change__c>")
        
        # Explicitly ensure the account fields are included in fieldsToNull for Account
        if subtask_key == 'account':
            for kl, orig_name in account_clear_fields.items():
                if orig_name not in fields_to_null:
                    fields_to_null.append(orig_name)
        elif subtask_key == 'opportunity':
            for kl, orig_name in opportunity_clear_fields.items():
                if orig_name not in fields_to_null:
                    fields_to_null.append(orig_name)
        
        unique_fields_to_null = list(set(fields_to_null))
        for f in unique_fields_to_null:
            fields_xml.append(f"<sf:fieldsToNull>{f}</sf:fieldsToNull>")
            
        sobject_block = f"""
        <sObjects xsi:type="sf:sObject" xmlns:sf="urn:sobject.partner.soap.sforce.com">
            <sf:type>{object_api_name}</sf:type>
            <sf:Id>{rec_id}</sf:Id>
            {"".join(fields_xml)}
        </sObjects>
        """
        sobjects_xml.append(sobject_block)
        
    soap_body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <update xmlns="urn:partner.soap.sforce.com">
      {"".join(sobjects_xml)}
    </update>
  </env:Body>
</env:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=UTF-8",
        "SOAPAction": "update",
        "Connection": "close"
    }
    
    results = []
    import time
    for attempt in range(3):
        try:
            res = requests.post(soap_url, data=soap_body.encode('utf-8'), headers=headers, timeout=60)
            break
        except Exception as e:
            if attempt == 2:
                raise e
            print(f"Request failed (attempt {attempt+1}): {str(e)}. Retrying in 2 seconds...")
            time.sleep(2)
            
    try:
        log_sfdc_request(soap_url, "POST", headers, soap_body, res.status_code, res.content)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for elem in root.iter():
                if elem.tag.split('}')[-1] == 'result':
                    res_data = {}
                    for child in elem:
                        tag = child.tag.split('}')[-1]
                        if tag == 'success':
                            res_data['success'] = (child.text.lower() == 'true')
                        elif tag == 'id':
                            res_data['id'] = child.text
                        elif tag == 'errors':
                            err_msg = ""
                            status_code = ""
                            for err_child in child:
                                err_tag = err_child.tag.split('}')[-1]
                                if err_tag == 'message':
                                    err_msg = err_child.text
                                elif err_tag == 'statusCode':
                                    status_code = err_child.text
                            res_data['error'] = f"{status_code}: {err_msg}"
                    results.append(res_data)
        else:
            try:
                root = ET.fromstring(res.content)
                faultstring_elem = None
                for elem in root.iter():
                    if elem.tag.split('}')[-1] == 'faultstring':
                        faultstring_elem = elem
                        break
                err_msg = faultstring_elem.text if faultstring_elem is not None else f"HTTP Status {res.status_code}"
            except:
                err_msg = f"HTTP Status {res.status_code}"
            for r in batch:
                results.append({'id': r.get('Id', r.get('id', '')), 'success': False, 'error': err_msg})
    except Exception as e:
        log_sfdc_request(soap_url, "POST", headers, soap_body, error_message=str(e))
        for r in batch:
            results.append({'id': r.get('Id', r.get('id', '')), 'success': False, 'error': str(e)})
            
    # Ensure result count matches batch size
    if len(results) != len(batch):
        padded_results = []
        for idx, r in enumerate(batch):
            rec_id = r.get('Id', r.get('id', ''))
            matched = None
            for res_item in results:
                if res_item.get('id') == rec_id:
                    matched = res_item
                    break
            if not matched and idx < len(results):
                matched = results[idx]
            if not matched:
                matched = {'id': rec_id, 'success': False, 'error': 'Failed to get result from Salesforce'}
            padded_results.append(matched)
        return padded_results
        
    return results

def update_sobjects_salesforce(soap_url, session_id, object_api_name, records, subtask_key):
    # Configured per object key (batch size & thread counts)
    object_configs = {
        'user': {'chunk_size': 1, 'max_workers': 6}
    }
    config = object_configs.get(subtask_key, {'chunk_size': 200, 'max_workers': 5})
    chunk_size = config['chunk_size']
    max_workers = config['max_workers']

    # Partition records into chunks
    chunks = [records[i:i+chunk_size] for i in range(0, len(records), chunk_size)]
    
    results_map = {}
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_chunk_idx = {}
        for idx, chunk in enumerate(chunks):
            future = executor.submit(
                update_sobjects_salesforce_single_batch, 
                soap_url, session_id, object_api_name, chunk, subtask_key
            )
            future_to_chunk_idx[future] = idx
            
        for future in as_completed(future_to_chunk_idx):
            chunk_idx = future_to_chunk_idx[future]
            chunk = chunks[chunk_idx]
            try:
                chunk_results = future.result()
            except Exception as e:
                chunk_results = [{'id': r.get('Id', r.get('id', '')), 'success': False, 'error': str(e)} for r in chunk]
                
            for item_idx, res_item in enumerate(chunk_results):
                record_idx = chunk_idx * chunk_size + item_idx
                results_map[record_idx] = res_item
                
    ordered_results = [results_map[i] for i in range(len(records))]
    return ordered_results

# API to reset subtask records
@app.route('/api/subtask/reset', methods=['POST'])
def reset_subtask():
    data = request.json or {}
    bu_config_id = data.get('bu_config_id')
    subtask_key = data.get('subtask_key')
    
    if not bu_config_id or not subtask_key:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
        
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            
            # Delete backup records and file data is not deleted to allow history
            cursor.execute("DELETE FROM backup_records WHERE bu_config_id = ? AND subtask_key = ?", (bu_config_id, subtask_key))
            
            # Reset subtask state
            cursor.execute("""
                UPDATE subtasks 
                SET run_state = 'ready', backup_state = 'ready', count = '备份后显示', success_count = 0, fail_count = 0
                WHERE bu_config_id = ? AND key = ?
            """, (bu_config_id, subtask_key))
            
            # Log this action
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("SELECT name FROM subtasks WHERE bu_config_id = ? AND key = ?", (bu_config_id, subtask_key))
            row = cursor.fetchone()
            task_name = row[0] if row else subtask_key
            cursor.execute("""
                INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                VALUES (?, ?, 'info', ?)
            """, (bu_config_id, now_str, f"[手动触发] 子任务 [{task_name}] 数据已重置，可重新操作。"))
            
            conn.commit()
            
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": f"重置失败: {str(e)}"}), 500

@app.route('/api/subtask/status', methods=['GET'])
def get_subtask_status():
    bu_config_id = request.args.get('bu_config_id')
    subtask_key = request.args.get('subtask_key')
    if not bu_config_id or not subtask_key:
        return jsonify({"success": False, "error": "缺少参数"}), 400
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT success_count, fail_count, run_state FROM subtasks WHERE bu_config_id = ? AND key = ?", (bu_config_id, subtask_key))
            row = cursor.fetchone()
        if row:
            return jsonify({
                "success": True,
                "successCount": row[0] or 0,
                "failCount": row[1] or 0,
                "runState": row[2] or 'ready'
            })
        return jsonify({"success": False, "error": "未找到记录"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/subtask/update', methods=['POST'])
def update_subtask():
    data = request.json or {}
    bu_config_id = data.get('bu_config_id')
    subtask_key = data.get('subtask_key')
    session_id = data.get('sessionId')
    server_url = data.get('serverUrl')
    
    if not bu_config_id or not subtask_key:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
        
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            
            # 1. Fetch pending (non-success) backup records for this subtask
            # 限制每次最多处理 500 条，避免一次性处理太多导致卡死
            cursor.execute("SELECT id, record_id, record_name, raw_data FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND (sync_status IS NULL OR sync_status != 'success') LIMIT 500", (bu_config_id, subtask_key))
            backup_rows = cursor.fetchall()
            
            print(f"[UPDATE-DEBUG] Subtask '{subtask_key}': Found {len(backup_rows)} pending records (limited to 500 per batch)")
            
            if not backup_rows:
                # Let's count existing successes/fails to return correct totals
                cursor.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                success_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
                fail_count = cursor.fetchone()[0]
                return jsonify({"success": True, "successCount": success_count, "failCount": fail_count, "message": "没有找到需要更新的备份记录"})
            
            # 设置运行状态为 running_update，让前端知道正在更新
            cursor.execute("UPDATE subtasks SET run_state = 'running_update' WHERE bu_config_id = ? AND key = ?", (bu_config_id, subtask_key))
            conn.commit()
            print(f"[UPDATE-DEBUG] Set run_state to 'running_update'")
            
            # 重新统计实际的总记录数，确保 count 字段准确
            cursor.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ?", (bu_config_id, subtask_key))
            actual_total = cursor.fetchone()[0]
            cursor.execute("UPDATE subtasks SET count = ? WHERE bu_config_id = ? AND key = ?", (f"{actual_total}条", bu_config_id, subtask_key))
            conn.commit()
            print(f"[UPDATE-DEBUG] Updated count to actual total: {actual_total}")
                
            cursor.execute("SELECT object_api_name FROM subtasks WHERE bu_config_id = ? AND key = ?", (bu_config_id, subtask_key))
            obj_row = cursor.fetchone()
            object_api_name = obj_row[0] if obj_row else 'User'
        
        success_count = 0
        fail_count = 0
        
        if session_id and server_url and subtask_key == 'user':
            # Update BU_Config_Refresh__c.userStatus__c to '处理中' at the start
            soap_url = f"{server_url}/services/Soap/u/58.0"
            update_bu_config_user_status(soap_url, session_id, bu_config_id, '处理中')
            
            # Do real Salesforce update for User!
            records_to_update = []
            db_row_map = {} # map index to db row ID
            for idx, row in enumerate(backup_rows):
                row_db_id = row[0]
                rec_id = row[1]
                raw_data_str = row[3]
                try:
                    record_data = json.loads(raw_data_str)
                except Exception:
                    record_data = {}
                if 'Id' not in record_data and 'id' not in record_data:
                    record_data['Id'] = rec_id
                records_to_update.append(record_data)
                db_row_map[idx] = row_db_id
            
            # Configured per object key (batch size & thread counts)
            object_configs = {
                'user': {'chunk_size': 1, 'max_workers': 6}
            }
            config = object_configs.get(subtask_key, {'chunk_size': 200, 'max_workers': 5})
            chunk_size = config['chunk_size']
            max_workers = config['max_workers']

            indexed_records = list(enumerate(records_to_update))
            chunks = [indexed_records[i:i+chunk_size] for i in range(0, len(indexed_records), chunk_size)]
            
            # Accumulate pending updates and flush in batches to reduce DB lock contention
            pending_updates = []  # list of (row_db_id, is_success, err_msg)
            completed_chunks = 0
            
            def flush_pending():
                nonlocal pending_updates
                if not pending_updates:
                    return
                with db_conn() as conn_write:
                    cursor_write = conn_write.cursor()
                    for row_db_id, is_success, err_msg in pending_updates:
                        if is_success:
                            cursor_write.execute("UPDATE backup_records SET sync_status = 'success', error_message = NULL WHERE id = ?", (row_db_id,))
                        else:
                            cursor_write.execute("UPDATE backup_records SET sync_status = 'failed', error_message = ? WHERE id = ?", (err_msg, row_db_id))
                    
                    # Recalculate totals
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                    sc = cursor_write.fetchone()[0]
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
                    fc = cursor_write.fetchone()[0]
                    
                    cursor_write.execute("UPDATE subtasks SET success_count = ?, fail_count = ? WHERE bu_config_id = ? AND key = ?", 
                                         (sc, fc, bu_config_id, subtask_key))
                    conn_write.commit()
                pending_updates = []
                return sc, fc
            
            pause_key = (bu_config_id, subtask_key)
            PAUSE_CONTROL[pause_key] = {'paused': False}
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_chunk = {}
                for chunk in chunks:
                    batch_records = [r for idx, r in chunk]
                    future = executor.submit(
                        update_sobjects_salesforce_single_batch, 
                        soap_url, session_id, object_api_name, batch_records, subtask_key
                    )
                    future_to_chunk[future] = chunk
                
                for future in as_completed(future_to_chunk):
                    # Check pause flag before processing result
                    if PAUSE_CONTROL.get(pause_key, {}).get('paused', False):
                        # Update run_state to paused and save current progress
                        with db_conn() as conn_pause:
                            cursor_pause = conn_pause.cursor()
                            cursor_pause.execute("UPDATE subtasks SET run_state = 'paused' WHERE bu_config_id = ? AND key = ?", 
                                               (bu_config_id, subtask_key))
                            conn_pause.commit()
                        # Flush any pending updates before pausing
                        flush_pending()
                        return jsonify({
                            "success": True,
                            "paused": True,
                            "successCount": success_count,
                            "failCount": fail_count,
                            "message": f"更新已暂停。成功: {success_count} 条，失败: {fail_count} 条。"
                        })
                    
                    chunk = future_to_chunk[future]
                    try:
                        chunk_results = future.result()
                    except Exception as e:
                        chunk_results = [{'id': r.get('Id', r.get('id', '')), 'success': False, 'error': str(e)} for idx, r in chunk]
                    
                    completed_chunks += 1
                    
                    # Accumulate results
                    for item_idx, res_info in enumerate(chunk_results):
                        original_idx = chunk[item_idx][0]
                        row_db_id = db_row_map.get(original_idx)
                        if not row_db_id:
                            continue
                        is_success = res_info.get('success', False)
                        err_msg = res_info.get('error', 'Salesforce update failed without error message') if not is_success else None
                        pending_updates.append((row_db_id, is_success, err_msg))
                    
                    # Flush in batches of 5 chunks or at the end
                    if completed_chunks % 5 == 0 or completed_chunks == len(chunks):
                        result = flush_pending()
                        if result:
                            success_count, fail_count = result
            
            # Clean up pause control after completion
            if pause_key in PAUSE_CONTROL:
                del PAUSE_CONTROL[pause_key]
        
        elif session_id and server_url and subtask_key == 'account':
            # Handle Account updates with batch execution for 契約 type accounts
            soap_url = f"{server_url}/services/Soap/u/58.0"
            
            # Update BU_Config_Refresh__c.accountStatus__c to '处理中' at the start
            update_bu_config_account_status(soap_url, session_id, bu_config_id, '处理中')
            
            # Do real Salesforce update for Account!
            records_to_update = []
            db_row_map = {} # map index to db row ID
            contract_accounts = []  # Collect accounts with Acc_Record_Type__c == '契約'
            
            for idx, row in enumerate(backup_rows):
                row_db_id = row[0]
                rec_id = row[1]
                raw_data_str = row[3]
                try:
                    record_data = json.loads(raw_data_str)
                except Exception:
                    record_data = {}
                if 'Id' not in record_data and 'id' not in record_data:
                    record_data['Id'] = rec_id
                records_to_update.append(record_data)
                db_row_map[idx] = row_db_id
                
                # Check if this is a contract account (契約)
                acc_record_type = record_data.get('Acc_Record_Type__c', record_data.get('acc_record_type__c', ''))
                print(f"Account {rec_id}: Acc_Record_Type__c = '{acc_record_type}'")
                if acc_record_type == '契約':
                    contract_accounts.append(rec_id)
                    print(f"  -> Added to contract_accounts list")
            
            print(f"Total contract accounts identified: {len(contract_accounts)}")
            
            # Configured per object key (batch size & thread counts)
            # 使用较小的批次避免卡死，但增加线程数提高并发
            object_configs = {
                'account': {'chunk_size': 20, 'max_workers': 2}
            }
            config = object_configs.get(subtask_key, {'chunk_size': 10, 'max_workers': 1})
            chunk_size = config['chunk_size']
            max_workers = config['max_workers']

            indexed_records = list(enumerate(records_to_update))
            chunks = [indexed_records[i:i+chunk_size] for i in range(0, len(indexed_records), chunk_size)]
            
            # Accumulate pending updates and flush in batches to reduce DB lock contention
            pending_updates = []  # list of (row_db_id, is_success, err_msg)
            completed_chunks = 0
            
            def flush_pending():
                nonlocal pending_updates
                if not pending_updates:
                    return
                with db_conn() as conn_write:
                    cursor_write = conn_write.cursor()
                    for row_db_id, is_success, err_msg in pending_updates:
                        if is_success:
                            cursor_write.execute("UPDATE backup_records SET sync_status = 'success', error_message = NULL WHERE id = ?", (row_db_id,))
                        else:
                            cursor_write.execute("UPDATE backup_records SET sync_status = 'failed', error_message = ? WHERE id = ?", (err_msg, row_db_id))
                    
                    # Recalculate totals
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                    sc = cursor_write.fetchone()[0]
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
                    fc = cursor_write.fetchone()[0]
                    
                    cursor_write.execute("UPDATE subtasks SET success_count = ?, fail_count = ? WHERE bu_config_id = ? AND key = ?", 
                                         (sc, fc, bu_config_id, subtask_key))
                    conn_write.commit()
                pending_updates = []
                return sc, fc
            
            pause_key = (bu_config_id, subtask_key)
            PAUSE_CONTROL[pause_key] = {'paused': False}
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_chunk = {}
                for chunk in chunks:
                    batch_records = [r for idx, r in chunk]
                    future = executor.submit(
                        update_sobjects_salesforce_single_batch, 
                        soap_url, session_id, object_api_name, batch_records, subtask_key
                    )
                    future_to_chunk[future] = chunk
                
                for future in as_completed(future_to_chunk):
                    # Check pause flag before processing result
                    if PAUSE_CONTROL.get(pause_key, {}).get('paused', False):
                        # Update run_state to paused and save current progress
                        with db_conn() as conn_pause:
                            cursor_pause = conn_pause.cursor()
                            cursor_pause.execute("UPDATE subtasks SET run_state = 'paused' WHERE bu_config_id = ? AND key = ?", 
                                               (bu_config_id, subtask_key))
                            conn_pause.commit()
                        # Flush any pending updates before pausing
                        flush_pending()
                        return jsonify({
                            "success": True,
                            "paused": True,
                            "successCount": success_count,
                            "failCount": fail_count,
                            "message": f"更新已暂停。成功: {success_count} 条，失败: {fail_count} 条。"
                        })
                    
                    chunk = future_to_chunk[future]
                    try:
                        chunk_results = future.result()
                    except Exception as e:
                        chunk_results = [{'id': r.get('Id', r.get('id', '')), 'success': False, 'error': str(e)} for idx, r in chunk]
                    
                    completed_chunks += 1
                    
                    # Accumulate results
                    for item_idx, res_info in enumerate(chunk_results):
                        original_idx = chunk[item_idx][0]
                        row_db_id = db_row_map.get(original_idx)
                        if not row_db_id:
                            continue
                        is_success = res_info.get('success', False)
                        err_msg = res_info.get('error', 'Salesforce update failed without error message') if not is_success else None
                        pending_updates.append((row_db_id, is_success, err_msg))
                    
                    # Flush in batches of 5 chunks or at the end
                    if completed_chunks % 5 == 0 or completed_chunks == len(chunks):
                        result = flush_pending()
                        if result:
                            success_count, fail_count = result
            
            # Clean up pause control after completion
            if pause_key in PAUSE_CONTROL:
                del PAUSE_CONTROL[pause_key]
            
            # After all account updates are done, trigger UpdateTheAgreementBatch for contract accounts
            print(f"Checking if should trigger batch. contract_accounts count: {len(contract_accounts)}")
            if contract_accounts:
                print(f"Triggering UpdateTheAgreementBatch for {len(contract_accounts)} contract accounts")
                # SOQL IN clause has a limit of 1000 values; chunk by 500 to be safe.
                # Each batch call submits one Apex Batch job to SFDC.
                BATCH_TRIGGER_CHUNK = 500
                trigger_chunks = [contract_accounts[i:i+BATCH_TRIGGER_CHUNK] 
                                  for i in range(0, len(contract_accounts), BATCH_TRIGGER_CHUNK)]
                
                trigger_success = 0
                trigger_failed = 0
                
                for chunk_idx, id_chunk in enumerate(trigger_chunks):
                    try:
                        account_ids_str = "','".join(id_chunk)
                        apex_code = (
                            f"List<Account> accList = [SELECT Id FROM Account WHERE Id IN ('{account_ids_str}')];"
                            f"Database.executeBatch(new UpdateTheAgreementBatch(accList), 1);"
                        )
                        
                        print(f"Executing Apex code for chunk {chunk_idx+1}/{len(trigger_chunks)}")
                        result = execute_apex_anonymous(server_url, session_id, apex_code)
                        
                        print(f"Batch trigger result: compiled={result.get('compiled')}, success={result.get('success')}")
                        if result.get('exceptionMessage'):
                            print(f"  exceptionMessage: {result.get('exceptionMessage')}")
                        if result.get('compileProblem'):
                            print(f"  compileProblem: {result.get('compileProblem')}")
                        
                        if result.get('compiled') and result.get('success'):
                            trigger_success += len(id_chunk)
                            print(f"Triggered UpdateTheAgreementBatch chunk {chunk_idx+1}/{len(trigger_chunks)} ({len(id_chunk)} accounts)")
                        else:
                            trigger_failed += len(id_chunk)
                            error_msg = result.get('exceptionMessage') or result.get('compileProblem') or 'Unknown error'
                            print(f"Failed UpdateTheAgreementBatch chunk {chunk_idx+1}: {error_msg}")
                            with db_conn() as conn_err:
                                cursor_err = conn_err.cursor()
                                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                cursor_err.execute("""
                                    INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                                    VALUES (?, ?, 'warning', ?)
                                """, (bu_config_id, now_str, f"UpdateTheAgreementBatch 触发失败 (批次{chunk_idx+1}): {error_msg}"))
                                conn_err.commit()
                    except Exception as e:
                        trigger_failed += len(id_chunk)
                        print(f"Exception triggering UpdateTheAgreementBatch chunk {chunk_idx+1}: {str(e)}")
                        with db_conn() as conn_err:
                            cursor_err = conn_err.cursor()
                            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            cursor_err.execute("""
                                INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                                VALUES (?, ?, 'error', ?)
                            """, (bu_config_id, now_str, f"UpdateTheAgreementBatch 触发异常 (批次{chunk_idx+1}): {str(e)}"))
                            conn_err.commit()
                
                # Log a summary
                print(f"Batch trigger summary: success={trigger_success}, failed={trigger_failed}")
                with db_conn() as conn_log:
                    cursor_log = conn_log.cursor()
                    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor_log.execute("""
                        INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                        VALUES (?, ?, 'info', ?)
                    """, (bu_config_id, now_str, 
                          f"契約客户 UpdateTheAgreementBatch 触发完成: 成功 {trigger_success} 条，失败 {trigger_failed} 条 (共 {len(trigger_chunks)} 个批次)"))
                    conn_log.commit()
            else:
                print("No contract accounts found, skipping batch trigger")
        

        elif session_id and server_url and subtask_key == 'opportunity':
            soap_url = f"{server_url}/services/Soap/u/58.0"
            
            # 1. Update opportunityCTOM Custom Label to false
            update_custom_label(server_url, session_id, 'opportunityCTOM', 'false')
            
            # 2. Update opportunityStatus__c to '处理中'
            update_bu_config_opportunity_status(soap_url, session_id, bu_config_id, '处理中')
            
            # 3. Prepare records
            records_to_update = []
            db_row_map = {}
            for idx, row in enumerate(backup_rows):
                row_db_id = row[0]
                rec_id = row[1]
                raw_data_str = row[3]
                try:
                    record_data = json.loads(raw_data_str)
                except Exception:
                    record_data = {}
                if 'Id' not in record_data and 'id' not in record_data:
                    record_data['Id'] = rec_id
                records_to_update.append(record_data)
                db_row_map[idx] = row_db_id
            
            chunk_size = 10
            max_workers = 1
            
            indexed_records = list(enumerate(records_to_update))
            chunks = [indexed_records[i:i+chunk_size] for i in range(0, len(indexed_records), chunk_size)]
            
            pending_updates = []
            completed_chunks = 0
            
            def flush_pending():
                nonlocal pending_updates
                if not pending_updates:
                    return
                with db_conn() as conn_write:
                    cursor_write = conn_write.cursor()
                    for row_db_id, is_success, err_msg in pending_updates:
                        if is_success:
                            cursor_write.execute("UPDATE backup_records SET sync_status = 'success', error_message = NULL WHERE id = ?", (row_db_id,))
                        else:
                            cursor_write.execute("UPDATE backup_records SET sync_status = 'failed', error_message = ? WHERE id = ?", (err_msg, row_db_id))
                    
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                    sc = cursor_write.fetchone()[0]
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
                    fc = cursor_write.fetchone()[0]
                    
                    cursor_write.execute("UPDATE subtasks SET success_count = ?, fail_count = ? WHERE bu_config_id = ? AND key = ?", 
                                         (sc, fc, bu_config_id, subtask_key))
                    conn_write.commit()
                pending_updates = []
                return sc, fc
            
            pause_key = (bu_config_id, subtask_key)
            PAUSE_CONTROL[pause_key] = {'paused': False}
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_chunk = {}
                for chunk in chunks:
                    batch_records = [r for idx, r in chunk]
                    future = executor.submit(
                        update_sobjects_salesforce_single_batch, 
                        soap_url, session_id, object_api_name, batch_records, subtask_key
                    )
                    future_to_chunk[future] = chunk
                
                for future in as_completed(future_to_chunk):
                    if PAUSE_CONTROL.get(pause_key, {}).get('paused', False):
                        with db_conn() as conn_pause:
                            cursor_pause = conn_pause.cursor()
                            cursor_pause.execute("UPDATE subtasks SET run_state = 'paused' WHERE bu_config_id = ? AND key = ?", 
                                               (bu_config_id, subtask_key))
                            conn_pause.commit()
                        flush_pending()
                        
                        # 暂停时改回true
                        update_custom_label(server_url, session_id, 'opportunityCTOM', 'true')
                        
                        return jsonify({
                            "success": True,
                            "paused": True,
                            "successCount": success_count,
                            "failCount": fail_count,
                            "message": f"更新已暂停。成功: {success_count} 条，失败: {fail_count} 条。"
                        })
                    
                    chunk = future_to_chunk[future]
                    try:
                        chunk_results = future.result()
                    except Exception as e:
                        chunk_results = [{'id': r.get('Id', r.get('id', '')), 'success': False, 'error': str(e)} for idx, r in chunk]
                    
                    completed_chunks += 1
                    
                    for item_idx, res_info in enumerate(chunk_results):
                        original_idx = chunk[item_idx][0]
                        row_db_id = db_row_map.get(original_idx)
                        if not row_db_id:
                            continue
                        is_success = res_info.get('success', False)
                        err_msg = res_info.get('error', 'Salesforce update failed without error message') if not is_success else None
                        pending_updates.append((row_db_id, is_success, err_msg))
                    
                    if completed_chunks % 5 == 0 or completed_chunks == len(chunks):
                        result = flush_pending()
                        if result:
                            success_count, fail_count = result

            if pause_key in PAUSE_CONTROL:
                del PAUSE_CONTROL[pause_key]
                
            # 完成后改回true
            update_custom_label(server_url, session_id, 'opportunityCTOM', 'true')


        else:
            # Simulation fallback:
            # - We can simulate small delay so progress ticks visually in UI
            import time
            for idx, row in enumerate(backup_rows):
                row_db_id = row[0]
                rec_id = row[1]
                rec_name = row[2]
                
                is_success = True
                err_msg = ""
                if len(backup_rows) > 1 and idx % 10 == 3:
                    is_success = False
                    err_msg = "Salesforce API Error: REQUIRED_FIELD_MISSING - Required fields are missing: [BU_Change__c]"
                
                time.sleep(0.05)
                
                with db_conn() as conn_write:
                    cursor_write = conn_write.cursor()
                    if is_success:
                        cursor_write.execute("UPDATE backup_records SET sync_status = 'success', error_message = NULL WHERE id = ?", (row_db_id,))
                    else:
                        cursor_write.execute("UPDATE backup_records SET sync_status = 'failed', error_message = ? WHERE id = ?", (err_msg, row_db_id))
                        
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                    success_count = cursor_write.fetchone()[0]
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
                    fail_count = cursor_write.fetchone()[0]
                    
                    cursor_write.execute("UPDATE subtasks SET success_count = ?, fail_count = ? WHERE bu_config_id = ? AND key = ?", 
                                         (success_count, fail_count, bu_config_id, subtask_key))
                    conn_write.commit()
                
        # Re-verify and final run_state update
        with db_conn() as conn_final:
            cursor_final = conn_final.cursor()
            cursor_final.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
            success_count = cursor_final.fetchone()[0]
            cursor_final.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
            fail_count = cursor_final.fetchone()[0]
            
            # 检查是否还有待处理记录
            cursor_final.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND (sync_status IS NULL OR sync_status = 'pending')", (bu_config_id, subtask_key))
            pending_count = cursor_final.fetchone()[0]
            
            # 如果还有待处理记录，继续保持为 running_update
            if pending_count > 0:
                new_run_state = 'running_update'
                print(f"[UPDATE-DEBUG] Still {pending_count} pending records, set run_state to 'running_update'")
            else:
                new_run_state = 'success' if fail_count == 0 else 'failed'
                print(f"[UPDATE-DEBUG] All records processed, set run_state to '{new_run_state}'")
            
            cursor_final.execute("UPDATE subtasks SET run_state = ? WHERE bu_config_id = ? AND key = ?", 
                                 (new_run_state, bu_config_id, subtask_key))
            conn_final.commit()
        
        # Update BU_Config_Refresh__c.userStatus__c based on final result for user subtask
        if session_id and server_url and subtask_key == 'user':
            soap_url = f"{server_url}/services/Soap/u/58.0"
            if fail_count > 0:
                update_bu_config_user_status(soap_url, session_id, bu_config_id, '部分失败')
            else:
                update_bu_config_user_status(soap_url, session_id, bu_config_id, '更新完成')
        

        # Update BU_Config_Refresh__c.opportunityStatus__c based on final result for opportunity subtask
        if session_id and server_url and subtask_key == 'opportunity':
            soap_url = f"{server_url}/services/Soap/u/58.0"
            if fail_count > 0:
                update_bu_config_opportunity_status(soap_url, session_id, bu_config_id, '部分失败')
            else:
                update_bu_config_opportunity_status(soap_url, session_id, bu_config_id, '更新完成')

        # Update BU_Config_Refresh__c.accountStatus__c based on final result for account subtask
        if session_id and server_url and subtask_key == 'account':
            soap_url = f"{server_url}/services/Soap/u/58.0"
            if fail_count > 0:
                update_bu_config_account_status(soap_url, session_id, bu_config_id, '部分失败')
            else:
                update_bu_config_account_status(soap_url, session_id, bu_config_id, '更新完成')
        
        return jsonify({
            "success": True,
            "successCount": success_count,
            "failCount": fail_count,
            "message": f"子任务更新完成。成功: {success_count} 条，失败: {fail_count} 条。"
        })
    except Exception as e:
        tb_str = traceback.format_exc()
        print("Exception in update_subtask:")
        print(tb_str)
        # Log to database terminal logs if possible
        try:
            with db_conn() as conn_err:
                cursor_err = conn_err.cursor()
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor_err.execute("""
                    INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                    VALUES (?, ?, 'error', ?)
                """, (bu_config_id, now_str, f"更新数据失败: {str(e)}\n{tb_str}"))
                conn_err.commit()
        except Exception as log_ex:
            print("Failed to log error to db:", str(log_ex))
        return jsonify({"success": False, "error": f"更新数据失败: {str(e)}"}), 500

# API to pause subtask update
@app.route('/api/subtask/pause', methods=['POST'])
def pause_subtask():
    data = request.json or {}
    bu_config_id = data.get('bu_config_id')
    subtask_key = data.get('subtask_key')
    session_id = data.get('sessionId')
    server_url = data.get('serverUrl')
    
    if not bu_config_id or not subtask_key:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    
    pause_key = (bu_config_id, subtask_key)
    
    # 无论当前是否在运行中（可能处于批次间隔），都写入暂停标志
    if pause_key not in PAUSE_CONTROL:
        PAUSE_CONTROL[pause_key] = {}
    PAUSE_CONTROL[pause_key]['paused'] = True
    
    # 若为询价对象(opportunity)，暂停时将标签恢复为 true (原值)
    if subtask_key == 'opportunity' and session_id and server_url:
        update_custom_label(server_url, session_id, 'opportunityCTOM', 'true')
        
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE subtasks SET run_state = 'paused' WHERE bu_config_id = ? AND key = ?", 
                           (bu_config_id, subtask_key))
            conn.commit()
    except Exception as e:
        print(f"Failed to update db state to paused: {e}")
        
    return jsonify({"success": True, "message": "暂停指令已生效，标签已还原"})

# API to resume subtask update (继续就是重新调用update)
@app.route('/api/subtask/resume', methods=['POST'])
def resume_subtask():
    # Resume is just calling update again, which will pick up from where it left off
    # (only pending/failed records will be updated)
    return update_subtask()

# API to retry failed subtask records
@app.route('/api/subtask/retry', methods=['POST'])
def retry_subtask():
    data = request.json or {}
    bu_config_id = data.get('bu_config_id')
    subtask_key = data.get('subtask_key')
    session_id = data.get('sessionId')
    server_url = data.get('serverUrl')
    
    if not bu_config_id or not subtask_key:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
        
    try:
        with db_conn() as conn:
            cursor = conn.cursor()
            
            # 1. Fetch failed backup records
            cursor.execute("SELECT id, record_id, record_name, raw_data FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
            failed_rows = cursor.fetchall()
            
            # Check if all records are already successful (for account batch trigger)
            cursor.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ?", (bu_config_id, subtask_key))
            total_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
            success_count = cursor.fetchone()[0]
            
            all_completed = (total_count > 0 and success_count == total_count)
            
            if not failed_rows and not all_completed:
                return jsonify({"success": True, "retriedCount": 0, "message": "没有需要重试的失败记录"})
                
            cursor.execute("SELECT object_api_name FROM subtasks WHERE bu_config_id = ? AND key = ?", (bu_config_id, subtask_key))
            obj_row = cursor.fetchone()
            object_api_name = obj_row[0] if obj_row else 'User'
        
        if session_id and server_url and subtask_key == 'user':
            # Update BU_Config_Refresh__c.userStatus__c to '处理中' at the start of retry
            soap_url = f"{server_url}/services/Soap/u/58.0"
            update_bu_config_user_status(soap_url, session_id, bu_config_id, '处理中')
            
            # Do real Salesforce update for failed User records!
            records_to_update = []
            db_row_map = {}
            for idx, row in enumerate(failed_rows):
                row_db_id = row[0]
                rec_id = row[1]
                raw_data_str = row[3]
                try:
                    record_data = json.loads(raw_data_str)
                except Exception:
                    record_data = {}
                if 'Id' not in record_data and 'id' not in record_data:
                    record_data['Id'] = rec_id
                records_to_update.append(record_data)
                db_row_map[idx] = row_db_id
            
            # Concurrency config
            object_configs = {
                'user': {'chunk_size': 1, 'max_workers': 6}
            }
            config = object_configs.get(subtask_key, {'chunk_size': 200, 'max_workers': 5})
            chunk_size = config['chunk_size']
            max_workers = config['max_workers']
            
            indexed_records = list(enumerate(records_to_update))
            chunks = [indexed_records[i:i+chunk_size] for i in range(0, len(indexed_records), chunk_size)]
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_chunk = {}
                for chunk in chunks:
                    batch_records = [r for idx, r in chunk]
                    future = executor.submit(
                        update_sobjects_salesforce_single_batch, 
                        soap_url, session_id, object_api_name, batch_records, subtask_key
                    )
                    future_to_chunk[future] = chunk
                    
                for future in as_completed(future_to_chunk):
                    chunk = future_to_chunk[future]
                    try:
                        chunk_results = future.result()
                    except Exception as e:
                        chunk_results = [{'id': r.get('Id', r.get('id', '')), 'success': False, 'error': str(e)} for idx, r in chunk]
                        
                    # Reopen connection for writing results of this chunk immediately
                    with db_conn() as conn_write:
                        cursor_write = conn_write.cursor()
                        
                        for item_idx, res_info in enumerate(chunk_results):
                            original_idx = chunk[item_idx][0]
                            row_db_id = db_row_map.get(original_idx)
                            if not row_db_id:
                                continue
                            is_success = res_info.get('success', False)
                            err_msg = res_info.get('error', 'Salesforce update failed without error message') if not is_success else None
                            
                            if is_success:
                                cursor_write.execute("UPDATE backup_records SET sync_status = 'success', error_message = NULL WHERE id = ?", (row_db_id,))
                            else:
                                cursor_write.execute("UPDATE backup_records SET sync_status = 'failed', error_message = ? WHERE id = ?", (err_msg, row_db_id))
                                
                        # Recalculate totals and write to subtasks in real-time
                        cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                        success_count = cursor_write.fetchone()[0]
                        cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
                        fail_count = cursor_write.fetchone()[0]
                        
                        cursor_write.execute("UPDATE subtasks SET success_count = ?, fail_count = ? WHERE bu_config_id = ? AND key = ?", 
                                             (success_count, fail_count, bu_config_id, subtask_key))
                        conn_write.commit()
        
        elif session_id and server_url and subtask_key == 'account':
            # Handle Account retry with batch execution
            soap_url = f"{server_url}/services/Soap/u/58.0"
            
            # If all records are already completed, skip update and go directly to batch trigger
            if all_completed:
                print(f"All {total_count} account records already completed. Skipping update, will trigger batch directly.")
            else:
                # Update BU_Config_Refresh__c.accountStatus__c to '处理中' at the start of retry
                update_bu_config_account_status(soap_url, session_id, bu_config_id, '处理中')
                
                # Do real Salesforce update for failed Account records!
                records_to_update = []
                db_row_map = {}
                for idx, row in enumerate(failed_rows):
                    row_db_id = row[0]
                    rec_id = row[1]
                    raw_data_str = row[3]
                    try:
                        record_data = json.loads(raw_data_str)
                    except Exception:
                        record_data = {}
                    if 'Id' not in record_data and 'id' not in record_data:
                        record_data['Id'] = rec_id
                    records_to_update.append(record_data)
                    db_row_map[idx] = row_db_id
                
                # Concurrency config
                object_configs = {
                    'account': {'chunk_size': 10, 'max_workers': 1}
                }
                config = object_configs.get(subtask_key, {'chunk_size': 10, 'max_workers': 1})
                chunk_size = config['chunk_size']
                max_workers = config['max_workers']
                
                indexed_records = list(enumerate(records_to_update))
                chunks = [indexed_records[i:i+chunk_size] for i in range(0, len(indexed_records), chunk_size)]
                
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_chunk = {}
                    for chunk in chunks:
                        batch_records = [r for idx, r in chunk]
                        future = executor.submit(
                            update_sobjects_salesforce_single_batch, 
                            soap_url, session_id, object_api_name, batch_records, subtask_key
                        )
                        future_to_chunk[future] = chunk
                        
                    for future in as_completed(future_to_chunk):
                        chunk = future_to_chunk[future]
                        try:
                            chunk_results = future.result()
                        except Exception as e:
                            chunk_results = [{'id': r.get('Id', r.get('id', '')), 'success': False, 'error': str(e)} for idx, r in chunk]
                            
                        # Reopen connection for writing results of this chunk immediately
                        with db_conn() as conn_write:
                            cursor_write = conn_write.cursor()
                            
                            for item_idx, res_info in enumerate(chunk_results):
                                original_idx = chunk[item_idx][0]
                                row_db_id = db_row_map.get(original_idx)
                                if not row_db_id:
                                    continue
                                is_success = res_info.get('success', False)
                                err_msg = res_info.get('error', 'Salesforce update failed without error message') if not is_success else None
                                
                                if is_success:
                                    cursor_write.execute("UPDATE backup_records SET sync_status = 'success', error_message = NULL WHERE id = ?", (row_db_id,))
                                else:
                                    cursor_write.execute("UPDATE backup_records SET sync_status = 'failed', error_message = ? WHERE id = ?", (err_msg, row_db_id))
                                    
                            # Recalculate totals and write to subtasks in real-time
                            cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                            success_count = cursor_write.fetchone()[0]
                            cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
                            fail_count = cursor_write.fetchone()[0]
                            
                            cursor_write.execute("UPDATE subtasks SET success_count = ?, fail_count = ? WHERE bu_config_id = ? AND key = ?", 
                                                 (success_count, fail_count, bu_config_id, subtask_key))
                            conn_write.commit()
            
            # Trigger batch for contract accounts (契約) - fetch all successful records
            with db_conn() as conn_batch:
                cursor_batch = conn_batch.cursor()
                cursor_batch.execute("SELECT record_id, raw_data FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                success_rows = cursor_batch.fetchall()
                
                contract_accounts = []
                for row in success_rows:
                    rec_id = row[0]
                    raw_data_str = row[1]
                    try:
                        record_data = json.loads(raw_data_str)
                        acc_record_type = record_data.get('Acc_Record_Type__c', record_data.get('acc_record_type__c', ''))
                        if acc_record_type == '契約':
                            contract_accounts.append(rec_id)
                    except:
                        pass
            
            # Trigger UpdateTheAgreementBatch for contract accounts
            print(f"Retry checking if should trigger batch. contract_accounts count: {len(contract_accounts)}")
            if contract_accounts:
                print(f"Triggering UpdateTheAgreementBatch for {len(contract_accounts)} contract accounts (Retry Flow)")
                BATCH_TRIGGER_CHUNK = 500
                trigger_chunks = [contract_accounts[i:i+BATCH_TRIGGER_CHUNK] 
                                  for i in range(0, len(contract_accounts), BATCH_TRIGGER_CHUNK)]
                
                trigger_success = 0
                trigger_failed = 0
                
                for chunk_idx, id_chunk in enumerate(trigger_chunks):
                    try:
                        account_ids_str = "','".join(id_chunk)
                        apex_code = (
                            f"List<Account> accList = [SELECT Id FROM Account WHERE Id IN ('{account_ids_str}')];"
                            f"Database.executeBatch(new UpdateTheAgreementBatch(accList), 1);"
                        )
                        
                        print(f"Executing Apex code for chunk {chunk_idx+1}/{len(trigger_chunks)} (Retry Flow)")
                        result = execute_apex_anonymous(server_url, session_id, apex_code)
                        
                        print(f"Batch trigger result: compiled={result.get('compiled')}, success={result.get('success')} (Retry Flow)")
                        if result.get('exceptionMessage'):
                            print(f"  exceptionMessage: {result.get('exceptionMessage')}")
                        if result.get('compileProblem'):
                            print(f"  compileProblem: {result.get('compileProblem')}")
                        
                        if result.get('compiled') and result.get('success'):
                            trigger_success += len(id_chunk)
                            print(f"Triggered UpdateTheAgreementBatch chunk {chunk_idx+1}/{len(trigger_chunks)} ({len(id_chunk)} accounts) (Retry Flow)")
                        else:
                            trigger_failed += len(id_chunk)
                            error_msg = result.get('exceptionMessage') or result.get('compileProblem') or 'Unknown error'
                            print(f"Failed UpdateTheAgreementBatch chunk {chunk_idx+1}: {error_msg} (Retry Flow)")
                            with db_conn() as conn_err:
                                cursor_err = conn_err.cursor()
                                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                cursor_err.execute("""
                                    INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                                    VALUES (?, ?, 'warning', ?)
                                """, (bu_config_id, now_str, f"UpdateTheAgreementBatch 触发失败 (批次{chunk_idx+1}): {error_msg}"))
                                conn_err.commit()
                    except Exception as e:
                        trigger_failed += len(id_chunk)
                        print(f"Exception triggering UpdateTheAgreementBatch chunk {chunk_idx+1}: {str(e)} (Retry Flow)")
                        with db_conn() as conn_err:
                            cursor_err = conn_err.cursor()
                            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            cursor_err.execute("""
                                INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                                VALUES (?, ?, 'error', ?)
                            """, (bu_config_id, now_str, f"UpdateTheAgreementBatch 触发异常 (批次{chunk_idx+1}): {str(e)}"))
                            conn_err.commit()
                
                # Log batch trigger summary
                print(f"Batch trigger summary: success={trigger_success}, failed={trigger_failed} (Retry Flow)")
                with db_conn() as conn_log:
                    cursor_log = conn_log.cursor()
                    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor_log.execute("""
                        INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                        VALUES (?, ?, 'info', ?)
                    """, (bu_config_id, now_str, 
                          f"重试: 契約客户 UpdateTheAgreementBatch 触发完成: 成功 {trigger_success} 条，失败 {trigger_failed} 条 (共 {len(trigger_chunks)} 个批次)"))
                    # Fix: change cursor_log.commit() to conn_log.commit()
                    conn_log.commit()
            else:
                print("No contract accounts found during retry, skipping batch trigger")

        elif session_id and server_url and subtask_key == 'opportunity':
            soap_url = f"{server_url}/services/Soap/u/58.0"
            if all_completed:
                print(f"All records already completed. Skipping update.")
            else:
                update_custom_label(server_url, session_id, 'opportunityCTOM', 'false')
                update_bu_config_opportunity_status(soap_url, session_id, bu_config_id, '处理中')
                
                records_to_update = []
                db_row_map = {}
                for idx, row in enumerate(failed_rows):
                    row_db_id = row[0]
                    rec_id = row[1]
                    raw_data_str = row[3]
                    try:
                        record_data = json.loads(raw_data_str)
                    except:
                        record_data = {}
                    if 'Id' not in record_data and 'id' not in record_data:
                        record_data['Id'] = rec_id
                    records_to_update.append(record_data)
                    db_row_map[idx] = row_db_id
                
                chunk_size = 10
                max_workers = 1
                indexed_records = list(enumerate(records_to_update))
                chunks = [indexed_records[i:i+chunk_size] for i in range(0, len(indexed_records), chunk_size)]
                
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_chunk = {}
                    for chunk in chunks:
                        batch_records = [r for idx, r in chunk]
                        future = executor.submit(
                            update_sobjects_salesforce_single_batch, 
                            soap_url, session_id, object_api_name, batch_records, subtask_key
                        )
                        future_to_chunk[future] = chunk
                    
                    for future in as_completed(future_to_chunk):
                        chunk = future_to_chunk[future]
                        try:
                            chunk_results = future.result()
                        except Exception as e:
                            chunk_results = [{'id': r.get('Id', r.get('id', '')), 'success': False, 'error': str(e)} for idx, r in chunk]
                        
                        with db_conn() as conn_write:
                            cursor_write = conn_write.cursor()
                            for item_idx, res_info in enumerate(chunk_results):
                                original_idx = chunk[item_idx][0]
                                row_db_id = db_row_map.get(original_idx)
                                if not row_db_id: continue
                                is_success = res_info.get('success', False)
                                err_msg = res_info.get('error', 'Salesforce update failed') if not is_success else None
                                
                                if is_success:
                                    cursor_write.execute("UPDATE backup_records SET sync_status = 'success', error_message = NULL WHERE id = ?", (row_db_id,))
                                else:
                                    cursor_write.execute("UPDATE backup_records SET sync_status = 'failed', error_message = ? WHERE id = ?", (err_msg, row_db_id))
                            
                            cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                            success_count = cursor_write.fetchone()[0]
                            cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
                            fail_count = cursor_write.fetchone()[0]
                            cursor_write.execute("UPDATE subtasks SET success_count = ?, fail_count = ? WHERE bu_config_id = ? AND key = ?", 
                                                (success_count, fail_count, bu_config_id, subtask_key))
                            conn_write.commit()

                update_custom_label(server_url, session_id, 'opportunityCTOM', 'true')

        
        else:
            # Reopen connection for writing simulation retry
            import time
            for idx, row in enumerate(failed_rows):
                row_db_id = row[0]
                
                time.sleep(0.05)
                
                with db_conn() as conn_write:
                    cursor_write = conn_write.cursor()
                    cursor_write.execute("UPDATE backup_records SET sync_status = 'success', error_message = NULL WHERE id = ?", (row_db_id,))
                    
                    # Recalculate
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
                    success_count = cursor_write.fetchone()[0]
                    cursor_write.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
                    fail_count = cursor_write.fetchone()[0]
                    
                    cursor_write.execute("UPDATE subtasks SET success_count = ?, fail_count = ? WHERE bu_config_id = ? AND key = ?", 
                                         (success_count, fail_count, bu_config_id, subtask_key))
                    conn_write.commit()
            
        # Re-verify and final run_state update
        with db_conn() as conn_final:
            cursor_final = conn_final.cursor()
            cursor_final.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'success'", (bu_config_id, subtask_key))
            success_count = cursor_final.fetchone()[0]
            cursor_final.execute("SELECT COUNT(*) FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? AND sync_status = 'failed'", (bu_config_id, subtask_key))
            fail_count = cursor_final.fetchone()[0]
            cursor_final.execute("UPDATE subtasks SET run_state = ? WHERE bu_config_id = ? AND key = ?", 
                                 ('success' if fail_count == 0 else 'failed', bu_config_id, subtask_key))
            conn_final.commit()
        
        # Update BU_Config_Refresh__c.userStatus__c based on retry result for user subtask
        if session_id and server_url and subtask_key == 'user':
            soap_url = f"{server_url}/services/Soap/u/58.0"
            if fail_count > 0:
                update_bu_config_user_status(soap_url, session_id, bu_config_id, '部分失败')
            else:
                update_bu_config_user_status(soap_url, session_id, bu_config_id, '更新完成')
        
        # Update BU_Config_Refresh__c.opportunityStatus__c based on retry result for opportunity subtask
        if session_id and server_url and subtask_key == 'opportunity':
            soap_url = f"{server_url}/services/Soap/u/58.0"
            if fail_count > 0:
                update_bu_config_opportunity_status(soap_url, session_id, bu_config_id, '部分失败')
            else:
                update_bu_config_opportunity_status(soap_url, session_id, bu_config_id, '更新完成')

        # Update BU_Config_Refresh__c.accountStatus__c based on retry result for account subtask
        if session_id and server_url and subtask_key == 'account':
            soap_url = f"{server_url}/services/Soap/u/58.0"
            if fail_count > 0:
                update_bu_config_account_status(soap_url, session_id, bu_config_id, '部分失败')
            else:
                update_bu_config_account_status(soap_url, session_id, bu_config_id, '更新完成')
        
        return jsonify({
            "success": True,
            "successCount": success_count,
            "failCount": fail_count,
            "retriedCount": len(failed_rows),
            "message": f"成功重试 {len(failed_rows)} 条记录。当前成功: {success_count} 条，失败: {fail_count} 条。"
        })
    except Exception as e:
        tb_str = traceback.format_exc()
        print("Exception in retry_subtask:")
        print(tb_str)
        try:
            with db_conn() as conn_err:
                cursor_err = conn_err.cursor()
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor_err.execute("""
                    INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
                    VALUES (?, ?, 'error', ?)
                """, (bu_config_id, now_str, f"重试失败: {str(e)}\n{tb_str}"))
                conn_err.commit()
        except Exception as log_ex:
            print("Failed to log error to db:", str(log_ex))
        return jsonify({"success": False, "error": f"重试失败: {str(e)}"}), 500

# API to query recent Apex Batch jobs
@app.route('/api/batch-status', methods=['GET'])
def get_batch_status():
    session_id = request.args.get('sessionId')
    server_url = request.args.get('serverUrl')
    
    if not session_id or not server_url:
        return jsonify({"success": False, "error": "Missing credentials"}), 400
        
    try:
        import re
        match = re.match(r'(https?://[^/]+)', server_url)
        base_url = match.group(1) if match else server_url
        
        # We can use the standard SOAP API endpoint
        soap_url = f"{base_url}/services/Soap/u/58.0"
        
        # SOQL to get recent UpdateTheAgreementBatch jobs
        soql = """
            SELECT Id, Status, JobItemsProcessed, TotalJobItems, NumberOfErrors, 
                   ExtendedStatus, CreatedDate, CompletedDate 
            FROM AsyncApexJob 
            WHERE ApexClass.Name = 'UpdateTheAgreementBatch' 
            ORDER BY CreatedDate DESC 
            LIMIT 10
        """
        
        soap_body = f"""<?xml version="1.0" encoding="utf-8" ?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Header>
    <SessionHeader xmlns="urn:partner.soap.sforce.com">
      <sessionId>{session_id}</sessionId>
    </SessionHeader>
  </env:Header>
  <env:Body>
    <query xmlns="urn:partner.soap.sforce.com">
      <queryString>{html.escape(soql)}</queryString>
    </query>
  </env:Body>
</env:Envelope>"""

        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": "query"
        }
        
        res = requests.post(soap_url, data=soap_body.encode('utf-8'), headers=headers, timeout=30)
        
        # Log this request so we can see what SFDC actually returns
        log_sfdc_request(soap_url, "POST", headers, soap_body, res.status_code, res.content)
        
        if res.status_code == 200:
            print("SFDC BATCH QUERY RESPONSE:")
            print(res.text)
            
            root = ET.fromstring(res.content)
            jobs = []
            
            # The namespace for Salesforce Partner API
            ns = {'sf': 'urn:partner.soap.sforce.com', 'xsi': 'http://www.w3.org/2001/XMLSchema-instance'}
            
            # Find all records in the QueryResult
            for record in root.findall('.//sf:records', ns):
                job = {}
                for child in record:
                    # Strip namespace from tag
                    tag = child.tag.split('}')[-1]
                    if tag != 'type' and tag != 'ApexClass':
                        job[tag] = child.text
                    # Specifically handle the ApexClass nested element if we need it
                    elif tag == 'ApexClass':
                        for subchild in child:
                            subtag = subchild.tag.split('}')[-1]
                            if subtag == 'Name':
                                job['ApexClassName'] = subchild.text
                jobs.append(job)
            
            print(f"Parsed {len(jobs)} jobs")
            return jsonify({
                "success": True,
                "jobs": jobs
            })
        else:
            return jsonify({
                "success": False,
                "error": f"HTTP {res.status_code}: {res.text[:200]}"
            }), res.status_code
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    import webview
    
    if getattr(sys, 'frozen', False) or os.environ.get('RUN_GUI') == '1':
        # Start pywebview, passing the Flask app directly
        webview.create_window("BU省刷新工具", app, width=1280, height=800)
        webview.start()
    else:
        # Get port from environment or default to 5000
        port = int(os.environ.get('PORT', 5000))
        print(f"Starting server on http://localhost:{port} ...")
        app.run(host='0.0.0.0', port=port, debug=True)
