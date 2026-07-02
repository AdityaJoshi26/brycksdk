1. Network DNS Configuration

We are unlinking the existing resolv.conf which may be a symlink managed by systemd-resolved and replacing it with a static DNS configuration pointing to Google's public DNS server. This ensures the system has a reliable and consistent DNS resolution.

bash

sudo unlink /etc/resolv.conf

Open the file and add the nameserver entry.

bash

sudo vi /etc/resolv.conf

Add the following content inside the file:

bash

nameserver 8.8.8.8

2. User Management

We are creating two users:

bryck — the primary operational user for the Bryck system with password while(1);

admin — the administrative user with password BryckAdm1n

The existing admin group is deleted before creating the new admin user to avoid group conflicts.

bash

# Create the bryck user
# Set password as: while(1);
sudo adduser bryck

# Remove existing admin group to avoid conflicts
sudo groupdel admin

# Create the admin user
# Set password as: BryckAdm1n
sudo adduser admin

While adding admin user we have to add below details for which continue using ENTER so default values would be used.

bryck@bryck-Super-Server:~$ sudo adduser admin
Adding user `admin' ...
Adding new group `admin' (1001) ...
Adding new user `admin' (1001) with group `admin' ...
The home directory `/home/admin' already exists.  Not copying from `/etc/skel'.
New password:
Retype new password:
passwd: password updated successfully
Changing the user information for admin
Enter the new value, or press ENTER for the default
        Full Name []: admin
        Room Number []:
        Work Phone []:
        Home Phone []:
        Other []:
Is the information correct? [Y/n] Y

3. Sudoers Configuration

We are configuring the sudoers file to grant appropriate permissions:

bryck and wsgi users get full passwordless sudo access

admin user gets full sudo access with password

A command alias MORE is defined to suppress syslog and PAM session logging for specific system commands

bash

sudo su
visudo

Add the following content inside the sudoers file:

bash

Cmnd_Alias MORE = /usr/sbin/nvme, /usr/bin/lsblk, /usr/bin/journalctl, /usr/sbin/parted, \
/usr/sbin/partprobe, /usr/bin/xxd, /usr/bin/dd, /opt/ansible/drivers/lsblk, \
/usr/sbin/sgdisk, /opt/ansible/drivers/cryptsetup, /usr/sbin/zpool, /usr/sbin/zfs, \
/usr/sbin/sysctl, /usr/bin/systemctl, /usr/bin/chmod, /usr/bin/umount, /usr/bin/mount, \
/usr/sbin/dmsetup, /usr/sbin/mdadm, /usr/sbin/lsof, /usr/bin/df, /sbin/ethtool, /sbin/blockdev

Defaults!MORE !syslog, !pam_session
bryck  ALL=(ALL)       NOPASSWD: ALL
wsgi   ALL=(ALL)       NOPASSWD: ALL
admin  ALL=(ALL)       ALL


4. Kernel Installation & Boot Configuration

We are installing a specific kernel version 6.5.0-45-generic along with its headers, modules, and extra modules. This is required for Bryck hardware compatibility. After installation, we update the GRUB bootloader to boot into this specific kernel version by default and reboot the system.

bash

sudo apt install -y linux-image-6.5.0-45-generic \
    linux-headers-6.5.0-45-generic \
    linux-modules-6.5.0-45-generic \
    linux-modules-extra-6.5.0-45-generic \
    sshpass vim -y

List all available GRUB menu entries to find the correct index for kernel 6.5.0-45. Start counting from 0 and note the position of the 6.5.0-45 entry.

bash

awk -F\' '/menuentry / {print $2}' /boot/grub/grub.cfg


Open the GRUB configuration file and update the default kernel entry.

bash

sudo vi /etc/default/grub

Modify the existing GRUB_DEFAULT entry as below:

bash

GRUB_DEFAULT="1>Ubuntu, with Linux 6.5.0-45-generic"

Apply the GRUB changes and reboot.

bash

sudo update-grub
sudo reboot

After reboot, install additional kernel modules for the running kernel:

bash

sudo apt-get install linux-modules-extra-$(uname -r) -y
sudo apt install linux-headers-$(uname -r) -y
sudo apt-get install network-manager -y

5. APT Sandbox Configuration

We are creating an APT sandbox configuration file to allow APT package operations to run as root. This is required in some restricted environments where the default sandbox user causes package installation failures.

bash

sudo vi /etc/apt/apt.conf.d/10sandbox

Add the following content inside the file:

bash

APT::Sandbox::User "root";

6. System Package Installation

We are installing all required system-level packages and Python libraries needed for Bryck to operate. This includes Python3, vsftpd FTP server, cryptsetup for encrypted volumes, fio and sysbench for performance benchmarking, rclone for cloud transfer operations, ansible for automated deployment, and pyroute2 for network configuration. We also ensure rc.local exits cleanly on boot.

