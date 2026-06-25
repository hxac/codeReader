# 容器镜像与多变体构建（Dockerfile）

## 1. 本讲目标

上一讲（u1-l2）你已经学会用 `make build` 在本机编译出 `nginx-ingress` 二进制。但生产环境里，这个二进制要和 **NGINX 本体**、**njs/OIDC 脚本**、**模板文件**一起装进容器镜像才能真正运行。

学完本讲，你应当能够：

1. 读懂 `build/Dockerfile` 的**多阶段构建**，说清从「编译 Go 二进制」到「组装出可运行镜像」经过了哪些阶段。
2. 理解本项目为什么有 **OSS / NGINX Plus / NAP WAF / DoS / FIPS** 这么多镜像变体，以及它们是靠什么变量切换的。
3. 看懂 Makefile 里 `debian-image`、`alpine-image-plus`、`ubi-image-nap-plus` 这一类 target 是如何驱动 `docker build` 的；尤其理解**本次更新后所有 OSS 变体都改为「从基础镜像从零安装 NGINX 包」**（不再叠加官方 `nginx:` 镜像）。
4. 说清 NGINX Plus 的**付费许可证凭据**为什么用 `--mount=type=secret` 挂载，而不是 `COPY` 进镜像——这是本项目的核心安全约定之一。

> 本讲承接 u1-l2（构建方式）。本讲不涉及 Go 编译细节，只关心「二进制如何变成镜像」。
>
> **本次更新提示**：相比上一版本，OSS 镜像构建方式有重要变化（详见 4.2），核心架构（多阶段、`BUILD_OS` 切换、凭据安全）保持不变。

---

## 2. 前置知识

### 什么是多阶段构建（multi-stage build）

一个 Dockerfile 里可以写多个 `FROM`，每个 `FROM` 开启一个**阶段（stage）**，每个阶段都可以起一个名字（`FROM ... AS 名字`）。后面阶段可以用 `COPY --from=名字` 把前面阶段的产物拷过来。

好处是：**编译环境**（带完整 Go 工具链、几个 GB）和**运行环境**（只放二进制 + NGINX）可以分离。最终镜像只包含最后一个阶段的层，工具链、源码、缓存都被丢弃，镜像又小又安全。

### 什么是 BuildKit

BuildKit 是 Docker 的新一代构建引擎（`DOCKER_BUILDKIT=1`）。本项目的 Dockerfile 重度依赖它提供的两个能力：

- `--mount=type=bind,from=...`：把另一个阶段的内容**临时挂载**进当前 `RUN`，而不写入镜像层。
- `--mount=type=secret,id=...`：把构建机器上的**密钥文件**临时挂载进 `RUN`，用完即焚，**绝不进入镜像层**。
- `ADD --link` / `COPY --link`：让新增内容成为独立层，提升缓存命中率。

Dockerfile 第 1 行 `# syntax=docker/dockerfile:1.25` 就是在声明「用这个版本的 frontend，解锁上述语法」。

### 「从零构建」是什么意思

NGINX 官方在 Docker Hub 上发布了预装好 NGINX 的镜像（如 `nginx:1.31.2`、`nginx:1.31.2-alpine3.23`）。历史上，本项目的 OSS Alpine/Debian 镜像**直接基于这些官方镜像**，再叠加 `nginx-module-otel`、`nginx-agent` 等组件。

本次更新（提交链 `fix/oss-build-optimisation`，关键提交 `build OSS images from scratch`、`remove DockerHub images`）改变了这一做法：**所有 OSS 变体都不再依赖 Docker Hub 上的官方 `nginx:` 镜像**，而是从一个**纯净的基础镜像**（`alpine:3.23`、`debian:trixie-slim`、`ubi-minimal`）出发，自行从 `nginx.org` / `packages.nginx.org` 仓库用包管理器（`apk`/`apt`/`microdnf`）安装 `nginx` 及其模块。UBI 本来就是这种方式，现在 Alpine/Debian 也统一成同样思路。

好处：摆脱对第三方预装镜像的依赖，所有发行版的 OSS 构建逻辑统一、可控、可审计。

### 几个名词

| 名词 | 含义 |
| --- | --- |
| NGINX（OSS） | 开源版 NGINX |
| NGINX Plus | 商业版，提供动态 API、健康检查等增强能力 |
| NAP（NGINX App Protect） | 基于 Plus 的 WAF / DoS 防护模块，分 v4 / v5 |
| FIPS | 美国联邦信息处理标准，启用经过认证的加密实现 |
| UBI | Red Hat 的 Universal Base Image，面向企业合规 |

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `build/Dockerfile` | 唯一的镜像定义文件，包含全部阶段与全部变体 |
| `Makefile` | 定义 `debian-image`、`alpine-image-plus` 等驱动 `docker build` 的 target |
| `build/scripts/common.sh` | 最终装配脚本：拷模板、njs、OIDC，设置 nginx 二进制权限 |
| `build/scripts/*.sh` | 各 OS 的辅助脚本（`agent.sh`、`nap-waf.sh`、`ubi-setup.sh` 等），被 Dockerfile 调用 |
| `build/README.md` | 指向官方构建文档 |

