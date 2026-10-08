FROM python:3.11.15-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt
COPY livebench /opt/livebench
COPY livecodebench /opt/livecodebench
COPY worker.py /opt/worker.py
RUN chmod -R a+rX /opt
ENV PYTHONPATH=/opt/livebench:/opt/livecodebench
WORKDIR /opt/livecodebench
USER 65534:65534
ENTRYPOINT ["python", "/opt/worker.py"]
