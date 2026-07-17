# 构建阶段：处理插件和元数据
ARG PYTHON_IMAGE=higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/python:3.11-alpine
ARG NGINX_IMAGE=higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/nginx:alpine
ARG ALPINE_MIRROR=""
ARG USE_LOCAL_PLUGINS=false

FROM $PYTHON_IMAGE AS builder-base

# 配置 Alpine 镜像源（可选，本地构建时可指定国内镜像加速）
ARG ALPINE_MIRROR
RUN if [ -n "$ALPINE_MIRROR" ]; then \
        sed -i "s|dl-cdn.alpinelinux.org|$ALPINE_MIRROR|g" /etc/apk/repositories; \
    fi

# 安装系统依赖
RUN apk add --no-cache \
    wget \
    ca-certificates \
    && update-ca-certificates

# 安装 ORAS 客户端
RUN set -eux; \
    ORAS_VERSION="1.2.3"; \
    ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/'); \
    wget -O /tmp/oras.tar.gz "https://github.com/oras-project/oras/releases/download/v${ORAS_VERSION}/oras_$(echo ${ORAS_VERSION})_linux_${ARCH}.tar.gz" \
    && tar -zxvf /tmp/oras.tar.gz -C /usr/local/bin \
    && rm -rf /tmp/oras.tar.gz oras \
    && oras version

WORKDIR /workspace

# 复制脚本和公共文件
COPY pull_plugins.py plugins.properties ./

FROM builder-base AS local-true
# 启用本地模式：复制本地插件源
COPY plugins/ ./plugins/

FROM builder-base AS local-false
# 不启用本地模式：plugins/ 目录由 pull_plugins.py 自行创建(os.makedirs)

FROM local-${USE_LOCAL_PLUGINS:-false} AS builder

# 根据 USE_LOCAL_PLUGINS 决定是否启用本地 WASM 文件覆盖
ARG USE_LOCAL_PLUGINS
RUN if [ "$USE_LOCAL_PLUGINS" = "true" ]; then \
        python3 pull_plugins.py --download-v2 --use-local; \
    else \
        python3 pull_plugins.py --download-v2; \
    fi && \
    rm -f /workspace/plugins/.gitkeep

# 运行阶段：最终镜像
FROM $NGINX_IMAGE

# 从构建阶段复制生成的文件
COPY --from=builder /workspace/plugins /usr/share/nginx/html/plugins

# 复制 Nginx 配置
COPY nginx.conf /etc/nginx/nginx.conf

# 暴露端口
EXPOSE 8080

# 启动 Nginx
CMD ["nginx", "-g", "daemon off;"]