ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG WORKER_SHA256
LABEL cad.table-recovery.worker-sha256=${WORKER_SHA256}
WORKDIR /app
COPY worker.py /app/worker.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "-B", "/app/worker.py"]
