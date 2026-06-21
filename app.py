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

app = Flask(__name__, template_folder='templates', static_folder='static')

# Check if running in a PyInstaller bundle
if getattr(sys, 'frozen', False):
    # Use a user-writable folder in the user's home directory for database and backups
    DATA_DIR = os.path.expanduser("~/BU省刷新工具")
    os.makedirs(DATA_DIR, exist_ok=True)
else:
    DATA_DIR = app.root_path

DATABASE_PATH = os.path.join(DATA_DIR, "sfdc_workspace.db")

def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
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
        work_location__c TEXT
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
    
    # Check if empty, seed with initial data
    # No initial fake data seeded, only keep real data
    conn.commit()
    conn.close()

init_db()

import re

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
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for elem in root.iter():
                if elem.tag.split('}')[-1] == 'size':
                    return int(elem.text)
    except Exception as e:
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
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for elem in root.iter():
                if elem.tag.split('}')[-1] == 'size':
                    return int(elem.text)
    except Exception as e:
        print(f"Failed fallback query for count: {str(e)}")
        
    return None

def get_sqlite_records(soap_url=None, session_id=None):
    conn = sqlite3.connect(DATABASE_PATH)
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
            "work_location__c": row[12] if len(row) > 12 else ""
        }
        
        subtasks_dict = {}
        for sub in subtask_rows:
            key = sub[2]
            raw_sql = sub[11]
            resolved_sql = process_sql_template(raw_sql, record_dict_temp)
            
            # Query real count if session is available
            count_str = sub[4] # Fallback to seeded count
            if soap_url and session_id and resolved_sql:
                real_count = query_salesforce_count(soap_url, session_id, resolved_sql)
                if real_count is not None:
                    count_str = f"{real_count}条"
            
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
                "sql": resolved_sql
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
            "subtasks": subtasks_dict
        })
        
    conn.close()
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
                "serverUrl": server_url_elem.text,
                "userId": user_id_elem.text if user_id_elem is not None else "",
                "userInfo": user_info
            })
        else:
            return jsonify({"success": False, "error": "登录失败：返回的数据中没有 Session ID 或 Server URL"}), 400

    except requests.exceptions.RequestException as req_err:
        return jsonify({"success": False, "error": f"网络请求失败: {str(req_err)}"}), 500
    except Exception as e:
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
        return jsonify({"success": False, "error": f"网络请求失败: {str(req_err)}"}), 500
    except Exception as e:
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
        res = requests.post(token_url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
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
            userinfo_res = requests.get(id_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
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
        
        conn = sqlite3.connect(DATABASE_PATH)
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
            conn.close()
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
            try:
                res_query = requests.post(soap_url, data=query_body.encode('utf-8'), 
                                          headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "query"}, timeout=20)
                if res_query.status_code == 200:
                    root_query = ET.fromstring(res_query.content)
                    
                    # Parse SOAP records
                    for elem in root_query.iter():
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
                            records_to_backup.append(rec_data)
                    soap_success = True
            except Exception as e:
                print(f"Salesforce query failed: {str(e)}, falling back to mock record generation.")

        # If SOAP query was not executed or failed, return error as we only support real data now
        if not soap_success:
            conn.close()
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
            
        conn.commit()
        conn.close()

        return jsonify({
            "success": True, 
            "resumed": False,
            "filePath": relative_path,
            "filename": f"{date_str}-{time_str}-备份.csv",
            "count": len(records_to_backup)
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"创建备份文件与入库失败: {str(e)}"}), 500

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
            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT backup_file_path FROM backup_records WHERE bu_config_id = ? AND subtask_key = ? ORDER BY id DESC LIMIT 1",
                           (bu_config_id, subtask_key))
            row = cursor.fetchone()
            conn.close()
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
            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT raw_data FROM backup_records WHERE bu_config_id = ? AND subtask_key = ?",
                           (bu_config_id, subtask_key))
            rows = cursor.fetchall()
            conn.close()
            
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
        res = requests.post(soap_url, data=describe_body.encode('utf-8'), headers=headers, timeout=20)
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
        conn_db = sqlite3.connect(DATABASE_PATH)
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
        conn_db.close()

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

        res_query = requests.post(soap_url, data=query_body.encode('utf-8'), headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "query"}, timeout=20)
        
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
        conn_db = sqlite3.connect(DATABASE_PATH)
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
                cursor_db.execute("SELECT execute, backup, run_state, backup_state, count FROM subtasks WHERE bu_config_id = ? AND key = ?", (rec_id, key))
                existing_sub = cursor_db.fetchone()
                
                if existing_sub:
                    exec_val = existing_sub[0]
                    backup_val = existing_sub[1]
                    run_state_val = existing_sub[2]
                    backup_state_val = existing_sub[3]
                    count_str = existing_sub[4] or "计算中..."
                else:
                    exec_val = 1
                    backup_val = 1
                    run_state_val = 'ready'
                    backup_state_val = 'ready'
                    count_str = "计算中..."
                
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
                INSERT OR REPLACE INTO subtasks (bu_config_id, key, name, count, execute, backup, run_state, backup_state, object_api_name, field_name, sql)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (rec_id, key, obj_info['objectLabel'], count_str, exec_val, backup_val, run_state_val, backup_state_val, obj_info['objectName'], field_name, resolved_sql))
                
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
                    "sql": resolved_sql
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
            INSERT OR REPLACE INTO bu_configs (id, name, province, city, currency, owner, created_by_name, created_by_time, modified_by_name, modified_by_time, progress_text, progress_color, work_location__c)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rec_id, name, province_val, city_val, "CNY - 中国人民币", owner_val, "精琢技术", created_time, "精琢技术", modified_time, progress_text_val, progress_color_val, rec.get('work_location__c', '')))

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
                "subtasks": subtasks
            })
            
        conn_db.commit()
        conn_db.close()
            
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
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
        if execute is not None:
            cursor.execute("UPDATE subtasks SET execute = ? WHERE bu_config_id = ? AND key = ?", 
                           (1 if execute else 0, bu_config_id, subtask_key))
        if backup is not None:
            cursor.execute("UPDATE subtasks SET backup = ? WHERE bu_config_id = ? AND key = ?", 
                           (1 if backup else 0, bu_config_id, subtask_key))
                           
        conn.commit()
        conn.close()
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
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
        # Query subtasks of this bu_config_id
        cursor.execute("SELECT key, sql FROM subtasks WHERE bu_config_id = ?", (bu_config_id,))
        rows = cursor.fetchall()
        
        counts = {}
        for row in rows:
            key = row[0]
            resolved_sql = row[1]
            
            real_count = None
            if soap_url and session_id and resolved_sql:
                real_count = query_salesforce_count(soap_url, session_id, resolved_sql)
                
            if real_count is not None:
                counts[key] = f"{real_count}条"
                # Update SQLite subtask count so it is cached!
                cursor.execute("UPDATE subtasks SET count = ? WHERE bu_config_id = ? AND key = ?", (f"{real_count}条", bu_config_id, key))
            else:
                counts[key] = "0条"
                
        conn.commit()
        conn.close()
        
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
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO terminal_logs (bu_config_id, timestamp, log_type, message)
            VALUES (?, ?, ?, ?)
        """, (bu_config_id, now_str, log_type, message))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API to retrieve logs for a config
@app.route('/api/logs/<bu_config_id>', methods=['GET'])
def get_terminal_logs(bu_config_id):
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, log_type, message FROM terminal_logs WHERE bu_config_id = ? ORDER BY id ASC", (bu_config_id,))
        rows = cursor.fetchall()
        conn.close()
        
        logs = [{"timestamp": r[0], "type": r[1], "message": r[2]} for r in rows]
        return jsonify({"success": True, "logs": logs})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API to delete logs for a config
@app.route('/api/logs/<bu_config_id>', methods=['DELETE'])
def delete_terminal_logs(bu_config_id):
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM terminal_logs WHERE bu_config_id = ?", (bu_config_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# API to get all tables in SQLite database
@app.route('/api/db/tables', methods=['GET'])
def get_db_tables():
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = [r[0] for r in cursor.fetchall()]
        conn.close()
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
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
        # Whitelist validation
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        valid_tables = [r[0] for r in cursor.fetchall()]
        if table_name not in valid_tables:
            conn.close()
            return jsonify({"success": False, "error": f"无效的表名: {table_name}"}), 400
            
        # Get schema columns
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [r[1] for r in cursor.fetchall()]
        
        # Get records
        cursor.execute(f"SELECT * FROM {table_name} LIMIT 1000")
        rows = cursor.fetchall()
        conn.close()
        
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
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE bu_configs SET progress_text = ?, progress_color = ? WHERE id = ?", 
                       (progress_text, progress_color or '', bu_config_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": f"更新进度失败: {str(e)}"}), 500

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
