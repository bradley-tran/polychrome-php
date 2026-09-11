FROM rust:1.90.0-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    autoconf bison re2c pkg-config make gcc g++ curl ca-certificates xz-utils \
    libxml2-dev libsqlite3-dev libssl-dev libcurl4-openssl-dev libonig-dev \
    libicu-dev libzip-dev libpng-dev libjpeg62-turbo-dev libwebp-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY src ./src
COPY native ./native
COPY scripts ./scripts
COPY preload ./preload
COPY LICENSE ./LICENSE
ENV POLYCHROME_PREFIX=/opt/polychrome
RUN ./scripts/build.sh && strip /opt/polychrome/bin/php \
    /opt/polychrome/bin/polychrome /opt/polychrome/sbin/php-fpm

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libxml2 libsqlite3-0 libssl3 libcurl4 libonig5 \
    libicu72 libzip4 libpng16-16 libjpeg62-turbo libwebp7 zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 polychrome \
    && useradd --uid 1000 --gid 1000 --create-home polychrome \
    && mkdir /app && chown polychrome:polychrome /app
COPY --from=build /opt/polychrome /opt/polychrome
ENV PATH=/opt/polychrome/bin:/opt/polychrome/sbin:$PATH
WORKDIR /app
USER 1000:1000
EXPOSE 9000
ENTRYPOINT ["polychrome"]
CMD ["--root", "/app", "--listen", "0.0.0.0:9000"]
