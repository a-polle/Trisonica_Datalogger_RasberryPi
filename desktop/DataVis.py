#!/usr/bin/env python3
"""
TriSonica Data Visualization Tool for Linux
Enhanced with Linux-specific features and integrations
"""

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import re
import os
import glob
import numpy as np
import json
import argparse
from pathlib import Path
from datetime import datetime
import sys
import subprocess

try:
    from windrose import WindroseAxes
    windrose_installed = True
except ImportError:
    windrose_installed = False

# Maps the short column names to a full description and unit for plotting.
PLOT_METADATA = {
    'S': ('3D Wind Speed', 'Speed (m/s)'),
    'S1': ('Sonic Speed 1', 'Speed (m/s)'),
    'S2': ('2D Wind Speed', 'Speed (m/s)'),
    'S3': ('Sonic Speed 3', 'Speed (m/s)'),
    'D': ('Wind Direction', 'Direction (°)'),
    'U': ('U-Vector (Zonal Wind)', 'Speed (m/s)'),
    'V': ('V-Vector (Meridional Wind)', 'Speed (m/s)'),
    'W': ('W-Vector (Vertical Wind)', 'Speed (m/s)'),
    'T': ('Air Temperature', 'Temperature (°C)'),
    'T1': ('Temperature 1', 'Temperature (°C)'),
    'T2': ('Temperature 2', 'Temperature (°C)'),
    'H': ('Relative Humidity', 'Humidity (%)'),
    'P': ('Atmospheric Pressure', 'Pressure (hPa)'),
    'PI': ('Pitch Angle', 'Angle (°)'),
    'RO': ('Roll Angle', 'Angle (°)'),
    'MD': ('Magnetic Heading', 'Direction (°)'),
    'TD': ('True Heading', 'Direction (°)')
}

def send_notification(title, message, urgency="normal"):
    """Send desktop notification via notify-send"""
    try:
        subprocess.run([
            'notify-send',
            '--urgency', urgency,
            '--app-name', 'TriSonica DataVis',
            title, message
        ], check=False, timeout=5)
    except:
        pass

def detect_log_format(file_path):
    """
    Detects the format of the log file (old tagged format vs new CSV format).
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            second_line = f.readline().strip()
            
            # Check if first line contains 'timestamp' (CSV header)
            if 'timestamp' in first_line.lower():
                return 'csv'
            
            # Check if lines contain parameter-value pairs like "S1 12.34, T1 25.67"
            if re.search(r'\w+\s+[\d\.-]+', first_line) or re.search(r'\w+\s+[\d\.-]+', second_line):
                return 'tagged'
            
            return 'unknown'
            
    except Exception as e:
        print(f"Error detecting format: {e}")
        return 'unknown'

def parse_tagged_format(file_path):
    """
    Parse the old tagged format with embedded timestamps and parameter tags.
    Example line: "2023-10-15 14:30:25.123456 - S1 12.34, T1 25.67, D1 180.5"
    """
    data_rows = []
    
    with open(file_path, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
                
            try:
                # Split timestamp and data
                if ' - ' in line:
                    timestamp_str, data_str = line.split(' - ', 1)
                else:
                    # Fallback: assume entire line is data with current timestamp
                    timestamp_str = datetime.now().isoformat()
                    data_str = line
                
                # Parse timestamp
                try:
                    timestamp = pd.to_datetime(timestamp_str)
                except:
                    timestamp = pd.to_datetime(datetime.now())
                
                # Parse data parameters
                row_data = {'timestamp': timestamp}
                
                # Split by commas and parse parameter-value pairs
                if ',' in data_str:
                    pairs = data_str.split(',')
                else:
                    pairs = [data_str]
                    
                for pair in pairs:
                    pair = pair.strip()
                    if not pair:
                        continue
                        
                    # Look for pattern: PARAM VALUE
                    match = re.match(r'(\w+)\s+([\d\.-]+)', pair)
                    if match:
                        param, value = match.groups()
                        try:
                            row_data[param] = float(value)
                        except ValueError:
                            row_data[param] = value
                
                if len(row_data) > 1:  # More than just timestamp
                    data_rows.append(row_data)
                    
            except Exception as e:
                print(f"Warning: Could not parse line {line_num}: {line[:50]}... Error: {e}")
                continue
    
    if not data_rows:
        raise ValueError("No valid data found in tagged format file")
    
    df = pd.DataFrame(data_rows)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.set_index('timestamp')
    return df

def parse_csv_format(file_path):
    """
    Parse the new CSV format with proper headers.
    """
    try:
        df = pd.read_csv(file_path)
        
        # Convert timestamp column
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df = df.set_index('timestamp')
        else:
            # If no timestamp column, create one
            df.index = pd.date_range(start='2023-01-01', periods=len(df), freq='S')
            
        return df
        
    except Exception as e:
        raise ValueError(f"Could not parse CSV format: {e}")

def load_trisonica_data(file_path):
    """
    Load and parse TriSonica data file, auto-detecting format.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    
    print(f"Loading data from: {file_path}")
    
    # Detect format
    format_type = detect_log_format(file_path)
    print(f"Detected format: {format_type}")
    
    # Parse based on format
    if format_type == 'csv':
        df = parse_csv_format(file_path)
    elif format_type == 'tagged':
        df = parse_tagged_format(file_path)
    else:
        # Try both formats
        try:
            df = parse_csv_format(file_path)
            print("Successfully parsed as CSV format")
        except:
            try:
                df = parse_tagged_format(file_path)
                print("Successfully parsed as tagged format")
            except:
                raise ValueError("Could not determine file format or parse data")
    
    # Clean and validate data
    df = df.select_dtypes(include=[np.number])  # Keep only numeric columns
    df = df.dropna(how='all')  # Remove rows with all NaN values
    
    print(f"Loaded {len(df)} data points with {len(df.columns)} parameters")
    print(f"Available parameters: {list(df.columns)}")
    print(f"Date range: {df.index.min()} to {df.index.max()}")
    
    return df

