FROM node:22-alpine AS dependencies

WORKDIR /workspace/apps/web

ENV NEXT_TELEMETRY_DISABLED=1

COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

FROM node:22-alpine AS application

WORKDIR /workspace/apps/web

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=dependencies /workspace/apps/web/node_modules ./node_modules
COPY apps/web ./
COPY packages/shared /workspace/packages/shared

RUN npm run build

EXPOSE 5173

CMD ["node", "node_modules/next/dist/bin/next", "start", "--hostname", "0.0.0.0", "--port", "5173"]
