#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.


set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPPERCACHE_ENV_FILE:-$REPO_ROOT/config/preppercache.env}"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing config file: $ENV_FILE"
  exit 1
fi

# shellcheck source=./load_env.sh
source "$REPO_ROOT/scripts/load_env.sh"
load_preppercache_env "$ENV_FILE"

if [[ "${PREPPERCACHE_AP_ENABLED:-0}" != "1" ]]; then
  echo "Access point mode is disabled in config/preppercache.env."
  echo "Stopping and disabling AP-related services."
  systemctl stop hostapd 2>/dev/null || true
  systemctl stop dnsmasq 2>/dev/null || true
  systemctl stop preppercache-ap-network.service 2>/dev/null || true
  systemctl disable hostapd 2>/dev/null || true
  systemctl disable dnsmasq 2>/dev/null || true
  systemctl disable preppercache-ap-network.service 2>/dev/null || true
  exit 0
fi

: "${PREPPERCACHE_AP_ADDRESS:=10.10.10.10}"
: "${PREPPERCACHE_AP_NETMASK:=255.255.255.0}"
: "${PREPPERCACHE_AP_CIDR:=24}"
: "${PREPPERCACHE_AP_DHCP_START:=10.10.10.50}"
: "${PREPPERCACHE_AP_DHCP_END:=10.10.10.150}"
: "${PREPPERCACHE_AP_DHCP_LEASE:=12h}"
: "${PREPPERCACHE_AP_PASSPHRASE:=prepperlocal}"

if [[ ${#PREPPERCACHE_AP_PASSPHRASE} -lt 8 || ${#PREPPERCACHE_AP_PASSPHRASE} -gt 63 ]]; then
  echo "PREPPERCACHE_AP_PASSPHRASE must be between 8 and 63 characters."
  exit 1
fi

echo "Installing access point packages..."
apt install -y hostapd dnsmasq

echo "Stopping services before reconfiguration..."
systemctl stop hostapd || true
systemctl stop dnsmasq || true

if command -v rfkill >/dev/null 2>&1; then
  echo "Unblocking Wi-Fi radio..."
  rfkill unblock wifi || true
fi

install -d -m 0755 /etc/hostapd
install -d -m 0755 /etc/dnsmasq.d

cat > /etc/hostapd/hostapd.conf <<EOF
country_code=$PREPPERCACHE_AP_COUNTRY
interface=$PREPPERCACHE_AP_INTERFACE
driver=nl80211
ssid=$PREPPERCACHE_AP_SSID
hw_mode=g
channel=$PREPPERCACHE_AP_CHANNEL
ieee80211n=1
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$PREPPERCACHE_AP_PASSPHRASE
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF

cat > /etc/default/hostapd <<EOF
DAEMON_CONF="/etc/hostapd/hostapd.conf"
EOF

cat > /etc/dnsmasq.d/preppercache-ap.conf <<EOF
interface=$PREPPERCACHE_AP_INTERFACE
bind-interfaces
domain-needed
bogus-priv
dhcp-range=$PREPPERCACHE_AP_DHCP_START,$PREPPERCACHE_AP_DHCP_END,$PREPPERCACHE_AP_NETMASK,$PREPPERCACHE_AP_DHCP_LEASE
dhcp-option=3,$PREPPERCACHE_AP_ADDRESS
dhcp-option=6,$PREPPERCACHE_AP_ADDRESS
address=/preppercache.local/$PREPPERCACHE_AP_ADDRESS
EOF

cat > /etc/systemd/system/preppercache-ap-network.service <<EOF
[Unit]
Description=Configure PrepperCache AP network interface
Before=hostapd.service dnsmasq.service
Wants=network-pre.target
After=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/ip link set $PREPPERCACHE_AP_INTERFACE down
ExecStart=/usr/sbin/ip addr flush dev $PREPPERCACHE_AP_INTERFACE
ExecStart=/usr/sbin/ip addr add $PREPPERCACHE_AP_ADDRESS/$PREPPERCACHE_AP_CIDR dev $PREPPERCACHE_AP_INTERFACE
ExecStart=/usr/sbin/ip link set $PREPPERCACHE_AP_INTERFACE up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd and enabling AP services..."
systemctl daemon-reload
systemctl unmask hostapd || true
systemctl enable preppercache-ap-network.service
systemctl enable hostapd
systemctl enable dnsmasq
systemctl restart preppercache-ap-network.service
systemctl restart hostapd
systemctl restart dnsmasq

echo "Access point configuration complete."
echo "SSID: $PREPPERCACHE_AP_SSID"
echo "Gateway: $PREPPERCACHE_AP_ADDRESS"
