ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/usr/lib/playwright

RUN apk add --no-cache \
        python3 \
        py3-pip \
        chromium \
        chromium-chromedriver \
        nss \
        freetype \
        harfbuzz \
        ca-certificates \
        ttf-freefont \
        tzdata \
        bash

# Tell Playwright to use the system chromium instead of downloading its own
# (Alpine binary works, ~120 MB instead of ~400 MB).
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser

WORKDIR /opt/eon_pl

COPY requirements.txt /opt/eon_pl/requirements.txt
RUN pip3 install --break-system-packages -r requirements.txt

COPY src/ /opt/eon_pl/src/
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
