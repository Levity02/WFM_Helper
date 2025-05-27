# app.py (top)
# --- Gevent Monkey Patching (ensure this is VERY FIRST) ---
import gevent
from gevent import monkey
monkey.patch_all() # Patches standard library for gevent compatibility
from gevent.pywsgi import WSGIServer # Import the gevent WSGI server

# --- PyInstaller Path Adjustments (ensure this is before Flask app creation) ---
import sys
import os # os is used for path joining and checks

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    print(f"Running in PyInstaller bundle. MEIPASS: {sys._MEIPASS}")
    try:
        template_folder = os.path.join(sys._MEIPASS, 'templates')
        static_folder = os.path.join(sys._MEIPASS, 'static')
        # Check if these folders exist at the expected bundled location
        if not os.path.exists(template_folder):
            print(f"WARNING: Bundled template_folder not found at: {template_folder}")
        if not os.path.exists(static_folder):
            print(f"WARNING: Bundled static_folder not found at: {static_folder}")
        flask_app_kwargs = {'template_folder': template_folder, 'static_folder': static_folder}
    except Exception as e:
        print(f"ERROR setting up bundled paths: {e}")
        flask_app_kwargs = {} # Fallback to default if error
else:
    print("Running in normal Python environment.")
    flask_app_kwargs = {} # Standard Flask initialization uses relative paths

# Original Flask and related imports
from flask import Flask, render_template, session, request, jsonify, g, url_for
from flask_socketio import SocketIO # Keep SocketIO import here

# Other standard library/third-party imports from your original file
import random
# import os # Already imported above
import uuid
import requests
import json
import threading # Keep for the processing_thread logic, gevent will make it cooperative

# Your custom logic module
import wfm_logic

# --- Flask App Initialization ---
app = Flask(__name__, **flask_app_kwargs) # Initialize Flask app using the kwargs
app.secret_key = os.urandom(24) # Now you can set the secret key

# Initialize SocketIO after app is created, now with gevent
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins="*")

# Constants from your original file
MARKET_BASE_URL = "https://warframe.market"
BANNER_IMAGES_SUBFOLDER = os.path.join('images', 'banners')
DEFAULT_BANNER_PATH = os.path.join('images', 'banner_default.jpg')

processing_thread = None # This will now be a gevent-cooperative thread

def get_banner_image_path():
    banners_full_path = os.path.join(app.static_folder, 'images', 'banners')
    selected_banner_rel_path = DEFAULT_BANNER_PATH
    if os.path.exists(banners_full_path) and os.path.isdir(banners_full_path):
        available_banners = [f for f in os.listdir(banners_full_path) if os.path.isfile(os.path.join(banners_full_path, f))]
        if available_banners:
            chosen_banner_name = random.choice(available_banners)
            selected_banner_rel_path = os.path.join(BANNER_IMAGES_SUBFOLDER, chosen_banner_name).replace('\\', '/')
    # Check if the selected (or default) banner actually exists
    if not os.path.exists(os.path.join(app.static_folder, selected_banner_rel_path)):
        # If chosen/default doesn't exist, try to fall back to the explicit default again, just in case
        if os.path.exists(os.path.join(app.static_folder, DEFAULT_BANNER_PATH)):
            return DEFAULT_BANNER_PATH.replace('\\','/')
        return None # No banner found
    return selected_banner_rel_path

# Initialization logic from your original file (wfm_logic parts)
print("Flask App: Initializing WFM Logic...")
wfm_logic.load_config() # Load config early
if not wfm_logic.DEVICE_ID:
    wfm_logic.DEVICE_ID = str(uuid.uuid4())
    print(f"Flask App: Generated new Device-Id for wfm_logic: {wfm_logic.DEVICE_ID}")
    # Ensure config is saved if a new device_id was generated.
    # load_config might return user_id if it exists, useful for save_config.
    loaded_config_data = wfm_logic.load_config() # Re-call may not be ideal, but ensure data is there
    user_id_from_config = loaded_config_data.get("user_id")
    # if user_id_from_config: # Only save if user_id known, or save can handle None
    wfm_logic.save_config(user_id_from_config) # save_config should handle None user_id if needed, or this logic needs adjustment

if not hasattr(wfm_logic, 'main_session') or wfm_logic.main_session is None:
    print("Flask App: Initializing wfm_logic.main_session...")
    wfm_logic.main_session = requests.Session()
    wfm_logic.main_session.headers.update({
        "User-Agent": "PythonScript/WFMHelperWebApp/0.4.3 (Flask; Python requests; SocketIO)",
        "Platform": wfm_logic.PLATFORM, "Language": wfm_logic.LANGUAGE
    })

print("Flask App: Fetching all items map via wfm_logic...")
if wfm_logic.fetch_all_items_and_build_map_v2(wfm_logic.main_session):
    print(f"Flask App: Item map built: {len(wfm_logic.ITEM_ID_TO_DETAILS_MAP)} items.")
else:
    print("Flask App: Warning - Item map could not be built on startup.")
print("-" * 30)


@app.before_request
def ensure_session_keys_and_wfm_globals():
    keys_to_init = {
        'wfm_jwt': None, 'wfm_csrf': None, 'wfm_user_id': None,
        'wfm_ingame_name': None, 'wfm_avatar_url': None,
        'wfm_user_status': None, 'wfm_user_reputation': None, 'wfm_auth_error': None
    }
    for key, default_value in keys_to_init.items():
        if key not in session:
            session[key] = default_value

    # Keep wfm_logic globals in sync with session
    wfm_logic.CURRENT_JWT_STRING = session.get('wfm_jwt')
    wfm_logic.CSRF_TOKEN = session.get('wfm_csrf')
    # DEVICE_ID is managed globally in wfm_logic, loaded from config or generated