---

## 4. 核心概念与源码讲解

### 4.1 多阶段 Dockerfile：从一个文件编译出整个镜像

#### 4.1.1 概念说明

本项目的 `build/Dockerfile` 是一个**超长但结构清晰**的多阶段文件：它用一长串 `FROM ... AS 阶段名` 把「下载依赖」「装 NGINX」「编译 Go 二进制」「最终装配」切分成几十个命名阶段，最后再由 Makefile 用 `--target 阶段名` 选择要产出哪一种镜像。

理解它的关键有两点：

1. **阶段之间用名字引用**：前一个阶段（如 `nginx-files`）的产物，会被后面阶段用 `--mount=type=bind,from=nginx-files` 临时挂载使用，而不会污染最终镜像。
2. **最终镜像只来自一条链**：无论 Dockerfile 里写了多少阶段，最终镜像的层只由 `--target` 指定的那个阶段及其 `FROM` 祖先决定。

#### 4.1.2 核心流程

可以把它想象成一条「流水线 + 一个总装车间」：

```
 ┌───────────────────────────────────────────────────────────────┐
 │  依赖层（不被打包进最终镜像，仅供其它阶段挂载使用）                  │
 │  golang-builder  alpine-fips  ubi10-packages  ubi-minimal       │
 │  nginx-files(scratch: 仓库密钥/脚本/.repo 文件)                  │
 └───────────────────────────────────────────────────────────────┘
                              │ 挂载引用
                              ▼
 ┌───────────────────────────────────────────────────────────────┐
 │  NGINX 运行基底（按 BUILD_OS 二选一/多选一）                       │
 │  alpine / debian / ubi            （OSS：从基础镜像从零装 nginx+模块）│
 │  alpine-plus / debian-plus / ...  （Plus：用密钥拉 nginx-plus 包） │
 │  *-nap-* / *-nap-v5-*             （再叠加 NAP WAF/DoS）          │
 └───────────────────────────────────────────────────────────────┘
                              │ FROM ${BUILD_OS}
                              ▼
 ┌───────────────────────────────────────────────────────────────┐
 │  common  —— 所有变体共享的最终装配                                │
 │  运行 common.sh：拷模板/njs/OIDC、设 nginx 权限、EXPOSE、ENTRYPOINT │
 └───────────────────────────────────────────────────────────────┘
                              │ COPY 二进制
                              ▼
 ┌───────────────────────────────────────────────────────────────┐
 │  最终产物阶段：local / container / goreleaser / aws / debug       │
 └───────────────────────────────────────────────────────────────┘
```

> 注意上图 OSS 一行相比上一版已更新：Alpine/Debian 不再「在官方镜像上加 otel+agent」，而是**与 UBI 一样从基础镜像从零安装** NGINX 及模块（njs/otel 等）。

「编译 Go 二进制」是**另一条平行支线**（`builder` 阶段），它不依赖 `common`，而是被最终产物阶段用 `COPY --from=builder` 引用。

#### 4.1.3 源码精读

Dockerfile 开头声明语法版本和一批构建参数。`BUILD_OS=debian` 是默认值，也是贯穿全场的「变体切换轴」：

[build/Dockerfile:1-16](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L1-L16) — 声明 BuildKit frontend 版本、各组件版本号（`NGINX_OSS_VERSION`、`NGINX_PLUS_VERSION`、NAP 版本）和最关键的 `ARG BUILD_OS=debian`。

第一个有意思的阶段是 `nginx-files`，它基于 `scratch`（空镜像），只负责把 NGINX 官方仓库的 GPG 密钥、`.repo` / `.sources` 文件、以及本地的辅助脚本收集到一起：

[build/Dockerfile:27-36](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L27-L36) — `FROM scratch AS nginx-files`，用 `ADD --link` 从远程 URL 拉取仓库密钥与 repo 文件。

> 注意：这些密钥/repo 文件之后会被**挂载**进其它阶段用于 `apt`/`apk`/`dnf` 下载包，但 `nginx-files` 这个阶段本身不会被 `--target` 选中，所以它永远不会出现在最终镜像里。

「编译 Go 二进制」由独立的 `builder` 阶段完成，它挂载源码进容器、用 `go build` 编译，并设置 `cap_net_bind_service` 能力（让非 root 的二进制能绑定 80/443 端口）：

[build/Dockerfile:803-815](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L803-L815) — `FROM golang-builder AS builder`，`CGO_ENABLED=0 GOOS=linux go build ... -ldflags "-s -w -X main.version=${IC_VERSION}"`，版本号通过 ldflags 在编译期注入（与 u1-l2 讲的 `make build` 一致）。

