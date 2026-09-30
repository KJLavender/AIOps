FROM python:3.12-slim

# kubectl: the agent shells out to it; in-cluster it uses the pod's ServiceAccount.
ARG KUBECTL_VERSION=v1.31.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY aiops/ ./aiops/
COPY data/ ./data/

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "aiops"]