@app.route('/')
def index():
    session['wfm_auth_error'] = None # Clear previous auth errors on page load

    # Attempt to get JWT from browser cookies if not already in session
    if not session.get('wfm_jwt'):
        browser_jwt = wfm_logic.try_fetch_jwt_from_browsers()
        if browser_jwt:
            # If JWT found, validate it by fetching /v2/me
            if not wfm_logic.DEVICE_ID: # Ensure device_id exists before API call
                 wfm_logic.DEVICE_ID = str(uuid.uuid4())
                 print(f"Flask App (index/jwt_fetch): Fallback Generated Device-Id: {wfm_logic.DEVICE_ID}")
                 # Potentially save config here if new device_id was generated and user_id becomes known
            profile_api_data, auth_failed, _ = wfm_logic.fetch_v2_me_manual_jwt(
                wfm_logic.main_session, browser_jwt, wfm_logic.DEVICE_ID, called_from_get_jwt=True
            )
            if not auth_failed and profile_api_data:
                session['wfm_jwt'] = browser_jwt
                jwt_payload = wfm_logic.parse_jwt_payload(browser_jwt)
                if jwt_payload:
                    session['wfm_csrf'] = jwt_payload.get("csrf_token")
                # Update wfm_logic globals
                wfm_logic.CURRENT_JWT_STRING = session['wfm_jwt']
                wfm_logic.CSRF_TOKEN = session.get('wfm_csrf')
            # No else here; if auth failed, user remains unauthenticated

    # Prepare user profile data for the template
    user_profile_for_template = {
        "username": "Guest", "status": "Invisible",
        "avatar_url": url_for('static', filename='images/default_avatar.png'),
        "reputation": 0, "visible_orders_count": 0, "total_listings_count": 0,
        "profile_url": MARKET_BASE_URL
    }
    api_status_final_source_for_log = "Default (Invisible)" # For debugging status source

    if session.get('wfm_jwt'):
        # Update wfm_logic globals (in case they were cleared or changed elsewhere)
        wfm_logic.CURRENT_JWT_STRING = session.get('wfm_jwt')
        wfm_logic.CSRF_TOKEN = session.get('wfm_csrf')

        # Fetch /v2/me to get primary profile data
        me_profile_data, me_auth_failed, me_ingame_name = wfm_logic.fetch_v2_me_manual_jwt(
            wfm_logic.main_session, session['wfm_jwt'], wfm_logic.DEVICE_ID
        )

        if not me_auth_failed and me_profile_data and isinstance(me_profile_data, dict):
            session['wfm_user_id'] = me_profile_data.get("id")
            session['wfm_ingame_name'] = me_ingame_name if me_ingame_name else me_profile_data.get("ingameName") # Prefer name from JWT content if available
            session['wfm_user_reputation'] = me_profile_data.get("reputation", 0)

            api_avatar_path = me_profile_data.get("avatar")
            if api_avatar_path:
                session['wfm_avatar_url'] = f"{wfm_logic.STATIC_ASSETS_BASE_URL}{api_avatar_path.lstrip('/')}"
            else:
                session['wfm_avatar_url'] = url_for('static', filename='images/default_avatar.png')

            # Ensure CSRF token from JWT payload if not already set (e.g. after manual JWT submission)
            if not session.get('wfm_csrf'):
                jwt_payload = wfm_logic.parse_jwt_payload(session['wfm_jwt'])
                if jwt_payload:
                    session['wfm_csrf'] = jwt_payload.get("csrf_token")
                    wfm_logic.CSRF_TOKEN = session.get('wfm_csrf') # Update global

            session['wfm_user_status'] = "Invisible" # Default, to be updated by further checks
            api_status_from_me = me_profile_data.get("status")
            if api_status_from_me:
                if api_status_from_me == "ingame":
                    session['wfm_user_status'] = "Online In Game"
                    api_status_final_source_for_log = "/v2/me"
                elif api_status_from_me == "online":
                    session['wfm_user_status'] = "Online"
                    api_status_final_source_for_log = "/v2/me"
            
            # Save/update config if user_id is now known and doesn't match, or if device_id was missing
            if session.get('wfm_user_id'):
                current_config = wfm_logic.load_config() # load fresh current state of config
                if current_config.get("user_id") != session['wfm_user_id'] or \
                   not current_config.get("device_id") or \
                   current_config.get("device_id") != wfm_logic.DEVICE_ID: # also save if device_id changed
                    print(f"Flask App: Saving config with user_id: {session['wfm_user_id']} and device_id: {wfm_logic.DEVICE_ID}")
                    wfm_logic.save_config(session['wfm_user_id']) # save_config uses global DEVICE_ID

    # Populate template profile from session (which should be up-to-date now)
    if session.get('wfm_ingame_name'):
        user_profile_for_template["username"] = session['wfm_ingame_name']
        user_profile_for_template["status"] = session.get('wfm_user_status', 'Invisible')
        user_profile_for_template["avatar_url"] = session.get('wfm_avatar_url')
        user_profile_for_template["profile_url"] = f"{MARKET_BASE_URL}/profile/{session['wfm_ingame_name']}"
        user_profile_for_template["reputation"] = session.get('wfm_user_reputation', 0)
    elif not session.get('wfm_jwt'): # If still no JWT after all attempts
        user_profile_for_template["username"] = "Not Authenticated"
        user_profile_for_template["status"] = "Invisible"


    sell_orders_for_template = []
    user_status_api_val_source = None # To track if status was updated from profile page or item lookups

    if session.get('wfm_jwt') and session.get('wfm_csrf') and session.get('wfm_ingame_name'):
        # Ensure wfm_logic globals are set for this part
        wfm_logic.CURRENT_JWT_STRING = session.get('wfm_jwt')
        wfm_logic.CSRF_TOKEN = session.get('wfm_csrf')

        # Fetch orders from profile page scrape
        fetched_orders_list, status_from_profile_scrape = wfm_logic.fetch_orders_from_profile_page(
            wfm_logic.main_session, session['wfm_ingame_name'], session['wfm_jwt']
        )
        all_visible_slugs_from_profile = [] # For status lookup fallback

        if fetched_orders_list is not None:
            # If /v2/me didn't give a clear online/ingame status, try status from profile page
            if status_from_profile_scrape and session.get('wfm_user_status', 'Invisible') == 'Invisible':
                user_status_api_val_source = status_from_profile_scrape
                api_status_final_source_for_log = "Profile Page Scrape"

            # Process orders for template
            sell_orders_for_template_raw = [o for o in fetched_orders_list if o.get("type") == "sell"]
            visible_count = 0
            temp_processed_orders = []
            for order_data in sell_orders_for_template_raw:
                # Set initial UI display text for status and min price
                order_data["initial_status_text"] = "HIDDEN" if not order_data.get("visible") else "Idle" #
                order_data["competitor_price"] = "N/A" # Default, will be updated by JS if processing

                # min_price_display should reflect numeric_min_price or "skip"
                if order_data.get("is_skipped"):
                    order_data["min_price_display"] = "skip" # Placeholder text handled by JS based on this
                else:
                    order_data["min_price_display"] = order_data.get("numeric_min_price", "") # Actual value

                temp_processed_orders.append(order_data)
                if order_data.get("visible"):
                    visible_count += 1
                    if order_data.get("item_slug"): # Collect slugs for potential status lookup
                        all_visible_slugs_from_profile.append(order_data.get("item_slug"))
            
            # Sort orders: visible first, then alphabetically by item name
            sell_orders_for_template = sorted(
                temp_processed_orders,
                key=lambda x: (not x['visible'], x.get('item_name',"").lower())
            )
            user_profile_for_template["visible_orders_count"] = visible_count
            user_profile_for_template["total_listings_count"] = len(sell_orders_for_template)

        # Fallback for user status if still 'Invisible' and we have some visible orders
        if not user_status_api_val_source and all_visible_slugs_from_profile and \
           session.get('wfm_user_id') and session.get('wfm_user_status', 'Invisible') == 'Invisible': #
            for slug_to_try in all_visible_slugs_from_profile[:3]: # Limit to checking a few items
                item_orders_api = wfm_logic.fetch_orders_for_item_slug_v2(wfm_logic.main_session, slug_to_try)
                if item_orders_api:
                    for order in item_orders_api:
                        order_user = order.get("user", {})
                        if order_user.get("id") == session['wfm_user_id']: # Found one of our orders
                            user_status_api_val_source = order_user.get("status")
                            api_status_final_source_for_log = f"Item Query Fallback ({slug_to_try})"
                            break # Found status from this item
                    if user_status_api_val_source: # If status found, no need to check other items
                        break
        
        # Update session and template profile with status if found from profile page or item lookup
        if user_status_api_val_source:
            if user_status_api_val_source == "ingame":
                session['wfm_user_status'] = "Online In Game"
            elif user_status_api_val_source == "online":
                session['wfm_user_status'] = "Online"
            # If 'invisible' or other, session['wfm_user_status'] remains as it was (likely 'Invisible')
            user_profile_for_template["status"] = session['wfm_user_status'] # Update template var
        else: # If no update from scrape or item lookup, ensure template uses current session status
             user_profile_for_template["status"] = session.get('wfm_user_status', 'Invisible')


    selected_banner_path = get_banner_image_path()
    auth_error_message = session.get('wfm_auth_error') # Get error if set by submit_jwt
    
    global processing_thread
    is_processing_active = (processing_thread is not None and processing_thread.is_alive())

    # Prepare item list for "Place Order" autocomplete
    items_for_autocomplete = []
    if wfm_logic.ITEM_ID_TO_DETAILS_MAP: # Ensure map is loaded
        for item_id, details in wfm_logic.ITEM_ID_TO_DETAILS_MAP.items():
            if details and details.get("name"): # Basic validation
                items_for_autocomplete.append({
                    "id": item_id,
                    "name": details.get("name"),
                    "max_rank": details.get("mod_max_rank") # Already being fetched
                })
        items_for_autocomplete.sort(key=lambda x: x["name"].lower())


    return render_template('index.html',
                           profile=user_profile_for_template,
                           banner_image_file_path=selected_banner_path,
                           sell_orders=sell_orders_for_template,
                           market_base_url=MARKET_BASE_URL,
                           auctions_url=f"{MARKET_BASE_URL}/auctions", # Example for nav link
                           auth_error=auth_error_message,
                           current_jwt_exists=bool(session.get('wfm_jwt')),
                           is_processing=is_processing_active,
                           items_for_autocomplete=json.dumps(items_for_autocomplete) # Pass as JSON string
                           )

