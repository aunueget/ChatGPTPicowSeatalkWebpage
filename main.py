# main.py  -- MicroPython for Raspberry Pi Pico W
import network, socket, time, ure, uasyncio as asyncio
from machine import UART, Pin

# ====== CONFIG ======
WIFI_SSID = "your-ssid"
WIFI_PASS = "your-password"

# choose mode: "nmea" if you have a SeaTalk->NMEA converter (recommended)
# or "seatalk_raw" to just capture raw bytes (useful for debugging)
MODE = "nmea"  # or "seatalk_raw"

# UART config -- change TX/RX pins to the pins you will use for reading SeaTalk/NMEA
UART_ID = 1
UART_BAUD = 4800
UART_TX_PIN = 4   # not used for reading but set anyway
UART_RX_PIN = 5

# web server settings
HOST = "0.0.0.0"
PORT = 80

# ====== STATE ======
state = {
    "lat": None,
    "lon": None,
    "speed_kn": None,
    "heading": None,   # degrees
    "raw_hex": None,
    "last_update": None
}

# ====== SETUP WIFI ======
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi...")
        wlan.connect(WIFI_SSID, WIFI_PASS)
        t0 = time.time()
        while not wlan.isconnected():
            time.sleep(0.5)
            if time.time() - t0 > 15:
                print("WiFi connect timeout")
                break
    print("network config:", wlan.ifconfig())

# ====== UART READ & PARSE ======
uart = UART(UART_ID, baudrate=UART_BAUD, tx=Pin(UART_TX_PIN), rx=Pin(UART_RX_PIN))

# helper: NMEA lat/lon conversion
def nmea_to_decimal(coord, hem):
    # coord like ddmm.mmmm or dddmm.mmmm
    if not coord or coord == '':
        return None
    try:
        dot = coord.find('.')
        deglen = dot - 2
        degrees = float(coord[:deglen])
        minutes = float(coord[deglen:])
        dec = degrees + minutes/60.0
        if hem in ('S','W'):
            dec = -dec
        return dec
    except Exception as e:
        return None

def parse_nmea_line(line):
    # basic parsing for GPRMC, GPGGA, GPVTG, GPHDT
    try:
        line = line.strip()
        if not line.startswith('$'):
            return
        parts = line.split(',')
        typ = parts[0][3:]
        if typ == 'RMC' or typ == 'GPRMC':
            # Example: $GPRMC,hhmmss,A,llll.ll,a,yyyyy.yy,a,x.x,xxx.x,ddmmyy,magvar,E*hh
            if parts[2] == 'A':  # valid
                lat = nmea_to_decimal(parts[3], parts[4])
                lon = nmea_to_decimal(parts[5], parts[6])
                speed_kn = float(parts[7]) if parts[7] else None
                # heading is parts[8]
                heading = float(parts[8]) if parts[8] else None
                if lat: state['lat'] = lat
                if lon: state['lon'] = lon
                state['speed_kn'] = speed_kn
                state['heading'] = heading if heading is not None else state.get('heading')
                state['last_update'] = time.time()
        elif typ == 'GGA' or typ == 'GPGGA':
            # $GPGGA,... lat lon ...
            lat = nmea_to_decimal(parts[2], parts[3])
            lon = nmea_to_decimal(parts[4], parts[5])
            if lat: state['lat'] = lat
            if lon: state['lon'] = lon
            state['last_update'] = time.time()
        elif typ == 'VTG' or typ == 'GPVTG':
            # track and speed: parts[1] track, parts[5] speed in knots
            speed_kn = float(parts[5]) if len(parts)>5 and parts[5] else None
            if speed_kn is not None:
                state['speed_kn'] = speed_kn
                state['last_update'] = time.time()
        elif typ == 'HDT' or typ == 'GPHDT':
            # heading
            heading = float(parts[1]) if parts[1] else None
            if heading is not None:
                state['heading'] = heading
                state['last_update'] = time.time()
    except Exception as e:
        print("NMEA parse err:", e, line)

