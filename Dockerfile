FROM n8nio/n8n:latest

USER root

COPY --chown=node:node docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

COPY --chown=node:node workflows/  /import/workflows/
COPY --chown=node:node credentials/ /import/credentials/

USER node
