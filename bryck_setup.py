#!/usr/bin/env python3
"""
Bryck SDK - Remote Setup Script
================================
Connects to a Bryck device via SSH, auto-detects device type from architecture,
and performs installation setup tasks.

Device Detection:
    arm64   -> bryckmini  (BlueField-3 DPU)
    x86-64  -> bryckserver (Supermicro server)

Usage:
    python3 bryck_setup.py <ip_address> [--username USERNAME] [--password PASSWORD]

Examples:
    python3 bryck_setup.py 192.168.1.100
    python3 bryck_setup.py 192.168.1.100 --username bryck --password 'while(1);'
"""

import argparse
import logging
import socket
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko is required. Install it with: pip3 install paramiko")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class BryckType(str, Enum):
    BRYCKSERVER = "bryckserver"
    BRYCKMINI = "bryckmini"


# Architecture string -> device type mapping
ARCH_TO_TYPE = {
    "arm64": BryckType.BRYCKMINI,
    "aarch64": BryckType.BRYCKMINI,
    "x86-64": BryckType.BRYCKSERVER,
    "x86_64": BryckType.BRYCKSERVER,
}


@dataclass
class DeviceConfig:
    """Configuration for a target Bryck device."""
    ip: str
    username: str = "bryck"
    password: str = "while(1);"
    bryck_type: BryckType | None = None  # Auto-detected after SSH
    ssh_port: int = 22
    timeout: int = 30
    # Budget for slow package/download/deploy commands (apt, pip, wget, tar,
    # scp, update-grub, ...). The 30s default is only meant for quick shell
    # commands; anything that touches the network or dpkg needs far longer or
    # it gets wrongly abandoned mid-install.
    long_timeout: int = 1800  # 30 minutes
    # For long-running commands we do NOT abandon on a wall-clock timer: as long
    # as the command keeps producing output we keep waiting (a kernel/apt install
    # can legitimately take a long time). We only give up if the command goes
    # completely silent — no stdout/stderr at all — for this many seconds, which
    # indicates it is genuinely stuck (e.g. waiting on an interactive prompt).
    idle_timeout: int = 600  # 10 minutes of total silence => considered hung
    bryck_build: str | None = None  # e.g. "tsecond-bryck-5.0.0.15"
    skip_reboot: bool = False  # Skip the reboot step (--skip-reboot)


# Substrings that mark a command as slow (package management, downloads,
# archive extraction, boot config, deploy). When a caller does not pass an
# explicit timeout, any command containing one of these gets config.long_timeout
# instead of the short config.timeout, so kernel/apt installs are never
# abandoned mid-flight.
LONG_RUNNING_MARKERS: tuple[str, ...] = (
    "apt ", "apt-get", "aptitude", "dpkg", "dist-upgrade", "--fix-broken",
    "pip install", "pip3 install", "wget", "curl", "update-grub",
    "tar -x", "tar -c", "tar -z", "tar -t", "gzip", "scp ", "sshpass",
    "ssh-copy-id", "modprobe", "bryckdeploy", "deploy_bryckcli",
    "replace_nfsd_module", "openssl req",
)


# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logger(device_ip: str) -> logging.Logger:
    """Create a logger that writes to both console and a per-device log file."""
    logger = logging.getLogger(f"bryck_setup.{device_ip}")
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (force UTF-8 to avoid Windows cp1252 encoding errors)
    console = logging.StreamHandler(
        stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    )
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # File handler (per device IP, UTF-8)
    log_file = LOG_DIR / f"setup_{device_ip.replace('.', '_')}.log"
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info(f"Log file: {log_file}")
    return logger


# ---------------------------------------------------------------------------
# SSH Connection
# ---------------------------------------------------------------------------

