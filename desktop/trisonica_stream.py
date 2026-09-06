#!/usr/bin/env python3
"""
Simple Trisonica raw data stream viewer with auto port detection.
Similar to 'screen /dev/ttyUSB0 115200' but automatically finds the Trisonica port.

Usage: python3 trisonica_stream.py
       or: ./trisonica_stream.py
"""

import serial
import glob
import sys
import signal
from typing import Optional, List

DEFAULT_BAUD_RATE = 115200

def find_serial_ports() -> List[str]:
    """Find all available serial ports on Linux"""
    patterns = [
        '/dev/ttyUSB*',      # USB-to-serial adapters
        '/dev/ttyACM*',      # USB CDC devices
        '/dev/ttyS*',        # Traditional serial ports
        '/dev/serial/by-id/*'  # Persistent device names
    ]

    ports = []
    for pattern in patterns:
        ports.extend(glob.glob(pattern))

    return sorted(set(ports))

def detect_trisonica_port() -> Optional[str]:
    """Auto-detect Trisonica device port"""
    ports = find_serial_ports()

    if not ports:
        print("ERROR: No serial ports found!")
        return None

    print(f"Found {len(ports)} serial port(s), testing for Trisonica...")

    # Test each port
    for port in ports:
        try:
            print(f"Testing {port}...", end=" ", flush=True)
            ser = serial.Serial(port, DEFAULT_BAUD_RATE, timeout=2)

            # Read several lines to detect Trisonica
            trisonica_detected = False
            for _ in range(10):  # Multiple attempts
                try:
                    line = ser.readline().decode('ascii', errors='ignore').strip()
                    if line and any(param in line for param in ['S ', 'S2', 'D ', 'T ', 'U ', 'V ']):
                        trisonica_detected = True
                        break
                except:
                    continue

            ser.close()

            if trisonica_detected:
                print("Trisonica found.")
                return port
            else:
                print("No Trisonica data detected on this port.")

        except Exception as e:
            print(f"Error while probing port: {e}")

    print("ERROR: No Trisonica devices found on any port")
    return None

def stream_raw_data(port: str):
    """Stream raw data from Trisonica device"""
    try:
        print(f"\nConnecting to Trisonica on {port} at {DEFAULT_BAUD_RATE} baud...")
        ser = serial.Serial(port, DEFAULT_BAUD_RATE, timeout=1)
        print("Connected! Streaming raw data (Press Ctrl+C to exit):\n")
        print("-" * 80)

        while True:
            try:
                line = ser.readline().decode('ascii', errors='ignore').strip()
                if line:
                    print(line)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Error reading data: {e}")
                break

    except serial.SerialException as e:
        print(f"ERROR: Could not connect to {port}: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ser.close()
            print("\n" + "-" * 80)
            print("Connection closed.")
        except:
            pass

def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully"""
    print("\n\nShutting down...")
    sys.exit(0)

def main():
    """Main function"""
    signal.signal(signal.SIGINT, signal_handler)

    print("Trisonica Auto Stream - Automatic port detection")
    print("=" * 50)

    # Auto-detect Trisonica port
    port = detect_trisonica_port()
    if not port:
        sys.exit(1)

    # Start streaming
    stream_raw_data(port)

if __name__ == '__main__':
    main()