async def uart_reader():
    buf = b""
    while True:
        await asyncio.sleep(0)
        if uart.any():
            b = uart.read(512)  # read up to 512 bytes
            if not b:
                continue
            # store raw hex for debugging
            state['raw_hex'] = ''.join('{:02X} '.format(x) for x in b)
            if MODE == "nmea":
                buf += b
                # split lines by CRLF
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    try:
                        sline = line.decode('ascii', 'ignore').strip()
                        if sline:
                            parse_nmea_line(sline)
                    except Exception as e:
                        print("line decode err", e)
            else:
                # seatalk_raw: keep last bytes for debugging, no parse
                state['last_update'] = time.time()
        else:
            await asyncio.sleep_ms(100)

# ====== HTTP server ======
INDEX_HTML = """HTTP/1.0 200 OK
Content-Type: text/html

<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>PicoW Sea Data</title>
<style>
body{font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial; text-align:center; padding:20px}
#compass { width:200px; height:200px; margin:20px auto; position:relative; }
#needle { width:4px; height:100px; background:#c33; position:absolute; left:50%; top:10px; transform-origin:50% 90%; transform: translateX(-50%) rotate(0deg); border-radius:2px; box-shadow:0 0 6px rgba(0,0,0,0.4)}
#dial { width:200px;height:200px;border:4px solid #222;border-radius:50%; position:relative}
#info { margin-top:10px }
.kv { font-size:18px; margin:6px }
</style>
</head>
<body>
<h2>Pico W — Boat Data</h2>
<div id="compass"><div id="dial"></div><div id="needle"></div></div>
<div id="info">
  <div class="kv">Heading: <span id="heading">—</span>°</div>
  <div class="kv">Speed: <span id="speed">—</span> kn</div>
  <div class="kv">Lat: <span id="lat">—</span></div>
  <div class="kv">Lon: <span id="lon">—</span></div>
  <div class="kv"><small id="raw"></small></div>
</div>
<script>
async function fetchData(){
  try {
    let resp = await fetch('/data');
    if (!resp.ok) throw '';
    let j = await resp.json();
    document.getElementById('heading').innerText = j.heading !== null ? j.heading.toFixed(1) : '—';
    document.getElementById('speed').innerText = j.speed_kn !== null ? j.speed_kn.toFixed(2) : '—';
    document.getElementById('lat').innerText = j.lat !== null ? j.lat.toFixed(6) : '—';
    document.getElementById('lon').innerText = j.lon !== null ? j.lon.toFixed(6) : '—';
    document.getElementById('raw').innerText = j.raw_hex ? 'Raw: '+j.raw_hex : '';
    // rotate needle: CSS rotate uses degrees clockwise; we subtract 0 to map
    let heading = j.heading !== null ? j.heading : 0;
    document.getElementById('needle').style.transform = 'translateX(-50%) rotate('+heading+'deg)';
  } catch(e){
    // console.log('fetch err', e);
  }
}

setInterval(fetchData, 1000);
fetchData();
</script>
</body>
</html>
"""

async def http_server():
    addr = socket.getaddrinfo(HOST, PORT)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(5)
    print("Listening on", addr)
    s.settimeout(0.5)
    while True:
        try:
            cl, addr = s.accept()
        except OSError:
            await asyncio.sleep(0)
            continue
        cl_file = cl.makefile('rwb', 0)
        try:
            req_line = cl_file.readline()
            if not req_line:
                cl.close(); continue
            method, path, _ = req_line.decode().split(' ',2)
            # consume headers
            while True:
                h = cl_file.readline()
                if not h or h == b'\r\n':
                    break
            if path == '/' or path.startswith('/index'):
                cl.send(INDEX_HTML.encode('utf-8'))
            elif path.startswith('/data'):
                # return JSON
                import ujson
                out = {
                    "lat": state['lat'],
                    "lon": state['lon'],
                    "speed_kn": state['speed_kn'],
                    "heading": state['heading'],
                    "raw_hex": state['raw_hex'],
                    "last_update": state['last_update']
                }
                body = ujson.dumps(out)
                hdr = "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body)
                cl.send(hdr.encode() + body.encode())
            else:
                cl.send("HTTP/1.0 404 Not Found\r\n\r\n".encode())
        except Exception as e:
            print("HTTP handling err", e)
        finally:
            try:
                cl_file.close()
            except:
                pass
            cl.close()
        await asyncio.sleep(0)

# ====== RUN ======
async def main():
    connect_wifi()
    await asyncio.gather(uart_reader(), http_server())

try:
    asyncio.run(main())
