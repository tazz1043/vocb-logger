import urllib3
import requests
from FlightRadar24 import FlightRadar24API
from datetime import datetime, timezone
import time
import openpyxl
import os
import math
import threading
from flask import Flask, send_file, render_template_string

# --- AAI FIREWALL / SSL BYPASS ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
old_request = requests.Session.request
def new_request(self, method, url, **kwargs):
    kwargs['verify'] = False
    return old_request(self, method, url, **kwargs)
requests.Session.request = new_request

app = Flask(__name__)
fr_api = FlightRadar24API()
excel_lock = threading.Lock()

EXCEL_FILE = "VOCB_Final_V6.xlsx"
RETENTION_DAYS = 4
logged_movements = set()
last_prune_date = None

CCB_LAT = 11.03
CCB_LON = 77.04
VOCB_GROUND_ALT_MAX = 1600 
VOCB_BOUNDARY_NM = 1.5 
TRACKING_RADIUS_NM = 30.0 

# ==============================================================================
# --- DAILY FLIGHT PLAN DATABASE (THE MINI-FDPS) ---
# Format: "SQUAWK": {"callsign": "CALL", "reg": "VT-XXX", "origin": "ICAO", "dest": "ICAO"}
# ==============================================================================
DAILY_FLIGHT_PLANS = {
    "4021": {"callsign": "VTSRC", "reg": "VT-SRC", "origin": "VABB", "dest": "VOCB"}
}

AIRPORT_MAP = {
    # Indian Domestic
    'DEL': 'VIDP', 'BOM': 'VABB', 'BLR': 'VOBL', 'HYD': 'VOHS',
    'MAA': 'VOMM', 'CCU': 'VECC', 'CJB': 'VOCB', 'COK': 'VOCI',
    'TRV': 'VOTV', 'PNQ': 'VAPO', 'AMD': 'VAAH', 'GOI': 'VOGO',
    'GOX': 'VOGA', 'LKO': 'VILK', 'GAU': 'VEGT', 'BBI': 'VEBS',
    'IXC': 'VICG', 'IXM': 'VOMD', 'TRZ': 'VOTR', 'TCR': 'VOTK',
    'CNN': 'VOCF', 'IXE': 'VOML', 'CCJ': 'VOCL', 'VTZ': 'VOVZ',
    'VGA': 'VOBZ', 'TIR': 'VOTP', 'RJA': 'VORY', 'IXB': 'VEBD',
    'PAT': 'VEPT', 'IXR': 'VERC', 'ATQ': 'VIAR', 'SXR': 'VISR',
    'JAI': 'VIJP', 'JOD': 'VIJO', 'UDR': 'VAUD', 'IDR': 'VAID',
    'BHO': 'VABP', 'NAG': 'VANP', 'STV': 'VASU', 'IXA': 'VEAT',
    'IMF': 'VEIM', 'SHL': 'VEKU', 'DMU': 'VEMR', 'HBX': 'VAHB',
    'NMI': 'VANM', 
    
    # Middle East & Gulf
    'DXB': 'OMDB', 'SHJ': 'OMSJ', 'AUH': 'OMAA', 'MCT': 'OOMS',
    'DOH': 'OTHH', 'KWI': 'OKBK', 'BAH': 'OBBI', 'RUH': 'OERK',
    'JED': 'OEJN', 'DMM': 'OEDF', 
    
    # Southeast Asia & Far East
    'SIN': 'WSSS', 'KUL': 'WMKK', 'BKK': 'VTBS', 'DMK': 'VTBD', 
    'HKG': 'VHHH', 'CMB': 'VCBI', 'DAC': 'VGHS', 'KTM': 'VNKT', 
    'MLE': 'VRMM', 'PHKT': 'VTSP', 'HAN': 'VVNB', 'SGN': 'VVTS', 
    
    # Europe & US
    'LHR': 'EGLL', 'FRA': 'EDDF', 'CDG': 'LFPG', 'AMS': 'EHAM', 
    'JFK': 'KJFK', 'EWR': 'KEWR', 'SFO': 'KSFO', 'ORD': 'KORD'
}

# --- VOCB APRON GEOFENCES ---
VOCB_BAYS = {
    "Bay 9": (11.02960, 77.03930), 
    "Bay 1": (11.02995, 77.03965), 
    "Bay 2": (11.03030, 77.04000), 
    "Bay 3": (11.03060, 77.04030), 
    "Bay 4": (11.03090, 77.04060), 
    "Bay 5": (11.03120, 77.04090),
    "Bay 6": (11.03170, 77.04170),
    "Bay 7": (11.03140, 77.04190), 
    "Bay 11": (11.03230, 77.04260) 
}

