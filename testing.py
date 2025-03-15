import socket
import threading
import time
import os
import logging
import sys
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

# Add timeout for recently received files (in seconds)
RECEIVED_FILE_TIMEOUT = 30

# Ensure shared folder exists
os.makedirs(SHARED_FOLDER, exist_ok=True)

def get_all_local_ips():
    """Get all IP addresses of this machine to avoid self-connections."""
    local_ips = set()
    try:
        # Always start with the main IP
        local_ips.add(local_ip)
        
        # Add localhost
        local_ips.add('127.0.0.1')
        
        # Get IP addresses from socket library
        for addrinfo in socket.getaddrinfo(socket.gethostname(), None):
            ip = addrinfo[4][0]
            if '.' in ip:  # Only include IPv4 addresses
                local_ips.add(ip)
        
        # Windows-specific method
        if os.name == 'nt':
            try:
                import subprocess
                output = subprocess.check_output("ipconfig", shell=True).decode('utf-8')
                for line in output.split('\n'):
                    if 'IPv4 Address' in line:
                        ip = line.split(':')[-1].strip()
                        local_ips.add(ip)
            except Exception as e:
                logging.error(f"Error getting Windows IPs: {e}")
    
    except Exception as e:
        logging.error(f"Error getting local IPs: {e}")
    
    return local_ips

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

    while running:
        try:
            data, addr = sock.recvfrom(1024)  # Receive message
            message = data.decode()
            peer_ip = addr[0]  # Get the sender's IP address

            logging.info(f"Received message: {message} from {peer_ip}")

            # Check if this is a DISCOVER message
            if message.startswith("DISCOVER:"):
                # Extract the IP from the message
                parts = message.split(":")
                if len(parts) >= 2:
                    message_ip = parts[1]
                    
                    # Debug info about the message
                    logging.info(f"Message IP: {message_ip}, Sender IP: {peer_ip}")
                    
                    # Skip local IPs and loopback
                    if peer_ip in local_ips or peer_ip == "127.0.0.1":
                        logging.info(f"Ignoring message from local IP: {peer_ip}")
                        continue
                    
                    # Skip if the source IP is our own IP
                    if message_ip == local_ip:
                        logging.info(f"Ignoring peer claiming to be me: {message_ip}")
                        continue
                        
                    # Add non-local peer to our list
                    with peers_lock:
                        if peer_ip not in peers:
                            peers.add(peer_ip)
                            logging.info(f"New peer added: {peer_ip}")
                        else:
                            logging.info(f"Peer {peer_ip} already known.")
        except Exception as e:
            logging.error(f"Error listening for peers: {e}")

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
            
            try:
                # Set timeout for receiving filename
                conn.settimeout(5)
                filename_data = conn.recv(1024)
                if not filename_data:
                    logging.warning("No filename received, closing connection")
                    conn.close()
                    continue
                    
                filename = filename_data.decode()
            except socket.timeout:
                logging.warning("Timeout waiting for filename, closing connection")
                conn.close()
                continue
            except Exception as e:
                logging.error(f"Error receiving filename: {e}")
                conn.close()
                continue

            # Add a timestamp to the filename to avoid overwriting
            base_name, ext = os.path.splitext(filename)
            new_filename = f"{base_name}_{int(time.time())}{ext}"
            file_path = os.path.join(SHARED_FOLDER, new_filename)
            absolute_path = os.path.abspath(file_path)

            # Set the receiving_file flag to True
            receiving_file = True
            logging.info(f"Receiving file: {new_filename} from {addr[0]}")

            try:
                with open(file_path, "wb") as f:
                    # Reset timeout for file data
                    conn.settimeout(30)  # 30 second timeout for data
                    
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
                    
                # Send acknowledgment
                try:
                    conn.sendall(b"ACK")
                except Exception as e:
                    logging.error(f"Error sending ACK: {e}")
            except Exception as e:
                logging.error(f"Error receiving file data: {e}")
            finally:
                conn.close()
                receiving_file = False
                
        except Exception as e:
            logging.error(f"Error in file transfer connection: {e}")
            receiving_file = False

def send_file(file_path):
    """Send a file to all peers."""
    # Skip if the path doesn't exist anymore (file deleted)
    if not os.path.exists(file_path):
        logging.warning(f"File {file_path} no longer exists, skipping send")
        return
        
    # Skip if we're currently receiving a file
    if receiving_file:
        logging.info(f"Skipping send for file being received: {file_path}")
        return

    # Get active peers
    active_peers = set()
    local_ips = get_all_local_ips()
    
    with peers_lock:
        for peer in peers:
            if peer not in local_ips and peer != "127.0.0.1":
                active_peers.add(peer)
    
    if not active_peers:
        logging.warning("No peers available. Waiting for discovery...")
        return

    filename = os.path.basename(file_path)
    logging.info(f"Sending {filename} to peers: {active_peers}")

    for peer in active_peers:
        try:
            # Read the file content
            with open(file_path, "rb") as f:
                file_data = f.read()

            # Create socket with timeout
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)  # 10 second connection timeout
            
            # Connect to peer
            sock.connect((peer, FILE_PORT))
            
            # Send filename
            sock.send(filename.encode())
            time.sleep(0.5)  # Short pause
            
            # Send file data
            sock.sendall(file_data)
            sock.shutdown(socket.SHUT_WR)  # Signal end of data
            
            # Wait for acknowledgment
            try:
                sock.settimeout(5)
                ack = sock.recv(1024)
                if ack == b"ACK":
                    logging.info(f"File sent to {peer}: {filename} (acknowledged)")
                else:
                    logging.warning(f"File sent to {peer}: {filename} (unexpected response)")
            except socket.timeout:
                logging.warning(f"File sent to {peer}: {filename} (no acknowledgment)")
            except Exception as e:
                logging.error(f"Error receiving acknowledgment from {peer}: {e}")
                
            sock.close()
        except ConnectionRefusedError:
            logging.error(f"Connection refused by {peer} - peer may be offline")
            # Remove unreachable peer
            with peers_lock:
                if peer in peers:
                    peers.remove(peer)
                    logging.info(f"Removed unreachable peer: {peer}")
        except Exception as e:
            logging.error(f"Failed to send file to {peer}: {e}")

