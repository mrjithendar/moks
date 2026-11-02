# moks-search with the two plugins upstream does not ship: S3 snapshots and Prometheus metrics. Both are
# pinned to the server's exact version, because OpenSearch refuses a plugin built for any other.
# rag.opensearch.secure.image takes the digest CI reports for this, with pluginsInstalled: true.
FROM opensearchproject/opensearch:3.9.0@sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e
RUN /usr/share/opensearch/bin/opensearch-plugin install --batch repository-s3 \
 && /usr/share/opensearch/bin/opensearch-plugin install --batch \
    https://github.com/opensearch-project/opensearch-prometheus-exporter/releases/download/3.9.0.0/prometheus-exporter-3.9.0.0.zip