真正「总装」的是 `common` 阶段，它 `FROM ${BUILD_OS}`——即从上面某一类 NGINX 基底继续，做所有变体都要做的事：

[build/Dockerfile:772-793](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L772-L793) — `FROM ${BUILD_OS} AS common`：调用 `common.sh` 装配文件、`EXPOSE 80 443`、`ENTRYPOINT ["/nginx-ingress"]`、`USER 101`。

最后的产物阶段都「站在 common 肩膀上」，只是二进制来源不同：

[build/Dockerfile:831-846](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L831-L846) — `container`（从 `builder` 阶段拷 Docker 编译的二进制）与 `local`（从宿主机拷本机编译的二进制）。

#### 4.1.4 代码实践

**实践目标**：在阅读源码的基础上，亲手把 Dockerfile 的阶段结构整理成一张表。

**操作步骤**：

1. 打开 `build/Dockerfile`。
2. 用搜索定位所有 `^FROM ` 开头的行（即每个阶段起点）。
3. 为每个阶段记录三列：阶段名、基础镜像、一句话作用。

**需要观察的现象**：

- `FROM` 数量远多于「实际产出镜像」的种类——大部分阶段是「中间件」或「依赖件」，只服务于别的阶段。
- 最终会被 `--target` 选中的「产物阶段」集中在文件末尾（`container`、`local`、`debug`、`goreleaser`、`aws`、`download`）。
- `common` 是几乎所有产物阶段的共同祖先。

**预期结果**：你会得到一张约 30 行的阶段表，并能指出「`common` 之前按 `BUILD_OS` 分叉、`common` 之后按二进制来源分叉」。

