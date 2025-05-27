# wfm_logic.py
import requests
import json
import time
import uuid
import base64
from bs4 import BeautifulSoup
import sys
import os

try:
    import browser_cookie3
except ImportError:
    print("LOG: 'browser_cookie3' library is not installed.")
    browser_cookie3 = None

# --- BEGIN MODIFICATION FOR APPDATA CONFIG PATH ---
CONFIG_APP_NAME = "WFM_Helper"  # Your application's name, used for subfolder in AppData
CONFIG_FILE_NAME = "config.json"
CONFIG_DIRECTORY = None # Will be determined below

# Try to get the AppData\Roaming path
app_data_roaming_path = os.getenv('APPDATA')
if app_data_roaming_path:
    CONFIG_DIRECTORY = os.path.join(app_data_roaming_path, CONFIG_APP_NAME)
else:
    # Fallback if APPDATA isn't set (very rare on Windows, more common on other OS if not fully configured)
    # Using user's home directory and a hidden-style folder name
    home_dir = os.path.expanduser("~")
    CONFIG_DIRECTORY = os.path.join(home_dir, "." + CONFIG_APP_NAME.replace(" ", "_")) # e.g., C:\Users\Brian\.WFM_Helper

# Ensure the directory exists
if CONFIG_DIRECTORY and not os.path.exists(CONFIG_DIRECTORY):
    try:
        os.makedirs(CONFIG_DIRECTORY)
        print(f"LOG: Created configuration directory at {CONFIG_DIRECTORY}")
    except OSError as e:
        print(f"LOG: Error creating configuration directory {CONFIG_DIRECTORY}: {e}")
        # If directory creation fails, saving config will likely fail.
        # For robustness, could fall back to current working directory, but that has its own issues.
        CONFIG_DIRECTORY = None # Indicate that a persistent path could not be established

if CONFIG_DIRECTORY:
    CONFIG_FILE = os.path.join(CONFIG_DIRECTORY, CONFIG_FILE_NAME)
else:
    # Last resort fallback if no suitable config directory could be established
    # This will try to save config.json in the current working directory,
    # which for a Nuitka one-file app might be the temp extraction folder (undesirable)
    # or where the user launched it from (if CWD isn't changed by the app).
    # This situation should be rare.
    print("LOG: CRITICAL - Could not establish a persistent configuration directory. Config may be temporary.")
    CONFIG_FILE = CONFIG_FILE_NAME # Fallback to relative path
# --- END MODIFICATION FOR APPDATA CONFIG PATH ---


# This function is now primarily for understanding the execution context
# or if other assets needed to be located relative to the script/exe path.
# It is NO LONGER USED for determining CONFIG_FILE's path.
def get_executable_path_info():
    if getattr(sys, 'frozen', False):
        main_module = sys.modules.get('__main__')
        if hasattr(main_module, '__compiled__'): # Nuitka
            if hasattr(main_module.__compiled__, 'containing_dir'):
                # This is the dir of the .exe for Nuitka onefile
                return main_module.__compiled__.containing_dir
        # PyInstaller or other frozen contexts
        return os.path.dirname(sys.executable)
    else:
        # Running as a normal script (dir of this wfm_logic.py file)
        return os.path.dirname(os.path.abspath(__file__))


API_V1_BASE_URL = "https://api.warframe.market/v1"
API_V2_BASE_URL = "https://api.warframe.market/v2"
PROFILE_BASE_URL = "https://warframe.market/profile"
STATIC_ASSETS_BASE_URL = "https://warframe.market/static/assets/"
PLATFORM = "pc"
LANGUAGE = "en"
REQUEST_DELAY = 1.1 # Default, can be overridden by config
LOOP_DELAY_SECONDS = 10 # Default, can be overridden by config
BUMP_THRESHOLD_CYCLES = 5 # Default, can be overridden by config

ITEM_ID_TO_DETAILS_MAP = {}
ITEMS_MAP_FETCHED = False
ITEM_USER_SETTINGS = {} # Will be loaded from config
DEVICE_ID = None # Will be loaded from config or generated
CSRF_TOKEN = None
CURRENT_JWT_STRING = None
main_session = None # requests.Session object

stop_processing_flag = False
ITEM_BUMP_ELIGIBILITY_CYCLES = {}

def parse_jwt_payload(jwt_string):
    if not jwt_string or len(jwt_string.split('.')) < 2: return None
    try:
        payload_b64 = jwt_string.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64).decode('utf-8')
        return json.loads(payload_json)
    except Exception as e: print(f"LOG: Error parsing JWT payload: {e}"); return None

def try_fetch_jwt_from_browsers():
    if not browser_cookie3: print("LOG: browser_cookie3 not available..."); return None
    print("LOG: Attempting to fetch JWT from Firefox browser cookies...")
    all_found_jwts_info = []
    target_domain = "warframe.market"; browser_name = "Firefox" # Assuming Firefox primary
    loader_func = getattr(browser_cookie3, 'firefox', None)
    if not loader_func: print(f"LOG: Firefox cookie loader not found."); return None
    cj = None
    try:
        cj = loader_func(domain_name=target_domain)
        if cj is None: print(f"LOG: No cookies loaded or {browser_name} not detected/no cookies for '{target_domain}'.")
    except browser_cookie3.BrowserCookieError as bce: print(f"LOG: BrowserCookieError for {browser_name}: {bce}"); return None
    except PermissionError as pe: print(f"LOG: PermissionError accessing {browser_name} cookie path: {pe}"); return None
    except Exception as e: print(f"LOG: An unexpected error loading {browser_name} cookies: {e}"); return None

    if cj:
        for cookie in cj:
            if cookie.domain_specified and target_domain in cookie.domain and cookie.name == "JWT":
                payload = parse_jwt_payload(cookie.value); iat = payload.get("iat", 0) if payload else 0
                all_found_jwts_info.append({"jwt_value": cookie.value, "iat": iat, "source_browser": browser_name})
                print(f"LOG: Found JWT for '{target_domain}' in {browser_name}."); break # Found one, good enough for now
    if not all_found_jwts_info: print(f"LOG: No JWT cookie named 'JWT' for domain '{target_domain}' in {browser_name}."); return None

    all_found_jwts_info.sort(key=lambda x: x["iat"], reverse=True) # Get the most recent one if multiple
    selected_jwt_info = all_found_jwts_info[0]; latest_jwt_value = selected_jwt_info["jwt_value"]
    if selected_jwt_info["iat"] == 0: print(f"LOG: Selected JWT (from {selected_jwt_info['source_browser']}) has no parsable 'iat' claim. Using anyway.")
    else: print(f"LOG: Using JWT from {selected_jwt_info['source_browser']} (Issued At Timestamp: {selected_jwt_info['iat']}).")
    return latest_jwt_value

