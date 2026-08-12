# Higress Plugin Server

## Immutable snapshot builds

Production gateway-version images are assembled only by the protected Higress
workflow from a canonical Higress snapshot, an exact plugin-server source
commit, and digest-addressed OCI artifacts. Invoke `pull_plugins.py --snapshot
snapshot.json --output plugins` for a local fixture. The downloader rejects a
missing/ambiguous plugin-server mapping, OCI tag/version drift, digest mismatch,
unsafe tar layout, missing Wasm content, and invalid Wasm magic. It writes
logical aliases (including `json-converter` / `jsonrpc-converter`) from the
same verified bytes and records their content hashes in `snapshot-inventory.json`.

The repository-local workflow is validation-only and has no production registry
credential. The external release cutover must grant the Higress pinned-snapshot
workflow the sole production writer for `higress/plugin-server:<gateway-version>`;
this repository may use a separate development/candidate namespace only.

HTTP server for Higress Wasm plugins

## 构建插件服务器镜像并推送

### 构建本地架构镜像

```bash
docker build -t higress-plugin-server:1.0.0 -f Dockerfile .
```

#### 国内镜像加速（可选）

本地构建时，如果 Alpine 官方源访问较慢，可以指定国内镜像源加速：

```bash
docker build --build-arg ALPINE_MIRROR=mirrors.aliyun.com -t higress-plugin-server:1.0.0 -f Dockerfile .
```

可选的镜像源：
- 阿里云：`mirrors.aliyun.com`
- 清华大学：`mirrors.tuna.tsinghua.edu.cn`
- 中科大：`mirrors.ustc.edu.cn`

> GitHub Actions 等海外环境无需指定，默认使用官方源 `dl-cdn.alpinelinux.org`。

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

## 使用本地 WASM 文件

不想把 WASM 推送到 OCI 仓库时（自定义插件，或用自编译版本覆盖官方插件），可直接把 `plugin.wasm` 放进仓库的 `plugins/` 目录。

> ⚠️ **默认行为**：`USE_LOCAL_PLUGINS` 默认为 `false`，构建时按 `plugins.properties` 从 OCI 仓库下载所有插件，`plugins/` 目录被忽略。
> 要启用本地 WASM 覆盖，需在构建时指定 `--build-arg USE_LOCAL_PLUGINS=true`。

启用本地模式后，`plugins/<插件名>/<版本>/plugin.wasm` 会随构建上下文带入；与本地同名同版本的 OCI 下载会被自动跳过，构建结束时为 `plugins/<插件名>/<版本>/` 下的 `plugin.wasm` 自动生成 `metadata.txt`。**无需修改 `plugins.properties`**——本地插件按目录约定自动识别。

### 启用本地 WASM 构建

```bash
docker build --build-arg USE_LOCAL_PLUGINS=true -t higress-plugin-server:1.0.0 -f Dockerfile .
```

也可与国内镜像加速参数组合使用：

```bash
docker build \
  --build-arg ALPINE_MIRROR=mirrors.aliyun.com \
  --build-arg USE_LOCAL_PLUGINS=true \
  -t higress-plugin-server:1.0.0 \
  -f Dockerfile .
```

### 两种用法

1. **新增自定义插件**：在 `plugins/<插件名>/<版本>/` 下放置 `plugin.wasm`，不要写进 `plugins.properties`。它不会被下载，构建后自动生成 `metadata.txt` 并被 serve。需要哪些版本就放哪些版本目录。

2. **覆盖官方插件**：保留 `plugins.properties` 中该插件的 OCI 配置，同时把你的 `plugin.wasm` 放到同名同版本目录下。启用本地模式后脚本检测到本地副本会跳过下载。`Dockerfile` 构建时同时处理 `1.0.0` 和 `2.0.0` 两个版本，所以要覆盖哪个版本就把文件放进对应版本目录。

### 目录结构

按 `plugins/<插件名>/<版本>/plugin.wasm` 放置；`metadata.txt` 无需手动提供，构建时按实际文件自动（重）生成：

```
.                              ← 仓库根目录（构建上下文）
├── plugins.properties
└── plugins/
    └── my-plugin/
        ├── 1.0.0/
        │   └── plugin.wasm
        └── 2.0.0/
            └── plugin.wasm
```

## 参考文档

- 如何修改内置插件镜像地址：https://higress.cn/docs/latest/ops/how-tos/builtin-plugin-url/
- 如何使用 HTTP/HTTPS 协议加载 Wasm 插件：https://higress.cn/docs/latest/ops/how-tos/load-wasm-with-http/