class FileChangeHandler(FileSystemEventHandler):
    """Monitor the shared folder for new or modified files."""
    def on_created(self, event):
        if not event.is_directory:
            abs_path = os.path.abspath(event.src_path)
            logging.info(f"New file detected: {abs_path}")
            
            # Skip files that are currently being received
            if receiving_file:
                logging.info(f"Skipping - currently receiving a file")
                return
                
            # Check if this is a recently received file
            with recently_received_lock:
                if abs_path in recently_received_files:
                    logging.info(f"Skipping recently received file: {abs_path}")
                    recently_received_files.remove(abs_path)
                    return
                    
            # Wait for the file to be fully written
            time.sleep(5)
            
            # Check if file still exists (might have been deleted)
            if os.path.exists(event.src_path):
                send_file(event.src_path)
            else:
                logging.warning(f"File {event.src_path} no longer exists, skipping send")

    def on_modified(self, event):
        if not event.is_directory:
            abs_path = os.path.abspath(event.src_path)
            logging.info(f"File modified: {abs_path}")
            
            # Skip files that are currently being received
            if receiving_file:
                logging.info(f"Skipping - currently receiving a file")
                return
                
            # Check if this is a recently received file
            with recently_received_lock:
                if abs_path in recently_received_files:
                    logging.info(f"Skipping recently received file: {abs_path}")
                    recently_received_files.remove(abs_path)
                    return
                    
            # Wait for the file to be fully written
            time.sleep(5)
            
            # Check if file still exists (might have been deleted)
            if os.path.exists(event.src_path):
                send_file(event.src_path)
            else:
                logging.warning(f"File {event.src_path} no longer exists, skipping send")

def start_file_watcher():
    """Start monitoring the shared folder for changes."""
    event_handler = FileChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, SHARED_FOLDER, recursive=False)
    observer.start()
    logging.info(f"File watcher started for folder: {os.path.abspath(SHARED_FOLDER)}")
    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

def clean_received_files():
    """Periodically clean up the recently received files set."""
    while running:
        time.sleep(RECEIVED_FILE_TIMEOUT)
        with recently_received_lock:
            if recently_received_files:
                logging.info(f"Clearing {len(recently_received_files)} old received files from tracking")
                recently_received_files.clear()

def debug_peers():
    """Debug function to print current peer list periodically."""
    while running:
        with peers_lock:
            if peers:
                logging.info(f"Current peers ({len(peers)}): {peers}")
            else:
                logging.warning("No peers currently in list")
        
        # Also log the recently received files
        with recently_received_lock:
            if recently_received_files:
                logging.info(f"Recently received files: {recently_received_files}")
        
        time.sleep(10)  # Print every 10 seconds

def check_network_config():
    """Function to check and log network configuration at startup."""
    logging.info("============ NETWORK CONFIGURATION ============")
    logging.info(f"Python version: {sys.version}")
    logging.info(f"OS: {os.name} ({sys.platform})")
    logging.info(f"Hostname: {hostname}")
    logging.info(f"Main IP: {local_ip}")
    
    # Get all local IPs for better debugging
    local_ips = get_all_local_ips()
    logging.info(f"All local IPs: {local_ips}")
    
    # Check network connectivity
    try:
        # Try to resolve google.com to verify internet connectivity
        socket.gethostbyname("google.com")
        logging.info("Internet connectivity: OK")
    except Exception:
        logging.warning("Internet connectivity: FAIL")
    
    logging.info("==============================================")

if __name__ == "__main__":
    # First check network configuration
    check_network_config()
    
    # Parse command-line arguments
    import argparse
    parser = argparse.ArgumentParser(description="P2P File Sharing Application")
    parser.add_argument("--watch", action="store_true", help="Enable file watcher")
    parser.add_argument("--test-peer", type=str, help="Add a specific IP as test peer")
    args = parser.parse_args()
    
    # Enable file watcher based on command-line argument
    ENABLE_FILE_WATCHER = args.watch
    
    # Add test peer if specified
    if args.test_peer:
        local_ips = get_all_local_ips()
        if args.test_peer not in local_ips:
            peers.add(args.test_peer)
            logging.info(f"Added test peer: {args.test_peer}")
        else:
            logging.warning(f"Not adding {args.test_peer} as test peer because it's a local IP")
    
    # Start all threads
    threads = [
        threading.Thread(target=broadcast_presence, daemon=True),
        threading.Thread(target=listen_for_peers, daemon=True),
        threading.Thread(target=handle_file_transfer, daemon=True),
        threading.Thread(target=debug_peers, daemon=True),
        threading.Thread(target=clean_received_files, daemon=True)
    ]
    
    if ENABLE_FILE_WATCHER:
        threads.append(threading.Thread(target=start_file_watcher, daemon=True))
        logging.info("File watcher enabled")
    else:
        logging.warning("File watcher disabled - use --watch flag to enable")
    
    for thread in threads:
        thread.start()
    
    logging.info("All threads started successfully!")
    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutting down...")
        running = False