def create_time_series_plot(df, parameter, title=None, ax=None):
    """
    Create a time series plot for a specific parameter.
    """
    if parameter not in df.columns:
        print(f"Warning: Parameter '{parameter}' not found in data")
        return None
    
    if ax is None:
        fig, ax = plt.subplots(figsize=(12, 6))
    
    # Get data and metadata
    data = df[parameter].dropna()
    if len(data) == 0:
        print(f"Warning: No valid data for parameter '{parameter}'")
        return None
    
    param_title, param_unit = PLOT_METADATA.get(parameter, (parameter, 'Unknown'))
    
    # Plot the data
    ax.plot(data.index, data.values, linewidth=1, alpha=0.8, label=param_title)
    
    # Formatting
    ax.set_xlabel('Time')
    ax.set_ylabel(param_unit)
    ax.set_title(title or f'{param_title} over Time')
    ax.grid(True, alpha=0.3)
    ax.legend()
    
    # Format x-axis
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
    
    return ax

def create_wind_rose(df, output_dir):
    """
    Create a wind rose plot if wind data is available.
    """
    if not windrose_installed:
        print("WindRose package not installed, skipping wind rose plot")
        send_notification("TriSonica Warning", "WindRose package not installed", "normal")
        return None
    
    # Find wind speed and direction columns
    wind_speed_col = None
    wind_dir_col = None
    
    for col in ['S', 'S2', 'S1']:
        if col in df.columns:
            wind_speed_col = col
            break
    
    for col in ['D', 'D1']:
        if col in df.columns:
            wind_dir_col = col
            break
    
    if wind_speed_col is None or wind_dir_col is None:
        print("Wind speed or direction data not available for wind rose")
        return None
    
    # Clean data
    wind_data = df[[wind_speed_col, wind_dir_col]].dropna()
    if len(wind_data) < 10:
        print("Insufficient wind data for wind rose plot")
        return None
    
    # Create wind rose
    fig, ax = plt.subplots(figsize=(10, 10), subplot_kw=dict(projection="windrose"))
    ax.bar(wind_data[wind_dir_col], wind_data[wind_speed_col], normed=True, opening=0.8, edgecolor='white')
    ax.set_legend(title='Wind Speed (m/s)', loc='upper left', bbox_to_anchor=(1.1, 1))
    ax.set_title('Wind Rose Diagram', pad=20)
    
    # Save plot
    output_path = os.path.join(output_dir, 'WindRose.png')
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    
    print(f"Wind rose saved: {output_path}")
    return output_path

