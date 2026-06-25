# 构建与运行方式：Makefile、go.mod 与常用目标

## 1. 本讲目标

上一讲（u1-l1）我们建立了整体心智模型：知道了 NGINX Ingress Controller（下称 NIC）是什么、采用「原始 client-go + 控制器调谐」的架构，以及它的五层分层。本讲要回答一个非常实际的问题：**这份源码怎么变成一个能跑的二进制？**

学完本讲，你应该能够：

- 看懂 `go.mod`，说出 NIC 用的是哪个 Go 版本，以及 client-go、nginx-plus-go-client、cert-manager 等核心依赖各自的角色。
- 理解 `Makefile` 的组织方式：哪些变量是「用户可覆盖」的，哪些不是，以及 `make build / make test / make lint / make format` 分别做了什么。
- 理解「构建标签（build tags）」机制，知道为什么测试要带 `-tags=aws,helmunit`。
- 自己动手编译出 `nginx-ingress` 二进制，并查看它的命令行 help。

本讲是后续所有源码阅读的前置条件——只有先把项目跑起来，后面跟踪调用链、修改参数、观察行为才有意义。

## 2. 前置知识

在开始之前，建议你先了解以下概念（不熟悉也没关系，下面会用通俗的方式补一句）：

- **Go module（模块）**：Go 的依赖管理单元。一个 `go.mod` 文件声明了模块名、Go 版本和所有依赖。可以类比成 Node.js 的 `package.json` 或 Rust 的 `Cargo.toml`。
- **Make / Makefile**：一个老牌的构建任务运行器。`make <目标>` 会执行 `Makefile` 里对应目标定义的命令。你把它当成一个「带依赖关系的脚本集合」即可。
- **build tag（构建标签）**：Go 源码顶部 `//go:build xxx` 这样的注释，用来告诉编译器「只有指定了 `xxx` 标签时，才编译这个文件」。常用来在不同构建场景下启用不同代码或测试。
- **ldflags（链接器标志）**：Go 在链接阶段可以通过 `-X` 标志把字符串「注入」到二进制里的某个变量。NIC 用这个机制在编译时把版本号写进二进制。
- **CI（持续集成）**：把代码提交后自动跑 lint/test/build 的流水线。本讲会顺带提到，但不深入（CI 在 u8-l7 专门讲）。

如果你对 Kubernetes、Ingress 这些词还陌生，建议先回头读 u1-l1。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [go.mod](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/go.mod) | Go 模块声明：模块名、Go 版本、直接依赖与间接依赖。是「这份代码依赖什么」的真相源。 |
| [Makefile](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile) | 构建任务总入口：定义了 build/test/lint/format/codegen 等几十个目标，以及大量可覆盖变量。 |
| [tools.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/tools.go) | 用一个带 `tools` 构建标签的文件，把构建工具（controller-gen、code-generator）作为模块依赖锁定版本。 |
| [.github/data/version.txt](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/.github/data/version.txt) | 存放当前版本号（`IC_VERSION=5.6.0`），Makefile 会读取它生成 `VERSION`。 |
| [cmd/nginx-ingress/main.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go) | 程序入口。里面有两个「构建期注入」的变量 `version` 和 `telemetryEndpoint`，与本讲的 ldflags 直接相关。 |

> 提示：本讲引用的源码都基于当前 HEAD `b678c44eb`。链接格式为永久链接 + 行号区间，点击可直接跳转。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**Go 模块与依赖**、**Makefile 目标体系**、**构建标签与本地构建**。

### 4.1 Go 模块与依赖

#### 4.1.1 概念说明

`go.mod` 是整个项目的「依赖清单」。它回答三个问题：

1. 这个模块叫什么？（模块名）
2. 用哪个 Go 版本编译？
3. 直接依赖哪些第三方库，版本各是多少？

NIC 的模块名是 `github.com/nginx/kubernetes-ingress`——这也是所有 internal/pkg 包的导入路径前缀。

#### 4.1.2 核心流程

Go 工具链处理依赖的简化流程：

1. `go build`/`go test` 时，Go 读取 `go.mod` 和 `go.sum`。
2. 解析 import 路径，确定需要的模块版本（直接依赖写在 `require` 里，间接依赖标 `// indirect`）。
3. 从模块代理或本地缓存下载依赖，编译。
4. `make deps` 目标会执行 `go mod tidy && go mod verify && go mod download`，保证依赖清单干净且校验通过。

