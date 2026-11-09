#!/usr/bin/env bash
# ==============================================================================
# DynoGatorLabs Wasteland SIGINT Platform — Handset APK Installer
# Automated deployment for Google Pixel 9 Pro XL & Android / GrapheneOS devices
# ==============================================================================
set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
AMBER='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APK_PATH="${SCRIPT_DIR}/apk/PictoSight-H8-2.1.0.apk"

if [[ ! -f "${APK_PATH}" ]]; then
    # Fallback to repo dist or downloads
    if [[ -f "${SCRIPT_DIR}/../dist/PictoSight-H8-2.1.0.apk" ]]; then
        APK_PATH="${SCRIPT_DIR}/../dist/PictoSight-H8-2.1.0.apk"
    elif [[ -f "${HOME}/Downloads/PictoSight-H8-2.1.0.apk" ]]; then
        APK_PATH="${HOME}/Downloads/PictoSight-H8-2.1.0.apk"
    else
        echo -e "${RED}[ERROR] APK file not found at ${APK_PATH}${NC}"
        exit 1
    fi
fi

echo -e "${CYAN}"
cat << 'EOF'
  ___             ___       _             _          _         
 |   \ _  _ _ _  / __| __ _| |_ ___ _ _  | |   __ _ | |__ ___  
 | |) | || | ' \| (_ |/ _` |  _/ _ \ '_| | |__/ _` || '_ (_-<  
 |___/ \_, |_||_|\___|\__,_|\__\___/_|   |____\__,_||_.__/__/  
       |__/                                                    
   [ Pip-Boy / Wasteland Mobile SIGINT Remote Handset Installer ]
EOF
echo -e "${NC}"

if ! command -v adb >/dev/null 2>&1; then
    echo -e "${RED}[!] 'adb' command not found. Installing android-tools-adb...${NC}"
    sudo apt-get update && sudo apt-get install -y android-tools-adb
fi

echo -e "${AMBER}[*] Scanning for connected Android / GrapheneOS devices...${NC}"
DEVICES=$(adb devices | grep -v "List" | grep "device$" | awk '{print $1}')

if [[ -z "${DEVICES}" ]]; then
    if [[ -t 0 ]]; then
        echo -e "${AMBER}[?] No USB device detected. Checking wireless debugging...${NC}"
        echo -e "    Ensure Wireless Debugging is enabled on phone, or connect via USB-C."
        read -rp "    Enter phone IP:PORT (or press Enter to cancel): " PHONE_TARGET
        if [[ -n "${PHONE_TARGET}" ]]; then
            adb connect "${PHONE_TARGET}"
            DEVICES=$(adb devices | grep -v "List" | grep "device$" | awk '{print $1}')
        fi
    else
        echo -e "${AMBER}[i] No USB Android target attached in non-interactive mode; skipping prompt.${NC}"
    fi
fi

if [[ -z "${DEVICES}" ]]; then
    echo -e "${RED}[ERROR] No ADB target available. Please connect Pixel and retry.${NC}"
    exit 1
fi

for TARGET in ${DEVICES}; do
    echo -e "${GREEN}[+] Target Device Found: ${TARGET}${NC}"
    echo -e "${CYAN}[*] Establishing Reverse Port Forwarding (8765 bridge, 7356 Gqrx)...${NC}"
    adb -s "${TARGET}" reverse tcp:8765 tcp:8765 || true
    adb -s "${TARGET}" reverse tcp:7356 tcp:7356 || true

    echo -e "${CYAN}[*] Sideloading Picto-Sight v2.1.0 APK to ${TARGET}...${NC}"
    adb -s "${TARGET}" install -r --no-incremental "${APK_PATH}"
    
    echo -e "${GREEN}[✔] Sideload successful! Launching Picto-Sight H8 remote...${NC}"
    adb -s "${TARGET}" shell monkey -p llc.resgen.pictosight -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
done

echo -e "${GREEN}==================================================================${NC}"
echo -e "${GREEN}[VAULT-APPROVED] Handset linked! Ready for wasteland RF surveillance.${NC}"
echo -e "${GREEN}==================================================================${NC}"