@app.route('/submit_jwt', methods=['POST'])
def submit_jwt_route():
    manual_jwt = request.form.get('manual_jwt_token')
    if not manual_jwt:
        return jsonify({"success": False, "message": "No JWT provided."})

    # Clear old session data on new JWT submission
    for key in ['wfm_jwt', 'wfm_csrf', 'wfm_user_id', 'wfm_ingame_name', 'wfm_avatar_url', 'wfm_user_status', 'wfm_user_reputation', 'wfm_auth_error']:
        session.pop(key, None)
    
    if not wfm_logic.DEVICE_ID: # Ensure device_id exists
         wfm_logic.DEVICE_ID = str(uuid.uuid4())
         print(f"Flask App (submit_jwt): Generated Device-Id for wfm_logic: {wfm_logic.DEVICE_ID}")

    # Validate new JWT
    profile_api_data, auth_failed, _ = wfm_logic.fetch_v2_me_manual_jwt(
        wfm_logic.main_session, manual_jwt, wfm_logic.DEVICE_ID
    )

    if not auth_failed and profile_api_data:
        session['wfm_jwt'] = manual_jwt
        jwt_payload = wfm_logic.parse_jwt_payload(manual_jwt)
        if jwt_payload:
            session['wfm_csrf'] = jwt_payload.get("csrf_token")
        
        # Update wfm_logic globals
        wfm_logic.CURRENT_JWT_STRING = session['wfm_jwt']
        wfm_logic.CSRF_TOKEN = session.get('wfm_csrf')
        
        session['wfm_user_id'] = profile_api_data.get("id") # From /v2/me
        # Potentially save config now that user_id is known
        if session.get('wfm_user_id'):
            current_config = wfm_logic.load_config()
            if current_config.get("user_id") != session['wfm_user_id'] or \
               current_config.get("device_id") != wfm_logic.DEVICE_ID:
                 print(f"Flask App (submit_jwt): Saving config with user_id: {session['wfm_user_id']} and device_id: {wfm_logic.DEVICE_ID}")
                 wfm_logic.save_config(session['wfm_user_id'])

        return jsonify({"success": True, "message": "JWT accepted. Page will refresh."})
    else:
        session['wfm_auth_error'] = "The manually submitted JWT is invalid or the Warframe.Market API could not be reached."
        return jsonify({"success": False, "message": session['wfm_auth_error']})


