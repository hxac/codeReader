# 构建与本地运行方式

## 1. 本讲目标

通过本讲，你将掌握从源码把 NGINX Gateway Fabric（NGF）“造出来并跑起来”的完整链路。具体目标：

- 看懂 NGF 的 `Makefile`，知道它如何用 GNU Make 把“编译二进制 / 构建镜像 / 部署 / 测试 / 检查”统一成一组 `make` 目标。
- 能独立完成 `make build`，产出 `gateway` 二进制，并理解构建期把版本号“焊”进二进制的原理。
- 能用 `kind` 在本地拉起一个 Kubernetes 集群，把镜像加载进去，安装 CRD 并用 Helm 部署 NGF。
- 知道单元测试、`vet`、`lint` 等质量检查的入口与 `dev-all` 这个“提交前一键全检”目标。
- 理解二进制的默认目标平台为什么是 `linux/amd64`，以及这会如何影响你在 macOS / Windows 上直接运行二进制。

本讲承接 [u1-l2 目录结构与代码组织](u1-l2-repo-structure.md)：上一讲我们画出了仓库地图，知道入口在 `cmd/gateway`、产品逻辑在 `internal/controller`；本讲就回答“这些代码怎么变成一个能运行的程序”。

## 2. 前置知识

在开始前，建议先了解以下概念（不熟悉也没关系，本讲会顺带说明）：

- **GNU Make**：一个用“目标—依赖—命令”描述构建任务的工具。NGF 没有用复杂的构建系统，几乎所有的开发动作都封装在 `Makefile` 里，输入 `make <目标>` 即可。
- **Go 模块与 `go build`**：NGF 是 Go 项目，入口包是 `github.com/nginx/nginx-gateway-fabric/v2/cmd/gateway`。`go build` 会把这个包编译成一个可执行文件。
- **链接期变量注入（`-X` 链接参数）**：Go 可以在编译时用 `-ldflags '-X 包名.变量名=值'` 给源码里的字符串变量赋值。NGF 用它把版本号、遥测端点等在构建期写死进二进制。
- **容器镜像与多阶段构建**：NGF 的控制面镜像是“多阶段”的——先用 Go 镜像编译，再把二进制塞进一个极小的 `scratch` 镜像。
- **kind（Kubernetes IN Docker）**：用一个 Docker 容器模拟一个完整的 Kubernetes 集群，适合本地开发与测试，不用云上集群也能跑 NGF。
- **cobra**：Go 里最常用的命令行框架。NGF 的 `gateway` 二进制用 cobra 组织成若干“子命令”。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `Makefile` | 全仓库的开发入口：编译、镜像、kind 部署、CRD 安装、测试、lint 的所有目标都在这里。 |
| `cmd/gateway/main.go` | 二进制的 `main` 函数：装配根命令并注册 5 个子命令；声明构建期注入的版本/遥测变量。 |
| `cmd/gateway/commands.go` | 用 cobra 定义根命令与 5 个子命令（controller / generate-certs / initialize / sleep / endpoint-picker）。 |
| `build/Dockerfile` | 控制面镜像的多阶段构建定义，能看到二进制如何进入最终镜像。 |
| `config/cluster/kind-cluster.yaml` | 本地 kind 集群的配置（默认双栈 IPv4+IPv6）。 |
| `docs/developer/quickstart.md` | 官方开发快速上手文档，是本讲命令的权威来源。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 Makefile 关键目标**、**4.2 kind 部署流程**、**4.3 测试与 lint 入口**。

### 4.1 Makefile 关键目标

#### 4.1.1 概念说明

NGF 把所有“怎么造、怎么跑、怎么查”的动作都收敛进了唯一的 `Makefile`。这样做的好处是：无论你是想编译、打镜像、起集群、装 CRD、跑测试还是做 lint，都只需要 `make <目标>`，不用记一长串 `go build ...` 或 `docker build ...`。

这个 `Makefile` 有两个鲜明特点：

1. **它自带说明书**：默认目标（输入 `make` 不带参数时执行的目标）就是 `help`，会扫描整个文件、把所有带 `## 注释` 的目标和变量整齐打印出来。
2. **变量分两层**：文件顶部明确区分了“不该被用户覆盖的变量”（版本号、目录路径等）和“可以被用户覆盖的变量”（镜像名、架构、tag 等），后者用 `?=` 赋默认值，允许你在命令行用 `make GOARCH=arm64 build` 临时覆盖。

#### 4.1.2 核心流程

