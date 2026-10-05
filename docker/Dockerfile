FROM python:3.11-slim
WORKDIR /app
COPY gateway/ /app/gateway/
RUN pip install --no-cache-dir ./gateway
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "webspec"]