@app.route('/start_processing', methods=['POST'])
def start_processing_route():
    global processing_thread
    if not session.get('wfm_jwt') or not session.get('wfm_csrf') or not session.get('wfm_user_id') or not session.get('wfm_ingame_name'):
        return jsonify({"success": False, "message": "Not authenticated. Cannot start processing."}), 401

    if processing_thread is not None and processing_thread.is_alive():
        return jsonify({"success": False, "message": "Processing is already running."})

    print("Flask App: Validating min prices before starting processing...")
    # Ensure wfm_logic globals are current for this operation
    wfm_logic.CURRENT_JWT_STRING = session.get('wfm_jwt')
    wfm_logic.CSRF_TOKEN = session.get('wfm_csrf')

    # Fetch current orders for validation (from profile page scrape)
    validation_orders_data, _ = wfm_logic.fetch_orders_from_profile_page(
        wfm_logic.main_session, session['wfm_ingame_name'], session['wfm_jwt']
    )
    if validation_orders_data is None: # Error fetching orders
        return jsonify({"success": False, "message": "Failed to fetch current orders for validation. Cannot start."}), 500

    missing_min_price_items = []
    for order in validation_orders_data:
        if order.get("type") == "sell" and order.get("visible"): # Only check visible sell orders
            item_id_str = order.get("item_id")
            item_name_for_error = order.get("item_name", f"Item ID {item_id_str}") # Use resolved name if available

            min_status = wfm_logic.check_min_price_set_for_item(item_id_str) # Checks ITEM_USER_SETTINGS
            is_valid_for_start = (isinstance(min_status, int) and min_status > 0) or min_status == "skip"
            
            if not is_valid_for_start: #
                # Try to get a better item name from the global map if profile scrape didn't have it fully resolved
                resolved_item_name = wfm_logic.ITEM_ID_TO_DETAILS_MAP.get(str(item_id_str), {}).get("name", item_name_for_error)
                missing_min_price_items.append(resolved_item_name)

    if missing_min_price_items:
        message = "Cannot start. Visible items need a valid min price (number > 0) or 'skip': " + ", ".join(missing_min_price_items)
        print(f"Flask App: Validation FAILED. {message}")
        socketio.emit('new_log_message', {'message': f"Validation FAILED: {message}", 'type': 'error', 'item_id': None, 'data': {}})
        return jsonify({"success": False, "message": message}), 400

    print("Flask App: Min price validation passed. Received request to start processing.")
    socketio.emit('new_log_message', {'message': "Min price validation passed. Starting processing...", 'type': 'info', 'item_id': None, 'data': {}})

    # Callback for the thread to emit SocketIO events
    def emit_update_to_client(item_id, message, data_dict=None): #
        update_type = "info" # Default type
        actual_data_payload = {} #

        if data_dict: #
            update_type = data_dict.get('type', 'info') # Allow data_dict to specify type
            # Remove 'type' from data_dict before sending as payload if it exists
            actual_data_payload = {k: v for k, v in data_dict.items() if k != 'type'} #

        if update_type == "orders_data_snapshot": #
            # Data for snapshot should be under 'orders' key in actual_data_payload
            socketio.emit('sell_orders_snapshot', {'orders': actual_data_payload.get('orders', [])}) #
        elif update_type == "user_status_update": # Handle user status updates #
            status_payload = {'new_status': actual_data_payload.get('new_status')} #
            socketio.emit('user_status_update', status_payload) #
        else: # Default to new_log_message #
            socketio.emit('new_log_message', { #
                "item_id": item_id,
                "message": message,
                "type": update_type, # Use the determined type
                "data": actual_data_payload # Send the rest of data_dict
            })
        
        socketio.sleep(0.01) # Small sleep to allow emit to process (gevent friendly sleep)

    wfm_logic.stop_processing_flag = False # Reset flag
    # For gevent, using threading.Thread is okay if gevent's monkey patching is active.
    # It will make the thread cooperative.
    processing_thread = threading.Thread(
        target=wfm_logic.analysis_thread_target,
        args=(
            wfm_logic.main_session, session['wfm_user_id'], session['wfm_ingame_name'],
            session['wfm_jwt'], session['wfm_csrf'], wfm_logic.DEVICE_ID,
            wfm_logic.ITEM_USER_SETTINGS.copy(), # Pass a copy of current settings
            emit_update_to_client # Pass the callback
        ),
        daemon=True # Daemonize thread so it exits when main app does
    )
    processing_thread.start()
    return jsonify({"success": True, "message": "Processing started."})


