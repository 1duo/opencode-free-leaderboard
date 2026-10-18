ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    g++-12=12.2.0-14+deb12u1 rustc=1.63.0+dfsg1-2 cargo=0.66.0+ds1-1 \
    golang-1.19-go=1.19.8-2 golang-github-stretchr-testify-dev=1.8.1-1 \
    librust-rand-dev=0.8.5-1 librust-regex+default-dev=1.7.1-1 librust-md5-dev=0.7.0-2 \
    && rm -rf /var/lib/apt/lists/*
RUN test "$(g++-12 -dumpfullversion)" = 12.2.0 \
    && rustc --version | grep -E '^rustc 1\.63\.0( |$)' \
    && /usr/lib/go-1.19/bin/go version | grep 'go1.19.8 ' \
    && dpkg-query -W > /opt/humanevalpack-packages.txt
RUN mkdir -p /opt/hep-rust/src /opt/hep-rust/.cargo \
    && printf '[package]\nname="hep-dependencies"\nversion="0.1.0"\nedition="2021"\n[dependencies]\nrand="=0.8.5"\nregex="=1.7.1"\nmd5="=0.7.0"\n' > /opt/hep-rust/Cargo.toml \
    && printf '[source.crates-io]\nreplace-with="debian"\n[source.debian]\ndirectory="/usr/share/cargo/registry"\n' > /opt/hep-rust/.cargo/config.toml \
    && printf 'fn main(){}\n' > /opt/hep-rust/src/main.rs \
    && cd /opt/hep-rust && cargo build --offline --release \
    && cat Cargo.lock >> /opt/humanevalpack-packages.txt \
    && chmod -R a+rX /opt/hep-rust
COPY humanevalpack_worker.py /opt/humanevalpack_worker.py
COPY humanevalpack_go.go /opt/humanevalpack_go.go
RUN GO111MODULE=off /usr/lib/go-1.19/bin/go build -trimpath -ldflags='-s -w' \
    -o /opt/hep-go-assemble /opt/humanevalpack_go.go
WORKDIR /work
USER 65534:65534
ENTRYPOINT ["python", "/opt/humanevalpack_worker.py"]
