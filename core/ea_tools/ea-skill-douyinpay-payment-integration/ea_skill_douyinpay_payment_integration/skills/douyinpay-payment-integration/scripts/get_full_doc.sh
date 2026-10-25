#!/bin/bash
# get_full_doc.sh

if [ -z "$1" ]; then
    echo "Missing document URL, cannot fetch full content"
    exit 1
fi

url="$1"

if [[ "$url" != https://pay.douyinpay.com/wiki/* ]]; then
    echo "Invalid document URL format. Must start with https://pay.douyinpay.com/wiki/"
    exit 1
fi

if [[ "$url" != *.md ]]; then
    url="${url}.md"
fi

response=$(curl -sL -w "\n__HTTP_STATUS__:%{http_code}" "$url" \
     -H "User-Agent: dypay-skill-full-doc-tool" \
     --connect-timeout 3 \
     --max-time 8 \
     --retry 1 \
     --retry-connrefused \
     --retry-max-time 15)

http_status=$(echo "$response" | tail -n1 | sed 's/^__HTTP_STATUS__://')
body=$(echo "$response" | sed '$d')

if [ "$http_status" != "200" ]; then
    echo "Error fetching document, please retry later. (status=$http_status)"
    exit 1
fi

echo "$body"