@app.route('/stop_processing', methods=['POST'])
def stop_processing_route():
    global processing_thread
    if processing_thread is None or not processing_thread.is_alive():
        message = "Processing is not currently running."
        socketio.emit('new_log_message', {'message': message, 'type': 'warn', 'item_id': None, 'data': {}})
        return jsonify({"success": False, "message": message})

    print("Flask App: Received request to stop processing.")
    wfm_logic.stop_processing_flag = True
    message = "Stop signal sent. Processing will halt after the current cycle or delay."
    socketio.emit('new_log_message', {'message': message, 'type': 'warn', 'item_id': None, 'data': {}})
    return jsonify({"success": True, "message": message})

@app.route('/processing_status', methods=['GET']) # For polling if needed, or initial state check
def processing_status_route():
    global processing_thread
    is_running = processing_thread is not None and processing_thread.is_alive()
    return jsonify({"is_processing": is_running})


@app.route('/update_min_price', methods=['POST'])
def update_min_price_route():
    if not session.get('wfm_jwt') or not session.get('wfm_user_id'): # Check auth
        return jsonify({"success": False, "message": "Not authenticated."}), 401

    data = request.get_json()
    item_id_str = str(data.get('item_id')) # Ensure string for dict keys

    if not item_id_str:
        return jsonify({"success": False, "message": "Item ID is missing."}), 400

    item_name = wfm_logic.ITEM_ID_TO_DETAILS_MAP.get(item_id_str, {}).get("name", f"Item ID {item_id_str}")

    # Initialize settings for the item if it's not already in ITEM_USER_SETTINGS
    if item_id_str not in wfm_logic.ITEM_USER_SETTINGS:
        wfm_logic.ITEM_USER_SETTINGS[item_id_str] = {"numeric_min": None, "skipped": False}
    
    original_settings = wfm_logic.ITEM_USER_SETTINGS[item_id_str].copy() # For comparison

    message_parts = []
    log_type = "info" # Default log type
    settings_were_actually_changed = False

    # Target values, start with originals
    new_numeric_min_target = original_settings.get("numeric_min")
    if 'numeric_min' in data: # Only process if key exists in payload
        val_raw = data['numeric_min']
        if isinstance(val_raw, str) and val_raw.strip() == "": # Empty string means clear
            new_numeric_min_target = None
        elif val_raw is None: # Explicit null means clear
            new_numeric_min_target = None
        else:
            try:
                price_val = int(val_raw) #
                if price_val <= 0: # Must be positive
                    log_type = "error"
                    message_parts.append(f"Error for '{item_name}': numeric min must be positive.")
                else:
                    new_numeric_min_target = price_val
            except (ValueError, TypeError):
                log_type = "error"
                message_parts.append(f"Error for '{item_name}': invalid numeric min value '{val_raw}'.")
    
    new_skipped_status_target = original_settings.get("skipped", False) # Default to false if not present #
    if 'skipped' in data and isinstance(data['skipped'], bool): # Only process if key exists and is boolean
        new_skipped_status_target = data['skipped']


    if log_type != "error": # Only proceed if no validation errors so far
        if new_numeric_min_target != original_settings.get("numeric_min"):
            wfm_logic.ITEM_USER_SETTINGS[item_id_str]["numeric_min"] = new_numeric_min_target
            settings_were_actually_changed = True
            message_parts.append(f"Numeric min for '{item_name}' {'cleared' if new_numeric_min_target is None else f'set to {new_numeric_min_target}p'}.")

        if new_skipped_status_target != original_settings.get("skipped", False): # Also check original 'skipped'
            wfm_logic.ITEM_USER_SETTINGS[item_id_str]["skipped"] = new_skipped_status_target
            settings_were_actually_changed = True
            message_parts.append(f"'{item_name}' skip status changed to {'skipped' if new_skipped_status_target else 'not skipped'}.")
        
        if not settings_were_actually_changed:
            # If payload had keys but values matched originals
            if 'numeric_min' in data or 'skipped' in data:
                 message_parts.append(f"Settings for '{item_name}' are already as requested.")
            else: # No relevant keys in payload
                 message_parts.append(f"No update data provided for '{item_name}'.")
        
        if not message_parts: # Fallback if nothing else was added
            message_parts.append(f"Settings for '{item_name}' processed.")


    final_message = " ".join(message_parts).strip()
    success_status = (log_type != "error")

    if success_status and settings_were_actually_changed:
        log_type = "success" # Upgrade log type for successful changes
        if not wfm_logic.save_config(session['wfm_user_id']): # Attempt to save
            final_message += " (Config save FAILED)"
            log_type = "warn" # Downgrade if save failed
    
    # Emit log to client
    socketio.emit('new_log_message', {
        'message': final_message, 'type': log_type, 'item_id': item_id_str,
        'data': {'new_settings': wfm_logic.ITEM_USER_SETTINGS[item_id_str].copy()} if success_status else {} # Send new state on success
    })

    return jsonify({
        "success": success_status,
        "message": final_message,
        "itemId": item_id_str, # For JS to confirm which item was updated
        "itemName": item_name,
        "new_numeric_min": wfm_logic.ITEM_USER_SETTINGS[item_id_str].get("numeric_min"),
        "new_skipped_status": wfm_logic.ITEM_USER_SETTINGS[item_id_str].get("skipped", False),
        "save_warning": log_type == "warn" and settings_were_actually_changed # Flag if save failed but change was made
    }), 200 if success_status else 400

