FROM python:3.13-alpine
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY driver.json /app/driver.json
COPY intg-iptv /app/intg-iptv
WORKDIR /app
ENV UC_CONFIG_HOME=/config UC_DISABLE_MDNS_PUBLISH=true PYTHONPATH=/app/intg-iptv
CMD ["python", "intg-iptv/driver.py"]
