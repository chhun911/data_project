import socket
import threading
import time
import os
import logging
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Network settings
BROADCAST_PORT = 5001  # Port for peer discovery
FILE_PORT = 5002       # Port for file transfers
SHARED_FOLDER = "shared/"  # Folder to share files

# Get local IP and hostname
hostname = socket.gethostname()
local_ip = socket.gethostbyname(hostname)

# Global variables
peers = set()  # Set of discovered peers
peers_lock = threading.Lock()  # Lock for thread-safe access to peers
running = True  # Flag to control threads
receiving_file = False  # Flag to track if a file is being received

# Add a set to track recently received files
recently_received_files = set()
recently_received_lock = threading.Lock()

# Ensure shared folder exists
os.makedirs(SHARED_FOLDER, exist_ok=True)

def broadcast_presence():
    """Broadcast this peer's presence to the network."""
    while running:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.2)
        message = f"DISCOVER:{local_ip}:{hostname}"
        
        try:
            sock.sendto(message.encode(), ('<broadcast>', BROADCAST_PORT))
            logging.info(f"Broadcasting presence: {message}")
            time.sleep(5)  # Broadcast every 5 seconds
        except Exception as e:
            logging.error(f"Error broadcasting presence: {e}")
        finally:
            sock.close()

def listen_for_peers():
    """Listen for peer discovery messages."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", BROADCAST_PORT))  # Listen on all interfaces

    logging.info("Listening for peer discovery messages on port 5001...")
    
    # Get all local IP addresses to avoid adding self as peer
    local_ips = get_all_local_ips()
    logging.info(f"Local IPs detected: {local_ips}")
    
    # Only store the main local IP for filtering
    main_local_ip = local_ip
    logging.info(f"Main local IP used for filtering: {main_local_ip}")

    while running:
        try:
            data, addr = sock.recvfrom(1024)  # Receive message
            message = data.decode()
            peer_ip = addr[0]  # Get the sender's IP address

            logging.info(f"Received message: {message} from {peer_ip}")

            # Check if the IP is already in our peer list
            with peers_lock:
                is_known_peer = peer_ip in peers
            
            # Extract the IP from the message (not the sender's IP)
            if message.startswith("DISCOVER:"):
                parts = message.split(":")
                if len(parts) >= 2:
                    message_ip = parts[1]
                    
                    # If message IP matches our main local IP, skip adding this peer
                    if message_ip == main_local_ip:
                        logging.info(f"Ignoring self-broadcast from {peer_ip} with my IP {main_local_ip}")
                        continue
                    
                    # Otherwise, consider adding the sender IP as a peer
                    with peers_lock:
                        if peer_ip not in peers:
                            peers.add(peer_ip)
                            logging.info(f"New peer added: {peer_ip}")
                        else:
                            logging.info(f"Peer {peer_ip} already known.")
        except Exception as e:
            logging.error(f"Error listening for peers: {e}")

def get_all_local_ips():
    """Get all IP addresses of this machine to avoid self-connections."""
    local_ips = set()
    try:
        # Get hostname
        hostname = socket.gethostname()
        # Get IP from hostname
        local_ips.add(socket.gethostbyname(hostname))
        
        # Get all network interfaces
        for interface in socket.getaddrinfo(socket.gethostname(), None):
            ip = interface[4][0]
            # Skip IPv6 addresses and localhost
            if ':' not in ip and ip != '127.0.0.1':
                local_ips.add(ip)
    except Exception as e:
        logging.error(f"Error getting local IPs: {e}")
        # At minimum, add the known local IP
        local_ips.add(local_ip)
    
    return local_ips

def handle_file_transfer():
    """Listen for incoming file transfers."""
    global receiving_file

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("0.0.0.0", FILE_PORT))  # Bind to all interfaces
    sock.listen()

    logging.info(f"Listening for file transfers on port {FILE_PORT}...")

    while running:
        try:
            conn, addr = sock.accept()
            logging.info(f"Connection accepted from {addr[0]}:{addr[1]}")
            filename = conn.recv(1024).decode()

            # Add a timestamp to the filename to avoid overwriting
            base_name, ext = os.path.splitext(filename)
            new_filename = f"{base_name}_{int(time.time())}{ext}"
            file_path = os.path.join(SHARED_FOLDER, new_filename)
            absolute_path = os.path.abspath(file_path)

            # Set the receiving_file flag to True
            receiving_file = True
            logging.info(f"Receiving file: {new_filename} from {addr[0]}")

            with open(file_path, "wb") as f:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    f.write(data)

            logging.info(f"File received: {new_filename}")
            
            # Add the received file to the set of recently received files
            with recently_received_lock:
                recently_received_files.add(absolute_path)
                logging.info(f"Added to recently received files: {absolute_path}")
                
            conn.sendall(b"ACK")  # Send acknowledgment
            conn.close()

            # Reset the receiving_file flag after the file is received
            receiving_file = False
        except Exception as e:
            logging.error(f"Error handling file transfer: {e}")
            receiving_file = False

def send_file(file_path):
    """Send a file to all peers."""
    global receiving_file

    if not peers:
        logging.warning("No peers available. Waiting for discovery...")
        return

    # Skip sending if the file is being received
    if receiving_file:
        logging.info(f"Skipping send for file being received: {file_path}")
        return

    filename = os.path.basename(file_path)
    logging.info(f"Sending {filename} to peers: {peers}")

    for peer in peers:
        try:
            # Read the file content before sending
            with open(file_path, "rb") as f:
                file_data = f.read()

            # Send the file
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((peer, FILE_PORT))
            sock.send(filename.encode())
            time.sleep(1)  # Ensure filename is sent first
            sock.sendall(file_data)  # Send the entire file content
            sock.close()
            logging.info(f"File sent to {peer}: {filename}")
        except Exception as e:
            logging.error(f"Failed to send file to {peer}: {e}")

class FileChangeHandler(FileSystemEventHandler):
    """Monitor the shared folder for new or modified files."""
    def on_created(self, event):
        if not event.is_directory:
            abs_path = os.path.abspath(event.src_path)
            logging.info(f"New file detected: {abs_path}")
            
            # Check if this is a recently received file
            with recently_received_lock:
                if abs_path in recently_received_files:
                    logging.info(f"Skipping recently received file: {abs_path}")
                    recently_received_files.remove(abs_path)
                    return
                    
            time.sleep(5)  # Wait for the file to be fully written
            send_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            abs_path = os.path.abspath(event.src_path)
            logging.info(f"File modified: {abs_path}")
            
            # Check if this is a recently received file
            with recently_received_lock:
                if abs_path in recently_received_files:
                    logging.info(f"Skipping recently received file: {abs_path}")
                    recently_received_files.remove(abs_path)
                    return
                    
            time.sleep(5)  # Wait for the file to be fully written
            send_file(event.src_path)

def start_file_watcher():
    """Start monitoring the shared folder for changes."""
    event_handler = FileChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, SHARED_FOLDER, recursive=False)
    observer.start()
    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

# Start all threads
threading.Thread(target=broadcast_presence, daemon=True).start()
threading.Thread(target=listen_for_peers, daemon=True).start()
threading.Thread(target=handle_file_transfer, daemon=True).start()
threading.Thread(target=start_file_watcher, daemon=True).start()

logging.info("All threads started successfully!")
try:
    while running:
        time.sleep(1)
except KeyboardInterrupt:
    logging.info("Shutting down...")
    running = False