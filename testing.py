import socket
import threading
import time
import os
import logging
import platform
import uuid
import ipaddress
import hashlib
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Network settings
BROADCAST_PORT = 5001  # Port for peer discovery
FILE_PORT = 5002       # Port for file transfers
SHARED_FOLDER = "shared/"  # Folder to share files
HEARTBEAT_INTERVAL = 5  # Seconds between heartbeats
PEER_TIMEOUT = 30      # Seconds before considering a peer offline

# Get unique device ID
DEVICE_ID = str(uuid.uuid4())[:8]  # Short unique ID for this instance

# Get local IP and hostname
hostname = socket.gethostname()
local_ip = socket.gethostbyname(hostname)

# Global variables
peers = {}  # Dict of discovered peers with last seen timestamp: {ip: {'last_seen': timestamp, 'hostname': hostname}}
peers_lock = threading.Lock()  # Lock for thread-safe access to peers
running = True  # Flag to control threads
receiving_file = False  # Flag to track if a file is being received

# Track received files by their hash to prevent duplication
file_hashes = {}  # {file_hash: {'timestamp': timestamp, 'filename': original_name}}
file_hashes_lock = threading.Lock()

# Add a set to track recently received files with timestamps
recently_received_files = {}  # {file_path: timestamp}
recently_received_lock = threading.Lock()

# Ensure shared folder exists
os.makedirs(SHARED_FOLDER, exist_ok=True)

def calculate_file_hash(file_path):
    """Calculate a SHA-256 hash of a file to uniquely identify it."""
    try:
        hasher = hashlib.sha256()
        with open(file_path, 'rb') as f:
            # Read in chunks to handle large files efficiently
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        logging.error(f"Failed to calculate hash for {file_path}: {e}")
        return None

def scan_existing_files():
    """Scan existing files in the shared folder and add their hashes to the dictionary."""
    try:
        for root, _, files in os.walk(SHARED_FOLDER):
            for filename in files:
                file_path = os.path.join(root, filename)
                file_hash = calculate_file_hash(file_path)
                if file_hash:
                    with file_hashes_lock:
                        file_hashes[file_hash] = {
                            'timestamp': os.path.getmtime(file_path),
                            'filename': filename
                        }
        logging.info(f"Scanned {len(file_hashes)} existing files in shared folder")
    except Exception as e:
        logging.error(f"Error scanning existing files: {e}")

def get_all_local_ips():
    """Get all IP addresses of this machine to avoid self-connections."""
    local_ips = set()
    try:
        # Get IP from hostname
        local_ips.add(socket.gethostbyname(hostname))
        
        # Get all network interfaces
        for interface in socket.getaddrinfo(socket.gethostname(), None):
            ip = interface[4][0]
            # Skip IPv6 addresses and localhost
            if ':' not in ip and ip != '127.0.0.1':
                local_ips.add(ip)
                
        # On Windows, try additional method
        if platform.system() == "Windows":
            local_host = socket.gethostbyname_ex(socket.gethostname())
            local_ips.update(local_host[2])
    except Exception as e:
        logging.error(f"Error getting local IPs: {e}")
        # At minimum, add the known local IP
        local_ips.add(local_ip)
    
    return local_ips

def broadcast_presence():
    """Broadcast this peer's presence to the network."""
    while running:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        message = f"DISCOVER:{local_ip}:{hostname}:{DEVICE_ID}"
        
        try:
            sock.sendto(message.encode(), ('<broadcast>', BROADCAST_PORT))
            # Also try subnet broadcast addresses
            local_ips = get_all_local_ips()
            for ip in local_ips:
                try:
                    # Calculate broadcast address for this IP's subnet
                    ip_obj = ipaddress.IPv4Address(ip)
                    network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
                    broadcast = str(network.broadcast_address)
                    sock.sendto(message.encode(), (broadcast, BROADCAST_PORT))
                except Exception:
                    pass  # Skip if we can't calculate broadcast for this IP
                    
            logging.debug(f"Broadcasting presence: {message}")
            time.sleep(HEARTBEAT_INTERVAL)
        except Exception as e:
            logging.error(f"Error broadcasting presence: {e}")
        finally:
            sock.close()

