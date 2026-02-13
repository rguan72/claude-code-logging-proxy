#!/usr/bin/env bash
set -euo pipefail

REGION="us-east-2"
INSTANCE_TYPE="t3.medium"
ROLE_NAME="claude-proxy-ec2-role"
PROFILE_NAME="claude-proxy-ec2-profile"
POLICY_NAME="claude-proxy-s3-policy"
SG_NAME="claude-proxy-sg"
KEY_NAME="claude-proxy-key"
KEY_FILE="$(cd "$(dirname "$0")" && pwd)/claude-proxy-key.pem"
TAG_NAME="claude-code-proxy"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Claude Code Proxy — AWS Deployment ==="
echo "Region: $REGION"
echo ""

# ---------- Account ID ----------
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET_NAME="claude-proxy-logs-${ACCOUNT_ID}-${REGION}"
echo "Account: $ACCOUNT_ID"
echo "S3 Bucket: $BUCKET_NAME"
echo ""

# ---------- S3 Bucket ----------
echo "--- S3 Bucket ---"
if aws s3api head-bucket --bucket "$BUCKET_NAME" --region "$REGION" 2>/dev/null; then
    echo "Bucket $BUCKET_NAME already exists"
else
    aws s3api create-bucket \
        --bucket "$BUCKET_NAME" \
        --region "$REGION" \
        --create-bucket-configuration LocationConstraint="$REGION"
    echo "Created bucket $BUCKET_NAME"
fi

# ---------- IAM Role ----------
echo ""
echo "--- IAM Role ---"
TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

if aws iam get-role --role-name "$ROLE_NAME" 2>/dev/null; then
    echo "Role $ROLE_NAME already exists"
else
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$TRUST_POLICY"
    echo "Created role $ROLE_NAME"
fi

# ---------- IAM Inline Policy (write-only S3) ----------
echo ""
echo "--- IAM Policy (write-only) ---"
S3_POLICY=$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject"],
    "Resource": ["arn:aws:s3:::${BUCKET_NAME}/*"]
  }]
}
POLICY
)

aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "$POLICY_NAME" \
    --policy-document "$S3_POLICY"
echo "Applied inline policy $POLICY_NAME (s3:PutObject only)"

# ---------- Instance Profile ----------
echo ""
echo "--- Instance Profile ---"
if aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" 2>/dev/null; then
    echo "Instance profile $PROFILE_NAME already exists"
else
    aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME"
    aws iam add-role-to-instance-profile \
        --instance-profile-name "$PROFILE_NAME" \
        --role-name "$ROLE_NAME"
    echo "Created instance profile $PROFILE_NAME"
    echo "Waiting for instance profile propagation..."
    sleep 10
fi

# ---------- Key Pair ----------
echo ""
echo "--- Key Pair ---"
if [ -f "$KEY_FILE" ]; then
    echo "Key file $KEY_FILE already exists, reusing"
else
    # Delete remote key if it exists without a local file
    aws ec2 delete-key-pair --key-name "$KEY_NAME" --region "$REGION" 2>/dev/null || true
    aws ec2 create-key-pair \
        --key-name "$KEY_NAME" \
        --region "$REGION" \
        --query 'KeyMaterial' \
        --output text > "$KEY_FILE"
    chmod 400 "$KEY_FILE"
    echo "Created key pair $KEY_NAME -> $KEY_FILE"
fi

# ---------- Security Group ----------
echo ""
echo "--- Security Group ---"
SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" \
    --region "$REGION" \
    --query 'SecurityGroups[0].GroupId' \
    --output text 2>/dev/null || echo "None")

if [ "$SG_ID" != "None" ] && [ -n "$SG_ID" ]; then
    echo "Security group $SG_NAME already exists: $SG_ID"
else
    SG_ID=$(aws ec2 create-security-group \
        --group-name "$SG_NAME" \
        --description "Claude Code Proxy - SSH and proxy access" \
        --region "$REGION" \
        --query 'GroupId' \
        --output text)
    aws ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" \
        --region "$REGION" \
        --ip-permissions \
            IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges='[{CidrIp=0.0.0.0/0}]' \
            IpProtocol=tcp,FromPort=8080,ToPort=8080,IpRanges='[{CidrIp=0.0.0.0/0}]'
    echo "Created security group $SG_NAME: $SG_ID"
fi

# ---------- Check for existing instance ----------
echo ""
echo "--- EC2 Instance ---"
EXISTING_ID=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=$TAG_NAME" "Name=instance-state-name,Values=running" \
    --region "$REGION" \
    --query 'Reservations[0].Instances[0].InstanceId' \
    --output text 2>/dev/null || echo "None")

