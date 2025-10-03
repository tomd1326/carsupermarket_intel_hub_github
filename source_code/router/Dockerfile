# source_code/router/Dockerfile

ARG BUILD_DIR="backend"

# FIXED: Use Lambda base image for building too - ensures GLIBC compatibility
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

# Install build dependencies for Lambda environment
RUN yum update -y && \
    yum install -y \
    git \
    gcc \
    gcc-c++ \
    make \
    openssl-devel \
    libffi-devel

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

# Install dependencies in Lambda-compatible environment
RUN pip3 install \
      --target /python_build \
      -r requirements.txt

FROM public.ecr.aws/lambda/python:3.9 as run-image

ENV TZ=Europe/London

COPY --from=build-image ${BUILD_DIR}/ ${LAMBDA_TASK_ROOT}/
COPY --from=build-image /python_build /python_build

COPY backend/ ${LAMBDA_TASK_ROOT}/

ENV PYTHONPATH=.:backend:/python_build

CMD ["main.handler"]
