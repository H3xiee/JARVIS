# modules/flight_intel.py
import json
import math
import os
import re
import csv
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import List, Dict, Any, Optional

ADSB_MIL_URL = 'https://api.adsb.lol/v2/mil'
OPENSKY_URL = 'https://opensky-network.org/api/states/all'

# High-fidelity tactical contingency fixtures for offline or rate-limited environments
OFFLINE_MIL_FIXTURES = [
    {
        'hex': 'ae01cd',
        'flight': 'RCH842',
        'r': '02-1102',
        't': 'C17',
        'desc': 'BOEING C-17A Globemaster III',
        'lat': 35.3341,
        'lon': -97.5122,
        'alt_baro': 28000,
        'gs': 432.5,
        'track': 74.0,
        'squawk': '1200',
        'category': 'Military Transport',
        'source': 'offline_contingency'
    },
    {
        'hex': 'ae125c',
        'flight': 'IRON61',
        'r': '62-3512',
        't': 'KC135',
        'desc': 'BOEING KC-135R Stratotanker',
        'lat': 38.8122,
        'lon': -85.1243,
        'alt_baro': 24000,
        'gs': 390.2,
        'track': 268.0,
        'squawk': '3142',
        'category': 'Aerial Refueling',
        'source': 'offline_contingency'
    },
    {
        'hex': 'ae07d9',
        'flight': 'SENTRY01',
        'r': '79-0003',
        't': 'E3TF',
        'desc': 'BOEING E-3 Sentry (AWACS)',
        'lat': 36.2114,
        'lon': -76.4321,
        'alt_baro': 31000,
        'gs': 378.0,
        'track': 185.0,
        'squawk': '4410',
        'category': 'Airborne Early Warning',
        'source': 'offline_contingency'
    },
    {
        'hex': 'ae14a1',
        'flight': 'VIPER11',
        'r': '90-0801',
        't': 'F16',
        'desc': 'LOCKHEED F-16C Fighting Falcon',
        'lat': 32.8412,
        'lon': -117.1524,
        'alt_baro': 16500,
        'gs': 520.4,
        'track': 315.0,
        'squawk': '5211',
        'category': 'Fighter Interceptor',
        'source': 'offline_contingency'
    },
    {
        'hex': '43c6f1',
        'flight': 'RRR7215',
        'r': 'ZZ664',
        't': 'R135',
        'desc': 'BOEING RC-135W Rivet Joint',
        'lat': 54.2183,
        'lon': 18.5312,
        'alt_baro': 33000,
        'gs': 415.0,
        'track': 88.0,
        'squawk': '7001',
        'category': 'Electronic Reconnaissance',
        'feed_category': 'mil',
        'mil': 1,
        'source': 'offline_contingency'
    }
]

# Privacy ICAO Address (PIA) fixtures (Brown / Dark Amber tactical visualization)
OFFLINE_PIA_FIXTURES = [
    {
        'hex': 'a209b1',
        'flight': 'PRIV-44',
        'r': 'N128PA',
        't': 'GLF6',
        'desc': 'GULFSTREAM G650ER (PIA ENCRYPTED)',
        'lat': 41.2510,
        'lon': -73.6520,
        'alt_baro': 43000,
        'gs': 488.0,
        'track': 240.0,
        'squawk': '4102',
        'feed_category': 'pia',
        'pia': 1,
        'category': 'PIA Privacy Transponder',
        'source': 'offline_contingency'
    },
    {
        'hex': 'a5582f',
        'flight': 'STEALTH-9',
        'r': 'N552PX',
        't': 'GLEX',
        'desc': 'BOMBARDIER GLOBAL 7500 (PIA PRIVACY)',
        'lat': 34.0200,
        'lon': -118.4500,
        'alt_baro': 47000,
        'gs': 512.0,
        'track': 310.0,
        'squawk': '5120',
        'feed_category': 'pia',
        'pia': 1,
        'category': 'PIA Privacy Transponder',
        'source': 'offline_contingency'
    },
    {
        'hex': '40781a',
        'flight': 'GHOST-01',
        'r': 'G-OPRA',
        't': 'F900',
        'desc': 'DASSAULT FALCON 900LX (PIA ANONYMOUS)',
        'lat': 51.1537,
        'lon': -0.1821,
        'alt_baro': 39000,
        'gs': 465.0,
        'track': 125.0,
        'squawk': '6211',
        'feed_category': 'pia',
        'pia': 1,
        'category': 'PIA Privacy Transponder',
        'source': 'offline_contingency'
    }
]

