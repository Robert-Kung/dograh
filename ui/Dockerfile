# syntax=docker/dockerfile:1
# Multi-stage build
# Stage 1: Dependencies
FROM node:20-alpine AS deps
WORKDIR /app

# Install Python and build dependencies for native modules
# This helps with ARM64 builds and native module compilation
RUN apk add --no-cache python3 make g++ libc6-compat

# Copy package files
COPY ui/package*.json ./

# Clean install with proper handling of native modules
RUN --mount=type=cache,target=/root/.npm npm ci

# Stage 2: Builder
FROM node:20-alpine AS builder
WORKDIR /app

# Install libc6-compat for native modules in builder stage too
RUN apk add --no-cache libc6-compat

# Copy dependencies from deps stage
COPY --from=deps /app/node_modules ./node_modules

# Copy all files needed for build
COPY ui/package*.json ./
COPY ui/tsconfig.json ./
COPY ui/next.config.ts ./
COPY ui/components.json ./
COPY ui/sentry.edge.config.ts ./
COPY ui/sentry.server.config.ts ./
COPY ui/postcss.config.mjs ./
COPY ui/public ./public
COPY ui/src ./src

# Set build-time environment variables (needed for Next.js build)
ENV NEXT_PUBLIC_NODE_ENV="oss"
ENV NEXT_TELEMETRY_DISABLED="1"
# W2d task 1.2h（U-19）：移除硬編的 Chatwoot URL 與 website token。
# 上游把它們烤進 builder 階段，Next.js 再把 NEXT_PUBLIC_* **內聯進 client bundle**
# ——自建之後這顆 token 就是我方烤進交付態、經閘門送給每個客戶主管瀏覽器的東西。
# 該第三方整合不在 egress-allowlist.yml、不在任何殘留登記，今天擋住它的只有
# 閘門的 CSP `script-src 'self'`。ChatwootWidget.tsx:43 在值為空時安全短路。
# 需要時由部署層以 build arg／env 提供（鍵名須先進 preflight 的正面表列）。
ENV BACKEND_URL="http://api:8000"

# Build the application with standalone mode
# Increase Node.js heap size to prevent out-of-memory errors during build
ENV NODE_OPTIONS="--max-old-space-size=4096"
RUN npm run build && \
    rm -rf /tmp/* /root/.npm /root/.next/cache

# Stage 3: Runner (production image)
FROM node:20-alpine AS runner
WORKDIR /app

# Environment variables will be provided by docker-compose
ENV NODE_ENV=production

# Create a non-root user
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy standalone build output
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

# Switch to non-root user
USER nextjs

# Expose the port Next.js runs on
EXPOSE 3010

# Start the production server using the standalone Node.js server
CMD sh -c "echo '🚀 Application ready at http://localhost:3010' && PORT=3010 node server.js"