finally:
    asyncio.new_event_loop()
# main.py  -- MicroPython for Raspberry Pi Pico W
import network, socket, time, ure, uasyncio as asyncio
from machine import UART, Pin

# ====== CONFIG ======
WIFI_SSID = "your-ssid"
WIFI_PASS = "your-password"

# choose mode: "nmea" if you have a SeaTalk->NMEA converter (recommended)
# or "seatalk_raw" to just capture raw bytes (useful for debugging)
MODE = "nmea"  # or "seatalk_raw"

# UART config -- change TX/RX pins to the pins you will use for reading SeaTalk/NMEA
UART_ID = 1
UART_BAUD = 4800
UART_TX_PIN = 4   # not used for reading but set anyway
UART_RX_PIN = 5

# web server settings
HOST = "0.0.0.0"
PORT = 80

# ====== STATE ======
state = {
    "lat": None,
    "lon": None,
    "speed_kn": None,
    "heading": None,   # degrees
    "raw_hex": None,
    "last_update": None
}

# ====== SETUP WIFI ======
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi...")
        wlan.connect(WIFI_SSID, WIFI_PASS)
        t0 = time.time()
        while not wlan.isconnected():
            time.sleep(0.5)
            if time.time() - t0 > 15:
                print("WiFi connect timeout")
                break
    print("network config:", wlan.ifconfig())

# ====== UART READ & PARSE ======
uart = UART(UART_ID, baudrate=UART_BAUD, tx=Pin(UART_TX_PIN), rx=Pin(UART_RX_PIN))

# helper: NMEA lat/lon conversion
def nmea_to_decimal(coord, hem):
    # coord like ddmm.mmmm or dddmm.mmmm
    if not coord or coord == '':
        return None
    try:
        dot = coord.find('.')
        deglen = dot - 2
        degrees = float(coord[:deglen])
        minutes = float(coord[deglen:])
        dec = degrees + minutes/60.0
        if hem in ('S','W'):
            dec = -dec
        return dec
    except Exception as e:
        return None

def parse_nmea_line(line):
    # basic parsing for GPRMC, GPGGA, GPVTG, GPHDT
    try:
        line = line.strip()
        if not line.startswith('$'):
            return
        parts = line.split(',')
        typ = parts[0][3:]
        if typ == 'RMC' or typ == 'GPRMC':
            # Example: $GPRMC,hhmmss,A,llll.ll,a,yyyyy.yy,a,x.x,xxx.x,ddmmyy,magvar,E*hh
            if parts[2] == 'A':  # valid
                lat = nmea_to_decimal(parts[3], parts[4])
                lon = nmea_to_decimal(parts[5], parts[6])
                speed_kn = float(parts[7]) if parts[7] else None
                # heading is parts[8]
                heading = float(parts[8]) if parts[8] else None
                if lat: state['lat'] = lat
                if lon: state['lon'] = lon
                state['speed_kn'] = speed_kn
                state['heading'] = heading if heading is not None else state.get('heading')
                state['last_update'] = time.time()
        elif typ == 'GGA' or typ == 'GPGGA':
            # $GPGGA,... lat lon ...
            lat = nmea_to_decimal(parts[2], parts[3])
            lon = nmea_to_decimal(parts[4], parts[5])
            if lat: state['lat'] = lat
            if lon: state['lon'] = lon
            state['last_update'] = time.time()
        elif typ == 'VTG' or typ == 'GPVTG':
            # track and speed: parts[1] track, parts[5] speed in knots
            speed_kn = float(parts[5]) if len(parts)>5 and parts[5] else None
            if speed_kn is not None:
                state['speed_kn'] = speed_kn
                state['last_update'] = time.time()
        elif typ == 'HDT' or typ == 'GPHDT':
            # heading
            heading = float(parts[1]) if parts[1] else None
            if heading is not None:
                state['heading'] = heading
                state['last_update'] = time.time()
    except Exception as e:
        print("NMEA parse err:", e, line)

