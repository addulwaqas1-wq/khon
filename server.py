import http.server
import socketserver
import json
import smtplib
import ssl
import socket
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import urllib.parse
import time
import uuid
import re
import threading
import mimetypes
from datetime import datetime
import webbrowser

PORT = 8001
STATS = {
    'sent': 0,
    'opened': 0,
    'clicked': 0,
    'failed': 0,
    'unverified': 0,
    'logs': [],
    'status': 'idle' # idle, sending, completed, error
}
STATS_LOCK = threading.Lock()

# Global object to handle background sending
class EmailSender(threading.Thread):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.daemon = True

    def run(self):
        with STATS_LOCK:
            STATS['status'] = 'sending'
        
        # Handle Schedule
        schedule_time_str = self.data.get('schedule_time')
        if schedule_time_str:
            try:
                # Expect ISO format: YYYY-MM-DDTHH:MM
                schedule_time = datetime.fromisoformat(schedule_time_str)
                now = datetime.now()
                delay_start = (schedule_time - now).total_seconds()
                
                if delay_start > 0:
                    log_msg = f"[{time.strftime('%H:%M:%S')}] Scheduled: Waiting {int(delay_start)}s to start."
                    with STATS_LOCK:
                        STATS['logs'].append(log_msg)
                    print(log_msg)
                    time.sleep(delay_start)
            except Exception as e:
                with STATS_LOCK:
                    STATS['logs'].append(f"[{time.strftime('%H:%M:%S')}] Schedule Error: {e}")

        # Handle Bulk Sending
        try:
            recipients = self.data.get('recipients', [])
            if not recipients and 'recipient' in self.data:
                recipients = [{'email': self.data['recipient'], 'name': '', 'company': ''}]

            sender_email = self.data['sender_email']
            password = self.data['password']
            delay_seconds = float(self.data.get('delay_seconds', 0.5))
            
            context = ssl.create_default_context()
            
            # Helper to create connection
            def create_connection():
                s = smtplib.SMTP("smtp.gmail.com", 587)
                s.starttls(context=context)
                try:
                    s.login(sender_email, password)
                    return s
                except smtplib.SMTPAuthenticationError:
                    raise Exception("Login failed! Check your email and APP PASSWORD.")
                except Exception as e:
                    raise Exception(f"Connection failed: {str(e)}")

            server = None
            try:
                server = create_connection()
                
                for recipient in recipients:
                    email_addr = recipient['email'].strip()
                    # Robust Verification (Regex)
                    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email_addr):
                         with STATS_LOCK:
                            STATS['unverified'] += 1
                            error_msg = "Invalid Email Format"
                            STATS['logs'].append(f"[{time.strftime('%H:%M:%S')}] Unverified {email_addr}: {error_msg}")
                         continue

                    # Retry Logic for each email
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            # Reconnect if server is None or disconnected (basic check)
                            if server is None:
                                server = create_connection()
                                
                            self.send_single_email(server, self.data, recipient)
                            
                            with STATS_LOCK:
                                STATS['sent'] += 1
                                STATS['logs'].append(f"[{time.strftime('%H:%M:%S')}] Sent to {email_addr}")
                            break # Success, exit retry loop

                        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, smtplib.SMTPHeloError) as e:
                            # Connection errors - try to reconnect
                            print(f"Connection lost ({e}), reconnecting... (Attempt {attempt+1}/{max_retries})")
                            try:
                                if server:
                                    try: server.quit() 
                                    except: pass
                            except: pass
                            server = None # Force recreation
                            if attempt == max_retries - 1:
                                with STATS_LOCK:
                                    STATS['failed'] += 1
                                    STATS['logs'].append(f"[{time.strftime('%H:%M:%S')}] Failed {email_addr}: Connection Error")
                        except Exception as e:
                            # Other errors (e.g. Recipient Refused) - do not retry
                            with STATS_LOCK:
                                STATS['failed'] += 1
                                error_msg = str(e)
                                STATS['logs'].append(f"[{time.strftime('%H:%M:%S')}] Failed {email_addr}: {error_msg}")
                            break
                        
                    # Apply Delay
                    time.sleep(delay_seconds) 
            
            finally:
                if server:
                    try: server.quit()
                    except: pass
            
            with STATS_LOCK:
                STATS['status'] = 'completed'
                STATS['logs'].append(f"[{time.strftime('%H:%M:%S')}] Campaign Completed.")

        except Exception as e:
            with STATS_LOCK:
                STATS['status'] = 'error'
                STATS['logs'].append(f"[{time.strftime('%H:%M:%S')}] Critical Error: {str(e)}")
            print(f"Critical Error: {e}")

    def send_single_email(self, server, data, recipient_data):
        sender_email = data['sender_email']
        sender_name = data.get('sender_name', 'Mass Mailer User')
        
        email_to = recipient_data['email']
        rec_name = recipient_data.get('name', '')
        rec_company = recipient_data.get('company', '')

        # Personalization
        subject = data['subject'].replace('{Name}', rec_name).replace('{Company}', rec_company)
        body_content = data['body'].replace('{Name}', rec_name).replace('{Company}', rec_company)

        # HTML Body (Assumes body_content is already HTML from Rich Text Editor)
        body_content_html = body_content
        
        # Tracking Links
        tracking_id = str(uuid.uuid4())
        base_url = data.get('base_url') or f"http://localhost:{PORT}"
        open_tracker = f'{base_url}/track/open?id={tracking_id}'

        # Rewrite all <a href=""> links to go through /track/click and then redirect
        # This enables "Clicked" tracking to increment reliably.
        def _rewrite_links(html_text: str) -> str:
            def _repl(m):
                href = m.group(1)
                try:
                    # Skip if already routed through /track/click
                    if href.startswith(f"{base_url}/track/click"):
                        return f'href="{href}"'
                    # URL-encode target
                    safe = urllib.parse.quote(href, safe='')
                    return f'href="{base_url}/track/click?url={safe}"'
                except Exception:
                    # Fallback: keep original href
                    return f'href="{href}"'
            return re.sub(r'href=["\'](.*?)["\']', _repl, html_text)
        
        body_content_html = _rewrite_links(body_content_html)
        
        # Construct HTML Body
        html = f"""
        <html>
          <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="padding: 20px;">
                {body_content_html}
            </div>
            <p style="font-size: 12px; color: #888; margin-top: 30px; border-top: 1px solid #eee; padding-top: 10px;">
                Sent via Mass Mailer
            </p>
            <img src="{open_tracker}" width="1" height="1" style="display:none;" />
          </body>
        </html>
        """
        def _html_to_text_with_tracked_links(html_text: str) -> str:
            def _repl(m):
                href = m.group(1)
                text = m.group(2)
                try:
                    if href.startswith(f"{base_url}/track/click"):
                        tracked = href
                    else:
                        safe = urllib.parse.quote(href, safe='')
                        tracked = f"{base_url}/track/click?url={safe}"
                    return f"{text} (link: {tracked})"
                except Exception:
                    return f"{text} (link: {href})"
            processed = re.sub(r'<a [^>]*href=["\'](.*?)["\'][^>]*>(.*?)</a>', _repl, body_content_html, flags=re.IGNORECASE|re.DOTALL)
            processed = re.sub(r'<[^>]+>', '', processed)
            processed = re.sub(r'\s+', ' ', processed).strip()
            return processed
        text_body = _html_to_text_with_tracked_links(body_content_html)
        
        message = MIMEMultipart("mixed") if data.get('attachment') else MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = f"{sender_name} <{sender_email}>"
        message["To"] = email_to

        # HTML Part
        html_part = MIMEText(html, "html")
        text_part = MIMEText(text_body, "plain")
        
        if data.get('attachment'):
             alt_part = MIMEMultipart("alternative")
             alt_part.attach(text_part)
             alt_part.attach(html_part)
             message.attach(alt_part)
             
             # Attachment Part
             att_data = data['attachment']
             try:
                 # Remove header if present (e.g. "data:application/pdf;base64,")
                 if ',' in att_data['data']:
                     header, encoded = att_data['data'].split(',', 1)
                 else:
                     encoded = att_data['data']
                     
                 file_data = base64.b64decode(encoded)
                 
                 # Guess MIME type
                 ctype, encoding = mimetypes.guess_type(att_data['name'])
                 if ctype is None or encoding is not None:
                     ctype = 'application/octet-stream'
                 
                 maintype, subtype = ctype.split('/', 1)
                 
                 part = MIMEBase(maintype, subtype)
                 part.set_payload(file_data)
                 encoders.encode_base64(part)
                 part.add_header('Content-Disposition', f'attachment; filename="{att_data["name"]}"')
                 message.attach(part)
             except Exception as e:
                 print(f"Attachment error: {e}")
        else:
             message.attach(text_part)
             message.attach(html_part)

        server.sendmail(sender_email, email_to, message.as_string())