# FAA Limiting Aircraft Data Displayed (LADD) fixtures (Deep Amber / Leather Brown visualization)
OFFLINE_LADD_FIXTURES = [
    {
        'hex': 'a9103c',
        'flight': 'LADD-82',
        'r': 'N882BL',
        't': 'C750',
        'desc': 'CESSNA CITATION X+ (FAA LADD BLOCKED)',
        'lat': 39.8561,
        'lon': -104.6737,
        'alt_baro': 45000,
        'gs': 525.0,
        'track': 175.0,
        'squawk': '3200',
        'feed_category': 'ladd',
        'ladd': 1,
        'category': 'FAA LADD Restricted',
        'source': 'offline_contingency'
    },
    {
        'hex': 'a7719d',
        'flight': 'EXECUTIVE-1',
        'r': 'N701EX',
        't': 'CL60',
        'desc': 'CHALLENGER 650 (LADD PROTECTED)',
        'lat': 25.7959,
        'lon': -80.2870,
        'alt_baro': 37000,
        'gs': 470.0,
        'track': 45.0,
        'squawk': '2214',
        'feed_category': 'ladd',
        'ladd': 1,
        'category': 'FAA LADD Restricted',
        'source': 'offline_contingency'
    }
]

# Emergency Squawk fixtures (Pulsing Alert Red visualization)
OFFLINE_EMERGENCY_FIXTURES = [
    {
        'hex': '4ca218',
        'flight': 'MAYDAY77',
        'r': 'EI-EMG',
        't': 'B738',
        'desc': 'BOEING 737-800 [GENERAL EMERGENCY SQUAWK 7700]',
        'lat': 53.4264,
        'lon': -6.2499,
        'alt_baro': 9800,
        'gs': 280.0,
        'track': 90.0,
        'squawk': '7700',
        'feed_category': 'emergency',
        'category': 'EMERGENCY SQUAWK 7700',
        'source': 'offline_contingency'
    }
]

# Commercial Airliner fixtures (Cyan / Sky Blue visualization)
OFFLINE_COMMERCIAL_FIXTURES = [
    {
        'hex': '80041a',
        'flight': 'AIC101',
        'r': 'VT-EXG',
        't': 'B77W',
        'desc': 'AIR INDIA BOEING 777-300ER',
        'lat': 28.5562,
        'lon': 77.1000,
        'alt_baro': 36000,
        'gs': 490.0,
        'track': 280.0,
        'squawk': '1410',
        'feed_category': 'commercial',
        'category': 'Commercial Air Transport',
        'source': 'offline_contingency'
    },
    {
        'hex': 'a194bc',
        'flight': 'UAL882',
        'r': 'N27901',
        't': 'B789',
        'desc': 'UNITED AIRLINES BOEING 787-9 DREAMLINER',
        'lat': 37.6188,
        'lon': -122.3750,
        'alt_baro': 38000,
        'gs': 505.0,
        'track': 250.0,
        'squawk': '4231',
        'feed_category': 'commercial',
        'category': 'Commercial Air Transport',
        'source': 'offline_contingency'
    }
]

OFFLINE_ALL_FIXTURES = (
    OFFLINE_MIL_FIXTURES +
    OFFLINE_PIA_FIXTURES +
    OFFLINE_LADD_FIXTURES +
    OFFLINE_EMERGENCY_FIXTURES +
    OFFLINE_COMMERCIAL_FIXTURES
)

for _f in OFFLINE_ALL_FIXTURES:
    _f['simulated'] = True
    _f['badge'] = 'SIMULATED'


# -------------------------------------------------------------------------
# God's Eye Flight Route Plausibility & Geometry (Bilawal Sidhu / routePlausible.js)
# -------------------------------------------------------------------------

D2R = math.pi / 180.0
R_KM = 6371.0


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in kilometers."""
    p1 = lat1 * D2R
    p2 = lat2 * D2R
    dp = (lat2 - lat1) * D2R
    dl = (lon2 - lon1) * D2R
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * (math.sin(dl / 2.0) ** 2)
    return R_KM * 2.0 * math.atan2(math.sqrt(max(0.0, a)), math.sqrt(max(0.0, 1.0 - a)))


def bearing_rad(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = lat1 * D2R
    p2 = lat2 * D2R
    dl = (lon2 - lon1) * D2R
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.atan2(y, x)


def cross_track_km(lat: float, lon: float, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Signed cross-track distance (km) of point from the great circle p1 -> p2."""
    d13 = great_circle_km(lat1, lon1, lat, lon) / R_KM
    b13 = bearing_rad(lat1, lon1, lat, lon)
    b12 = bearing_rad(lat1, lon1, lat2, lon2)
    return math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(b13 - b12)))) * R_KM


