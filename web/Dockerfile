# The dashboard as a container (deploy/k8s_aws.py builds it on the K3s server for the aws-prod
# overlays): Next.js's standalone output, so the runtime image holds no dev dependencies.
#   docker build -t sde-dashboard:local web
FROM node:24-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:24-alpine
WORKDIR /web
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3100 HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /web/.next/standalone ./
COPY --from=build --chown=node:node /web/.next/static ./.next/static
# node's uid, as a number: runAsNonRoot can't verify a named user and won't start the container.
USER 1000
EXPOSE 3100
# SDE_URL and SDE_API_KEY come from the environment at run time (the route reads process.env).
CMD ["node", "server.js"]
