# Trusted dependency preparation. Build with BASE_IMAGE set to the immutable
# The Fuck runtime ID recorded in the pilot manifest. Never install upstream code.
ARG BASE_IMAGE=repopilot-real-tasks:local
FROM ${BASE_IMAGE}
USER root
RUN python -m pip install --no-cache-dir --no-deps \
    'future==0.18.3' 'click==7.1.2' 'Jinja2==2.11.2' 'MarkupSafe==2.0.1' \
    'whichcraft==0.6.1' 'binaryornot==0.4.4' 'chardet==3.0.4' 'poyo==0.5.0' \
    'jinja2-time==0.2.0' 'arrow==0.15.6' 'python-dateutil==2.8.1' \
    'appdirs==1.4.4' 'requests==2.23.0' 'urllib3==1.25.9' \
    'certifi==2020.4.5.2' 'idna==2.9' 'ruamel.yaml==0.16.13' \
    'pytest-cov==2.10.0' 'coverage==5.1'
USER 10001:10001
