# Bryck SDK Installation Guide

> **Before starting the installation, complete all checkpoints below in order.**

---

## Table of Contents

1. [Pre-Installation Checkpoints](#1-pre-installation-checkpoints)
2. [APT Sources Configuration](#2-apt-sources-configuration)
3. [Kernel Installation & GRUB Configuration](#3-kernel-installation--grub-configuration)
4. [Package Installation](#4-package-installation)
5. [Cloud SDK & Azure CLI Setup](#5-cloud-sdk--azure-cli-setup)
6. [Network Setup](#6-network-setup)
7. [SSL Certificate Generation](#7-ssl-certificate-generation)
8. [SSH Key Setup](#8-ssh-key-setup)
9. [Build Deployment](#9-build-deployment)
10. [Post-Installation Configuration](#10-post-installation-configuration)
11. [Desktop Environment & Services](#11-desktop-environment--services)
12. [NetworkManager Configuration](#12-networkmanager-configuration)

---

## 1. Pre-Installation Checkpoints

### 1.1 Configure DNS

```bash
sudo vi /etc/resolv.conf
```

Add the following line:

```
nameserver 8.8.8.8
```

### 1.2 Create Users

```bash
sudo adduser bryck       # password: while(1);
sudo groupdel admin
sudo adduser admin       # password: BryckAdm1n
```

### 1.3 Update the Sudoers File

```bash
sudo su
visudo
```

Add the following entries:

```
Cmnd_Alias MORE = /usr/sbin/nvme, /usr/bin/lsblk, /usr/bin/journalctl, /usr/sbin/parted, /usr/sbin/partprobe, /usr/bin/xxd, /usr/bin/dd, /opt/ansible/drivers/lsblk, /usr/sbin/sgdisk, /opt/ansible/drivers/cryptsetup, /usr/sbin/zpool, /usr/sbin/zfs, /usr/sbin/sysctl, /usr/bin/systemctl, /usr/bin/chmod, /usr/bin/umount, /usr/bin/mount, /usr/sbin/dmsetup, /usr/sbin/mdadm, /usr/sbin/lsof, /usr/bin/df, /sbin/ethtool, /sbin/blockdev
Defaults!MORE !syslog, !pam_session
bryck  ALL=(ALL)       NOPASSWD: ALL
wsgi  ALL=(ALL)       NOPASSWD: ALL
admin  ALL=(ALL)      ALL
```

---

## 2. APT Sources Configuration

### 2.1 Backup and Replace sources.list

```bash
sudo mv /etc/apt/sources.list /home/bryck/bkp_sources.list
sudo vi /etc/apt/sources.list
```

Add the following content:

```
## Note, this file is written by cloud-init on first boot of an instance
## modifications made here will not survive a re-bundle.
## if you wish to make changes you can:
## a.) add 'apt_preserve_sources_list: true' to /etc/cloud/cloud.cfg
##     or do the same in user-data
## b.) add sources in /etc/apt/sources.list.d
## c.) make changes to template file /etc/cloud/templates/sources.list.tmpl

# See http://help.ubuntu.com/community/UpgradeNotes for how to upgrade to
# newer versions of the distribution.
deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy main restricted
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy main restricted

## Major bug fix updates produced after the final release of the
## distribution.
deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-updates main restricted
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-updates main restricted

## N.B. software from this repository is ENTIRELY UNSUPPORTED by the Ubuntu
## team. Also, please note that software in universe WILL NOT receive any
## review or updates from the Ubuntu security team.
deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy universe
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy universe
deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-updates universe
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-updates universe

## N.B. software from this repository is ENTIRELY UNSUPPORTED by the Ubuntu
## team, and may not be under a free licence. Please satisfy yourself as to
## your rights to use the software. Also, please note that software in
## multiverse WILL NOT receive any review or updates from the Ubuntu
## security team.
deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy multiverse
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy multiverse
deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-updates multiverse
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-updates multiverse

## N.B. software from this repository may not have been tested as
## extensively as that contained in the main release, although it includes
## newer versions of some applications which may provide useful features.
## Also, please note that software in backports WILL NOT receive any review
## or updates from the Ubuntu security team.
deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-backports main restricted universe multiverse
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-backports main restricted universe multiverse

deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-security main restricted
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-security main restricted
deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-security universe
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-security universe
deb http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-security multiverse
# deb-src http://repos.tsecond.ai/arm/mirror/ports.ubuntu.com/ubuntu-ports jammy-security multiverse
```

### 2.2 Update Package Lists

```bash
sudo apt update
```

---

## 3. Kernel Installation & GRUB Configuration

### 3.1 Install Kernel 6.5.0-45

```bash
sudo apt install -y linux-image-6.5.0-45-generic linux-headers-6.5.0-45-generic linux-modules-6.5.0-45-generic linux-modules-extra-6.5.0-45-generic sshpass vim
```

### 3.2 Set Default GRUB Entry

List available kernel entries:

```bash
awk -F\' '/menuentry / {print $2}' /boot/grub/grub.cfg
```

> Check which entry contains `6.5.0-45`. Entries start from 0 — in this case it's listed at position 2.

Edit GRUB defaults:

```bash
vi /etc/default/grub
```

Set:

```
GRUB_DEFAULT="1>Ubuntu, with Linux 6.5.0-45-generic"
```

```bash
sudo update-grub
```

### 3.3 Change Hostname

Change the hostname to `bryckserver` or `bryckmini` using:

```bash
sudo nmtui
```

### 3.4 Disable GRUB Password Protection

```bash
sudo vi /etc/grub.d/40_custom
```

Comment out the following lines:

```
#set superusers="admin"
#password_pbkdf2 admin grub.pbkdf2.sha512.10000.5EB1FF92FDD89BDAF3395174282C77430656A6DBEC1F9289D5F5DAD17811AD0E2196D0E49B49EF31C21972669D180713E265BB2D1D4452B2EA9C7413C3471C53.F533423479EE7465785CC2C79B637BDF77004B5CC16C1DDE806BCEA50BF411DE04DFCCE42279E2E1F605459F1ABA3A0928CE9271F2C84E7FE7BF575DC22935B1
```

```bash
sudo update-grub
sudo reboot
```

---

## 4. Package Installation

### 4.1 Post-Reboot Kernel Modules

```bash
sudo apt-get install linux-modules-extra-$(uname -r) -y
sudo apt install linux-headers-$(uname -r) -y
sudo apt-get install network-manager -y
```

### 4.2 Configure APT Sandbox

```bash
sudo vi /etc/apt/apt.conf.d/10sandbox
```

Add:

```
APT::Sandbox::User "root";
```

### 4.3 Install Required Packages

```bash
sudo apt install python3 python3-pip vsftpd sysbench net-tools ethtool cryptsetup fio unzip pkg-config libsystemd-dev -y
sudo apt install rclone curl jq sysbench -y
sudo pip3 install ansible
sudo pip3 install pyroute2
```

### 4.4 Configure rc.local

```bash
sudo su
echo "exit 0" >> /etc/rc.local
```

---

## 5. Cloud SDK & Azure CLI Setup

### 5.1 Google Cloud SDK

```bash
curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg

echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee -a /etc/apt/sources.list.d/google-cloud-sdk.list

sudo pip3 install --no-cache-dir -U crcmod
```

### 5.2 Azure CLI

```bash
curl -sL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor | sudo tee /usr/share/keyrings/microsoft.gpg > /dev/null

echo "deb [arch=arm64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/azure-cli.list
```

---

## 6. Network Setup

### 6.1 Flush iptables Rules

```bash
sudo su
sudo rm /etc/iptables/rules.*
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT
iptables -t nat -F
iptables -t mangle -F
iptables -F
iptables -X
ip6tables -P INPUT ACCEPT
ip6tables -P FORWARD ACCEPT
ip6tables -P OUTPUT ACCEPT
ip6tables -t nat -F
ip6tables -t mangle -F
ip6tables -F
ip6tables -X
sudo iptables-save > /etc/iptables/rules.v4
```

### 6.2 Configure NetworkManager Managed Devices

```bash
sudo vim /usr/lib/NetworkManager/conf.d/10-globally-managed-devices.conf
```

Add ethernet to the unmanaged-devices exception list:

```
unmanaged-devices=*,except:type:wifi,except:type:gsm,except:type:cdma,except:type:ethernet
```

---

## 7. SSL Certificate Generation

```bash
sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
-days 1825 \
-keyout /etc/ssl/private/bryckweb-selfsigned.key \
-out /etc/ssl/certs/bryckweb-selfsigned.crt \
-subj "/C=US/ST=California/L=San Jose/O=TSecond Inc./CN=bryckmini/emailAddress=support@tsecond.ai" \
-addext "keyUsage=digitalSignature,keyEncipherment"
```

---

## 8. SSH Key Setup

```bash
sudo su - bryck
ssh-keygen -t rsa -N "" -f /home/bryck/.ssh/id_rsa
sshpass -p 'while(1);' ssh-copy-id -o StrictHostKeyChecking=no bryck@localhost
```

---

## 9. Build Deployment

### 9.1 Copy Build from 193

Login to 193 and copy the build and inventory:

```bash
scp builds/tsecond-bryck-5.0.0.15.tar.gz inventory <ip>:
```

### 9.2 Install the Build on Target

```bash
tar -xvf tsecond-bryck-5.0.0.15.tar.gz
```

Edit `/home/bryck/inventory` and change:

```
bryck_type=mini
```

Then run:

```bash
cd tsecond-bryck-5.0.0.15
python3 bryckdeploy install -v
```

---

## 10. Post-Installation Configuration

### 10.1 Update Bryck Config

Edit `/etc/bryck/bryckutil/config.json` and set:

```json
"enable_hot_plug": "False"
"bryck_type": "mini"
```

### 10.2 Set Environment Variable

```bash
sudo vi /etc/bash.bashrc
```

Add:

```bash
export HAILO_MONITOR=1
```

---

## 11. Desktop Environment & Services

### 11.1 Install XFCE & XRDP

```bash
sudo apt install xfce4 xfce4-goodies -y
sudo apt install xrdp -y
```

### 11.2 Disable Unnecessary Services

```bash
sudo systemctl disable netfilter-persistent openibd
```

### 11.3 Configure OOB Network Interface

```bash
sudo nmcli device set oob_net0 managed yes
sudo nmcli connection modify oob_net0 autoconnect yes
sudo nmcli device connect oob_net0
sudo nmcli device reapply oob_net0
```

---

## 12. NetworkManager Configuration

### 12.1 Disable Mellanox Keyfile

```bash
sudo vi /etc/NetworkManager/conf.d/40-mlnx.conf
```

Comment out the unmanaged-devices line:

```ini
[keyfile]
#unmanaged-devices+=driver:mlx5_core;driver:mlx5e_rep;driver:vxlan
```

### 12.2 Switch from systemd-networkd to NetworkManager

```bash
sudo systemctl reload NetworkManager.service
sudo systemctl disable --now systemd-networkd.service systemd-networkd.socket networkd-dispatcher.service && sudo systemctl restart NetworkManager
sudo apt purge netplan netplan.io -y
```

### 12.3 Configure Interfaces with nmtui

```bash
sudo nmtui
```

- Change the name of the interface from `netplan-oob_net0` to `oob_net0`
- Add `p0` and `p1` interfaces
- Save and exit

```bash
sudo systemctl reload NetworkManager
```

### 12.4 Install pyroute2

```bash
/opt/bryck/.venv/bryck/bin/pip3 install pyroute2==0.7.12
```

### 12.5 Disable Cloud Network Config

```bash
sudo vi /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
```

Add:

```
{config: disabled}
```

### 12.6 Disable Default Route on tmfifo_net0

Edit `/etc/NetworkManager/system-connections/tmfifo_net0.nmconnection` and comment out `dns` and `route1`:

```ini
[ipv4]
address1=192.168.100.2/30
#dns=192.168.100.1;
method=manual
#route1=0.0.0.0/0,192.168.100.1,1025
```