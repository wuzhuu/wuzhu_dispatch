FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/opt/stock
RUN addgroup --gid 10001 stockapp && adduser --uid 10001 --gid 10001 --disabled-password --gecos "" stockapp
WORKDIR /opt/stock
COPY stackAnalys/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY stackAnalys /opt/stock/stackAnalys
COPY scripts /opt/stock/scripts
COPY config /opt/stock/config
COPY runtime /opt/stock/runtime
COPY maintenance /opt/stock/maintenance
COPY entrypoint.sh /opt/stock/entrypoint.sh
RUN chmod 0755 /opt/stock/entrypoint.sh /opt/stock/runtime/*.py /opt/stock/maintenance/*.py && mkdir -p /var/lib/stock/data /var/lib/stock/queue /var/lib/stock/lease && chown -R 10001:10001 /opt/stock /var/lib/stock
USER 10001:10001
ENTRYPOINT ["/opt/stock/entrypoint.sh"]