bash

sudo apt install python3 python3-pip vsftpd sysbench net-tools ethtool \
    cryptsetup fio unzip pkg-config libsystemd-dev cron -y

sudo apt install rclone curl jq sysbench -y

sudo pip3 install ansible

sudo pip3 install pyroute2

sudo su
echo "exit 0" >> /etc/rc.local


7. Network Policy Setup

We are flushing and resetting all iptables and ip6tables firewall rules to allow all traffic on INPUT, FORWARD, and OUTPUT. This is necessary to ensure that Bryck network operations are not blocked by any pre-existing firewall rules. All NAT and mangle tables are also cleared.

sudo su

Copy and execute the entire block below at once.

bash

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
sudo iptables-save

8. SSH Key Setup

We are switching to the bryck user and generating an RSA SSH key pair without a passphrase for passwordless SSH authentication. We then copy the public key to localhost so that the Bryck automation scripts can SSH into the local machine without manual password entry.

bash

sudo su - bryck

ssh-keygen -t rsa -N "" -f /home/bryck/.ssh/id_rsa

sshpass -p 'while(1);' ssh-copy-id -o StrictHostKeyChecking=no bryck@localhost


9. SSL Certificate Generation

We are generating a self-signed SSL certificate using elliptic curve cryptography prime256v1 for the Bryck web interface. This certificate enables HTTPS access to the Bryck UI. The certificate is valid for 5 years (1825 days) and is stored in the standard system SSL directories.

bash

sudo openssl req -x509 -newkey ec \
    -pkeyopt ec_paramgen_curve:prime256v1 \
    -nodes \
    -days 1825 \
    -keyout /etc/ssl/private/bryckweb-selfsigned.key \
    -out /etc/ssl/certs/bryckweb-selfsigned.crt \
    -subj "/C=US/ST=California/L=San Jose/O=TSecond Inc./CN=bryck/emailAddress=support@tsecond.ai" \
    -addext "keyUsage=digitalSignature,keyEncipherment"


10. Bryck Build Deployment

We are downloading the Bryck build tarball and inventory file from the TSecond repository. After extracting the build, we configure the inventory file to set the correct bryck_type and run the Bryck deployment script to install the full Bryck system.

Log in to the target machine (e.g., machine 28) before executing these steps.

bash

wget http://repos.tsecond.ai/ubuntu//tsecond-bryck-5.0.0.X.tar.gz
wget http://repos.tsecond.ai/ubuntu/inventory

or scp from 192.168.6.28 system, login to 28 system and goto /home/bryck/build/
scp tsecond-bryck-5.0.0.151.tar.gz bryck@<system-ip>:/home/bryck/.

On Build installation system untar the file
tar -xvf tsecond-bryck-5.0.0.X.tar.gz

Copy inventory file from 28 system, login to 28 system and goto /home/bryck/

scp inventory bryck@<system-ip>:/home/bryck/.

Open the inventory file and set bryck_type=bryck.

bash

vi /home/bryck/inventory

Change the following line inside the inventory file:

bash

bryck_type=bryck

Run the deployment script.

bash

cd tsecond-bryck-5.0.0.X
python3 bryckdeploy install -v

validate installation status from ansible log

tail -f /opt/ansible/ansible.log

If it is Indian machine :

sudo update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
source /etc/default/locale
python3 bryckdeploy uninstall -v
python3 bryckdeploy install -v

After deployment, configure NetworkManager to manage Ethernet interfaces.

bash

sudo vim /usr/lib/NetworkManager/conf.d/10-globally-managed-devices.conf

Append the following to the unmanaged-devices list inside the file:

bash

except:type:ethernet



Set the Hailo monitor environment variable for AI hardware support.

bash

sudo vi /etc/bash.bashrc

Add the following line inside the file:

bash

export HAILO_MONITOR=1

11. Cloud CLI Tools Installation

We are installing all three major cloud provider CLI tools required for Bryck cloud transfer operations. This includes Google Cloud CLI for GCP integration, crcmod Python library for optimized GCP transfer performance, and Azure CLI for Microsoft Azure integration.

Google Cloud CLI Setup

bash

curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | \
    sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg

echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] \
    https://packages.cloud.google.com/apt cloud-sdk main" | \
    sudo tee -a /etc/apt/sources.list.d/google-cloud-sdk.list
    
sudo apt-get update

sudo apt-get install google-cloud-cli

sudo pip3 install --no-cache-dir -U crcmod


Azure CLI Setup

bash

curl -sL https://packages.microsoft.com/keys/microsoft.asc | \
    gpg --dearmor | \
    sudo tee /usr/share/keyrings/microsoft.gpg > /dev/null

