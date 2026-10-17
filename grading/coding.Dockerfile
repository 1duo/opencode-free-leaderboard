ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
COPY coding-requirements.txt /coding-requirements.txt
RUN pip install --no-cache-dir -r /coding-requirements.txt
COPY bigcodebench /opt/bigcodebench
COPY coding_worker.py /opt/coding_worker.py
RUN chmod -R a+rX /opt/bigcodebench /opt/coding_worker.py
ENV PYTHONPATH=/opt/bigcodebench:/opt/livebench:/opt/livecodebench MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /tmp
USER 65534:65534
ENTRYPOINT ["python", "/opt/coding_worker.py"]
