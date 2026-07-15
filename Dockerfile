FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 buddy2api \
    && mkdir -p /app/data \
    && chown -R buddy2api:buddy2api /app \
    && chmod +x /app/docker-entrypoint.sh \
    && ln -s /app/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]

EXPOSE 8787

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8787"]
