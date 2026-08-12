FROM docker.m.daocloud.io/library/ubuntu:24.04
ARG UBUNTU_MIRROR=https://mirrors.aliyun.com/ubuntu
RUN if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
      sed -i -E "s#https?://(archive|security).ubuntu.com/ubuntu#${UBUNTU_MIRROR}#g" /etc/apt/sources.list.d/ubuntu.sources; \
    else \
      printf 'deb %s noble main restricted universe multiverse\ndeb %s noble-updates main restricted universe multiverse\ndeb %s noble-security main restricted universe multiverse\n' "$UBUNTU_MIRROR" "$UBUNTU_MIRROR" "$UBUNTU_MIRROR" > /etc/apt/sources.list; \
    fi
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99 HOME=/home/cmcc PYTHONUNBUFFERED=1 CMCC_VERSION=1.1
RUN set -eux; \
  for MIRROR in "${UBUNTU_MIRROR}" https://mirrors.cloud.tencent.com/ubuntu https://mirrors.ustc.edu.cn/ubuntu https://mirrors.aliyun.com/ubuntu http://archive.ubuntu.com/ubuntu; do \
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
      sed -i -E "s#https?://[^ ]+/ubuntu#${MIRROR}#g" /etc/apt/sources.list.d/ubuntu.sources; \
    else \
      printf 'deb %s noble main restricted universe multiverse\ndeb %s noble-updates main restricted universe multiverse\ndeb %s noble-security main restricted universe multiverse\n' "${MIRROR}" "${MIRROR}" "${MIRROR}" > /etc/apt/sources.list; \
    fi; \
    printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-retries; \
    if apt-get update -o Acquire::Retries=5 \
 && apt-get install -y --fix-missing --no-install-recommends \
 ca-certificates curl xvfb openbox procps file python3 python3-pip x11vnc novnc websockify \
 libgtk-3-0 libnotify4 libnss3 libxss1 libxtst6 xdg-utils libatspi2.0-0 \
 libuuid1 libsecret-1-0 libasound2t64 libgbm1 libdrm2 libxcomposite1 \
 libxdamage1 libxrandr2 libxkbcommon0 libxfixes3 libpango-1.0-0 libcairo2 \
 libatk1.0-0 libatk-bridge2.0-0 && rm -rf /var/lib/apt/lists/*; then exit 0; fi; \
    rm -rf /var/lib/apt/lists/*; \
  done; \
  echo '所有 Ubuntu APT 镜像源均失败'; exit 100

COPY CMCC-JTYDN-UOSx86-2.23.1.deb /tmp/cmcc.deb
RUN dpkg -i /tmp/cmcc.deb || (apt-get update -o Acquire::Retries=5 && apt-get -f install -y --fix-missing) && rm -f /tmp/cmcc.deb \
 && useradd -m -s /bin/bash cmcc
RUN mkdir -p /opt/cmcc-runtime-lib /opt/cmcc-app /data \
 && find /opt/chuanyun-vdi-client/resources/app.asar.unpacked/node_modules/chuanyunAddOn/ccsdk/uos/lib -maxdepth 1 -type f -name '*.so*' \
 ! -name 'libc.so*' ! -name 'libm.so*' ! -name 'libpthread.so*' ! -name 'librt.so*' ! -name 'libdl.so*' ! -name 'libresolv.so*' -exec ln -s {} /opt/cmcc-runtime-lib/ \; \
 && chmod 4755 /opt/chuanyun-vdi-client/chrome-sandbox || true
RUN chown -R cmcc:cmcc /opt/cmcc-runtime-lib /opt/chuanyun-vdi-client /data

COPY requirements.txt /opt/cmcc-app/requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /opt/cmcc-app/requirements.txt
COPY service.py /opt/cmcc-app/service.py
COPY webui /opt/cmcc-app/webui
COPY entrypoint.sh /opt/cmcc-app/entrypoint.sh
RUN chmod +x /opt/cmcc-app/entrypoint.sh && chown -R cmcc:cmcc /opt/cmcc-app /home/cmcc /data
WORKDIR /opt/cmcc-app
EXPOSE 8080
ENTRYPOINT ["/opt/cmcc-app/entrypoint.sh"]