`Makefile` 的整体结构可以这样理解：

```text
顶部变量定义
  ├── 不可覆盖：VERSION=edge、目录路径、链接参数模板
  └── 可覆盖（? = 默认值）：GOARCH=amd64、GOOS=linux、TAG、OUT_DIR=build/out ...

目标（带 ## 注释的才会进 help）
  ├── build            → 编译出 build/out/gateway 二进制
  ├── build-images     → docker build 打 NGF + NGINX 两个镜像
  ├── create-kind-cluster / load-images / install-*-crds / helm-install-local
  ├── install-ngf-local-build  → 上面几步的“一键聚合”
  ├── unit-test / lint / vet / fmt
  └── dev-all          → 提交前“一键全检”
```

**编译流程**：`build` 目标调用 `go build`，把入口包 `.../cmd/gateway` 编译成 `$(OUT_DIR)/gateway`（即 `build/out/gateway`）。编译时通过 `-ldflags` 把 `main.version`、遥测周期、遥测端点等字符串变量注入进去——这就是为什么运行时二进制“知道”自己的版本。

**版本注入原理**：`VERSION` 默认是 `edge`，所以本地直接 `make build` 出来的二进制版本号就是 `edge`；正式发布时 CI 会把 `VERSION` 设成真实版本号（如 `1.4.6`）。

#### 4.1.3 源码精读

**默认目标与自述帮助**。文件第 66 行把 `help` 设为默认目标，第 72–75 行的 `help` 目标用 `grep + awk` 把所有带 `## ` 的目标和变量格式化输出：

[Makefile:66](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L66) — 把 `help` 设为默认目标，所以光敲 `make` 就能看到全部可用目标。

[Makefile:72-L75](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L72-L75) — `help` 目标：扫描 `## ` 注释，分别列出 Targets 与 Variables。这也是你在本讲后续遇到任何不确定目标时的查询入口。

**可覆盖变量与默认平台**。第 44 行起的注释 `# variables that can be overridden by the user` 标出可覆盖区，其中：

[Makefile:54-L55](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L54-L55) — `GOARCH ?= amd64`、`GOOS ?= linux`。**注意：默认编译出的是 Linux 二进制**，这一点对实践任务很关键（见 4.1.4）。

**版本注入的链接参数**。第 22–24 行定义了传给 `go build` 的 `-ldflags`：

[Makefile:22-L24](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L22-L24) — 用 `-X main.version=${VERSION}` 等参数，把版本号和遥测配置在链接期写进 `main.go` 里的同名变量。`-s -w` 则是去掉符号表和调试信息以缩小体积。

**编译目标本身**。第 135–140 行：

[Makefile:135-L140](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L135-L140) — `build` 目标。仅在 `TARGET=local`（默认）时执行；用 `CGO_ENABLED=0 GOOS=$(GOOS) GOARCH=$(GOARCH) go build ... -o $(OUT_DIR)/gateway` 产出二进制到 `build/out/gateway`。

**构建镜像**。第 96–98 行的 `build-ngf-image` 先依赖 `build`（先编译），再 `docker build`：

[Makefile:96-L98](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L96-L98) — `build-ngf-image: check-for-docker build`，调用 `docker build --target $(TARGET)`，用 `--target` 选择多阶段构建中的某一阶段。

**多阶段 Dockerfile**。`build/Dockerfile` 共有 4 个阶段：

