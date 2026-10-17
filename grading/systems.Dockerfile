ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    gcc=4:12.2.0-3 gcc-12=12.2.0-14+deb12u1 \
    libc6-dev=2.36-9+deb12u14 libasan8=12.2.0-14+deb12u1 libubsan1=12.2.0-14+deb12u1 \
    && rm -rf /var/lib/apt/lists/* \
    && test "$(gcc -dumpfullversion)" = 12.2.0 \
    && dpkg-query -W > /opt/systems-packages.txt
COPY frameworks /opt/systems-frameworks
COPY systems_worker.py /opt/systems_worker.py
RUN chmod -R a+rX /opt/systems-frameworks /opt/systems_worker.py
WORKDIR /work
USER 65534:65534
ENTRYPOINT ["python", "/opt/systems_worker.py"]