def route_plausible(
    lat: Optional[float],
    lon: Optional[float],
    origin: Optional[Dict[str, Any]],
    destination: Optional[Dict[str, Any]],
    max_cross_track_km: float = 350.0
) -> bool:
    """
    Validates whether an adsbdb scheduled route matches the aircraft's actual position.
    Rejects wrong-leg routes (e.g. return flights from previous cycle).
    """
    if lat is None or lon is None or not origin or not destination:
        return True
    o_lat = origin.get("lat")
    o_lon = origin.get("lon")
    d_lat = destination.get("lat")
    d_lon = destination.get("lon")
    if o_lat is None or o_lon is None or d_lat is None or d_lon is None:
        return True

    total_corridor = great_circle_km(o_lat, o_lon, d_lat, d_lon)
    if total_corridor < 50.0:
        return True

    dist_origin = great_circle_km(lat, lon, o_lat, o_lon)
    dist_dest = great_circle_km(lat, lon, d_lat, d_lon)

    # Near either terminal
    if dist_origin < 150.0 or dist_dest < 150.0:
        return True

    # Far off corridor sum
    if (dist_origin + dist_dest) > (total_corridor * 1.6 + 250.0):
        return False

    xt = abs(cross_track_km(lat, lon, o_lat, o_lon, d_lat, d_lon))
    return xt <= max_cross_track_km


