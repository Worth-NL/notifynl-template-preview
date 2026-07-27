#!/bin/bash
DOCKER_IMAGE_NAME=notifications-template-preview
PORT=6013

if [[ "${@}" == "web" || "${@}" == "web-local" ]]; then
  EXPOSED_PORTS="-e PORT=${PORT} -p 127.0.0.1:${PORT}:${PORT}"
else
  EXPOSED_PORTS=""
fi

docker run -it --rm \
  --network notifynl-devcontainer_devcontainer_devcontainer \
  -e NOTIFY_ENVIRONMENT=development \
  -e FLASK_DEBUG=1 \
  -e STATSD_ENABLED= \
  -e AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-test} \
  -e AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-test} \
  -e AWS_ENDPOINT_URL=${AWS_ENDPOINT_URL:-http://ministack:4566} \
  -e TEMPLATE_PREVIEW_INTERNAL_SECRETS='["my-secret-key"]' \
  -e DANGEROUS_SALT="dev-notify-salt" \
  -e SECRET_KEY="dev-notify-secret-key" \
  -e NOTIFICATION_QUEUE_PREFIX=${NOTIFICATION_QUEUE_PREFIX} \
  -e SENTRY_ENABLED=${SENTRY_ENABLED:-0} \
  -e SENTRY_DSN=${SENTRY_DSN:-} \
  -e SENTRY_ERRORS_SAMPLE_RATE=${SENTRY_ERRORS_SAMPLE_RATE:-} \
  -e SENTRY_TRACES_SAMPLE_RATE=${SENTRY_TRACES_SAMPLE_RATE:-} \
  -e SKIP_TEST_CMYK_PDF=${SKIP_TEST_CMYK_PDF:-1} \
  -e PROCESSED_PDF_SIZE_DIFFERENCE=${PROCESSED_PDF_SIZE_DIFFERENCE:-0.01} \
  ${EXPOSED_PORTS} \
  -v $(pwd):/home/vcap/app \
  ${DOCKER_ARGS} \
  ${DOCKER_IMAGE_NAME} \
  ${@}