@app.route('/request_order_update', methods=['POST'])
def request_order_update_route():
    if not session.get('wfm_jwt') or not session.get('wfm_csrf') or not session.get('wfm_user_id'):
        return jsonify({"success": False, "message": "Not authenticated."}), 401

    data = request.get_json()
    order_id = data.get('order_id')
    item_id = data.get('item_id') # For logging and finding item name
    new_price = data.get('price')
    new_quantity = data.get('quantity')
    new_visible = data.get('visible')
    item_rank = data.get('rank') # Passed from JS, might be null/None

    item_name = wfm_logic.ITEM_ID_TO_DETAILS_MAP.get(str(item_id), {}).get("name", f"Item ID {item_id}")

    # Basic validation
    if not order_id or not isinstance(item_id, str) or not item_id.strip() or \
       not isinstance(new_price, int) or not isinstance(new_quantity, int) or not isinstance(new_visible, bool): #
        
        error_msg_detail = f"Order update validation failed for '{item_name}' (Order ID: {order_id}, Item ID: {item_id}). Received: price={new_price}, qty={new_quantity}, visible={new_visible}."
        socketio.emit('new_log_message', {'message': error_msg_detail, 'type': 'error', 'item_id': item_id})
        return jsonify({"success": False, "message": "Missing or invalid parameters for order update."}), 400
    
    if new_quantity < 0: # WFM API might reject, good to catch early
        socketio.emit('new_log_message', {'message': f"Order update failed for '{item_name}': Quantity cannot be negative.", 'type': 'error', 'item_id': item_id})
        return jsonify({"success": False, "message": "Quantity cannot be negative."}), 400

    # Call the wfm_logic function to update the order
    success, api_message = wfm_logic.update_order_via_v1_put(
        req_session=wfm_logic.main_session,
        order_id_to_update=order_id,
        new_price=new_price, # Pass price even if not changed by this action
        new_quantity=new_quantity,
        new_visibility=new_visible,
        current_rank=item_rank,
        jwt_token=session['wfm_jwt'],
        csrf_token_val=session['wfm_csrf'],
        device_id_val=wfm_logic.DEVICE_ID
    )

    log_message = ""
    log_type = "error" # Default to error
    snapshot_refresh_failed = False

    if success:
        log_type = "success"
        # Construct a more user-friendly log message
        log_message = f"Update request for '{item_name}' (Order ID: {order_id}) sent with Qty: {new_quantity}, Visible: {new_visible}. WFM API success."
        
        # After successful update, re-fetch all orders and send snapshot
        all_orders_snapshot_data, _ = wfm_logic.fetch_orders_from_profile_page(
            wfm_logic.main_session, session['wfm_ingame_name'], session['wfm_jwt']
        )
        if all_orders_snapshot_data is not None:
            current_sell_orders_for_ui = [o for o in all_orders_snapshot_data if o.get("type") == "sell"]
            socketio.emit('sell_orders_snapshot', {'orders': current_sell_orders_for_ui})
        else:
            snapshot_refresh_failed = True # Flag this
            print(f"Flask App: Warning - Failed to fetch orders for snapshot after update of order {order_id}.")
            log_message += " (But snapshot refresh afterwards)" #
            log_type = "warn" # Downgrade log type if refresh failed
            
    else: # API call failed
        log_message = f"Failed to update '{item_name}' (Order ID: {order_id}). Reason: {api_message}"

    socketio.emit('new_log_message', {'message': log_message.strip(), 'type': log_type, 'item_id': item_id})
    return jsonify({"success": success, "message": log_message.strip(), "api_response_detail": api_message if not success else "Update successful." })


@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')
    # Consider emitting initial status or requesting data if needed upon new connection
    # For example, current processing status, or a fresh order snapshot if appropriate

@socketio.on('disconnect')
def handle_disconnect():
    print(f'Client disconnected: {request.sid}')
    
# --- Route for DELETING an order ---
@app.route('/delete_order', methods=['POST'])
def delete_order_route():
    if not session.get('wfm_jwt') or not session.get('wfm_csrf') or not session.get('wfm_user_id'):
        return jsonify({"success": False, "message": "Not authenticated."}), 401

    data = request.get_json()
    order_id = data.get('order_id')
    item_id = data.get('item_id') # For logging and finding item name

    item_name_default = f"Item ID {item_id}" if item_id else "Unknown Item"
    item_name = wfm_logic.ITEM_ID_TO_DETAILS_MAP.get(str(item_id), {}).get("name", item_name_default) if item_id else item_name_default

    if not order_id or not isinstance(item_id, str) or not item_id.strip():
        error_msg_detail = f"Order deletion validation failed for '{item_name}': Missing order_id or item_id."
        socketio.emit('new_log_message', {'message': error_msg_detail, 'type': 'error', 'item_id': item_id})
        return jsonify({"success": False, "message": "Missing or invalid parameters for order deletion."}), 400

    # Call the wfm_logic function to delete the order using V2 API
    success, api_message = wfm_logic.delete_order_v2(
        session_obj=wfm_logic.main_session,
        order_id=order_id,
        jwt_token=session['wfm_jwt'],
        csrf_token_val=session['wfm_csrf'],
        device_id_val=wfm_logic.DEVICE_ID
    )

    log_message = ""
    log_type = "error" # Default to error
    action_message_for_ui = api_message # For the small action message area in UI

    if success:
        log_type = "success"
        log_message = f"Order for '{item_name}' (Order ID: {order_id}) successfully deleted from WFM."
        action_message_for_ui = f"'{item_name}' listing deleted."
        
        # After successful deletion, re-fetch all orders and send snapshot
        print(f"Flask App: Order {order_id} deleted. Re-fetching orders for snapshot.")
        all_orders_snapshot_data, _ = wfm_logic.fetch_orders_from_profile_page(
            wfm_logic.main_session, session['wfm_ingame_name'], session['wfm_jwt']
        )
        if all_orders_snapshot_data is not None:
            current_sell_orders_for_ui = [o for o in all_orders_snapshot_data if o.get("type") == "sell"]
            socketio.emit('sell_orders_snapshot', {'orders': current_sell_orders_for_ui})
            print(f"Flask App: Emitted updated sell_orders_snapshot after order deletion.")
        else:
            print(f"Flask App: Warning - Failed to fetch orders for snapshot after deletion of order {order_id}.")
            log_message += " (Note: UI snapshot refresh after deletion encountered an issue)" #
            action_message_for_ui += " (Snapshot refresh failed)" # Append to UI message
            log_type = "warn" # Downgrade log type
            
    else: # API call failed
        log_message = f"Failed to delete order for '{item_name}' (Order ID: {order_id}). API Reason: {api_message}"
        # action_message_for_ui is already api_message or a derivative

    # Emit detailed log to script log area
    socketio.emit('new_log_message', {'message': log_message, 'type': log_type, 'item_id': item_id, 'data': {'order_id_deleted': order_id} if success else {}})
    # Return a simpler message for the action message area
    return jsonify({"success": success, "message": action_message_for_ui })