# 1x1 Transparent GIF
PIXEL_GIF = b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'

# Disposable / Temporary Email Domains removed

# verify_smtp_handshake removed

# get_mx_records removed

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query = urllib.parse.parse_qs(parsed_path.query)

        if path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(STATS).encode())
            return

        elif path == '/track/open':
            # Increment open count
            with STATS_LOCK:
                STATS['opened'] += 1
                log_entry = f"[{time.strftime('%H:%M:%S')}] Email Opened!"
                STATS['logs'].append(log_entry)
            print(log_entry)
            
            # Return transparent pixel
            self.send_response(200)
            self.send_header('Content-type', 'image/gif')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(PIXEL_GIF)
            return

        elif path == '/track/click':
            with STATS_LOCK:
                STATS['clicked'] += 1
                log_entry = f"[{time.strftime('%H:%M:%S')}] Link Clicked!"
                STATS['logs'].append(log_entry)
            print(log_entry)

            # Redirect to original URL or home
            target_url = query.get('url', ['/'])[0]
            self.send_response(302)
            self.send_header('Location', target_url)
            self.end_headers()
            return

        # Default to serving static files
        return super().do_GET()

    def do_POST(self):
        if self.path == '/api/send':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))

            host = self.headers.get('Host', f'localhost:{PORT}')
            proto_header = self.headers.get('X-Forwarded-Proto', '')
            scheme = 'https' if proto_header.lower() == 'https' else 'http'
            data['base_url'] = f"{scheme}://{host}"

            try:
                # Start Background Thread
                sender_thread = EmailSender(data)
                sender_thread.start()

                response = {
                    "status": "success",
                    "message": "Campaign started in background."
                }
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            except Exception as e:
                error_msg = str(e)
                print(f"Error starting campaign: {error_msg}")
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": error_msg}).encode())
            return

        elif self.path == '/api/verify':
            self.send_response(404)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Email verifier removed'}).encode())
            return
            
        return super().do_POST()

    # send_single_email moved to EmailSender class


print(f"Starting server on port {PORT}...")
socketserver.TCPServer.allow_reuse_address = True
try:
    with socketserver.TCPServer(("", PORT), RequestHandler) as httpd:
        print("Server running...")
        def _open_browser():
            time.sleep(1)
            try:
                webbrowser.open(f"http://localhost:{PORT}")
            except Exception:
                pass
        threading.Thread(target=_open_browser, daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping server...")
        finally:
            httpd.server_close()
except Exception as e:
    print(f"Server crashed: {e}")
    import traceback
    traceback.print_exc()