if [ "$EXISTING_ID" != "None" ] && [ -n "$EXISTING_ID" ]; then
    PUBLIC_IP=$(aws ec2 describe-instances \
        --instance-ids "$EXISTING_ID" \
        --region "$REGION" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' \
        --output text)
    echo "Instance already running: $EXISTING_ID ($PUBLIC_IP)"
else
    # ---------- AMI (latest Amazon Linux 2023) ----------
    AMI_ID=$(aws ec2 describe-images \
        --owners amazon \
        --filters "Name=name,Values=al2023-ami-2023*-x86_64" "Name=state,Values=available" \
        --region "$REGION" \
        --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
        --output text)
    echo "Using AMI: $AMI_ID"

    # ---------- Build user-data with embedded source ----------
    SOURCE_TAR=$(cd "$PROJECT_DIR" && tar czf - config.py logger.py proxy.py requirements.txt | base64)

    USER_DATA=$(cat <<'USERDATA_HEADER'
#!/bin/bash
set -euxo pipefail
exec > /var/log/claude-proxy-setup.log 2>&1

echo "=== Claude Code Proxy EC2 Setup ==="

# Install Python 3.12
dnf install -y python3.12 python3.12-pip tar

# Create app directory
APP_DIR="/opt/claude-code-proxy"
mkdir -p "$APP_DIR"

# Decode embedded source files
cd "$APP_DIR"
base64 -d <<'TAREOF' | tar xzf -
USERDATA_HEADER
)

    USER_DATA+=$'\n'"${SOURCE_TAR}"$'\n'

    USER_DATA+=$(cat <<USERDATA_FOOTER
TAREOF

# Create venv and install deps
python3.12 -m venv "\$APP_DIR/venv"
"\$APP_DIR/venv/bin/pip" install -r "\$APP_DIR/requirements.txt"

# Create log directory
mkdir -p "\$APP_DIR/logs"

# Create systemd service
cat > /etc/systemd/system/claude-proxy.service <<SVCEOF
[Unit]
Description=Claude Code Proxy
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=\$APP_DIR
Environment=LOG_DIR=\$APP_DIR/logs
Environment=S3_BUCKET=${BUCKET_NAME}
Environment=S3_PREFIX=claude-proxy-logs
Environment=ANTHROPIC_API_BASE=https://api.anthropic.com
Environment=PROXY_PORT=8080
Environment=UPSTREAM_READ_TIMEOUT=300
ExecStart=\$APP_DIR/venv/bin/uvicorn proxy:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable claude-proxy
systemctl start claude-proxy

echo "=== Setup complete ==="
USERDATA_FOOTER
)

    # ---------- Launch Instance ----------
    INSTANCE_ID=$(aws ec2 run-instances \
        --image-id "$AMI_ID" \
        --instance-type "$INSTANCE_TYPE" \
        --key-name "$KEY_NAME" \
        --security-group-ids "$SG_ID" \
        --iam-instance-profile Name="$PROFILE_NAME" \
        --region "$REGION" \
        --user-data "$USER_DATA" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$TAG_NAME}]" \
        --query 'Instances[0].InstanceId' \
        --output text)

    echo "Launched instance: $INSTANCE_ID"
    echo "Waiting for instance to be running..."
    aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"

    PUBLIC_IP=$(aws ec2 describe-instances \
        --instance-ids "$INSTANCE_ID" \
        --region "$REGION" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' \
        --output text)
    echo "Instance running: $INSTANCE_ID ($PUBLIC_IP)"
fi

# ---------- Output ----------
echo ""
echo "============================================"
echo "  Deployment complete!"
echo "============================================"
echo ""
echo "SSH:          ssh -i $KEY_FILE ec2-user@$PUBLIC_IP"
echo "Health:       curl http://$PUBLIC_IP:8080/health"
echo "Setup log:    ssh -i $KEY_FILE ec2-user@$PUBLIC_IP 'sudo cat /var/log/claude-proxy-setup.log'"
echo "Service:      ssh -i $KEY_FILE ec2-user@$PUBLIC_IP 'sudo systemctl status claude-proxy'"
echo ""
echo "Configure Claude Code:"
echo "  export ANTHROPIC_BASE_URL=http://$PUBLIC_IP:8080"
echo ""
echo "Verify write-only S3 (should be DENIED):"
echo "  ssh -i $KEY_FILE ec2-user@$PUBLIC_IP 'aws s3 ls s3://$BUCKET_NAME/'"
echo ""
echo "Note: Allow 2-3 minutes for instance initialization before testing."