# --- NEW ROUTE for Placing Order ---
@app.route('/place_order_route', methods=['POST'])
def place_order_route():
    if not session.get('wfm_jwt') or not session.get('wfm_csrf') or not session.get('wfm_user_id'):
        return jsonify({"success": False, "message": "Not authenticated. Cannot place order."}), 401

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "No data provided for new order."}), 400

    item_id = data.get('itemId')
    price_str = data.get('price')
    quantity_str = data.get('quantity')
    rank_str = data.get('rank')
    app_min_price_str = data.get('appMinPrice') # Optional
    app_skip_reprice = data.get('appSkipReprice', False) # Optional, defaults to False

    item_name_for_log = data.get('itemName', f"Item ID {item_id}") # For logging, fallback to ID

    # --- Validation ---
    if not item_id: # Item ID is crucial
        socketio.emit('new_log_message', {'message': f"Place Order Error: Item ID is missing.", 'type': 'error'})
        return jsonify({"success": False, "message": "Item ID is missing. Please select an item."}), 400
    try:
        price = int(price_str) #
        if price <= 0: raise ValueError("Price must be positive.")
    except (ValueError, TypeError):
        socketio.emit('new_log_message', {'message': f"Place Order Error for '{item_name_for_log}': Invalid price '{price_str}'.", 'type': 'error', 'item_id': item_id})
        return jsonify({"success": False, "message": "Price must be a positive whole number."}), 400
    try:
        quantity = int(quantity_str) #
        if quantity <= 0: raise ValueError("Quantity must be positive.")
    except (ValueError, TypeError):
        socketio.emit('new_log_message', {'message': f"Place Order Error for '{item_name_for_log}': Invalid quantity '{quantity_str}'.", 'type': 'error', 'item_id': item_id})
        return jsonify({"success": False, "message": "Quantity must be a positive whole number."}), 400
    try:
        rank = int(rank_str) # Rank can be 0
        if rank < 0: raise ValueError("Rank cannot be negative.")
    except (ValueError, TypeError):
        socketio.emit('new_log_message', {'message': f"Place Order Error for '{item_name_for_log}': Invalid rank '{rank_str}'.", 'type': 'error', 'item_id': item_id})
        return jsonify({"success": False, "message": "Rank must be a non-negative whole number (0 if not applicable)."}), 400
    
    app_numeric_min = None #
    if app_min_price_str is not None and app_min_price_str != '': # Check if provided and not empty
        try:
            app_numeric_min = int(app_min_price_str) #
            if app_numeric_min <= 0: #
                socketio.emit('new_log_message', {'message': f"Place Order Info for '{item_name_for_log}': Optional app min price '{app_min_price_str}' invalid, will not be saved.", 'type': 'warn', 'item_id': item_id})
                app_numeric_min = None # Don't save invalid app min price
        except (ValueError, TypeError):
            socketio.emit('new_log_message', {'message': f"Place Order Info for '{item_name_for_log}': Optional app min price '{app_min_price_str}' invalid, will not be saved.", 'type': 'warn', 'item_id': item_id})
            app_numeric_min = None #


    # --- Call wfm_logic to place the order ---
    success, api_message, listed_item_id = wfm_logic.place_new_sell_order_v1(
        req_session=wfm_logic.main_session,
        item_id_to_list=item_id,
        price=price,
        quantity=quantity,
        rank=rank,
        jwt_token=session['wfm_jwt'],
        csrf_token_val=session['wfm_csrf'],
        device_id_val=wfm_logic.DEVICE_ID
    )

    log_type = "error" # Default
    final_user_message = api_message # Default to API message for failures

    if success:
        log_type = "success"
        final_user_message = f"Successfully placed order for '{item_name_for_log}' (Price: {price}p, Qty: {quantity}, Rank: {rank})."
        socketio.emit('new_log_message', {'message': final_user_message, 'type': log_type, 'item_id': listed_item_id}) # Use listed_item_id from response

        # Save app-specific settings if provided and order placement was successful
        settings_changed_for_new_item = False #
        if listed_item_id: # Should be same as item_id sent, but use WFM's confirmation if available
            if listed_item_id not in wfm_logic.ITEM_USER_SETTINGS: # Initialize if new
                wfm_logic.ITEM_USER_SETTINGS[listed_item_id] = {"numeric_min": None, "skipped": False}
            
            if app_numeric_min is not None: #
                wfm_logic.ITEM_USER_SETTINGS[listed_item_id]["numeric_min"] = app_numeric_min
                settings_changed_for_new_item = True
                socketio.emit('new_log_message', {'message': f"App setting: Min price for new listing '{item_name_for_log}' set to {app_numeric_min}p.", 'type': 'info', 'item_id': listed_item_id})

            if app_skip_reprice: # app_skip_reprice is a boolean #
                wfm_logic.ITEM_USER_SETTINGS[listed_item_id]["skipped"] = True
                settings_changed_for_new_item = True
                socketio.emit('new_log_message', {'message': f"App setting: New listing '{item_name_for_log}' set to be skipped for auto-repricing.", 'type': 'info', 'item_id': listed_item_id})
            
            if settings_changed_for_new_item:
                if not wfm_logic.save_config(session['wfm_user_id']): # Save to config.json
                    socketio.emit('new_log_message', {'message': f"Warning: Failed to save app settings for new item '{item_name_for_log}' to config.", 'type': 'warn', 'item_id': listed_item_id})
                    final_user_message += " (App settings save failed)" # Append to user message


        # Refresh order list in UI by emitting snapshot
        all_orders_snapshot_data, _ = wfm_logic.fetch_orders_from_profile_page(
            wfm_logic.main_session, session['wfm_ingame_name'], session['wfm_jwt']
        )
        if all_orders_snapshot_data is not None:
            current_sell_orders_for_ui = [o for o in all_orders_snapshot_data if o.get("type") == "sell"]
            socketio.emit('sell_orders_snapshot', {'orders': current_sell_orders_for_ui})
            socketio.emit('new_log_message', {'message': "Order list refreshed after placing new order.", 'type': 'info'})
        else:
            # Problem fetching new orders list
            socketio.emit('new_log_message', {'message': "Warning: Failed to refresh order list after placing new order.", 'type': 'warn'})
            final_user_message += " (UI refresh failed)" # Append to user message
            if log_type == "success": log_type = "warn" # Downgrade overall status if refresh failed

    else: # API call failed
        socketio.emit('new_log_message', {'message': f"Failed to place order for '{item_name_for_log}': {api_message}", 'type': 'error', 'item_id': item_id})
        # final_user_message is already api_message

    return jsonify({"success": success, "message": final_user_message})


