# Higress Plugin Server

HTTP server for Higress Wasm plugins

## 构建插件服务器镜像并推送

### 构建本地架构镜像

```bash
docker build -t higress-plugin-server:1.0.0 -f Dockerfile .
```

### 构建多架构镜像并推送至仓库

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t {your-image-storage}/higress-plugin-server:1.0.0 \
  -f Dockerfile \
  --push \
  .
```

## 本地启动插件服务器

```bash
docker run -d --name higress-plugin-server --rm -p 8080:8080 higress-plugin-server:1.0.0
```

## K8s 部署 plugin-server

```
apiVersion: apps/v1
kind: Deployment
metadata:
  name: higress-plugin-server
  namespace: higress-system
spec:
  replicas: 2
  selector:
    matchLabels:
      app: higress-plugin-server
      higress: higress-plugin-server
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 25%
  progressDeadlineSeconds: 600
  revisionHistoryLimit: 10
  template:
    metadata:
      labels:
        app: higress-plugin-server
        higress: higress-plugin-server
    spec:
      containers:
        - name: higress-core
          image: higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/plugin-server:1.0.0
          imagePullPolicy: Always
          ports:
            - containerPort: 8080
              protocol: TCP
          resources:
            requests:
              memory: "128Mi"
              cpu: "200m"
            limits:
              memory: "256Mi"
              cpu: "500m"
      restartPolicy: Always
---
apiVersion: v1
kind: Service
metadata:
  name: higress-plugin-server
  namespace: higress-system
  labels:
    app: higress-plugin-server
    higress: higress-plugin-server
spec:
  type: ClusterIP
  ports:
    - port: 80
      protocol: TCP
      targetPort: 8080
  selector:
    app: higress-plugin-server
    higress: higress-plugin-server
```


## 配置插件地址

### 在 Higress Console 中配置插件下载地址

要满足这一需求，只需要为 Higress Console 容器添加 HIGRESS_ADMIN_WASM_PLUGIN_CUSTOM_IMAGE_URL_PATTERN 环境变量，值为自定义镜像地址的格式模版。模版可以按需使用 ${name} 和 ${version} 作为插件名称和镜像版本的占位符。

示例：

```bash
HIGRESS_ADMIN_WASM_PLUGIN_CUSTOM_IMAGE_URL_PATTERN=http://higress-plugin-server.higress-system.svc/plugins/${name}/${version}/plugin.wasm
```

> `higress-plugin-server.higress-system.svc` 替换为 Higress Gateway 可以用来访问插件服务器的地址。

Higress Console 针对 key-rate-limit 插件生成的镜像地址将为：http://higress-plugin-server.higress-system.svc/plugins/key-rate-limit/1.0.0/plugin.wasm

### 修改对接 Nacos 3.x 所生成的 MCP Server 插件地址配置

要满足这一需求，只需要为 Higress Controller 容器添加 MCP_SERVER_WASM_IMAGE_URL 环境变量，值为根据自定义镜像地址格式模版生成的 mcp-server 插件地址。

示例：

```bash
MCP_SERVER_WASM_IMAGE_URL=http://higress-plugin-server.higress-system.svc/plugins/mcp-server/1.0.0/plugin.wasm
```

> `higress-plugin-server.higress-system.svc` 替换为 Higress Gateway 可以用来访问插件服务器的地址。

## 参考文档

- 如何修改内置插件镜像地址：https://higress.cn/docs/latest/ops/how-tos/builtin-plugin-url/
- 如何使用 HTTP/HTTPS 协议加载 Wasm 插件：https://higress.cn/docs/latest/ops/how-tos/load-wasm-with-http/
