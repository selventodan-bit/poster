FROM php:8.3-cli
RUN apt-get update && apt-get install -y --no-install-recommends \
    unzip git libonig-dev libxml2-dev libzip-dev libpng-dev libicu-dev \
    && docker-php-ext-install mysqli pdo_mysql mbstring gd zip intl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
