#!/usr/bin/env python3
"""
TriSonica Data Logger for Linux
Optimized for Linux desktop and server environments with systemd integration
"""

import serial
import datetime
import time
import sys
import signal
import re
import os
import glob
import argparse
import platform
import subprocess
from collections import deque
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from pathlib import Path

# Linux optimized imports
from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.text import Text
from rich.align import Align
from rich import box
from rich.columns import Columns
from rich.tree import Tree

# --- Configuration ---
DEFAULT_BAUD_RATE = 115200
MAX_DATAPOINTS = 1500  # Balanced for Linux systems
UPDATE_INTERVAL = 0.05  # Fast updates for Linux
LOG_ROTATION_SIZE = 50 * 1024 * 1024  # 50MB logs

@dataclass
class Config:
    serial_port: str = "auto"
    baud_rate: int = DEFAULT_BAUD_RATE
    log_dir: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "OUTPUT")
    show_raw_data: bool = False
    save_statistics: bool = True
    plot_enabled: bool = False
    max_log_size: int = LOG_ROTATION_SIZE
    enable_notifications: bool = True

@dataclass
class DataPoint:
    timestamp: datetime.datetime
    raw_data: str
    parsed_data: Dict[str, str] = field(default_factory=dict)

@dataclass
class Statistics:
    min_val: float = 0.0
    max_val: float = 0.0
    mean_val: float = 0.0
    current_val: float = 0.0
    std_dev: float = 0.0
    count: int = 0
    # Running accumulators so mean/std cover the whole session, consistent with
    # the lifetime min/max/count reported alongside them.
    _sum: float = 0.0
    _sum_sq: float = 0.0
    # Recent samples, kept only for the live sparkline display.
    values: deque = field(default_factory=lambda: deque(maxlen=150))

