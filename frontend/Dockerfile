FROM node:18-alpine AS builder
WORKDIR /app

# Install dependencies
COPY package.json package-lock.json ./
RUN npm ci --silent

# Copy source code and build
COPY . .
RUN npm run build

# Production image
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
# Use custom nginx config for SPA and proxy
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