def load_config():
    global ITEM_USER_SETTINGS, DEVICE_ID, LOOP_DELAY_SECONDS, BUMP_THRESHOLD_CYCLES
    # Defaults are set globally, load_config overrides them if file exists and has keys
    try:
        # CONFIG_FILE is now globally defined at the top, pointing to AppData
        if not os.path.exists(CONFIG_FILE):
            print(f"LOG: {CONFIG_FILE_NAME} not found at {CONFIG_DIRECTORY}. Using defaults and will attempt to create it on save.")
            ITEM_USER_SETTINGS = {}; DEVICE_ID = None; # Reset to defaults if no file
            return {} # Return empty dict as no config was loaded

        with open(CONFIG_FILE, 'r') as f:
            config_data = json.load(f)
            print(f"LOG: Configuration loaded from {CONFIG_FILE}")

            # Migration for old "min_prices" structure if it exists
            old_min_prices = config_data.get("min_prices")
            if old_min_prices and "item_price_settings" not in config_data: # Check if new key is missing
                print("LOG: Migrating old 'min_prices' to new 'item_price_settings' format.")
                ITEM_USER_SETTINGS = {}
                for item_id, value in old_min_prices.items():
                    if isinstance(value, dict) and "min" in value and "skip" in value: # Old detailed structure
                        ITEM_USER_SETTINGS[item_id] = {"numeric_min": value["min"], "skipped": value["skip"]}
                    elif value == "skip": # Old simpler skip
                        ITEM_USER_SETTINGS[item_id] = {"numeric_min": None, "skipped": True}
                    elif isinstance(value, int): # Old simpler numeric min
                        ITEM_USER_SETTINGS[item_id] = {"numeric_min": value, "skipped": False}
                # We should remove the old "min_prices" key after migration if we save back
            else:
                ITEM_USER_SETTINGS = config_data.get("item_price_settings", {})

            DEVICE_ID = config_data.get("device_id") # Load or keep as None if not found
            LOOP_DELAY_SECONDS = config_data.get("loop_delay_seconds", LOOP_DELAY_SECONDS) # Use default if not in config
            BUMP_THRESHOLD_CYCLES = config_data.get("bump_threshold_cycles", BUMP_THRESHOLD_CYCLES) # Use default if not in config
            return config_data # Return all loaded data
    except FileNotFoundError: # Should be caught by os.path.exists above, but as a safeguard
        print(f"LOG: {CONFIG_FILE_NAME} not found (secondary check). Using defaults.");
        ITEM_USER_SETTINGS = {}; DEVICE_ID = None; return {}
    except json.JSONDecodeError:
        print(f"LOG: Error decoding {CONFIG_FILE_NAME} at {CONFIG_DIRECTORY}. File might be corrupted. Using defaults.");
        ITEM_USER_SETTINGS = {}; DEVICE_ID = None; return {}
    except Exception as e:
        print(f"LOG: Unexpected error loading config from {CONFIG_FILE}: {e}. Using defaults.");
        ITEM_USER_SETTINGS = {}; DEVICE_ID = None; return {}

def save_config(user_id_to_save): # user_id is now a parameter
    global ITEM_USER_SETTINGS, DEVICE_ID, LOOP_DELAY_SECONDS, BUMP_THRESHOLD_CYCLES
    
    if not CONFIG_DIRECTORY: # Check if a valid directory was established
        print(f"LOG: ERROR - Cannot save config, no valid configuration directory established (CONFIG_DIRECTORY is None).")
        return False

    try:
        # Ensure CONFIG_DIRECTORY exists one last time before writing
        if not os.path.exists(CONFIG_DIRECTORY):
            try:
                os.makedirs(CONFIG_DIRECTORY)
                print(f"LOG: Created configuration directory at {CONFIG_DIRECTORY} just before saving.")
            except OSError as e:
                print(f"LOG: ERROR - Failed to create configuration directory {CONFIG_DIRECTORY} on save: {e}")
                return False

        config_to_write = {
            "user_id": user_id_to_save, # Save the passed user_id
            "device_id": DEVICE_ID, # DEVICE_ID is global, managed by load_config or generated
            "loop_delay_seconds": LOOP_DELAY_SECONDS, # Global, might have been updated from default
            "bump_threshold_cycles": BUMP_THRESHOLD_CYCLES, # Global
            "item_price_settings": ITEM_USER_SETTINGS # Global
        }
        # Remove old "min_prices" key if it exists from a previous migration
        if "min_prices" in config_to_write:
            del config_to_write["min_prices"]

        with open(CONFIG_FILE, 'w') as f_write: # CONFIG_FILE is global, points to AppData
            json.dump(config_to_write, f_write, indent=4)
        print(f"LOG: Configuration saved to {CONFIG_FILE}"); return True
    except Exception as e: print(f"LOG: Unexpected error saving config to {CONFIG_FILE}: {e}"); return False

# ... (rest of your wfm_logic.py functions: prettify_slug, fetch_all_items_and_build_map_v2, etc. remain unchanged) ...
# Make sure they don't try to define CONFIG_FILE themselves.

def prettify_slug(slug_str):
    if not slug_str:
        return None
    return slug_str.replace('_', ' ').replace('-', ' ').title()