class TrisonicaDataLoggerLinux:
    def __init__(self, config: Config):
        self.config = config
        self.serial_port = None
        self.log_file = None
        self.stats_file = None
        self.console = Console()
        self.running = False
        self.start_time = time.time()
        
        # Data storage
        self.data_points = deque(maxlen=MAX_DATAPOINTS)
        self.point_count = 0
        self.stats = {}
        
        # CSV column management
        self.csv_columns = ['timestamp']
        self.csv_headers_written = False
        self.disk_full = False  # set when a log write fails (full disk / device removed)
        
        # Linux specific
        self.last_update = time.time()
        self.update_rate = 0.0
        self.system_info = self.get_system_info()
        
        # Visualization data storage
        self.viz_data = {
            'wind_speed': deque(maxlen=75),
            'temperature': deque(maxlen=75),
            'wind_direction': deque(maxlen=75),
            'timestamps': deque(maxlen=75)
        }
        
        # Ensure log directory exists
        os.makedirs(config.log_dir, exist_ok=True)
        
        # Setup logging
        self.setup_logging()
        
        # Signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        if hasattr(signal, 'SIGUSR1'):
            signal.signal(signal.SIGUSR1, self.statistics_handler)
        
        # Print startup banner
        self.print_startup_banner()

    def get_system_info(self) -> Dict[str, str]:
        """Get Linux system information"""
        try:
            # Get distribution info
            try:
                with open('/etc/os-release', 'r') as f:
                    os_info = f.read()
                    distro = 'Unknown'
                    for line in os_info.split('\n'):
                        if line.startswith('PRETTY_NAME='):
                            distro = line.split('=', 1)[1].strip('"')
                            break
            except:
                distro = platform.platform()
            
            # Get hostname
            hostname = platform.node()
            
            # Get kernel version
            kernel = platform.release()
            
            # Get CPU info
            try:
                with open('/proc/cpuinfo', 'r') as f:
                    cpu_info = f.read()
                    cpu_model = 'Unknown'
                    for line in cpu_info.split('\n'):
                        if line.startswith('model name'):
                            cpu_model = line.split(':', 1)[1].strip()
                            break
            except:
                cpu_model = platform.processor()
            
            return {
                'hostname': hostname,
                'distro': distro,
                'kernel': kernel,
                'cpu': cpu_model,
                'platform': 'Linux',
                'python_version': sys.version.split()[0]
            }
        except:
            return {
                'hostname': platform.node(),
                'distro': 'Unknown Linux',
                'kernel': platform.release(),
                'cpu': 'Unknown',
                'platform': 'Linux',
                'python_version': sys.version.split()[0]
            }
        
    def print_startup_banner(self):
        """Print Linux startup banner"""
        banner = f"""
╔══════════════════════════════════════════════════════════════════╗
║                TRISONICA DATA LOGGER - Linux                    ║
║                                                                  ║
║      Optimized for Linux with systemd service integration       ║
║                                                                  ║
║  Host: {self.system_info['hostname']:<25} Distro: {self.system_info['distro'][:20]:<20} ║
╚══════════════════════════════════════════════════════════════════╝
"""
        self.console.print(banner, style="bold cyan")
        self.console.print(f"Distribution: {self.system_info['distro']}")
        self.console.print(f"Kernel: {self.system_info['kernel']}")
        self.console.print(f"CPU: {self.system_info['cpu'][:50]}")
        self.console.print(f"Python: {self.system_info['python_version']}")
        self.console.print(f"Log Directory: {self.config.log_dir}")
        self.console.print(f"Max Data Points: {MAX_DATAPOINTS:,}")
        self.console.print("─" * 70)
        
    def send_notification(self, title: str, message: str, urgency: str = "normal"):
        """Send desktop notification via notify-send"""
        if not self.config.enable_notifications:
            return
        try:
            subprocess.run([
                'notify-send',
                '--urgency', urgency,
                '--app-name', 'TriSonica Logger',
                title, message
            ], check=False, timeout=5)
        except:
            pass
        
    def find_serial_ports(self) -> List[str]:
        """Find cross-platform serial ports"""
        if sys.platform == "win32":
            ports = [f"COM{i}" for i in range(1, 257)]
            valid_ports = []
            for port in ports:
                try:
                    s = serial.Serial(port)
                    s.close()
                    valid_ports.append(port)
                except (OSError, serial.SerialException):
                    pass
            return valid_ports
            
        patterns = [
            '/dev/ttyUSB*',
            '/dev/ttyACM*',
            '/dev/ttyS*',
            '/dev/cu.usbserial-*',
            '/dev/cu.usbmodem*'
        ]
        
        ports = []
        for pattern in patterns:
            ports.extend(glob.glob(pattern))
            
        # Filter out standard system serial ports unless they're the only option
        if len(ports) > 1:
            ports = [p for p in ports if not p.startswith('/dev/ttyS')]
            
        return sorted(ports)
        
    def auto_detect_serial_port(self) -> Optional[str]:
        """Auto-detect Trisonica on Linux"""
        ports = self.find_serial_ports()
        
        if not ports:
            self.console.print("[ERROR] No serial ports found!", style="bold red")
            self.console.print("Make sure your user is in the 'dialout' group:")
            self.console.print("sudo usermod -a -G dialout $USER")
            self.send_notification("TriSonica Error", "No serial ports found", "critical")
            return None
            
        self.console.print(f"[INFO] Found {len(ports)} serial port(s):")
        for i, port in enumerate(ports):
            self.console.print(f"  {i+1}. {port}")
            
        # Test each port
        for port in ports:
            try:
                self.console.print(f"[TEST] Testing {port}...", end="")
                ser = serial.Serial(port, self.config.baud_rate, timeout=2)
                
                # Read several lines to detect Trisonica
                trisonica_detected = False
                for _ in range(12):
                    try:
                        line = ser.readline().decode('ascii', errors='ignore').strip()
                        if line and any(param in line for param in ['S1', 'S2', 'S3', 'T1', 'T2']):
                            trisonica_detected = True
                            break
                    except:
                        continue
                        
                ser.close()
                
                if trisonica_detected:
                    self.console.print(" [SUCCESS] Trisonica detected!", style="bold green")
                    self.send_notification("TriSonica Connected", f"Device found on {port}")
                    return port
                else:
                    self.console.print(" [FAIL] No Trisonica data", style="dim")
                    
            except PermissionError:
                self.console.print(f" [ERROR] Permission denied", style="red")
                self.console.print(f"Run: sudo chmod 666 {port}")
            except Exception as e:
                self.console.print(f" [ERROR] {e}", style="red")
                
        self.console.print("[WARNING] No Trisonica devices found", style="bold yellow")
        self.send_notification("TriSonica Warning", "No devices found", "normal")
        return None
        
    def setup_logging(self):
        """Setup enhanced logging for Linux"""
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
        
        # Data log
        self.log_filename = f"TrisonicaData_{timestamp}.csv"
        self.log_path = os.path.join(self.config.log_dir, self.log_filename)
        self.log_file = open(self.log_path, 'w', newline='', encoding='utf-8')
        
        # Statistics log
        if self.config.save_statistics:
            self.stats_filename = f"TrisonicaStats_{timestamp}.csv"
            self.stats_path = os.path.join(self.config.log_dir, self.stats_filename)
            self.stats_file = open(self.stats_path, 'w', newline='', encoding='utf-8')
            self.stats_file.write("timestamp,parameter,min,max,mean,std_dev,count\n")
            
        self.console.print(f"[LOG] Data Log: {self.log_filename}")
        if self.config.save_statistics:
            self.console.print(f"[LOG] Stats Log: {self.stats_filename}")
            
    def signal_handler(self, signum, frame):
        """Signal handler: only request shutdown; never touch files here.

        save_final_statistics() rewrites the stats file with seek(0)+truncate().
        Doing that from signal context could re-enter it while the periodic save
        in the main loop is mid-truncate and corrupt the file, so the handler
        just flips the flag and the loop's `finally: cleanup()` writes the final
        stats once.
        """
        signal_names = {signal.SIGINT: "SIGINT", signal.SIGTERM: "SIGTERM"}
        signal_name = signal_names.get(signum, str(signum))
        self.console.print(f"\n[SHUTDOWN] Received {signal_name}, shutting down...", style="bold yellow")
        self.running = False
        
    def statistics_handler(self, signum, frame):
        """Handle SIGUSR1 to print current statistics"""
        self.console.print("\n[STATS] Current statistics:")
        for key, stat in self.stats.items():
            self.console.print(f"  {key}: {stat.current_val:.3f} (count: {stat.count})")
        
    def connect_serial(self) -> bool:
        """Connect with enhanced error handling"""
        if self.config.serial_port == "auto":
            port = self.auto_detect_serial_port()
            if not port:
                return False
        else:
            port = self.config.serial_port
            
        try:
            self.serial_port = serial.Serial(port, self.config.baud_rate, timeout=1)
            self.console.print(f"[SUCCESS] Connected to {port} at {self.config.baud_rate:,} baud", style="bold green")
            return True
        except serial.SerialException as e:
            self.console.print(f"[ERROR] Connection failed: {e}", style="bold red")
            return False
            
    def parse_data_line(self, line: str) -> Dict[str, str]:
        """Enhanced parsing with better error handling"""
        parsed = {}
        try:
            if ',' in line:
                pairs = line.strip().split(',')
                for pair in pairs:
                    pair = pair.strip()
                    if ' ' in pair:
                        parts = pair.split(' ', 1)
                        if len(parts) == 2:
                            key, value = parts
                            parsed[key.strip()] = value.strip()
            else:
                parts = line.strip().split()
                for i in range(0, len(parts)-1, 2):
                    if i+1 < len(parts):
                        parsed[parts[i]] = parts[i+1]
                        
        except Exception as e:
            pass
            
        return parsed
    
    def _open_new_data_file(self):
        """Open a fresh data file and reset the header state (used on schema change).

        Any header already written to the old file no longer describes the new,
        wider column set, so we start a new file rather than emit rows whose field
        count no longer matches the header.
        """
        if self.log_file and not self.log_file.closed:
            self.log_file.close()

        # Use a unique filename so a schema change that happens within the same
        # wall-clock second as the previous file does not overwrite (and lose) it.
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
        candidate = os.path.join(self.config.log_dir, f"TrisonicaData_{timestamp}.csv")
        suffix = 1
        while os.path.exists(candidate):
            candidate = os.path.join(self.config.log_dir,
                                     f"TrisonicaData_{timestamp}_{suffix}.csv")
            suffix += 1
        self.log_path = candidate
        self.log_filename = os.path.basename(self.log_path)
        self.log_file = open(self.log_path, 'w', newline='', encoding='utf-8')
        self.csv_headers_written = False
        self.console.print(f"[LOG] Schema changed; rotated to new data file: {self.log_filename}",
                           style="yellow")

    def update_csv_columns(self, parsed_data: Dict[str, str]):
        """Update CSV columns based on new parameters found.

        If a new parameter appears *after* the header has been written, the header
        can no longer be widened in place, so rotate to a new file to keep every
        row's field count consistent with its header.
        """
        new_columns = False
        for key in parsed_data.keys():
            if key not in self.csv_columns:
                self.csv_columns.append(key)
                new_columns = True

        if self.csv_headers_written and new_columns:
            self._open_new_data_file()

        if not self.csv_headers_written:
            self.log_file.write(','.join(self.csv_columns) + '\n')
            self.csv_headers_written = True

    def write_csv_row(self, timestamp: datetime.datetime, parsed_data: Dict[str, str]):
        """Write a properly formatted CSV row.

        Wrapped so that an I/O error (e.g. a full disk or an unplugged log
        device) is surfaced loudly and flips a disk_full flag instead of being
        silently swallowed by the caller, which would drop samples unnoticed.
        """
        row_values = []
        for column in self.csv_columns:
            if column == 'timestamp':
                row_values.append(timestamp.isoformat())
            else:
                value = parsed_data.get(column, '')
                row_values.append(value)

        try:
            self.log_file.write(','.join(row_values) + '\n')
            self.log_file.flush()
            if self.disk_full:
                self.disk_full = False
                self.console.print("[LOG] Disk write recovered.", style="green")
        except OSError as e:
            if not self.disk_full:
                self.console.print(f"[ERROR] Cannot write log data (disk full or "
                                   f"device removed?): {e}", style="bold red")
                self.send_notification("TriSonica Error",
                                       "Cannot write log data — disk full?", "critical")
            self.disk_full = True
        
    def calculate_statistics(self, key: str, value: float):
        """Enhanced statistics with standard deviation"""
        if key not in self.stats:
            self.stats[key] = Statistics()
            
        stat = self.stats[key]
        stat.current_val = value
        stat.count += 1
        stat.values.append(value)
        stat._sum += value
        stat._sum_sq += value * value

        if stat.count == 1:
            stat.min_val = stat.max_val = stat.mean_val = value
            stat.std_dev = 0.0
        else:
            stat.min_val = min(stat.min_val, value)
            stat.max_val = max(stat.max_val, value)

            stat.mean_val = stat._sum / stat.count
            variance = stat._sum_sq / stat.count - stat.mean_val ** 2
            stat.std_dev = variance ** 0.5 if variance > 0 else 0.0
                
    def read_serial_data(self) -> Optional[DataPoint]:
        """Enhanced data reading with performance metrics"""
        if not self.serial_port or not self.serial_port.is_open:
            return None
            
        try:
            line = self.serial_port.readline().decode('ascii', errors='ignore').strip()
            if not line:
                return None

            # UTC (timezone-aware) so logged timestamps stay monotonic across DST
            # transitions and local-clock adjustments; ISO output carries +00:00.
            timestamp = datetime.datetime.now(datetime.timezone.utc)
            parsed = self.parse_data_line(line)

            # A non-empty line that parses to nothing (garbled/partial frame) must
            # not reach update_csv_columns: it would write a header with no real
            # columns and lock every later parameter out of the header.
            if not parsed:
                return None

            self.update_csv_columns(parsed)
            self.write_csv_row(timestamp, parsed)

            for key, value_str in parsed.items():
                try:
                    value = float(value_str)
                    self.calculate_statistics(key, value)
                    
                    if key == 'S2':
                        self.viz_data['wind_speed'].append(value)
                    elif key == 'T':
                        self.viz_data['temperature'].append(value)
                    elif key == 'D':
                        self.viz_data['wind_direction'].append(value)
                        
                except ValueError:
                    pass
                    
            self.viz_data['timestamps'].append(timestamp)
                    
            now = time.time()
            if now - self.last_update > 0:
                self.update_rate = 1.0 / (now - self.last_update)
            self.last_update = now
            
            return DataPoint(timestamp, line, parsed)

        except serial.SerialException as e:
            # Device removed / port error: report once and stop the loop rather
            # than spinning silently.
            self.console.print(f"[ERROR] Serial read failed: {e}", style="bold red")
            self.running = False
            return None
        except Exception as e:
            # Don't silently swallow unexpected errors — surface them so a real
            # bug isn't hidden behind a dropped sample.
            self.console.print(f"[ERROR] Unexpected error reading data: {e}", style="bold red")
            return None

    def save_final_statistics(self):
        """Write the current statistics snapshot, overwriting any earlier one.

        Called both periodically and at shutdown, so it rewrites the file from
        the start each time rather than appending; otherwise every checkpoint
        would leave a stale duplicate block behind.
        """
        if not self.config.save_statistics or not self.stats_file:
            return

        self.stats_file.seek(0)
        self.stats_file.truncate()
        self.stats_file.write("timestamp,parameter,min,max,mean,std_dev,count\n")

        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for key, stat in self.stats.items():
            self.stats_file.write(f"{timestamp},{key},{stat.min_val:.6f},{stat.max_val:.6f},"
                                f"{stat.mean_val:.6f},{stat.std_dev:.6f},{stat.count}\n")
        self.stats_file.flush()
    
    def create_layout(self) -> Layout:
        """Create enhanced Linux layout"""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=5),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=4)
        )
        layout["main"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=1)
        )
        layout["left"].split_column(
            Layout(name="current_data", ratio=2),
            Layout(name="raw_data", size=10)
        )
        layout["right"].split_column(
            Layout(name="statistics", ratio=1),
            Layout(name="system_info", size=12)
        )
        return layout
        
    def update_display(self, layout: Layout):
        """Enhanced Linux display"""
        elapsed = time.time() - self.start_time
        runtime = str(datetime.timedelta(seconds=int(elapsed)))
        
        # Header
        header_table = Table.grid(expand=True)
        header_table.add_column(justify="left", ratio=1)
        header_table.add_column(justify="center", ratio=1)
        header_table.add_column(justify="right", ratio=1)
        
        memory_usage = f"{sys.getsizeof(self.data_points) / 1024:.1f} KB"
        
        header_table.add_row(
            f"TriSonica Linux Logger - {self.system_info['hostname']}",
            f"Runtime: {runtime}",
            f"Points: {self.point_count:,}"
        )
        header_table.add_row(
            f"Update Rate: {self.update_rate:.1f} Hz",
            f"Memory: {memory_usage}",
            f"Log: {os.path.basename(self.log_path)}"
        )
        
        layout["header"].update(Panel(header_table, title="System Status", style="bold blue"))
        
        # Current data
        if self.data_points:
            latest = self.data_points[-1]
            
            data_table = Table(title="Current Measurements", box=box.ROUNDED)
            data_table.add_column("Parameter", style="cyan", width=12)
            data_table.add_column("Value", style="green", width=12)
            data_table.add_column("Unit", style="dim", width=10)
            data_table.add_column("Quality", style="yellow", width=12)
            
            for key, value in latest.parsed_data.items():
                try:
                    val = float(value)
                    if key.startswith('S'):
                        unit = "m/s"
                        quality = "Good" if 0 <= val <= 50 else "Check Range"
                    elif key.startswith('T'):
                        unit = "°C"
                        quality = "Good" if -40 <= val <= 60 else "Check Range"
                    else:
                        unit = ""
                        quality = "Unknown"
                except:
                    unit = ""
                    quality = "Invalid"
                    
                data_table.add_row(key, value, unit, quality)
                
            layout["current_data"].update(Panel(data_table, title="Live Data"))
            
            # Raw data
            if self.config.show_raw_data:
                raw_lines = []
                for dp in list(self.data_points)[-8:]:
                    timestamp = dp.timestamp.strftime('%H:%M:%S.%f')[:-3]
                    raw_lines.append(f"{timestamp}: {dp.raw_data}")
                    
                raw_text = "\n".join(raw_lines)
                layout["raw_data"].update(Panel(raw_text, title="Raw Data Stream"))
            else:
                layout["raw_data"].update(Panel("Raw data display disabled\nUse --show-raw to enable", title="Raw Data"))
                
        else:
            layout["current_data"].update(Panel("Waiting for data...", title="Live Data"))
            layout["raw_data"].update(Panel("No data received yet", title="Raw Data"))
            
        # Statistics
        if self.stats:
            stats_table = Table(title="Statistical Analysis", box=box.ROUNDED)
            stats_table.add_column("Parameter", style="cyan")
            stats_table.add_column("Current", style="green")
            stats_table.add_column("Min", style="blue")
            stats_table.add_column("Max", style="red")
            stats_table.add_column("Mean", style="yellow")
            stats_table.add_column("Std Dev", style="magenta")
            stats_table.add_column("Count", style="white")
            
            for key, stat in self.stats.items():
                stats_table.add_row(
                    key,
                    f"{stat.current_val:.3f}",
                    f"{stat.min_val:.3f}",
                    f"{stat.max_val:.3f}",
                    f"{stat.mean_val:.3f}",
                    f"{stat.std_dev:.3f}",
                    f"{stat.count:,}"
                )
                
            layout["statistics"].update(Panel(stats_table, title="Statistics"))
        else:
            layout["statistics"].update(Panel("No statistics available", title="Statistics"))
            
        # System info
        system_table = Table(title="System Info", box=box.SIMPLE)
        system_table.add_column("Property", style="cyan")
        system_table.add_column("Value", style="white")
        
        system_table.add_row("Hostname", self.system_info['hostname'])
        system_table.add_row("Distribution", self.system_info['distro'][:30])
        system_table.add_row("Kernel", self.system_info['kernel'])
        system_table.add_row("CPU", self.system_info['cpu'][:30])
        system_table.add_row("Python", self.system_info['python_version'])
        
        layout["system_info"].update(Panel(system_table, title="Linux System"))
            
        # Footer
        footer_info = []
        footer_info.append(f"Data: {self.log_filename}")
        if self.config.save_statistics:
            footer_info.append(f"Stats: {self.stats_filename}")
        footer_info.append("Ctrl+C: exit | SIGUSR1: stats")
        
        footer_text = " | ".join(footer_info)
        layout["footer"].update(Panel(Align.center(footer_text), style="dim"))
        
    def run(self):
        """Main execution with Linux-optimized interface"""
        if not self.connect_serial():
            # Files were opened in __init__; close them so a failed connect
            # doesn't leak handles or leave stray empty log files.
            self.cleanup()
            return False

        layout = self.create_layout()
        
        try:
            with Live(layout, refresh_per_second=25, screen=True) as live:
                self.running = True
                while self.running:
                    data_point = self.read_serial_data()
                    if data_point:
                        self.point_count += 1
                        self.data_points.append(data_point)
                        
                        # Save statistics periodically
                        if self.point_count % 200 == 0:
                            self.save_final_statistics()
                            
                    self.update_display(layout)
                    time.sleep(UPDATE_INTERVAL)
                    
        except KeyboardInterrupt:
            pass
        finally:
            self.cleanup()
            
        return True
        
    def cleanup(self):
        """Enhanced Linux cleanup"""
        # Write the final statistics snapshot here (normal thread context) rather
        # than from the signal handler, then close the files. Safe to call even
        # when no data was logged.
        if self.stats_file and not self.stats_file.closed:
            self.save_final_statistics()

        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
            self.console.print("[CLEANUP] Serial port closed", style="green")

        if self.log_file and not self.log_file.closed:
            self.log_file.close()
            self.console.print(f"[CLEANUP] Data log saved: {self.log_path}", style="green")
            
        if self.stats_file and not self.stats_file.closed:
            self.stats_file.close()
            self.console.print(f"[CLEANUP] Statistics saved: {self.stats_path}", style="green")
            
        # Final summary
        if self.point_count > 0:
            elapsed = time.time() - self.start_time
            avg_rate = self.point_count / elapsed if elapsed > 0 else 0
            self.console.print(f"\n[SUMMARY] Session Summary:")
            self.console.print(f"   Total Points: {self.point_count:,}")
            self.console.print(f"   Runtime: {datetime.timedelta(seconds=int(elapsed))}")
            self.console.print(f"   Average Rate: {avg_rate:.1f} Hz")
            self.console.print(f"   Data Quality: {len(self.stats)} parameters tracked")
            
            self.send_notification("TriSonica Session Complete", 
                                 f"Logged {self.point_count:,} data points")
            
        self.console.print("[SUCCESS] Cleanup complete", style="bold green")

