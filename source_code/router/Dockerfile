ARG BUILD_DIR="backend"

FROM public.ecr.aws/lambda/python:3.9 as build-image

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
RUN yum update -y && \
  yum install -y \
  git

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

FROM public.ecr.aws/lambda/python:3.9 as run-image

ENV TZ=Europe/London

# System dependencies required by Chromium on Amazon Linux 2
RUN yum install -y \
    atk \
    cups-libs \
    gtk3 \
    libXcomposite \
    libXcursor \
    libXdamage \
    libXext \
    libXi \
    libXrandr \
    libXScrnSaver \
    libXtst \
    pango \
    xorg-x11-fonts-100dpi \
    xorg-x11-fonts-75dpi \
    xorg-x11-fonts-cyrillic \
    xorg-x11-fonts-misc \
    xorg-x11-fonts-Type1 \
    xorg-x11-utils \
    alsa-lib \
    nss \
    && yum clean all

COPY --from=build-image ${BUILD_DIR}/ ${LAMBDA_TASK_ROOT}/
COPY --from=build-image /python_build /python_build

COPY backend/ ${LAMBDA_TASK_ROOT}/

ENV PYTHONPATH=.:backend:/python_build

# Install Playwright and Chromium in the run image so the browser binary is available at runtime
RUN pip3 install playwright && \
    playwright install chromium

CMD ["main.handler"]