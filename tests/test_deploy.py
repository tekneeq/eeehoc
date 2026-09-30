"""Deploy scripts and nginx front door match the eeefut pattern, on :8083."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_workflow_sshes_and_runs_deploy_sh():
    text = (ROOT / ".github/workflows/deploy-ec2.yml").read_text()
    assert "branches: [main]" in text
    assert "secrets.EC2_HOST" in text
    assert "secrets.EC2_USER" in text
    assert "secrets.EC2_SSH_PRIVATE_KEY" in text
    assert "secrets.EC2_APP_DIR" in text
    assert "git pull --ff-only origin main" in text
    assert "./deploy.sh" in text
    assert "eeehoc" in text


def test_deploy_sh_healthchecks_8083():
    text = (ROOT / "deploy.sh").read_text()
    assert "./restart.sh" in text
    assert "eeehoc-dashboard" in text
    assert "http://127.0.0.1:8083/health" in text
    assert "8082" not in text


def test_restart_sh_recreates_container_on_8083():
    text = (ROOT / "restart.sh").read_text()
    assert "ensure_docker" in text
    assert "docker_cmd build" in text
    assert "docker_cmd rm -f eeehoc-dashboard" in text
    assert "-p 8083:8083" in text
    assert "--restart unless-stopped" in text
    assert "8082" not in text


def test_install_docker_script_covers_amazon_linux():
    text = (ROOT / "scripts/install-docker-amazon-linux.sh").read_text()
    assert "dnf install -y docker" in text
    assert "yum install -y docker" in text
    assert "systemctl enable --now docker" in text
    assert "usermod -aG docker" in text


def test_nginx_routes_port_80_to_dashboard():
    text = (ROOT / "scripts/nginx-eeehoc-dashboard.conf").read_text()
    assert "listen 80 default_server" in text
    assert "server 127.0.0.1:8083" in text
    assert "server_name eeehoc.com www.eeehoc.com _;" in text
    assert "acme-challenge" in text
    assert "proxy_pass http://eeehoc_dashboard" in text
    assert "8082" not in text
    assert "/eeesoc/" not in text
    assert "/eeefut/" not in text


def test_nginx_https_server_name_and_443():
    text = (ROOT / "scripts/nginx-eeehoc-https.conf").read_text()
    assert "listen 443 ssl" in text
    assert "server_name eeehoc.com www.eeehoc.com;" in text
    assert "ssl_certificate" in text
    assert "return 301 https://eeehoc.com" in text
    assert "127.0.0.1:8083" in text


def test_install_nginx_script_starts_inactive_unit():
    text = (ROOT / "scripts/install-nginx-80.sh").read_text()
    assert "systemctl start nginx" in text
    assert "disable_nginx_default_80.py" in text
    assert "8083" in text


def test_disable_amazon_linux_padded_listen_80():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from disable_nginx_default_80 import disable_default_80

    al_conf = """
http {
    include /etc/nginx/conf.d/*.conf;

    server {
        listen       80;
        listen       [::]:80;
        server_name  _;
        root         /usr/share/nginx/html;
    }
}
"""
    patched, n = disable_default_80(al_conf)
    assert n == 1
    assert "eeehoc: default :80 server disabled" in patched
    assert "#         listen       80;" in patched
    live = "\n".join(line for line in patched.splitlines() if not line.lstrip().startswith("#"))
    assert "listen" not in live

    again, n2 = disable_default_80(patched)
    assert n2 == 0