def listen_for_peers():
    """Listen for peer discovery messages."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", BROADCAST_PORT))  # Listen on all interfaces

    logging.info("Listening for peer discovery messages on port 5001...")
    
    # Get all local IP addresses to avoid adding self as peer
    local_ips = get_all_local_ips()
    logging.info(f"Local IPs detected: {local_ips}")
    
    # Also track our own device ID
    own_device_ids = {DEVICE_ID}

    while running:
        try:
            data, addr = sock.recvfrom(1024)  # Receive message
            message = data.decode()
            peer_ip = addr[0]  # Get the sender's IP address

            if message.startswith("DISCOVER:"):
                parts = message.split(":")
                if len(parts) >= 4:  # Ensure we have IP, hostname, and device ID
                    advertised_ip = parts[1]
                    peer_hostname = parts[2]
                    peer_device_id = parts[3]
                    
                    # Skip if this is from ourselves (either by IP or device ID)
                    if peer_ip in local_ips or advertised_ip in local_ips or peer_device_id in own_device_ids:
                        continue
                        
                    with peers_lock:
                        current_time = time.time()
                        # Update peer info with timestamp
                        peers[peer_ip] = {
                            'last_seen': current_time,
                            'hostname': peer_hostname,
                            'device_id': peer_device_id
                        }
                        logging.info(f"Peer updated: {peer_ip} ({peer_hostname})")
                        
            # Periodically clean up stale peers
            if random.random() < 0.1:  # ~10% chance each iteration to reduce overhead
                cleanup_stale_peers()
                    
        except Exception as e:
            logging.error(f"Error listening for peers: {e}")

def cleanup_stale_peers():
    """Remove peers that haven't been seen recently."""
    current_time = time.time()
    with peers_lock:
        stale_peers = [ip for ip, data in peers.items() 
                      if current_time - data['last_seen'] > PEER_TIMEOUT]
        for ip in stale_peers:
            logging.info(f"Removing stale peer: {ip}")
            peers.pop(ip, None)

def handle_file_transfer():
    """Listen for incoming file transfers."""
    global receiving_file

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", FILE_PORT))  # Bind to all interfaces
    sock.listen(5)  # Allow up to 5 queued connections
    sock.settimeout(1)  # Add timeout to allow for graceful shutdown

    logging.info(f"Listening for file transfers on port {FILE_PORT}...")

    while running:
        try:
            conn, addr = sock.accept()
            # Handle each connection in a separate thread
            transfer_thread = threading.Thread(
                target=process_incoming_file,
                args=(conn, addr),
                daemon=True
            )
            transfer_thread.start()
        except socket.timeout:
            continue  # Just a timeout, continue the loop
        except Exception as e:
            if running:  # Only log if we're still supposed to be running
                logging.error(f"Error accepting connection: {e}")
    
    sock.close()
    logging.info("File transfer server stopped")

def process_incoming_file(conn, addr):
    """Process an incoming file transfer in its own thread."""
    global receiving_file
    receiving_file = True
    
    try:
        conn.settimeout(10)  # Set timeout for receiving data
        
        # First receive the file hash to check if we already have it
        file_hash = conn.recv(64).decode().strip()
        
        # Check if we already have this file
        with file_hashes_lock:
            if file_hash in file_hashes:
                logging.info(f"Already have file with hash {file_hash}, skipping download")
                conn.sendall(b"HAVE")
                receiving_file = False
                conn.close()
                return
            
        # We don't have this file, proceed with receiving it
        conn.sendall(b"PROCEED")
            
        # Now receive the filename
        filename = conn.recv(1024).decode()

        # Add a timestamp to the filename to avoid overwriting
        base_name, ext = os.path.splitext(filename)
        new_filename = f"{base_name}_{int(time.time())}{ext}"
        file_path = os.path.join(SHARED_FOLDER, new_filename)
        absolute_path = os.path.abspath(file_path)

        logging.info(f"Receiving file: {new_filename} from {addr[0]}")

        # Read file size first (if sent)
        file_size_bytes = conn.recv(16)
        try:
            file_size = int(file_size_bytes.decode().strip())
            logging.info(f"File size: {file_size} bytes")
        except:
            file_size = None  # Size not provided
            
        # Now receive the file
        received_bytes = 0
        with open(file_path, "wb") as f:
            while True:
                data = conn.recv(8192)  # Larger buffer for faster transfers
                if not data:
                    break
                f.write(data)
                received_bytes += len(data)
                
                # Log progress for large files
                if file_size and file_size > 1048576 and received_bytes % 1048576 < 8192:  # ~1MB intervals
                    percent = (received_bytes / file_size) * 100
                    logging.info(f"Receiving file: {percent:.1f}% ({received_bytes}/{file_size})")

        logging.info(f"File received: {new_filename} ({received_bytes} bytes)")
        
        # Add the file to our hash dictionary
        with file_hashes_lock:
            file_hashes[file_hash] = {
                'timestamp': time.time(),
                'filename': filename
            }
        
        # Add the received file to the set of recently received files
        with recently_received_lock:
            recently_received_files[absolute_path] = time.time()
            logging.info(f"Added to recently received files: {absolute_path}")
            
        conn.sendall(b"ACK")  # Send acknowledgment
    except Exception as e:
        logging.error(f"Error processing incoming file: {e}")
    finally:
        conn.close()
        receiving_file = False

