FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# mergerfs-tools is vendored from an immutable commit and verified by SHA256,
# not fetched from mutable master.
ARG MERGERFS_TOOLS_REF=80d6c9511da554009415d67e7c0ead1256c1fc41
ARG MERGERFS_TOOLS_SHA256=9f716b6309846c2fe02d7d6b308fbd2f4a52ab7a264e760cf798f8435656e404

# rsync is the only host tool mergerfs-tools needs at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        rsync \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Vendor mergerfs-tools at a pinned commit (app.py expects them in /app/tools/src).
# Downloaded with Python so curl/unzip stay out of the runtime image.
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://codeload.github.com/trapexit/mergerfs-tools/tar.gz/${MERGERFS_TOOLS_REF}', '/tmp/mergerfs-tools.tar.gz')" \
    && echo "${MERGERFS_TOOLS_SHA256}  /tmp/mergerfs-tools.tar.gz" | sha256sum -c - \
    && mkdir -p /app/tools \
    && tar -xzf /tmp/mergerfs-tools.tar.gz -C /app/tools --strip-components=1 \
    && rm -f /tmp/mergerfs-tools.tar.gz

COPY app.py ./

EXPOSE 8480

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8480'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/api/metrics').status==200 else 1)"

CMD ["python3", "app.py"]
