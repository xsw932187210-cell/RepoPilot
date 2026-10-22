FROM python:3.8-slim@sha256:1d52838af602b4b5a831beb13a0e4d073280665ea7be7f69ce2382f29c5a613f
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev
RUN python -m pip install --no-cache-dir 'pip==23.0.1' 'setuptools==68.2.2' 'wheel==0.41.3' \
    && python -m pip install --no-cache-dir \
       'pytest==6.2.5' 'pytest-mock==3.6.1' 'mock==4.0.3' 'psutil==5.9.8' \
       'colorama==0.4.6' 'six==1.16.0' 'decorator==4.4.2' 'pyte==0.8.2'
ENV PYTHONPATH=/workspace PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0
COPY evals/runtime/sitecustomize.py /usr/local/lib/python3.8/site-packages/sitecustomize.py
WORKDIR /workspace
USER 10001:10001