def send_file(file_path):
    """Send a file to all peers."""
    global receiving_file
    
    with peers_lock:
        active_peers = list(peers.keys())
    
    if not active_peers:
        logging.warning("No peers available. Waiting for discovery...")
        return

    # Skip sending if the file is being received
    if receiving_file:
        logging.info(f"Skipping send for file being received: {file_path}")
        return

    # Check if file still exists and is readable
    if not os.path.exists(file_path) or not os.access(file_path, os.R_OK):
        logging.warning(f"File no longer exists or isn't readable: {file_path}")
        return

    # Calculate file hash
    file_hash = calculate_file_hash(file_path)
    if not file_hash:
        logging.error(f"Failed to calculate hash for {file_path}")
        return
        
    # Store in our hash dictionary
    filename = os.path.basename(file_path)
    with file_hashes_lock:
        file_hashes[file_hash] = {
            'timestamp': time.time(),
            'filename': filename
        }

    filesize = os.path.getsize(file_path)
    logging.info(f"Sending {filename} ({filesize} bytes) to {len(active_peers)} peers")

    # Read the file content once before sending to all peers
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
    except Exception as e:
        logging.error(f"Failed to read file {file_path}: {e}")
        return

    for peer in active_peers:
        # Send to each peer in a separate thread to not block the main thread
        send_thread = threading.Thread(
            target=send_file_to_peer,
            args=(peer, filename, filesize, file_data, file_hash),
            daemon=True
        )
        send_thread.start()

