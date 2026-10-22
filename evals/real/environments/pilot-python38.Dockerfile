# Trusted preparation only. The operator verifies BASE_IMAGE against the manifest's
# immutable base_image before building. Candidate containers never run pip.
ARG BASE_IMAGE=repopilot-real-tasks:local
FROM ${BASE_IMAGE}
USER root
RUN python -m pip install --no-cache-dir --no-deps 'python-toolbox==1.0.11' 'nose==1.3.7'
USER 10001:10001
