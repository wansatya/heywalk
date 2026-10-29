# HeyWalk web image: production build of the React IDE served by nginx.
#
# nginx serves the static Vite build and proxies same-origin /api requests
# (REST + the run-event WebSocket) to the `api` service. The frontend uses
# relative /api calls, so NO build-time secrets are needed and MODEL_API_KEY
# stays server-side in the api container only.
#
# Build from the repo root:
#   docker build -f docker/web.Dockerfile -t heywalk-web:latest .
# Or: docker compose build web

FROM node:20-slim AS build

WORKDIR /src
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web ./
RUN npm run build

FROM nginx:alpine

COPY docker/web.nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
