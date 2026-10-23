FROM php:8.3-cli@sha256:60a100afa8efd88c0f556ce60e891c9bb8b1e8bca15721e4db07eca4fa79c07a
RUN apt-get update && apt-get install -y --no-install-recommends \
    unzip git libonig-dev libxml2-dev libzip-dev libpng-dev libicu-dev \
    && docker-php-ext-install mysqli pdo_mysql mbstring gd zip intl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
