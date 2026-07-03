# Bryck SDK - Remote Setup Script

Automated provisioning tool that connects to a Bryck device via SSH, auto-detects the device type from its architecture, and performs a full installation pipeline.

## Device Types

| Architecture | Device Type | Hardware |
|---|---|---|
| `arm64` / `aarch64` | **bryckmini** | BlueField-3 DPU (Nvidia) |
| `x86-64` / `x86_64` | **bryckserver** | Supermicro server |

## Requirements

```bash
pip install paramiko
```

## Usage

```bash
python3 bryck_setup.py <ip_address> [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--username`, `-u` | `bryck` | SSH username |
| `--password`, `-p` | `while(1);` | SSH password |
| `--port` | `22` | SSH port |
| `--bryck-build`, `-b` | None | Build name (e.g. `tsecond-bryck-5.0.0.15`) |
| `--dry-run` | Off | Print all commands without executing |
| `--device-type` | Auto-detect | Required with `--dry-run`. One of: `bryckmini`, `bryckserver` |

## Examples

### Standard setup (auto-detects device type)

```bash
python3 bryck_setup.py 192.168.1.100
```

### Full install with build deployment

```bash
python3 bryck_setup.py 192.168.1.100 --bryck-build tsecond-bryck-5.0.0.15
```

### Custom credentials

```bash
python3 bryck_setup.py 10.0.0.5 --username admin --password 'BryckAdm1n'
```

### Dry run (no SSH, prints all commands)

```bash
python3 bryck_setup.py 192.168.1.100 --dry-run --device-type bryckmini
python3 bryck_setup.py 192.168.1.100 --dry-run --device-type bryckserver --bryck-build tsecond-bryck-5.0.0.15
```

## Task Pipeline

The script executes these tasks in order:

| # | Task | bryckserver | bryckmini |
|---|------|:-----------:|:---------:|
| 1 | Configure DNS (`/etc/resolv.conf`) | ✓ | ✓ |
| 2 | Create Users (bryck & admin) | ✓ | ✓ |
| 3 | Configure Sudoers | ✓ | ✓ |
| 4 | Configure APT Sources (tsecond mirror) | ✓ | ✓ |
| 5 | Install Kernel 6.5.0-45 & GRUB config | ✓ | skip |
| 6 | Set Hostname | ✓ | ✓ |
| 7 | Disable GRUB Password | ✓ | skip |
| 8 | Reboot & Install Post-Kernel Packages | reboot + install | install only |
| 9 | Configure APT Sandbox | ✓ | ✓ |
| 10 | Install System Packages & Cloud SDKs | ✓ | ✓ |
| 11 | Flush IPTables | ✓ | ✓ |
| 12 | Configure NetworkManager, SSL & SSH Keys | ✓ | ✓ |
| 13 | Deploy Bryck Build (if `--bryck-build` set) | ✓ | ✓ |
| 14 | Post-Deploy Desktop & Network Config | ✓ | ✓ |

## Task Details

### 1. Configure DNS
Writes `nameserver 8.8.8.8` to `/etc/resolv.conf`.

### 2. Create Users
Creates `bryck` (password: `while(1);`) and `admin` (password: `BryckAdm1n`).

### 3. Configure Sudoers
Appends NOPASSWD entries for bryck/wsgi and command aliases.

### 4. Configure APT Sources
Replaces `/etc/apt/sources.list` with the TSecond mirror (architecture-appropriate).

### 5. Install Kernel (bryckserver only)
Installs `linux-image-6.5.0-45-generic` and sets GRUB to boot it.

### 6. Set Hostname
Sets hostname to `bryckmini` or `bryckserver` based on device type.

### 7. Disable GRUB Password (bryckserver only)
Comments out `set superusers` and `password_pbkdf2` in `/etc/grub.d/40_custom`.

### 8. Reboot & Post-Kernel
Reboots (bryckserver only), then installs `linux-modules-extra`, `linux-headers`, and `network-manager`.

### 9. Configure APT Sandbox
Creates `/etc/apt/apt.conf.d/10sandbox` to allow apt downloads as root.

### 10. Install Packages & Cloud SDKs
- **APT:** python3, pip, vsftpd, sysbench, net-tools, ethtool, cryptsetup, fio, rclone, curl, jq
- **Pip:** ansible, pyroute2, boto3, netifaces, crcmod
- **Repos:** Google Cloud SDK, Azure CLI

### 11. Flush IPTables
Resets all iptables/ip6tables rules to ACCEPT and saves to `/etc/iptables/rules.v4`.

### 12. Configure NetworkManager, SSL & SSH Keys
- Writes `/usr/lib/NetworkManager/conf.d/10-globally-managed-devices.conf` with `[keyfile]` section
- Generates self-signed SSL cert for bryckweb
- Generates SSH key for bryck user and copies to localhost

### 13. Deploy Bryck Build
- Stops existing bryck services
- Runs `bryckdeploy uninstall` (clean uninstall)
- SCPs build tarball from build server (192.168.6.193 for arm64, 192.168.6.28 for amd64)
- Downloads inventory, sets `bryck_type`
- Runs `bryckdeploy install -v` (monitors ansible.log for up to 40 minutes)
- Updates `/etc/bryck/bryckutil/config.json`

### 14. Post-Deploy Desktop & Network Config
- Installs xfce4 + xrdp
- Disables netfilter-persistent and openibd
- Configures oob_net0 via nmcli (managed, autoconnect)
- Writes `/etc/NetworkManager/conf.d/40-mlnx.conf` (disables mlx5 unmanaged)
- Disables systemd-networkd, restarts NetworkManager
- Purges netplan
- Renames `netplan-oob_net0` → `oob_net0`, adds `p0`/`p1` interfaces (via nmcli)
- Installs pyroute2==0.7.12, boto3, netifaces in bryck venv
- Disables cloud network config
- Disables default route on tmfifo_net0

## Logs

Logs are written to `logs/setup_<ip>.log` (one per device IP).

## Notes

- The script **cannot** power off the device. The only power action is a reboot (bryckserver only, after kernel change).
- On **bryckmini**, all network configuration uses `nmcli` (not `nmtui`) for fully programmatic operation.
- If a task fails, subsequent tasks still attempt to run. Check the summary at the end.
- SSH reconnection is automatic if the transport drops mid-pipeline.