class FlightIntelEngine:
    """
    Real-time Airspace & Military Radar Intelligence Engine.
    Queries adsb.lol / OpenSky feeds for live military tracking,
    extracts flight trajectories, and feeds real-time telemetry into J.A.R.V.I.S. HUD.
    """
    def __init__(self):
        self.cache_ttl = 15
        self._last_fetch_time = 0
        self._cached_flights: List[Dict[str, Any]] = []
        self._init_enrichment_cache()

    def get_military_aircraft(self, limit: int = 12) -> List[Dict[str, Any]]:
        """
        Fetch active worldwide military flights from adsb.lol/v2/mil.
        Falls back to OpenSky or offline contingency fixtures.
        """
        now = time.time()
        if self._cached_flights and (now - self._last_fetch_time) < self.cache_ttl:
            return self._cached_flights[:limit]

        flights = []
        # Attempt 1: adsb.lol military API
        try:
            req = urllib.request.Request(
                ADSB_MIL_URL,
                headers={
                    'User-Agent': 'JARVIS-AirspaceMonitor/2.0 (Tactical Recon HUD)',
                    'Accept': 'application/json'
                }
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                ac_list = data.get('ac', [])
                for ac in ac_list:
                    flight_id = (ac.get('flight') or ac.get('r') or ac.get('hex') or '').strip()
                    if not flight_id:
                        continue
                    flights.append({
                        'hex': ac.get('hex', '').strip(),
                        'flight': flight_id,
                        'r': ac.get('r', '').strip(),
                        't': ac.get('t', 'MIL').strip(),
                        'desc': ac.get('desc', ac.get('t', 'Military Aircraft')).strip(),
                        'lat': ac.get('lat', 0.0),
                        'lon': ac.get('lon', 0.0),
                        'alt_baro': ac.get('alt_baro', ac.get('alt_geom', 0)),
                        'gs': ac.get('gs', 0.0),
                        'track': ac.get('track', 0.0),
                        'squawk': ac.get('squawk', 'N/A'),
                        'category': 'Military Airspace',
                        'source': 'adsb.lol'
                    })
        except Exception as e:
            # Silently catch network or sandbox restrictions
            pass

        # Attempt 1.5: OSIRIS Live Military Flights
        if not flights:
            try:
                from modules.osiris_intel import get_osiris_client
                osiris_flights = get_osiris_client().get_flights(military_only=True)
                for mf in osiris_flights.get("military", []):
                    f_id = (mf.get("callsign") or mf.get("registration") or mf.get("icao24") or "MIL-FLIGHT").strip()
                    flights.append({
                        'hex': mf.get('icao24', '').strip(),
                        'flight': f_id,
                        'r': mf.get('registration', '').strip(),
                        't': mf.get('model', 'MIL').strip(),
                        'desc': mf.get('model', 'Military Combat Aircraft').strip(),
                        'lat': mf.get('lat', 0.0),
                        'lon': mf.get('lng', 0.0),
                        'alt_baro': mf.get('alt', 0),
                        'gs': mf.get('speed_knots', 0.0),
                        'track': mf.get('heading', 0.0),
                        'squawk': mf.get('squawk', 'N/A'),
                        'category': 'Military Airspace',
                        'source': 'osiris'
                    })
            except Exception as e:
                pass

        # Attempt 2: If no flights returned, use contingency fixtures
        if not flights:
            flights = list(OFFLINE_MIL_FIXTURES)

        self._cached_flights = flights
        self._last_fetch_time = now
        return flights[:limit]

    def get_flight_trace(self, icao_hex: str) -> Optional[Dict[str, Any]]:
        """
        Fetch 24h trajectory trace points for a given ICAO hex code from adsb.lol.
        """
        hex_clean = icao_hex.strip().lower()
        if len(hex_clean) < 2:
            return None
        url = f'https://adsb.lol/data/traces/{hex_clean[-2:]}/trace_full_{hex_clean}.json'
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'JARVIS-AirspaceMonitor/2.0'}
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception:
            return None

    def format_tactical_debrief(self, flights: List[Dict[str, Any]]) -> str:
        """
        Generate J.A.R.V.I.S. spoken briefing on active military targets.
        """
        if not flights:
            return 'Airspace radar reports no military transponder signatures in range, Sir.'

        top = flights[0]
        desc = top.get('desc') or top.get('t') or 'tactical aircraft'
        callsign = top.get('flight', 'Unknown')
        alt_raw = top.get('alt_baro', 0)
        speed = top.get('gs', 0)
        if str(alt_raw).lower() == 'ground':
            alt_phrase = 'on the ground'
        else:
            try:
                alt_phrase = f'at {int(float(alt_raw)):,} feet'
            except (ValueError, TypeError):
                alt_phrase = f'at {alt_raw} feet'

        lines = [
            f'Locked onto {len(flights)} military radar signatures.',
            f'Lead track is {callsign} ({desc}) {alt_phrase}, tearing through at {speed} knots.'
        ]
        if len(flights) > 1:
            second = flights[1]
            sec_alt = second.get('alt_baro', 0)
            sec_alt_phrase = 'on the ground' if str(sec_alt).lower() == 'ground' else f'at {sec_alt} ft'
            lines.append(f'Secondary target {second.get("flight", "VIPER")} ({second.get("t", "F16")}) {sec_alt_phrase}.')
        return ' '.join(lines)

    # -------------------------------------------------------------------------
    # God's Eye adsbdb.com Enrichment & Caching (Bilawal Sidhu / enrichment.js)
    # -------------------------------------------------------------------------
    def _init_enrichment_cache(self):
        self._cache_dir = Path(__file__).resolve().parent.parent / '.cache'
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._adsbdb_cache_file = self._cache_dir / 'adsbdb.json'
        self._enrich_routes: Dict[str, Any] = {}
        self._enrich_aircraft: Dict[str, Any] = {}
        self._enrich_ttl = 30 * 24 * 3600  # 30 days

        if self._adsbdb_cache_file.exists():
            try:
                with open(self._adsbdb_cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._enrich_routes = data.get('routes', {})
                    self._enrich_aircraft = data.get('aircraft', {})
            except Exception:
                pass

    def _persist_enrichment_cache(self):
        try:
            with open(self._adsbdb_cache_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'routes': self._enrich_routes,
                    'aircraft': self._enrich_aircraft
                }, f)
        except Exception:
            pass

    def get_flight_route(self, callsign: str) -> Optional[Dict[str, Any]]:
        callsign_clean = (callsign or '').strip().upper()
        if not callsign_clean or len(callsign_clean) < 3:
            return None

        now = time.time()
        cached = self._enrich_routes.get(callsign_clean)
        if cached and (now - cached.get('at', 0)) < self._enrich_ttl:
            return cached.get('data')

        url = f'https://api.adsbdb.com/v0/callsign/{urllib.parse.quote(callsign_clean)}'
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'JARVIS-Tactical/2.0 (Gods-Eye-View-Parity)'}
            )
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                fr = data.get('response', {}).get('flightroute')
                if not fr:
                    self._enrich_routes[callsign_clean] = {'at': now, 'data': None}
                    self._persist_enrichment_cache()
                    return None

                origin = fr.get('origin', {})
                dest = fr.get('destination', {})
                parsed = {
                    'airline': (fr.get('airline') or {}).get('name'),
                    'airline_icao': (fr.get('airline') or {}).get('icao'),
                    'origin': {
                        'code': origin.get('iata_code') or origin.get('icao_code') or '',
                        'name': origin.get('municipality') or origin.get('name') or '',
                        'lat': origin.get('latitude'),
                        'lon': origin.get('longitude')
                    },
                    'destination': {
                        'code': dest.get('iata_code') or dest.get('icao_code') or '',
                        'name': dest.get('municipality') or dest.get('name') or '',
                        'lat': dest.get('latitude'),
                        'lon': dest.get('longitude')
                    }
                }
                self._enrich_routes[callsign_clean] = {'at': now, 'data': parsed}
                self._persist_enrichment_cache()
                return parsed
        except urllib.error.HTTPError as he:
            if he.code == 404:
                # Negative cache 404 to protect public API from repeated queries on unknown callsigns
                self._enrich_routes[callsign_clean] = {'at': now, 'data': None}
                self._persist_enrichment_cache()
            if cached and cached.get('data'):
                return cached.get('data')
            return None
        except Exception:
            if cached and cached.get('data'):
                return cached.get('data')
            return None

    def get_aircraft_meta(self, icao_hex: str) -> Optional[Dict[str, Any]]:
        hex_clean = (icao_hex or '').strip().lower()
        if not hex_clean or len(hex_clean) != 6:
            return None

        now = time.time()
        cached = self._enrich_aircraft.get(hex_clean)
        if cached and (now - cached.get('at', 0)) < self._enrich_ttl:
            return cached.get('data')

        url = f'https://api.adsbdb.com/v0/aircraft/{urllib.parse.quote(hex_clean)}'
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'JARVIS-Tactical/2.0 (Gods-Eye-View-Parity)'}
            )
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                ac = data.get('response', {}).get('aircraft')
                if not ac:
                    self._enrich_aircraft[hex_clean] = {'at': now, 'data': None}
                    self._persist_enrichment_cache()
                    return None

                manufacturer = ac.get('manufacturer') or ''
                ac_type = ac.get('type') or ''
                type_name = f"{manufacturer} {ac_type}".strip() if manufacturer and ac_type else ac_type
                parsed = {
                    'type_code': ac.get('icao_type'),
                    'type_name': type_name or None,
                    'registration': ac.get('registration') or None,
                    'registered_owner': ac.get('registered_owner') or None
                }
                self._enrich_aircraft[hex_clean] = {'at': now, 'data': parsed}
                self._persist_enrichment_cache()
                return parsed
        except urllib.error.HTTPError as he:
            if he.code == 404:
                self._enrich_aircraft[hex_clean] = {'at': now, 'data': None}
                self._persist_enrichment_cache()
            if cached and cached.get('data'):
                return cached.get('data')
            return None
        except Exception:
            if cached and cached.get('data'):
                return cached.get('data')
            return None

    def get_flight_enrichment(
        self,
        callsign: str,
        icao_hex: str = '',
        current_lat: Optional[float] = None,
        current_lon: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Enrich flight with operator airline name, aircraft type description, and verified origin -> destination route.
        """
        route = self.get_flight_route(callsign)
        meta = self.get_aircraft_meta(icao_hex) if icao_hex else None

        is_plausible = True
        if route and current_lat is not None and current_lon is not None:
            is_plausible = route_plausible(
                current_lat, current_lon,
                route.get('origin'), route.get('destination')
            )

        origin_code = (route.get('origin') or {}).get('code', '') if route else ''
        dest_code = (route.get('destination') or {}).get('code', '') if route else ''
        route_str = f"{origin_code} → {dest_code}" if (origin_code and dest_code) else None

        return {
            'callsign': (callsign or '').strip().upper(),
            'hex': (icao_hex or '').strip().lower() if icao_hex else '',
            'airline': (route.get('airline') if route else None),
            'origin': (route.get('origin') if route else None),
            'destination': (route.get('destination') if route else None),
            'route_plausible': is_plausible,
            'route_summary': route_str if is_plausible else None,
            'type_code': (meta.get('type_code') if meta else None),
            'type_name': (meta.get('type_name') if meta else None),
            'registration': (meta.get('registration') if meta else None),
            'owner': (meta.get('registered_owner') if meta else None),
        }


# -------------------------------------------------------------------------
# Part 5: OpenFlights Airline Dataset & Flight OSINT Engine
# -------------------------------------------------------------------------

class AirlineDatabase:
    """
    Parser and query interface for OpenFlights airlines.dat dataset.
    Maps between Airline names, IATA codes (e.g. BA), and ICAO codes (e.g. BAW).
    Dataset licensed under the Open Database License (ODbL) v1.0.
    """
    def __init__(self, dat_path: Optional[Path] = None):
        if dat_path is None:
            dat_path = Path(__file__).resolve().parent.parent / "data" / "airlines.dat"
        self.dat_path = Path(dat_path)
        self.iata_to_icao: Dict[str, str] = {}
        self.icao_to_name: Dict[str, str] = {}
        self.icao_to_iata: Dict[str, str] = {}
        self.name_to_icao: Dict[str, str] = {}
        self._load()

    def _load(self):
        if not self.dat_path.exists():
            return
        import csv
        try:
            with open(self.dat_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) < 8:
                        continue
                    name = row[1].strip()
                    iata = row[3].strip()
                    icao = row[4].strip()
                    active = row[7].strip()
                    if icao and icao != r"\N" and icao != "-":
                        icao_upper = icao.upper()
                        if iata and iata != r"\N" and iata != "-":
                            iata_upper = iata.upper()
                            if iata_upper not in self.iata_to_icao or active == "Y":
                                self.iata_to_icao[iata_upper] = icao_upper
                            self.icao_to_iata[icao_upper] = iata_upper
                        if icao_upper not in self.icao_to_name or active == "Y":
                            self.icao_to_name[icao_upper] = name
                        name_lower = name.lower()
                        if name_lower not in self.name_to_icao or active == "Y":
                            self.name_to_icao[name_lower] = icao_upper
        except Exception:
            pass

    def lookup_airline(self, query: str) -> Optional[Dict[str, str]]:
        q = (query or "").strip()
        if not q:
            return None
        q_upper = q.upper()
        if q_upper in self.icao_to_name:
            return {
                "icao": q_upper,
                "iata": self.icao_to_iata.get(q_upper, ""),
                "name": self.icao_to_name[q_upper]
            }
        if q_upper in self.iata_to_icao:
            icao = self.iata_to_icao[q_upper]
            return {
                "icao": icao,
                "iata": q_upper,
                "name": self.icao_to_name.get(icao, "")
            }
        q_lower = q.lower()
        if q_lower in self.name_to_icao:
            icao = self.name_to_icao[q_lower]
            return {
                "icao": icao,
                "iata": self.icao_to_iata.get(icao, ""),
                "name": self.icao_to_name.get(icao, q)
            }
        for name_key, icao in self.name_to_icao.items():
            if q_lower == name_key or q_lower in name_key or name_key in q_lower:
                return {
                    "icao": icao,
                    "iata": self.icao_to_iata.get(icao, ""),
                    "name": self.icao_to_name.get(icao, "")
                }
        return None

    def translate_flight_number(self, flight_str: str) -> str:
        """Translates IATA flight numbers like 'BA117' to ICAO callsign 'BAW117'."""
        s = (flight_str or "").strip().upper()
        import re
        m = re.match(r"^([A-Z0-9]{2})(\d{1,5}[A-Z]?)$", s)
        if m:
            prefix = m.group(1)
            num = m.group(2)
            if prefix in self.iata_to_icao:
                return f"{self.iata_to_icao[prefix]}{num}"
        return s


class FlightTrailStore:
    """
    Stores recent positions per aircraft across restarts in data/flight_trails.json (capped & gitignored).
    """
    def __init__(self, trails_path: Optional[Path] = None):
        if trails_path is None:
            trails_path = Path(__file__).resolve().parent.parent / "data" / "flight_trails.json"
        self.trails_path = Path(trails_path)
        self.max_points_per_aircraft = 60
        self.max_aircraft_total = 500
        self._trails: Dict[str, List[Dict[str, Any]]] = {}
        self._load()

    def _load(self):
        if not self.trails_path.exists():
            return
        try:
            with open(self.trails_path, "r", encoding="utf-8") as f:
                self._trails = json.load(f)
        except Exception:
            self._trails = {}

    def _save(self):
        try:
            self.trails_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trails_path, "w", encoding="utf-8") as f:
                json.dump(self._trails, f)
        except Exception:
            pass

    def record_position(self, identifier: str, lat: float, lon: float, alt: float = 0.0) -> None:
        key = (identifier or "").strip().lower()
        if not key or lat is None or lon is None:
            return
        points = self._trails.get(key, [])
        now_ts = int(time.time())
        # Avoid duplicate point if within 0.001 deg of last point
        if points:
            last = points[-1]
            if abs(last.get("lat", 0) - lat) < 0.0005 and abs(last.get("lon", 0) - lon) < 0.0005:
                return
        points.append({"lat": round(lat, 5), "lon": round(lon, 5), "alt": round(alt, 1), "ts": now_ts})
        if len(points) > self.max_points_per_aircraft:
            points = points[-self.max_points_per_aircraft:]
        self._trails[key] = points

        if len(self._trails) > self.max_aircraft_total:
            # Drop oldest entries
            sorted_keys = sorted(self._trails.keys(), key=lambda k: (self._trails[k][-1]["ts"] if self._trails[k] else 0))
            for old_key in sorted_keys[:len(self._trails) - self.max_aircraft_total]:
                del self._trails[old_key]

        self._save()

    def get_trail(self, identifier: str) -> List[Dict[str, Any]]:
        key = (identifier or "").strip().lower()
        if not key:
            return []
        return list(self._trails.get(key, []))


_AIRLINE_DB = None
_TRAIL_STORE = None

def get_airline_database() -> AirlineDatabase:
    global _AIRLINE_DB
    if _AIRLINE_DB is None:
        _AIRLINE_DB = AirlineDatabase()
    return _AIRLINE_DB

def get_flight_trail_store() -> FlightTrailStore:
    global _TRAIL_STORE
    if _TRAIL_STORE is None:
        _TRAIL_STORE = FlightTrailStore()
    return _TRAIL_STORE


def search_flights(query: str, limit: int = 15) -> List[Dict[str, Any]]:
    """
    Search active flights by callsign, flight number (e.g. BA117 -> BAW117), airline name,
    registration, or ICAO24 hex. Enriches with adsbdb route and updates flight trails.
    """
    q_raw = (query or "").strip()
    if not q_raw:
        return []

    adb = get_airline_database()
    engine = get_flight_intel_engine()
    trail_store = get_flight_trail_store()

    # Determine query interpretations
    callsign_query = adb.translate_flight_number(q_raw)
    airline_info = adb.lookup_airline(q_raw)
    airline_icao = airline_info.get("icao") if airline_info else ""
    airline_name = airline_info.get("name") if airline_info else ""

    is_hex = bool(re.match(r"^[0-9a-fA-F]{6}$", q_raw))
    is_reg = bool(re.match(r"^[A-Za-z0-9]{1,3}-[A-Za-z0-9]+$", q_raw))

    matched_contacts: List[Dict[str, Any]] = []

    # 1. Live queries via adsb.lol / OpenSky
    # Attempt by hex
    if is_hex:
        hex_clean = q_raw.lower()
        try:
            req = urllib.request.Request(
                f"https://api.adsb.lol/v2/hex/{hex_clean}",
                headers={"User-Agent": "Mozilla/5.0 (Tactical-OSINT-HUD)"}
            )
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                data = json.loads(resp.read().decode())
                for ac in data.get("ac", []):
                    matched_contacts.append(ac)
        except Exception:
            pass

    # Attempt by registration
    if is_reg and not matched_contacts:
        reg_clean = q_raw.upper()
        try:
            req = urllib.request.Request(
                f"https://api.adsb.lol/v2/reg/{urllib.parse.quote(reg_clean)}",
                headers={"User-Agent": "Mozilla/5.0 (Tactical-OSINT-HUD)"}
            )
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                data = json.loads(resp.read().decode())
                for ac in data.get("ac", []):
                    matched_contacts.append(ac)
        except Exception:
            pass

    # Attempt by callsign / airline prefix
    target_callsigns = []
    if callsign_query and callsign_query != q_raw:
        target_callsigns.append(callsign_query)
    target_callsigns.append(q_raw.upper())
    if airline_icao and airline_icao not in target_callsigns:
        target_callsigns.append(airline_icao)

    if not matched_contacts:
        for cs in target_callsigns:
            try:
                req = urllib.request.Request(
                    f"https://api.adsb.lol/v2/callsign/{urllib.parse.quote(cs)}",
                    headers={"User-Agent": "Mozilla/5.0 (Tactical-OSINT-HUD)"}
                )
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    data = json.loads(resp.read().decode())
                    for ac in data.get("ac", []):
                        matched_contacts.append(ac)
                if matched_contacts:
                    break
            except Exception:
                pass

    # 2. Attempt OpenSky states if adsb.lol had 0 or errored
    if not matched_contacts:
        try:
            opensky_url = "https://opensky-network.org/api/states/all"
            if is_hex:
                opensky_url += f"?icao24={q_raw.lower()}"
            req = urllib.request.Request(
                opensky_url,
                headers={"User-Agent": "Mozilla/5.0 (Tactical-OSINT-HUD)"}
            )
            with urllib.request.urlopen(req, timeout=4.5) as resp:
                data = json.loads(resp.read().decode())
                states = data.get("states") or []
                for st in states:
                    if not st or len(st) < 11:
                        continue
                    st_hex = (st[0] or "").lower()
                    st_cs = (st[1] or "").strip().upper()
                    st_lon = st[5]
                    st_lat = st[6]
                    st_alt_m = st[7]
                    st_speed_mps = st[9]
                    st_track = st[10]

                    # Match criteria
                    match = False
                    if is_hex and st_hex == q_raw.lower():
                        match = True
                    elif st_cs:
                        if st_cs == callsign_query or st_cs == q_raw.upper():
                            match = True
                        elif airline_icao and st_cs.startswith(airline_icao):
                            match = True
                        elif q_raw.upper() in st_cs:
                            match = True

                    if match and st_lat is not None and st_lon is not None:
                        matched_contacts.append({
                            "hex": st_hex,
                            "flight": st_cs,
                            "lat": st_lat,
                            "lon": st_lon,
                            "alt_baro": (st_alt_m * 3.28084 if st_alt_m is not None else 0),
                            "gs": (st_speed_mps * 1.94384 if st_speed_mps is not None else 0),
                            "track": st_track or 0,
                            "source": "opensky"
                        })
                    if len(matched_contacts) >= limit:
                        break
        except Exception:
            pass

    # 3. Offline / contingency fallback fixtures
    if not matched_contacts:
        candidates = OFFLINE_ALL_FIXTURES
        for ac in candidates:
            ac_cs = (ac.get("flight") or "").upper()
            ac_hex = (ac.get("hex") or "").lower()
            ac_reg = (ac.get("r") or "").upper()
            ac_desc = (ac.get("desc") or "").lower()

            match = False
            if is_hex and ac_hex == q_raw.lower():
                match = True
            elif is_reg and ac_reg == q_raw.upper():
                match = True
            elif ac_cs and (ac_cs == callsign_query or ac_cs == q_raw.upper() or (airline_icao and ac_cs.startswith(airline_icao))):
                match = True
            elif airline_name and airline_name.lower() in ac_desc:
                match = True
            elif q_raw.lower() in ac_cs.lower() or q_raw.lower() in ac_desc:
                match = True

            if match:
                matched_contacts.append(dict(ac))

    # Format, enrich, record trails and assemble results
    results = []
    seen = set()
    for ac in matched_contacts[:limit]:
        hex_code = (ac.get("hex") or "").lower().strip()
        callsign = (ac.get("flight") or ac.get("callsign") or "").strip().upper()
        if not hex_code and not callsign:
            continue
        dedup_key = hex_code or callsign
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        lat = float(ac.get("lat") or 0.0)
        lon = float(ac.get("lon") or 0.0)
        alt = float(ac.get("alt_baro") or ac.get("alt") or 0.0)
        speed = float(ac.get("gs") or ac.get("speed") or 0.0)
        track = float(ac.get("track") or ac.get("heading") or 0.0)
        squawk = str(ac.get("squawk") or "N/A")
        tail = ac.get("r") or ac.get("registration") or ""
        ac_type = ac.get("t") or ac.get("desc") or ac.get("model") or "AIR"

        # Record position into trails
        if lat and lon:
            trail_store.record_position(hex_code or callsign, lat, lon, alt)
            if hex_code and callsign:
                trail_store.record_position(callsign, lat, lon, alt)

        # AdsBDB Enrichment
        enrich = engine.get_flight_enrichment(callsign, hex_code, lat, lon)
        final_airline = enrich.get("airline") or airline_name or (adb.lookup_airline(callsign[:3]) or {}).get("name")
        final_type = enrich.get("type_name") or enrich.get("type_code") or ac_type
        final_reg = enrich.get("registration") or tail or hex_code

        results.append({
            "callsign": callsign or hex_code.upper(),
            "airline": final_airline,
            "type": final_type,
            "registration": final_reg,
            "origin": enrich.get("origin"),
            "destination": enrich.get("destination"),
            "origin_code": (enrich.get("origin") or {}).get("code"),
            "dest_code": (enrich.get("destination") or {}).get("code"),
            "route_summary": enrich.get("route_summary"),
            "altitude": round(alt),
            "speed": round(speed),
            "lat": lat,
            "lon": lon,
            "hex": hex_code,
            "track": track,
            "squawk": squawk,
            "simulated": ac.get("simulated", False),
            "badge": ac.get("badge")
        })

    return results


def get_flight_history(hex_code: str) -> Dict[str, Any]:
    """
    OpenSky past-flight lookup for an aircraft. Reads OPENSKY_CLIENT_ID and
    OPENSKY_CLIENT_SECRET from environment or configuration.
    If lookup fails or credentials unavailable, returns 'history unavailable'.
    Never fabricates a track.
    """
    clean_hex = (hex_code or "").strip().lower()
    if not clean_hex or len(clean_hex) != 6:
        return {
            "status": "history unavailable",
            "available": False,
            "flights": []
        }

    client_id = os.environ.get("OPENSKY_CLIENT_ID") or os.environ.get("OPENSKY_USERNAME")
    client_secret = os.environ.get("OPENSKY_CLIENT_SECRET") or os.environ.get("OPENSKY_PASSWORD")

    try:
        from core.config import get_config
        if not client_id:
            client_id = get_config("OPENSKY_CLIENT_ID") or get_config("OPENSKY_USERNAME")
        if not client_secret:
            client_secret = get_config("OPENSKY_CLIENT_SECRET") or get_config("OPENSKY_PASSWORD")
    except Exception:
        pass

    if not client_id or not client_secret:
        return {
            "status": "history unavailable",
            "available": False,
            "flights": []
        }

    import base64
    now = int(time.time())
    begin = now - (7 * 86400)
    url = f"https://opensky-network.org/api/flights/aircraft?icao24={clean_hex}&begin={begin}&end={now}"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "JARVIS-Airspace-History/2.0",
            "Authorization": "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("utf-8")
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list) and data:
                    formatted = []
                    for fl in data[:8]:
                        formatted.append({
                            "callsign": (fl.get("callsign") or "").strip(),
                            "departure": fl.get("estDepartureAirport") or "N/A",
                            "arrival": fl.get("estArrivalAirport") or "N/A",
                            "firstSeen": fl.get("firstSeen"),
                            "lastSeen": fl.get("lastSeen"),
                        })
                    return {
                        "status": "LIVE",
                        "available": True,
                        "flights": formatted
                    }
    except Exception:
        pass

    return {
        "status": "history unavailable",
        "available": False,
        "flights": []
    }


_DEFAULT_ENGINE = None

def get_flight_intel_engine() -> FlightIntelEngine:
    global _DEFAULT_ENGINE
    if _DEFAULT_ENGINE is None:
        _DEFAULT_ENGINE = FlightIntelEngine()
    return _DEFAULT_ENGINE


