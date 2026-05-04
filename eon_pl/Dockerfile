ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM $BUILD_FROM

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Warsaw \
    CHROMIUM_BIN=/usr/bin/chromium-browser \
    CHROMEDRIVER_BIN=/usr/bin/chromedriver

RUN apk add --no-cache \
        python3 \
        py3-pip \
        chromium \
        chromium-chromedriver \
        chromium-swiftshader \
        nss \
        freetype \
        harfbuzz \
        ca-certificates \
        ttf-freefont \
        ttf-dejavu \
        ttf-liberation \
        font-noto \
        tzdata \
        bash \
        xvfb-run \
        mesa-dri-gallium

WORKDIR /opt/eon_pl

COPY requirements.txt /opt/eon_pl/requirements.txt
RUN pip3 install --break-system-packages -r requirements.txt

COPY src/ /opt/eon_pl/src/
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