def create_summary_plot(df, output_dir, base_filename):
    """
    Create a comprehensive summary plot with multiple subplots.
    """
    # Determine available parameters
    available_params = [col for col in df.columns if col in PLOT_METADATA]
    
    if not available_params:
        print("No recognized parameters found for plotting")
        return None
    
    # Determine subplot layout
    n_plots = min(len(available_params), 9)  # Maximum 9 subplots
    cols = 3
    rows = (n_plots + cols - 1) // cols
    
    fig, axes = plt.subplots(rows, cols, figsize=(15, 4*rows))
    if rows == 1:
        axes = [axes] if cols == 1 else axes
    else:
        axes = axes.flatten()
    
    # Create individual plots
    for i, param in enumerate(available_params[:n_plots]):
        ax = axes[i] if n_plots > 1 else axes
        create_time_series_plot(df, param, ax=ax)
    
    # Hide unused subplots
    for i in range(n_plots, len(axes)):
        axes[i].set_visible(False)
    
    # Overall formatting
    plt.suptitle(f'TriSonica Data Summary - {base_filename}', fontsize=16, y=0.98)
    plt.tight_layout()
    plt.subplots_adjust(top=0.93)
    
    # Save plot
    output_path = os.path.join(output_dir, f'Summary_{base_filename}.png')
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    
    print(f"Summary plot saved: {output_path}")
    return output_path

def create_individual_plots(df, output_dir, base_filename):
    """
    Create individual plots for each parameter.
    """
    plot_paths = []
    
    for param in df.columns:
        if param in PLOT_METADATA:
            param_title, param_unit = PLOT_METADATA[param]
            
            fig, ax = plt.subplots(figsize=(12, 6))
            create_time_series_plot(df, param, ax=ax)
            
            # Save individual plot
            safe_param = re.sub(r'[^\w\-_\.]', '_', param)
            output_path = os.path.join(output_dir, f'{safe_param}_{base_filename}.png')
            plt.tight_layout()
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            plot_paths.append(output_path)
            print(f"Individual plot saved: {output_path}")
    
    return plot_paths

def save_statistics(df, output_dir, base_filename):
    """
    Save statistical summary to JSON and CSV files.
    """
    stats = {}
    
    for param in df.columns:
        if param in PLOT_METADATA:
            data = df[param].dropna()
            if len(data) > 0:
                stats[param] = {
                    'count': int(len(data)),
                    'mean': float(data.mean()),
                    'std': float(data.std()),
                    'min': float(data.min()),
                    'max': float(data.max()),
                    'median': float(data.median()),
                    'description': PLOT_METADATA[param][0],
                    'unit': PLOT_METADATA[param][1]
                }
    
    # Save as JSON
    json_path = os.path.join(output_dir, f'Statistics_{base_filename}.json')
    with open(json_path, 'w') as f:
        json.dump(stats, f, indent=2)
    
    # Save as CSV
    csv_path = os.path.join(output_dir, f'Statistics_{base_filename}.csv')
    stats_df = pd.DataFrame(stats).T
    stats_df.to_csv(csv_path)
    
    print(f"Statistics saved: {json_path}")
    print(f"Statistics saved: {csv_path}")
    
    return json_path, csv_path

def open_file_manager(path):
    """Open file manager to show generated plots"""
    try:
        # Try different file managers commonly found on Linux
        file_managers = ['xdg-open', 'nautilus', 'dolphin', 'thunar', 'pcmanfm']
        
        for fm in file_managers:
            try:
                subprocess.run([fm, path], check=True, timeout=5)
                print(f"Opened {path} with {fm}")
                return True
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                continue
        
        print(f"Could not open file manager for: {path}")
        return False
    except Exception as e:
        print(f"Error opening file manager: {e}")
        return False

def process_data_file(input_path, output_dir=None, create_individual=True, create_windrose=True, open_results=False):
    """
    Process a single data file and generate all visualizations.
    """
    if not output_dir:
        output_dir = os.path.join(os.path.dirname(input_path), 'PLOTS')
    
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    # Get base filename
    base_filename = Path(input_path).stem
    
    try:
        # Load data
        df = load_trisonica_data(input_path)
        
        # Create plots
        print("\nGenerating visualizations...")
        send_notification("TriSonica DataVis", "Starting visualization generation")
        
        # Summary plot
        summary_path = create_summary_plot(df, output_dir, base_filename)
        
        # Individual plots
        plot_count = 0
        if create_individual:
            individual_paths = create_individual_plots(df, output_dir, base_filename)
            plot_count += len(individual_paths)
        
        # Wind rose
        if create_windrose:
            windrose_path = create_wind_rose(df, output_dir)
            if windrose_path:
                plot_count += 1
        
        # Statistics
        stats_paths = save_statistics(df, output_dir, base_filename)
        
        print(f"\n✓ Processing complete!")
        print(f"Output directory: {output_dir}")
        print(f"Generated {plot_count} plots")
        
        send_notification("TriSonica DataVis", f"Generated {plot_count} plots successfully")
        
        # Open file manager if requested
        if open_results:
            open_file_manager(output_dir)
        
        return True
        
    except Exception as e:
        print(f"Error processing file: {e}")
        send_notification("TriSonica Error", f"Processing failed: {str(e)}", "critical")
        return False

