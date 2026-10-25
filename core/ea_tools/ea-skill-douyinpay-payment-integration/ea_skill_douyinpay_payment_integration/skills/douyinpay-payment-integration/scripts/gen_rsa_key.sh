#!/bin/bash

BITS=2048
OUT_DIR="./certs"

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --bits)    BITS="$2"; shift ;;
        --out-dir) OUT_DIR="$2"; shift ;;
        *)         echo "Unknown parameter: $1"; exit 1 ;;
    esac
    shift
done

mkdir -p "$OUT_DIR"

KEY_FILE="$OUT_DIR/doupay.key.pem"
REQ_FILE="$OUT_DIR/doupay.req.pem"
CERT_FILE="$OUT_DIR/doupay.platform.pem"

FAIL_HINT="Please manually generate key pairs per DouyinPay official docs: https://pay.douyinpay.com/wiki/66aa57118a7da602efb9bc2f/67bee79569cf2a053adb0a68"

fail_and_exit() {
    local reason="$1"
    rm -f "$KEY_FILE" "$REQ_FILE"
    echo "[gen_rsa_key] Generation failed: $reason" >&2
    echo "[gen_rsa_key] $FAIL_HINT" >&2
    exit 1
}

if ! command -v openssl >/dev/null 2>&1; then
    fail_and_exit "openssl not found. Please install openssl and retry, or complete key/cert setup on the merchant platform."
fi

echo "Generating RSA key pair in $OUT_DIR..."

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:"$BITS" -out "$KEY_FILE" 2>/dev/null &&
openssl req -new -sha256 -key "$KEY_FILE" -out "$REQ_FILE" -utf8 \
    -subj "/C=CN/O=DouyinPay Technology Co., Ltd." 2>/dev/null || {
    fail_and_exit "openssl execution error"
}

cat > "$CERT_FILE" <<'EOF'
-----BEGIN CERTIFICATE-----
This is a placeholder file for the DouyinPay public key certificate (RSA):
Upload doupay.req.pem (the CSR file) to the DouyinPay merchant platform
(Product Center - Key Management - Apply for New Certificate) to obtain the
merchant public key certificate and serial number.
You can then download the required DouyinPay public key certificate from the platform.
-----END CERTIFICATE-----
EOF

echo "Successfully generated:"
echo "  Private Key:   $KEY_FILE"
echo "  CSR:           $REQ_FILE"
echo "  Platform Cert: $CERT_FILE"