def send_file_to_peer(peer_ip, filename, filesize, file_data, file_hash):
    """Send a file to a specific peer."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)  # Set timeout for connection
        sock.connect((peer_ip, FILE_PORT))
        
        # First send the file hash to let the peer decide if it needs the file
        sock.send(file_hash.encode().ljust(64))
        
        # Wait for response - should the transfer proceed?
        response = sock.recv(16).decode().strip()
        if response == "HAVE":
            logging.info(f"Peer {peer_ip} already has file {filename}, skipping transfer")
            sock.close()
            return
        
        # Send filename
        sock.send(filename.encode())
        time.sleep(0.2)  # Short pause
        
        # Send file size
        sock.send(f"{filesize}".encode().ljust(16))
        time.sleep(0.2)  # Short pause
        
        # Send file data
        sock.sendall(file_data)
        
        # Wait for acknowledgment
        try:
            sock.settimeout(5)
            ack = sock.recv(16)
            if ack == b"ACK":
                logging.info(f"File sent successfully to {peer_ip}: {filename}")
            else:
                logging.warning(f"Unexpected acknowledgment from {peer_ip}: {ack}")
        except:
            logging.warning(f"No acknowledgment received from {peer_ip}")
            
    except Exception as e:
        logging.error(f"Failed to send file to {peer_ip}: {e}")
        
        # If connection failed, check if peer is still responsive
        with peers_lock:
            if peer_ip in peers:
                # Mark the peer for potential cleanup on next cycle
                peers[peer_ip]['last_seen'] = time.time() - PEER_TIMEOUT + 5
    finally:
        sock.close()

class FileChangeHandler(FileSystemEventHandler):
    """Monitor the shared folder for new or modified files."""
    def process_file_event(self, event):
        if event.is_directory:
            return
            
        abs_path = os.path.abspath(event.src_path)
        
        # Check if this is a temporary file
        if any(abs_path.endswith(ext) for ext in ['.tmp', '.crdownload', '.part']):
            logging.debug(f"Ignoring temporary file: {abs_path}")
            return
        
        # Check if this is a recently received file
        current_time = time.time()
        with recently_received_lock:
            if abs_path in recently_received_files:
                time_diff = current_time - recently_received_files[abs_path]
                if time_diff < 30:  # Within 30 seconds
                    logging.info(f"Skipping recently received file: {abs_path}")
                    return
                else:
                    # Clean up old entries
                    recently_received_files.pop(abs_path, None)
        
        # Wait to ensure file is fully written
        wait_for_file_stability(abs_path)
        
        # Send the file
        send_file(abs_path)
    
    def on_created(self, event):
        self.process_file_event(event)

    def on_modified(self, event):
        self.process_file_event(event)

def wait_for_file_stability(file_path, timeout=10, check_interval=0.5):
    """Wait until file size stops changing (indicating it's fully written)."""
    start_time = time.time()
    last_size = -1
    
    while time.time() - start_time < timeout:
        try:
            current_size = os.path.getsize(file_path)
            if current_size == last_size and current_size > 0:
                return True  # File size stable
            last_size = current_size
            time.sleep(check_interval)
        except Exception:
            # File might be temporarily locked or deleted
            time.sleep(check_interval)
    
    logging.warning(f"File stability timeout for {file_path}")
    return False  # Timeout reached

def cleanup_recent_files():
    """Periodically clean up the recently received files list."""
    while running:
        time.sleep(60)  # Run every minute
        current_time = time.time()
        with recently_received_lock:
            expired = [path for path, timestamp in recently_received_files.items() 
                      if current_time - timestamp > 300]  # 5 minutes expiration
            for path in expired:
                recently_received_files.pop(path, None)
        
        # Also clean up old file hashes, but keep a longer history
        with file_hashes_lock:
            expired_hashes = [h for h, data in file_hashes.items()
                            if current_time - data['timestamp'] > 86400]  # 24 hours
            for h in expired_hashes:
                file_hashes.pop(h, None)
        
        logging.debug(f"Cleaned up {len(expired)} entries from recently received files")

def start_file_watcher():
    """Start monitoring the shared folder for changes."""
    event_handler = FileChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, SHARED_FOLDER, recursive=True)  # Also watch subdirectories
    observer.start()
    
    logging.info(f"File watcher started for directory: {os.path.abspath(SHARED_FOLDER)}")
    return observer

def display_status():
    """Display status information about peers and transfers."""
    with peers_lock:
        peer_count = len(peers)
        peer_list = [f"{ip} ({data['hostname']})" for ip, data in peers.items()]
    
    logging.info(f"Status: Connected to {peer_count} peers")
    if peer_list:
        logging.info(f"Peers: {', '.join(peer_list)}")
    else:
        logging.info("No peers connected")

# Import some additional modules we need
import random

# Scan existing files at startup
scan_existing_files()

# Start all threads
broadcast_thread = threading.Thread(target=broadcast_presence, daemon=True)
listen_thread = threading.Thread(target=listen_for_peers, daemon=True)
transfer_thread = threading.Thread(target=handle_file_transfer, daemon=True)
cleanup_thread = threading.Thread(target=cleanup_recent_files, daemon=True)

broadcast_thread.start()
listen_thread.start()
transfer_thread.start()
cleanup_thread.start()

# Start file watcher and get the observer reference
file_observer = start_file_watcher()

logging.info("All threads started successfully!")
logging.info(f"Device ID: {DEVICE_ID}")
logging.info(f"Local IP: {local_ip}, hostname: {hostname}")
logging.info(f"Shared folder: {os.path.abspath(SHARED_FOLDER)}")

try:
    # Main loop - display status periodically
    while running:
        display_status()
        time.sleep(10)
except KeyboardInterrupt:
    logging.info("Shutting down...")
    running = False
    file_observer.stop()
    file_observer.join()