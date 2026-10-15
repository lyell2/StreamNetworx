import socket, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

def check_srt_listener():
    try:
        # Check if MediaMTX is running
        res = subprocess.run(["pgrep", "mediamtx"], stdout=subprocess.PIPE)
        return res.returncode == 0
    except:
        return False

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            if check_srt_listener():
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"healthy","service":"srt-ingress"}')
            else:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"unhealthy"}')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args): pass

if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8082), HealthHandler).serve_forever()
