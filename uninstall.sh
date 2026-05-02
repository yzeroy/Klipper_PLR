#!/bin/bash

# --- SELF-ELEVATION ---
# If the script is not running as root, re-run it with sudo
if [ "$EUID" -ne 0 ]; then
    echo "This script needs root privileges for uninstallation. Requesting sudo..."
    sudo "$0" "$@"
    exit $?
fi

# --- ENVIRONMENT SETUP ---
# Determine the real user to find their home directory
if [ -n "$SUDO_USER" ]; then
    REAL_USER="$SUDO_USER"
else
    REAL_USER="$USER"
fi

USER_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

# Define paths
KLIPPER_DIR="$USER_HOME/klipper"
EXTRAS_DIR="$KLIPPER_DIR/klippy/extras"
PRINTER_DATA_DIR="$USER_HOME/printer_data/config"

echo "Uninstalling Power Loss Recovery..."
echo "Target home directory: $USER_HOME"

# --- START OF UNINSTALLATION ---

# 1. Stop Klipper service
echo "Stopping Klipper service..."
systemctl stop klipper

# 2. Remove Python extra file
if [ -f "$EXTRAS_DIR/power_loss_recovery.py" ]; then
    rm -f "$EXTRAS_DIR/power_loss_recovery.py"
    echo "Removed power_loss_recovery.py from extras."
fi

# 3. Remove configuration file
if [ -f "$PRINTER_DATA_DIR/power_loss_recovery.cfg" ]; then
    rm -f "$PRINTER_DATA_DIR/power_loss_recovery.cfg"
    echo "Removed power_loss_recovery.cfg."
fi

# 4. Remove [include] from printer.cfg
if [ -f "$PRINTER_DATA_DIR/printer.cfg" ]; then
    # Removes the specific include line
    sed -i '/\[include power_loss_recovery.cfg\]/d' "$PRINTER_DATA_DIR/printer.cfg"
    echo "Removed include from printer.cfg."
fi

# 5. Remove update_manager entry from moonraker.conf
if [ -f "$PRINTER_DATA_DIR/moonraker.conf" ]; then
    # This sed command removes the [update_manager] block and its following lines
    sed -i '/\[update_manager Klipper_power_loss_recovery\]/,/is_system_service: False/d' "$PRINTER_DATA_DIR/moonraker.conf"
    echo "Removed update_manager entry from moonraker.conf."
fi

# 6. Optional: Clean up empty lines left by sed
if [ -f "$PRINTER_DATA_DIR/printer.cfg" ]; then
    sed -i '${/^$/d;}' "$PRINTER_DATA_DIR/printer.cfg"
fi
if [ -f "$PRINTER_DATA_DIR/moonraker.conf" ]; then
    sed -i '${/^$/d;}' "$PRINTER_DATA_DIR/moonraker.conf"
fi

# 7. Start Klipper service
echo "Starting Klipper service..."
systemctl start klipper

echo "Uninstallation complete. Klipper has been restarted."