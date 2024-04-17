ARG BUILD_DIR="backend"
ARG COMPANY_NAME="aramisauto"

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

RUN mkdir -p ${BUILD_DIR}

#COPY ./requirements.txt .
COPy ./pyproject.toml .
COPY ./backend ${BUILD_DIR}/

RUN pip3 install \
      --upgrade pip \
      --target ${BUILD_DIR}

RUN pip3 install poetry
RUN poetry export -f requirements.txt --output requirements.txt

#RUN pip install --target ${BUILD_DIR} pricing-toolbox==${LAST_PKG_VERSION} --extra-index-url ${PKG_URL}

RUN pip3 install \
      --target ${BUILD_DIR} \
      -r requirements.txt

FROM public.ecr.aws/lambda/python:3.9 as run-image

COPY --from=build-image ${BUILD_DIR}/* ${LAMBDA_TASK_ROOT}/

COPY backend/* ${LAMBDA_TASK_ROOT}/

ARG DOMAIN_ML
ARG DOMAIN_GENERIC
ARG DOMAIN_NAME_CAR_CONFLICT
ARG DOMAIN_NAME_FMC

ENV DOMAIN_NAME_ML=${DOMAIN_ML}
ENV DOMAIN_NAME_GENERIC=${DOMAIN_GENERIC}
ENV DOMAIN_NAME_CAR_CONFLICT=${DOMAIN_NAME_CAR_CONFLICT}
ENV DOMAIN_NAME_FMC=${DOMAIN_NAME_FMC}
CMD ["main.handler"]