async def uart_reader():
    buf = b""
    while True:
        await asyncio.sleep(0)
        if uart.any():
            b = uart.read(512)  # read up to 512 bytes
            if not b:
                continue
            # store raw hex for debugging
            state['raw_hex'] = ''.join('{:02X} '.format(x) for x in b)
            if MODE == "nmea":
                buf += b
                # split lines by CRLF
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    try:
                        sline = line.decode('ascii', 'ignore').strip()
                        if sline:
                            parse_nmea_line(sline)
                    except Exception as e:
                        print("line decode err", e)
            else:
                # seatalk_raw: keep last bytes for debugging, no parse
                state['last_update'] = time.time()
        else:
            await asyncio.sleep_ms(100)

# ====== HTTP server ======
INDEX_HTML = """HTTP/1.0 200 OK
Content-Type: text/html

<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>PicoW Sea Data</title>
<style>
body{font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial; text-align:center; padding:20px}
#compass { width:200px; height:200px; margin:20px auto; position:relative; }
#needle { width:4px; height:100px; background:#c33; position:absolute; left:50%; top:10px; transform-origin:50% 90%; transform: translateX(-50%) rotate(0deg); border-radius:2px; box-shadow:0 0 6px rgba(0,0,0,0.4)}
#dial { width:200px;height:200px;border:4px solid #222;border-radius:50%; position:relative}
#info { margin-top:10px }
.kv { font-size:18px; margin:6px }
</style>
</head>
<body>
<h2>Pico W — Boat Data</h2>
<div id="compass"><div id="dial"></div><div id="needle"></div></div>
<div id="info">
  <div class="kv">Heading: <span id="heading">—</span>°</div>
  <div class="kv">Speed: <span id="speed">—</span> kn</div>
  <div class="kv">Lat: <span id="lat">—</span></div>
  <div class="kv">Lon: <span id="lon">—</span></div>
  <div class="kv"><small id="raw"></small></div>
</div>
<script>
async function fetchData(){
  try {
    let resp = await fetch('/data');
    if (!resp.ok) throw '';
    let j = await resp.json();
    document.getElementById('heading').innerText = j.heading !== null ? j.heading.toFixed(1) : '—';
    document.getElementById('speed').innerText = j.speed_kn !== null ? j.speed_kn.toFixed(2) : '—';
    document.getElementById('lat').innerText = j.lat !== null ? j.lat.toFixed(6) : '—';
    document.getElementById('lon').innerText = j.lon !== null ? j.lon.toFixed(6) : '—';
    document.getElementById('raw').innerText = j.raw_hex ? 'Raw: '+j.raw_hex : '';
    // rotate needle: CSS rotate uses degrees clockwise; we subtract 0 to map
    let heading = j.heading !== null ? j.heading : 0;
    document.getElementById('needle').style.transform = 'translateX(-50%) rotate('+heading+'deg)';
  } catch(e){
    // console.log('fetch err', e);
  }
}

setInterval(fetchData, 1000);
fetchData();
</script>
</body>
</html>
"""

async def http_server():
    addr = socket.getaddrinfo(HOST, PORT)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(5)
    print("Listening on", addr)
    s.settimeout(0.5)
    while True:
        try:
            cl, addr = s.accept()
        except OSError:
            await asyncio.sleep(0)
            continue
        cl_file = cl.makefile('rwb', 0)
        try:
            req_line = cl_file.readline()
            if not req_line:
                cl.close(); continue
            method, path, _ = req_line.decode().split(' ',2)
            # consume headers
            while True:
                h = cl_file.readline()
                if not h or h == b'\r\n':
                    break
            if path == '/' or path.startswith('/index'):
                cl.send(INDEX_HTML.encode('utf-8'))
            elif path.startswith('/data'):
                # return JSON
                import ujson
                out = {
                    "lat": state['lat'],
                    "lon": state['lon'],
                    "speed_kn": state['speed_kn'],
                    "heading": state['heading'],
                    "raw_hex": state['raw_hex'],
                    "last_update": state['last_update']
                }
                body = ujson.dumps(out)
                hdr = "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body)
                cl.send(hdr.encode() + body.encode())
            else:
                cl.send("HTTP/1.0 404 Not Found\r\n\r\n".encode())
        except Exception as e:
            print("HTTP handling err", e)
        finally:
            try:
                cl_file.close()
            except:
                pass
            cl.close()
        await asyncio.sleep(0)

# ====== RUN ======
async def main():
    connect_wifi()
    await asyncio.gather(uart_reader(), http_server())

try:
    asyncio.run(main())
finally:
    asyncio.new_event_loop()