def select_file_cli():
    """Command-line file selection using find and dialog tools"""
    try:
        # Look for CSV files in common locations
        search_paths = [
            './OUTPUT/*.csv',
            '../OUTPUT/*.csv',
            '~/Documents/TriSonica*/*.csv',
            './TrisonicaData_*.csv'
        ]
        
        found_files = []
        for pattern in search_paths:
            found_files.extend(glob.glob(os.path.expanduser(pattern)))
        
        if not found_files:
            print("No TriSonica data files found in common locations")
            return None
        
        # Remove duplicates and sort by modification time
        found_files = list(set(found_files))
        found_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        
        print("\nFound TriSonica data files:")
        for i, file_path in enumerate(found_files[:10], 1):  # Show max 10 files
            mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            print(f"  {i}. {os.path.basename(file_path)} ({size_mb:.1f}MB, {mod_time.strftime('%Y-%m-%d %H:%M')})")
        
        while True:
            try:
                choice = input(f"\nSelect file (1-{min(len(found_files), 10)}) or 'q' to quit: ").strip()
                if choice.lower() == 'q':
                    return None
                
                index = int(choice) - 1
                if 0 <= index < min(len(found_files), 10):
                    return found_files[index]
                else:
                    print("Invalid selection")
            except (ValueError, KeyboardInterrupt):
                print("\nOperation cancelled")
                return None
                
    except Exception as e:
        print(f"Error in file selection: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description='TriSonica Data Visualization Tool for Linux')
    parser.add_argument('input_path', nargs='?', help='Input CSV/log file or directory')
    parser.add_argument('--output-dir', help='Output directory for plots')
    parser.add_argument('--no-individual', action='store_true', help='Skip individual parameter plots')
    parser.add_argument('--no-windrose', action='store_true', help='Skip wind rose plot')
    parser.add_argument('--interactive', action='store_true', help='Interactive file selection')
    parser.add_argument('--open', action='store_true', help='Open file manager with results')
    parser.add_argument('--batch', action='store_true', help='Batch process all CSV files in directory')
    
    args = parser.parse_args()
    
    # Determine input path
    if args.interactive or not args.input_path:
        print("Interactive file selection...")
        input_path = select_file_cli()
        if not input_path:
            print("No file selected, exiting.")
            return
    else:
        input_path = args.input_path
    
    if not input_path or not os.path.exists(input_path):
        print(f"Error: Input path '{input_path}' does not exist")
        return
    
    # Process files
    if os.path.isdir(input_path) or args.batch:
        # Process all CSV files in directory
        if os.path.isfile(input_path):
            input_path = os.path.dirname(input_path)
            
        csv_files = glob.glob(os.path.join(input_path, "*.csv"))
        if not csv_files:
            print(f"No CSV files found in directory: {input_path}")
            return
        
        print(f"Found {len(csv_files)} CSV files to process")
        
        success_count = 0
        for csv_file in csv_files:
            print(f"\nProcessing: {os.path.basename(csv_file)}")
            output_dir = args.output_dir or os.path.join(input_path, 'PLOTS')
            if process_data_file(
                csv_file, 
                output_dir, 
                create_individual=not args.no_individual,
                create_windrose=not args.no_windrose,
                open_results=False  # Don't open for each file in batch
            ):
                success_count += 1
        
        print(f"\n✓ Batch processing complete! {success_count}/{len(csv_files)} files processed successfully")
        
        if args.open and success_count > 0:
            final_output_dir = args.output_dir or os.path.join(input_path, 'PLOTS')
            open_file_manager(final_output_dir)
            
    else:
        # Process single file
        print(f"Processing: {os.path.basename(input_path)}")
        output_dir = args.output_dir or os.path.join(os.path.dirname(input_path), 'PLOTS')
        process_data_file(
            input_path, 
            output_dir,
            create_individual=not args.no_individual,
            create_windrose=not args.no_windrose,
            open_results=args.open
        )
    
    print("\n✓ All processing complete!")

if __name__ == "__main__":
    main()