def main():
    parser = argparse.ArgumentParser(description='Trisonica Data Logger for Linux')
    parser.add_argument('--port', default='auto', help='Serial port (default: auto-detect)')
    parser.add_argument('--baud', type=int, default=DEFAULT_BAUD_RATE, help='Baud rate')
    parser.add_argument('--log-dir', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "OUTPUT"), help='Log directory')
    parser.add_argument('--show-raw', action='store_true', help='Show raw data stream')
    parser.add_argument('--no-stats', action='store_true', help='Disable statistics logging')
    parser.add_argument('--no-notifications', action='store_true', help='Disable desktop notifications')
    parser.add_argument('--daemon', action='store_true', help='Run as daemon (background service)')
    
    args = parser.parse_args()
    
    config = Config(
        serial_port=args.port,
        baud_rate=args.baud,
        log_dir=args.log_dir,
        show_raw_data=args.show_raw,
        save_statistics=not args.no_stats,
        enable_notifications=not args.no_notifications
    )
    
    logger = TrisonicaDataLoggerLinux(config)
    
    if args.daemon:
        if sys.platform == "win32":
            print("[ERROR] Daemon mode is not supported on Windows.")
            sys.exit(1)
        # Simple daemonization
        try:
            import daemon
        except ImportError:
            print("[ERROR] --daemon requires the python-daemon package "
                  "(pip install python-daemon).")
            sys.exit(1)
        with daemon.DaemonContext():
            logger.run()
    else:
        sys.exit(0 if logger.run() else 1)

if __name__ == '__main__':
    main()