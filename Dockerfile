FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=Asia/Jakarta \
    PROC_ROOT=/host/proc SYS_ROOT=/host/sys FS_ROOT_PREFIX=/host/root \
    MON_HOST=0.0.0.0 MON_PORT=8090

# lpstat/lpadmin/cupsenable untuk panel printer
RUN apt-get update \
 && apt-get install -y --no-install-recommends cups-client tzdata procps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY monitor.py index.html login.html manifest.json sw.js icon-192.png icon-512.png ./

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/login',timeout=4).status<400 else 1)"

CMD ["python3", "monitor.py"]