**待本地验证**：若想确认哪个阶段是最终层，可执行 `docker buildx imagetools inspect` 查看镜像层（需要能访问镜像仓库，本机不一定具备，可标注待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nginx-files` 用 `FROM scratch` 而不是 `FROM debian`？

**参考答案**：`nginx-files` 只是一堆密钥/repo/脚本的「打包盒」，本身不含任何运行环境，用 `scratch` 能让它体积最小、且语义上明确表示「这不是一个可运行的镜像，只是一个文件集合」，且它不会被 `--target` 选中为产物。

**练习 2**：`builder` 阶段和 `common` 阶段之间有 `FROM` 依赖吗？

**参考答案**：没有。`builder` 基于 `golang-builder`，`common` 基于 `${BUILD_OS}`，两条支线相互独立。它们只在最终产物阶段（如 `container`）通过 `COPY --from=builder` 才汇合。

---

### 4.2 镜像变体矩阵：BUILD_OS 作为切换轴（含 OSS 从零构建）

#### 4.2.1 概念说明

NGINX Ingress Controller 要同时满足开源用户（用 OSS NGINX）、付费用户（用 Plus）、需要 WAF/DoS 的企业、以及金融合规（FIPS、UBI）等众多场景。如果每个场景写一个 Dockerfile，会有几十份几乎重复的文件难以维护。

本项目的设计是：**只用一个 Dockerfile，用 `BUILD_OS` 这个变量来切换运行基底**。Makefile 里的每个 image target，本质上是带着不同的 `--build-arg BUILD_OS=...` 去构建同一个 Dockerfile。

**本次更新的核心变化**：三个 OSS 基底（`alpine`、`debian`、`ubi`）现在**全部采用「从零构建」**——即从一个不含 NGINX 的纯净基础镜像出发，自行安装 `nginx` 及所需模块。此前 Alpine/Debian 是叠加在官方 `nginx:` 镜像上的，现已改为与 UBI 一致的方式（提交 `build OSS images from scratch`、`remove DockerHub images`）。

#### 4.2.2 核心流程

`BUILD_OS` 的取值是一个组合维度，可以拆成几个正交的轴：

```
基础发行版：    alpine        debian（默认）    ubi
是否 Plus：    （OSS）         -plus            -plus        （OSS：ubi）
是否 NAP：                     -nap             -nap-v5      （空表示无 WAF/DoS）
是否 FIPS：    -fips          （Alpine 专用）
Agent 版本：                  (-agent 表示 Agent v3，无后缀表示 v2)
```

把 `BUILD_OS` 代入 `common` 阶段的 `FROM ${BUILD_OS}`，就选中了对应的 NGINX 基底。例如：

- `BUILD_OS=debian` → `FROM debian`（OSS NGINX on Debian，从 `debian:trixie-slim` 从零安装）
- `BUILD_OS=debian-plus` → `FROM debian-plus`（Plus on Debian）
- `BUILD_OS=alpine-plus-nap-fips-agent` → `FROM alpine-plus-nap-fips-agent`（Plus + NAP WAF v4 + FIPS + Agent v3 on Alpine）

#### 4.2.3 源码精读

先看三个 OSS 基底——**现在三者的构建思路完全一致：都从纯净基础镜像出发，用各自的包管理器从 nginx.org/packages.nginx.org 仓库安装 NGINX 及模块**。

Alpine OSS 阶段从 `alpine:3.23` 出发，用 `apk add -X <仓库>` 把 `nginx`、`nginx-module-njs`、`nginx-module-otel` 和 `nginx-agent` 从 NGINX 官方仓库装进来（注意 `~${NGINX_OSS_VERSION}` 这种版本锁定语法）：

[build/Dockerfile:92-110](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L92-L110) — `FROM alpine:3.23 ... AS alpine`，`apk add` 安装基础依赖（openssl/curl/ca-certificates 等），再用 `apk add -X ".../mainline/alpine/..."` 从 NGINX 仓库装 `nginx~${NGINX_OSS_VERSION}`、`nginx-module-njs`、`nginx-module-otel`，以及来自 agent 仓库的 `nginx-agent`。

Debian OSS 阶段同理，从 `debian:trixie-slim` 出发，先把 nginx.org 仓库加进 apt 源，再 `apt-get install nginx nginx-module-njs nginx-module-otel nginx-agent`：

[build/Dockerfile:113-138](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L113-L138) — `FROM debian:trixie-slim ... AS debian`，导入 GPG 签名密钥、写入 apt 源后，`apt-get install ... nginx=${NGINX_OSS_VERSION}* nginx-module-njs=... nginx-module-otel=... nginx-agent=...`。

UBI OSS 阶段则用 `microdnf` 从 NGINX 的 centos 仓库安装（这次更新还顺手把原来挤在一行的 `microdnf install` 重排成多行，仅可读性变化，逻辑不变）：

[build/Dockerfile:140-189](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L140-L189) — `FROM ubi-minimal AS ubi`，安装 `nginx`、`nginx-module-njs/otel/image-filter/xslt`、`nginx-agent`，并打上 UBI 必需的 Red Hat LABEL。

> 这三段现在风格统一：**纯净基础镜像 + 挂载签名密钥/repo + 用包管理器装 NGINX 及模块**。这是本次「OSS 镜像构建优化」的核心，也解释了为什么 `nginx-module-njs` 现在在三个发行版里都被显式安装——以前 Alpine/Debian 靠官方镜像自带 njs，现在要自己装。

再看 Plus 基底。Plus 版 NGINX 不在公共镜像里，必须从 `pkgs.nginx.com` 下载——这就需要凭据（见 4.4）。Alpine Plus 阶段把 Plus 仓库地址追加到 `/etc/apk/repositories` 后 `apk add nginx-plus ...`：

[build/Dockerfile:196-215](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L196-L215) — `FROM alpine:3.22 ... AS alpine-plus`，注意第一行 `RUN` 就挂载了 `type=secret,id=nginx-repo.crt/key`。

`common.sh` 里还有一处用 `BUILD_OS` 区分 OSS 与 Plus 的逻辑——它根据 `BUILD_OS` 是否含 `plus` 来决定拷贝 `nginx.tmpl` 还是 `nginx-plus.tmpl`，以及是否需要 OIDC 模板：

[build/scripts/common.sh:5-14](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/scripts/common.sh#L5-L14) — `if [ -z "${BUILD_OS##*plus*}" ]` 时拷 Plus 模板与 `oidc.tmpl`。

#### 4.2.4 代码实践

**实践目标**：亲手验证三个 OSS 阶段都「从零构建」，并能说出各自的基础镜像与安装命令。

**操作步骤**：

1. 打开 `build/Dockerfile`，分别定位 `AS alpine`、`AS debian`、`AS ubi` 三个阶段。
2. 对每个阶段记录两列：① 它的 `FROM` 基础镜像是什么；② 它用哪条命令安装 `nginx` 主程序（`apk add` / `apt-get install` / `microdnf install`）。
3. 查看 Makefile 的 `all-images`（见 4.3.3）列出的全部变体 target，对其中任意三个 target，拆解它的 `BUILD_OS` 取值，推导出：发行版 / 是否 Plus / 是否 NAP / NAP 版本 / Agent 版本。

**需要观察的现象**：

- 三个 OSS 阶段的 `FROM` 都**不是** `nginx:` 官方镜像，而是 `alpine:3.23` / `debian:trixie-slim` / `ubi-minimal`。
- 三个阶段都**显式安装 `nginx` 和 `nginx-module-njs`**（以前 Alpine/Debian 不是这样）。
- target 名（如 `alpine-image-nap-v5-plus-fips-agent`）几乎就是 `BUILD_OS` 的可读镜像。

**预期结果**：能写出类似「`debian-image-nap-dos-plus-agent` = Debian + Plus + NAP WAF&DoS + Agent v3」的映射，并指出三个 OSS 变体的「从零构建」三要素（基础镜像 + 仓库 + 安装命令）。

**待本地验证**：可选地执行 `make -n debian-image` 查看 Makefile 实际展开的 `docker build` 命令，确认 `--build-arg BUILD_OS` 与你的推导一致（`-n` 只打印不执行，安全）。

#### 4.2.5 小练习与答案

**练习 1**：本次更新后，`alpine`、`debian`、`ubi` 三个 OSS 阶段的 `FROM` 分别是什么？为什么三个阶段都要显式安装 `nginx-module-njs`？

**参考答案**：`alpine` 的 `FROM alpine:3.23`、`debian` 的 `FROM debian:trixie-slim`、`ubi` 的 `FROM ubi-minimal`——三者都是**不含 NGINX 的纯净基础镜像**。正因为不再依赖「官方 `nginx:` 镜像里自带的 njs」，所有需要的模块（包括 njs）都必须由各阶段自行从 nginx.org 仓库显式安装，所以三个阶段现在都装 `nginx-module-njs`。这是「OSS 从零构建」带来的必然结果。

**练习 2**：为什么 NAP 相关的阶段常常成对出现（一个带 `-agent`，一个不带）？

**参考答案**：`-agent` 后缀代表安装 `nginx-agent` 的 **v3**，不带后缀则安装 **v2**。NGINX Agent 有两个主版本仍在维护，企业可能因对接的管控平台不同而选择其一，所以为每个 NAP 基底各产出 v2/v3 两个镜像。

---

### 4.3 构建脚本与 Makefile 目标：如何驱动 docker build

#### 4.3.1 概念说明

Dockerfile 里几十个阶段，到底构建哪一个、带哪些参数？这件事交给 Makefile 统一编排。Makefile 提供了一组语义化的 target（`debian-image`、`alpine-image-plus` 等），它们本质都是「拼好一条 `docker build` 命令」，区别只在 `--build-arg BUILD_OS=...` 和是否带凭据。

本次更新还**重组了 Makefile 的 image target 分组**：新增了 `###### NIC + NGINX OSS Images (built from scratch) ######` 与 `###### NIC + NGINX PLUS Images ######` 两个注释分组，把三个 OSS 目标（`alpine-image`/`debian-image`/`ubi-image`）归到一起，并相应更新了 help 文案。

