#!/bin/sh
# Entrypoint: runs the snapapp Flask server on port 8080.
#
# Reference-dataset config: unset these env vars to fall back to the
# defaults in app/paths.py (the Manhattan Beach reference).
: "${SNAPAPP_MEGALOC_DB:=/home/dev/lomita_reference_db}"
: "${SNAPAPP_REF_MODEL_DIR:=/home/dev/lomita_reference/sparse}"
: "${SNAPAPP_REF_IMAGES_URL:=s3://production-outputs/geocam/134_lomita_street_office_capture/NiyLkqz/undistorted_images/images}"
: "${SNAPAPP_REF_CACHE_MAX_GB:=20}"
: "${SNAPAPP_S3_ENDPOINT_URL:=http://minio-dn.geocam.io}"
: "${SNAPAPP_S3_ACCESS_KEY:=manager}"
: "${SNAPAPP_S3_SECRET_KEY:=hjO4gsZYAcgV1fmskMLNVqHEOsqd6jGSqceLgkMs}"

# GeoCam manager-api — used for the "Find GeoCam cells" button. Set
# SNAPAPP_GEOCAM_API_TOKEN in the environment (do not commit) or leave
# empty to make the button surface a clean "token not set" error.
: "${SNAPAPP_GEOCAM_API_URL:=https://manager-api-app.geocam.io}"
# The workflow URL pattern — fill in once you know the real manager-UI
# path. `{cell_slug}`, `{cell_map_slug}`, `{project_slug}` substitute.
: "${SNAPAPP_GEOCAM_WORKFLOW_URL:=https://manager-ui.geocam.io/cells/{cell_slug}}"

export SNAPAPP_MEGALOC_DB SNAPAPP_REF_MODEL_DIR SNAPAPP_REF_IMAGES_URL \
       SNAPAPP_REF_CACHE_MAX_GB SNAPAPP_S3_ENDPOINT_URL \
       SNAPAPP_S3_ACCESS_KEY SNAPAPP_S3_SECRET_KEY \
       SNAPAPP_GEOCAM_API_URL SNAPAPP_GEOCAM_API_TOKEN \
       SNAPAPP_GEOCAM_WORKFLOW_URL

cd "$(dirname "$0")"
export PYTHONPATH="$(pwd):$PYTHONPATH"
exec python3 -m app.server --port 8080 --host 0.0.0.0 "$@"