def fetch_all_items_and_build_map_v2(session_obj: requests.Session):
    global ITEM_ID_TO_DETAILS_MAP, ITEMS_MAP_FETCHED
    
    if ITEMS_MAP_FETCHED: 
        # print("LOG: Item map already fetched."); # Can be noisy, optional
        return True
        
    all_items_url = f"{API_V2_BASE_URL}/items"
    print(f"LOG: Fetching all item details from {all_items_url} (v2) for item map...")
    request_headers = {"Accept": "application/json", "User-Agent": session_obj.headers.get("User-Agent", "WFM_Logic_Module/1.0"), "Platform": PLATFORM, "Language": LANGUAGE}
    time.sleep(REQUEST_DELAY); response = None
    try:
        response = session_obj.get(all_items_url, headers=request_headers, timeout=60)
        response.raise_for_status(); items_response_data = response.json()
        
        items_list = []
        if isinstance(items_response_data, dict) and "data" in items_response_data:
            data_content = items_response_data.get("data")
            if isinstance(data_content, list):
                items_list = data_content
            else:
                print(f"LOG: Warning - 'data' field in /v2/items response is not a list. Raw 'data': {str(data_content)[:200]}")
        elif isinstance(items_response_data, list): 
            items_list = items_response_data
            print(f"LOG: Warning - /v2/items response was a direct list, not expected dict structure.")
        
        if not items_list: 
            print(f"LOG: Critical Warning - Could not extract items list from /v2/items response. Raw response: {str(items_response_data)[:500]}")
            return False 

        ITEM_ID_TO_DETAILS_MAP.clear()
        for item_details in items_list: 
            if not isinstance(item_details, dict): 
                print(f"LOG: Warning - Expected item_details dict, got {type(item_details)}. Value: {str(item_details)[:100]}"); 
                continue
            
            item_id = item_details.get("id")
            item_slug = item_details.get("slug")
            api_item_name = item_details.get("name") 
            icon_path = item_details.get("icon")
            
            i18n_data = item_details.get("i18n", {}).get(LANGUAGE, {})
            resolved_name = api_item_name 
            if isinstance(i18n_data, dict) and i18n_data.get("item_name"): 
                resolved_name = i18n_data.get("item_name")
            if not resolved_name and item_slug: resolved_name = prettify_slug(item_slug)
            
            final_name_for_map = resolved_name or f"ItemID_{item_id}" 
            mod_max_rank = item_details.get("maxRank") 
            
            if item_id:
                ITEM_ID_TO_DETAILS_MAP[item_id] = {
                    "name": final_name_for_map, 
                    "slug": item_slug, 
                    "icon": icon_path, 
                    "mod_max_rank": mod_max_rank 
                }
        
        ITEMS_MAP_FETCHED = True
        print(f"LOG: Item map built: {len(ITEM_ID_TO_DETAILS_MAP)} items."); 
        return True
        
    except requests.exceptions.RequestException as e: print(f"LOG: Request error in fetch_all_items_and_build_map_v2: {e}")
    except json.JSONDecodeError as e:
        resp_text = "N/A"; 
        if response is not None: 
            try: resp_text = response.text[:200]
            except Exception as ex_resp: resp_text = f"Error getting response text: {ex_resp}"
        else: resp_text = "Response object was None."
        print(f"LOG: JSON decode error in fetch_all_items_and_build_map_v2: {e} - Response text sample: {resp_text}")
    except Exception as e: print(f"LOG: Generic error in fetch_all_items_and_build_map_v2: {e}")
    
    ITEMS_MAP_FETCHED = False 
    return False

def fetch_v2_me_manual_jwt(session_obj: requests.Session, current_jwt: str, device_id_val: str = None, called_from_get_jwt=False):
    if not current_jwt: return None, True, None
    me_url = f"{API_V2_BASE_URL}/me"
    request_headers = {"Authorization": f"Bearer {current_jwt}", "Accept": "application/json", "User-Agent": session_obj.headers.get("User-Agent", "WFM_Logic_Module/1.0"), "Platform": PLATFORM, "Language": LANGUAGE}
    if device_id_val: request_headers["Device-Id"] = device_id_val
    if not called_from_get_jwt: time.sleep(REQUEST_DELAY)
    try:
        response = session_obj.get(me_url, headers=request_headers, timeout=10)
        if response.status_code == 401:
            if not called_from_get_jwt: print(f"LOG: {me_url} auth failed (401).");
            return None, True, None
        response.raise_for_status(); user_profile_response_envelope = response.json()
        profile_actual_data = user_profile_response_envelope.get("data")
        if not isinstance(profile_actual_data, dict):
            print(f"LOG: Error - 'data' field in /v2/me response is not a dict. Response: {user_profile_response_envelope}"); return None, False, None
        ingame_name = profile_actual_data.get("ingameName"); return profile_actual_data, False, ingame_name
    except requests.exceptions.HTTPError as http_err:
        is_auth_failure = http_err.response.status_code == 401
        if not called_from_get_jwt: print(f"LOG: HTTP error during {me_url} request: {http_err}");
        return None, is_auth_failure, None
    except requests.exceptions.RequestException as e:
        if not called_from_get_jwt: print(f"LOG: Request error during {me_url} request: {e}"); return None, False, None
    except Exception as e:
        if not called_from_get_jwt: print(f"LOG: Unexpected error during {me_url}: {e}"); return None, False, None
    return None, False, None

def fetch_orders_for_item_slug_v2(session_obj: requests.Session, item_slug: str):
    if not item_slug: print("LOG: item_slug is required for fetch_orders_for_item_slug_v2"); return []
    item_orders_url = f"{API_V2_BASE_URL}/orders/item/{item_slug}"
    request_headers = {"Accept": "application/json", "User-Agent": session_obj.headers.get("User-Agent", "WFM_Logic_Module/1.0"), "Platform": PLATFORM, "Language": LANGUAGE}
    time.sleep(REQUEST_DELAY); response = None
    try:
        response = session_obj.get(item_orders_url, headers=request_headers, timeout=15)
        response.raise_for_status(); response_data = response.json()
        orders = []
        if isinstance(response_data, dict) and "data" in response_data and isinstance(response_data["data"], list):
            orders = response_data["data"]
        elif isinstance(response_data, dict) and "payload" in response_data and isinstance(response_data["payload"], dict) and \
             "orders" in response_data["payload"] and isinstance(response_data["payload"]["orders"], list):
            orders = response_data["payload"]["orders"]
        else:
            print(f"LOG: Warning - Could not find 'data' list or 'payload.orders' list in /v2/orders/item/{item_slug}. Raw: {str(response_data)[:500]}")
        if not isinstance(orders, list):
            print(f"LOG: Critical Warning - 'orders' is not a list after parsing /v2/orders/item/{item_slug}. Type: {type(orders)}"); return []
        return orders
    except requests.exceptions.HTTPError as http_err: print(f"LOG: HTTP error in fetch_orders_for_item_slug_v2 ({item_slug}): {http_err}")
    except requests.exceptions.RequestException as e: print(f"LOG: Request error in fetch_orders_for_item_slug_v2 ({item_slug}): {e}")
    except json.JSONDecodeError: print(f"LOG: JSON decode error in fetch_orders_for_item_slug_v2 ({item_slug}). Response: {response.text[:200] if response else 'No response'}")
    except Exception as e: print(f"LOG: Unexpected error in fetch_orders_for_item_slug_v2 ({item_slug}): {e}")
    return []