#### 4.3.2 核心流程

Makefile 先定义好「docker build 的命令模板」，再让每个 image target 填入自己的参数：

```
DOCKER_CMD = docker build
              --platform linux/$(ARCH)
              $(DOCKER_BUILD_OPTIONS)        # 含 IC_VERSION、PACKAGE_REPO
              --target $(TARGET)             # 默认 local：拷宿主机二进制
              -f build/Dockerfile
              -t $(BUILD_IMAGE) .

debian-image:                            # 注意：本次更新后不再依赖 build
	$(DOCKER_CMD) --build-arg BUILD_OS=debian ...
```

要点：

1. **默认 TARGET=local**：即默认把「本机 `make build` 出来的 `./nginx-ingress`」拷进镜像（对应 Dockerfile 的 `local` 阶段）。
2. **大部分 image target 先依赖 `build`**：先在本机编译二进制，再装进镜像。**本次更新的例外**：OSS 的 `alpine-image` 与 `debian-image` 去掉了对 `build` 的前置依赖（`ubi-image` 及所有 `*-plus*` 目标仍保留 `: build`）。因此直接 `make alpine-image` 时，需自行确保宿主机已 `make build` 出二进制——否则 `local` 阶段的 `COPY nginx-ingress /` 会因找不到文件而失败。
3. **变体由 `--build-arg BUILD_OS` 决定**，Plus 变体额外注入凭据参数 `PLUS_ARGS`。
4. **`all-images`** 是一个循环，依次构建全部 25 个变体。

#### 4.3.3 源码精读

先看命令模板与默认值：