#### 4.1.3 源码精读

**模块名与 Go 版本**——第一行声明模块名，第三行声明 Go 版本（go 1.26.4）：

[go.mod:L1-L3](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/go.mod#L1-L3) — 声明模块名 `github.com/nginx/kubernetes-ingress` 与 Go 1.26.4 版本。

**核心直接依赖**（节选，这些都是后续讲义会反复出现的库）：

- `k8s.io/client-go`、`k8s.io/api`、`k8s.io/apimachinery`（0.36.2）：这就是 u1-l1 反复提到的「原始 client-go」，是控制器 list-watch、workqueue、informer 的实现基础。
- `github.com/nginx/nginx-plus-go-client/v3`（v3.0.1）：NGINX Plus API 的 Go 客户端，用于 u5-l4 的动态配置（免重载更新 upstream）。
- `github.com/cert-manager/cert-manager`（v1.20.2）：用于 u6-l3 的 cert-manager 集成。
- `github.com/aws/aws-sdk-go-v2/...`（config、marketplacemetering）：AWS Marketplace 计量相关，这就是测试为什么需要 `aws` 构建标签的原因。
- `github.com/prometheus/client_golang`、`github.com/nginx/nginx-prometheus-exporter`、`github.com/nginx/telemetry-exporter`：u7 单元可观测性用到的指标与遥测库。

[go.mod:L5-L34](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/go.mod#L5-L34) — 直接依赖块（`require` 第一段），集中了上述核心库。

> 小提示：`go.mod` 后半部分大量的 `// indirect` 是「间接依赖」——你的直接依赖自己又依赖它们。你不必背它们，只需知道修改直接依赖时，Go 会自动维护这一段。

#### 4.1.4 代码实践

**实践目标**：从 `go.mod` 读出 NIC 的技术栈画像。

**操作步骤**：

1. 打开 `go.mod`，找到第一段 `require (...)`（不标 indirect 的那一段）。
2. 对照上一讲 u1-l1 提到的五层架构，把每个依赖「归位」到对应的架构层。
3. 找出 `replace` 指令（文件末尾），看看本项目对哪些库做了版本替换。

**预期结果**：你会得到一张「依赖 → 架构层」对照表，例如 `k8s.io/client-go → 控制器层`、`nginx-plus-go-client → NGINX 进程管理层`、`cert-manager → 安全认证层`。`replace` 段落会把两个 `google.golang.org/protobuf` 旧版本指向 1.33.0，解决依赖冲突。

**需要观察的现象**：直接依赖只有 28 行，但间接依赖有近 200 行——这正说明 NIC 是个「重量级」项目，依赖树庞大。

> 本实践不需要运行命令，属于「源码阅读型实践」。若想确认依赖是否齐全，可运行 `make deps`（见 4.2）。

#### 4.1.5 小练习与答案

**练习 1**：NIC 用的是哪个 Go 版本？如果本机 Go 版本更低会怎样？

> **答案**：Go 1.26.4（`go.mod` 第 3 行）。如果本机版本更低，Go 工具链会拒绝编译或提示版本不满足，需要升级本机 Go。

**练习 2**：为什么 `k8s.io/client-go` 是「核心」依赖，而不只是普通依赖？

> **答案**：u1-l1 已指出，NIC 不使用 controller-runtime，而是直接基于原始 client-go 的 SharedInformerFactory + workqueue 构建控制器。所以 client-go 是整个控制器子系统（u3 单元）的地基，几乎所有 informer、队列、缓存机制都来自它。

---

### 4.2 Makefile 目标体系

#### 4.2.1 概念说明

NIC 的 `Makefile` 是项目的「操作入口」。它把所有常用操作（构建、测试、格式化、代码生成、镜像构建）都封装成 `make xxx` 目标。这样做的好处是：无论你是开发者还是 CI，都用同一套命令，行为一致。

Makefile 里有两类变量需要注意：

- **不可被用户覆盖**（用 `=` 直接赋值）：如 `BINARY_NAME`、`VERSION`。
- **可被用户覆盖**（用 `?=`，意思是「如果没设过才赋默认值」）：如 `ARCH`、`GOOS`、`TARGET`、`NGINX_OSS_VERSION` 等。

#### 4.2.2 核心流程

Makefile 的执行逻辑可以概括为：

1. 读取顶部的变量定义（版本号、镜像名、Go 镜像、lint 版本等）。
2. 用户运行 `make <目标>`，Make 查找该目标的依赖与命令。
3. 默认目标（`.DEFAULT_GOAL`）是 `help`——所以直接敲 `make` 不带参数，会打印所有可用目标，而不是误编译。
4. 文件型目标（如 `nginx-ingress-amd64`）会根据源码是否变化决定是否重新构建（增量构建）。

```
make <target>
   │
   ├── 变量展开（VER/ARCH/GOOS/TARGET/...）
   │
   ├── 解析依赖（.DEFAULT_GOAL = help）
   │
   └── 执行目标命令（go build / go test / docker build / ...）
```

#### 4.2.3 源码精读

**版本号的来源**——Makefile 从 `version.txt` 读取版本，并拼出 `VERSION`：

[Makefile:L2-L5](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L2-L5) — `VER` 从 `.github/data/version.txt` 的 `IC_VERSION=5.6.0` 提取，`BINARY_NAME=nginx-ingress`，`VERSION=5.6.0-SNAPSHOT`（本地构建时加 `-SNAPSHOT` 后缀）。

**ldflags 注入**——这是「构建期把版本号写进二进制」的关键。注意 `-X main.version=...` 正好对应 `main.go` 里的 `var version string`：

[Makefile:L49-L51](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L49-L51) — `GO_LINKER_FLAGS` 把 `main.version` 和 `main.telemetryEndpoint` 两个变量在链接期注入；`-s -w` 用于裁掉调试信息、减小体积。

[cmd/nginx-ingress/main.go:L55-L58](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L55-L58) — 注释明确写着 `// Injected during build`，`version` 与 `telemetryEndpoint` 这两个空字符串变量，正是上面 ldflags 注入的目标。

**默认目标是 help**——避免空手敲 `make` 误触发重操作：

[Makefile:L69-L74](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L69-L74) — `.DEFAULT_GOAL:=help`，且 `help` 目标会用 `grep` 提取所有「`目标名: ## 说明`」格式的行，打印成一份带颜色高亮的目标清单。

**测试目标**——注意 `-tags=aws,helmunit` 与 `-shuffle=on`：

[Makefile:L108-L110](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L108-L110) — `make test` 实际执行 `go test -tags=aws,helmunit -shuffle=on ./...`。`-shuffle=on` 会让测试用例随机排序执行，帮助发现依赖测试顺序的隐藏 bug（后面 4.3 会解释这两个 tag）。

**快照更新**——模板输出变化时专用：

[Makefile:L112-L114](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L112-L114) — `make test-update-snaps` 用 `UPDATE_SNAPS=always` 重新生成黄金快照文件（u4-l8、u8-l4 会深入）。

**lint 与 format**——代码质量门槛：

[Makefile:L79-L82](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L79-L82) — `make lint` 用 Docker 跑 `golangci-lint`，并且只检查相对 `origin/main` 的 diff（`--new-from-patch`），即「只管新增改动」。

[Makefile:L91-L96](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L91-L96) — `make format` 先 `go install` 指定版本的 `goimports` 和 `gofumpt`，再对全仓格式化。

**codegen 三件套**——改了 CRD 类型后必跑（u2-l3 深入）：

[Makefile:L122-L137](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L122-L137) — `verify-codegen`（校验生成代码是否过期）、`update-codegen`（跑 hack 脚本重新生成 DeepCopy/typed client）、`update-crds`（用 `controller-gen` 从 kubebuilder 标注生成 CRD YAML）。

**deps 与 clean**——环境维护：

[Makefile:L343-L349](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L343-L349) — `make deps` 整理依赖（`go mod tidy && verify && download`），`make clean-cache` 清空 Go 模块缓存。

[Makefile:L338-L341](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L338-L341) — `make clean` 删除各架构的 `nginx-ingress` 二进制和 `dist/` 目录。

#### 4.2.4 代码实践

**实践目标**：用 `make help` 生成一张「目标速查表」并分类。

**操作步骤**：

1. 在项目根目录运行 `make`（等价于 `make help`）。
2. 观察输出，它分为 `Targets:` 和 `Variables:` 两段。
3. 把所有目标按用途分类，例如：
   - 构建：`build`、`build-local`、`clean`
   - 测试：`test`、`test-update-snaps`、`cover`
   - 代码质量：`lint`、`format`、`staticcheck`、`govulncheck`
   - 代码生成：`update-codegen`、`update-crds`、`verify-codegen`
   - 镜像：`debian-image`、`alpine-image`、`ubi-image`、`all-images`（镜像类目标在 u1-l5 详讲）
4. 再看 `Variables:` 段，确认哪些变量可以 `VARIABLE=value make xxx` 覆盖。

**需要观察的现象**：`Targets` 里大约有 50+ 个目标，其中一半以上是不同变体的镜像构建目标（`debian-image-*`、`alpine-image-*`、`ubi-image-*`）——这印证了 u1-l1 说的「支持 OSS/Plus/NAP/DoS 多变体」。

**预期结果**：得到一张分类清晰的目标表。如果某条命令环境不具备（比如没装 Docker），Makefile 多数目标会给出彩色提示并优雅退出（见 `@docker -v || ...` 写法），而不是直接崩溃。

> 如果无法运行（如只读环境），把 `make help` 的输出当作「待本地验证」标注，先通过阅读 Makefile 第 73-74 行的 grep 逻辑理解它的输出格式即可。

#### 4.2.5 小练习与答案

**练习 1**：直接运行 `make`（不带参数）会发生什么？为什么这样设计？

> **答案**：会执行 `help` 目标，打印所有可用目标和变量。因为 `.DEFAULT_GOAL:=help`（第 69 行），避免误触发 `build` 或镜像构建等重操作。

**练习 2**：`make lint` 为什么用 Docker 跑，并且只检查 `origin/main` 的 diff？

> **答案**：用 Docker 是为了保证所有人/CI 用同一个 `golangci-lint` 版本（`GOLANGCI_LINT_VERSION`），消除「我这能过你那报错」的环境差异；只检查 diff（`--new-from-patch`）是只关心本次新增改动是否引入问题，不背历史包袱，更友好。

---

### 4.3 构建标签与本地构建

#### 4.3.1 概念说明

本模块有两个重点：

1. **Go build tag（构建标签）**：用注释控制「哪些文件参与编译」。NIC 用它来（a）把工具依赖关进一个不参与正常编译的文件，以及（b）给测试加上条件编译。
2. **本地构建链路**：`make build` / `make build-local` 如何一步步把 `cmd/nginx-ingress/main.go` 编成 `nginx-ingress` 二进制。

#### 4.3.2 核心流程

本地构建链路：

```
make build（TARGET=local，默认）
   │
   ├── 进入 build 目标的 ifeq(local) 分支
   │
   ├── 委托给文件型目标 nginx-ingress-<ARCH>
   │       │
   │       └── 依赖 GO_SRCS（git ls-files 出来的所有 .go + go.mod/sum + version.txt）
   │
   └── 执行 go build
           │
           ├── CGO_ENABLED=0（纯静态）
           ├── GOOS / GOARCH（跨平台）
           ├── -trimpath（去掉本机路径，便于可复现构建）
           ├── -ldflags "-s -w -X main.version=... -X main.telemetryEndpoint=..."
           └── -o nginx-ingress-<ARCH>  cmd/nginx-ingress（入口包）
```

关键点：**增量构建**。`nginx-ingress-<ARCH>` 这个文件目标依赖 `GO_SRCS`，而 `GO_SRCS` 用 `git ls-files` 列出已跟踪的 `.go` 文件。只有这些文件变了，才会重新编译——这就是 Makefile 第 66 行注释提醒的「新增 .go 文件要先 `git add`，否则增量构建感知不到」。

#### 4.3.3 源码精读

**GO_SRCS 增量源列表**：

[Makefile:L66-L67](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L66-L67) — 用 `git ls-files` 列出所有非测试 `.go` 文件加 `go.mod/go.sum/version.txt`，作为二进制的依赖来源。注释明确指出只跟踪已提交/暂存文件。

**真正的 go build 命令**（在 `nginx-ingress-$(ARCH)` 这个文件型目标里）：

[Makefile:L147-L150](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L147-L150) — 核心编译命令：`CGO_ENABLED=0` 纯静态、`GOOS`/`GOARCH` 跨平台、`-trimpath` 去本机路径、`-ldflags` 注入版本号，输出到 `nginx-ingress-<ARCH>`，再 `cp` 一份成 `nginx-ingress`。

**build 目标的分支分发**——根据 `TARGET` 选不同构建路径：

[Makefile:L152-L170](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L152-L170) — `make build` 按 `TARGET` 取值分派：`local`（本地增量编译）、`download`（从 Docker 镜像里抠二进制）、`debug`（带调试符号 `-gcflags "all=-N -l"`）、`container`（在镜像构建阶段编译）、`goreleaser`（用 GoReleaser 发布构建）。默认 `TARGET=local`（第 33 行）。

**build-local 是 build 的快捷别名**：

[Makefile:L144-L145](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L144-L145) — `make build-local` 直接指向文件目标 `nginx-ingress-$(ARCH)`，等价于 `make build TARGET=local`，是最常用的本地开发命令。

**测试为什么要带 `-tags=aws,helmunit`**：

[Makefile:L108-L110](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L108-L110) — `make test` 强制带 `aws,helmunit`。`aws` 对应 AWS Marketplace 计量相关测试（依赖 aws-sdk-go-v2），`helmunit` 对应 Helm chart 单元测试。不带这两个 tag，这些测试文件根本不会被编译，测试覆盖就会少一大块。

**tools.go 用 build tag 锁定工具版本**：

[tools.go:L1-L12](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/tools.go#L1-L12) — 这个文件顶部有 `//go:build tools`，意味着正常编译/测试时它**不参与**（默认不带 `tools` tag）。但 `go mod tidy` 会扫描它的 import，从而把 `k8s.io/code-generator`、`sigs.k8s.io/controller-tools/cmd/controller-gen` 这两个构建工具的版本写进 `go.mod`。这是 Go 社区推荐的「把工具版本也纳入模块管理」的标准手法。

#### 4.3.4 代码实践

**实践目标**：本地编译出 `nginx-ingress` 二进制，查看它的 help 输出，验证版本号确实被注入。

**操作步骤**：

1. 确认本机已装 Go 1.26.4（或更高），且在项目根目录。
2. 运行 `make build-local`（或 `make build`，默认 `TARGET=local`）。
3. 构建成功后，根目录会出现 `nginx-ingress`（以及 `nginx-ingress-amd64`）。
4. 运行 `./nginx-ingress -h` 查看 help，挑选 3 个 flag 记录（u1-l4 会逐个讲 flag）。
5. （可选）验证版本注入：运行 `./nginx-ingress -v 2>&1 | head -20`，观察日志里是否出现 `5.6.0-SNAPSHOT` 之类的版本字符串。

**需要观察的现象**：

- 第二步：第一次构建会下载大量依赖、耗时较长；之后增量构建只在源码变动时才重编。
- 第四步：help 输出会列出所有命令行 flag，每个 flag 后面带一段说明。
- 第五步：若版本号显示为空，说明构建时没带 ldflags（比如直接用了 `go build` 而非 `make build`）。

**预期结果**：

- `make build-local` 产出 `nginx-ingress` 二进制。
- `./nginx-ingress -h` 打印 flag 清单。
- 日志/版本输出包含 `5.6.0-SNAPSHOT`（来自 `version.txt` 的 `IC_VERSION=5.6.0` 加 `-SNAPSHOT` 后缀）。

> 如果当前是只读/沙箱环境无法运行 `make build-local`，请标注「待本地验证」。你仍可以通过阅读 [Makefile:L149](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L149) 这条 `go build` 命令，理解它和 `go build` 裸命令的区别（多了 ldflags、trimpath、CGO_ENABLED=0）。

#### 4.3.5 小练习与答案

**练习 1**：为什么新增一个 `.go` 文件后，`make build-local` 可能不会重新编译？

> **答案**：因为 `GO_SRCS` 用 `git ls-files` 只列出已提交/暂存的文件（Makefile 第 67 行）。新文件若未 `git add`，不在 `GO_SRCS` 里，Make 就认为依赖没变，跳过编译。解决办法：先 `git add` 新文件。

**练习 2**：`make build` 和 `make build-local` 有什么区别？`TARGET=debug` 又有什么不同？

> **答案**：`make build-local` 直接走文件型目标做本地增量编译；`make build` 是个分发器，根据 `TARGET` 决定路径，默认 `local` 等价于 `build-local`。`TARGET=debug` 会用 `-gcflags "all=-N -l"` 关闭优化、保留调试符号，方便用 `dlv` 调试，但产物体积更大、跑得更慢（Makefile 第 159-163 行）。

**练习 3**：如果不带 `-tags=aws,helmunit`，`go test ./...` 会怎样？

> **答案**：带 `//go:build aws` 或 `//go:build helmunit` 的测试文件不会被编译，这些测试用例会被跳过，测试覆盖变小。所以官方约定「始终用 `make test`，不要裸跑 `go test`」（见 CLAUDE.md 的「Build, Test, Validate」一节）。

## 5. 综合实践

把本讲三个模块串起来，完成一个「从依赖到二进制」的完整闭环任务：

1. **读依赖**：打开 `go.mod`，挑出 `k8s.io/client-go`、`github.com/nginx/nginx-plus-go-client/v3`、`github.com/cert-manager/cert-manager` 三个依赖，分别在后续单元（u3 / u5 / u6）找到它们对应的角色，写一句话说明。
2. **看目标**：运行 `make help`，确认你能在输出里找到 `build`、`test`、`lint`、`format`、`update-crds` 这五个目标，并复述它们的用途。
3. **编译并验证版本注入**：运行 `make build-local`，得到 `nginx-ingress`；运行 `./nginx-ingress -h` 记录 3 个 flag；再确认版本号被 ldflags 注入（搜索日志里的 `5.6.0-SNAPSHOT`）。
4. **理解 build tag**：阅读 `tools.go` 顶部的 `//go:build tools`，解释为什么这个文件不影响正常编译，却能锁定 `controller-gen` 的版本。

> 产出建议：写一份不超过 300 字的「NIC 构建速查」，包含「用什么 Go 版本」「怎么编译」「怎么测试」「版本号从哪来」四点。这份速查在你后续改代码、跑测试时会反复用到。

## 6. 本讲小结

- NIC 的 Go 模块名是 `github.com/nginx/kubernetes-ingress`，使用 Go 1.26.4；核心依赖 client-go、nginx-plus-go-client、cert-manager 分别支撑控制器、Plus 动态配置、证书集成。
- `Makefile` 是统一操作入口，默认目标是 `help`，常用目标包括 `build`/`build-local`、`test`、`test-update-snaps`、`lint`、`format`、`update-codegen`/`update-crds`、`deps`/`clean`。
- 版本号来自 `.github/data/version.txt`（`IC_VERSION=5.6.0`），通过 ldflags（`-X main.version=...`）在编译期注入 `main.go` 的 `var version`，所以直接 `go build` 不会带版本号，必须走 `make build`。
- `make test` 强制带 `-tags=aws,helmunit` 和 `-shuffle=on`，前者保证 AWS/Helm 相关测试参与编译，后者随机化用例顺序以发现隐藏的顺序依赖。
- `tools.go` 用 `//go:build tools` 把构建工具（controller-gen、code-generator）作为模块依赖锁定版本，这是 Go 社区管理工具版本的标准手法。
- 本地增量构建依赖 `GO_SRCS`（`git ls-files` 列出的源码），所以新增 `.go` 文件要先 `git add` 才会被增量构建感知。

## 7. 下一步学习建议

下一步建议按顺序学习：

1. **u1-l3 项目目录结构与模块布局**：先把 `internal/`、`pkg/`、`cmd/`、`charts/`、`examples/` 的空间地图建立起来，再读具体源码会更轻松。
2. **u1-l4 命令行入口与启动 flags**：本讲实践里你已经看到 `./nginx-ingress -h` 的 flag 清单，u1-l4 会从 `main()` 出发，逐个讲解这些 flag（如 `-nginx-configmaps`、`-ingress-class`、`-enable-leader-election`）的作用与启动校验链。
3. 如果你想立刻看到「配置生成」全貌，也可以先跳到 u4 单元，但建议先走完 u1 的目录与入口两讲，避免迷路。