def fetch_orders_from_profile_page(session_obj: requests.Session, ingame_name: str, current_jwt_for_cookie: str):
    global ITEM_ID_TO_DETAILS_MAP, ITEM_USER_SETTINGS # Uses these globals
    if not ingame_name: print("LOG: Error - In-game name required for profile page fetch."); return None, None
    if not current_jwt_for_cookie: print("LOG: Error - JWT required for profile page cookie."); return None, None
    profile_url = f"{PROFILE_BASE_URL}/{ingame_name}"
    original_cookies = session_obj.cookies.copy()
    session_obj.cookies.set("JWT", current_jwt_for_cookie, domain="warframe.market", path="/")
    request_headers = {"User-Agent": session_obj.headers.get("User-Agent", "WFM_Logic_Module/1.0"), "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.5", "Cache-Control": "no-cache", "Pragma": "no-cache"}
    time.sleep(REQUEST_DELAY); processed_orders_for_snapshot = []; user_status_from_profile_scrape = None
    try:
        response = session_obj.get(profile_url, headers=request_headers, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        script_tag = soup.find('script', {'id': 'application-state', 'type': 'application/json'})
        if not script_tag:
            print(f"LOG: Error - Could not find <script id='application-state'> in {profile_url}")
            return None, None
        app_state_json = json.loads(script_tag.string)
        current_user_data = app_state_json.get("currentUser")
        if isinstance(current_user_data, dict): user_status_from_profile_scrape = current_user_data.get("status")
        payload_data_from_page = app_state_json.get("payload", {})
        sell_orders_raw = payload_data_from_page.get("sell_orders", [])
        buy_orders_raw = payload_data_from_page.get("buy_orders", [])
        if not sell_orders_raw and not buy_orders_raw: # Fallback to another common structure
            profile_data_for_orders = app_state_json.get("profile", {})
            sell_orders_raw = profile_data_for_orders.get("sell", [])
            buy_orders_raw = profile_data_for_orders.get("buy", [])
        all_orders_raw = sell_orders_raw + buy_orders_raw
        for order_raw in all_orders_raw:
            item_data = order_raw.get("item", {})
            raw_item_id = item_data.get("id")
            if not raw_item_id:
                print(f"LOG: Warning - Order found without item ID in profile scrape: {order_raw}"); continue
            item_id_str = str(raw_item_id)

            item_name_from_order = item_data.get(LANGUAGE, {}).get("item_name")
            if not item_name_from_order and "item_name" in item_data: item_name_from_order = item_data.get("item_name") # Fallback
            item_slug = item_data.get("url_name"); item_icon_path = item_data.get("icon")
            mod_rank_from_order = order_raw.get("mod_rank") # Can be None or 0
            map_details = ITEM_ID_TO_DETAILS_MAP.get(item_id_str) # Get details from our global map
            
            resolved_item_name = item_name_from_order
            if not resolved_item_name and map_details:
                resolved_item_name = map_details.get("name")
            if not resolved_item_name: # Still no name
                 resolved_item_name = f"Item ID {item_id_str}" # Last resort

            # Prefer map name if it's more complete and order name isn't specific enough
            if map_details and map_details.get("name") and (not item_name_from_order or item_name_from_order == resolved_item_name):
                 if map_details.get("name") != f"ItemID_{item_id_str}": # Don't use placeholder map name
                    resolved_item_name = map_details.get("name")

            resolved_item_slug = item_slug
            if map_details and map_details.get("slug"): resolved_item_slug = map_details.get("slug")
            resolved_item_icon_path = item_icon_path
            if map_details and map_details.get("icon"): resolved_item_icon_path = map_details.get("icon")
            mod_max_rank_from_map = map_details.get("mod_max_rank") if map_details else None
            full_icon_url = None
            if resolved_item_icon_path:
                if resolved_item_icon_path.startswith("http"): full_icon_url = resolved_item_icon_path
                elif resolved_item_icon_path.startswith("/"): full_icon_url = f"{STATIC_ASSETS_BASE_URL}{resolved_item_icon_path.lstrip('/')}"
                else: full_icon_url = f"{STATIC_ASSETS_BASE_URL}{resolved_item_icon_path}"
            
            user_setting = ITEM_USER_SETTINGS.get(item_id_str, {"numeric_min": None, "skipped": False}) # Default if not in settings
            numeric_min = user_setting.get("numeric_min"); is_skipped = user_setting.get("skipped", False)
            order_for_ui = {"item_id": item_id_str, "item_name": resolved_item_name, "item_slug": resolved_item_slug, "order_id": order_raw.get("id"), "platinum": order_raw.get("platinum"), "quantity": order_raw.get("quantity"), "visible": order_raw.get("visible", False), "rank": mod_rank_from_order, "mod_max_rank": mod_max_rank_from_map, "type": order_raw.get("order_type"), "icon_url": full_icon_url, "numeric_min_price": numeric_min, "is_skipped": is_skipped}
            processed_orders_for_snapshot.append(order_for_ui)
        return processed_orders_for_snapshot, user_status_from_profile_scrape
    except requests.exceptions.RequestException as e: print(f"LOG: Request error fetching profile page {profile_url}: {e}")
    except json.JSONDecodeError as e: print(f"LOG: Error decoding JSON from application-state in {profile_url}.")
    except Exception as e: print(f"LOG: Unexpected error in fetch_orders_from_profile_page: {e}")
    finally: session_obj.cookies = original_cookies
    return None, None

def update_order_via_v1_put(req_session: requests.Session, order_id_to_update: str, new_price: int, new_quantity: int, new_visibility: bool, current_rank,
                            jwt_token: str, csrf_token_val: str, device_id_val: str = None):
    if not all([order_id_to_update, jwt_token, csrf_token_val]):
        print("LOG: Error - Missing order_id, JWT, or CSRF for v1 PUT."); return False, "Missing auth details for WFM API update."
    if new_quantity < 0:
        warning_msg = f"Attempted to set quantity to {new_quantity} for order {order_id_to_update}. API requires non-negative. Clamping to 0."
        print(f"LOG: {warning_msg}"); new_quantity = 0
    
    order_id_str = str(order_id_to_update).strip()
    update_url = f"{API_V1_BASE_URL}/profile/orders/{order_id_str}"
    original_cookies = req_session.cookies.copy()
    req_session.cookies.set("JWT", jwt_token, domain="warframe.market", path="/")
    request_headers = {"Authorization": f"Bearer {jwt_token}", "X-CSRFToken": csrf_token_val, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": req_session.headers.get("User-Agent", "WFM_Logic_Module/1.0"), "Platform": PLATFORM, "Language": LANGUAGE, "Origin": "https://warframe.market", "Referer": f"{PROFILE_BASE_URL}/"}
    if device_id_val: request_headers["Device-Id"] = device_id_val

    payload = {"order_id": order_id_str, "platinum": new_price, "quantity": new_quantity, "visible": new_visibility}
    if current_rank is not None: payload["rank"] = current_rank # Only include if not None

    time.sleep(REQUEST_DELAY)
    try:
        response = req_session.put(update_url, headers=request_headers, json=payload, timeout=20)
        response.raise_for_status()
        return True, "Order updated successfully on Warframe.Market."
    except requests.exceptions.HTTPError as http_err:
        error_message = f"WFM API Error ({http_err.response.status_code}) for order {order_id_str}."
        try:
            err_payload = http_err.response.json()
            error_detail = err_payload.get('error', http_err.response.text[:150])
            error_message += f" Detail: {error_detail}"
        except json.JSONDecodeError: error_message += f" Raw Response: {http_err.response.text[:150]}"
        except Exception: pass
        print(f"LOG: {error_message}")
        return False, error_message
    except requests.exceptions.RequestException as req_err:
        error_message = f"Network error updating order {order_id_str}: {req_err}"
        print(f"LOG: {error_message}")
        return False, error_message
    except Exception as e:
        error_message = f"Unexpected error updating order {order_id_str}: {e}"
        print(f"LOG: {error_message}")
        return False, error_message
    finally:
        req_session.cookies = original_cookies

def check_min_price_set_for_item(item_id_str: str):
    global ITEM_USER_SETTINGS # Uses this global
    settings = ITEM_USER_SETTINGS.get(str(item_id_str)) # Ensure string key
    if settings:
        if settings.get("skipped", False): return "skip"
        numeric_min = settings.get("numeric_min")
        if isinstance(numeric_min, int) and numeric_min > 0: return numeric_min
    return None # No valid setting or not skipped

def fetch_current_user_status(session_obj: requests.Session, user_ingame_name: str, current_jwt: str, user_id: str):
    if not all([session_obj, user_ingame_name, current_jwt, user_id]):
        print("LOG (fetch_current_user_status): Missing parameters.")
        return "Invisible" # Default/fallback

    # Try /v2/me first as it's most direct
    me_profile_data, _, _ = fetch_v2_me_manual_jwt(session_obj, current_jwt, DEVICE_ID) # DEVICE_ID is global
    if me_profile_data and isinstance(me_profile_data, dict):
        status_from_me = me_profile_data.get("status")
        if status_from_me == "ingame": return "Online In Game"
        if status_from_me == "online": return "Online"
        # if 'invisible', continue to other methods

    # Try profile page scrape (might be more up-to-date than /v2/me if user just changed status on website)
    _, status_from_profile_scrape = fetch_orders_from_profile_page(session_obj, user_ingame_name, current_jwt)
    if status_from_profile_scrape:
        if status_from_profile_scrape == "ingame": return "Online In Game"
        if status_from_profile_scrape == "online": return "Online"
        # if 'invisible', continue

    # Fallback: check status from one of their own item listings
    all_orders_snapshot_data, _ = fetch_orders_from_profile_page(session_obj, user_ingame_name, current_jwt)
    if all_orders_snapshot_data:
        visible_sell_orders = [o for o in all_orders_snapshot_data if o.get("type") == "sell" and o.get("visible") and o.get("item_slug")]
        
        status_from_item_lookup = None
        for order_to_check in visible_sell_orders[:2]: # Check first few visible sell orders
            item_slug_to_try = order_to_check.get("item_slug")
            item_orders_api = fetch_orders_for_item_slug_v2(session_obj, item_slug_to_try)
            if item_orders_api:
                for order in item_orders_api:
                    order_user = order.get("user", {})
                    if order_user.get("id") == user_id: # Found one of our orders
                        status_from_item_lookup = order_user.get("status")
                        if status_from_item_lookup == "ingame": return "Online In Game"
                        if status_from_item_lookup == "online": return "Online"
                        break # Found our status from this item's listing
                if status_from_item_lookup in ["ingame", "online"]: # If definitive status found
                    break # No need to check other items
    
    return "Invisible" # Default if all other methods fail or return 'invisible'


def perform_analysis_and_update_cycle_core(
        req_session: requests.Session, current_user_id: str, user_ingame_name: str,
        jwt_token: str, csrf_token_val: str, device_id_val: str,
        update_callback=None):
    global ITEM_USER_SETTINGS, ITEM_ID_TO_DETAILS_MAP, PLATFORM, BUMP_THRESHOLD_CYCLES, ITEM_BUMP_ELIGIBILITY_CYCLES, REQUEST_DELAY, stop_processing_flag, LOOP_DELAY_SECONDS # Added LOOP_DELAY_SECONDS

    def _send_update(item_id_for_log, message_content, data_payload=None, msg_type="info"):
        current_data_for_callback = data_payload if data_payload is not None else {}
        # Ensure 'type' key is always present in data_payload for consistency in JS handler
        current_data_for_callback['type'] = msg_type # This now correctly sets the type in data_payload
        if update_callback:
            try: update_callback(item_id_for_log, message_content, current_data_for_callback)
            except Exception as cb_ex: print(f"WFM_LOGIC_ERROR: Error in update_callback: {cb_ex}")

    _send_update(None, f"--- Starting Analysis Cycle ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---", msg_type="info")

    if not all([jwt_token, user_ingame_name, csrf_token_val]):
        _send_update(None, "Cycle skipped: Missing Auth Details.", msg_type="error"); return False

    all_orders_snapshot_data, _ = fetch_orders_from_profile_page(req_session, user_ingame_name, jwt_token)
    if all_orders_snapshot_data is None:
        _send_update(None, "Error: Failed to fetch orders for current cycle snapshot.", msg_type="error"); return False

    current_sell_orders_for_ui = [order for order in all_orders_snapshot_data if order.get("type") == "sell"]
    _send_update(None, f"Refreshed orders snapshot ({len(current_sell_orders_for_ui)} sell items).",
                 data_payload={'orders': current_sell_orders_for_ui}, msg_type="orders_data_snapshot") # Type in data_payload

    if not current_sell_orders_for_ui:
        _send_update(None, "No sell orders to analyze in current cycle (after snapshot).", msg_type="info"); return True

    active_sell_orders_to_process = [order for order in current_sell_orders_for_ui if order.get("visible")]
    active_sell_orders_to_process.sort(key=lambda x: x.get("item_name", "").lower()) # Sort for consistent processing order

    if not active_sell_orders_to_process:
        _send_update(None, "No VISIBLE 'sell' orders to process.", msg_type="info"); return True

    _send_update(None, f"--- Analyzing {len(active_sell_orders_to_process)} VISIBLE SELL Orders (Sorted Alphabetically) ---", msg_type="info")
    
    updated_listings_count = 0; bumped_listings_count = 0
    for order_idx, order in enumerate(active_sell_orders_to_process):
        if stop_processing_flag: _send_update(None, "Processing stopped by flag.", msg_type="warn"); return True # Check flag before each item
        
        str_item_id = order.get("item_id"); name = order.get("item_name", f"Item ID {str_item_id}"); slug = order.get("item_slug"); api_price = order.get("platinum"); order_id_val = order.get("order_id"); qty = order.get("quantity"); visible_status = order.get("visible"); rank = order.get("rank")

        _send_update(str_item_id, f"Analyzing: {name} (Price: {api_price}p, Qty: {qty})", data_payload={"current_price": api_price, "qty": qty, "rank": rank}, msg_type="detail")
        if not all([str_item_id, name and not name.startswith("Item ID"), api_price is not None, order_id_val, qty is not None]): # Check for resolved name
            _send_update(str_item_id, f"Error: Incomplete or unresolved order data for '{name}'. Skipping.", data_payload={}, msg_type="error"); continue
        if not slug:
            _send_update(str_item_id, f"Error: Missing slug for '{name}'. Cannot fetch competitors. Skipping analysis.", data_payload={}, msg_type="error"); ITEM_BUMP_ELIGIBILITY_CYCLES[str_item_id] = 0; continue
        
        user_min_or_skip_status = check_min_price_set_for_item(str_item_id) # Uses global ITEM_USER_SETTINGS
        if user_min_or_skip_status == "skip":
            _send_update(str_item_id, f"Skipped (user config): {name}", data_payload={"min_price_setting": "skip"}, msg_type="info"); ITEM_BUMP_ELIGIBILITY_CYCLES[str_item_id] = 0; continue
        if user_min_or_skip_status is None: # No valid numeric min set
            _send_update(str_item_id, f"Action Required: Set Minimum Price for {name}", data_payload={"min_price_setting": None}, msg_type="warn"); ITEM_BUMP_ELIGIBILITY_CYCLES[str_item_id] = 0; continue
        
        user_min = user_min_or_skip_status # This is now the numeric min price
        _send_update(str_item_id, f"Fetching competitors for {name}...", data_payload={"min_price": user_min}, msg_type="detail")
        
        competitors = fetch_orders_for_item_slug_v2(req_session, slug) # API call
        if not competitors: # Includes error cases from fetch_orders_for_item_slug_v2
            _send_update(str_item_id, f"No/Error fetching competitors for '{name}'.", data_payload={"competitor_count": 0, "competitor_price": "N/A"}, msg_type="warn"); ITEM_BUMP_ELIGIBILITY_CYCLES[str_item_id] = 0; continue
        
        lowest_comp_price = float('inf'); ingame_sellers = 0
        for comp_order in competitors:
            comp_user = comp_order.get("user", {})
            if not isinstance(comp_user, dict): continue # Skip malformed user data
            if comp_user.get("platform") == PLATFORM and comp_order.get("type") == "sell" and comp_user.get("id") != current_user_id and comp_user.get("status") == "ingame":
                price_val = comp_order.get("platinum")
                if isinstance(price_val, (int, float)) and price_val > 0: lowest_comp_price = min(lowest_comp_price, price_val); ingame_sellers +=1
        
        _send_update(str_item_id, f"Found {ingame_sellers} other 'in-game' PC sellers for '{name}'. Lowest price: {lowest_comp_price if lowest_comp_price != float('inf') else 'N/A'}.", data_payload={"competitor_count": ingame_sellers, "competitor_price": lowest_comp_price if lowest_comp_price != float('inf') else "N/A"}, msg_type="detail")

        if not ingame_sellers or lowest_comp_price == float('inf'): # No valid competitors
            _send_update(str_item_id, f"No valid competitor prices found for '{name}'. Cannot determine optimal price.", data_payload={"competitor_price": "N/A"}, msg_type="info"); ITEM_BUMP_ELIGIBILITY_CYCLES[str_item_id] = 0; continue
        
        target_p = max(int(lowest_comp_price - 1), int(user_min)) # Undercut by 1p, but not below user_min
        _send_update(str_item_id, f"{name}: Lowest comp: {lowest_comp_price}p. Your min: {user_min}p. Target: {target_p}p. Current: {api_price}p.", data_payload={"competitor_price": lowest_comp_price, "target_price": target_p, "current_price": api_price, "min_price": user_min}, msg_type="detail")
        
        if target_p == api_price: # Price is optimal
            _send_update(str_item_id, f"Price is optimal for {name} at {api_price}p.", data_payload={"current_price": api_price, "target_price": target_p}, msg_type="success")
            current_bump_cycle = ITEM_BUMP_ELIGIBILITY_CYCLES.get(str_item_id, 0)
            is_undercut_by_others = api_price > lowest_comp_price # If our optimal price is higher than someone else's lowest
            
            if not is_undercut_by_others: # We are not being undercut (or we are the lowest)
                current_bump_cycle += 1; ITEM_BUMP_ELIGIBILITY_CYCLES[str_item_id] = current_bump_cycle
                _send_update(str_item_id, f"Bump Candidate ({name}): Cycle {current_bump_cycle}/{BUMP_THRESHOLD_CYCLES}", data_payload={"bump_cycle": current_bump_cycle}, msg_type="info")
                if current_bump_cycle >= BUMP_THRESHOLD_CYCLES:
                    _send_update(str_item_id, f"Attempting BUMP for '{name}' at {api_price}p.", data_payload={"price": api_price}, msg_type="info")
                    update_success, _ = update_order_via_v1_put(req_session, order_id_val, api_price, qty, visible_status, rank, jwt_token, csrf_token_val, device_id_val)
                    if update_success:
                        bumped_listings_count += 1; ITEM_BUMP_ELIGIBILITY_CYCLES[str_item_id] = 0 # Reset cycle count on successful bump
                        _send_update(str_item_id, f"Listing BUMPED: {name}!", data_payload={"price": api_price, "outcome": "success"}, msg_type="success")
                    else: _send_update(str_item_id, f"Bump FAILED for {name}.", data_payload={"price": api_price, "outcome": "failure"}, msg_type="error") # Bump failure doesn't reset cycle count, will retry next time
            else: # We are being undercut, so reset bump eligibility
                ITEM_BUMP_ELIGIBILITY_CYCLES[str_item_id] = 0
                _send_update(str_item_id, f"Not bump candidate ({name}): currently undercut by other sellers at {lowest_comp_price}p.", data_payload={"current_price": api_price, "lowest_competitor": lowest_comp_price}, msg_type="detail")
        else: # Price needs adjustment
            ITEM_BUMP_ELIGIBILITY_CYCLES[str_item_id] = 0 # Reset bump cycle if price changes
            _send_update(str_item_id, f"Updating price for '{name}' from {api_price}p to {target_p}p.", data_payload={"old_price": api_price, "new_price": target_p}, msg_type="info")
            update_success, _ = update_order_via_v1_put(req_session, order_id_val, target_p, qty, visible_status, rank, jwt_token, csrf_token_val, device_id_val)
            if update_success:
                updated_listings_count += 1
                _send_update(str_item_id, f"Price Updated: {name} to {target_p}p!", data_payload={"price": target_p, "outcome": "success"}, msg_type="success")
            else: _send_update(str_item_id, f"Price Update FAILED for {name}.", data_payload={"target_price": target_p, "outcome": "failure"}, msg_type="error")
    
    _send_update(None, f"--- Cycle Summary --- Adjusted: {updated_listings_count}, Bumped: {bumped_listings_count}", msg_type="info")
    return True

def analysis_thread_target(req_session_obj, user_id, ingame_name, jwt, csrf, device_id, initial_user_settings, update_callback=None):
    global stop_processing_flag, ITEM_USER_SETTINGS, ITEM_BUMP_ELIGIBILITY_CYCLES, main_session, LOOP_DELAY_SECONDS # Ensure LOOP_DELAY_SECONDS is global

    def _send_thread_update(item_id_for_log, message_content, data_payload=None, msg_type="info"):
        current_data_for_callback = data_payload if data_payload is not None else {}
        current_data_for_callback['type'] = msg_type # Ensure type is in the data payload for JS
        if update_callback:
            try:
                update_callback(item_id_for_log, message_content, current_data_for_callback)
            except Exception as cb_ex:
                print(f"WFM_LOGIC_ERROR: Error in update_callback from analysis_thread: {cb_ex}")

    _send_thread_update(None, f"Analysis thread started for user {ingame_name}.", msg_type="info")
    stop_processing_flag = False # Reset flag at start of thread
    ITEM_USER_SETTINGS = initial_user_settings.copy() # Use the copy passed at thread start
    ITEM_BUMP_ELIGIBILITY_CYCLES = {} # Reset bump cycles at start of thread

    if not main_session and not req_session_obj : # Check if a session object is available
        _send_thread_update(None, "CRITICAL - No session object available for analysis thread.", msg_type="error")
        return
    current_session_for_calls = req_session_obj if req_session_obj else main_session


    cycle_count = 0
    while not stop_processing_flag:
        cycle_count += 1
        # Ensure LOOP_DELAY_SECONDS is current (could be changed by config reload if we implement that)
        current_loop_delay = LOOP_DELAY_SECONDS # Use the global value

        perform_analysis_and_update_cycle_core(
            current_session_for_calls, user_id, ingame_name, jwt, csrf, device_id,
            update_callback=update_callback
        )

        if stop_processing_flag: # Check flag immediately after core cycle
            _send_thread_update(None, "Stop flag detected after core cycle. Terminating loop.", msg_type="warn")
            break
        
        _send_thread_update(None, f"Cycle finished. Waiting {current_loop_delay} seconds (with status check)...", msg_type="info")
        
        # Fetch and emit user status during the delay period
        current_status = fetch_current_user_status(current_session_for_calls, ingame_name, jwt, user_id)
        _send_thread_update(None, f"Status update: {current_status}",
                            data_payload={"new_status": current_status}, msg_type="user_status_update") # Set type for JS

        # Wait for LOOP_DELAY_SECONDS, but check stop_processing_flag periodically
        wait_start_time = time.time()
        while time.time() - wait_start_time < current_loop_delay:
            if stop_processing_flag:
                break # Break inner wait loop if flag is set
            time.sleep(0.2) # Sleep in small intervals to be responsive to the flag
        
        if stop_processing_flag: # Check flag again after wait loop
             _send_thread_update(None, "Stop flag detected during cooldown. Terminating loop.", msg_type="warn")
             break

    _send_thread_update(None, f"Analysis thread for {ingame_name} received stop signal and is terminating.", msg_type="warn")
    stop_processing_flag = False # Reset for future starts, though thread instance will be new

def delete_order_v2(session_obj: requests.Session, order_id: str, jwt_token: str, csrf_token_val: str, device_id_val: str = None):
    if not all([order_id, jwt_token, csrf_token_val]):
        error_msg = "Error: Missing order_id, JWT, or CSRF for v2 DELETE order."
        print(f"LOG: {error_msg}")
        return False, error_msg

    delete_url = f"{API_V2_BASE_URL}/orders/{str(order_id).strip()}"
    
    original_cookies = session_obj.cookies.copy()
    session_obj.cookies.set("JWT", jwt_token, domain="warframe.market", path="/")

    request_headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/json",
        "X-CSRFToken": csrf_token_val,
        "User-Agent": session_obj.headers.get("User-Agent", "WFM_Logic_Module/1.0"),
        "Platform": PLATFORM,
        "Language": LANGUAGE,
        "Origin": "https://warframe.market",
        "Referer": f"{PROFILE_BASE_URL}/" # Typical referer
    }
    if device_id_val:
        request_headers["Device-Id"] = device_id_val

    print(f"LOG: Attempting to DELETE order {order_id} at {delete_url}")
    time.sleep(REQUEST_DELAY) # Respect rate limits

    try:
        response = session_obj.delete(delete_url, headers=request_headers, timeout=20)
        response.raise_for_status() # Will raise HTTPError for 4xx/5xx responses

        # Successful deletion usually returns 200 or 204 (No Content)
        if 200 <= response.status_code < 300 : # Check for any 2xx success status
            print(f"LOG: Order {order_id} deleted successfully. Status: {response.status_code}")
            return True, f"Order {order_id} deleted successfully from Warframe.Market."
        else:
            # This case might be rare if raise_for_status() is used, but as a fallback
            error_message = f"Unexpected status code {response.status_code} deleting order {order_id}."
            try: error_message += f" Response: {response.text[:250]}"
            except Exception: pass
            print(f"LOG: {error_message}")
            return False, error_message

    except requests.exceptions.HTTPError as http_err:
        error_message = f"WFM API Error ({http_err.response.status_code}) deleting order {order_id}."
        try:
            err_payload = http_err.response.json() # Try to parse JSON error response
            error_detail = err_payload.get('error', http_err.response.text[:150]) # Get specific error message
            error_message += f" Detail: {error_detail}"
        except json.JSONDecodeError: error_message += f" Raw Response: {http_err.response.text[:150]}" # If not JSON
        except Exception: pass # Catch any other error during error parsing
        print(f"LOG: {error_message}")
        return False, error_message
    except requests.exceptions.RequestException as req_err: # Network errors, DNS, timeout, etc.
        error_message = f"Network error deleting order {order_id}: {req_err}"
        print(f"LOG: {error_message}")
        return False, error_message
    except Exception as e: # Catch-all for any other unexpected errors
        error_message = f"Unexpected error deleting order {order_id}: {e}"
        print(f"LOG: {error_message}")
        return False, error_message
    finally:
        session_obj.cookies = original_cookies # Restore original cookies

def place_new_sell_order_v1(req_session: requests.Session, item_id_to_list: str, price: int, quantity: int, rank: int,
                               jwt_token: str, csrf_token_val: str, device_id_val: str = None):
    if not all([item_id_to_list, isinstance(price, int), price > 0,
                isinstance(quantity, int), quantity > 0,
                isinstance(rank, int), rank >= 0, # Rank can be 0
                jwt_token, csrf_token_val]):
        missing = [
            arg_name for arg_name, arg_val in [
                ("item_id", item_id_to_list), ("price", price), ("quantity", quantity),
                ("rank", rank), ("jwt_token", jwt_token), ("csrf_token", csrf_token_val)
            ] if not arg_val and not isinstance(arg_val, int) # Check for falsy values (excluding 0 for rank/price/qty if allowed)
        ]
        validation_msg = f"Invalid parameters for placing new order. Check: {', '.join(missing)}. Price/Qty must be >0, Rank >=0."
        if not isinstance(price, int) or price <=0: validation_msg += " Invalid price."
        if not isinstance(quantity, int) or quantity <=0: validation_msg += " Invalid quantity."
        if not isinstance(rank, int) or rank <0: validation_msg += " Invalid rank."

        print(f"LOG: {validation_msg}")
        return False, validation_msg, None # Return None for listed_item_id on failure

    place_order_url = f"{API_V1_BASE_URL}/profile/orders"
    
    original_cookies = req_session.cookies.copy()
    req_session.cookies.set("JWT", jwt_token, domain="warframe.market", path="/")

    request_headers = {
        "Authorization": f"Bearer {jwt_token}",
        "X-CSRFToken": csrf_token_val,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": req_session.headers.get("User-Agent", "WFM_Logic_Module/1.0"),
        "Platform": PLATFORM,
        "Language": LANGUAGE,
        "Origin": "https://warframe.market",
        "Referer": f"{PROFILE_BASE_URL}/" # Typical referer
    }
    if device_id_val:
        request_headers["Device-Id"] = device_id_val

    payload = {
        "item_id": item_id_to_list,
        "order_type": "sell",
        "platinum": price,
        "quantity": quantity,
        "visible": True, # New orders are typically visible by default
    }
    if rank is not None: # Only include rank if it's provided (could be 0 for unranked)
        payload["rank"] = rank
    
    print(f"LOG: Attempting to POST new sell order to {place_order_url} with payload: {payload}")
    time.sleep(REQUEST_DELAY) # Respect rate limits

    try:
        response = req_session.post(place_order_url, headers=request_headers, json=payload, timeout=20)
        response.raise_for_status() # Will raise HTTPError for 4xx/5xx responses
        
        # Successful order placement usually returns 200 with the order details,
        # or sometimes 201 Created.
        print(f"LOG: New order for item ID {item_id_to_list} placed successfully. Status: {response.status_code}")
        # The V1 API for placing orders doesn't typically return the full new order ID in a simple way,
        # it usually just confirms success. The item_id_to_list is what we used.
        return True, "New order placed successfully on Warframe.Market.", item_id_to_list # Return the item_id used
        
    except requests.exceptions.HTTPError as http_err:
        error_message = f"WFM API Error ({http_err.response.status_code}) placing new order for item ID {item_id_to_list}."
        try:
            err_payload = http_err.response.json()
            error_detail = err_payload.get('error', str(err_payload)) # str(err_payload) if 'error' key is missing
            error_message += f" Detail: {error_detail}"
        except json.JSONDecodeError: # If error response is not JSON
            error_message += f" Raw Response: {http_err.response.text[:250]}"
        except Exception: # Catch any other error during error parsing
            pass
        print(f"LOG: {error_message}")
        return False, error_message, None
    except requests.exceptions.RequestException as req_err: # Network errors, DNS, timeout, etc.
        error_message = f"Network error placing new order for item ID {item_id_to_list}: {req_err}"
        print(f"LOG: {error_message}")
        return False, error_message, None
    except Exception as e: # Catch-all for any other unexpected errors
        error_message = f"Unexpected error placing new order for item ID {item_id_to_list}: {e}"
        print(f"LOG: {error_message}")
        return False, error_message, None
    finally:
        req_session.cookies = original_cookies # Restore original cookies

print("wfm_logic.py (AppData config, improved defaults, safer saves) loaded.")