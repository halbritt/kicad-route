FROM ubuntu@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg git build-essential python3 python3-venv python3-dev time && rm -rf /var/lib/apt/lists/*
COPY kicad.sources /etc/apt/sources.list.d/kicad.sources
RUN apt-get update && apt-cache policy kicad && apt-get install -y --no-install-recommends kicad=10.0.7~ubuntu24.04.1 && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:/usr/local/bin:/usr/bin:/bin
RUN curl --fail --location --proto '=https' --tlsv1.2 https://sh.rustup.rs --output /opt/rustup-init.sh && sh /opt/rustup-init.sh -y --profile minimal --default-toolchain 1.99.1 --no-modify-path && rm /opt/rustup-init.sh
COPY uv /usr/local/bin/uv
RUN python3 -c 'import sys,pcbnew; assert sys.version.split()[0]=="3.12.3"; assert pcbnew.GetBuildVersion().split("-")[0]=="10.0.7"; print(sys.version);print(pcbnew.GetBuildVersion())' && test "$(kicad-cli --version)" = 10.0.7 && rustc -vV && cargo --version && uv --version
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
