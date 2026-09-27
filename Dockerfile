FROM python:3.12-slim

# mergerfs-tools is vendored from an immutable commit, not mutable master.zip.
ARG MERGERFS_TOOLS_REF=80d6c9511da554009415d67e7c0ead1256c1fc41

# rsync/curl are used by the mergerfs-tools scripts at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        rsync \
        unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Vendor mergerfs-tools at a pinned commit (app.py expects them in /app/tools/src).
RUN curl -fsSL "https://codeload.github.com/trapexit/mergerfs-tools/tar.gz/${MERGERFS_TOOLS_REF}" -o /tmp/mergerfs-tools.tar.gz \
    && mkdir -p /app/tools \
    && tar -xzf /tmp/mergerfs-tools.tar.gz -C /app/tools --strip-components=1 \
    && rm -f /tmp/mergerfs-tools.tar.gz

COPY app.py ./

EXPOSE 8480

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8480/api/metrics').status==200 else 1)"

CMD ["python3", "app.py"]