active_targets = {}

def get_distance(lat1, lon1, lat2, lon2):
    R = 3440.065 
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))

def get_radial(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    initial_bearing = math.atan2(x, y)
    return int((math.degrees(initial_bearing) + 360) % 360)

def get_runway(heading):
    if heading is None: return "N/A"
    if heading >= 320 or heading < 140:
        return "RWY 05"
    else:
        return "RWY 23"

def detect_bay(lat, lon):
    closest_bay = None
    min_dist = 0.035 
    for bay_name, (b_lat, b_lon) in VOCB_BAYS.items():
        dist = get_distance(lat, lon, b_lat, b_lon)
        if dist < min_dist:
            min_dist = dist
            closest_bay = bay_name
    return closest_bay

def clean_str(val): return str(val) if val else "N/A"

def init_excel():
    with excel_lock:
        if not os.path.exists(EXCEL_FILE):
            wb = openpyxl.Workbook()
            wb.remove(wb.active)
            ws_arr = wb.create_sheet('VOCB Arrivals')
            ws_arr.append(['Date(Z)', 'Callsign', 'Reg', 'Origin', 'ATA(Z)', 'Runway', 'Bay'])
            ws_dep = wb.create_sheet('VOCB Departures')
            ws_dep.append(['Date(Z)', 'Callsign', 'Reg', 'Dest', 'ATD(Z)', 'Runway', 'Bay'])
            ws_ovf = wb.create_sheet('Overflights')
            ws_ovf.append(['Date(Z)', 'Callsign', 'Reg', 'Origin', 'Dest', 'Entry(Z)', 'Exit(Z)'])
            ws_misc = wb.create_sheet('Misc Movements')
            ws_misc.append(['Date(Z)', 'Callsign', 'Reg', 'Origin', 'Dest', 'ATD(Z)', 'ATA(Z)', 'Pos/Bay'])
            wb.save(EXCEL_FILE)

def prune_old_logs():
    """Deletes rows older than RETENTION_DAYS (4 days) to keep the Excel file clean."""
    with excel_lock:
        if not os.path.exists(EXCEL_FILE):
            return
        try:
            wb = openpyxl.load_workbook(EXCEL_FILE)
            now_utc = datetime.now(timezone.utc)
            
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                # Iterate backwards from bottom row to row 2 so deleting rows doesn't shift indices
                for row_idx in range(ws.max_row, 1, -1):
                    date_val = clean_str(ws.cell(row=row_idx, column=1).value)
                    try:
                        row_date = datetime.strptime(date_val, '%d/%b/%Y').replace(tzinfo=timezone.utc)
                        if (now_utc - row_date).days > RETENTION_DAYS:
                            ws.delete_rows(row_idx, 1)
                    except ValueError:
                        pass
            wb.save(EXCEL_FILE)
        except Exception:
            pass

def load_existing_logs():
    init_excel()
    prune_old_logs()
    with excel_lock:
        try:
            wb = openpyxl.load_workbook(EXCEL_FILE)
            logged_movements.clear()
            ws = wb['VOCB Arrivals']
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[1]: logged_movements.add(f"Arr_{clean_str(row[0])}_{clean_str(row[1])}_{clean_str(row[4])}")
            ws = wb['VOCB Departures']
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[1]: logged_movements.add(f"Dep_{clean_str(row[0])}_{clean_str(row[1])}_{clean_str(row[4])}")
            ws = wb['Overflights']
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[1]: logged_movements.add(f"Ovf_{clean_str(row[0])}_{clean_str(row[1])}_{clean_str(row[5])}")
            ws = wb['Misc Movements']
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[1]: logged_movements.add(f"Misc_{clean_str(row[0])}_{clean_str(row[1])}_{clean_str(row[5])}_{clean_str(row[6])}")
        except Exception:
            pass

def log_to_excel(sheet_name, unique_id, row_data):
    if unique_id in logged_movements: return unique_id
    with excel_lock:
        try:
            wb = openpyxl.load_workbook(EXCEL_FILE)
            ws = wb[sheet_name]
            ws.append(row_data)
            wb.save(EXCEL_FILE)
            logged_movements.add(unique_id)
            return unique_id
        except Exception:
            return None

def update_excel_post_arrival(unique_id, bay_name):
    if not unique_id: return
    with excel_lock:
        try:
            wb = openpyxl.load_workbook(EXCEL_FILE)
            ws = wb['VOCB Arrivals']
            for row_idx in range(ws.max_row, 1, -1):
                row = ws[row_idx]
                d_arr = clean_str(row[0].value)
                c_arr = clean_str(row[1].value)
                if c_arr != "N/A": 
                    ata = clean_str(row[4].value)
                    row_id = f"Arr_{d_arr}_{c_arr}_{ata}"
                    if row_id == unique_id:
                        ws.cell(row=row_idx, column=7).value = bay_name
                        wb.save(EXCEL_FILE)
                        break
        except Exception:
            pass

def track_live_airspace():
    global last_prune_date
    bounds = "11.6,10.4,76.4,77.6" 
    current_time_zulu = datetime.now(timezone.utc).strftime('%H:%M')
    current_date_zulu = datetime.now(timezone.utc).strftime('%d/%b/%Y') 
    current_scan_ids = set()

    # Run 4-day cleanup once every new UTC day
    if last_prune_date != current_date_zulu:
        prune_old_logs()
        last_prune_date = current_date_zulu

    try:
        flights = fr_api.get_flights(bounds=bounds)
        
        for f in flights:
            dist_from_ccb = get_distance(f.latitude, f.longitude, CCB_LAT, CCB_LON)
            if dist_from_ccb > TRACKING_RADIUS_NM: continue

            callsign_raw = clean_str(f.callsign).strip().upper()
            reg_clean = clean_str(f.registration).strip().upper()
            ac_type = clean_str(getattr(f, 'aircraft_code', '')).strip().upper()
            squawk = clean_str(getattr(f, 'squawk', 'N/A')).strip()

            api_orig = clean_str(getattr(f, 'origin_airport_iata', 'N/A')).strip().upper()
            api_dest = clean_str(getattr(f, 'destination_airport_iata', 'N/A')).strip().upper()
            orig_icao = AIRPORT_MAP.get(api_orig, api_orig) if api_orig != 'N/A' else 'N/A'
            dest_icao = AIRPORT_MAP.get(api_dest, api_dest) if api_dest != 'N/A' else 'N/A'
            
            # --- THE MINI-FDPS OVERRIDE ENGINE ---
            if squawk in DAILY_FLIGHT_PLANS:
                plan = DAILY_FLIGHT_PLANS[squawk]
                if callsign_raw in ["", "N/A"] or callsign_raw == ac_type:
                    callsign_check = plan.get("callsign", callsign_raw)
                else:
                    callsign_check = callsign_raw
                
                if reg_clean in ["", "N/A"] and plan.get("reg", "N/A") != "N/A":
                    reg_clean = plan["reg"]
                if orig_icao == 'N/A' and plan.get("origin", "N/A") != "N/A":
                    orig_icao = plan["origin"]
                if dest_icao == 'N/A' and plan.get("dest", "N/A") != "N/A":
                    dest_icao = plan["dest"]
            else:
                if callsign_raw in ["", "N/A"] or callsign_raw == ac_type:
                    if reg_clean not in ["", "N/A"]:
                        callsign_check = reg_clean
                    else:
                        sq_display = squawk if squawk not in ["", "N/A"] else "0000"
                        type_display = ac_type if ac_type != "" else "UNK"
                        callsign_check = f"{type_display}-SQ{sq_display}"
                else:
                    callsign_check = callsign_raw
            
            military_prefixes = ('IFC', 'IAF', 'VAYU', 'SARP', 'VU', 'CG', 'NAVY', 'IN')
            if orig_icao == 'VOSX' or dest_icao == 'VOSX' or api_orig == 'SXV' or api_dest == 'SXV' or callsign_check.startswith(military_prefixes):
                continue

            f_id = f.id
            current_scan_ids.add(f_id)
            alt = f.altitude
            speed = getattr(f, 'ground_speed', 0)
            heading = getattr(f, 'heading', 0)
            
            if f_id not in active_targets:
                active_targets[f_id] = {
                    'callsign': callsign_check,
                    'reg': reg_clean,
                    'origin': orig_icao,
                    'dest': dest_icao,
                    'first_alt': alt,
                    'first_lat': f.latitude,
                    'first_lon': f.longitude,
                    'first_seen_time': current_time_zulu,
                    'last_seen_time': current_time_zulu,  
                    'last_seen_date': current_date_zulu, 
                    'last_alt': alt,
                    'last_lat': f.latitude,
                    'last_lon': f.longitude,
                    'last_dist': dist_from_ccb,
                    'max_speed': speed,
                    'last_heading': heading,
                    'status': 'tracking',
                    'start_dist': dist_from_ccb,
                    'event_date': None, 
                    'event_time': None,
                    'deep_dive_done': False
                }
            else:
                target = active_targets[f_id]
                target['last_seen_time'] = current_time_zulu
                target['last_seen_date'] = current_date_zulu
                target['last_alt'] = alt
                target['last_lat'] = f.latitude
                target['last_lon'] = f.longitude
                target['last_dist'] = dist_from_ccb
                target['last_heading'] = heading
                if speed > target['max_speed']: target['max_speed'] = speed
                
                # --- FR24 DEEP DIVE ENGINE ---
                if not target.get('deep_dive_done') and (target['origin'] == 'N/A' or target['dest'] == 'N/A'):
                    try:
                        details = fr_api.get_flight_details(f_id)
                        if details and 'airport' in details:
                            if target['origin'] == 'N/A' and details['airport'].get('origin'):
                                deep_orig_iata = details['airport']['origin'].get('code', {}).get('iata', '')
                                deep_orig_icao = details['airport']['origin'].get('code', {}).get('icao', '')
                                if deep_orig_icao: target['origin'] = deep_orig_icao
                                elif deep_orig_iata: target['origin'] = AIRPORT_MAP.get(deep_orig_iata, deep_orig_iata)
                                
                            if target['dest'] == 'N/A' and details['airport'].get('destination'):
                                deep_dest_iata = details['airport']['destination'].get('code', {}).get('iata', '')
                                deep_dest_icao = details['airport']['destination'].get('code', {}).get('icao', '')
                                if deep_dest_icao: target['dest'] = deep_dest_icao
                                elif deep_dest_iata: target['dest'] = AIRPORT_MAP.get(deep_dest_iata, deep_dest_iata)
                    except Exception:
                        pass
                    target['deep_dive_done'] = True

                # DEPARTURE LOGIC
                if target['status'] == 'tracking' and target['first_alt'] < 3000 and speed >= 50:
                    target['status'] = 'logged_dep'
                    target['event_date'] = current_date_zulu
                    target['event_time'] = current_time_zulu 
                    atd = target['event_time']
                    rwy = get_runway(heading)
                    
                    if target['start_dist'] > VOCB_BOUNDARY_NM:
                        radial = get_radial(CCB_LAT, CCB_LON, target['first_lat'], target['first_lon'])
                        pos_str = f"R-{radial:03d} / {target['start_dist']:.1f} NM"
                        unique_id = f"Misc_{target['event_date']}_{target['callsign']}_{atd}_N/A"
                        log_to_excel("Misc Movements", unique_id, [target['event_date'], target['callsign'], target['reg'], target['origin'], target['dest'], atd, "N/A", pos_str])
                    else:
                        bay = detect_bay(target['first_lat'], target['first_lon']) or "Unknown"
                        unique_id = f"Dep_{target['event_date']}_{target['callsign']}_{atd}"
                        log_to_excel("VOCB Departures", unique_id, [target['event_date'], target['callsign'], target['reg'], target['dest'], atd, rwy, bay])

                # ARRIVAL LOGIC
                elif target['status'] == 'tracking' and target['max_speed'] > 50 and alt < VOCB_GROUND_ALT_MAX and speed < 60:
                    target['status'] = 'logged_arr'
                    target['event_date'] = current_date_zulu 
                    target['event_time'] = current_time_zulu
                    ata = target['event_time']
                    rwy = get_runway(heading)
                    target['landing_rwy'] = rwy 
                    
                    if target['last_dist'] > VOCB_BOUNDARY_NM:
                        radial = get_radial(CCB_LAT, CCB_LON, target['last_lat'], target['last_lon'])
                        pos_str = f"R-{radial:03d} / {target['last_dist']:.1f} NM"
                        unique_id = f"Misc_{target['event_date']}_{target['callsign']}_N/A_{ata}"
                        log_to_excel("Misc Movements", unique_id, [target['event_date'], target['callsign'], target['reg'], target['origin'], target['dest'], "N/A", ata, pos_str])
                    else:
                        unique_id = f"Arr_{target['event_date']}_{target['callsign']}_{ata}"
                        target['logged_id'] = log_to_excel("VOCB Arrivals", unique_id, [target['event_date'], target['callsign'], target['reg'], target['origin'], ata, rwy, "Taxiing"])

                # PARKING BAY DETECTION 
                elif target['status'] == 'logged_arr' and speed < 5 and not target.get('bay_logged') and target['last_dist'] <= VOCB_BOUNDARY_NM:
                    bay = detect_bay(target['last_lat'], target['last_lon'])
                    if bay:
                        update_excel_post_arrival(target.get('logged_id'), bay)
                        target['bay_logged'] = True

        for target_id in list(active_targets.keys()):
            if target_id not in current_scan_ids:
                target = active_targets[target_id]
                if target['status'] == 'tracking':
                    if target['max_speed'] < 40: 
                        del active_targets[target_id]
                        continue

                    is_cjb_flight = (target['origin'] in ['VOCB', 'CJB'] or target['dest'] in ['VOCB', 'CJB'])
                    
                    if is_cjb_flight:
                        if target['dest'] in ['VOCB', 'CJB']:
                            ata = target['last_seen_time']
                            ata_date = target['last_seen_date']
                            log_to_excel("VOCB Arrivals", f"Arr_{ata_date}_{target['callsign']}_{ata}", [ata_date, target['callsign'], target['reg'], target['origin'], ata, "N/A", "Unknown"])
                        else:
                            atd = target['last_seen_time']
                            atd_date = target['last_seen_date']
                            log_to_excel("VOCB Departures", f"Dep_{atd_date}_{target['callsign']}_{atd}", [atd_date, target['callsign'], target['reg'], target['dest'], atd, "N/A", "Unknown"])
                    else:
                        if target['last_dist'] < TRACKING_RADIUS_NM and target['last_alt'] < 4000:
                            radial = get_radial(CCB_LAT, CCB_LON, target['last_lat'], target['last_lon'])
                            pos_str = f"R-{radial:03d} / {target['last_dist']:.1f} NM"
                            ata = target['last_seen_time']
                            ata_date = target['last_seen_date']
                            log_to_excel("Misc Movements", f"Misc_{ata_date}_{target['callsign']}_N/A_{ata}", [ata_date, target['callsign'], target['reg'], target['origin'], target['dest'], "N/A", ata, pos_str])
                        elif target['last_alt'] <= 15000:
                            entry_t = target.get('first_seen_time', '')
                            exit_t = target['last_seen_time']
                            ovf_date = target['last_seen_date']
                            log_to_excel("Overflights", f"Ovf_{ovf_date}_{target['callsign']}_{entry_t}", [ovf_date, target['callsign'], target['reg'], target['origin'], target['dest'], entry_t, exit_t])
                    
                del active_targets[target_id]

    except Exception:
        pass

def radar_background_loop():
    load_existing_logs()
    while True:
        track_live_airspace()
        time.sleep(3)

# Start the radar sweep thread when the server boots
radar_thread = threading.Thread(target=radar_background_loop, daemon=True)
radar_thread.start()

# --- WEB SERVER ENDPOINTS FOR RENDER ---
@app.route('/')
def home():
    now_z = datetime.now(timezone.utc).strftime('%d/%b/%Y %H:%M:%S Z')
    html = f"""
    <html>
        <head><title>VOCB A-SMGCS Audit Logger</title></head>
        <body style="font-family: Arial, sans-serif; background: #121212; color: #e0e0e0; padding: 40px;">
            <h2>📡 VOCB A-SMGCS Tracker (3–4 Day Rolling Excel Mode)</h2>
            <p><b>Server UTC Clock:</b> {now_z}</p>
            <p><b>Active Aircraft in 30 NM Sector:</b> {len(active_targets)}</p>
            <p><b>Total Movements in Current 4-Day Window:</b> {len(logged_movements)}</p>
            <br>
            <a href="/download" style="background: #2e7d32; color: white; padding: 14px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                📥 Download VOCB_Final_V6.xlsx
            </a>
        </body>
    </html>
    """
    return render_template_string(html)

@app.route('/download')
def download_excel():
    init_excel()
    with excel_lock:
        return send_file(EXCEL_FILE, as_attachment=True, download_name="VOCB_Final_V6.xlsx")

@app.route('/health')
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
