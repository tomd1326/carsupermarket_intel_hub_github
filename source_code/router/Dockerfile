ARG BUILD_DIR="backend"

FROM public.ecr.aws/lambda/python:3.12 as build-image

ARG PKG_URL
ARG LAST_PKG_VERSION
ARG DOMAIN_ML
ARG DOMAIN_GENERIC
ARG DOMAIN_NAME_CAR_CONFLICT
ARG DOMAIN_NAME_FMC

ENV TZ=Europe/Paris

# Configuring tzdata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Install build dependencies
RUN dnf update -y && \
    dnf install -y git && \
    dnf clean all

# Include global arg in this stage of the build
ARG BUILD_DIR
ARG LOCAL_ENV

RUN mkdir -p ${BUILD_DIR}

COPY ./pyproject.toml .
COPY ./backend ${BUILD_DIR}/

# Upgrade pip
RUN pip3 install --upgrade pip

# Generating the requirements.txt file
RUN pip3 install poetry poetry-plugin-export

RUN if [ "$LOCAL_ENV" = "1" ]; then \
        poetry export -f requirements.txt --output requirements.txt --with dev; \
    else \
        poetry export -f requirements.txt --output requirements.txt; \
    fi

#RUN pip install --target ${BUILD_DIR} pricing-toolbox==${LAST_PKG_VERSION} --extra-index-url ${PKG_URL}

RUN pip3 install \
      --target /python_build \
      -r requirements.txt

FROM public.ecr.aws/lambda/python:3.12 as run-image

ENV TZ=Europe/London

COPY --from=build-image ${BUILD_DIR}/ ${LAMBDA_TASK_ROOT}/
COPY --from=build-image /python_build /python_build

COPY backend/ ${LAMBDA_TASK_ROOT}/

ENV PYTHONPATH=.:backend:/python_build
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install Chromium system dependencies via dnf (install-deps uses apt-get which is unavailable on Amazon Linux)
RUN dnf install -y \
    nss \
    dbus \
    dbus-libs \
    atk \
    at-spi2-atk \
    at-spi2-core \
    cups-libs \
    libdrm \
    libxkbcommon \
    libXcomposite \
    libXdamage \
    libXext \
    libXfixes \
    libXrandr \
    libXrender \
    libX11 \
    libxcb \
    mesa-libgbm \
    pango \
    cairo \
    alsa-lib \
    fontconfig \
    lsof \
    libstdc++ \
    libgcc \
    && dnf clean all

# Install Playwright and Chromium
RUN pip3 install "playwright>=1.44.0" && \
    python3 -m playwright install chromium

CMD ["main.handler"]