[build/Dockerfile:1-L31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/build/Dockerfile#L1-L31) — `builder` 阶段（`golang:1.26`）直接 `RUN make build`；`common` 阶段是极小的 `scratch`，注入 `BUILD_AGENT` 环境变量并设 `ENTRYPOINT [ "/usr/bin/gateway" ]`；`container` 阶段从 `builder` 拷贝编译产物，`local` 阶段从本地 `./build/out/gateway` 拷贝。`--target` 决定走哪一条。

**二进制入口**。回到 `cmd/gateway/main.go`，第 8–18 行声明了那些构建期注入的变量，第 20–35 行是 `main`：

[cmd/gateway/main.go:8-L18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L8-L18) — `version`、`telemetryReportPeriod` 等变量，注释明确写着 `// Set during go build.`，与上面 `Makefile` 的 `-X` 注入一一对应。

[cmd/gateway/main.go:20-L35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L20-L35) — `main` 创建根命令，并用 `AddCommand` 注册 5 个子命令：`controller`、`generate-certs`、`initialize`、`sleep`、`endpoint-picker`。执行失败时把错误打到 stderr 并以退出码 1 结束。

#### 4.1.4 代码实践

**实践目标**：编译出 `gateway` 二进制，运行 `gateway --help` 看到 5 个子命令，并验证版本号注入。

**操作步骤**：

1. 在仓库根目录先看一遍可用目标与变量：
   ```bash
   make help
   ```
2. 编译二进制。如果你在 **Linux amd64** 机器上：
   ```bash
   make build
   ```
   如果你在 **macOS（Intel 或 Apple Silicon）**，默认 `GOOS=linux` 产出的二进制无法在本机直接运行，需要覆盖为目标主机的 OS：
   ```bash
   make GOOS=darwin build      # macOS
   # 或 make GOOS=darwin GOARCH=arm64 build  # Apple Silicon
   ```
3. 产物在 `build/out/gateway`。查看帮助与子命令：
   ```bash
   ./build/out/gateway --help
   ```
4. 验证版本号注入。由于 `controller` 子命令要求 `--gateway-ctlr-name`，我们换一种方式：直接在启动日志里看版本。可以只看帮助里是否出现子命令名，再观察二进制大小：
   ```bash
   ls -lh build/out/gateway
   ```
   > 说明：`controller` 启动时会在日志打印 `version`（来自注入的 `main.version`），本地默认值是 `edge`。完整启动需要 Kubernetes 环境，留到 4.2。

**需要观察的现象**：

- `make help` 输出分为 `Targets` 与 `Variables` 两段，每个目标都带一行说明。
- `gateway --help` 列出 5 个子命令：`controller`、`generate-certs`、`initialize`、`sleep`、`endpoint-picker`。
- 不覆盖 `GOOS` 在 macOS 上运行会报 `cannot execute binary file` 之类的错——这正是“默认编译 Linux 二进制”的体现。

**预期结果**：你得到一个可在目标平台运行的 `gateway` 二进制，并确认它是一个多子命令程序。

> 待本地验证：不同操作系统下 `gateway --help` 的精确排版可能因终端而略有差异；版本号 `edge` 需要启动 `controller` 才能在日志中确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `make build` 在 macOS 上默认产出的二进制无法直接运行？请从 `Makefile` 找到依据。

> **参考答案**：因为 `Makefile` 第 54–55 行把 `GOARCH` 默认设为 `amd64`、`GOOS` 默认设为 `linux`，`build` 目标（135–140 行）用这些值做交叉编译，产出的是 Linux 可执行文件，macOS 内核无法执行。需要 `make GOOS=darwin build` 覆盖。

**练习 2**：若想让本地构建的二进制版本号显示成 `1.0.0-demo` 而不是 `edge`，应该怎么构建？

> **参考答案**：`VERSION` 是不可覆盖区里 `VERSION = edge`（第 2 行，用的是 `=` 而非 `?=`），但它是普通变量，可在命令行覆盖：`make VERSION=1.0.0-demo build`。该值会经第 22 行的 `-X main.version=${VERSION}` 注入到 `main.version`。

**练习 3**：`build-ngf-image` 为什么依赖 `build`？这对镜像构建有什么好处？

> **参考答案**：见第 97 行 `build-ngf-image: check-for-docker build`。它先触发 `make build` 产出 `build/out/gateway`，然后 `Dockerfile` 的 `local` 阶段直接 `COPY ./build/out/gateway`（Dockerfile 第 26–27 行）。这样本地改动源码后打镜像，不必在容器里重新编译，构建更快；而 CI 走 `container` 阶段时则在 `builder` 阶段内 `RUN make build` 自行编译。

### 4.2 kind 部署流程

#### 4.2.1 概念说明

NGF 是一个 Kubernetes 控制器，必须在集群里运行才有意义。为了不让开发者每次都去申请云上集群，NGF 用 **kind** 在本地用 Docker 容器模拟出一个完整的 Kubernetes 集群。

完整部署一个本地 NGF 需要四件事按顺序完成：

1. **建集群**：`kind create cluster`，用一个配置文件指定 Kubernetes 版本与网络栈。
2. **加载镜像**：`kind load docker-image`，把上一步本地构建的镜像塞进 kind 节点（kind 节点不会去公网拉你的本地镜像）。
3. **安装 CRD**：Gateway API 的 CRD（GatewayClass / Gateway / HTTPRoute 等）必须先于控制器存在。
4. **用 Helm 部署 NGF**：把控制面 Deployment 与数据面 NGINX 部署起来。

`Makefile` 既提供了每一步的原子目标，也提供了把它们串起来的“一键”聚合目标。

#### 4.2.2 核心流程

```text
make install-ngf-local-build        ← 一键聚合（本地构建 + 部署）
        │
        ├─ build-images             ← 构建 NGF + NGINX 镜像（内含 make build）
        ├─ load-images              ← kind load docker-image 把两个镜像塞进节点
        └─ helm-install-local       ← Helm 安装
                ├─ install-gateway-crds   ← 先装 Gateway API CRD
                └─ helm install ...        ← 部署控制面 + 数据面
```

关键点：**镜像必须先 `kind load`，Helm 部署时镜像拉取策略要设成 `Never`**（默认 `PULL_POLICY ?= Never`，见 `Makefile` 第 50 行），否则节点会去远程仓库拉一个不存在的镜像而失败。

集群配置默认是**双栈（IPv4 + IPv6）**，写在 `config/cluster/kind-cluster.yaml`。

#### 4.2.3 源码精读

**创建 kind 集群**。第 211–214 行：

[Makefile:211-L214](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L211-L214) — `create-kind-cluster`：先校验 `kind` 是否安装，再 `kind create cluster --image kindest/node:$(KIND_K8S_VERSION) --config $(KIND_CONFIG_FILE)`。Kubernetes 版本由第 30 行的 `KIND_K8S_VERSION = v1.36.1` 固定。

**集群配置**。`config/cluster/kind-cluster.yaml`：

[config/cluster/kind-cluster.yaml:1-L7](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/config/cluster/kind-cluster.yaml#L1-L7) — 只有一个 `control-plane` 节点，`ipFamily: dual` 表示同时启用 IPv4 和 IPv6。若想只跑 IPv4/IPv6，可改这里的 `ipFamily`（quickstart 文档有示例）。

**加载镜像**。第 255–257 行：

[Makefile:255-L257](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L255-L257) — `load-images`：`kind load docker-image $(PREFIX):$(TAG) $(NGINX_PREFIX):$(TAG)`，把控制面镜像和 NGINX 数据面镜像一起塞进节点。

**安装 CRD**。第 161–163 行装 Gateway API CRD，第 157–159 行装 NGF 自带 CRD：

[Makefile:161-L163](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L161-L163) — `install-gateway-crds`：用 `kubectl kustomize` 渲染 `config/crd/gateway-api/standard`（或 `experimental`）后 `kubectl apply --server-side`。

[Makefile:157-L159](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L157-L159) — `install-crds`：渲染并安装 NGF 自己的 CRD（如 NginxProxy 等）。

**Helm 部署**。第 272–277 行：

[Makefile:272-L277](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L272-L277) — `helm-install-local`：先依赖 `install-gateway-crds` 保证 CRD 就绪；若启用推理扩展再装推理 CRD；最后 `helm install nginx-gateway $(CHART_DIR) ...`，通过一连串 `--set` 把本地镜像名/tag、`pullPolicy=Never`、Service 类型等注入 Helm。

**一键聚合**。第 263–264 行把前三步串起来：

[Makefile:263-L264](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L263-L264) — `install-ngf-local-build: build-images load-images helm-install-local`。这是本地开发最常用的“一条命令部署”。

#### 4.2.4 代码实践

**实践目标**：在本地 kind 集群里部署 NGF，并验证控制面 Pod 与 GatewayClass 就绪。

**操作步骤**：

1. 确保已安装 Docker、kind、kubectl、Helm（详见 `docs/developer/quickstart.md` 的 “Setup Your Development Environment”）。
2. 建集群：
   ```bash
   make create-kind-cluster
   ```
3. 一键构建并部署（含构建镜像、加载镜像、装 CRD、Helm 安装）：
   ```bash
   make install-ngf-local-build
   ```
4. 观察部署结果：
   ```bash
   kubectl -n nginx-gateway get pods
   kubectl get gatewayclass
   ```

**需要观察的现象**：

- `nginx-gateway` 命名空间下出现控制面 Pod 与 NGINX 数据面 Pod，最终都 `Running`。
- `gatewayclass` 列表里能看到 NGF 认领的 GatewayClass（其 `CONTROLLER` 为 `gateway.nginx.org/nginx-gateway-controller`，呼应 [u1-l1](u1-l1-project-overview.md) 讲过的控制器名）。

**预期结果**：NGF 在本地集群成功运行并就绪。

> 待本地验证：首次构建镜像耗时较长（要下载 Go 模块）；若镜像未 `kind load` 或 `pullPolicy` 不是 `Never`，Pod 会因 `ImagePullBackOff` 起不来。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Helm 安装时要显式 `--set nginxGateway.image.pullPolicy=Never`？

> **参考答案**：因为本地构建的镜像只存在于 kind 节点内部（通过 `kind load` 注入），并没有推送到任何远程仓库。若 `pullPolicy` 为 `Always` 或 `IfNotPresent` 且节点上没有缓存，kubelet 会去远程仓库拉取一个不存在的镜像而失败。`Makefile` 第 50 行 `PULL_POLICY ?= Never` 正是为了配合本地镜像。

**练习 2**：`helm-install-local` 为什么要把 `install-gateway-crds` 列为依赖？

> **参考答案**：控制器启动后会 watch GatewayClass/Gateway/HTTPRoute 等资源；若这些 CRD 尚未注册到集群，控制器无法建立 informer、甚至启动失败。第 273 行 `helm-install-local: ... install-gateway-crds` 确保先装 CRD 再起控制器。

### 4.3 测试与 lint 入口

#### 4.3.1 概念说明

一个能长期维护的项目必须有自动化质量门槛。NGF 在 `Makefile` 里提供了一组检查目标，并用 `dev-all` 把它们打包成“提交 PR 前一键跑全”的入口。常见的几类检查：

- **单元测试**（`unit-test`）：用 `go test` 跑 `cmd/...` 和 `internal/...` 的全部测试。
- **静态检查**（`vet`）：`go vet`，捕捉可疑代码结构。
- **代码风格检查**（`lint`）：用 `golangci-lint` 聚合大量 linter。
- **格式化**（`fmt`）：`go fmt`。
- **一键全检**（`dev-all`）：依赖整理 + 格式化 + vet + lint + 单测（含 njs 模块单测）。

`unit-test` 还特意带了 `-race`（竞态检测）和 `-shuffle=on`（随机化测试顺序）两个标志，这对一个大量使用并发（事件循环、缓存、leader 选举）的控制器尤为重要。

#### 4.3.2 核心流程

```text
make dev-all                ← 提交前一键全检
   ├─ deps        (go mod tidy / verify / download)
   ├─ fmt         (go fmt ./...)
   ├─ njs-fmt     (prettier 格式化 njs 模块)
   ├─ vet         (go vet ./...)
   ├─ lint        (golangci-lint run --fix)
   ├─ unit-test   (go test ./cmd/... ./internal/... -race -shuffle=on ...)
   └─ njs-unit-test
```

测试覆盖率会被写到 `coverage.out`，并生成 `cover.html` 供查看。

#### 4.3.3 源码精读

**单元测试**。第 239–242 行：

[Makefile:239-L242](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L239-L242) — `unit-test`：`go test ./cmd/... ./internal/... -buildvcs -race -shuffle=on -coverprofile=coverage.out -covermode=atomic`，随后用 `go tool cover -html` 生成可视化报告。`-race` 检测数据竞争，`-shuffle=on` 让测试顺序随机化以暴露隐含的依赖。

**lint**。第 235–237 行：

[Makefile:235-L237](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L235-L237) — `lint`：通过 `go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@$(GOLANGCI_LINT_VERSION) run --fix` 运行，版本由第 28 行 `GOLANGCI_LINT_VERSION = v2.12.2` 固定（用 `go run ...@版本` 免去本地预装）。

**vet**。第 231–233 行：

[Makefile:231-L233](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L231-L233) — `vet`：`go vet ./...`，Go 内置的静态检查。

**一键全检**。第 346–347 行：

[Makefile:346-L347](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L346-L347) — `dev-all: deps fmt njs-fmt vet lint unit-test njs-unit-test`，把开发期所有检查串成一个目标。

#### 4.3.4 代码实践

**实践目标**：跑一遍单元测试与 lint，并定位某个测试的位置。

**操作步骤**：

1. 跑单元测试（首次会编译较多依赖，稍慢）：
   ```bash
   make unit-test
   ```
2. 跑 lint（首次会下载 golangci-lint）：
   ```bash
   make lint
   ```
3. 查看覆盖率报告：
   ```bash
   # 在浏览器打开 cover.html
   ```
4. 挑一个包看它的测试文件，例如 `cmd/gateway` 下的 `*_test.go`，对照 `make unit-test` 跑过的内容。

**需要观察的现象**：

- `unit-test` 输出每个包的 `ok` 与耗时，末尾生成 `coverage.out` / `cover.html`。
- `lint` 若发现问题会直接报告文件与规则；`--fix` 会尝试自动修复可修复项。

**预期结果**：本地干净源码应能通过 `unit-test` 与 `lint`。

> 待本地验证：`-race` 会让测试变慢且更吃内存；在低配机器上可能需要较长时间。

#### 4.3.5 小练习与答案

**练习 1**：`unit-test` 加 `-shuffle=on` 的意义是什么？哪类 bug 只有靠它才容易暴露？

> **参考答案**：`-shuffle=on` 随机化测试执行顺序。它能暴露“测试之间隐含依赖”或“某个测试依赖另一个测试先跑留下的全局状态”的 bug——这类问题在固定顺序下永远通过，换台机器或换个顺序就偶发失败。

**练习 2**：为什么 `lint` 用 `go run ...@版本` 而不是直接 `golangci-lint run`？

> **参考答案**：这样不必要求开发者本地预装特定版本的 golangci-lint，`go run github.com/.../golangci-lint@$(GOLANGCI_LINT_VERSION)` 会按第 28 行固定的版本临时拉取并运行，保证所有人用同一套 lint 规则，避免“我这能过你那不能过”。

## 5. 综合实践

把本讲三个模块串起来，完成一次“从源码到运行”的完整闭环，并把每一步对应到 `Makefile` 的具体目标：

1. **查目标**：`make help`，找到 `build`、`build-images`、`create-kind-cluster`、`load-images`、`install-gateway-crds`、`helm-install-local`、`install-ngf-local-build`、`unit-test`、`lint`、`dev-all` 这些目标，确认它们在 `Makefile` 中的行号。
2. **编译并验证子命令**：`make GOOS=<你的主机OS> build`，运行 `./build/out/gateway --help`，确认 5 个子命令（对照 `cmd/gateway/main.go:23-L29`）。
3. **起集群 + 部署**：`make create-kind-cluster` 然后 `make install-ngf-local-build`。
4. **核对运行态**：`kubectl -n nginx-gateway get pods` 与 `kubectl get gatewayclass`，确认控制器名 `gateway.nginx.org/nginx-gateway-controller`（呼应 [u1-l1](u1-l1-project-overview.md)）。
5. **质量检查**：在改任何代码之前先 `make unit-test` 与 `make lint` 建立绿色基线。

完成后再回头画一张图：`源码 → make build → build/out/gateway → docker build → kind load → helm install → 运行中的 Pod`。这张图就是 NGF 控制面从代码到生产的全部路径，后续讲义（u2 CLI、u3 Manager 组装）都将在这条链路的“运行中的 Pod”内部展开。

## 6. 本讲小结

- NGF 用唯一的 `Makefile` 作为开发入口，`make help` 是自带的目标/变量说明书，默认目标就是 `help`。
- `make build` 产出 `build/out/gateway`，默认平台是 `linux/amd64`（`GOOS=linux`、`GOARCH=amd64`），在 macOS 上需覆盖 `GOOS` 才能直接运行。
- 版本号与遥测配置通过 `-ldflags '-X main.version=...'` 在构建期注入 `main.go` 的同名变量，本地默认版本为 `edge`。
- 二进制是 cobra 多子命令程序：`controller`（控制面主命令）、`generate-certs`、`initialize`、`sleep`、`endpoint-picker`。
- 本地部署链路：`create-kind-cluster` → `build-images` → `load-images` → `install-gateway-crds` → `helm-install-local`，`install-ngf-local-build` 一键聚合；本地镜像必须配 `pullPolicy=Never`。
- 质量门槛：`unit-test`（带 `-race -shuffle=on`）、`vet`、`lint`，`dev-all` 是提交前一键全检。

## 7. 下一步学习建议

本讲让 NGF “跑起来”了，但还没解释 `controller` 子命令启动后做了什么。建议下一步：

- 学习 [u2-l1 命令行子命令总览](u2-l1-cli-subcommands-overview.md)：深入 `cmd/gateway/commands.go`，逐一弄清 5 个子命令各自的职责与处理函数入口。
- 再进入 [u2-l2 controller 命令与运行时配置](u2-l2-controller-command-and-config.md)：看清楚一长串 flag 如何汇聚成 `config.Config`，又是如何交给 `controller.StartManager` 的。
- 阅读源码时，可配合 `docs/developer/quickstart.md` 与 `Makefile` 互相印证，遇到陌生 `make` 目标就 `make help` 查注释。