class SSHConnection:
    """Manages an SSH connection to a Bryck device."""

    def __init__(self, config: DeviceConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        """Establish SSH connection to the device."""
        self.logger.info(f"Connecting to {self.config.ip}:{self.config.ssh_port} as '{self.config.username}'...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            self.client.connect(
                hostname=self.config.ip,
                port=self.config.ssh_port,
                username=self.config.username,
                password=self.config.password,
                timeout=self.config.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            # Keep the transport alive during long, quiet installs so the TCP
            # connection is not dropped by an idle NAT/firewall and a dead peer
            # is detected promptly.
            transport = self.client.get_transport()
            if transport is not None:
                transport.set_keepalive(30)
            self.logger.info("SSH connection established successfully.")
        except paramiko.AuthenticationException:
            self.logger.error("Authentication failed. Check username/password.")
            raise
        except paramiko.SSHException as e:
            self.logger.error(f"SSH error: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            raise

    def is_active(self) -> bool:
        """Return True if the SSH transport is currently alive."""
        if not self.client:
            return False
        transport = self.client.get_transport()
        return transport is not None and transport.is_active()

    def reconnect(self) -> bool:
        """
        Re-establish the SSH connection after a dropped/dead transport.

        A single command that hangs long enough can tear down the paramiko
        transport; without this, every subsequent command fails with
        'SSH session not active'. Reconnecting lets the remaining tasks run.
        """
        self.logger.info("  Re-establishing SSH connection...")
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        try:
            self.connect()
            return True
        except Exception as e:
            self.logger.error(f"  Reconnect failed: {e}")
            return False

    def _default_timeout(self, command: str) -> int:
        """
        Pick a sensible timeout when the caller doesn't specify one.

        Slow operations (apt/dpkg/pip installs, downloads, archive extraction,
        update-grub, deploy scripts) get config.long_timeout; everything else
        gets the short config.timeout. This prevents kernel/package installs
        from being wrongly abandoned after 30s while still failing fast on
        genuinely hung quick commands.
        """
        lowered = command.lower()
        if any(marker in lowered for marker in LONG_RUNNING_MARKERS):
            return self.config.long_timeout
        return self.config.timeout

    def run_command(
        self, command: str, use_sudo: bool = False, timeout: int | None = None
    ) -> tuple[int, str, str]:
        """
        Execute a command on the remote device.

        Two waiting strategies are used:

        * Long-running commands (package installs, downloads, deploys — see
          LONG_RUNNING_MARKERS) are waited on by ACTIVITY, not a wall clock.
          As long as the command keeps producing output we keep waiting, so a
          kernel/apt install is never abandoned just because it is slow. We
          only give up if it goes completely silent for config.idle_timeout
          seconds (a genuine hang, e.g. an interactive prompt).

        * Short commands use a bounded wall-clock timeout so a hung quick
          command cannot block the pipeline forever.

        An explicit `timeout` argument always wins and is treated as a hard
        wall-clock cap (used by callers that intentionally bound a command).

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        explicit_timeout = timeout is not None
        long_running = any(m in command.lower() for m in LONG_RUNNING_MARKERS)
        if timeout is None:
            timeout = self._default_timeout(command)

        # Heal a dead transport (e.g. after a previous command hung) so this
        # command — and the rest of the pipeline — can still proceed.
        if not self.is_active():
            if not self.reconnect():
                return -1, "", "SSH session not active and reconnect failed"

        full_command = f"echo '{self.config.password}' | sudo -S {command}" if use_sudo else command
        self.logger.debug(f"Executing: {command}")

        # Activity-based waiting when the command is long-running and the caller
        # did not force a hard cap: wait as long as output keeps flowing.
        use_idle_wait = long_running and not explicit_timeout

        try:
            stdin, stdout, stderr = self.client.exec_command(full_command, timeout=None)
            channel = stdout.channel
            channel.setblocking(0)

            out_chunks: list[bytes] = []
            err_chunks: list[bytes] = []
            start = time.monotonic()
            last_activity = start
            abandoned = False

            while True:
                got_data = False
                while channel.recv_ready():
                    data = channel.recv(65536)
                    if data:
                        out_chunks.append(data)
                        got_data = True
                while channel.recv_stderr_ready():
                    data = channel.recv_stderr(65536)
                    if data:
                        err_chunks.append(data)
                        got_data = True

                now = time.monotonic()
                if got_data:
                    last_activity = now

                # Finished: exit status posted and no buffered output remains.
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break

                if use_idle_wait:
                    # Only abandon after a long stretch of TOTAL silence.
                    if now - last_activity > self.config.idle_timeout:
                        self.logger.warning(
                            f"Command produced no output for {self.config.idle_timeout}s "
                            f"and was abandoned (likely stuck): {command}"
                        )
                        abandoned = True
                        break
                else:
                    if now - start > timeout:
                        self.logger.warning(
                            f"Command exceeded {timeout}s and was abandoned: {command}"
                        )
                        abandoned = True
                        break

                if not got_data:
                    time.sleep(0.2)

            if abandoned:
                try:
                    channel.close()
                except Exception:
                    pass
                return 124, "", f"abandoned after no progress"

            exit_code = channel.recv_exit_status()
            out = b"".join(out_chunks).decode(errors="replace").strip()
            err = b"".join(err_chunks).decode(errors="replace").strip()
        except (socket.timeout, paramiko.SSHException, OSError) as e:
            self.logger.warning(f"Command failed on transport ({command}): {e}")
            # Transport is likely dead; drop the client so the next call reconnects.
            try:
                if self.client:
                    self.client.close()
            except Exception:
                pass
            self.client = None
            return -1, "", f"transport error: {e}"

        if exit_code == 0:
            self.logger.debug(f"Command succeeded (exit 0)")
        else:
            self.logger.warning(f"Command exited with code {exit_code}: {err}")

        if out:
            self.logger.debug(f"STDOUT: {out}")
        if err and exit_code != 0:
            self.logger.debug(f"STDERR: {err}")

        return exit_code, out, err

    def disconnect(self) -> None:
        """Close the SSH connection."""
        if self.client:
            self.client.close()
            self.client = None
            self.logger.info("SSH connection closed.")


# ---------------------------------------------------------------------------
# Setup Tasks
# ---------------------------------------------------------------------------

class SetupTask:
    """Base class for setup tasks."""

    name: str = "Unnamed Task"

    # Foundational tasks whose failure makes every later task pointless
    # (e.g. creating the bryck user, granting sudo, reconnecting as bryck).
    # When a critical task fails the runner aborts instead of cascading
    # dozens of doomed sudo commands.
    critical: bool = False

    def __init__(self, ssh: SSHConnection, logger: logging.Logger):
        self.ssh = ssh
        self.logger = logger

    def run(self) -> bool:
        """Execute the task. Returns True on success, False on failure."""
        raise NotImplementedError


class ConfigureDNS(SetupTask):
    """
    Task: Configure DNS resolver.
    
    Writes 'nameserver 8.8.8.8' to /etc/resolv.conf to ensure
    the device can resolve external hostnames during setup.
    """

    name = "Configure DNS (/etc/resolv.conf)"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Check current resolv.conf
        self.logger.info("  Checking current /etc/resolv.conf...")
        exit_code, current_content, _ = self.ssh.run_command("cat /etc/resolv.conf")

        if "nameserver 8.8.8.8" in current_content:
            self.logger.info("  DNS already configured (nameserver 8.8.8.8 present). Skipping.")
            return True

        self.logger.info(f"  Current content:\n{current_content}")

        # Step 2: Unlink existing resolv.conf (may be a symlink managed by systemd-resolved)
        self.logger.info("  Unlinking /etc/resolv.conf...")
        exit_code, _, err = self.ssh.run_command("unlink /etc/resolv.conf", use_sudo=True)
        if exit_code != 0:
            self.logger.warning(f"  Unlink returned non-zero (may not exist): {err}")

        # Step 3: Write the new resolv.conf
        self.logger.info("  Writing nameserver 8.8.8.8 to /etc/resolv.conf...")
        write_cmd = "bash -c 'printf \"nameserver 8.8.8.8\\n\" > /etc/resolv.conf'"
        exit_code, out, err = self.ssh.run_command(write_cmd, use_sudo=True)

        if exit_code != 0:
            self.logger.error(f"  Failed to write /etc/resolv.conf: {err}")
            return False

        # Step 3: Verify
        self.logger.info("  Verifying configuration...")
        exit_code, content, _ = self.ssh.run_command("cat /etc/resolv.conf")

        if "nameserver 8.8.8.8" in content:
            self.logger.info("  DNS configured successfully.")
            return True
        else:
            self.logger.error(f"  Verification failed. Content: {content}")
            return False


class CreateUsers(SetupTask):
    """
    Task: Create bryck and admin users.

    - Creates 'bryck' user with password 'while(1);'
    - Deletes 'admin' group if it exists
    - Creates 'admin' user with password 'BryckAdm1n'
    """

    name = "Create Users (bryck & admin)"
    critical = True  # everything downstream runs as bryck and needs sudo

    USERS = [
        {"username": "bryck", "password": "while(1);"},
        {"username": "admin", "password": "BryckAdm1n"},
    ]

    def _user_exists(self, username: str) -> bool:
        """Check if a user already exists on the system."""
        exit_code, _, _ = self.ssh.run_command(f"id {username}")
        return exit_code == 0

    def _create_user(self, username: str, password: str) -> bool:
        """Create a user with the given password."""
        if self._user_exists(username):
            self.logger.info(f"  User '{username}' already exists. Skipping creation.")
            return True

        self.logger.info(f"  Creating user '{username}'...")
        # Use useradd + chpasswd to avoid interactive adduser prompts
        exit_code, _, err = self.ssh.run_command(
            f"useradd -m -s /bin/bash {username}", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to create user '{username}': {err}")
            return False

        # Set password
        self.logger.info(f"  Setting password for '{username}'...")
        exit_code, _, err = self.ssh.run_command(
            f"bash -c 'echo \"{username}:{password}\" | chpasswd'", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to set password for '{username}': {err}")
            return False

        self.logger.info(f"  User '{username}' created successfully.")
        return True

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")
        success = True

        # Step 1: Create bryck user
        if not self._create_user("bryck", "while(1);"):
            success = False

        # Step 1b: Grant 'bryck' sudo access via group membership BEFORE any
        # later task reconnects as bryck. This is the fix for the classic
        # "bryck is not in the sudoers file" cascade: useradd alone leaves the
        # user unprivileged, so the very next sudo command (run as bryck) would
        # fail. Group membership means 'echo <bryck password> | sudo -S ...'
        # works immediately, even before /etc/sudoers is customised.
        self.logger.info("  Adding 'bryck' to the 'sudo' group...")
        exit_code, _, err = self.ssh.run_command("usermod -aG sudo bryck", use_sudo=True)
        if exit_code != 0:
            self.logger.error(f"  Failed to add 'bryck' to sudo group: {err}")
            success = False
        else:
            self.logger.info("  'bryck' added to sudo group.")

        # Step 2: Delete admin group (if exists) to avoid conflict with admin user
        self.logger.info("  Removing 'admin' group if it exists...")
        exit_code, _, err = self.ssh.run_command("groupdel admin", use_sudo=True)
        if exit_code == 0:
            self.logger.info("  Group 'admin' deleted.")
        else:
            self.logger.info(f"  Group 'admin' not found or already removed: {err}")

        # Step 3: Create admin user
        if not self._create_user("admin", "BryckAdm1n"):
            success = False

        # Verify
        for user_info in self.USERS:
            username = user_info["username"]
            if self._user_exists(username):
                self.logger.info(f"  [OK] User '{username}' verified.")
            else:
                self.logger.error(f"  [FAIL] User '{username}' does not exist.")
                success = False

        return success


class ReconnectAsBryckUser(SetupTask):
    """
    Task: Drop the current SSH session and reconnect as the 'bryck' user.

    Must run immediately after CreateUsers so that all subsequent tasks
    execute natively as 'bryck' rather than as whatever user initially
    connected (e.g. ubuntu, root, admin).

    Idempotent: if already connected as 'bryck', logs and skips.
    """

    name = "Reconnect as bryck User"

    BRYCK_USER = "bryck"
    BRYCK_PASSWORD = "while(1);"
    critical = True  # all later tasks must run natively as bryck

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Check who we are right now
        exit_code, current_user, err = self.ssh.run_command("whoami")
        current_user = current_user.strip()

        if current_user == self.BRYCK_USER:
            self.logger.info(f"  Already connected as '{self.BRYCK_USER}'. Skipping reconnect.")
            return True

        self.logger.info(f"  Currently connected as '{current_user}'. Switching to '{self.BRYCK_USER}'...")

        # Step 2: Verify bryck user actually exists before trying to reconnect
        exit_code, _, _ = self.ssh.run_command(f"id {self.BRYCK_USER}")
        if exit_code != 0:
            self.logger.error(f"  User '{self.BRYCK_USER}' does not exist. Cannot reconnect.")
            return False

        # Step 3: Update credentials on the shared config so all future
        #         reconnects (including post-reboot) also use bryck credentials
        self.ssh.config.username = self.BRYCK_USER
        self.ssh.config.password = self.BRYCK_PASSWORD

        # Step 4: Close current session and reconnect as bryck
        self.logger.info(f"  Closing current session and reconnecting as '{self.BRYCK_USER}'...")
        if self.ssh.client:
            try:
                self.ssh.client.close()
            except Exception:
                pass
            self.ssh.client = None

        if not self.ssh.reconnect():
            self.logger.error(f"  Failed to reconnect as '{self.BRYCK_USER}'.")
            return False

        # Step 5: Verify the new session is bryck
        exit_code, verified_user, err = self.ssh.run_command("whoami")
        verified_user = verified_user.strip()

        if verified_user == self.BRYCK_USER:
            self.logger.info(f"  Reconnected successfully as '{self.BRYCK_USER}'.")
            return True
        else:
            self.logger.error(
                f"  Reconnect succeeded but 'whoami' returned '{verified_user}' "
                f"(expected '{self.BRYCK_USER}')."
            )
            return False


class ConfigureSudoers(SetupTask):
    """
    Task: Update /etc/sudoers directly.

    Appends to /etc/sudoers:
    - Cmnd_Alias MORE for specific binaries
    - Defaults for MORE (no syslog, no pam_session)
    - bryck: NOPASSWD ALL
    - wsgi: NOPASSWD ALL
    - admin: ALL (password required)
    """

    name = "Configure Sudoers (/etc/sudoers)"
    critical = True  # bryck needs passwordless sudo for the rest of the run

    SUDOERS_LINES = [
        "Cmnd_Alias MORE = /usr/sbin/nvme, /usr/bin/lsblk, /usr/bin/journalctl, /usr/sbin/parted, /usr/sbin/partprobe, /usr/bin/xxd, /usr/bin/dd, /opt/ansible/drivers/lsblk, /usr/sbin/sgdisk, /opt/ansible/drivers/cryptsetup, /usr/sbin/zpool, /usr/sbin/zfs, /usr/sbin/sysctl, /usr/bin/systemctl, /usr/bin/chmod, /usr/bin/umount, /usr/bin/mount, /usr/sbin/dmsetup, /usr/sbin/mdadm, /usr/sbin/lsof, /usr/bin/df, /sbin/ethtool, /sbin/blockdev",
        "Defaults!MORE !syslog, !pam_session",
        "bryck  ALL=(ALL)       NOPASSWD: ALL",
        "wsgi  ALL=(ALL)       NOPASSWD: ALL",
        "admin  ALL=(ALL)      ALL",
    ]

    SUDOERS_FILE = "/etc/sudoers"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Backup current sudoers
        self.logger.info("  Backing up /etc/sudoers to /etc/sudoers.bak...")
        exit_code, _, err = self.ssh.run_command(
            f"cp {self.SUDOERS_FILE} {self.SUDOERS_FILE}.bak", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to backup sudoers: {err}")
            return False

        # Step 2: Check if already configured
        self.logger.info("  Checking if entries already exist...")
        exit_code, current_content, _ = self.ssh.run_command(
            f"cat {self.SUDOERS_FILE}", use_sudo=True
        )
        if "Cmnd_Alias MORE" in current_content and "bryck  ALL=(ALL)" in current_content:
            self.logger.info("  Sudoers already configured. Skipping.")
            return True

        # Step 3: Append entries to /etc/sudoers
        self.logger.info("  Appending entries to /etc/sudoers...")
        content_to_append = "\\n# Bryck SDK sudoers configuration\\n" + "\\n".join(self.SUDOERS_LINES) + "\\n"
        write_cmd = f"bash -c 'echo -e \"{content_to_append}\" >> {self.SUDOERS_FILE}'"
        exit_code, _, err = self.ssh.run_command(write_cmd, use_sudo=True)

        if exit_code != 0:
            self.logger.error(f"  Failed to append to sudoers: {err}")
            return False

        # Step 4: Validate with visudo -c
        self.logger.info("  Validating sudoers syntax with visudo -c...")
        exit_code, out, err = self.ssh.run_command("visudo -c", use_sudo=True)

        if exit_code != 0:
            self.logger.error(f"  Sudoers validation FAILED: {err}")
            self.logger.error("  Restoring backup...")
            self.ssh.run_command(
                f"cp {self.SUDOERS_FILE}.bak {self.SUDOERS_FILE}", use_sudo=True
            )
            return False

        self.logger.info(f"  visudo -c: {out}")

        # Step 5: Verify content
        self.logger.info("  Verifying configuration...")
        exit_code, content, _ = self.ssh.run_command(
            f"cat {self.SUDOERS_FILE}", use_sudo=True
        )
        if "bryck  ALL=(ALL)" in content and "Cmnd_Alias MORE" in content:
            self.logger.info("  Sudoers configured successfully.")
            return True
        else:
            self.logger.error("  Verification failed.")
            return False


class ConfigureAPTSources(SetupTask):
    """
    Task: Configure APT sources list.

    - Backs up existing /etc/apt/sources.list to /home/bryck/bkp_sources.list
    - Writes the TSecond mirror sources for Ubuntu Jammy (arm64)
    - Runs apt update
    """

    name = "Configure APT Sources (/etc/apt/sources.list)"

    SOURCES_LIST = """\
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
"""

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Check if already configured
        self.logger.info("  Checking current /etc/apt/sources.list...")
        exit_code, current_content, _ = self.ssh.run_command("cat /etc/apt/sources.list")
        if "repos.tsecond.ai" in current_content:
            self.logger.info("  APT sources already configured (tsecond mirror present). Skipping.")
            return True

        # Step 2: Backup existing sources.list
        self.logger.info("  Backing up /etc/apt/sources.list to /home/bryck/bkp_sources.list...")
        exit_code, _, err = self.ssh.run_command(
            "mv /etc/apt/sources.list /home/bryck/bkp_sources.list", use_sudo=True
        )
        if exit_code != 0:
            self.logger.warning(f"  Backup move failed (may not exist): {err}")

        # Step 3: Write new sources.list using tee (avoids single-quote issues with bash -c)
        self.logger.info("  Writing new /etc/apt/sources.list...")
        # Write via SFTP to avoid shell quoting issues with heredoc
        try:
            sftp = self.ssh.client.open_sftp()
            with sftp.file("/tmp/sources.list.new", "w") as f:
                f.write(self.SOURCES_LIST)
            sftp.close()
        except Exception as e:
            self.logger.error(f"  Failed to write sources.list via SFTP: {e}")
            return False

        # Move from temp location to final path
        exit_code, _, err = self.ssh.run_command(
            "mv /tmp/sources.list.new /etc/apt/sources.list", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to move sources.list: {err}")
            return False

        # Step 4: Verify the file was written
        self.logger.info("  Verifying sources.list...")
        exit_code, content, _ = self.ssh.run_command("grep -c 'repos.tsecond.ai' /etc/apt/sources.list")
        if exit_code != 0 or content.strip() == "0":
            self.logger.error("  Verification failed - tsecond mirror not found in sources.list")
            return False
        self.logger.info(f"  Found {content.strip()} tsecond mirror entries in sources.list.")

        # Step 5: Run apt update
        self.logger.info("  Running apt update...")
        exit_code, out, err = self.ssh.run_command("apt update", use_sudo=True)

        if exit_code != 0:
            self.logger.warning(f"  apt update returned non-zero (may have warnings): {err}")
            # apt update can return non-zero for non-fatal warnings, so just log it
        else:
            self.logger.info("  apt update completed successfully.")

        self.logger.info("  APT sources configured successfully.")
        return True


class InstallKernel(SetupTask):
    """
    Task: Install Linux kernel 6.5.0-45 and configure GRUB to boot it.

    Runs on both bryckserver (x86-64) and bryckmini (arm64).

    - Installs linux-image, headers, modules, modules-extra for 6.5.0-45-generic
    - Installs sshpass and vim
    - Parses GRUB menu entries to find the correct entry for 6.5.0-45
    - Sets GRUB_DEFAULT to boot the new kernel
    - Runs update-grub
    """

    name = "Install Kernel 6.5.0-45 & Configure GRUB"

    KERNEL_VERSION = "6.5.0-45-generic"
    KERNEL_PACKAGES = [
        "linux-image-6.5.0-45-generic",
        "linux-headers-6.5.0-45-generic",
        "linux-modules-6.5.0-45-generic",
        "linux-modules-extra-6.5.0-45-generic",
        "sshpass",
        "vim",
    ]

    # Retry the install a few times: package mirrors and dpkg locks can cause
    # transient failures that succeed on a second attempt.
    INSTALL_ATTEMPTS = 3

    def _wait_for_apt_lock(self, max_wait: int = 300) -> None:
        """Block until no other process holds the dpkg/apt lock (bounded)."""
        self.logger.info("  Waiting for any running apt/dpkg to release the lock...")
        wait_cmd = (
            "for i in $(seq 1 %d); do "
            "  if fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock "
            "/var/lib/apt/lists/lock >/dev/null 2>&1; then sleep 2; else exit 0; fi; "
            "done; exit 0" % (max_wait // 2)
        )
        self.ssh.run_command(f"bash -c '{wait_cmd}'", use_sudo=True, timeout=max_wait + 30)

    def _install_kernel_packages(self) -> bool:
        """Install kernel packages with lock-wait, index refresh, and retries."""
        packages_str = " ".join(self.KERNEL_PACKAGES)

        for attempt in range(1, self.INSTALL_ATTEMPTS + 1):
            self.logger.info(
                f"  Installing kernel packages (attempt {attempt}/{self.INSTALL_ATTEMPTS}): {packages_str}"
            )
            self.logger.info("  This may take several minutes...")

            # Make sure no other apt/dpkg run is holding the lock, and repair
            # any half-configured state left by a previous interrupted run.
            self._wait_for_apt_lock()
            self.ssh.run_command("apt-get update", use_sudo=True)
            self.ssh.run_command(
                "DEBIAN_FRONTEND=noninteractive dpkg --configure -a", use_sudo=True
            )
            self.ssh.run_command(
                "DEBIAN_FRONTEND=noninteractive apt-get --fix-broken install -y", use_sudo=True
            )

            exit_code, out, err = self.ssh.run_command(
                f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {packages_str}",
                use_sudo=True,
            )
            if exit_code == 0:
                self.logger.info("  Kernel packages installed successfully.")
                return True

            # A timeout (124) or transient mirror/lock error — retry.
            self.logger.warning(
                f"  Kernel install attempt {attempt} failed (exit {exit_code}): {err or out}"
            )

        self.logger.error(
            f"  Kernel installation failed after {self.INSTALL_ATTEMPTS} attempts."
        )
        return False

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Check if kernel is already installed
        self.logger.info(f"  Checking if kernel {self.KERNEL_VERSION} is already installed...")
        exit_code, out, _ = self.ssh.run_command(
            f"dpkg -l linux-image-{self.KERNEL_VERSION} 2>/dev/null | grep -q '^ii'"
        )
        if exit_code == 0:
            self.logger.info(f"  Kernel {self.KERNEL_VERSION} already installed.")
        else:
            # Step 2: Install kernel packages (with lock-wait + retries)
            if not self._install_kernel_packages():
                return False

        # Step 3: Check if GRUB is already configured for this kernel
        self.logger.info("  Checking current GRUB_DEFAULT...")
        exit_code, grub_current, _ = self.ssh.run_command("grep GRUB_DEFAULT /etc/default/grub")
        if self.KERNEL_VERSION in grub_current:
            self.logger.info(f"  GRUB already configured for {self.KERNEL_VERSION}. Skipping.")
            return True

        # Step 4: Parse GRUB menu entries to find the correct entry
        self.logger.info("  Parsing GRUB menu entries...")
        exit_code, grub_entries, err = self.ssh.run_command(
            "awk -F\\' '/menuentry / {print $2}' /boot/grub/grub.cfg"
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to parse GRUB config: {err}")
            return False

        self.logger.info(f"  GRUB entries found:\n{grub_entries}")

        # Find the entry containing our kernel version
        target_entry = None
        for entry in grub_entries.splitlines():
            if self.KERNEL_VERSION in entry and "recovery" not in entry.lower():
                target_entry = entry
                break

        if not target_entry:
            self.logger.error(f"  Could not find GRUB entry for {self.KERNEL_VERSION}")
            return False

        self.logger.info(f"  Target GRUB entry: {target_entry}")

        # Step 5: Set GRUB_DEFAULT
        grub_default = f"1>{target_entry}"
        self.logger.info(f"  Setting GRUB_DEFAULT=\"{grub_default}\"...")

        # Use sed to replace GRUB_DEFAULT line in /etc/default/grub
        sed_cmd = f"sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=\"{grub_default}\"/' /etc/default/grub"
        exit_code, _, err = self.ssh.run_command(sed_cmd, use_sudo=True)

        if exit_code != 0:
            self.logger.error(f"  Failed to update /etc/default/grub: {err}")
            return False

        # Step 6: Verify GRUB_DEFAULT was set
        exit_code, grub_content, _ = self.ssh.run_command("grep GRUB_DEFAULT /etc/default/grub")
        self.logger.info(f"  Current GRUB_DEFAULT: {grub_content}")

        # Step 7: Run update-grub
        self.logger.info("  Running update-grub...")
        exit_code, out, err = self.ssh.run_command("update-grub", use_sudo=True)

        if exit_code != 0:
            self.logger.error(f"  update-grub failed: {err}")
            return False

        self.logger.info("  update-grub completed successfully.")
        self.logger.info(f"  Kernel {self.KERNEL_VERSION} will be used on next reboot.")
        return True


class SetHostname(SetupTask):
    """
    Task: Set the system hostname based on detected device type.

    - bryckmini  -> hostname 'bryckmini'
    - bryckserver -> hostname 'bryckserver'
    - Also updates /etc/hosts to include the new hostname (avoids sudo resolution errors)
    """

    name = "Set Hostname"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        target_hostname = self.ssh.config.bryck_type.value
        self.logger.info(f"  Setting hostname to '{target_hostname}'...")

        exit_code, _, err = self.ssh.run_command(
            f"hostnamectl set-hostname {target_hostname}", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to set hostname: {err}")
            return False

        # Update /etc/hosts to include the new hostname (prevents sudo resolution errors)
        self.logger.info(f"  Ensuring '{target_hostname}' is in /etc/hosts...")
        exit_code, hosts_content, _ = self.ssh.run_command("cat /etc/hosts")
        if target_hostname not in hosts_content:
            self.ssh.run_command(
                f"bash -c 'echo \"127.0.1.1 {target_hostname}\" >> /etc/hosts'",
                use_sudo=True,
            )
            self.logger.info(f"  Added '127.0.1.1 {target_hostname}' to /etc/hosts.")
        else:
            self.logger.info(f"  '{target_hostname}' already in /etc/hosts.")

        # Verify
        exit_code, current, _ = self.ssh.run_command("hostname")
        self.logger.info(f"  Hostname is now: {current}")

        if current.strip() == target_hostname:
            self.logger.info("  Hostname set successfully.")
            return True
        else:
            self.logger.error(f"  Expected '{target_hostname}', got '{current.strip()}'")
            return False


class DisableGRUBPassword(SetupTask):
    """
    Task: Disable GRUB password protection.

    Comments out 'set superusers' and 'password_pbkdf2' lines
    in /etc/grub.d/40_custom, then runs update-grub.
    """

    name = "Disable GRUB Password (/etc/grub.d/40_custom)"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Check if the file exists and has the lines
        self.logger.info("  Reading /etc/grub.d/40_custom...")
        exit_code, content, err = self.ssh.run_command("cat /etc/grub.d/40_custom", use_sudo=True)

        if exit_code != 0:
            self.logger.warning(f"  /etc/grub.d/40_custom not found or unreadable: {err}")
            self.logger.info("  Nothing to do.")
            return True

        self.logger.debug(f"  Current content:\n{content}")

        # Check if already commented out
        if "set superusers" not in content or content.count("#set superusers") > 0:
            # Check if there's an uncommented line
            has_uncommented = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("set superusers") or stripped.startswith("password_pbkdf2"):
                    has_uncommented = True
                    break
            if not has_uncommented:
                self.logger.info("  GRUB password already disabled (lines already commented). Skipping.")
                return True

        # Step 2: Comment out the superusers and password lines using sed
        self.logger.info("  Commenting out 'set superusers' line...")
        exit_code, _, err = self.ssh.run_command(
            "sed -i 's/^set superusers/#set superusers/' /etc/grub.d/40_custom", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to comment out superusers: {err}")
            return False

        self.logger.info("  Commenting out 'password_pbkdf2' line...")
        exit_code, _, err = self.ssh.run_command(
            "sed -i 's/^password_pbkdf2/#password_pbkdf2/' /etc/grub.d/40_custom", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to comment out password_pbkdf2: {err}")
            return False

        # Step 3: Verify
        self.logger.info("  Verifying changes...")
        exit_code, new_content, _ = self.ssh.run_command("cat /etc/grub.d/40_custom", use_sudo=True)
        self.logger.debug(f"  Updated content:\n{new_content}")

        # Step 4: Run update-grub
        self.logger.info("  Running update-grub...")
        exit_code, out, err = self.ssh.run_command("update-grub", use_sudo=True)

        if exit_code != 0:
            self.logger.error(f"  update-grub failed: {err}")
            return False

        self.logger.info("  GRUB password protection disabled successfully.")
        return True


class RebootAndInstallPostKernel(SetupTask):
    """
    Task: Reboot the machine and install post-reboot kernel packages.

    - Reboots the device
    - Waits for SSH to become available again (with retry and backoff)
    - Verifies kernel 6.5.0-45-generic is running (aborts if not)
    - Installs linux-modules-extra and linux-headers for the running kernel
    - Installs network-manager
    """

    name = "Reboot & Install Post-Kernel Packages"

    # How long to wait for the machine to come back up
    REBOOT_WAIT_INITIAL = 30       # seconds to wait before first retry
    REBOOT_RETRY_INTERVAL = 10    # seconds between retries
    REBOOT_MAX_WAIT = 300         # max total seconds to wait (5 minutes)

    def _wait_for_ssh(self) -> bool:
        """Wait for SSH to become available after reboot."""
        self.logger.info(f"  Waiting {self.REBOOT_WAIT_INITIAL}s for device to shut down...")
        time.sleep(self.REBOOT_WAIT_INITIAL)

        elapsed = self.REBOOT_WAIT_INITIAL
        attempt = 0

        while elapsed < self.REBOOT_MAX_WAIT:
            attempt += 1
            self.logger.info(f"  Connection attempt {attempt} (elapsed: {elapsed}s)...")

            try:
                self.ssh.client = paramiko.SSHClient()
                self.ssh.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.ssh.client.connect(
                    hostname=self.ssh.config.ip,
                    port=self.ssh.config.ssh_port,
                    username=self.ssh.config.username,
                    password=self.ssh.config.password,
                    timeout=10,
                    look_for_keys=False,
                    allow_agent=False,
                )
                self.logger.info(f"  SSH connection re-established after {elapsed}s.")
                return True
            except Exception as e:
                self.logger.debug(f"  Attempt {attempt} failed: {e}")
                if self.ssh.client:
                    try:
                        self.ssh.client.close()
                    except Exception:
                        pass
                    self.ssh.client = None
                time.sleep(self.REBOOT_RETRY_INTERVAL)
                elapsed += self.REBOOT_RETRY_INTERVAL

        self.logger.error(f"  Device did not come back within {self.REBOOT_MAX_WAIT}s.")
        return False

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        if self.ssh.config.skip_reboot:
            self.logger.info("  --skip-reboot set: skipping reboot and kernel verification.")
            self.logger.info("  Installing post-kernel packages against the currently running kernel.")
        else:
            # Step 1: Initiate reboot
            self.logger.info("  Initiating reboot (kernel was changed)...")
            # Use nohup and background to avoid the SSH channel closing mid-command
            self.ssh.run_command("nohup bash -c 'sleep 2 && reboot' &", use_sudo=True)
            self.logger.info("  Reboot command sent.")

            # Close current connection (it will be dropped anyway)
            try:
                self.ssh.client.close()
            except Exception:
                pass
            self.ssh.client = None

            # Step 2: Wait for SSH to come back
            self.logger.info("  Waiting for device to reboot and SSH to become available...")
            if not self._wait_for_ssh():
                return False

            # Step 3: Verify the machine is up
            exit_code, uptime_out, _ = self.ssh.run_command("uptime")
            self.logger.info(f"  Device is up: {uptime_out}")

            # Step 4: Verify the correct kernel is running
            exit_code, kernel_out, _ = self.ssh.run_command("uname -a")
            self.logger.info(f"  Kernel check (uname -a): {kernel_out}")

            if "6.5.0-45-generic" not in kernel_out:
                self.logger.error(
                    f"  KERNEL VERIFICATION FAILED: Expected '6.5.0-45-generic' in uname output, "
                    f"got: {kernel_out}"
                )
                return False
            self.logger.info("  Kernel 6.5.0-45-generic verified successfully.")

        exit_code, kernel_out, _ = self.ssh.run_command("uname -r")
        self.logger.info(f"  Running kernel: {kernel_out}")

        # Step 4: Install linux-modules-extra for running kernel
        kernel_ver = kernel_out.strip()
        exit_code, _, _ = self.ssh.run_command(
            f"dpkg -l linux-modules-extra-{kernel_ver} 2>/dev/null | grep -q '^ii'"
        )
        if exit_code == 0:
            self.logger.info(f"  linux-modules-extra-{kernel_ver} already installed. Skipping.")
        else:
            self.logger.info(f"  Installing linux-modules-extra-{kernel_ver}...")
            exit_code, out, err = self.ssh.run_command(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y linux-modules-extra-$(uname -r)",
                use_sudo=True,
            )
            if exit_code != 0:
                self.logger.error(f"  linux-modules-extra installation failed: {err}")
                return False
            else:
                self.logger.info("  linux-modules-extra installed.")

        # Step 5: Install linux-headers for running kernel
        exit_code, _, _ = self.ssh.run_command(
            f"dpkg -l linux-headers-{kernel_ver} 2>/dev/null | grep -q '^ii'"
        )
        if exit_code == 0:
            self.logger.info(f"  linux-headers-{kernel_ver} already installed. Skipping.")
        else:
            self.logger.info(f"  Installing linux-headers-{kernel_ver}...")
            exit_code, out, err = self.ssh.run_command(
                "DEBIAN_FRONTEND=noninteractive apt install -y linux-headers-$(uname -r)",
                use_sudo=True,
            )
            if exit_code != 0:
                self.logger.error(f"  linux-headers installation failed: {err}")
                return False
            else:
                self.logger.info("  linux-headers installed.")

        # Step 6: Install network-manager
        exit_code, _, _ = self.ssh.run_command(
            "dpkg -l network-manager 2>/dev/null | grep -q '^ii'"
        )
        if exit_code == 0:
            self.logger.info("  network-manager already installed. Skipping.")
        else:
            self.logger.info("  Installing network-manager...")
            exit_code, out, err = self.ssh.run_command(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y network-manager",
                use_sudo=True,
            )
            if exit_code != 0:
                self.logger.error(f"  Failed to install network-manager: {err}")
                return False
            self.logger.info("  network-manager installed.")

        self.logger.info("  Reboot & post-kernel install completed successfully.")
        return True


class ConfigureAPTSandbox(SetupTask):
    """
    Task: Create APT sandbox configuration.

    Creates /etc/apt/apt.conf.d/10sandbox with APT::Sandbox::User "root"
    to allow apt to run downloads as root (required for certain environments).
    """

    name = "Configure APT Sandbox"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        conf_file = "/etc/apt/apt.conf.d/10sandbox"
        conf_content = 'APT::Sandbox::User "root";'

        # Check if already configured
        self.logger.info(f"  Checking {conf_file}...")
        exit_code, current, _ = self.ssh.run_command(f"cat {conf_file}", use_sudo=True)
        if exit_code == 0 and "Sandbox::User" in current:
            self.logger.info("  APT sandbox already configured. Skipping.")
            return True

        # Write the config file using tee (avoids shell quoting issues)
        self.logger.info(f"  Writing {conf_file}...")
        exit_code, _, err = self.ssh.run_command(
            f'bash -c "echo \'APT::Sandbox::User \\\"root\\\";\' > {conf_file}"', use_sudo=True
        )
        if exit_code != 0:
            # Fallback: use SFTP
            self.logger.info("  Shell write failed, using SFTP fallback...")
            try:
                sftp = self.ssh.client.open_sftp()
                with sftp.file("/tmp/10sandbox", "w") as f:
                    f.write('APT::Sandbox::User "root";\n')
                sftp.close()
                exit_code, _, err = self.ssh.run_command(
                    f"mv /tmp/10sandbox {conf_file}", use_sudo=True
                )
                if exit_code != 0:
                    self.logger.error(f"  Failed to write {conf_file}: {err}")
                    return False
            except Exception as e:
                self.logger.error(f"  SFTP fallback failed: {e}")
                return False

        # Verify
        exit_code, content, _ = self.ssh.run_command(f"cat {conf_file}", use_sudo=True)
        if "Sandbox::User" in content:
            self.logger.info(f"  APT sandbox configured successfully.")
            return True
        else:
            self.logger.error("  Verification failed.")
            return False


class InstallPackagesAndSDKs(SetupTask):
    """
    Task: Install required system packages, Python packages, and cloud SDK repos.

    - Installs system packages (python3, pip, vsftpd, sysbench, etc.)
    - Installs Python packages (ansible, pyroute2)
    - Configures /etc/rc.local
    """

    name = "Install Packages & Cloud SDKs"

    APT_PACKAGES_BATCH1 = [
        "python3", "python3-pip", "vsftpd", "sysbench", "net-tools",
        "ethtool", "cryptsetup", "fio", "unzip", "pkg-config", "libsystemd-dev",
        "krb5-user",  # Kerberos auth support; DEBIAN_FRONTEND=noninteractive suppresses realm prompts
    ]

    APT_PACKAGES_BATCH2 = [
        "rclone", "curl", "jq", "sysbench",
    ]

    PIP_PACKAGES = ["ansible", "pyroute2" ]

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 0: Ensure package index is fresh and fix any broken state
        self.logger.info("  Running apt update...")
        self.ssh.run_command("apt-get update", use_sudo=True)
        self.logger.info("  Fixing any broken packages...")
        self.ssh.run_command("DEBIAN_FRONTEND=noninteractive apt --fix-broken install -y", use_sudo=True)

        # Step 1: Install apt packages (batch 1)
        pkgs = " ".join(self.APT_PACKAGES_BATCH1)
        self.logger.info(f"  Installing packages (batch 1): {pkgs}")
        exit_code, out, err = self.ssh.run_command(
            f"DEBIAN_FRONTEND=noninteractive apt install -y {pkgs}", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Batch 1 install failed: {err}")
            return False
        self.logger.info("  Batch 1 installed.")

        # Step 2: Install apt packages (batch 2)
        pkgs = " ".join(self.APT_PACKAGES_BATCH2)
        self.logger.info(f"  Installing packages (batch 2): {pkgs}")
        exit_code, out, err = self.ssh.run_command(
            f"DEBIAN_FRONTEND=noninteractive apt install -y {pkgs}", use_sudo=True
        )
        if exit_code != 0:
            self.logger.warning(f"  Batch 2 install had issues (non-fatal): {err}")
        else:
            self.logger.info("  Batch 2 installed.")

        # Step 3: Install pip packages
        for pkg in self.PIP_PACKAGES:
            # Check if already installed
            pkg_name = pkg.split("==")[0]  # handle pinned versions like pyroute2==0.7.12
            exit_code, _, _ = self.ssh.run_command(f"pip3 show {pkg_name} >/dev/null 2>&1")
            if exit_code == 0:
                self.logger.info(f"  pip package '{pkg_name}' already installed. Skipping.")
                continue
            self.logger.info(f"  Installing pip package: {pkg}")
            exit_code, out, err = self.ssh.run_command(
                f"pip3 install {pkg}", use_sudo=True
            )
            if exit_code != 0:
                self.logger.warning(f"  pip3 install {pkg} failed: {err}")
            else:
                self.logger.info(f"  {pkg} installed.")

        # Step 4: Configure /etc/rc.local
        self.logger.info("  Configuring /etc/rc.local...")
        exit_code, rc_content, _ = self.ssh.run_command("cat /etc/rc.local 2>/dev/null")
        if "exit 0" not in rc_content:
            self.ssh.run_command(
                "bash -c 'echo \"exit 0\" >> /etc/rc.local'", use_sudo=True
            )
            self.ssh.run_command("chmod +x /etc/rc.local", use_sudo=True)
            self.logger.info("  /etc/rc.local configured.")
        else:
            self.logger.info("  /etc/rc.local already has 'exit 0'. Skipping.")

        self.logger.info("  Base packages configured successfully.")
        return True


class InstallCloudCLIs(SetupTask):
    """
    Task: Remove cloud provider CLIs (Google Cloud, Azure).

    Google Cloud CLI and Azure CLI are intentionally NOT installed. This task
    ensures they are absent: if a previous run of this script installed them
    (or added their apt repos / GPG keys), they are uninstalled and the repo
    definitions are cleaned up. Each sub-step is non-fatal.
    """

    name = "Remove Cloud CLIs (Google / Azure)"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Remove google-cloud-cli if present
        self.logger.info("  Removing google-cloud-cli (if present)...")
        exit_code, _, _ = self.ssh.run_command("which gcloud")
        if exit_code == 0:
            exit_code, _, err = self.ssh.run_command(
                "DEBIAN_FRONTEND=noninteractive apt-get purge -y google-cloud-cli",
                use_sudo=True,
            )
            if exit_code != 0:
                self.logger.warning(f"  google-cloud-cli removal failed: {err}")
            else:
                self.logger.info("  google-cloud-cli removed.")
        else:
            self.logger.info("  google-cloud-cli not installed. Skipping.")

        # Remove the Google Cloud SDK apt repo + key added by earlier runs
        self.ssh.run_command(
            "rm -f /etc/apt/sources.list.d/google-cloud-sdk.list "
            "/usr/share/keyrings/cloud.google.gpg",
            use_sudo=True,
        )

        # Step 2: Remove azure-cli if present
        self.logger.info("  Removing azure-cli (if present)...")
        exit_code, _, _ = self.ssh.run_command("which az")
        if exit_code == 0:
            exit_code, _, err = self.ssh.run_command(
                "DEBIAN_FRONTEND=noninteractive apt-get purge -y azure-cli",
                use_sudo=True,
            )
            if exit_code != 0:
                self.logger.warning(f"  azure-cli removal failed: {err}")
            else:
                self.logger.info("  azure-cli removed.")
        else:
            self.logger.info("  azure-cli not installed. Skipping.")

        # Remove the Azure CLI apt repo + key added by earlier runs
        self.ssh.run_command(
            "rm -f /etc/apt/sources.list.d/azure-cli.list "
            "/usr/share/keyrings/microsoft.gpg",
            use_sudo=True,
        )

        # Clean up orphaned dependencies left behind by the purges
        self.ssh.run_command(
            "DEBIAN_FRONTEND=noninteractive apt-get autoremove -y", use_sudo=True
        )

        self.logger.info("  Cloud CLIs removed successfully.")
        return True


class FlushIPTables(SetupTask):
    """
    Task: Reset iptables/ip6tables to allow-all and save clean rules.

    Runs all iptables/ip6tables reset commands in a single shell block (matching
    the reference setup doc).  An overall timeout of BLOCK_TIMEOUT seconds guards
    against the block hanging — if it does, the hang is reported clearly and the
    task continues (best-effort).
    """

    name = "Flush IPTables (Network Setup)"

    # Overall timeout (seconds) for the entire iptables reset block.
    # If the block does not finish within this window the device has likely
    # locked up on the xtables lock and the hang is reported.
    BLOCK_TIMEOUT = 60

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Remove existing iptables rule files
        self.logger.info("  Removing existing /etc/iptables/rules.*...")
        self.ssh.run_command("rm -f /etc/iptables/rules.*", use_sudo=True)

        # Step 2: Run the full iptables + ip6tables reset in one shell block
        self.logger.info("  Running iptables/ip6tables reset block...")
        flush_script = (
            "iptables -P INPUT ACCEPT && "
            "iptables -P FORWARD ACCEPT && "
            "iptables -P OUTPUT ACCEPT && "
            "iptables -t nat -F && "
            "iptables -t mangle -F && "
            "iptables -F && "
            "iptables -X && "
            "ip6tables -P INPUT ACCEPT && "
            "ip6tables -P FORWARD ACCEPT && "
            "ip6tables -P OUTPUT ACCEPT && "
            "ip6tables -t nat -F && "
            "ip6tables -t mangle -F && "
            "ip6tables -F && "
            "ip6tables -X"
        )
        exit_code, _, err = self.ssh.run_command(
            f"timeout {self.BLOCK_TIMEOUT} bash -c '{flush_script}'",
            use_sudo=True,
            timeout=self.BLOCK_TIMEOUT + 10,
        )
        if exit_code == 124:
            self.logger.error(
                f"  HUNG: iptables reset block did not complete within {self.BLOCK_TIMEOUT}s. "
                "The device appears to have locked on the xtables lock. "
                "Manual intervention may be required (reboot or 'modprobe -r iptable_filter')."
            )
            flush_ok = False
        elif exit_code != 0:
            self.logger.warning(f"  iptables reset block exited with code {exit_code}: {err}")
            flush_ok = False
        else:
            self.logger.info("  iptables/ip6tables reset block completed.")
            flush_ok = True

        # Step 3: Ensure /etc/iptables directory exists
        self.ssh.run_command("mkdir -p /etc/iptables", use_sudo=True)

        # Step 4: Save clean rules
        self.logger.info("  Saving clean rules to /etc/iptables/rules.v4...")
        exit_code, _, err = self.ssh.run_command(
            "bash -c 'iptables-save > /etc/iptables/rules.v4'",
            use_sudo=True,
        )
        save_ok = exit_code == 0
        if save_ok:
            exit_code, content, _ = self.ssh.run_command(
                "grep -c 'ACCEPT' /etc/iptables/rules.v4", use_sudo=True
            )
            self.logger.info(f"  rules.v4 contains {content.strip()} ACCEPT entries.")
        else:
            self.logger.warning(f"  Could not save iptables rules: {err}")

        if flush_ok and save_ok:
            self.logger.info("  IPTables flushed and saved successfully.")
        else:
            self.logger.warning(
                "  IPTables reset completed with warnings (best-effort on this device)."
            )
        # Firewall reset is best-effort; do not fail the whole run over it.
        return True


class ConfigureNetworkManagerAndSSL(SetupTask):
    """
    Task: Configure NetworkManager managed devices, generate SSL cert, and setup SSH keys.

    1. Updates NetworkManager config to manage ethernet devices
    2. Generates a self-signed SSL certificate for bryckweb
    3. Generates SSH key for bryck user and copies it to localhost
    """

    name = "Configure NetworkManager, SSL & SSH Keys"

    NM_CONF_FILE = "/usr/lib/NetworkManager/conf.d/10-globally-managed-devices.conf"
    NM_CONF_CONTENT = "[keyfile]\nunmanaged-devices=*,except:type:wifi,except:type:gsm,except:type:cdma,except:type:ethernet\n"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # --- Part 1: NetworkManager managed devices ---
        self.logger.info("  Configuring NetworkManager managed devices...")
        exit_code, current, _ = self.ssh.run_command(f"cat {self.NM_CONF_FILE}", use_sudo=True)

        if "[keyfile]" in current and "except:type:ethernet" in current:
            self.logger.info("  NetworkManager already configured correctly. Skipping.")
        else:
            self.logger.info("  Writing correct NM config (fixing potential corruption)...")
            # Write config via SFTP (avoids quoting issues)
            try:
                sftp = self.ssh.client.open_sftp()
                with sftp.file("/tmp/10-globally-managed-devices.conf", "w") as f:
                    f.write(self.NM_CONF_CONTENT)
                sftp.close()
            except Exception as e:
                self.logger.error(f"  SFTP write failed: {e}")
                return False

            exit_code, _, err = self.ssh.run_command(
                f"mv /tmp/10-globally-managed-devices.conf {self.NM_CONF_FILE}", use_sudo=True
            )
            if exit_code != 0:
                self.logger.error(f"  Failed to move NM config: {err}")
                return False
            self.logger.info("  NetworkManager config updated.")

        # --- Part 2: Generate self-signed SSL certificate ---
        self.logger.info("  Generating self-signed SSL certificate...")
        exit_code, _, _ = self.ssh.run_command(
            "test -f /etc/ssl/certs/bryckweb-selfsigned.crt"
        )
        if exit_code == 0:
            self.logger.info("  SSL certificate already exists. Skipping.")
        else:
            # Determine CN based on device type
            cn = self.ssh.config.bryck_type.value  # bryckmini or bryckserver

            openssl_cmd = (
                "openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes "
                "-days 1825 "
                "-keyout /etc/ssl/private/bryckweb-selfsigned.key "
                "-out /etc/ssl/certs/bryckweb-selfsigned.crt "
                f'-subj "/C=US/ST=California/L=San Jose/O=TSecond Inc./CN={cn}/emailAddress=support@tsecond.ai" '
                '-addext "keyUsage=digitalSignature,keyEncipherment"'
            )
            exit_code, _, err = self.ssh.run_command(openssl_cmd, use_sudo=True)
            if exit_code != 0:
                self.logger.error(f"  SSL cert generation failed: {err}")
                return False

            # Verify cert was created
            exit_code, _, _ = self.ssh.run_command(
                "test -f /etc/ssl/certs/bryckweb-selfsigned.crt"
            )
            if exit_code != 0:
                self.logger.error("  SSL cert file not found after generation.")
                return False
            self.logger.info("  SSL certificate generated successfully.")

        # --- Part 3: SSH key setup for bryck user ---
        self.logger.info("  Setting up SSH keys for bryck user...")
        # Must check with sudo since the file is owned by bryck with mode 600
        exit_code, _, _ = self.ssh.run_command(
            "test -f /home/bryck/.ssh/id_rsa", use_sudo=True
        )
        if exit_code == 0:
            self.logger.info("  SSH key already exists for bryck. Skipping keygen.")
        else:
            # Ensure .ssh directory exists
            self.ssh.run_command(
                "bash -c 'mkdir -p /home/bryck/.ssh && chown bryck:bryck /home/bryck/.ssh && chmod 700 /home/bryck/.ssh'",
                use_sudo=True,
            )

            # Generate the key directly as root (we already have sudo) and then
            # hand ownership to bryck. This avoids the fragile nested
            # 'su - bryck -c "..."' quoting that mangled the empty-passphrase
            # argument and made ssh-keygen fail with "Too many arguments".
            self.logger.info("  Generating RSA key for bryck...")
            self.ssh.run_command(
                "rm -f /home/bryck/.ssh/id_rsa /home/bryck/.ssh/id_rsa.pub",
                use_sudo=True,
            )
            exit_code, _, err = self.ssh.run_command(
                "ssh-keygen -t rsa -b 4096 -N '' -f /home/bryck/.ssh/id_rsa",
                use_sudo=True,
            )
            if exit_code != 0:
                self.logger.error(f"  SSH keygen failed: {err}")
                return False
            # root created the files; give them back to bryck.
            self.ssh.run_command(
                "chown bryck:bryck /home/bryck/.ssh/id_rsa /home/bryck/.ssh/id_rsa.pub",
                use_sudo=True,
            )
            self.logger.info("  SSH key generated.")

        # Copy SSH key to localhost
        self.logger.info("  Copying SSH key to bryck@localhost...")
        exit_code, _, err = self.ssh.run_command(
            "su - bryck -c \"sshpass -p 'while(1);' ssh-copy-id -o StrictHostKeyChecking=no bryck@localhost\"",
            use_sudo=True,
        )
        if exit_code != 0:
            self.logger.warning(f"  ssh-copy-id returned non-zero: {err}")
            # May fail if already copied or sshd not running locally, non-fatal
        else:
            self.logger.info("  SSH key copied to localhost.")

        # Verify SSH to localhost works
        self.logger.info("  Verifying SSH to localhost as bryck...")
        exit_code, out, _ = self.ssh.run_command(
            'su - bryck -c "ssh -o StrictHostKeyChecking=no -o BatchMode=yes bryck@localhost whoami"',
            use_sudo=True,
        )
        if exit_code == 0 and "bryck" in out:
            self.logger.info("  SSH to localhost verified successfully.")
        else:
            self.logger.warning(f"  SSH verification returned: {out} (may need sshd running)")

        self.logger.info("  NetworkManager, SSL & SSH keys configured.")
        return True


class DeployBryckBuild(SetupTask):
    """
    Task: Download, extract, and install the Bryck build.

    For bryckmini:
        - SCP the build tarball from the build server (192.168.6.193)
          using credentials bryck / while(1);
    For bryckserver:
        - Downloads build tarball from repos.tsecond.ai

    Then:
    - Downloads inventory file
    - Sets bryck_type in inventory (mini or bryck)
    - Runs bryckdeploy install
    - Updates /etc/bryck/bryckutil/config.json
    - Adds HAILO_MONITOR=1 to /etc/bash.bashrc
    """

    name = "Deploy Bryck Build"

    REPO_BASE_URL = "http://repos.tsecond.ai/ubuntu"

    # Build servers
    # bryckmini (BlueField DPU / arm64 builds)
    BUILD_SERVER_MINI_IP = "192.168.6.193"
    # bryckserver (amd64 builds)
    BUILD_SERVER_AMD64_IP = "192.168.6.28"
    BUILD_SERVER_USER = "bryck"
    BUILD_SERVER_PASSWORD = "while(1);"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        build_name = self.ssh.config.bryck_build
        if not build_name:
            self.logger.info("  No --bryck-build specified. Skipping deployment.")
            return True

        # Strip .tar.gz if user included it in the build name
        if build_name.endswith(".tar.gz"):
            build_name = build_name[: -len(".tar.gz")]
        # Strip architecture suffix if user included it
        if build_name.endswith("-arm64"):
            build_name = build_name[: -len("-arm64")]
        if build_name.endswith("-amd64"):
            build_name = build_name[: -len("-amd64")]

        # Determine bryck_type value for inventory
        # bryckmini -> "mini", bryckserver -> "bryck"
        if self.ssh.config.bryck_type == BryckType.BRYCKMINI:
            inventory_type = "mini"
        else:
            inventory_type = "bryck"

        # Candidate tarball names. The build server may host the archive either
        # WITH an architecture suffix (e.g. ...-arm64.tar.gz) or WITHOUT one
        # (e.g. ....tar.gz). We check both — locally and on the build server —
        # and use whichever actually exists.
        arch_suffix = "arm64" if self.ssh.config.bryck_type == BryckType.BRYCKMINI else "amd64"
        tarball_candidates = [
            f"{build_name}-{arch_suffix}.tar.gz",
            f"{build_name}.tar.gz",
        ]
        tarball = tarball_candidates[0]  # resolved for real in Step 2
        deploy_dir = f"/home/bryck/{build_name}"

        # Step 1: If already installed, uninstall first (enforce clean install)
        self.logger.info("  Checking if Bryck is already installed...")
        exit_code, _, _ = self.ssh.run_command("test -d /opt/bryck")
        if exit_code == 0:
            self.logger.info("  Bryck is already installed. Stopping services and running clean uninstall...")
            # Stop all bryck services before uninstall for clean teardown
            self.ssh.run_command("systemctl stop 'bryck*' 2>/dev/null || true", use_sudo=True)
            self.ssh.run_command("systemctl stop bryckweb bryckutil bryckmonitor 2>/dev/null || true", use_sudo=True)
            # Find the existing deploy directory to run uninstall from
            exit_code, existing_dir, _ = self.ssh.run_command(
                "ls -d /home/bryck/tsecond-bryck-* 2>/dev/null | head -1"
            )
            uninstall_dir = existing_dir.strip() if exit_code == 0 and existing_dir.strip() else deploy_dir
            exit_code, out, err = self.ssh.run_command(
                f'su - bryck -c "cd {uninstall_dir} && python3 bryckdeploy uninstall -v"',
                use_sudo=True,
                timeout=300,
            )
            if exit_code != 0:
                self.logger.warning(f"  bryckdeploy uninstall returned non-zero: {err}")
                self.logger.info("  Attempting manual cleanup of /opt/bryck...")
                self.ssh.run_command("rm -rf /opt/bryck", use_sudo=True)
            else:
                self.logger.info("  bryckdeploy uninstall completed.")

        # Step 2: Download/transfer build tarball
        # Ensure the bryck home directory exists and is owned/writable by the
        # bryck user. Without this, SCP into /home/bryck fails with
        # "Permission denied" (the dir may be root-owned after provisioning).
        self.logger.info("  Ensuring /home/bryck is writable by the bryck user...")
        self.ssh.run_command("mkdir -p /home/bryck", use_sudo=True)
        self.ssh.run_command("chown bryck:bryck /home/bryck", use_sudo=True)
        self.ssh.run_command("chmod 755 /home/bryck", use_sudo=True)

        # First see if any candidate is already present locally AND is a valid
        # gzip archive. A bare `test -f` is not enough: a previous interrupted
        # SCP can leave a truncated/corrupt file behind, which then fails tar
        # with "unexpected end of file". `gzip -t` catches that so we re-download.
        local_tarball = None
        for candidate in tarball_candidates:
            exit_code, _, _ = self.ssh.run_command(f"test -f /home/bryck/{candidate}")
            if exit_code != 0:
                continue
            self.logger.info(f"  Found local /home/bryck/{candidate}; verifying integrity...")
            gz_ok, _, _ = self.ssh.run_command(f"gzip -t /home/bryck/{candidate}", use_sudo=True)
            if gz_ok == 0:
                local_tarball = candidate
                break
            self.logger.warning(
                f"  /home/bryck/{candidate} is corrupt/incomplete. Removing so it can be re-downloaded."
            )
            self.ssh.run_command(f"rm -f /home/bryck/{candidate}", use_sudo=True)

        if local_tarball is not None:
            tarball = local_tarball
            self.logger.info(f"  Tarball already exists at /home/bryck/{tarball}. Skipping download.")
        else:
            # Determine which build server to SCP from
            if self.ssh.config.bryck_type == BryckType.BRYCKMINI:
                build_server_ip = self.BUILD_SERVER_MINI_IP
            else:
                build_server_ip = self.BUILD_SERVER_AMD64_IP

            # sshpass is required for both the remote probe and the SCP below,
            # so make sure it is present before we use it.
            exit_code, _, _ = self.ssh.run_command("which sshpass")
            if exit_code != 0:
                self.logger.info("  Installing sshpass (needed for build-server transfer)...")
                self.ssh.run_command(
                    "DEBIAN_FRONTEND=noninteractive apt-get install -y sshpass", use_sudo=True
                )

            # Probe the build server for whichever candidate actually exists.
            ssh_opts = (
                "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                "-o LogLevel=ERROR -o ConnectTimeout=15"
            )
            remote_tarball = None
            for candidate in tarball_candidates:
                self.logger.info(
                    f"  Checking {self.BUILD_SERVER_USER}@{build_server_ip}:"
                    f"/home/bryck/builds/{candidate} ..."
                )
                probe_cmd = (
                    f"sshpass -p '{self.BUILD_SERVER_PASSWORD}' "
                    f"ssh {ssh_opts} "
                    f"{self.BUILD_SERVER_USER}@{build_server_ip} "
                    f"test -f /home/bryck/builds/{candidate}"
                )
                exit_code, _, _ = self.ssh.run_command(probe_cmd, use_sudo=True, timeout=60)
                if exit_code == 0:
                    remote_tarball = candidate
                    self.logger.info(f"  Found on build server: {candidate}")
                    break
                self.logger.info(f"  Not present: {candidate}")

            if remote_tarball is None:
                self.logger.error(
                    f"  No build tarball found on {self.BUILD_SERVER_USER}@{build_server_ip}:"
                    f"/home/bryck/builds/ (tried: {', '.join(tarball_candidates)})."
                )
                return False

            tarball = remote_tarball
            dest_path = f"/home/bryck/{tarball}"

            # Determine the source size up front so we can show real progress
            # and confirm the transfer actually completed.
            size_cmd = (
                f"sshpass -p '{self.BUILD_SERVER_PASSWORD}' "
                f"ssh {ssh_opts} {self.BUILD_SERVER_USER}@{build_server_ip} "
                f"stat -c %s /home/bryck/builds/{tarball}"
            )
            _, size_out, _ = self.ssh.run_command(size_cmd, use_sudo=True, timeout=60)
            source_size = int(size_out.strip()) if size_out.strip().isdigit() else 0
            if source_size:
                self.logger.info(f"  Build size: {source_size / (1024*1024):.1f} MiB")

            # Run the SCP in the BACKGROUND and monitor the destination file's
            # size. scp is silent (no TTY) so a foreground call looks frozen and
            # any fixed wall-clock cap risks killing a slow/large transfer. By
            # watching byte growth we can wait as long as it keeps progressing
            # and abort only on a genuine stall.
            self.logger.info(
                f"  Downloading from {self.BUILD_SERVER_USER}@{build_server_ip}:"
                f"/home/bryck/builds/{tarball} (background + progress monitor)..."
            )
            rc_file = "/home/bryck/.scp_rc"
            scp_log = "/home/bryck/.scp_log"
            self.ssh.run_command(f"rm -f {rc_file} {scp_log}", use_sudo=True)

            # Write a launcher script via SFTP to sidestep nested-quote issues.
            try:
                sftp = self.ssh.client.open_sftp()
                with sftp.file("/tmp/run_scp.sh", "w") as f:
                    f.write("#!/bin/bash\n")
                    f.write(
                        f"sshpass -p '{self.BUILD_SERVER_PASSWORD}' "
                        f"scp {ssh_opts} "
                        f"{self.BUILD_SERVER_USER}@{build_server_ip}:/home/bryck/builds/{tarball} "
                        f"{dest_path} > {scp_log} 2>&1\n"
                    )
                    f.write(f"echo $? > {rc_file}\n")
                sftp.close()
            except Exception as e:
                self.logger.error(f"  Failed to stage SCP launcher: {e}")
                return False
            self.ssh.run_command("mv /tmp/run_scp.sh /home/bryck/run_scp.sh", use_sudo=True)
            self.ssh.run_command("chmod +x /home/bryck/run_scp.sh", use_sudo=True)
            self.ssh.run_command("nohup bash /home/bryck/run_scp.sh >/dev/null 2>&1 &", use_sudo=True)

            # Monitor progress: poll destination size until the rc file appears.
            scp_rc = None
            last_size = -1
            last_change = time.monotonic()
            while True:
                _, rc_out, _ = self.ssh.run_command(f"cat {rc_file} 2>/dev/null || true")
                if rc_out.strip().isdigit():
                    scp_rc = int(rc_out.strip())
                    break

                _, sz_out, _ = self.ssh.run_command(f"stat -c %s {dest_path} 2>/dev/null || echo 0")
                cur = int(sz_out.strip()) if sz_out.strip().isdigit() else 0
                now = time.monotonic()
                if cur != last_size:
                    last_size = cur
                    last_change = now
                    if source_size:
                        pct = min(100, cur * 100 // source_size)
                        self.logger.info(
                            f"  Downloading... {cur / (1024*1024):.1f}/"
                            f"{source_size / (1024*1024):.1f} MiB ({pct}%)"
                        )
                    else:
                        self.logger.info(f"  Downloading... {cur / (1024*1024):.1f} MiB")
                elif now - last_change > self.ssh.config.idle_timeout:
                    self.logger.error(
                        f"  Download made no progress for {self.ssh.config.idle_timeout}s "
                        f"(stuck at {cur} bytes). Aborting."
                    )
                    self.ssh.run_command(f"rm -f {dest_path}", use_sudo=True)
                    return False
                time.sleep(5)

            if scp_rc != 0:
                _, scp_err, _ = self.ssh.run_command(f"cat {scp_log} 2>/dev/null || true")
                self.logger.error(f"  SCP from build server failed (rc={scp_rc}): {scp_err}")
                self.ssh.run_command(f"rm -f {dest_path}", use_sudo=True)
                return False

            # Verify the transferred archive is intact before we rely on it.
            gz_ok, _, gz_err = self.ssh.run_command(f"gzip -t {dest_path}", use_sudo=True)
            if gz_ok != 0:
                self.logger.error(
                    f"  Transferred tarball {dest_path} is corrupt after SCP: {gz_err}"
                )
                self.ssh.run_command(f"rm -f {dest_path}", use_sudo=True)
                return False
            self.logger.info(f"  Download complete and verified: {dest_path}")
            # Set ownership
            self.ssh.run_command(f"chown bryck:bryck {dest_path}", use_sudo=True)

        # Step 2b: Ensure sshpass is installed (needed for later SCP operations)
        exit_code, _, _ = self.ssh.run_command("which sshpass")
        if exit_code != 0:
            self.logger.info("  Installing sshpass...")
            self.ssh.run_command(
                "DEBIAN_FRONTEND=noninteractive apt-get install -y sshpass", use_sudo=True
            )

        # Step 3: Download inventory file
        self.logger.info("  Downloading inventory file...")
        exit_code, _, _ = self.ssh.run_command("test -f /home/bryck/inventory")
        if exit_code == 0:
            self.logger.info("  Inventory file already exists. Skipping download.")
        else:
            exit_code, _, err = self.ssh.run_command(
                f"wget -q -O /home/bryck/inventory {self.REPO_BASE_URL}/inventory",
                use_sudo=True,
            )
            if exit_code != 0:
                self.logger.error(f"  Failed to download inventory: {err}")
                return False
            self.ssh.run_command("chown bryck:bryck /home/bryck/inventory", use_sudo=True)

        # Step 4: Set bryck_type in inventory
        self.logger.info(f"  Setting bryck_type={inventory_type} in inventory...")
        exit_code, _, err = self.ssh.run_command(
            f"sed -i 's/^bryck_type=.*/bryck_type={inventory_type}/' /home/bryck/inventory",
            use_sudo=True,
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to update inventory: {err}")
            return False

        # Step 5: Extract tarball
        self.logger.info(f"  Extracting {tarball}...")
        exit_code, _, _ = self.ssh.run_command(f"test -d {deploy_dir}")
        if exit_code == 0:
            self.logger.info("  Build already extracted. Skipping.")
        else:
            exit_code, _, err = self.ssh.run_command(
                f"tar -xzf /home/bryck/{tarball} -C /home/bryck/",
                use_sudo=True,
            )
            if exit_code != 0:
                self.logger.error(f"  Failed to extract tarball: {err}")
                return False

        # Resolve the ACTUAL top-level directory the archive extracted to. It
        # usually matches build_name, but a differently-named tarball may unpack
        # into a different folder — in which case the hardcoded deploy_dir would
        # be wrong and the later `cd {deploy_dir}` would fail.
        exit_code, top_dir, _ = self.ssh.run_command(
            f"tar -tzf /home/bryck/{tarball} 2>/dev/null | head -1 | cut -d/ -f1"
        )
        top_dir = top_dir.strip()
        if top_dir and top_dir != build_name:
            self.logger.info(
                f"  Archive top-level dir '{top_dir}' differs from build name "
                f"'{build_name}'; using /home/bryck/{top_dir}."
            )
            deploy_dir = f"/home/bryck/{top_dir}"
        self.ssh.run_command(f"chown -R bryck:bryck {deploy_dir}", use_sudo=True)

        # Step 5b: Fix known package version conflicts before deploy
        # The ansible playbook pins jq=1.6-2.1ubuntu3 but libjq1 may have been
        # updated to 1.6-2.1ubuntu3.1, causing a dependency mismatch.
        # We must downgrade to the EXACT versions the playbook expects.
        self.logger.info("  Fixing potential package version conflicts (jq/libjq1)...")
        self.ssh.run_command(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades "
            "libjq1=1.6-2.1ubuntu3 jq=1.6-2.1ubuntu3 2>/dev/null || true",
            use_sudo=True,
        )

        # Step 6: Run bryckdeploy install (in background) and monitor ansible.log
        self.logger.info("  Running bryckdeploy install -v (this may take 15-30+ minutes)...")
        self.logger.info("  Monitoring /opt/ansible/ansible.log for completion...")

        # Record the current ansible.log line count so we only monitor new output
        self.ssh.run_command("mkdir -p /opt/ansible", use_sudo=True)
        self.ssh.run_command("touch /opt/ansible/ansible.log", use_sudo=True)
        self.ssh.run_command("chmod 666 /opt/ansible/ansible.log", use_sudo=True)
        exit_code, wc_out, _ = self.ssh.run_command("wc -l < /opt/ansible/ansible.log 2>/dev/null")
        ansible_log_start_line = int(wc_out.strip()) if wc_out.strip().isdigit() else 0
        self.logger.info(f"  ansible.log baseline: {ansible_log_start_line} lines (monitoring from here)")

        # Launch bryckdeploy in background using a launcher script (avoids
        # shell quoting / transport issues with nohup+su+sudo via paramiko)
        deploy_log = f"/home/bryck/bryckdeploy_output.log"
        launcher_script = f"/home/bryck/run_bryckdeploy.sh"
        try:
            sftp = self.ssh.client.open_sftp()
            with sftp.file("/tmp/run_bryckdeploy.sh", "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f"cd {deploy_dir}\n")
                f.write(f"python3 bryckdeploy install -v > {deploy_log} 2>&1\n")
            sftp.close()
        except Exception as e:
            self.logger.error(f"  Failed to write launcher script: {e}")
            return False

        self.ssh.run_command(f"mv /tmp/run_bryckdeploy.sh {launcher_script}", use_sudo=True)
        self.ssh.run_command(f"chmod +x {launcher_script}", use_sudo=True)
        self.ssh.run_command(f"chown bryck:bryck {launcher_script}", use_sudo=True)

        # Run the script as bryck user in background (nohup + disown)
        self.ssh.run_command(
            f"bash -c 'nohup su - bryck -c {launcher_script} </dev/null >/dev/null 2>&1 & disown'",
            use_sudo=True,
            timeout=10,
        )

        # Give the process a moment to start
        time.sleep(5)

        # Verify it launched
        exit_code, proc_out, _ = self.ssh.run_command("pgrep -f 'bryckdeploy install'")
        if exit_code != 0:
            self.logger.error("  bryckdeploy process did not start. Check launcher script.")
            exit_code, script_err, _ = self.ssh.run_command(f"cat {deploy_log} 2>/dev/null")
            self.logger.error(f"  Deploy log: {script_err}")
            return False
        self.logger.info(f"  bryckdeploy started (PID: {proc_out.strip()})")

        # Poll /opt/ansible/ansible.log until we see completion or failure
        success = self._wait_for_ansible_completion(deploy_dir, deploy_log, ansible_log_start_line)

        if not success:
            return False

        self.logger.info("  bryckdeploy install completed.")

        # Step 7: Post-deploy configuration
        self._post_deploy_config(inventory_type)

        self.logger.info("  Bryck build deployed successfully.")
        return True

    def _wait_for_ansible_completion(self, deploy_dir: str, deploy_log: str, log_start_line: int) -> bool:
        """
        Monitor /opt/ansible/ansible.log and the bryckdeploy process to detect
        when the installation finishes (success or failure).

        Only reads lines added AFTER log_start_line to ignore previous runs.

        Checks:
        - ansible.log for 'failed=1' or 'unreachable=1' -> failure
        - ansible.log for PLAY RECAP with 'failed=0' -> success
        - bryckdeploy process no longer running -> check exit status from log
        """
        ANSIBLE_LOG = "/opt/ansible/ansible.log"
        POLL_INTERVAL = 30   # seconds between checks
        MAX_WAIT = 2400      # 40 minutes max
        # tail command to only read lines from this run (skip previous content)
        tail_new_cmd = f"tail -n +{log_start_line + 1} {ANSIBLE_LOG} 2>/dev/null"

        elapsed = 0
        last_log_lines = 0

        while elapsed < MAX_WAIT:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            # Check if the bryckdeploy process is still running
            exit_code, proc_out, _ = self.ssh.run_command(
                "pgrep -f 'bryckdeploy install' > /dev/null 2>&1 && echo RUNNING || echo DONE"
            )
            process_running = "RUNNING" in proc_out

            # Read the tail of NEW ansible.log content to check for completion markers
            exit_code, log_tail, _ = self.ssh.run_command(
                f"{tail_new_cmd} | tail -50"
            )

            # Log progress (show new line count from this run)
            exit_code, wc_out, _ = self.ssh.run_command(f"wc -l < {ANSIBLE_LOG} 2>/dev/null")
            total_lines = int(wc_out.strip()) if wc_out.strip().isdigit() else 0
            current_lines = total_lines - log_start_line
            if current_lines > last_log_lines:
                self.logger.info(f"  [{elapsed}s] ansible.log: {current_lines} new lines (process {'running' if process_running else 'finished'})")
                last_log_lines = current_lines

            # Check for PLAY RECAP which indicates ansible finished
            if "PLAY RECAP" in log_tail:
                # Look for failure indicators in the RECAP
                if "failed=0" in log_tail and "unreachable=0" in log_tail:
                    self.logger.info("  Ansible PLAY RECAP: all tasks succeeded (failed=0, unreachable=0).")
                    return True
                elif "failed=" in log_tail:
                    # Extract the recap line for logging
                    for line in log_tail.splitlines():
                        if "failed=" in line:
                            self.logger.error(f"  Ansible PLAY RECAP indicates failure: {line.strip()}")
                    return False

            # If process is done but no PLAY RECAP yet, check deploy log for exit
            if not process_running:
                self.logger.info("  bryckdeploy process has exited. Checking results...")
                # Give a moment for log flush
                time.sleep(5)

                # Re-read ansible log for final state (only new content from this run)
                exit_code, final_log, _ = self.ssh.run_command(f"{tail_new_cmd} | tail -100")

                if "PLAY RECAP" in final_log:
                    if "failed=0" in final_log and "unreachable=0" in final_log:
                        self.logger.info("  Ansible PLAY RECAP: all tasks succeeded.")
                        return True
                    else:
                        for line in final_log.splitlines():
                            if "failed=" in line:
                                self.logger.error(f"  Ansible PLAY RECAP: {line.strip()}")
                        return False

                # No PLAY RECAP found — check deploy output log
                exit_code, deploy_out, _ = self.ssh.run_command(f"tail -20 {deploy_log} 2>/dev/null")
                self.logger.info(f"  bryckdeploy output (last 20 lines):\n{deploy_out}")

                # If /opt/bryck exists after deploy, consider it successful
                exit_code, _, _ = self.ssh.run_command("test -d /opt/bryck")
                if exit_code == 0:
                    self.logger.info("  /opt/bryck exists — treating as successful install.")
                    return True

                self.logger.error("  bryckdeploy exited without clear success signal.")
                return False

        # Timeout reached
        self.logger.error(f"  bryckdeploy did not complete within {MAX_WAIT}s.")
        # Show last ansible log lines for debugging (only from this run)
        exit_code, log_tail, _ = self.ssh.run_command(f"{tail_new_cmd} | tail -30")
        self.logger.error(f"  Last ansible.log lines:\n{log_tail}")
        return False

    def _post_deploy_config(self, inventory_type: str) -> None:
        """Apply post-deployment configuration."""

        # Update config.json
        config_file = "/etc/bryck/bryckutil/config.json"
        self.logger.info(f"  Updating {config_file}...")

        exit_code, _, _ = self.ssh.run_command(f"test -f {config_file}")
        if exit_code == 0:
            # Set enable_hot_plug to False
            self.ssh.run_command(
                f'sed -i \'s/"enable_hot_plug": *"[^"]*"/"enable_hot_plug": "False"/\' {config_file}',
                use_sudo=True,
            )
            # Set bryck_type
            self.ssh.run_command(
                f'sed -i \'s/"bryck_type": *"[^"]*"/"bryck_type": "{inventory_type}"/\' {config_file}',
                use_sudo=True,
            )
            self.logger.info(f"  config.json updated (enable_hot_plug=False, bryck_type={inventory_type}).")
        else:
            self.logger.warning(f"  {config_file} not found. Skipping config update.")

        # Add HAILO_MONITOR=1 to /etc/bash.bashrc
        self.logger.info("  Adding HAILO_MONITOR=1 to /etc/bash.bashrc...")
        exit_code, content, _ = self.ssh.run_command("grep 'HAILO_MONITOR' /etc/bash.bashrc")
        if exit_code == 0:
            self.logger.info("  HAILO_MONITOR already in bash.bashrc. Skipping.")
        else:
            self.ssh.run_command(
                'bash -c \'echo "export HAILO_MONITOR=1" >> /etc/bash.bashrc\'',
                use_sudo=True,
            )
            self.logger.info("  HAILO_MONITOR=1 added to /etc/bash.bashrc.")


class FixConfigJsonPermissions(SetupTask):
    """
    Task: Ensure /etc/bryck/bryckutil/config.json is owned by bryck:bryck
    with permissions 755 (-rwxr-xr-x).

    Runs unconditionally (with or without --bryck-build) so that the
    correct owner/mode is enforced even when the deploy step is skipped.
    Skips gracefully if the file does not exist yet.
    """

    name = "Fix config.json Permissions (bryck:bryck 755)"
    CONFIG_FILE = "/etc/bryck/bryckutil/config.json"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Check if the file exists
        exit_code, _, _ = self.ssh.run_command(f"test -f {self.CONFIG_FILE}")
        if exit_code != 0:
            self.logger.info(f"  {self.CONFIG_FILE} not found. Skipping (deploy not yet run).")
            return True

        # Step 2: Check current ownership and permissions
        exit_code, stat_out, _ = self.ssh.run_command(
            f"stat -c '%U %G %a' {self.CONFIG_FILE}", use_sudo=True
        )
        parts = stat_out.strip().split()
        if len(parts) == 3:
            owner, group, mode = parts
            self.logger.info(f"  Current: owner={owner}, group={group}, mode={mode}")
            if owner == "bryck" and group == "bryck" and mode == "755":
                self.logger.info("  Permissions already correct. Skipping.")
                return True
        else:
            self.logger.warning(f"  Could not parse stat output: '{stat_out}'. Proceeding with fix.")

        # Step 3: Fix ownership
        self.logger.info(f"  Setting owner bryck:bryck on {self.CONFIG_FILE}...")
        exit_code, _, err = self.ssh.run_command(
            f"chown bryck:bryck {self.CONFIG_FILE}", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  chown failed: {err}")
            return False

        # Step 4: Fix permissions
        self.logger.info(f"  Setting mode 755 on {self.CONFIG_FILE}...")
        exit_code, _, err = self.ssh.run_command(
            f"chmod 755 {self.CONFIG_FILE}", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  chmod failed: {err}")
            return False

        # Step 5: Verify
        exit_code, verify_out, _ = self.ssh.run_command(
            f"stat -c '%U %G %a' {self.CONFIG_FILE}", use_sudo=True
        )
        parts = verify_out.strip().split()
        if len(parts) == 3 and parts[0] == "bryck" and parts[1] == "bryck" and parts[2] == "755":
            self.logger.info(f"  config.json permissions fixed: bryck:bryck 755.")
            return True
        else:
            self.logger.error(f"  Verification failed. stat output: '{verify_out}'")
            return False


class ConfigureNFSExport(SetupTask):
    """
    Task: Validate and fix NFS export options in Ansible configuration.

    Ensures anonuid and anongid in /opt/ansible/roles/add-export/vars/main.yml
    match the actual UID and GID of the bryck user. This is required for
    correct NFS access permissions on the Bryck mount point.

    Applies to both bryckserver and bryckmini.
    Skipped gracefully if /opt/ansible does not exist (deploy was skipped).
    """

    name = "Configure NFS Export (anonuid/anongid)"
    NFS_VARS_FILE = "/opt/ansible/roles/add-export/vars/main.yml"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Check if the file exists (requires a completed deploy)
        self.logger.info(f"  Checking {self.NFS_VARS_FILE}...")
        exit_code, _, _ = self.ssh.run_command(f"test -f {self.NFS_VARS_FILE}", use_sudo=True)
        if exit_code != 0:
            self.logger.info("  File not found — deploy may have been skipped. Skipping NFS config.")
            return True

        # Step 2: Get bryck user UID and GID
        exit_code, uid_out, err = self.ssh.run_command("id -u bryck")
        if exit_code != 0:
            self.logger.error(f"  Could not get bryck UID: {err}")
            return False
        bryck_uid = uid_out.strip()

        exit_code, gid_out, err = self.ssh.run_command("id -g bryck")
        if exit_code != 0:
            self.logger.error(f"  Could not get bryck GID: {err}")
            return False
        bryck_gid = gid_out.strip()

        self.logger.info(f"  bryck user: uid={bryck_uid}, gid={bryck_gid}")

        # Step 3: Read current file content
        exit_code, current_content, _ = self.ssh.run_command(
            f"cat {self.NFS_VARS_FILE}", use_sudo=True
        )
        self.logger.info(f"  Current content:\n{current_content}")

        # Step 4: Check if already correct
        if f"anonuid={bryck_uid}" in current_content and f"anongid={bryck_gid}" in current_content:
            self.logger.info("  anonuid/anongid already match bryck UID/GID. Skipping.")
            return True

        # Step 5: Update anonuid
        self.logger.info(f"  Setting anonuid={bryck_uid}...")
        exit_code, _, err = self.ssh.run_command(
            f"sed -i 's/anonuid=[0-9]*/anonuid={bryck_uid}/g' {self.NFS_VARS_FILE}",
            use_sudo=True,
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to update anonuid: {err}")
            return False

        # Step 6: Update anongid
        self.logger.info(f"  Setting anongid={bryck_gid}...")
        exit_code, _, err = self.ssh.run_command(
            f"sed -i 's/anongid=[0-9]*/anongid={bryck_gid}/g' {self.NFS_VARS_FILE}",
            use_sudo=True,
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to update anongid: {err}")
            return False

        # Step 7: Verify
        self.logger.info("  Verifying configuration...")
        exit_code, final_content, _ = self.ssh.run_command(
            f"cat {self.NFS_VARS_FILE}", use_sudo=True
        )
        self.logger.info(f"  Final content:\n{final_content}")

        if f"anonuid={bryck_uid}" in final_content and f"anongid={bryck_gid}" in final_content:
            self.logger.info("  NFS export options configured successfully.")
            return True
        else:
            self.logger.error("  Verification failed — anonuid/anongid not updated correctly.")
            return False


class ConfigureNetworkManagerInterfaces(SetupTask):
    """
    Task: Configure NetworkManager interfaces — runs independently of bryck build.

    - Ensures mlx5 data-path interfaces are named p0/p1 (loads driver, creates
      udev rename rules by MAC, renames live if needed)
    - Configures oob_net0 (managed, autoconnect, connect, reapply)
    - Writes 40-mlnx.conf (disables Mellanox keyfile)
    - Configures NetworkManager.conf (dns=none, ifupdown managed=true)
    - Disables systemd-networkd, switches to NetworkManager
    - Purges netplan
    - Renames netplan-oob_net0 -> oob_net0 via nmcli
    - Adds p0 and p1 ethernet connections via nmcli
    - Disables cloud network config
    - Disables default route on tmfifo_net0
    """

    name = "Configure NetworkManager Interfaces"

    MLNX_CONF_CONTENT = "[keyfile]\n#unmanaged-devices+=driver:mlx5_core;driver:mlx5e_rep;driver:vxlan\n"

    # Interfaces that are NOT data-path ports (excluded from p0/p1 detection)
    EXCLUDED_IFACES = {"lo", "oob_net0", "tmfifo_net0", "p0", "p1"}

    def _ensure_p0_p1_interfaces(self) -> None:
        """
        Ensure mlx5 data-path interfaces are named p0/p1.

        1. Loads mlx5_core kernel module if not already loaded.
        2. Discovers mlx5_core interfaces that are not management interfaces.
        3. If found with non-canonical names, renames them live with 'ip link'.
        4. Writes /etc/udev/rules.d/70-bryck-net.rules (MAC-based) so the names
           persist across reboots.
        """
        self.logger.info("  Ensuring p0/p1 interfaces exist...")

        # Step 1: Load mlx5_core if not loaded
        exit_code, _, _ = self.ssh.run_command("lsmod | grep -q mlx5_core")
        if exit_code != 0:
            # Try to install linux-modules-extra for the running kernel first
            # (mlx5_core may not be present on BlueField custom kernels until installed)
            self.logger.info("  mlx5_core not loaded — installing kernel modules if needed...")
            exit_code, uname_out, _ = self.ssh.run_command("uname -r")
            kernel = uname_out.strip()
            self.ssh.run_command(
                f"DEBIAN_FRONTEND=noninteractive apt-get install -y "
                f"linux-modules-extra-{kernel} 2>/dev/null || true",
                use_sudo=True,
            )
            self.logger.info("  Loading mlx5_core...")
            exit_code, _, err = self.ssh.run_command("modprobe mlx5_core", use_sudo=True)
            if exit_code != 0:
                self.logger.warning(
                    f"  modprobe mlx5_core failed: {err}. "
                    "p0/p1 may require MLNX_OFED or a reboot after package install."
                )
            else:
                self.logger.info("  mlx5_core loaded successfully.")
                time.sleep(3)
                # Persist mlx5_core load on boot
                self.ssh.run_command(
                    "grep -qxF 'mlx5_core' /etc/modules || echo 'mlx5_core' >> /etc/modules",
                    use_sudo=True,
                )
                self.logger.info("  mlx5_core added to /etc/modules (loads on boot).")
        else:
            self.logger.info("  mlx5_core already loaded.")

        # Step 2: Check if p0/p1 already exist
        exit_code, ifaces_out, _ = self.ssh.run_command("ls /sys/class/net/ 2>/dev/null")
        existing = set(ifaces_out.split())
        if "p0" in existing and "p1" in existing:
            self.logger.info("  p0 and p1 already exist. Skipping rename.")
            return

        # Step 3: Find mlx5_core data-path interfaces (not management ones)
        find_cmd = (
            "for iface in $(ls /sys/class/net/ 2>/dev/null); do "
            "  case $iface in lo|oob_net0|tmfifo_net0|p0|p1) continue ;; esac; "
            "  driver=$(readlink /sys/class/net/$iface/device/driver 2>/dev/null "
            "            | xargs basename 2>/dev/null); "
            '  [ "$driver" = "mlx5_core" ] && echo $iface; '
            "done"
        )
        exit_code, found_out, _ = self.ssh.run_command(find_cmd)
        found_ifaces = sorted([i.strip() for i in found_out.splitlines() if i.strip()])

        if not found_ifaces:
            self.logger.warning(
                "  No mlx5_core data-path interfaces found. "
                "p0/p1 may appear after bryckdeploy or on reboot."
            )
            return

        self.logger.info(f"  Found mlx5 data-path interfaces: {found_ifaces}")

        # Step 4: Map first -> p0, second -> p1
        mappings = {}
        if len(found_ifaces) >= 1:
            mappings[found_ifaces[0]] = "p0"
        if len(found_ifaces) >= 2:
            mappings[found_ifaces[1]] = "p1"

        udev_lines = []
        for old_name, new_name in mappings.items():
            # Get MAC for udev rule
            exit_code, mac_out, _ = self.ssh.run_command(
                f"cat /sys/class/net/{old_name}/address 2>/dev/null"
            )
            mac = mac_out.strip()
            if mac:
                udev_lines.append(
                    f'SUBSYSTEM=="net", ACTION=="add", ATTR{{address}}=="{mac}", NAME="{new_name}"'
                )
                self.logger.info(f"  {old_name} (mac={mac}) -> {new_name}")

            # Rename live for current session
            self.logger.info(f"  Renaming {old_name} -> {new_name} live...")
            # Wrap in bash -c so sudo applies to the WHOLE compound command;
            # otherwise only the first 'ip link' runs as root and the rest run
            # unprivileged.
            exit_code, _, err = self.ssh.run_command(
                f"bash -c 'ip link set {old_name} down 2>/dev/null; "
                f"ip link set {old_name} name {new_name} && ip link set {new_name} up'",
                use_sudo=True,
            )
            if exit_code != 0:
                self.logger.warning(f"  Live rename {old_name} -> {new_name} failed: {err}")
            else:
                self.logger.info(f"  {new_name} is up.")

        # Step 5: Write udev rules for persistent naming across reboots
        if udev_lines:
            rules_content = "\n".join(udev_lines) + "\n"
            try:
                sftp = self.ssh.client.open_sftp()
                with sftp.file("/tmp/70-bryck-net.rules", "w") as f:
                    f.write(rules_content)
                sftp.close()
            except Exception as e:
                self.logger.warning(f"  Failed to write udev rules via SFTP: {e}")
                return
            self.ssh.run_command(
                "bash -c 'mv /tmp/70-bryck-net.rules /etc/udev/rules.d/70-bryck-net.rules "
                "&& udevadm control --reload-rules && udevadm trigger'",
                use_sudo=True,
            )
            self.logger.info("  Udev rules written: p0/p1 naming will persist on reboot.")

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # --- Step 0: Ensure p0/p1 interfaces exist (mlx5 driver + udev rename) ---
        self._ensure_p0_p1_interfaces()

        # --- Step 1: Configure oob_net0 ---
        self.logger.info("  Configuring oob_net0 interface...")
        self.ssh.run_command("nmcli device set oob_net0 managed yes 2>/dev/null || true", use_sudo=True)
        self.ssh.run_command("nmcli connection modify oob_net0 autoconnect yes 2>/dev/null || true", use_sudo=True)
        self.ssh.run_command("nmcli device connect oob_net0 2>/dev/null || true", use_sudo=True)
        self.ssh.run_command("nmcli device reapply oob_net0 2>/dev/null || true", use_sudo=True)

        # --- Step 2: Write /etc/NetworkManager/conf.d/40-mlnx.conf ---
        self.logger.info("  Writing /etc/NetworkManager/conf.d/40-mlnx.conf...")
        try:
            sftp = self.ssh.client.open_sftp()
            with sftp.file("/tmp/40-mlnx.conf", "w") as f:
                f.write(self.MLNX_CONF_CONTENT)
            sftp.close()
        except Exception as e:
            self.logger.error(f"  SFTP write failed: {e}")
            return False

        self.ssh.run_command(
            "bash -c 'mkdir -p /etc/NetworkManager/conf.d && mv /tmp/40-mlnx.conf /etc/NetworkManager/conf.d/40-mlnx.conf'",
            use_sudo=True,
        )

        # --- Step 3: Configure NetworkManager.conf ---
        self.logger.info("  Configuring /etc/NetworkManager/NetworkManager.conf...")
        nm_conf = "/etc/NetworkManager/NetworkManager.conf"
        exit_code, nm_content, _ = self.ssh.run_command(f"cat {nm_conf} 2>/dev/null || echo ''")

        if "dns=none" not in nm_content:
            if "[main]" in nm_content:
                self.ssh.run_command(
                    f"sed -i '/^\\[main\\]/a dns=none' {nm_conf}", use_sudo=True
                )
            else:
                self.ssh.run_command(
                    f"printf '\\n[main]\\ndns=none\\n' >> {nm_conf}", use_sudo=True
                )
            self.logger.info("  [main] dns=none added.")
        else:
            self.logger.info("  dns=none already present.")

        if "[ifupdown]" in nm_content and "managed=true" in nm_content:
            self.logger.info("  [ifupdown] managed=true already present.")
        else:
            if "[ifupdown]" in nm_content:
                self.ssh.run_command(
                    f"sed -i 's/^managed=false/managed=true/' {nm_conf}", use_sudo=True
                )
            else:
                self.ssh.run_command(
                    f"printf '\\n[ifupdown]\\nmanaged=true\\n' >> {nm_conf}", use_sudo=True
                )
            self.logger.info("  [ifupdown] managed=true configured.")

        # --- Step 4: Reload NM, disable systemd-networkd, restart NM ---
        self.logger.info("  Reloading NetworkManager...")
        self.ssh.run_command("systemctl reload NetworkManager.service", use_sudo=True)

        self.logger.info("  Disabling systemd-networkd, switching to NetworkManager...")
        self.ssh.run_command(
            "systemctl disable --now systemd-networkd.service systemd-networkd.socket networkd-dispatcher.service 2>/dev/null || true",
            use_sudo=True,
        )
        self.ssh.run_command("systemctl restart NetworkManager", use_sudo=True)

        # --- Step 5: Purge netplan ---
        self.logger.info("  Purging netplan...")
        self.ssh.run_command(
            "DEBIAN_FRONTEND=noninteractive apt purge -y netplan netplan.io 2>/dev/null || true",
            use_sudo=True,
        )

        # --- Step 6: Rename netplan-oob_net0 -> oob_net0, add p0/p1 ---
        self.logger.info("  Checking NM connections...")
        exit_code, connections, _ = self.ssh.run_command("nmcli -t -f NAME connection show 2>/dev/null || echo ''")

        if "netplan-oob_net0" in connections:
            self.ssh.run_command(
                'nmcli connection modify "netplan-oob_net0" con-name "oob_net0"', use_sudo=True
            )
            self.logger.info("  Renamed netplan-oob_net0 -> oob_net0.")
        else:
            self.logger.info("  netplan-oob_net0 not found (already renamed or not yet created). Skipping.")

        if "p0" not in connections:
            self.ssh.run_command(
                "nmcli connection add type ethernet con-name p0 ifname p0 autoconnect yes",
                use_sudo=True,
            )
            self.logger.info("  p0 connection added.")
        else:
            self.logger.info("  p0 already exists. Skipping.")

        if "p1" not in connections:
            self.ssh.run_command(
                "nmcli connection add type ethernet con-name p1 ifname p1 autoconnect yes",
                use_sudo=True,
            )
            self.logger.info("  p1 connection added.")
        else:
            self.logger.info("  p1 already exists. Skipping.")

        self.ssh.run_command("systemctl reload NetworkManager", use_sudo=True)

        # --- Step 7: Disable cloud network config ---
        self.logger.info("  Disabling cloud network config...")
        self.ssh.run_command("mkdir -p /etc/cloud/cloud.cfg.d", use_sudo=True)
        exit_code, _, _ = self.ssh.run_command(
            "test -f /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg"
        )
        if exit_code != 0:
            try:
                sftp = self.ssh.client.open_sftp()
                with sftp.file("/tmp/99-disable-network-config.cfg", "w") as f:
                    f.write("{config: disabled}\n")
                sftp.close()
            except Exception as e:
                self.logger.warning(f"  SFTP write for cloud config failed: {e}")
            else:
                self.ssh.run_command(
                    "mv /tmp/99-disable-network-config.cfg /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg",
                    use_sudo=True,
                )
                self.logger.info("  Cloud network config disabled.")
        else:
            self.logger.info("  Cloud network config already disabled. Skipping.")

        # --- Step 8: Disable default route on tmfifo_net0 ---
        self.logger.info("  Disabling default route on tmfifo_net0...")
        tmfifo_file = "/etc/NetworkManager/system-connections/tmfifo_net0.nmconnection"
        exit_code, _, _ = self.ssh.run_command(f"test -f {tmfifo_file}")
        if exit_code == 0:
            self.ssh.run_command(f"sed -i 's/^dns=/#dns=/' {tmfifo_file}", use_sudo=True)
            self.ssh.run_command(f"sed -i 's/^route1=/#route1=/' {tmfifo_file}", use_sudo=True)
            self.ssh.run_command("systemctl reload NetworkManager", use_sudo=True)
            self.logger.info("  tmfifo_net0 default route disabled.")
        else:
            self.logger.info(f"  {tmfifo_file} not found. Skipping.")

        self.logger.info("  NetworkManager interfaces configured successfully.")
        return True


class PostDeployVenvPackages(SetupTask):
    """
    Task: Post-deployment bryck-specific packages (requires bryck build installed).

    - Installs xfce4 desktop and xrdp
    - Disables netfilter-persistent and openibd
    - Installs pyroute2==0.7.12, netifaces into bryck venv
    """

    name = "Post-Deploy Venv Packages & Desktop"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # --- Step 1: Install XFCE4 desktop & XRDP ---
        self.logger.info("  Installing xfce4 and xfce4-goodies...")
        exit_code, _, err = self.ssh.run_command(
            "DEBIAN_FRONTEND=noninteractive apt install -y xfce4 xfce4-goodies", use_sudo=True
        )
        if exit_code != 0:
            self.logger.warning(f"  xfce4 install issue: {err}")
        else:
            self.logger.info("  xfce4 installed.")

        self.logger.info("  Installing xrdp...")
        exit_code, _, err = self.ssh.run_command(
            "DEBIAN_FRONTEND=noninteractive apt install -y xrdp", use_sudo=True
        )
        if exit_code != 0:
            self.logger.warning(f"  xrdp install issue: {err}")
        else:
            self.logger.info("  xrdp installed.")

        # --- Step 2: Disable netfilter-persistent and openibd ---
        self.logger.info("  Disabling netfilter-persistent and openibd...")
        self.ssh.run_command(
            "systemctl disable netfilter-persistent openibd 2>/dev/null || true", use_sudo=True
        )

        # --- Step 3: Install pip packages in bryck venv ---
        venv_pip = "/opt/bryck/.venv/bryck/bin/pip3"
        exit_code, _, _ = self.ssh.run_command(f"test -f {venv_pip}")
        if exit_code != 0:
            self.logger.warning(f"  bryck venv not found at {venv_pip}. Skipping venv installs.")
            return True

        for pkg, version in [("pyroute2", "0.7.12"), ("netifaces", None)]:
            pkg_spec = f"{pkg}=={version}" if version else pkg
            self.logger.info(f"  Installing {pkg_spec} in bryck venv...")
            exit_code, _, err = self.ssh.run_command(
                f"{venv_pip} install {pkg_spec}", use_sudo=True
            )
            if exit_code != 0:
                self.logger.warning(f"  {pkg_spec} install issue: {err}")
            else:
                self.logger.info(f"  {pkg_spec} installed.")

        self.logger.info("  Post-deploy venv packages installed successfully.")
        return True


class DownloadBryckCLIAndNFSDPatch(SetupTask):
    """
    Task: Download BryckCLI and NFSD Patch from TSecond repository.

    - Downloads bryckcli.tar.gz (command-line interface for Bryck operations)
    - Downloads nfsd_patch.tar.gz (custom NFSD kernel module optimized for Bryck)
    """

    name = "Download BryckCLI & NFSD Patch"

    DOWNLOADS = [
        ("http://repos.tsecond.ai/ubuntu/bryckcli.tar.gz", "/home/bryck/bryckcli.tar.gz"),
        ("http://repos.tsecond.ai/ubuntu/nfsd_patch.tar.gz", "/home/bryck/nfsd_patch.tar.gz"),
    ]

    def _ensure_dns(self) -> None:
        """Check DNS resolution and fix /etc/resolv.conf if broken."""
        self.logger.info("  Checking DNS resolution...")
        exit_code, _, _ = self.ssh.run_command(
            "nslookup repos.tsecond.ai >/dev/null 2>&1"
        )
        if exit_code == 0:
            self.logger.info("  DNS is working.")
            return

        self.logger.warning("  DNS resolution failed. Fixing /etc/resolv.conf...")
        self.ssh.run_command(
            "bash -c 'printf \"nameserver 8.8.8.8\\n\" > /etc/resolv.conf'", use_sudo=True
        )
        # Verify fix
        exit_code, _, _ = self.ssh.run_command(
            "nslookup repos.tsecond.ai >/dev/null 2>&1"
        )
        if exit_code == 0:
            self.logger.info("  DNS fixed successfully.")
        else:
            self.logger.error("  DNS still broken after fix attempt.")

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Ensure DNS works (may have been broken by NetworkManager reconfiguration)
        self._ensure_dns()

        success = True
        for url, dest in self.DOWNLOADS:
            filename = dest.split("/")[-1]

            # Check if already downloaded
            exit_code, _, _ = self.ssh.run_command(f"test -s {dest}", use_sudo=True)
            if exit_code == 0:
                self.logger.info(f"  {filename} already exists at {dest}. Skipping.")
                continue

            self.logger.info(f"  Downloading {url}...")
            exit_code, _, err = self.ssh.run_command(
                f"wget {url} -O {dest}", use_sudo=True, timeout=120
            )
            if exit_code != 0:
                self.logger.error(f"  Failed to download {filename}: {err}")
                success = False
                continue

            # Verify file was actually downloaded (non-zero size)
            exit_code, _, _ = self.ssh.run_command(f"test -s {dest}", use_sudo=True)
            if exit_code != 0:
                self.logger.error(f"  {filename} download produced empty/missing file.")
                success = False
                continue

            # Set ownership
            self.ssh.run_command(f"chown bryck:bryck {dest}", use_sudo=True)
            self.logger.info(f"  {filename} downloaded successfully.")

        return success


class ApplyNFSDPatchAndRemoveSamba(SetupTask):
    """
    Task: Apply NFSD patch and remove Samba services.

    - Stops nfs-server and nfs-kernel-server
    - Extracts nfsd_patch.tar.gz and runs replace_nfsd_module.sh
      to replace the default NFSD kernel module with Bryck's custom one
    - Stops and disables smbd and nmbd (Bryck uses ksmbd instead)
    - Removes smbd and nmbd service files
    """

    name = "Apply NFSD Patch & Remove Samba"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # NFSD patch is only for bryckserver (x86-64, kernel 6.5.0-45-generic)
        # bryckmini uses a different kernel and doesn't need this patch
        if self.ssh.config.bryck_type == BryckType.BRYCKMINI:
            self.logger.info("  Skipping NFSD patch: not applicable to bryckmini (different kernel).")
            # Still remove Samba services on bryckmini
            self._remove_samba()
            return True

        # --- Part 1: Apply NFSD Patch ---
        self.logger.info("  Stopping NFS server...")
        self.ssh.run_command("systemctl stop nfs-server 2>/dev/null || true", use_sudo=True)
        self.ssh.run_command("systemctl stop nfs-kernel-server 2>/dev/null || true", use_sudo=True)

        # Check if nfsd_patch.tar.gz exists
        tarball = "/home/bryck/nfsd_patch.tar.gz"
        exit_code, _, _ = self.ssh.run_command(f"test -f {tarball}", use_sudo=True)
        if exit_code != 0:
            self.logger.error(f"  {tarball} not found. Run DownloadBryckCLIAndNFSDPatch first.")
            return False

        # Extract nfsd_patch.tar.gz
        self.logger.info("  Extracting nfsd_patch.tar.gz...")
        extract_dir = "/home/bryck/nfsd_patch"
        exit_code, _, err = self.ssh.run_command(
            f"tar -xzf {tarball} -C /home/bryck/", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to extract nfsd_patch.tar.gz: {err}")
            return False

        # Run replace_nfsd_module.sh
        self.logger.info("  Running replace_nfsd_module.sh...")
        exit_code, out, err = self.ssh.run_command(
            f"bash {extract_dir}/replace_nfsd_module.sh", use_sudo=True, timeout=120
        )
        if exit_code != 0:
            self.logger.error(f"  replace_nfsd_module.sh failed: {err}")
            return False
        self.logger.info(f"  NFSD patch applied successfully.")
        if out:
            self.logger.debug(f"  Script output: {out}")

        # --- Part 2: Remove Samba Services ---
        self._remove_samba()

        self.logger.info("  NFSD patch applied and Samba services removed.")
        return True

    def _remove_samba(self) -> None:
        """Stop, disable, and remove Samba service files."""
        self.logger.info("  Stopping smbd and nmbd...")
        self.ssh.run_command("systemctl stop smbd nmbd 2>/dev/null || true", use_sudo=True)

        self.logger.info("  Disabling smbd and nmbd...")
        self.ssh.run_command("systemctl disable smbd nmbd 2>/dev/null || true", use_sudo=True)

        self.logger.info("  Removing smbd and nmbd service files...")
        self.ssh.run_command(
            "rm -f /lib/systemd/system/smbd.service /lib/systemd/system/nmbd.service",
            use_sudo=True,
        )

        # Reload systemd to pick up removed service files
        self.ssh.run_command("systemctl daemon-reload", use_sudo=True)

        self.logger.info("  Samba services removed (Bryck uses ksmbd).")


class InstallBryckCLI(SetupTask):
    """
    Task: Install BryckCLI tool.

    BryckCLI provides a command-line interface to manage all Bryck operations
    such as Format, Mount, Eject, Erase, and Scan from the terminal.

    - Extracts bryckcli.tar.gz
    - Runs deploy_bryckcli install
    - Verifies installation succeeded
    """

    name = "Install BryckCLI"

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Check if already installed
        exit_code, _, _ = self.ssh.run_command("which bryckcli", use_sudo=True)
        if exit_code == 0:
            self.logger.info("  BryckCLI already installed. Skipping.")
            return True

        # Step 2: Verify tarball exists
        tarball = "/home/bryck/bryckcli.tar.gz"
        exit_code, _, _ = self.ssh.run_command(f"test -f {tarball}", use_sudo=True)
        if exit_code != 0:
            self.logger.error(f"  {tarball} not found. Run DownloadBryckCLIAndNFSDPatch first.")
            return False

        # Step 3: Extract bryckcli.tar.gz
        extract_dir = "/home/bryck/bryckcli"
        self.logger.info("  Extracting bryckcli.tar.gz...")
        exit_code, _, err = self.ssh.run_command(
            f"tar -xzf {tarball} -C /home/bryck/", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to extract bryckcli.tar.gz: {err}")
            return False

        # Step 4: Run deploy_bryckcli install
        self.logger.info("  Running deploy_bryckcli install...")
        exit_code, out, err = self.ssh.run_command(
            f"bash {extract_dir}/deploy_bryckcli install", use_sudo=True, timeout=120
        )
        if exit_code != 0:
            self.logger.error(f"  deploy_bryckcli install failed: {err}")
            return False

        # Step 5: Verify installation
        if "Installed" in out:
            self.logger.info(f"  BryckCLI installed successfully. Output: {out.strip()}")
        else:
            # Check if the binary is available even without the expected message
            exit_code, _, _ = self.ssh.run_command("which bryckcli", use_sudo=True)
            if exit_code == 0:
                self.logger.info("  BryckCLI binary found (install output differed from expected).")
            else:
                self.logger.error(f"  BryckCLI installation verification failed. Output: {out}")
                return False

        return True


class ConfigureNVMeDrives(SetupTask):
    """
    Task: Detect NVMe drive models and OS drive, update config.json.

    - Runs 'nvme list' to detect all NVMe drive models and serial numbers
    - Appends any new drive models to bryck_drive_model in config.json
    - Identifies the OS drive (root filesystem) and appends its serial to skip_drives
    - Only appends, never removes or replaces existing entries
    """

    name = "Configure NVMe Drives (config.json)"
    CONFIG_FILE = "/etc/bryck/bryckutil/config.json"

    def _parse_nvme_list(self, output: str) -> list[dict]:
        """
        Parse 'nvme list' output into a list of dicts with keys:
        node, sn, model
        """
        drives = []
        lines = output.splitlines()
        # Find the header line to determine column positions
        header_idx = None
        for i, line in enumerate(lines):
            if "Node" in line and "SN" in line and "Model" in line:
                header_idx = i
                break

        if header_idx is None:
            return drives

        header = lines[header_idx]
        # Find column start positions from header
        node_start = header.find("Node")
        sn_start = header.find("SN")
        model_start = header.find("Model")
        # Find next column after Model (Namespace)
        ns_start = header.find("Namespace")
        if ns_start == -1:
            ns_start = header.find("Usage")

        # Parse data lines (skip header and separator)
        for line in lines[header_idx + 2:]:
            if not line.strip() or line.startswith("-"):
                continue
            # Extract fields by column positions
            node = line[node_start:sn_start].strip() if sn_start > node_start else ""
            sn = line[sn_start:model_start].strip() if model_start > sn_start else ""
            model = line[model_start:ns_start].strip() if ns_start > model_start else ""

            if node and sn:
                drives.append({"node": node, "sn": sn, "model": model})

        return drives

    def _get_os_drive_device(self) -> str | None:
        """Identify the NVMe device holding the root filesystem."""
        # Get the device for /
        exit_code, df_out, _ = self.ssh.run_command("df / | tail -1 | awk '{print $1}'")
        if exit_code != 0 or not df_out.strip():
            return None

        root_device = df_out.strip()  # e.g. /dev/nvme0n1p1 or /dev/nvme0n1

        # Strip partition number to get the base device (e.g. /dev/nvme0n1p1 -> /dev/nvme0n1)
        # NVMe partitions are like /dev/nvme0n1p1, base is /dev/nvme0n1
        if "nvme" in root_device:
            # Remove trailing pN (partition)
            import re
            base = re.sub(r'p\d+$', '', root_device)
            return base

        return None

    def run(self) -> bool:
        self.logger.info(f"[TASK] {self.name}")

        # Step 1: Check if config.json exists
        exit_code, _, _ = self.ssh.run_command(f"test -f {self.CONFIG_FILE}", use_sudo=True)
        if exit_code != 0:
            self.logger.info(f"  {self.CONFIG_FILE} not found (deploy may have been skipped). Skipping.")
            return True

        # Step 2: Run nvme list
        self.logger.info("  Running nvme list...")
        exit_code, nvme_out, err = self.ssh.run_command("nvme list", use_sudo=True)
        if exit_code != 0:
            self.logger.error(f"  nvme list failed: {err}")
            return False

        self.logger.info(f"  nvme list output:\n{nvme_out}")

        # Step 3: Parse drive info
        drives = self._parse_nvme_list(nvme_out)
        if not drives:
            self.logger.warning("  No NVMe drives detected. Skipping.")
            return True

        self.logger.info(f"  Detected {len(drives)} NVMe drive(s):")
        for d in drives:
            self.logger.info(f"    {d['node']} | SN: {d['sn']} | Model: {d['model']}")

        # Step 4: Read current config.json
        self.logger.info(f"  Reading {self.CONFIG_FILE}...")
        exit_code, config_content, err = self.ssh.run_command(
            f"cat {self.CONFIG_FILE}", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to read config.json: {err}")
            return False

        import json
        try:
            config = json.loads(config_content)
        except json.JSONDecodeError as e:
            self.logger.error(f"  Failed to parse config.json: {e}")
            return False

        # Step 5: Update bryck_drive_model (append new models only)
        existing_models = config.get("bryck_drive_model", [])
        new_models = []
        for d in drives:
            model = d["model"]
            if model and model not in existing_models:
                new_models.append(model)

        if new_models:
            self.logger.info(f"  Appending new drive models: {new_models}")
            config["bryck_drive_model"] = existing_models + new_models
        else:
            self.logger.info("  All drive models already in config. No changes needed.")

        # Step 6: Identify OS drive and add its serial to skip_drives
        self.logger.info("  Identifying OS drive...")
        os_device = self._get_os_drive_device()
        existing_skip = config.get("skip_drives", [])
        new_skips = []

        if os_device:
            self.logger.info(f"  OS root filesystem is on: {os_device}")
            # Find the serial number of the OS drive
            for d in drives:
                if d["node"] == os_device:
                    if d["sn"] not in existing_skip:
                        new_skips.append(d["sn"])
                        self.logger.info(f"  Adding OS drive SN to skip_drives: {d['sn']}")
                    else:
                        self.logger.info(f"  OS drive SN {d['sn']} already in skip_drives.")
                    break
            else:
                self.logger.warning(f"  Could not find {os_device} in nvme list output.")
        else:
            self.logger.info("  OS drive is not NVMe. No skip_drives update needed.")

        if new_skips:
            config["skip_drives"] = existing_skip + new_skips

        # Step 7: Write updated config.json if changes were made
        if not new_models and not new_skips:
            self.logger.info("  No config changes needed.")
            return True

        self.logger.info(f"  Writing updated {self.CONFIG_FILE}...")
        updated_content = json.dumps(config, indent=4)

        try:
            sftp = self.ssh.client.open_sftp()
            with sftp.file("/tmp/config.json.new", "w") as f:
                f.write(updated_content + "\n")
            sftp.close()
        except Exception as e:
            self.logger.error(f"  SFTP write failed: {e}")
            return False

        exit_code, _, err = self.ssh.run_command(
            f"mv /tmp/config.json.new {self.CONFIG_FILE}", use_sudo=True
        )
        if exit_code != 0:
            self.logger.error(f"  Failed to move config.json: {err}")
            return False

        # Step 8: Verify
        exit_code, verify_content, _ = self.ssh.run_command(
            f"cat {self.CONFIG_FILE}", use_sudo=True
        )
        try:
            verify_config = json.loads(verify_content)
            final_models = verify_config.get("bryck_drive_model", [])
            final_skips = verify_config.get("skip_drives", [])
            self.logger.info(f"  Final bryck_drive_model ({len(final_models)} entries): {final_models}")
            self.logger.info(f"  Final skip_drives ({len(final_skips)} entries): {final_skips}")
        except json.JSONDecodeError:
            self.logger.warning("  Could not re-parse config for verification.")

        self.logger.info("  NVMe drive configuration updated successfully.")
        return True


# ---------------------------------------------------------------------------
# Task Runner
# ---------------------------------------------------------------------------

# Register all tasks here in execution order.
TASK_REGISTRY: list[type[SetupTask]] = [
    ConfigureDNS,
    CreateUsers,
    ConfigureSudoers,       # grant bryck NOPASSWD sudo while still privileged (ubuntu)
    ReconnectAsBryckUser,   # only switch to bryck AFTER it has sudo
    ConfigureAPTSources,
    InstallKernel,
    SetHostname,
    DisableGRUBPassword,
    RebootAndInstallPostKernel,
    ConfigureAPTSandbox,
    InstallPackagesAndSDKs,
    FlushIPTables,
    ConfigureNetworkManagerAndSSL,
    ConfigureNetworkManagerInterfaces,   # NM config — independent of build
    InstallCloudCLIs,                    # removes Google/Azure CLIs before deploy
    DeployBryckBuild,
    FixConfigJsonPermissions,
    ConfigureNFSExport,
    PostDeployVenvPackages,              # bryck venv + desktop — requires build
    DownloadBryckCLIAndNFSDPatch,
    ApplyNFSDPatchAndRemoveSamba,
    InstallBryckCLI,
    ConfigureNVMeDrives,
    FixConfigJsonPermissions,   # Must run last — ConfigureNVMeDrives mv overwrites permissions
    # Add future tasks below:
    # ...
]


# ---------------------------------------------------------------------------
# Device Detection
# ---------------------------------------------------------------------------

def detect_device_type(ssh: SSHConnection, logger: logging.Logger) -> BryckType | None:
    """
    Auto-detect device type by reading architecture from hostnamectl.

    Detection logic:
        arm64  / aarch64  -> bryckmini  (BlueField-3 DPU, Nvidia)
        x86-64 / x86_64   -> bryckserver (Supermicro server)
    """
    logger.info("Detecting device type via 'hostnamectl'...")
    exit_code, output, err = ssh.run_command("hostnamectl")

    if exit_code != 0:
        logger.error(f"hostnamectl failed: {err}")
        return None

    logger.debug(f"hostnamectl output:\n{output}")

    # Parse architecture line
    arch = None
    for line in output.splitlines():
        if "Architecture" in line:
            arch = line.split(":", 1)[1].strip()
            break

    if not arch:
        logger.error("Could not find 'Architecture' in hostnamectl output.")
        return None

    logger.info(f"Detected architecture: {arch}")

    device_type = ARCH_TO_TYPE.get(arch)
    if device_type is None:
        logger.error(f"Unknown architecture '{arch}'. Expected one of: {list(ARCH_TO_TYPE.keys())}")
        return None

    logger.info(f"Identified as: {device_type.value}")
    return device_type


def run_all_tasks(config: DeviceConfig) -> bool:
    """
    Connect to the device and run all registered setup tasks.
    
    Returns True if all tasks succeeded.
    """
    logger = setup_logger(config.ip)
    logger.info("=" * 60)
    logger.info(f"Bryck Setup - Target: {config.ip}")
    logger.info("=" * 60)

    ssh = SSHConnection(config, logger)

    try:
        ssh.connect()
    except Exception:
        logger.error("Aborting: Could not establish SSH connection.")
        return False

    # --- Auto-detect device type from architecture ---
    if config.bryck_type is None:
        config.bryck_type = detect_device_type(ssh, logger)
        if config.bryck_type is None:
            logger.error("Aborting: Could not detect device type.")
            ssh.disconnect()
            return False

    logger.info(f"Device type: {config.bryck_type.value}")

    results: dict[str, bool] = {}

    try:
        for task_cls in TASK_REGISTRY:
            task = task_cls(ssh, logger)
            logger.info("-" * 40)

            # A previous task may have left the transport dead; heal it before
            # this task so one bad command can't cascade into every later task.
            if not ssh.is_active():
                logger.warning("  SSH session is not active; attempting to reconnect...")
                if not ssh.reconnect():
                    logger.error(f"  Skipping '{task.name}': SSH reconnect failed.")
                    results[task.name] = False
                    continue

            try:
                success = task.run()
            except Exception as e:
                logger.error(f"  Task '{task.name}' raised an exception: {e}")
                success = False
            results[task.name] = success

            # Abort early on foundational failures: without a bryck user that
            # has working sudo, every remaining task would just fail with
            # "bryck is not in the sudoers file". Continuing wastes time and
            # buries the real error under dozens of cascading failures.
            if task.critical and not success:
                logger.error(
                    f"  Critical task '{task.name}' failed. Aborting run — "
                    "remaining tasks depend on it."
                )
                break
    finally:
        ssh.disconnect()

    # Summary

    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    all_passed = True
    for task_name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        logger.info(f"  [{status}] {task_name}")
        if not passed:
            all_passed = False

    if all_passed:
        logger.info("All tasks completed successfully.")
    else:
        logger.error("Some tasks failed. Check the log for details.")

    return all_passed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bryck SDK - Remote device setup via SSH (auto-detects device type)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Device type is auto-detected from architecture:
  arm64   -> bryckmini  (BlueField-3 DPU)
  x86-64  -> bryckserver (Supermicro server)

Examples:
  python3 bryck_setup.py 192.168.1.100
  python3 bryck_setup.py 10.0.0.5 --username admin --password 'BryckAdm1n'
        """,
    )
    parser.add_argument("ip", help="IP address of the target Bryck device")
    parser.add_argument("--username", "-u", default="bryck", help="SSH username (default: bryck)")
    parser.add_argument("--password", "-p", default="while(1);", help="SSH password (default: while(1);)")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument(
        "--bryck-build", "-b",
        default=None,
        help="Bryck build name, e.g. 'tsecond-bryck-5.0.0.15' (without .tar.gz)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print all commands that would be executed without connecting to the device.",
    )
    parser.add_argument(
        "--skip-reboot",
        action="store_true",
        default=False,
        help="Skip the reboot step (post-kernel packages are still installed on the running kernel).",
    )
    parser.add_argument(
        "--device-type",
        choices=["bryckmini", "bryckserver"],
        default=None,
        help="Device type for dry-run (required with --dry-run since no SSH detection occurs).",
    )
    return parser.parse_args()


def dry_run_report(config: DeviceConfig) -> None:
    """
    Print a full report of all commands that would be executed on the target
    device, organized by task. No SSH connection is made.
    """
    device = config.bryck_type.value
    build = config.bryck_build or "<not specified>"

    # Determine architecture-dependent values
    if config.bryck_type == BryckType.BRYCKMINI:
        inventory_type = "mini"
        arch_suffix = "arm64"
        build_server_ip = "192.168.6.193"
    else:
        inventory_type = "bryck"
        arch_suffix = "amd64"
        build_server_ip = "192.168.6.28"

    tarball = f"{build}-{arch_suffix}.tar.gz" if config.bryck_build else "<build>.tar.gz"
    deploy_dir = f"/home/bryck/{build}" if config.bryck_build else "/home/bryck/<build>"

    print("=" * 70)
    print(f"  BRYCK SETUP - DRY RUN REPORT")
    print(f"  Target: {config.ip}:{config.ssh_port}")
    print(f"  User: {config.username}")
    print(f"  Device Type: {device}")
    print(f"  Build: {build}")
    print("=" * 70)
    print()

    task_num = 0

    # --- Task 1: Configure DNS ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Configure DNS (/etc/resolv.conf)")
    print(f"{'─'*70}")
    print(f"  [CHECK]  cat /etc/resolv.conf")
    print(f"  [SUDO]   unlink /etc/resolv.conf")
    print(f"  [SUDO]   bash -c 'echo \"nameserver 8.8.8.8\" > /etc/resolv.conf'")
    print(f"  [VERIFY] cat /etc/resolv.conf")
    print()

    # --- Task 2: Create Users ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Create Users (bryck & admin)")
    print(f"{'─'*70}")
    print(f"  [CHECK]  id bryck")
    print(f"  [SUDO]   useradd -m -s /bin/bash bryck")
    print(f"  [SUDO]   bash -c 'echo \"bryck:while(1);\" | chpasswd'")
    print(f"  [SUDO]   usermod -aG sudo bryck")
    print(f"  [SUDO]   groupdel admin")
    print(f"  [CHECK]  id admin")
    print(f"  [SUDO]   useradd -m -s /bin/bash admin")
    print(f"  [SUDO]   bash -c 'echo \"admin:BryckAdm1n\" | chpasswd'")
    print()

    # --- Task 3: Configure Sudoers (runs as bootstrap user, before switching to bryck) ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Configure Sudoers (/etc/sudoers)")
    print(f"{'─'*70}")
    print(f"  [SUDO]   cp /etc/sudoers /etc/sudoers.bak")
    print(f"  [SUDO]   cat /etc/sudoers")
    print(f"  [SUDO]   bash -c 'echo -e \"<sudoers entries>\" >> /etc/sudoers'")
    print(f"           Appends: Cmnd_Alias MORE, Defaults!MORE, bryck/wsgi NOPASSWD, admin ALL")
    print(f"  [SUDO]   visudo -c")
    print()

    # --- Task 4: Reconnect as bryck ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Reconnect as bryck User")
    print(f"{'─'*70}")
    print(f"  [RUN]    whoami  (check current user)")
    print(f"  [SKIP]   if already 'bryck', skips reconnect")
    print(f"  [RUN]    id bryck  (verify user exists)")
    print(f"  [ACTION] Close SSH session; reconnect as bryck / while(1);")
    print(f"  [VERIFY] whoami  (confirm new session is bryck)")
    print()

    # --- Task 5: Configure APT Sources ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Configure APT Sources (/etc/apt/sources.list)")
    print(f"{'─'*70}")
    print(f"  [CHECK]  cat /etc/apt/sources.list")
    print(f"  [SUDO]   mv /etc/apt/sources.list /home/bryck/bkp_sources.list")
    print(f"  [SFTP]   Write /tmp/sources.list.new (tsecond mirror for {'arm64' if config.bryck_type == BryckType.BRYCKMINI else 'amd64'})")
    print(f"  [SUDO]   mv /tmp/sources.list.new /etc/apt/sources.list")
    print(f"  [SUDO]   apt update")
    print()

    # --- Task 5: Install Kernel (bryckserver only) ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Install Kernel 6.5.0-45 & Configure GRUB")
    print(f"{'─'*70}")
    if config.bryck_type == BryckType.BRYCKMINI:
        print(f"  [SKIP]   Not applicable to bryckmini (uses bluefield kernel)")
    else:
        print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt install -y \\")
        print(f"             linux-image-6.5.0-45-generic linux-headers-6.5.0-45-generic")
        print(f"             linux-modules-6.5.0-45-generic linux-modules-extra-6.5.0-45-generic")
        print(f"             sshpass vim")
        print(f"  [READ]   awk -F\\' '/menuentry/' /boot/grub/grub.cfg")
        print(f"  [SUDO]   sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=\"1>...\"/'' /etc/default/grub")
        print(f"  [SUDO]   update-grub")
    print()

    # --- Task 6: Set Hostname ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Set Hostname")
    print(f"{'─'*70}")
    print(f"  [SUDO]   hostnamectl set-hostname {device}")
    print(f"  [SUDO]   bash -c 'echo \"127.0.1.1 {device}\" >> /etc/hosts'")
    print()

    # --- Task 7: Disable GRUB Password ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Disable GRUB Password (/etc/grub.d/40_custom)")
    print(f"{'─'*70}")
    if config.bryck_type == BryckType.BRYCKMINI:
        print(f"  [SKIP]   Not applicable to bryckmini")
    else:
        print(f"  [SUDO]   sed -i 's/^set superusers/#set superusers/' /etc/grub.d/40_custom")
        print(f"  [SUDO]   sed -i 's/^password_pbkdf2/#password_pbkdf2/' /etc/grub.d/40_custom")
        print(f"  [SUDO]   update-grub")
    print()

    # --- Task 8: Reboot & Post-Kernel ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Reboot & Install Post-Kernel Packages")
    print(f"{'─'*70}")
    if config.bryck_type == BryckType.BRYCKSERVER:
        print(f"  [SUDO]   nohup bash -c 'sleep 2 && reboot' &")
        print(f"  [WAIT]   Wait up to 300s for SSH to come back")
    else:
        print(f"  [SKIP]   No reboot needed (bryckmini)")
    print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt-get install -y linux-modules-extra-$(uname -r)")
    print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt install -y linux-headers-$(uname -r)")
    print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt-get install -y network-manager")
    print()

    # --- Task 9: Configure APT Sandbox ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Configure APT Sandbox")
    print(f"{'─'*70}")
    print(f"  [SUDO]   bash -c 'echo \'APT::Sandbox::User \"root\";\'  > /etc/apt/apt.conf.d/10sandbox'")
    print()

    # --- Task 10: Install Packages & SDKs ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Install Packages & Cloud SDKs")
    print(f"{'─'*70}")
    print(f"  [SUDO]   apt-get update")
    print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt --fix-broken install -y")
    print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt install -y \\")
    print(f"             python3 python3-pip vsftpd sysbench net-tools ethtool")
    print(f"             cryptsetup fio unzip pkg-config libsystemd-dev krb5-user")
    print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt install -y \\")
    print(f"             rclone curl jq sysbench")
    print(f"  [SUDO]   pip3 install ansible")
    print(f"  [SUDO]   pip3 install pyroute2")
    print(f"  [SUDO]   pip3 install netifaces")
    print(f"  [SUDO]   chmod +x /etc/rc.local")
    print(f"  [SUDO]   curl + gpg -> /usr/share/keyrings/cloud.google.gpg")
    print(f"  [SUDO]   Add Google Cloud SDK apt source")
    print(f"  [SUDO]   apt-get update && apt-get install -y google-cloud-cli")
    print(f"  [SUDO]   pip3 install --no-cache-dir -U crcmod")
    print(f"  [SUDO]   curl + gpg -> /usr/share/keyrings/microsoft.gpg")
    print(f"  [SUDO]   Add Azure CLI apt source")
    print(f"  [SUDO]   apt-get update && apt-get install -y azure-cli")
    print()

    # --- Task 11: Flush IPTables ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Flush IPTables (Network Setup)")
    print(f"{'─'*70}")
    print(f"  [SUDO]   rm -f /etc/iptables/rules.*")
    print(f"  [SUDO]   iptables -w 5 -P INPUT ACCEPT")
    print(f"  [SUDO]   iptables -w 5 -P FORWARD ACCEPT")
    print(f"  [SUDO]   iptables -w 5 -P OUTPUT ACCEPT")
    print(f"  [SUDO]   iptables -w 5 -t nat -F")
    print(f"  [SUDO]   iptables -w 5 -t mangle -F")
    print(f"  [SUDO]   iptables -w 5 -F")
    print(f"  [SUDO]   iptables -w 5 -X")
    print(f"  [SUDO]   ip6tables -w 5  (same as above for IPv6)")
    print(f"  [SUDO]   mkdir -p /etc/iptables")
    print(f"  [SUDO]   iptables-save > /etc/iptables/rules.v4")
    print()

    # --- Task 12: Configure NetworkManager, SSL & SSH Keys ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Configure NetworkManager, SSL & SSH Keys")
    print(f"{'─'*70}")
    print(f"  [SFTP]   Write /tmp/10-globally-managed-devices.conf:")
    print(f"             [keyfile]")
    print(f"             unmanaged-devices=*,except:type:wifi,except:type:gsm,except:type:cdma,except:type:ethernet")
    print(f"  [SUDO]   mv /tmp/10-globally-managed-devices.conf /usr/lib/NetworkManager/conf.d/10-globally-managed-devices.conf")
    print(f"  [SUDO]   openssl req -x509 -newkey ec ... -out /etc/ssl/certs/bryckweb-selfsigned.crt")
    print(f"  [SUDO]   mkdir -p /home/bryck/.ssh && chown/chmod")
    print(f"  [SUDO]   ssh-keygen -t rsa -b 4096 -N '' -f /home/bryck/.ssh/id_rsa")
    print(f"  [SUDO]   chown bryck:bryck /home/bryck/.ssh/id_rsa /home/bryck/.ssh/id_rsa.pub")
    print(f"  [SUDO]   su - bryck -c 'sshpass -p ... ssh-copy-id bryck@localhost'")
    print()

    # --- Configure NetworkManager Interfaces (build-independent) ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Configure NetworkManager Interfaces")
    print(f"{'─'*70}")
    print(f"  [SUDO]   nmcli device set oob_net0 managed yes")
    print(f"  [SUDO]   nmcli connection modify oob_net0 autoconnect yes")
    print(f"  [SUDO]   nmcli device connect / reapply oob_net0")
    print(f"  [SFTP]   Write /etc/NetworkManager/conf.d/40-mlnx.conf (disable Mellanox keyfile)")
    print(f"  [SUDO]   Configure NetworkManager.conf (dns=none, ifupdown managed=true)")
    print(f"  [SUDO]   systemctl reload/restart NetworkManager; disable systemd-networkd")
    print(f"  [SUDO]   apt purge netplan netplan.io")
    print(f"  [SUDO]   nmcli: rename netplan-oob_net0 -> oob_net0; add p0/p1 connections")
    print(f"  [SFTP]   Write /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg")
    print(f"  [SUDO]   Disable dns/route1 in tmfifo_net0.nmconnection")
    print()

    # --- Task: Deploy Bryck Build ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Deploy Bryck Build")
    print(f"{'─'*70}")
    if not config.bryck_build:
        print(f"  [SKIP]   No --bryck-build specified")
    else:
        print(f"  [SUDO]   systemctl stop 'bryck*'")
        print(f"  [SUDO]   systemctl stop bryckweb bryckutil bryckmonitor")
        print(f"  [SUDO]   su - bryck -c 'cd <existing_dir> && python3 bryckdeploy uninstall -v'")
        print(f"  [SUDO]   rm -rf /opt/bryck  (if uninstall fails)")
        print(f"  [SUDO]   sshpass -p '...' scp {build_server_ip}:/home/bryck/builds/{tarball} /home/bryck/{tarball}")
        print(f"  [SUDO]   wget -q -O /home/bryck/inventory http://repos.tsecond.ai/ubuntu/inventory")
        print(f"  [SUDO]   sed -i 's/^bryck_type=.*/bryck_type={inventory_type}/' /home/bryck/inventory")
        print(f"  [SUDO]   tar -xzf /home/bryck/{tarball} -C /home/bryck/")
        print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades libjq1=... jq=...")
        print(f"  [BG]     su - bryck -c 'cd {deploy_dir} && python3 bryckdeploy install -v'")
        print(f"  [WAIT]   Monitor /opt/ansible/ansible.log for up to 40 minutes")
        print(f"  [SUDO]   sed -i ... /etc/bryck/bryckutil/config.json  (enable_hot_plug=False, bryck_type={inventory_type})")
        print(f"  [SUDO]   echo 'export HAILO_MONITOR=1' >> /etc/bash.bashrc")
    print()

    # --- Task 14: Fix config.json Permissions ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Fix config.json Permissions (bryck:bryck 755)")
    print(f"{'─'*70}")
    print(f"  [CHECK]  test -f /etc/bryck/bryckutil/config.json")
    print(f"  [SKIP]   if file not found (deploy not yet run)")
    print(f"  [SUDO]   stat -c '%U %G %a' /etc/bryck/bryckutil/config.json")
    print(f"  [SKIP]   if already bryck:bryck 755")
    print(f"  [SUDO]   chown bryck:bryck /etc/bryck/bryckutil/config.json")
    print(f"  [SUDO]   chmod 755 /etc/bryck/bryckutil/config.json")
    print(f"  [VERIFY] stat -c '%U %G %a' /etc/bryck/bryckutil/config.json")
    print()

    # --- Task: Configure NFS Export ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Configure NFS Export (anonuid/anongid)")
    print(f"{'─'*70}")
    print(f"  [CHECK]  test -f /opt/ansible/roles/add-export/vars/main.yml")
    print(f"  [RUN]    id -u bryck  (detect UID)")
    print(f"  [RUN]    id -g bryck  (detect GID)")
    print(f"  [SUDO]   cat /opt/ansible/roles/add-export/vars/main.yml")
    print(f"  [SUDO]   sed -i 's/anonuid=[0-9]*/anonuid=<uid>/g' ...")
    print(f"  [SUDO]   sed -i 's/anongid=[0-9]*/anongid=<gid>/g' ...")
    print(f"  [VERIFY] cat /opt/ansible/roles/add-export/vars/main.yml")
    print()

    # --- Task: Post-Deploy Venv Packages & Desktop ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Post-Deploy Venv Packages & Desktop")
    print(f"{'─'*70}")
    print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt install -y xfce4 xfce4-goodies")
    print(f"  [SUDO]   DEBIAN_FRONTEND=noninteractive apt install -y xrdp")
    print(f"  [SUDO]   systemctl disable netfilter-persistent openibd")
    print(f"  [SUDO]   /opt/bryck/.venv/bryck/bin/pip3 install pyroute2==0.7.12")
    print(f"  [SUDO]   /opt/bryck/.venv/bryck/bin/pip3 install netifaces")
    print()

    # --- Task 16: Download BryckCLI & NFSD Patch ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Download BryckCLI & NFSD Patch")
    print(f"{'─'*70}")
    print(f"  [CHECK]  test -f /home/bryck/bryckcli.tar.gz")
    print(f"  [SUDO]   wget -q -O /home/bryck/bryckcli.tar.gz http://repos.tsecond.ai/ubuntu/bryckcli.tar.gz")
    print(f"  [CHECK]  test -f /home/bryck/nfsd_patch.tar.gz")
    print(f"  [SUDO]   wget -q -O /home/bryck/nfsd_patch.tar.gz http://repos.tsecond.ai/ubuntu/nfsd_patch.tar.gz")
    print(f"  [SUDO]   chown bryck:bryck /home/bryck/bryckcli.tar.gz /home/bryck/nfsd_patch.tar.gz")
    print()

    # --- Task 17: Apply NFSD Patch & Remove Samba ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Apply NFSD Patch & Remove Samba")
    print(f"{'─'*70}")
    print(f"  [SUDO]   systemctl stop nfs-server")
    print(f"  [SUDO]   systemctl stop nfs-kernel-server")
    print(f"  [SUDO]   tar -xzf /home/bryck/nfsd_patch.tar.gz -C /home/bryck/")
    print(f"  [SUDO]   bash /home/bryck/nfsd_patch/replace_nfsd_module.sh")
    print(f"  [SUDO]   systemctl stop smbd nmbd")
    print(f"  [SUDO]   systemctl disable smbd nmbd")
    print(f"  [SUDO]   rm -f /lib/systemd/system/smbd.service /lib/systemd/system/nmbd.service")
    print(f"  [SUDO]   systemctl daemon-reload")
    print()

    # --- Task 18: Install BryckCLI ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Install BryckCLI")
    print(f"{'─'*70}")
    print(f"  [CHECK]  which bryckcli")
    print(f"  [CHECK]  test -f /home/bryck/bryckcli.tar.gz")
    print(f"  [SUDO]   tar -xzf /home/bryck/bryckcli.tar.gz -C /home/bryck/")
    print(f"  [SUDO]   bash /home/bryck/bryckcli/deploy_bryckcli install")
    print(f"  [VERIFY] Expected: 'Bryckcli is Installed. Use the command \"bryckcli\" to manage bryck'")
    print()

    # --- Task 19: Configure NVMe Drives ---
    task_num += 1
    print(f"{'─'*70}")
    print(f"  TASK {task_num}: Configure NVMe Drives (config.json)")
    print(f"{'─'*70}")
    print(f"  [CHECK]  test -f /etc/bryck/bryckutil/config.json")
    print(f"  [SUDO]   nvme list")
    print(f"  [RUN]    df / (identify OS drive)")
    print(f"  [READ]   cat /etc/bryck/bryckutil/config.json")
    print(f"  [UPDATE] Append new NVMe models to bryck_drive_model[]")
    print(f"  [UPDATE] Append OS drive serial to skip_drives[]")
    print(f"  [SFTP]   Write updated config.json")
    print()

    print("=" * 70)
    print(f"  END OF DRY RUN - {task_num} tasks, no commands were executed.")
    print("=" * 70)


def main() -> None:
    args = parse_args()

    # Force UTF-8 stdout/stderr so box-drawing characters in the dry-run
    # report (and any unicode in logs) don't crash on Windows consoles that
    # default to cp1252.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass

    # Handle dry-run mode
    if args.dry_run:
        if not args.device_type:
            print("ERROR: --device-type is required with --dry-run (no SSH to auto-detect).")
            print("       Use: --device-type bryckmini  OR  --device-type bryckserver")
            sys.exit(1)
        config = DeviceConfig(
            ip=args.ip,
            username=args.username,
            password=args.password,
            bryck_type=BryckType(args.device_type),
            ssh_port=args.port,
            bryck_build=args.bryck_build,
            skip_reboot=args.skip_reboot,
        )
        dry_run_report(config)
        sys.exit(0)

    config = DeviceConfig(
        ip=args.ip,
        username=args.username,
        password=args.password,
        bryck_type=None,  # Auto-detected after SSH connection
        ssh_port=args.port,
        bryck_build=args.bryck_build,
        skip_reboot=args.skip_reboot,
    )

    success = run_all_tasks(config)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
