ARG BUILD_DIR="backend"

FROM python:3.9-slim-buster as build-image
   
ARG PKG_URL
ARG LAST_PKG_VERSION
ARG DOMAIN_ML
ARG DOMAIN_GENERIC
ARG DOMAIN_NAME_CAR_CONFLICT
ARG DOMAIN_NAME_FMC

ENV TZ=Europe/Paris

# Configuring tzdata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Install aws-lambda-cpp build dependencies, git, python3, pip3
RUN apt-get update -y && \
  apt-get install -y \
  git-all

# Include global arg in this stage of the build
ARG BUILD_DIR
ARG LOCAL_ENV

RUN mkdir -p ${BUILD_DIR}

COPy ./pyproject.toml .
COPY ./backend ${BUILD_DIR}/

# Upgrade pip
RUN pip3 install \
      --upgrade pip \
      --target ${BUILD_DIR}

# Generating the requirements.txt file
RUN pip3 install poetry

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

COPY --from=build-image ${BUILD_DIR}/ ${LAMBDA_TASK_ROOT}/
COPY --from=build-image /python_build /python_build

COPY backend/* ${LAMBDA_TASK_ROOT}/

ENV PYTHONPATH=.:backend:/python_build

CMD ["main.handler"]