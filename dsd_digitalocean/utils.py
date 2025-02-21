"""Utilities specific to Digital Ocean."""

import os
import time
from pathlib import Path

import paramiko

from django_simple_deploy.management.commands.utils import plugin_utils
from django_simple_deploy.management.commands.utils.plugin_utils import dsd_config


def run_server_cmd_ssh(cmd, timeout=10, show_output=True, skip_logging=None):
    """Run a command on the server, through an SSH connection.

    Returns:
        Tuple of Str: (stdout, stderr)
    """
    # If skip_logging is not explicitly set, set it to False.
    # This matches the default in plugin_utils.write_output().
    if skip_logging is None:
        skip_logging = False

    plugin_utils.write_output("Running server command over SSH...", skip_logging=skip_logging)
    if show_output:
        plugin_utils.write_output(f"  command: {cmd}", skip_logging=skip_logging)
    else:
        plugin_utils.write_output("  (command not shown)", skip_logging=skip_logging)

    # Get client.
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # Run command, and close connection.
    try:
        client.connect(
            hostname = os.environ.get("DSD_HOST_IPADDR"),
            username = dsd_config.server_username,
            password = os.environ.get("DSD_HOST_PW"),
            timeout = timeout
        )
        _stdin, _stdout, _stderr = client.exec_command(cmd)

        stdout = _stdout.read().decode().strip()
        stderr = _stderr.read().decode().strip()
    finally:
        client.close()

    # Show stdout and stderr, unless suppressed.
    if stdout and show_output:
        plugin_utils.write_output(stdout, skip_logging=skip_logging)
    if stderr and show_output:
        plugin_utils.write_output(stderr, skip_logging=skip_logging)

    # Return both stdout and stderr.
    return stdout, stderr

def configure_firewall():
    """Configure the ufw firewall."""
    plugin_utils.write_output("Configuring firewall...")
    cmd = "sudo ufw allow OpenSSH"
    run_server_cmd_ssh(cmd)

    cmd = "sudo ufw --force enable"
    run_server_cmd_ssh(cmd)

def set_server_username():
    """Sets dsd_config.server_username, for logging into the server.

    The username will either be set through an env var, or django_user from
    an earlier run, or root if no non-root user has been added yet, ie for
    a fresh VM. If it's root, we'll add a new user.

    - DO_DJANGO_USER gets priority if it's set.
    - Then, try django_user.
    - If django_user is not available, use root to add django_user.

    Returns:
        None
    Sets:
        dsd_config.server_username
    Raises:
        DSDCommandError: If unable to connect to server and establish a username.
    """
    plugin_utils.write_output("Determining server username...")

    if (username := os.environ.get("DO_DJANGO_USER")):
        # Use this custom username.
        dsd_config.server_username = username
        plugin_utils.write_output(f"  username: {username}")
        return

    # No custom username. Try to connect with default username.
    dsd_config.server_username = "django_user"
    try:
        run_server_cmd_ssh("uptime")
    except paramiko.ssh_exception.AuthenticationException:
        # Default non-root user doesn't exist.
        dsd_config.server_username = "root"
        plugin_utils.write_output("  Using root for now...")
    else:
        # Default username works, we're done here.
        plugin_utils.write_output(f"  username: {dsd_config.server_username}")
        return

    add_server_user()
    plugin_utils.write_output(f"  username: {dsd_config.server_username}")


def reboot_if_required():
    """Reboot the server if required.

    Returns:
        bool: True if rebooted, False if not rebooted.
    """
    plugin_utils.write_output("Checking if reboot required...")

    cmd = "ls /var/run"
    stdout, stderr = run_server_cmd_ssh(cmd, show_output=False)

    if "reboot-required" in stdout:
        reboot_server()
        return True
    else:
        plugin_utils.write_output("  No reboot required.")
        return False

def reboot_server():
    """Reboot the server, and wait for it to be available again.

    Returns:
        None
    Raises:
        DSDCommandError: If the server is unavailable after rebooting.
    """
    plugin_utils.write_output("  Rebooting...")
    cmd = "sudo systemctl reboot"
    stdout, stderr = run_server_cmd_ssh(cmd)

    # Pause to let shutdown begin; polling too soon shows server available because
    # shutdown hasn't started yet.
    time.sleep(5)

    # Poll for availability.
    if not check_server_available():
        raise DSDCommandError("Cannot reach server after reboot.")