echo "deb [arch=arm64 signed-by=/usr/share/keyrings/microsoft.gpg] \
    https://packages.microsoft.com/repos/azure-cli/ $(lsb_release -cs) main" | \
    sudo tee /etc/apt/sources.list.d/azure-cli.list

sudo apt update
sudo apt install azure-cli -y


12. NFS Configuration

We are validating the NFS export options in the Ansible configuration to ensure that anonuid and anongid values correctly reference the bryck user's UID and GID. This is critical for proper NFS access permissions on the Bryck mount point.

bash

cat /opt/ansible/roles/add-export/vars/main.yml

Verify the following configuration is present and that anonuid and anongid match the UID and GID of the bryck user on your system:

bash

nfs_options: "*(rw,async,all_squash,no_subtree_check,insecure,anonuid=1001,anongid=1001)"

13. Additional Package Installation

We are installing additional packages required for Bryck's full functionality. krb5-user provides Kerberos authentication support and ubuntu-desktop is required for certain UI components.

bash

sudo apt install krb5-user -y 

above cmd output will give interactive prompt, 
in that don't specify any value, just click ok and contiune


#do not install this if install media is a server !
sudo apt install ubuntu-desktop -y

14. Download BryckCLI and NFSD Patch

We are downloading two additional components from the TSecond repository. BryckCLI provides a command-line interface for managing Bryck operations and the NFSD patch replaces the default NFSD kernel module with a custom one optimized for Bryck.

bash

wget http://repos.tsecond.ai/ubuntu/bryckcli.tar.gz
wget http://repos.tsecond.ai/ubuntu/nfsd_patch.tar.gz

15. NFSD Patch & SMB Cleanup

We are stopping the NFS server and replacing the default NFSD kernel module with Bryck's custom NFSD patch for optimized NFS performance. We are also stopping and disabling the Samba services smbd and nmbd and removing their service files as Bryck uses its own SMB implementation via ksmbd.

Apply NFSD Patch

bash

sudo systemctl stop nfs-server
sudo systemctl stop nfs-kernel-server

tar -xzvf nfsd_patch.tar.gz
cd nfsd_patch/
sudo bash replace_nfsd_module.sh

Remove Samba Services

bash

sudo systemctl stop smbd nmbd
sudo systemctl disable smbd nmbd
sudo rm -rf /lib/systemd/system/smbd.service \
            /lib/systemd/system/nmbd.service

16. Serial Number Configuration

We are creating the Bryck serial number configuration file. This file is required by the Bryck system to identify the device. The serial number should be populated with the device's actual serial number. An empty string is used as a placeholder during initial setup.

bash

sudo vi /etc/bryck/serial_number.json

Add the following content inside the file. Replace the empty string with the actual serial number if available:

bash

{"serial_number":""}

17. BryckCLI Installation

We are installing the BryckCLI tool which provides a command-line interface to manage all Bryck operations such as Format, Mount, Eject, Erase, and Scan from the terminal.

bash

tar -xzvf bryckcli.tar.gz
cd bryckcli/
bash deploy_bryckcli install

Expected output after successful installation:

Code

Bryckcli is Installed. Use the command "bryckcli" to manage bryck

18. Final Network Manager Fix

If the network interfaces are still showing as unmanaged after previous configuration steps, we apply a final fix by updating NetworkManager.conf to set managed=true. If the issue persists, we completely remove netplan and disable systemd-networkd to hand over full network management to NetworkManager, followed by a reboot via IPMI.

bash

sudo vi /etc/NetworkManager/NetworkManager.conf

Ensure the following is present inside the file under [ifupdown]:

bash

[ifupdown]
managed=true

If network interfaces are still unmanaged after the above change, run the following:

bash

sudo apt purge netplan netplan.io -y

sudo systemctl disable --now systemd-networkd.service \
    systemd-networkd.socket \
    networkd-dispatcher.service

Check the nvme drive model and update in /etc/bryck/bryckutil/config.json

#List the drives 
sudo nvme list

#check the drive model eg: EXPE4M7680GB-AA-TSD001, EXNVME_W1
#Check if the drive model is part of /etc/bryck/bryckutil/config.json 
#file "bryck_drive_model" section, if the drive model not there update like below
"bryck_drive_model": ["Linux","KXG50ZNV1T02 TOSHIBA","KXG50PNV1T02 TOSHIBA", "KXG50PNV2T04 TOSHIBA","KXG50ZNV256G TOSHIBA","Corsair MP600 PRO NH"],

Check the OS drive is NVMe if so need to add this drive part of skip_drive list

#List the drives
sudo nvme list

#Identify the OS drive
df -kh

# Add the OS drive and other drive which should not be part of zfs
vi /etc/bryck/bryckutil/config.json

#update section "skip_drives" like below
"skip_drives": ["38KF709MF3JP","38KF7003F3JP"],

🔄 After completing the above steps, reboot the machine from IPMI to apply all changes.