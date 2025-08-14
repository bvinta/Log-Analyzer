import os
import re
import sys
import time
import json
import yaml
import zipfile
import logging
import requests
from pathlib import Path
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify, send_from_directory, abort
from jira_interface.jira_interface import FordJIRA, JIRAUpdateFailure
import uuid
from flask import session

jira = FordJIRA(server="Ford",username="have ur ford email@ford.com",password="Have ur jira secret key")

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'

from colorama import Fore, Style, init
init(autoreset=True)
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'WARNING': Fore.YELLOW,
        'ERROR': Fore.RED,
        'CRITICAL': Fore.RED + Style.BRIGHT,
        'DEBUG': Fore.BLUE,
        'INFO': Fore.GREEN
    }
    def format(self, record):
        color = self.COLORS.get(record.levelname, '')
        message = super().format(record)
        return color + message + Style.RESET_ALL

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter('%(levelname)s: %(message)s'))
logger.addHandler(handler)

STORAGE_KEY_LIST = [
    "0:UNKNOWN",
    "1000:NAS",
    "1001:OBJECT_STORAGE",
    "1002:LOCAL_FS",
    "1003:OBJECT_STORAGE_EXPLODED",
    "1004:VIRTUAL_STORAGE",
    "1005:GOOGLE_CLOUD_STORE",
    "1006:GOOGLE_CLOUD_STORE_EXPLODED",
    "1007:LOCAL_FS_EXPLODED",
    "1008:ALTERNATE_S3_STORE_EXPLODED"
]

SEARCH_YAML_PATH = "search.yaml"
SAVED_SETS_PATH = "saved_keyword_sets.yaml"

def load_keywords():
    if not os.path.exists(SEARCH_YAML_PATH):
        return {'searches': []}
    try:
        with open(SEARCH_YAML_PATH, 'r') as f:
            data = yaml.safe_load(f) or {}
        if 'searches' not in data:
            data['searches'] = []
        return data
    except Exception as e:
        logger.error(f"Failed to load {SEARCH_YAML_PATH}: {e}")
        return {'searches': []}

def save_keywords(data):
    try:
        with open(SEARCH_YAML_PATH, 'w') as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        return True, None
    except Exception as e:
        logger.error(f"Failed to save {SEARCH_YAML_PATH}: {e}")
        return False, str(e)

def load_saved_sets():
    if not os.path.exists(SAVED_SETS_PATH):
        return {'sets': []}
    try:
        with open(SAVED_SETS_PATH, 'r') as f:
            data = yaml.safe_load(f) or {}
        if 'sets' not in data:
            data['sets'] = []
        return data
    except Exception as e:
        logger.error(f"Failed to load {SAVED_SETS_PATH}: {e}")
        return {'sets': []}

def save_saved_sets(data):
    try:
        with open(SAVED_SETS_PATH, 'w') as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        return True, None
    except Exception as e:
        logger.error(f"Failed to save {SAVED_SETS_PATH}: {e}")
        return False, str(e)

