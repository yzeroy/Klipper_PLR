#!/bin/bash

# --- SELF-ELEVATION ---
# If the script is not running as root, re-run it with sudo
if [ "$EUID" -ne 0 ]; then
    echo "This script needs root privileges. Requesting sudo..."
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
OWNER="$REAL_USER"

# Define paths
KLIPPER_DIR="$USER_HOME/klipper"
EXTRAS_DIR="$KLIPPER_DIR/klippy/extras"
PRINTER_DATA_DIR="$USER_HOME/printer_data/config"
PROJECT_DIR="$PWD"

echo "Running as: $(whoami) (Real user: $REAL_USER)"
echo "Home directory: $USER_HOME"

# Check if the configuration directory exists
if [ ! -d "$PRINTER_DATA_DIR" ]; then
    echo "Error: Directory $PRINTER_DATA_DIR not found."
    exit 1
fi

# --- START OF INSTALLATION ---

# 1. Stop Klipper service
echo "Stopping Klipper service..."
systemctl stop klipper

# 2. Prepare variables.cfg
if [ ! -f "$PRINTER_DATA_DIR/variables.cfg" ]; then
    touch "$PRINTER_DATA_DIR/variables.cfg" && echo "variables.cfg created."
fi

# 3. Copy files and set permissions
echo "Copying files..."

# Configuration file
if [ -f "$PROJECT_DIR/power_loss_recovery.cfg" ]; then
    cp -f "$PROJECT_DIR/power_loss_recovery.cfg" "$PRINTER_DATA_DIR/"
fi

# Python extras
if [ -d "$EXTRAS_DIR" ]; then
    if [ -f "$PROJECT_DIR/power_loss_recovery.py" ]; then
        cp -f "$PROJECT_DIR/power_loss_recovery.py" "$EXTRAS_DIR/"
        chmod 644 "$EXTRAS_DIR/power_loss_recovery.py"
        echo "power_loss_recovery.py updated in extras (644)."
    else
        echo "Warning: power_loss_recovery.py not found in project directory."
    fi
else
    echo "Warning: Extras directory not found ($EXTRAS_DIR)."
fi

# 4. Auto-replace paths in configuration
if [ -f "$PRINTER_DATA_DIR/power_loss_recovery.cfg" ]; then
    sed -i -E "s|\{USER_HOME\}|$USER_HOME|i" "$PRINTER_DATA_DIR/power_loss_recovery.cfg"
    sed -i -E "s|\{PLR_DIR\}|$USER_HOME/printer_data/plr|i" "$PRINTER_DATA_DIR/power_loss_recovery.cfg"
fi

# 5. Modify printer.cfg (Add include to the FIRST line)
if [ ! -f "$PRINTER_DATA_DIR/printer.cfg" ]; then
    touch "$PRINTER_DATA_DIR/printer.cfg"
fi

if ! grep -Fxq '[include power_loss_recovery.cfg]' "$PRINTER_DATA_DIR/printer.cfg"; then
    # Použití sed k vložení na první řádek (1i)
    sed -i '1i [include power_loss_recovery.cfg]' "$PRINTER_DATA_DIR/printer.cfg"
    echo "Include added to the first line of printer.cfg."
else
    echo "Include already exists in printer.cfg."
fi

# 6. Modify moonraker.conf (Add update_manager entry directly)
if [ -f "$PRINTER_DATA_DIR/moonraker.conf" ]; then
    if grep -q '\[update_manager Klipper_power_loss_recovery\]' "$PRINTER_DATA_DIR/moonraker.conf"; then
        echo "Update manager entry already exists in moonraker.conf."
    else
        echo "Adding update_manager entry to moonraker.conf..."
        cat >> "$PRINTER_DATA_DIR/moonraker.conf" << EOF

[update_manager Klipper_power_loss_recovery]
type: git_repo
path: $PROJECT_DIR
origin: https://github.com/yzeroy/Klipper_PLR.git
primary_branch: main
install_script: install.sh
is_system_service: False
EOF
    fi
else
    echo "Warning: moonraker.conf not found. Skip adding update manager."
fi

# 7. Final ownership fix
chown "$OWNER":"$OWNER" "$PRINTER_DATA_DIR/power_loss_recovery.cfg"
chown "$OWNER":"$OWNER" "$PRINTER_DATA_DIR/variables.cfg"
chown "$OWNER":"$OWNER" "$PRINTER_DATA_DIR/moonraker.conf"
# Fix ownership for extras only if it exists
if [ -f "$EXTRAS_DIR/power_loss_recovery.py" ]; then
    chown "$OWNER":"$OWNER" "$EXTRAS_DIR/power_loss_recovery.py"
fi
echo "Ownership fixed for configuration files."

# 8. Start Klipper service
echo "Starting Klipper service..."
systemctl start klipper

echo "Installation and restart complete."