def check_server_available(delay=10, timeout=300):
    """Check if the server is responding.

    Returns:
        bool
    """
    plugin_utils.write_output("Checking if server is responding...")

    max_attempts = int(timeout / delay)
    for attempt in range(max_attempts):
        try:
            stdout, stderr = run_server_cmd_ssh("uptime")
            plugin_utils.write_output("  Server is available.")
            return True
        except TimeoutError:
            plugin_utils.write_output(f"  Attempt {attempt+1}/{max_attempts} failed.")
            plugin_utils.write_output(f"    Waiting {delay}s for server to become available.")
            time.sleep(delay)

    plugin_utils.write_output("Server did not respond.")
    return False

def add_server_user():
    """Add a non-root user.
    Returns:
        None
    Raises:
        DSDCommandError: If unable to connect using new user.
    """
    # # Leave if there's already a non-root user.
    # username = os.environ.get("DSD_HOST_USERNAME")
    # if (username != "root") or dsd_config.unit_testing:
    #     return

    # Add the new user.
    django_username = "django_user"
    plugin_utils.write_output(f"Adding non-root user: {django_username}")
    cmd = f"useradd -m {django_username}"
    run_server_cmd_ssh(cmd)

    # Set the password.
    plugin_utils.write_output("  Setting password; will not display or log this.")
    password = os.environ.get("DSD_HOST_PW")
    cmd = f'echo "{django_username}:{password}" | chpasswd'
    run_server_cmd_ssh(cmd, show_output=False, skip_logging=True)

    # Add user to sudo group.
    plugin_utils.write_output("  Adding user to sudo group.")
    cmd = f"usermod -aG sudo {django_username}"
    run_server_cmd_ssh(cmd)

    # Modify /etc/sudoers.d to allow scripted use of sudo commands.
    plugin_utils.write_output("  Modifying /etc/sudoers.d.")
    cmd = f'echo "{django_username} ALL=(ALL) NOPASSWD:SETENV: /usr/bin/apt-get, NOPASSWD: /usr/bin/apt-get, /usr/bin/systemctl reboot, /usr/sbin/ufw" | sudo tee /etc/sudoers.d/{django_username}'
    run_server_cmd_ssh(cmd)

    # Use the new user from this point forward.
    dsd_config.server_username = django_username

    # Verify connection.
    plugin_utils.write_output("  Ensuring connection...")
    if not check_server_available():
        msg = "Could not connect with new user."
        raise DSDCommandError(msg)

def install_uv():
    """Install uv on the server."""
    plugin_utils.write_output("Installing uv...")
    cmd = "curl -LsSf https://astral.sh/uv/install.sh | sh"
    run_server_cmd_ssh(cmd)

def install_python():
    """Install Python on the server."""
    plugin_utils.write_output("Installing Python...")
    cmd = f"/home/{dsd_config.server_username}/.local/bin/uv python install 3.12"
    run_server_cmd_ssh(cmd)

def configure_git(templates_path):
    """Configure Git for pushing project to server."""

    # --- Server configuration ---
    plugin_utils.write_output("Initializing Git project on server...")

    template_path = templates_path / "post-receive"
    project_path = Path(f"/home/{dsd_config.server_username}/{dsd_config.local_project_name}")

    # Make a project directory.
    cmd = f"mkdir -p {project_path}"
    run_server_cmd_ssh(cmd)

    cmd = f"chown -R {dsd_config.server_username}:{dsd_config.server_username} {project_path}"
    run_server_cmd_ssh(cmd)

    # Make a bare git repository.
    cmd = f"git init --bare /home/{dsd_config.server_username}/{dsd_config.local_project_name}.git"
    run_server_cmd_ssh(cmd)

    # Set default branch to main.
    plugin_utils.write_output("  Setting default branch to main.")
    cmd = "git config --global init.defaultBranch main"
    run_server_cmd_ssh(cmd)

    # Write post-receive hook.
    context = {
        "project_path": project_path.as_posix(),
    }
    post_receive_string = plugin_utils.get_template_string(template_path, context)

    post_receive_path = Path(f"{project_path}.git") / "hooks" / "post-receive"
    cmd = f'echo "{post_receive_string}" > {post_receive_path.as_posix()}'
    run_server_cmd_ssh(cmd)

    # Make hook executable.
    plugin_utils.write_output("  Making hook executable...")
    cmd = f"chmod +x {post_receive_path.as_posix()} "
    run_server_cmd_ssh(cmd)

    # --- Local configuration ---

    plugin_utils.write_output("  Adding remote to local Git project.")
    cmd = f"git remote add do_server '{dsd_config.server_username}@{os.environ.get("DSD_HOST_IPADDR")}:{dsd_config.local_project_name}.git'"
    plugin_utils.run_quick_command(cmd)


def push_project():
    """Push the project to the server."""
    plugin_utils.write_output("  Pushing project code to server.")
    cmd = f"git push do_server main"
    breakpoint()
    plugin_utils.run_quick_command(cmd)