[Makefile:33](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/Makefile#L33) — `TARGET ?= local`，默认用宿主机二进制装配。

[Makefile:62-64](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/Makefile#L62-L64) — `DOCKER_CMD` 模板定义，以及 `export DOCKER_BUILDKIT = 1` 强制启用 BuildKit（secrets/mount 语法依赖它）。

新分组下的三个 OSS image target（本次更新把它们集中到了一起，且 `alpine-image`/`debian-image` 不再带 `build` 前置依赖）：

[Makefile:187-209](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/Makefile#L187-L209) — 「NIC + NGINX OSS Images (built from scratch)」分组：`alpine-image`、`debian-image`、`ubi-image`。三者都只传 `BUILD_OS` 与 OSS 版本号，**不带凭据**；注意 `ubi-image` 仍带 `: build`，而另两者不带。

Plus 变体代表：

[Makefile:240-242](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/Makefile#L240-L242) — `debian-image-plus`：多了 `$(PLUS_ARGS)`，即注入 Plus 凭据与版本。

`PLUS_ARGS` 把两个本地文件以 BuildKit secret 形式传进去（注意是 `--secret` 而非 `COPY`）：

[Makefile:14](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/Makefile#L14) — `PLUS_ARGS = --build-arg NGINX_PLUS_VERSION=... --secret id=nginx-repo.crt,src=nginx-repo.crt --secret id=nginx-repo.key,src=nginx-repo.key`。

UBI + NAP 变体还会额外带 Red Hat 的 `rhel_license` secret：

[Makefile:290-293](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/Makefile#L290-L293) — `ubi-image-nap-plus` 同时挂载 `nginx-repo.*` 与 `rhel_license` 两类凭据。

最后是「一把梭」的 `all-images`：

[Makefile:336-342](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/Makefile#L336-L342) — 列出全部 25 个变体 target，在循环里为每个变体打上带后缀的 tag 依次构建（用于 CI 一次性产出全部镜像）。

> 顺带一提，二进制来源也能切：CI 发布时常用 `TARGET=goreleaser`（从 GoReleaser 产物拷）或 `TARGET=aws`（AWS Marketplace 专用产物），对应 Dockerfile 末尾的 `goreleaser` / `aws` 阶段（见 [build/Dockerfile:936-941](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L936-L941) 与 [build/Dockerfile:980-986](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L980-L986)）。

#### 4.3.4 代码实践

**实践目标**：在不真正构建镜像的前提下，看到 Makefile 会执行的完整 `docker build` 命令，并验证 OSS 目标不带 `--secret`。

**操作步骤**：

1. 在仓库根目录执行 `make -n debian-image`（`-n` 表示 dry-run，只打印命令不执行）。
2. 再执行 `make -n debian-image-plus`。
3. 对比两次输出的 `docker build ...` 命令，重点看 `--build-arg BUILD_OS=` 和 `--secret` 的差异。

**需要观察的现象**：

- OSS 变体命令里**没有**任何 `--secret`。
- Plus 变体命令里出现了 `--secret id=nginx-repo.crt,src=nginx-repo.crt --secret id=nginx-repo.key,src=nginx-repo.key`。
- 两条命令的 `-f build/Dockerfile` 完全相同——确认是同一个 Dockerfile。

**预期结果**：直观看到「同一份 Dockerfile，靠 build-arg + secret 切换变体」。

**待本地验证**：若本机没装 docker，则只能人工对照源码推导，标注待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `debian-image` 不带 `--secret`，而 `debian-image-plus` 带？

**参考答案**：OSS NGINX 的包来自公共仓库（本次更新后，Alpine/Debian 也改为从 `nginx.org` 的公共 mainline 仓库用 `apk`/`apt` 下载），无需鉴权；Plus 包来自付费仓库 `pkgs.nginx.com`，必须用 `nginx-repo.crt/.key` 鉴权才能下载，所以需要把凭据作为 secret 传入。

**练习 2**：如果把 `TARGET` 从默认的 `local` 改成 `container`，镜像构建会有什么不同？

**参考答案**：`local` 会先用本机 `make build` 编译二进制再 `COPY` 进镜像；`container` 则跳过本机编译，改由 Dockerfile 内的 `builder` 阶段在容器里用 `go build` 编译（见 4.1.3）。后者不依赖宿主机的 Go 环境，更利于在干净 CI 环境里复现。

---

### 4.4 凭据安全约定：Plus license 为何不进镜像层

#### 4.4.1 概念说明

NGINX Plus 是商业产品，下载它的包需要一对客户证书（`nginx-repo.crt` / `nginx-repo.key`）。这对证书是**高敏感凭据**——任何拿到它的人都能冒充该客户从付费仓库拉取 Plus。

如果在 Dockerfile 里写 `COPY nginx-repo.crt /etc/ssl/nginx/`，这对证书就会被**永久烤进镜像的某一层**。即便后续 `RUN rm` 删掉，Docker 镜像是分层的、层不可变，证书依然存在于历史层里，任何人 `docker pull` 后都能用 `docker history` 或解包层文件把它挖出来。

本项目（也是项目的核心安全不变量之一，记录在 `CLAUDE.md` 里）的约定是：**Plus 凭据永远用 BuildKit secret 挂载，绝不 `COPY` 进镜像**。

#### 4.4.2 核心流程

BuildKit secret 的工作机制：

```
 构建机器                              构建中的容器（某个 RUN 期间）
 ┌──────────────┐    --secret id=    ┌────────────────────────────┐
 │ nginx-repo.crt│ ───────────────▶ │ 挂载到 /etc/apk/cert.pem   │
 │ nginx-repo.key│   (临时挂载)       │ 仅此 RUN 可见，用完即焚      │
 └──────────────┘                    └────────────────────────────┘
                                              │ RUN 结束
                                              ▼
                                      证书不进入任何镜像层 ✅
```

- `--secret` 只在声明了 `--mount=type=secret` 的那一条 `RUN` 执行期间，把文件以 tmpfs 形式挂载进来。
- 该 `RUN` 结束后，挂载点消失，提交到镜像层的「文件系统快照」里**不含**这些密钥。
- 因此最终镜像的任何一层都不包含 `nginx-repo.*`。

#### 4.4.3 源码精读

Makefile 这边把本地证书文件声明为 secret（而非 build-arg，更非 COPY）：

[Makefile:14](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/Makefile#L14) — `--secret id=nginx-repo.crt,src=nginx-repo.crt --secret id=nginx-repo.key,src=nginx-repo.key`。

Dockerfile 这边在需要下载 Plus 包的 `RUN` 里，用 `--mount=type=secret,id=...` 把证书挂到 apt/apk/dnf 期望的位置。Alpine Plus 阶段把证书挂到 apk 的客户端证书路径：

[build/Dockerfile:203-215](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L203-L215) — `--mount=type=secret,id=nginx-repo.crt,dst=/etc/apk/cert.pem` 与 `...key,dst=/etc/apk/cert.key`，紧接着 `apk add nginx-plus ...`。这两行挂载**只在当前 RUN 生效**。

Debian 的 Plus 安装位于中间阶段 `debian-plus-only`，把证书挂到 apt 的客户端证书路径后 `apt-get install ... nginx-plus ...`：

[build/Dockerfile:385-404](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L385-L404) — `--mount=type=secret,id=nginx-repo.crt,dst=/etc/ssl/nginx/nginx-repo.crt` 与 `.key`，然后 `apt-get install ... nginx-plus nginx-plus-module-njs ...`。

UBI Plus 阶段则挂到 dnf/yum 的客户端证书路径：

[build/Dockerfile:571-594](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L571-L594) — UBI Plus 阶段（`FROM ubi-minimal AS ubi-10-plus`）同样用 `type=secret` 挂证书后 `microdnf install nginx-plus ...`。

> 三个发行版的挂载目标路径不同（apk/apt/dnf 各自的约定），但**手法完全一致**：都是 BuildKit secret 临时挂载，从不持久化。对比 OSS 阶段（如 [build/Dockerfile:92-110](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L92-L110)）则完全没有 `type=secret`——因为 OSS 包不需鉴权（即便本次更新后改从 `nginx.org` 安装，用的仍是公开仓库、无需客户证书）。

一个旁证：`nginx-files` 阶段拉下来的仓库 GPG 公钥（`nginx_signing.key`，见 [build/Dockerfile:34-36](https://github.com/nginxinc/kubernetes-ingress/blob/a4e23f43afdb055a5426712af0a347282fbc770c/build/Dockerfile#L34-L36)）是**公开的签名密钥**，不是敏感凭据，所以可以用 `ADD` 直接放进阶段；而 Plus 的 `nginx-repo.crt`（客户私有证书）则必须用 secret——这正体现了「公开 vs 私有」的区别对待。

#### 4.4.4 代码实践

**实践目标**：验证「Plus 凭据确实不会出现在最终镜像层」，并理解其机制。

**操作步骤**：

1. 在 Dockerfile 中搜索 `type=secret`，记录它出现在哪些阶段（提示：全部是 `*-plus*` / `*-nap*` 阶段）。
2. 在 Dockerfile 中搜索 `COPY .*nginx-repo` 或 `ADD .*nginx-repo.crt`，确认**搜不到**任何把客户证书持久化进镜像的写法。
3. （可选，需有 Plus 凭据）执行 `make debian-image-plus` 后，用 `docker history <镜像>` 或 `dive <镜像>` 检查每一层，确认没有 `nginx-repo.crt/.key` 文件残留。

**需要观察的现象**：

- `type=secret` 仅出现在安装 Plus/NAP 包的 `RUN` 里。
- 没有任何 `COPY/ADD` 把 `nginx-repo.*` 写入镜像。
- （可选）`docker history` 里看不到客户证书被写入任何层。

**预期结果**：从源码与（可能的）实际镜像两个角度，都确认凭据「只在构建期临时挂载、不进产物」。

**待本地验证**：步骤 3 需要合法的 Plus 客户证书才能构建，多数读者无法执行，可标注待本地验证；步骤 1-2 是纯源码阅读，可立即完成。

#### 4.4.5 小练习与答案

**练习 1**：假设有人图省事，把 `COPY nginx-repo.crt /etc/ssl/nginx/` 加进了 Debian Plus 阶段并随后 `RUN rm`，这样做有什么严重后果？

**参考答案**：Docker 镜像是分层且层不可变的。`COPY` 创建了一个含证书的层，后续 `RUN rm` 只是在更新的层里「遮盖」它，并不能删掉历史层里的证书。任何 `docker pull` 该镜像的人都能通过解包历史层把这对付费客户证书挖出来，造成凭据泄露。因此必须用 `--mount=type=secret`，它在 `RUN` 结束后不会留下任何层。

**练习 2**：为什么 `nginx-files` 阶段可以用 `ADD nginx_signing.key` 而不用 secret？

**参考答案**：`nginx_signing.key`（及其 `.pub`/`.rsa.pub`）是 NGINX 官方的**公开签名密钥**，用于校验软件包完整性，本来就是公开分发的，不构成敏感凭据；而 `nginx-repo.crt` 是**客户私有的下载证书**，二者敏感级别完全不同，所以处理方式也不同。本次更新后 OSS 阶段正是靠挂载这个公开签名密钥来信任 `nginx.org` 仓库的包。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「镜像变体逆向分析」小任务：

1. **选定一个 target**：在 Makefile 的 `all-images` 列表里挑一个相对复杂的，例如 `ubi-image-nap-v5-plus-agent`。
2. **拆解 BUILD_OS**：推导 `BUILD_OS=ubi-10-plus-nap-v5-agent`，写明它的发行版（UBI）、是否 Plus（是）、NAP 版本（v5）、Agent 版本（v3）。
3. **追踪它在 Dockerfile 里的 FROM 链**：从 `common` 往回追，找到它依赖的 NGINX 基底阶段（`ubi-10-plus-nap-v5-agent` ← `ubi-10-plus-nap-v5-base` ← `ubi-minimal`）。
4. **判断它的凭据需求**：指出该变体需要哪些 secret（`nginx-repo.crt/.key`，UBI 还可能需要 `rhel_license`），并解释为什么。
5. **写出它的最终镜像包含什么**：NGINX Plus + NAP WAF v5 模块 + nginx-agent v3 + `nginx-ingress` 二进制 + `.tmpl` 模板（Plus 版）+ njs/OIDC 脚本。

**对比一个 OSS 变体**（巩固本次更新的核心）：再挑 `debian-image`，指出 `BUILD_OS=debian`，追踪它走的是 `debian` 阶段——`FROM debian:trixie-slim`，**从 nginx.org 公共仓库用 `apt-get install nginx nginx-module-njs ...` 从零安装**，不需要任何 `--secret`。

完成后，你应当能用一张图说清「一个 `make xxx-image` 命令背后，Dockerfile 走过了哪些阶段、挂载了哪些 secret、最终镜像里有什么、不含什么」，并能指出 OSS 变体「从零构建」与 Plus 变体「凭据拉包」的根本区别。

> 进阶（可选）：对照 `build/Dockerfile` 末尾的产物阶段，说明如果 CI 把 `TARGET` 设为 `goreleaser`，这条链会怎样从「`builder` 阶段在容器内编译」改走「GoReleaser 产物拷入」，从而理解本项目如何用一套 Dockerfile 同时支持本地开发与正式发布。

---

## 6. 本讲小结

- `build/Dockerfile` 是**单文件、多阶段**的镜像定义：依赖层 → NGINX 运行基底 → `common` 总装 → 二进制来源不同的产物阶段。
- 镜像变体由 **`BUILD_OS` 这一个 `--build-arg` 切换**，覆盖 OSS/Plus × Alpine/Debian/UBI × NAP(WAF v4/v5、DoS) × FIPS × Agent v2/v3 的组合矩阵。
- **本次更新后，所有 OSS 变体（Alpine/Debian/UBI）都从纯净基础镜像「从零安装」NGINX 及模块**（不再叠加官方 `nginx:` Docker Hub 镜像），三个发行版的构建思路因此统一；`nginx-module-njs` 等模块现在被显式安装。
- Makefile 的 `xxx-image` target 是对 `docker build` 的封装：默认 `TARGET=local` 拷宿主机二进制，Plus 变体额外注入 `PLUS_ARGS` 凭据参数；OSS 目标已被归入「NIC + NGINX OSS Images (built from scratch)」分组，且 `alpine-image`/`debian-image` 不再依赖 `build`。
- **NGINX Plus 凭据用 `--mount=type=secret` 临时挂载**，只在安装 Plus 包的那条 `RUN` 期间可见，**绝不 `COPY` 进镜像层**——这是本项目防止付费凭据泄露的核心安全约定。
- OSS 变体不带 secret（公共仓库免鉴权），这一区别在 Dockerfile 里表现为「Plus/NAP 阶段才有 `type=secret`」。

---

## 7. 下一步学习建议

本讲讲清了「镜像怎么来」。接下来的学习方向：

1. **u1-l6（安装方式）**：镜像造好后如何部署进集群——去看 `deployments/` 清单与 Helm chart，理解 Deployment/ConfigMap/RBAC 如何把镜像跑起来。
2. **回头印证 u1-l4（启动 flags）**：镜像的 `ENTRYPOINT ["/nginx-ingress"]` 会带上你在 u1-l4 学到的那些 flag，试着在 deployment 清单里找到它们对应的传参方式。
3. **为后续扩展打基础**：将来如果要新增一种镜像变体（例如新加一个发行版），本讲的「`BUILD_OS` 切换轴 + common 总装」结构就是改动蓝本；届时可结合 `nic-docker-images` 这个 skill 一起看。本次「OSS 从零构建」的统一化也意味着，新增 OSS 发行版只需复制 Alpine/Debian 的「基础镜像 + 仓库 + 安装命令」三段式即可。