def unzip_flat(zip_path, output_folder):
    os.makedirs(output_folder, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        for member in zip_ref.infolist():
            if not member.is_dir():
                filename = os.path.basename(member.filename)
                if not filename:
                    continue
                source = zip_ref.open(member)
                target_path = os.path.join(output_folder, filename)
                counter = 1
                original_name = filename
                while os.path.exists(target_path):
                    name, ext = os.path.splitext(original_name)
                    filename = f"{name}_{counter}{ext}"
                    target_path = os.path.join(output_folder, filename)
                    counter += 1
                with open(target_path, "wb") as target:
                    target.write(source.read())
                print(f"Extracted: {filename}")


def get_session_folder():
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
    session_folder = os.path.join(os.getcwd(), 'user_sessions', session['session_id'])
    os.makedirs(session_folder, exist_ok=True)
    return session_folder

def search_lines_with_regex(file_path, fine_name ,include_patterns, exclude_patterns):
    include_regex = [re.compile(pat, re.IGNORECASE) for pat in include_patterns]
    exclude_regex = [re.compile(pat, re.IGNORECASE) for pat in exclude_patterns]

    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            for line in file:
                if any(regex.search(line) for regex in include_regex):
                    if not any(regex.search(line) for regex in exclude_regex):
                        with open(fine_name, "a", encoding='utf-8') as f:
                            f.write(line.strip() + "\n")
    except Exception as e:
        logger.error(f"Error searching file {file_path}: {e}")

def search_files_on_quip_event(event_id):
    logger.info("Requesting download token")
    url = "https://corp.sts.ford.com/adfs/oauth2/token"
    payload = 'client_id=9536b3da-2e4c-f771-4a80-3498471dd9c3&client_secret=glyWVfuGjN5-iJvqPwMg4P2Db96TJPVvLb_vhaky&grant_type=client_credentials&resource=urn:fnv_search:resource:api_fnv_search_graphql:prod'
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(url, data=payload, headers=headers)
    response = json.loads(response.text)
    logger.debug(f"{response}")
    current_token = response['access_token']
    logger.info("Searching files and storage_type")
    url = f"https://api.int.core.ford.com/fnv-search/v1/event/{event_id}"
    headers = {
      "Authorization": f"Bearer {current_token}",
      "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers)
    response = json.loads(response.text)
    custom_json = {}
    custom_json['creation_time'] = response.get('creation_time', '')
    custom_json['delivery_time'] = response.get('delivery_time', '')
    custom_json['vehicle_vin'] = response.get('vehicle', {}).get('vin', '')
    custom_json['children'] = []
    for item in response.get('children', []):
        storage_key = item.get('storage_key', '')
        storage_type = item.get('storage_type', '')
        ecu_platform = item.get('ecu', {}).get('platform', '')
        files = item.get('files', [])
        custom_json['children'].append({
            "storage_type": storage_type,
            "storage_key": storage_key,
            "ecu_platform": ecu_platform,
            "files": files
        })
        logger.info(f"Searching logs on : {ecu_platform}")
    return custom_json

def download_zip(download_array, zip_name):
    url_api = "https://www.diagnostics.ford.com/fnv-diag-files/v1/api/files"
    logger.info("Requesting download token")
    url = "https://corp.sts.ford.com/adfs/oauth2/token"
    payload = 'client_id=9536b3da-2e4c-f771-4a80-3498471dd9c3&client_secret=glyWVfuGjN5-iJvqPwMg4P2Db96TJPVvLb_vhaky&grant_type=client_credentials'
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(url, data=payload, headers=headers)
    response = json.loads(response.text)
    download_token = response['access_token']
    try:
        headers = {
            "Authorization": f"Bearer {download_token}",
            "Content-Type": "application/json"
        }
        with requests.get(url_api, json=download_array, headers=headers, stream=True) as response:
            response.raise_for_status()
            with open(zip_name, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
        if Path(zip_name).is_file():
            logger.info(f"File saved successfully: {zip_name}")
            return True
        else:
            logger.error("Error: File was not created")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Connection Error: {e}")
    except IOError as e:
        logger.error(f"Error writing the file: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    return False

def find_key(list_of_items, search_word):
    for item in list_of_items:
        if ":" in item:
            key, value = item.split(":", 1)
            if search_word.lower() in value.lower():
                return key.strip()
    return None

def clear_old_logs_and_zips(session_folder):
    # Remove log text files and zip files ONLY in this session's folder
    for filename in os.listdir(session_folder):
        if filename.startswith('logs_') and (filename.endswith('.txt') or filename.endswith('.zip')):
            try:
                os.remove(os.path.join(session_folder, filename))
                logger.info(f"Deleted old log file: {filename} in session {session.get('session_id')}")
            except Exception as e:
                logger.warning(f"Could not delete old log file {filename} in session {session.get('session_id')}: {e}")

    # Remove unzipped folder inside session folder (if exists)
    unzip_folder = os.path.join(session_folder, 'unzipped')
    import shutil
    if os.path.exists(unzip_folder):
        try:
            shutil.rmtree(unzip_folder)
            logger.info(f"Deleted old unzipped folder in session {session.get('session_id')}")
        except Exception as e:
            logger.warning(f"Could not delete unzipped folder in session {session.get('session_id')}: {e}")

def download_logs_for_keyword(searches, quip_event_id, zip_name):
    files_path = []
    for item in searches:
        files_path.append(f"logworthy/system/{item['file']}")
    download_array = []
    logger.debug("Calling search API for files and storage type")
    quip_data = search_files_on_quip_event(quip_event_id)
    for item in quip_data['children']:
        current_storage_type = item['storage_type']
        current_storage_key = item['storage_key']
        for filename in item['files']:
            for key in files_path:
                if filename == key:
                    logger.warning(f"Key found : {key}")
                    search_term = current_storage_type
                    found_key = find_key(STORAGE_KEY_LIST, search_term)
                    logger.warning(f"data_key found : {key}")
                    download_array.append(f"{found_key}/{current_storage_key}/{key}")
    logger.info(f"Array of files to download on zip file : {download_array}")
    if len(download_array) == 0:
        logger.critical("No files found to download. Ensure file/platform is correct.")
        sys.exit()
    logger.debug("Downloading Zip File")
    download_zip(download_array, zip_name)
    print("logs OK")
    return "logs OK"

exclude_words = ["Settings", "BroadcastQueue", "BluetoothAdapter"]
regex_to_exclude = [rf'\b{re.escape(word)}\b' for word in exclude_words]

@app.route('/', methods=['GET', 'POST'])
def home():
    keywords_data = load_keywords()
    if request.method == 'POST':
        quip_event_id = request.form.get('quip_event')
        if not quip_event_id:
            flash('Please load a filter from the Searching Techniques tab before proceeding with entering QUIP event number.', 'error')
            return redirect(url_for('home'))

        session_folder = get_session_folder()

        # Clear old logs only for this session folder
        clear_old_logs_and_zips(session_folder)

        zip_name = os.path.join(session_folder, f"logs_{int(time.time())}.zip")
        try:
            res = download_logs_for_keyword(keywords_data['searches'], quip_event_id, zip_name)
            unzip_folder = os.path.join(session_folder, 'unzipped')
            unzip_flat(zip_name, unzip_folder)

            # Delete old filtered logs before creating new ones (individual keyword files)
            for item in keywords_data['searches']:
                for word in item.get('keywords', []):
                    file_name = os.path.join(session_folder, f"logs_{item['file']}_{word}.txt")
                    if os.path.exists(file_name):
                        os.remove(file_name)

            # Also delete old combined file if exists
            combined_file_path = os.path.join(session_folder, "logs_all_filtered_keywords.txt")

            if os.path.exists(combined_file_path):
                os.remove(combined_file_path)
                logger.info(f"Created combined filtered log file: {combined_file_path}")


            # Open combined file once for all keyword outputs
            with open(combined_file_path, 'w', encoding='utf-8') as combined_f_out:

                for item in keywords_data['searches']:
                    file = item['file']
                    for word in item.get('keywords', []):
                        # Create individual keyword file and write header
                        file_name = os.path.join(session_folder, f"logs_{file}_{word}.txt")
                        with open(file_name, 'w', encoding='utf-8') as f_out:
                            f_out.write(f"Search File: {file}\n")
                            f_out.write(f"Filter Keywords: {word}\n")
                            f_out.write(f"QUIP Event ID: {quip_event_id}\n---\n")

                        word_regex = rf'\b{re.escape(word)}\b'
                        include_regex = re.compile(word_regex, re.IGNORECASE)
                        exclude_regexes = [re.compile(pat, re.IGNORECASE) for pat in regex_to_exclude]

                        try:
                            with open(os.path.join(unzip_folder, file), 'r', encoding='utf-8') as input_file:
                                for line in input_file:
                                    if include_regex.search(line) and not any(rx.search(line) for rx in exclude_regexes):
                                        # Append to individual file
                                        with open(file_name, 'a', encoding='utf-8') as f_out:
                                            f_out.write(line)
                                        # Append to combined file with context
                                        combined_f_out.write(f"File: {file} | Keyword: {word} | Line: {line}")
                        except Exception as e:
                            logger.error(f"Error searching file {file} for keyword {word}: {e}")

            flash(f"Logs processed successfully! Created ZIP: {os.path.basename(zip_name)}", "success")
        except Exception as e:
            logger.error(f"Error processing logs: {e}")
            flash(f"Error processing logs: {e}", "error")
        return redirect(url_for('home'))
    return render_template_string(PAGE_HTML, active_tab="analyzer")



@app.route('/manage_keywords')
def manage_keywords():
    return render_template_string(PAGE_HTML, active_tab="manage")

@app.route('/download_logs')
def download_logs():
    session_folder = get_session_folder()
    if not os.path.exists(session_folder):
        log_files = []
    else:
        log_files = []
        for filename in os.listdir(session_folder):
            if filename.startswith('logs_') and filename.endswith('.txt') and os.path.isfile(os.path.join(session_folder, filename)):
                size_kb = os.path.getsize(os.path.join(session_folder, filename)) / 1024
                log_files.append({'name': filename, 'size_kb': f"{size_kb:.2f}"})
    return render_template_string(PAGE_HTML, active_tab="download_logs", log_files=log_files)

@app.route('/download/<path:filename>')
def download_file(filename):
    if not (filename.startswith('logs_') and filename.endswith('.txt')):
        abort(404)
    session_folder = get_session_folder()
    full_path = os.path.join(session_folder, filename)
    if not os.path.isfile(full_path):
        abort(404)
    return send_from_directory(session_folder, filename, as_attachment=True)

@app.route('/api/keywords', methods=['GET', 'POST'])
def api_keywords():
    if request.method == 'GET':
        data = load_keywords()
        return jsonify(data)
    new_data = request.get_json()
    if not new_data or 'searches' not in new_data:
        return jsonify({'success': False, 'error': 'Invalid data'}), 400
    success, err = save_keywords(new_data)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': err}), 500

@app.route('/api/saved_sets', methods=['GET', 'POST'])
def api_saved_sets():
    if request.method == 'GET':
        data = load_saved_sets()
        return jsonify(data)
    req = request.get_json()
    set_name = req.get('set_name', '')
    if not set_name:
        return jsonify({'success': False, 'error': 'No set_name provided'}), 400
    saved = load_saved_sets()
    chosen_set = None
    for s in saved.get('sets', []):
        if s.get('name') == set_name:
            chosen_set = s
            break
    if not chosen_set:
        return jsonify({'success': False, 'error': f"Set '{set_name}' not found"}), 404
    to_save = {'searches': chosen_set.get('searches', [])}
    success, err = save_keywords(to_save)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': err}), 500

@app.route('/api/save_named_set', methods=['POST'])
def api_save_named_set():
    req = request.get_json()
    name = req.get('name', '').strip()
    searches = req.get('searches')
    if not name:
        return jsonify({'success': False, 'error': 'Set name required'}), 400
    if not isinstance(searches, list):
        return jsonify({'success': False, 'error': 'Invalid searches data'}), 400

    saved_sets = load_saved_sets()
    exists = False
    for s in saved_sets['sets']:
        if s['name'] == name:
            s['searches'] = searches
            exists = True
            break
    if not exists:
        saved_sets['sets'].append({'name': name, 'searches': searches})

    success, err = save_saved_sets(saved_sets)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': err}), 500

@app.route('/api/delete_named_set', methods=['POST'])
def api_delete_named_set():
    req = request.get_json()
    name = req.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Set name required'}), 400

    saved_sets = load_saved_sets()
    before_count = len(saved_sets['sets'])
    saved_sets['sets'] = [s for s in saved_sets['sets'] if s['name'] != name]
    if len(saved_sets['sets']) == before_count:
        return jsonify({'success': False, 'error': 'Set name not found'}), 404
    success, err = save_saved_sets(saved_sets)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': err}), 500
        
@app.route('/api/log_content')
def api_log_content():
    filename = request.args.get('filename')
    if not filename or not (filename.startswith('logs_') and filename.endswith('.txt')):
        return jsonify({'error': 'Invalid filename parameter'}), 400
    session_folder = get_session_folder()
    full_path = os.path.join(session_folder, filename)
    if not os.path.isfile(full_path):
        return jsonify({'error': 'File not found'}), 404
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'content': content})
    except Exception as e:
        return jsonify({'error': f"Couldn't read file: {str(e)}"}), 500

@app.route('/api/post_to_jira', methods=['POST'])
def post_to_jira():
    data = request.get_json()
    issue_key = data.get('jira_id', '').strip()
    comment_text = data.get('comment_text', '').strip()
    if not issue_key:
        return jsonify({'success': False, 'error': 'Jira ID is required'}), 400
    if not comment_text:
        return jsonify({'success': False, 'error': 'No log content provided'}), 400
    try:
        from jira_interface.jira_interface import FordJIRA, JIRAUpdateFailure
        jira = FordJIRA(server="Ford")
        response = jira.add_comment(issue_key, comment_text)
        return jsonify({'success': True, 'response': response})
    except JIRAUpdateFailure as e:
        return jsonify({'success': False, 'error': f'JIRA update failed: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f'Unexpected error: {str(e)}'}), 500

PAGE_HTML = """

<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log Analyzer</title>
<link 
  href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" 
  rel="stylesheet" 
  crossorigin="anonymous">
<style>
  body { background-color: #f4f6f8; }
  .container { max-width: 960px; margin-top: 30px; margin-bottom: 40px; }
  .nav-pills .nav-link.active {
    background-color: #0d6efd;
  }
  .rounded-pill {
    border-radius: 50rem !important;
  }
  .form-control, .form-select {
    border-radius: 0.5rem;
  }
  button.btn {
    border-radius: 0.5rem;
  }
  table tbody tr:hover {
    background-color: #f1f9ff;
  }
  .table-responsive {
    max-height: 400px;
    overflow-y: auto;
  }
  label.form-label { font-weight: 600; }
  .mt-3 { margin-top: 1rem !important; }
  .mb-3 { margin-bottom: 1rem !important; }
</style>
</head>
<body>
<div class="container bg-white p-4 rounded shadow-sm">
  <ul class="nav nav-pills nav-fill rounded-pill mb-4" role="tablist" aria-label="Main tabs">
    <li class="nav-item" role="presentation">
      <a class="nav-link rounded-pill {% if active_tab == 'analyzer' %}active{% endif %}" href="{{ url_for('home') }}" role="tab" aria-selected="{{ 'true' if active_tab == 'analyzer' else 'false' }}">Single QUIP Event</a>
    </li>
    <li class="nav-item" role="presentation">
      <a class="nav-link rounded-pill {% if active_tab == 'manage' %}active{% endif %}" href="{{ url_for('manage_keywords') }}" role="tab" aria-selected="{{ 'true' if active_tab == 'manage' else 'false' }}">Searching Techniques</a>
    </li>
    <li class="nav-item" role="presentation">
      <a class="nav-link rounded-pill {% if active_tab == 'download_logs' %}active{% endif %}" href="{{ url_for('download_logs') }}" role="tab" aria-selected="{{ 'true' if active_tab == 'download_logs' else 'false' }}">Analyzed Logs</a>
    </li>
  </ul>

  {% if active_tab == 'analyzer' %}
    <h1 class="mb-4 text-center">Log Analyzer</h1>
    <div class="alert alert-danger alert-dismissible fade show" role="alert" style="max-width: 420px; margin: 0 auto 1rem;">
  Please load a filter from the <strong>Searching Techniques</strong> tab before proceeding with the QUIP event search.
  <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
</div>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="alert alert-{{ 'success' if category=='success' else 'danger' }}">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    <form method="post" id="quipForm" aria-label="QUIP event form" class="mx-auto" style="max-width: 420px;">
      <label for="quip_event" class="form-label">Enter QUIP Event ID:</label>
      <input type="text" id="quip_event" name="quip_event" class="form-control mb-3" placeholder="Paste QUIP event ID" required autocomplete="off">
      <button type="submit" id="submitBtn" class="btn btn-primary w-100">Search & Download Analyzed Logs</button>
    </form>
    <div id="loading" class="text-primary fw-bold text-center mt-3" role="status" aria-live="polite" aria-atomic="true" style="display:none;">
      Processing... Please wait.
    </div>
    <script>
      const form = document.getElementById('quipForm');
      const loading = document.getElementById('loading');
      const submitBtn = document.getElementById('submitBtn');
      form.addEventListener('submit', () => {
        submitBtn.disabled = true;
        loading.style.display = 'block';
      });
    </script>

  {% elif active_tab == 'manage' %}
    <h1 class="mb-4 text-center">Searching Techniques</h1>
    <div class="d-flex flex-wrap gap-2 mb-3 align-items-center">
      <select id="savedSetSelector" class="form-select" aria-label="Select saved keyword set" style="max-width: 280px;">
        <option value="">-- Select Saved Set --</option>
      </select>
      <button id="loadSavedSetBtn" class="btn btn-primary" type="button">Load Saved Set</button>
      <button id="deleteSavedSetBtn" class="btn btn-danger" type="button">Delete Saved Set</button>
    </div>

    <div class="d-flex gap-2 mb-3 align-items-center">
      <input type="text" id="newSetNameInput" class="form-control" style="max-width: 280px;" placeholder="Name this keyword set (to save current)" aria-label="Enter name for new saved keyword set">
      <button id="saveNamedSetBtn" class="btn btn-success" type="button">Save as New Set</button>
    </div>

    <!-- Added Save Existing Set button -->
    <div class="d-flex gap-2 mb-3 align-items-center">
      <button id="saveExistingSetBtn" class="btn btn-primary" type="button" disabled>Save Loaded Set</button>
      <span id="loadedSetInfo" class="ms-3 fst-italic text-secondary">No saved set loaded.</span>
    </div>

    <div class="d-flex gap-2 mb-3">
      <button id="addKeywordBtn" class="btn btn-success" type="button">+ Add Keyword</button>
      <!-- Removed the original generic Save Changes button to avoid confusion -->
    </div>

    <div class="table-responsive" style="max-height: 440px;">
      <table id="keywordsTable" class="table table-striped table-hover align-middle">
        <thead>
          <tr>
            <th>File Name <small class="text-muted">(e.g., aos_logcat.txt)</small></th>
            <th>Keywords <small class="text-muted">(comma separated)</small></th>
            <th style="width: 100px;">Actions</th>
          </tr>
        </thead>
        <tbody id="tableBody"></tbody>
      </table>
    </div>
    <div id="saveStatus" role="alert" class="mt-3 fw-bold"></div>

    <script>
      let keywordsData = [];
      let savedSets = [];
      let loadedSetName = null; // Keep track of the currently loaded saved set's name

      async function loadKeywords(){
        try {
          let resp = await fetch('/api/keywords');
          if(!resp.ok) throw new Error(`Status ${resp.status}`);
          let data = await resp.json();
          keywordsData = Array.isArray(data.searches) ? data.searches : [];
        } catch(e){
          keywordsData = [];
          alert('Failed to load keywords: '+e);
        }
      }

      function renderTable(){
        const tbody = document.getElementById('tableBody');
        tbody.innerHTML = '';
        if(!Array.isArray(keywordsData) || keywordsData.length === 0){
          const tr = document.createElement('tr');
          const td = document.createElement('td');
          td.colSpan = 3;
          td.className = 'text-center fst-italic text-secondary';
          td.textContent = 'No keywords defined yet. Click "+ Add Keyword" to create one.';
          tr.appendChild(td);
          tbody.appendChild(tr);
          return;
        }
        keywordsData.forEach((entry,index)=>{
          const tr = document.createElement('tr');
          const tdFile = document.createElement('td');
          const inputFile = document.createElement('input');
          inputFile.type = 'text';
          inputFile.className = 'form-control form-control-sm';
          inputFile.value = entry.file || '';
          inputFile.placeholder = 'Filename';
          inputFile.setAttribute('aria-label','File name for keyword group');
          inputFile.oninput = ()=> keywordsData[index].file = inputFile.value;
          tdFile.appendChild(inputFile);
          tr.appendChild(tdFile);

          const tdKeywords = document.createElement('td');
          const inputKW = document.createElement('input');
          inputKW.type = 'text';
          inputKW.className = 'form-control form-control-sm';
          inputKW.value = Array.isArray(entry.keywords) ? entry.keywords.join(', ') : '';
          inputKW.placeholder = 'keyword1, keyword2, ...';
          inputKW.setAttribute('aria-label','Keywords comma separated');
          inputKW.oninput = ()=> {
            keywordsData[index].keywords = inputKW.value.split(',').map(w=>w.trim()).filter(w=>w.length>0);
          }
          tdKeywords.appendChild(inputKW);
          tr.appendChild(tdKeywords);

          const tdActions = document.createElement('td');
          const delBtn = document.createElement('button');
          delBtn.className = 'btn btn-sm btn-danger';
          delBtn.type = 'button';
          delBtn.title = 'Delete this keyword entry';
          delBtn.textContent = 'Delete';
          delBtn.onclick = ()=> {
            if(confirm('Are you sure you want to delete this keyword entry?')){
              keywordsData.splice(index,1);
              renderTable();
              document.getElementById('saveStatus').textContent = '';
            }
          }
          tdActions.appendChild(delBtn);
          tr.appendChild(tdActions);

          tbody.appendChild(tr);
        })
      }

      // Validation reusable function
      function validateKeywordsData(){
        for(let i=0; i<keywordsData.length; i++){
          if(!keywordsData[i].file || keywordsData[i].file.trim()===''){
            alert(`File name cannot be empty (row ${i+1})`);
            return false;
          }
          if(!Array.isArray(keywordsData[i].keywords) || keywordsData[i].keywords.length === 0){
            alert(`Keywords cannot be empty (row ${i+1})`);
            return false;
          }
        }
        return true;
      }

      async function saveKeywordsToAPI(dataToSave){
        try{
          let resp = await fetch('/api/keywords',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify(dataToSave)
          });
          let data = await resp.json();
          if(data.success){
            return { success: true };
          }
          else return { success: false, error: data.error || 'Unknown error' };
        }catch(e){
          return { success: false, error: e.message };
        }
      }

      // Save loaded set updates (overwrite existing set)
      async function saveLoadedSet(){
        if(!loadedSetName){
          alert("No saved set loaded to save.");
          return;
        }
        if(!validateKeywordsData()) return;
        // Update the savedSets with the edited info for loadedSetName
        let statusDiv = document.getElementById('saveStatus');
        statusDiv.style.color = '';
        statusDiv.textContent = `Saving changes to set "${loadedSetName}"...`;

        // Update savedSets array locally
        let found = false;
        for (let s of savedSets){
          if(s.name === loadedSetName){
            s.searches = keywordsData; 
            found = true; 
            break;
          }
        }
        if(!found){
          // In case the set disappeared somehow - add it (?)
          savedSets.push({name: loadedSetName, searches: keywordsData});
        }

        // Save updated savedSets.yaml
        const res = await fetch('/api/save_named_set',{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({name: loadedSetName, searches: keywordsData})
        });
        const jsonRes = await res.json();
        if(jsonRes.success){
          statusDiv.style.color = 'green';
          statusDiv.textContent = `Saved changes to set "${loadedSetName}".`;
          await loadSavedSets(); // Refresh savedSets dropdown
        }else{
          statusDiv.style.color = 'red';
          statusDiv.textContent = 'Error saving set: ' + (jsonRes.error || 'Unknown error');
        }
      }

      async function saveKeywords(){
        const statusDiv = document.getElementById('saveStatus');
        statusDiv.style.color = '';
        statusDiv.textContent = 'Saving...';
        if(!validateKeywordsData()){
          statusDiv.textContent = '';
          return;
        }
        const result = await saveKeywordsToAPI({searches: keywordsData});
        if(result.success){
          statusDiv.style.color = 'green';
          statusDiv.textContent = 'Keywords saved successfully.';
          await loadSavedSets(); // refresh saved sets dropdown if needed
        }
        else{
          statusDiv.style.color = 'red';
          statusDiv.textContent = 'Failed to save keywords: ' + result.error;
        }
      }

      async function loadSavedSets(){
        try{
          let resp = await fetch('/api/saved_sets');
          if(!resp.ok) throw new Error(`Status ${resp.status}`);
          let data = await resp.json();
          savedSets = Array.isArray(data.sets) ? data.sets : [];
          populateSavedSetDropdown();
        }catch(e){
          savedSets = [];
          alert('Failed to load saved sets: ' + e);
        }
      }

      function populateSavedSetDropdown(){
        const selector = document.getElementById('savedSetSelector');
        selector.innerHTML = '<option value="">-- Select Saved Set --</option>';
        savedSets.forEach(set=>{
          const opt = document.createElement('option');
          opt.value = set.name;
          opt.textContent = set.name;
          selector.appendChild(opt);
        });
      }

      // Load a saved set into the editing table and track loadedSetName
      async function loadSelectedSet(){
        const selector = document.getElementById('savedSetSelector');
        const setName = selector.value;
        const statusDiv = document.getElementById('saveStatus');
        if(!setName) {
          alert("Please select a saved set to load.");
          return;
        }
        statusDiv.style.color = '';
        statusDiv.textContent = `Loading saved set "${setName}"...`;
        try{
          let resp = await fetch('/api/saved_sets',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({set_name:setName}),
          });
          let data = await resp.json();
          if(data.success){
            // After loading searches into search.yaml, get them
            await loadKeywords();
            renderTable();
            loadedSetName = setName;
            document.getElementById('loadedSetInfo').textContent = `Loaded set: "${loadedSetName}"`;
            document.getElementById('saveExistingSetBtn').disabled = false;
            statusDiv.style.color = 'green';
            statusDiv.textContent = `Set "${setName}" loaded and ready for editing.`;
          } else {
            alert('Failed to load saved set: ' + (data.error ?? 'Unknown error'));
            statusDiv.textContent = '';
          }
        }catch(e){
          alert('Error loading saved set: ' + e);
          statusDiv.textContent = '';
        }
      }

      // Save as new named set
      async function saveNamedSet(){
        const input = document.getElementById('newSetNameInput');
        const name = input.value.trim();
        const statusDiv = document.getElementById('saveStatus');
        if(!name){
          alert('Please enter a name for the new saved set.');
          return;
        }
        if(!validateKeywordsData()) return;
        statusDiv.style.color = '';
        statusDiv.textContent = 'Saving named set...';
        try{
          const resp = await fetch('/api/save_named_set',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({name: name, searches: keywordsData})
          });
          const data = await resp.json();
          if(data.success){
            statusDiv.style.color = 'green';
            statusDiv.textContent = `Named set "${name}" saved successfully.`;
            input.value = '';
            await loadSavedSets();
            // Automatically load the newly saved set for editing:
            document.getElementById('savedSetSelector').value = name;
            loadedSetName = name;
            document.getElementById('loadedSetInfo').textContent = `Loaded set: "${loadedSetName}"`;
            document.getElementById('saveExistingSetBtn').disabled = false;
          } else {
            statusDiv.style.color = 'red';
            statusDiv.textContent = 'Failed to save named set: ' + (data.error || 'Unknown error');
          }
        }catch(e){
          statusDiv.style.color = 'red';
          statusDiv.textContent = 'Error saving named set: ' + e;
        }
      }

      // Delete saved named set
      async function deleteNamedSet(){
        const selector = document.getElementById('savedSetSelector');
        const name = selector.value;
        const statusDiv = document.getElementById('saveStatus');
        if(!name){
          alert('Please select a saved set to delete.');
          return;
        }
        if(!confirm(`Are you sure you want to delete the saved set "${name}"?`)){
          return;
        }
        statusDiv.style.color = '';
        statusDiv.textContent = 'Deleting named set...';
        try{
          const resp = await fetch('/api/delete_named_set',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({name: name})
          });
          const data = await resp.json();
          if(data.success){
            statusDiv.style.color = 'green';
            statusDiv.textContent = `Named set "${name}" deleted successfully.`;
            // If the deleted set is currently loaded for editing, clear loadedSetName & reset UI
            if(loadedSetName === name){
              loadedSetName = null;
              document.getElementById('loadedSetInfo').textContent = 'No saved set loaded.';
              document.getElementById('saveExistingSetBtn').disabled = true;
              // Optionally clear table:
              keywordsData = [];
              renderTable();
            }
            await loadSavedSets();
            // Clear dropdown selection after delete:
            selector.value = '';
          } else {
            statusDiv.style.color = 'red';
            statusDiv.textContent = 'Failed to delete named set: ' + (data.error || 'Unknown error');
          }
        }catch(e){
          statusDiv.style.color = 'red';
          statusDiv.textContent = 'Error deleting named set: ' + e;
        }
      }

      document.getElementById('addKeywordBtn').addEventListener('click', () => {
        keywordsData.push({file:'', keywords: []});
        renderTable();
        document.getElementById('saveStatus').textContent = '';
      });

      // Save loaded set button
      document.getElementById('saveExistingSetBtn').addEventListener('click', () => {
        saveLoadedSet();
      });

      // Save as new set button
      document.getElementById('saveNamedSetBtn').addEventListener('click', () => {
        saveNamedSet();
      });

      document.getElementById('loadSavedSetBtn').addEventListener('click', () => {
        loadSelectedSet();
      });

      document.getElementById('deleteSavedSetBtn').addEventListener('click', () => {
        deleteNamedSet();
      });

      (async () => {
        await loadKeywords();
        renderTable();
        await loadSavedSets();
        // Initially disable Save Loaded Set button until load
        document.getElementById('saveExistingSetBtn').disabled = true;
        document.getElementById('loadedSetInfo').textContent = 'No saved set loaded.';
      })();
    </script>

  {% elif active_tab == 'download_logs' %}
    <h1 class="mb-4 text-center">Analyzed Logs</h1>
    {% if not log_files %}
      <p class="text-center text-muted">No output log files found yet.</p>
    {% else %}
      <div class="mb-3 w-50 mx-auto">
        <label for="logFileSelect" class="form-label">Select log files to view (Ctrl/Cmd + click to select multiple):</label>
        <select class="form-select" id="logFileSelect" aria-label="Select log files" multiple style="height: 12rem;">
          {% for log_file in log_files %}
          <option value="{{ log_file.name }}">{{ log_file.name }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="mb-3 w-50 mx-auto">
        <label for="jiraIdInput" class="form-label">Enter Jira ID:</label>
        <input type="text" id="jiraIdInput" class="form-control" placeholder="e.g. PROJ-1234" aria-label="Jira ID input">
      </div>
      <div class="mb-3 w-50 mx-auto d-flex justify-content-between">
        <button id="clearLogsBtn" class="btn btn-secondary">Clear Logs</button>
        <button id="postToJiraBtn" class="btn btn-warning">Post to Jira</button>
      </div>
      <div class="mb-4 w-100">
        <label for="logContentBox" class="form-label">Log file contents (editable before posting):</label>
        <textarea id="logContentBox" rows="20" class="form-control" style="white-space: pre-wrap; font-family: monospace;" aria-live="polite"></textarea>
      </div>
      <div class="table-responsive" style="max-height:450px;">
        <table class="table table-striped table-hover align-middle">
          <thead>
            <tr>
              <th>File Name</th>
              <th>Size (KB)</th>
              <th>Download</th>
            </tr>
          </thead>
          <tbody>
            {% for log_file in log_files %}
            <tr>
              <td>{{ log_file.name }}</td>
              <td>{{ log_file.size_kb }}</td>
              <td><a href="{{ url_for('download_file', filename=log_file.name) }}" class="btn btn-primary btn-sm" role="button" download>Download</a></td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    {% endif %}
    <script>
      const logSelect = document.getElementById('logFileSelect');
      const logContentBox = document.getElementById('logContentBox');
      const jiraIdInput = document.getElementById('jiraIdInput');
      const postToJiraBtn = document.getElementById('postToJiraBtn');
      const clearLogsBtn = document.getElementById('clearLogsBtn');

      logSelect.addEventListener('change', async () => {
        const selectedOptions = Array.from(logSelect.selectedOptions);
        if (selectedOptions.length === 0) {
          logContentBox.value = '';
          return;
        }
        logContentBox.value = 'Loading...';
        let combinedContent = '';
        for (const option of selectedOptions) {
          try {
            const filename = option.value;
            const response = await fetch(`/api/log_content?filename=${encodeURIComponent(filename)}`);
            if (!response.ok) {
              combinedContent += `\n--- Error loading ${filename}: ${response.statusText} ---\n`;
              continue;
            }
            const data = await response.json();
            if (data.error) {
              combinedContent += `\n--- Error loading ${filename}: ${data.error} ---\n`;
            } else {
              combinedContent += `\n--- Begin file: ${filename} ---\n`;
              combinedContent += data.content;
              combinedContent += `\n--- End file: ${filename} ---\n`;
            }
          } catch (e) {
            combinedContent += `\n--- Unexpected error loading ${option.value}: ${e.message} ---\n`;
          }
        }
        logContentBox.value = combinedContent.trim();
      });

      clearLogsBtn.addEventListener('click', () => {
        logContentBox.value = '';
        // Clear dropdown selection
        for (let i=0; i<logSelect.options.length; i++){
          logSelect.options[i].selected = false;
        }
      });

      postToJiraBtn.addEventListener('click', async () => {
        const jiraId = jiraIdInput.value.trim();
        const logText = logContentBox.value.trim();
        if(!jiraId){
          alert('Please enter a Jira ID.');
          jiraIdInput.focus();
          return;
        }
        if(!logText){
          alert('No log content to post. Please select one or more log files first or enter the content manually.');
          return;
        }
        postToJiraBtn.disabled = true;
        postToJiraBtn.textContent = 'Posting...';
        try {
          const resp = await fetch('/api/post_to_jira', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              jira_id: jiraId,
              comment_text: logText
            })
          });
          const data = await resp.json();
          if(data.success){
            alert('Successfully posted logs to Jira!');
            jiraIdInput.value = '';
            logContentBox.value = '';
            for (let i=0; i<logSelect.options.length; i++){
              logSelect.options[i].selected = false;
            }
          } else {
            alert('Failed to post to Jira: ' + (data.error || 'Unknown error'));
          }
        } catch(e) {
          alert('Error posting to Jira: ' + e.message);
        } finally {
          postToJiraBtn.disabled = false;
          postToJiraBtn.textContent = 'Post to Jira';
        }
      });
    </script>
  {% endif %}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>

"""

if __name__ == "__main__":
    app.run(debug=True,host="0.0.0.0",port=5000)