if __name__ == '__main__':
    # Ensure banners directory exists
    banners_dir_abs = os.path.join(app.static_folder, 'images', 'banners')
    if not os.path.exists(banners_dir_abs):
        os.makedirs(banners_dir_abs)
    
    # Check for default banner (optional, for user feedback)
    if not os.path.exists(os.path.join(app.static_folder, 'images', 'banner_default.jpg')):
        print(f"Warning: Default banner 'static/images/banner_default.jpg' not found.")

    print("Starting Flask-SocketIO server with gevent WSGIServer...")
    # Instead of socketio.run, we use gevent's WSGIServer directly for more control.
    # Flask-SocketIO integrates with this by having socketio.WSGIApp wrap the Flask app.
    # However, for Flask-SocketIO versions that manage the server internally based on async_mode,
    # socketio.run() is usually preferred.
    # Let's try to make socketio.run() work with gevent first, as it's cleaner.
    # The issue might be the `debug=True` in combination with gevent if gevent's own reloader isn't used.
    # Flask's reloader and gevent can sometimes conflict.
    
    # Reverting to socketio.run as it *should* handle gevent correctly.
    # The `use_reloader=False` is important here, especially with gevent.
    # Debug mode for Flask might still be problematic if it tries to use its own reloader.
    # If this still fails, we might need to disable Flask's debug mode when using gevent for serving.

    HOST = '127.0.0.1'
    PORT = 5001
    
    # Option 1: Let Flask-SocketIO handle it (preferred if it works)
    # The `debug=True` for Flask app itself might be an issue with gevent.
    # Let's ensure Flask's own debug mode isn't interfering if `socketio.run` is used with gevent.
    # Typically, when using a production-like server (even gevent's dev server),
    # you let that server handle reloading/debugging if supported, not Flask's built-in one.
    # However, `socketio.run` is a convenience wrapper.

    # If `app.debug` is True and `use_reloader` is not False, Flask's own reloader might kick in.
    # `socketio.run` passes `use_reloader` to `app.run` if not using a gevent server directly.
    # When async_mode is gevent, it *should* use the gevent server.

    print(f"Attempting to start server on http://{HOST}:{PORT}")
    try:
        # Forcing app.debug = False when using socketio.run with gevent might be more stable
        # if the reloader is causing issues. The `debug=True` in socketio.run enables SocketIO debugging.
        # app.debug = False # Explicitly disable Flask's reloader if it's conflicting
        
        socketio.run(app, host=HOST, port=PORT, debug=True, use_reloader=False)
    except Exception as e:
        print(f"Error during server startup: {e}")
        print("If the error is related to 'Invalid environment', ensure 'gevent-websocket' is installed.")
        print("Try: pip install gevent-websocket")

    # Option 2: Explicit WSGIServer (if socketio.run continues to fail to start)
    # This gives more direct control but is less common for typical Flask-SocketIO setups.
    # print(f"Starting gevent WSGIServer on http://{HOST}:{PORT}")
    # http_server = WSGIServer((HOST, PORT), app) # 'app' is the Flask app
    # try:
    #     http_server.serve_forever()
    # except KeyboardInterrupt:
    #     print("Server stopped by user.")
    # except Exception as e:
    #     print(f"Gevent WSGIServer error: {e}")