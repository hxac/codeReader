# 把它跑起来：环境准备与各端构建运行

## 1. 本讲目标

上一讲（u1-l1）我们建立了「一个 Go 核心 + 多种外壳」的架构认知。本讲解决一个非常实际的问题：**亲手把 Gopeed 构建出来并跑起来**。

学完本讲，你应该能够：

1. 用 `go install` / `go build` 两种方式构建 Gopeed 的 CLI（命令行下载器），并用它完成一次真实下载。
2. 说清构建命令矩阵：CLI、纯 API 服务、Web 服务端、桌面动态库、移动端绑定分别对应哪个入口包、用什么命令、带哪些参数。
3. 理解 `-tags nosqlite,web`、`-buildmode=c-shared`、`-ldflags "-X ..."` 这些构建参数的确切含义，并能在源码中找到它们作用的证据。
4. 用 `docker compose` 启动 Web 版 Gopeed，并通过 `/api/v1/info` 接口验证容器内的运行状态。

## 2. 前置知识

本讲会用到一些 Go 工具链与容器的基础概念，先用通俗语言过一遍：

| 概念 | 通俗解释 |
| --- | --- |
| 编译型语言 | Go 源码需要先「编译」成机器码（二进制文件）才能运行，不像 Python 那样边读边执行。产物是一个可直接执行的文件。 |
| 交叉编译 | 在一台机器上编译出「给另一种操作系统/CPU 架构用」的二进制。Go 对此支持极好，例如在 Mac 上编译 Linux amd64 的服务器程序。 |
| CGO | Go 调用 C 代码的机制。开启 CGO 后编译产物依赖本地 C 工具链（gcc/clang），交叉编译会变难；设置 `CGO_ENABLED=0` 则强制纯 Go 编译。 |
| build tags（构建标签） | 写在 Go 文件第一行的条件编译指令，如 `//go:build web`。`go build -tags web` 时该文件参与编译，否则被排除。同一目录下可以有「带标签」和「不带标签」的多套实现。 |
| `-ldflags "-X ..."` | 链接期变量注入。在编译时把一个字符串「塞进」某个包的全局变量，最典型的用途是注入版本号，避免手工改源码。 |
| `-buildmode=c-shared` | 把 Go 代码编译成 C 风格的动态库（Windows 的 `.dll`、macOS 的 `.dylib`、Linux 的 `.so`），供其他语言（如 Dart/Flutter）通过 FFI 调用。 |
| gomobile | Go 官方提供的工具，把 Go 包编译成 Android 的 `.aar` 和 iOS 的 framework，供移动端 App 嵌入。 |
| `go:embed` | Go 1.16+ 的指令，把静态文件（如编译好的前端页面）**打包进二进制内部**，部署时只需要分发一个可执行文件。 |
| 多阶段 Docker 构建 | Dockerfile 里先在一个「胖」阶段（带完整 Go 工具链）编译，再只把产物复制进一个「瘦」阶段（如 alpine），最终镜像很小。 |
| PUID / PGID / umask | Linux 权限三件套。容器内默认以 root 运行，通过这两个用户 ID 和文件权限掩码，让下载的文件归属宿主机指定的用户。 |

如果某些概念暂时体会不深，没关系，第 4 节会结合 Gopeed 的真实源码逐一对照。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md) | 官方构建文档：环境要求与桌面/移动/Web 三套构建命令 |
| [go.mod](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod) | Go 模块定义：要求的 Go 版本与依赖清单（判断 `nosqlite` 标签去向的关键证据） |
| [cmd/gopeed/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go) / [cmd/gopeed/flags.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/flags.go) | CLI 入口与命令行参数 |
| [cmd/api/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/api/main.go) | 纯 API 服务的最小入口（本地开发用） |
| [cmd/web/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/main.go) / [cmd/web/flags.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/flags.go) | Web 服务端入口（带 `web` 构建标签与 `go:embed`）及其三层配置加载 |
| [cmd/server.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go) | `cmd/web` 与 `cmd/api` 共享的启动逻辑（含 `InDocker` 默认目录分支） |
| [pkg/base/info.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/info.go) | 被 `-ldflags -X` 注入的两个全局变量 `Version` / `InDocker` |
| [bind/desktop/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/desktop/main.go) | 桌面端 c-shared 动态库入口（`//export` C 导出函数） |
| [pkg/rest/api.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/rest/api.go) | `/api/v1/info` 处理器，验证构建注入变量的观察点 |
| [Dockerfile](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/Dockerfile) / [entrypoint.sh](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/entrypoint.sh) / [docker-compose.yml](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/docker-compose.yml) | 容器化构建与启动 |
| [.github/workflows/build.yml](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/.github/workflows/build.yml) | 官方 CI 的完整构建矩阵（各平台命令的「标准答案」） |
| [internal/webview/goprovider/provider.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/webview/goprovider/provider.go) | 另一个构建标签用例：`//go:build cgo` 控制 WebView 提供者是否参与编译 |

## 4. 核心概念与源码讲解

### 4.1 构建命令矩阵：一个核心，多种产物

#### 4.1.1 概念说明

Gopeed 是单一 Go module（`github.com/GopeedLab/gopeed`），但通过**不同的入口包 + 不同的构建参数**，能产出形态完全不同的产物。这就是上一讲「一个核心、多种外壳」在构建层面的体现。

理解本模块的关键不是背命令，而是抓住一张「入口包 → 产物」的映射表：

| 产物 | 入口包 | 核心命令 | 需要的构建标签 |
| --- | --- | --- | --- |
| CLI 命令行下载器 | `cmd/gopeed` | `go build` / `go install` | 无 |
| 纯 API 服务（本地开发） | `cmd/api` | `go build ./cmd/api` | 无 |
| Web 服务端（含嵌入 UI） | `cmd/web` | `flutter build web` 后 `go build -tags nosqlite,web` | `web`（以及 `nosqlite`） |
| 桌面动态库 | `bind/desktop` | `go build -buildmode=c-shared` | `nosqlite` |
| Android / iOS 绑定 | `bind/mobile` | `gomobile bind` | `nosqlite` |

#### 4.1.2 核心流程

以最常用的三条路径为例，流程如下：

```text
路径 A：CLI
  go install / go build
    └─ cmd/gopeed（无构建约束）
         └─ download.Boot() 链式 API 直连核心

路径 B：Web 服务端
  flutter build web                       # 先产出静态页面
    └─ cp 到 cmd/web/dist/                # 供 go:embed 嵌入
         └─ go build -tags nosqlite,web ./cmd/web
              └─ cmd.Start(cfg)           # 共享启动逻辑（cmd/server.go）

路径 C：桌面
  go build -tags nosqlite -buildmode=c-shared ./bind/desktop
    └─ 生成 libgopeed.dll / .dylib / .so
         └─ flutter build windows / macos / linux 把动态库打进 App
```

注意路径 B 的依赖顺序：**必须先有 `cmd/web/dist` 目录，Go 编译才能通过**（原因见 4.2 模块），这也是为什么直接 `docker build` 一个不含 dist 的仓库会失败，详见 4.3。

#### 4.1.3 源码精读

**① 环境要求写在 README 的 Development 一节**

[README.md:L103-L106](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L103-L106) 规定开发环境为 Go 1.25+ 与 Flutter 3.41+：

```md
### 🌍 Environment

1. Go 1.25+
2. Flutter 3.41+
```

这两个数字在 [go.mod:L1-L3](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod#L1-L3) 和 CI 中互相印证：`go.mod` 声明 `go 1.25.4`，而 [.github/workflows/build.yml:L15-L17](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/.github/workflows/build.yml#L15-L17) 固定了 `GO_VERSION: "1.25.4"`、`FLUTTER_VERSION: "3.41.6"`。本地 `go install` 时若本机 Go 版本低于 1.25.4，工具链会自动尝试下载对应版本（GOTOOLCHAIN 默认行为）。

**② CLI 入口：`cmd/gopeed` 没有任何构建约束**

[cmd/gopeed/flags.go:L23-L36](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/flags.go#L23-L36) 定义了 CLI 的全部参数：`-C` 并发连接数（默认 16）、`-D` 存储目录（默认当前目录）、`-checksum` / `-checksum-algo` 校验和，URL 是位置参数（第一个非 flag 实参）：

```go
args.connections = flag.Int("C", 16, "Concurrent connections.")
args.dir = flag.String("D", dir, "Store directory.")
args.checksum = flag.String("checksum", "", "Expected checksum hash (hex encoded).")
args.checksumAlgo = flag.String("checksum-algo", "md5", "Checksum algorithm: md5, sha1, or sha256.")
```

[cmd/gopeed/main.go:L34-L63](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L34-L63) 是核心的 `download.Boot().URL(...).Listener(...).Create(...)` 链式调用——CLI 不经过任何桥接层，直接把核心当库用（这个 API 会在 u1-l4 深入讲解）。README 给普通用户的安装方式是 [README.md:L75-L81](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L75-L81)：

```bash
go install github.com/GopeedLab/gopeed/cmd/gopeed@latest
```

**③ 纯 API 入口：`cmd/api` 是「最小服务」样板**

[cmd/api/main.go:L11-L23](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/api/main.go#L11-L23) 只有 20 行，注释明确写着 `// only for local development`：

```go
cfg := &model.StartConfig{
    Network:   "tcp",
    Address:   "127.0.0.1:9999",
    Storage:   model.StorageBolt,
    WebEnable: true,
}
if err := cmd.Start(cfg); err != nil {
```

它把监听地址、存储方式全部写死，不接受任何命令行参数，用于开发者快速在本地拉起服务端。对比之下，`cmd/web` 把同样的 `StartConfig` 暴露成一层完整的 flags/配置文件/环境变量体系（见 4.3.3 的 ③）。

**④ 共享启动逻辑：`cmd/server.go`**

`cmd/web` 和 `cmd/api` 最后都调用同一个 [cmd/server.go:L19-L29](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L19-L29)：`rest.BuildServer(cfg)` 组装服务器，随后打印 banner、处理首次加载的默认配置。启动成功后会输出 `Server start success on http://...`（[cmd/server.go:L62-L68](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L62-L68)），这个输出是我们后面实践中判断「服务已就绪」的信号。

**⑤ 桌面动态库：`bind/desktop`**

[bind/desktop/main.go:L30-L47](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/bind/desktop/main.go#L30-L47) 展示了 c-shared 形态的特殊写法：`main` 函数是空的，真正的内容是用 `//export` 注释导出给 C/FFI 的函数（`Start`、`Stop` 等）：

```go
func main() {}

//export Start
func Start(cfg *C.char) (int, *C.char) {
    ...
}
```

对应 README 中三个平台的桌面构建命令（[README.md:L129](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L129) 为 Windows，L137 为 macOS，L145 为 Linux），全部带 `-tags nosqlite -buildmode=c-shared`，产物路径分别落到 `ui/flutter/windows|macos|linux` 下的约定位置，再由 `flutter build` 打包。

**⑥ 移动端：gomobile 绑定**

[README.md:L150-L176](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L150-L176) 给出 `gomobile init` 初始化与 `gomobile bind -tags nosqlite ... -target=android/-target=ios` 两条绑定命令，产出 `.aar`（Android）与 `.xcframework`（iOS）。其内部细节属于 u8-l3 的内容，本讲只需建立「移动端是又一种外壳」的认知。

#### 4.1.4 代码实践

**实践目标**：本地从源码构建 CLI，并完成一次真实下载（不依赖外网也可以做）。

**操作步骤**：

1. 在仓库根目录执行（无需任何标签）：

   ```bash
   go build -o bin/gopeed ./cmd/gopeed
   ```

2. 制造一个本地下载源（避免依赖公网直链的有效性）：

   ```bash
   mkdir -p /tmp/gopeed-lab && cd /tmp/gopeed-lab
   dd if=/dev/urandom of=demo.bin bs=1M count=8   # 生成 8MB 测试文件
   python3 -m http.server 8000
   ```

3. 另开一个终端，用刚构建的 CLI 下载：

   ```bash
   ./bin/gopeed -D /tmp/gopeed-dl http://127.0.0.1:8000/demo.bin
   ```

4. 再试一次多连接与单连接的差别：

   ```bash
   ./bin/gopeed -C 1 -D /tmp/gopeed-dl http://127.0.0.1:8000/demo.bin
   ./bin/gopeed -C 8 -D /tmp/gopeed-dl http://127.0.0.1:8000/demo.bin
   ```

**需要观察的现象**：

- 终端出现 `■□□□...` 格式的进度条，实时刷新（由 [cmd/gopeed/main.go:L82-L113](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/gopeed/main.go#L82-L113) 的 `printProgress` 用 `\r` 回车覆写实现）。
- 下载结束打印 `complete` 与 `saving path: /tmp/gopeed-dl`。
- 对比 `-C 1` 与 `-C 8`：本地回环网络下差异可能不明显；若换成远程大文件会更显著（多连接分块原理在 u5-l2 展开）。

**预期结果**：`/tmp/gopeed-dl/demo.bin` 出现且大小为 8MB。本地回环下载的具体耗时与速度显示**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `go install github.com/GopeedLab/gopeed/cmd/gopeed@latest` 不需要任何 `-tags`，而构建 `cmd/web` 必须加 `-tags web`？

**答案**：`cmd/gopeed` 目录下的 Go 文件没有任何 `//go:build` 约束，默认全部参与编译；`cmd/web/main.go` 第一行是 `//go:build web`，不加标签时该包的所有 Go 文件都被构建系统排除，`go build ./cmd/web` 会直接报错（详见 4.2.4 实践第 1 步）。

**练习 2**：`cmd/api` 和 `cmd/web` 都能启动 REST 服务，它们的核心差异是什么？

**答案**：`cmd/api` 的 `StartConfig` 是硬编码的（固定 `127.0.0.1:9999`、Bolt 存储），注释标明只用于本地开发；`cmd/web` 把监听地址、鉴权、存储目录、白名单等全部参数化（flags / config.json / 环境变量三层加载），并用 `go:embed` 把 Flutter Web 静态资源嵌进二进制，是可部署的生产形态。

**练习 3**：如果你想给 macOS 桌面版构建动态库，应该执行什么命令？产物需要放到哪里？

**答案**：参考 [README.md:L137](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L137)：`go build -tags nosqlite -ldflags="-w -s" -buildmode=c-shared -o ui/flutter/macos/Frameworks/libgopeed.dylib github.com/GopeedLab/gopeed/bind/desktop`，随后在 `ui/flutter` 目录执行 `flutter build macos`。产物路径是 Flutter 工程的约定位置，不能随意放。

### 4.2 build tags 与构建期注入：`nosqlite`、`web` 与 `-ldflags -X`

#### 4.2.1 概念说明

本模块解决三个「编译期开关」问题：

1. **`web` 标签**：Gopeed 自己源码里的开关，控制 `cmd/web` 这个入口是否存在。它的存在让「不带 UI 的仓库」也能编译出所有其他产物，同时避免 `go vet ./...`、IDE 对一个缺少 `dist` 目录的包反复报错。
2. **`nosqlite` 标签**：**不在 Gopeed 自己的源码里**（我们在仓库内全局搜索 `nosqlite`，只命中 README、Dockerfile、CI 的构建命令，没有任何 `//go:build nosqlite`）。它是传给**依赖库**的开关，作用与 SQLite 相关代码的裁剪有关，目的是让 `CGO_ENABLED=0` 的纯 Go 交叉编译可行。
3. **`-ldflags "-X ..."`**：编译期把版本号、是否在 Docker 内这两个信息注入 [pkg/base/info.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/info.go) 的全局变量，运行时通过 `/api/v1/info` 接口暴露。

#### 4.2.2 核心流程

构建标签的判定流程（以 `cmd/web` 为例）：

```text
go build ./cmd/web                go build -tags web ./cmd/web
        │                                   │
        ▼                                   ▼
读取 main.go 的 //go:build web          标签集合 = {web}
        │                                   │
   标签集合 = {} ──► 条件不满足 ──► 文件被排除
   结果：包内没有任何 Go 文件
   报错：build constraints exclude all Go files
                                        │
                                        ▼
                              main.go 参与编译
                              继续处理 //go:embed dist/*
                              要求 dist 目录存在且有匹配文件
```

`-ldflags -X` 的注入流程：

```text
go build -ldflags "-X pkg/path.Variable=value"
        │
        ▼
链接阶段把字符串 value 写入目标变量的初始值
        │
        ▼
运行时 pkg/base.Version / pkg/base.InDocker 即为注入值
        │
        ▼
GET /api/v1/info 返回 {version, inDocker, os, arch, ...}
```

#### 4.2.3 源码精读

**① `web` 标签的门禁：`cmd/web/main.go` 头两行**

[cmd/web/main.go:L1-L2](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/main.go#L1-L2) 是整个仓库里唯一一处 `//go:build web`：

```go
//go:build web
// +build web
```

（第二行 `// +build` 是 Go 1.17 之前的旧语法，保留它可以在老工具链上同样生效。）

**② `go:embed` 要求 dist 必须先存在**

[cmd/web/main.go:L17-L24](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/main.go#L17-L24) 把 Flutter Web 构建产物嵌入二进制：

```go
//go:embed dist/*
var dist embed.FS

func main() {
	sub, err := fs.Sub(dist, "dist")
```

`embed.FS` 的模式 `dist/*` 若匹配不到任何文件，编译直接失败。所以 README 的 Web 构建流程（[README.md:L182-L189](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/README.md#L182-L189)）是三步走：`flutter build web` → `rm -rf cmd/web/dist && cp -r ui/flutter/build/web cmd/web/dist` → `go build -tags nosqlite,web ...`。

**③ 注入目标：`pkg/base/info.go`**

[pkg/base/info.go:L3-L11](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/info.go#L3-L11) 定义了两个「编译期留空、构建时填充」的变量，注释里就写着注入示例：

```go
// Version is the build version, set at build time, using `go build -ldflags "-X github.com/GopeedLab/gopeed/pkg/base.Version=1.0.0"`.
var Version string
var InDocker string

func init() {
	if Version == "" {
		Version = "dev"
	}
}
```

不做注入时 `Version` 为空字符串，`init()` 会兜底成 `"dev"`——这就是为什么源码直接 `go build` 出来的程序版本号显示 `dev`。

**④ 观察点：`/api/v1/info`**

[pkg/rest/api.go:L13-L22](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/rest/api.go#L13-L22) 把这两个变量连同运行时信息一起返回：

```go
func Info(w http.ResponseWriter, r *http.Request) {
	info := map[string]any{
		"version":  base.Version,
		"runtime":  runtime.Version(),
		"os":       runtime.GOOS,
		"arch":     runtime.GOARCH,
		"inDocker": base.InDocker == "true",
	}
```

路由挂载在 `/api/v1/info`（可在 [pkg/rest/invoke_test.go:L33](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/rest/invoke_test.go#L33) 的测试中看到这个路径）。它是我们验证「注入是否生效」的最直接手段。

**⑤ `nosqlite` 的证据链**

事实一：仓库内没有任何 `.go` 文件带 `nosqlite` 构建约束——构建命令（README/Dockerfile/CI）里的 `-tags nosqlite` 传给了依赖图。

事实二：[go.mod:L111-L112](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod#L111-L112) 与 [go.mod:L172-L173](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod#L172-L173) 中，所有 SQLite 相关包（`go-llsqlite/adapter`、`go-llsqlite/crawshaw`、`modernc.org/sqlite`、`zombiezen.com/go/sqlite`）**全部是 indirect（间接）依赖**，它们由 BT 协议依赖的 `anacrolix/torrent`（[go.mod:L8](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod#L8)）引入，用于 BT 的 piece 完成状态存储。

事实三：Dockerfile 与 CI 在 `CGO_ENABLED=0` 交叉编译时总是带上 `nosqlite`（[.github/workflows/build.yml:L666-L686](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/.github/workflows/build.yml#L666-L686) 中 Web 服务器对 windows/darwin/linux × 386/amd64/arm64 九个组合的循环里同时设置了 `export CGO_ENABLED=0` 与 `-tags nosqlite,web`）。

结论：`nosqlite` 用于在依赖层面裁掉 SQLite（含 CGO 实现）相关代码路径，使纯 Go 交叉编译可行。该标签在**依赖库源码内部**具体排除了哪些文件，本仓库无法直接看到，**待本地验证**（可在模块缓存中搜索 `//go:build nosqlite`，或对比加/不加标签时 `go list -deps ./cmd/web` 的差异）。

**⑥ 对照组：仓库自己的 `cgo` 标签用例**

并非所有编译开关都藏在依赖里。[internal/webview/goprovider/provider.go:L1-L2](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/webview/goprovider/provider.go#L1-L2) 用 `//go:build cgo` 控制「原生 WebView 提供者」是否参与编译——`CGO_ENABLED=0` 时 Go 会自动禁用 `cgo` 标签，这个依赖 `webview_go`（C 绑定）的文件就被整体排除。这解释了为什么服务器/容器形态（无 GUI）可以完全纯 Go 编译，而桌面形态必须装 C 工具链（README 桌面构建一节强调的 cgo 要求）。

#### 4.2.4 代码实践

**实践目标**：亲手触发并修复 `web` 标签与 `go:embed` 的两个编译报错，再验证 `-ldflags -X` 的注入效果。

**操作步骤**：

1. **触发标签门禁**（预期失败）：

   ```bash
   go build -o /tmp/gopeed-web ./cmd/web
   ```

2. **触发 embed 门禁**（预期仍失败）：

   ```bash
   mkdir -p cmd/web/dist
   go build -tags web -o /tmp/gopeed-web ./cmd/web
   ```

3. **补齐 embed 文件后成功**：

   ```bash
   echo hello > cmd/web/dist/index.html
   go build -tags web -o /tmp/gopeed-web ./cmd/web
   ```

4. **启动并观察默认版本号**：

   ```bash
   /tmp/gopeed-web -P 19999
   # 另一个终端：
   curl http://127.0.0.1:19999/api/v1/info
   ```

5. **注入自定义版本号重建**：

   ```bash
   go build -tags web \
     -ldflags="-X github.com/GopeedLab/gopeed/pkg/base.Version=lab-1.0" \
     -o /tmp/gopeed-web ./cmd/web
   /tmp/gopeed-web -P 19999
   curl http://127.0.0.1:19999/api/v1/info
   ```

6. 实验结束后清理临时产物，避免影响后续真正的 Web 构建：`rm -rf cmd/web/dist`。

**需要观察的现象**：

- 第 1 步报错，形如 `build constraints exclude all Go files in .../cmd/web`（具体文案以本地 Go 版本输出为准，**待本地验证**）。
- 第 2 步报错，形如 `pattern dist/*: no matching files found`——空目录也不行，`embed` 至少要有一个匹配文件。
- 第 3 步编译成功；第 4 步服务启动时打印 banner 与 `Server start success on http://0.0.0.0:19999`，`/api/v1/info` 返回的 `version` 为 `"dev"`（`init()` 兜底）。
- 第 5 步 `version` 变为 `"lab-1.0"`，证明注入生效。

**预期结果**：五步依次呈现「失败 → 失败 → 成功 → dev → lab-1.0」的状态变化，完整覆盖本模块三个知识点。注意：此时嵌入的只是占位 `index.html`，浏览器打开首页只会看到 `hello`，属正常现象。

#### 4.2.5 小练习与答案

**练习 1**：在仓库里执行 `git grep "go:build nosqlite"` 没有任何结果，这说明什么？

**答案**：说明 `nosqlite` 标签不控制本仓库的任何文件，它通过 `-tags` 传递给依赖库生效。结合 `go.mod` 中 SQLite 相关包全部是 indirect、以及 CI 在 `CGO_ENABLED=0` 交叉编译时总是携带该标签这两个事实，可推断其用途是裁掉依赖中的 SQLite/CGO 代码路径以支持纯 Go 编译（依赖内部具体机制待确认）。

**练习 2**：不做任何 `-ldflags` 注入直接构建的二进制，`/api/v1/info` 里 `version` 会是什么？为什么？

**答案**：`"dev"`。因为 [pkg/base/info.go:L7-L11](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/info.go#L7-L11) 的 `init()` 检测到 `Version == ""` 时会写入兜底值 `"dev"`。

**练习 3**：`-w -s` 这两个 ldflags（README 所有构建命令里都有）是干什么的？去掉会影响功能吗？

**答案**：`-s` 去掉符号表、`-w` 去掉 DWARF 调试信息，两者都只为减小二进制体积，不影响程序逻辑与 `-X` 注入（注入发生在链接期改写变量初始值，与调试信息无关）。开发调试时如果需要用调试器断点，反而应该去掉它们。

### 4.3 Docker 部署入口：多阶段镜像与容器启动

#### 4.3.1 概念说明

对使用者来说，Docker 是体验 Gopeed Web 版门槛最低的方式：一条 `docker compose up` 就能得到带完整 Web UI 的下载服务。对源码学习者来说，Dockerfile 是一份「官方构建命令的浓缩版」——它把 4.1、4.2 两个模块的知识点全部固化在一行 `go build` 里。

需要理解的容器背景知识：

- **多阶段构建**：第一阶段用 `golang:1.25.4-alpine` 镜像编译，第二阶段只把产物复制进干净的 `alpine:3.23`，最终镜像不含 Go 工具链。
- **su-exec**：alpine 下的轻量「以指定用户执行命令」工具（sudo 的极简替代），配合 `PUID/PGID` 实现权限映射。
- **数据卷**：容器是临时的，任务数据库（`/app/storage`）与下载目录（`/app/Downloads`）需要挂载到宿主机才能持久化。

#### 4.3.2 核心流程

官方镜像（`liwei2633/gopeed`）从 CI 到运行的完整链路：

```text
CI（build-docker 任务，build.yml）
  flutter build web --no-web-resources-cdn
    └─ rm -rf cmd/web/dist && cp -r ui/flutter/build/web cmd/web/dist
         └─ docker buildx 多架构构建（linux/386, amd64, arm64, arm/v7）
              └─ 推送 liwei2633/gopeed:latest 与 :v版本号

Dockerfile（两阶段）
  阶段1 golang:1.25.4-alpine
    CGO_ENABLED=0 go build -tags nosqlite,web
      -ldflags "-s -w -X base.Version=$VERSION -X base.InDocker=true"
    → dist/gopeed（单个自包含二进制，UI 已嵌入）
  阶段2 alpine:3.23
    复制二进制 + entrypoint.sh，安装 su-exec
    VOLUME /app/storage，EXPOSE 9999

运行时（entrypoint.sh）
  chown -R PUID:PGID /app → umask → su-exec 降权 exec ./gopeed "$@"
```

#### 4.3.3 源码精读

**① Dockerfile 的编译阶段：一行命令浓缩全部构建知识**

[Dockerfile:L6-L9](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/Dockerfile#L6-L9)：

```dockerfile
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -tags nosqlite,web \
      -ldflags="-s -w -X github.com/GopeedLab/gopeed/pkg/base.Version=$VERSION -X github.com/GopeedLab/gopeed/pkg/base.InDocker=true" \
      -o dist/gopeed github.com/GopeedLab/gopeed/cmd/web
```

逐项拆解：`CGO_ENABLED=0`（纯 Go，配合 alpine 与多架构交叉编译）、`-tags nosqlite,web`（4.2 讲过的两个标签）、`-X` 注入 `Version` 与 `InDocker` 两个变量。注意 `InDocker` 被写死为 `true`——因为程序运行在容器里，这个信息必须构建期告知。

**② 运行阶段：权限与端口约定**

[Dockerfile:L11-L23](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/Dockerfile#L11-L23) 声明了 `VOLUME /app/storage`（任务数据库）、`ENV PUID=0 PGID=0 UMASK=022`、`EXPOSE 9999` 与入口脚本。配合 [entrypoint.sh:L1-L6](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/entrypoint.sh#L1-L6)：

```sh
chown -R ${PUID}:${PGID} /app
umask ${UMASK}
exec su-exec ${PUID}:${PGID} ./gopeed "$@"
```

容器启动时先把 `/app` 整体归属到指定用户/组，再设置新建文件的默认权限掩码，最后降权执行下载服务。`"$@"` 会把 `docker run` 追加的参数透传给 `gopeed` 二进制（这就是 compose 里 `command: -u Username -p Password` 生效的原因）。

**③ docker-compose.yml：一键启动模板**

[docker-compose.yml:L1-L15](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/docker-compose.yml#L1-L15) 完整内容只有 15 行：映射 `9999:9999` 端口、设置 `PUID/PGID/UMASK`、把宿主机 `~/gopeed/Downloads` 挂到容器 `/app/Downloads`（storage 卷默认注释掉，按需打开），`restart: unless-stopped` 保证自愈，最后一行被注释的 `command:` 是开启 Web 登录认证的示例。

**④ `InDocker` 在运行时的分支意义**

[cmd/server.go:L39-L54](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L39-L54) 首次启动时决定默认下载目录：

```go
// Set default download dir, in docker, it will be ${exe}/Downloads, else it will be ${user}/Downloads
if downloadDir == "" {
    if base.InDocker == "true" {
        downloadDir = filepath.Join(filepath.Dir(cfg.StorageDir), "Downloads")
    } else {
        ...
        downloadDir = filepath.Join(userDir, "Downloads")
    }
```

容器内取「存储目录旁边的 Downloads」（即 `/app/Downloads`，与 compose 挂载点精确对应）；裸机则取用户主目录的 Downloads。这就是构建期注入的 `InDocker` 影响运行行为的直接证据——构建参数不只是「元数据」，它能改变业务分支。

**⑤ Web 服务的三层配置加载**

`cmd/web` 的参数有明确优先级。[cmd/web/flags.go:L31-L42](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/flags.go#L31-L42)：

```go
cliConfig := loadCliArgs()
loadConfigFile(cfg, *cliConfig.configPath)
loadEnvVars(cfg)
// override with non-default command line arguments
overrideWithCliArgs(cfg, cliConfig)
```

即：**配置文件（默认 `./config.json`）→ 环境变量 → 命令行参数覆盖**。命令行旗标定义在 [cmd/web/flags.go:L45-L64](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/flags.go#L45-L64)：`-A/-address`、`-P/-port`（默认 9999）、`-u/-username`、`-p/-password`（注释明确说明：不设密码则不启用 Web 认证）、`-T/-api-token`、`-mcp-enable`、`-d/-storage-dir`、`-white-download-dirs` 等。环境变量通过反射按 `GOPEED_` 前缀 + json 标签大写映射（[cmd/web/flags.go:L154-L176](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/flags.go#L154-L176)），例如 `GOPEED_PORT=9999`。

**⑥ CI 如何产出官方镜像**

[.github/workflows/build.yml:L777-L784](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/.github/workflows/build.yml#L777-L784) 先在本机执行 `flutter build web` 并复制到 `cmd/web/dist`，随后 [build.yml:L785-L802](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/.github/workflows/build.yml#L785-L802) 用 `docker/build-push-action` 以 QEMU/Buildx 构建 `linux/386`、`linux/amd64`、`linux/arm64`、`linux/arm/v7` 四个架构，打上 `liwei2633/gopeed:latest` 与版本号标签推送——这也再次解释了为什么 Dockerfile 必须 `CGO_ENABLED=0`：四个架构里多数没有现成的 C 交叉工具链。

#### 4.3.4 代码实践

**实践目标**：用 Docker 跑起 Web 版 Gopeed，并通过 API 验证「容器内构建注入」确实生效。

**操作步骤**：

1. **方式 A（推荐，无需 Flutter）**：直接使用官方镜像，在仓库根目录执行：

   ```bash
   docker compose up -d
   docker compose logs -f gopeed   # 看到 banner 与 Server start success 即就绪
   ```

2. 验证服务与注入信息：

   ```bash
   curl http://127.0.0.1:9999/api/v1/info
   ```

3. 浏览器访问 `http://localhost:9999`，确认出现 Gopeed Web UI，可新建一个 HTTP 下载任务。

4. **方式 B（完整本地构建，需要 Flutter 3.41+）**：模拟 CI 流程后本地构建镜像：

   ```bash
   cd ui/flutter && flutter build web && cd ../../
   rm -rf cmd/web/dist && cp -r ui/flutter/build/web cmd/web/dist
   docker build -t gopeed:lab .
   docker run -d -p 9998:9999 -v ~/gopeed-lab:/app/Downloads gopeed:lab
   curl http://127.0.0.1:9998/api/v1/info
   ```

5. （可选）开启登录认证：编辑 `docker-compose.yml`，取消 `command: -u Username -p Password` 的注释（换成自己的账号密码），`docker compose up -d` 重建后浏览器访问将要求登录。

**需要观察的现象**：

- 第 2 步返回的 JSON 中 `inDocker: true`、`version` 为具体版本号（非 `dev`）——与 4.2.4 中本地构建的 `dev` / `false` 形成对照。
- 第 3 步 Web UI 正常可用；创建任务后文件出现在宿主机 `~/gopeed/Downloads`（compose 挂载）。
- 第 4 步（若执行）本地构建的镜像同样 `inDocker: true`（Dockerfile 写死注入）。
- 第 5 步开启认证后，未登录的浏览器访问会跳转登录页。

**预期结果**：`/api/v1/info` 返回类似 `{"version":"x.y.z", ..., "inDocker":true, "os":"linux", "arch":"..."}` 的结果。若在 Apple Silicon 或 ARM 机器上，`arch` 会是 `arm64`（对应多架构镜像自动选择的架构）。具体 JSON 字段顺序与版本号**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：容器里的 `PUID=1000 PGID=1000` 是如何变成文件属主的？

**答案**：[entrypoint.sh:L1-L6](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/entrypoint.sh#L1-L6) 先 `chown -R ${PUID}:${PGID} /app` 把容器内目录改为指定属主，再用 `su-exec ${PUID}:${PGID}` 让服务进程以该用户身份运行，于是下载产生的新文件属主即为 `PUID`，与宿主机用户对齐。

**练习 2**：为什么 compose 要把宿主机目录挂载到容器的 `/app/Downloads` 而不是别的路径？

**答案**：容器形态下 Dockerfile 注入了 `InDocker=true`，[cmd/server.go:L42-L44](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L42-L44) 由此把默认下载目录设为「存储目录旁的 Downloads」，即 `/app/Downloads`。挂载点必须与这个默认值一致，文件才会落到宿主机。

**练习 3**：如果直接对「没有执行过 `flutter build web` 的仓库」执行 `docker build`，会在哪一步失败？

**答案**：在阶段一的 `go build` 失败。因为 `COPY . .` 不会带来不存在的 `cmd/web/dist`，而 [cmd/web/main.go:L17-L18](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/web/main.go#L17-L18) 的 `//go:embed dist/*` 匹配不到文件会直接编译报错。所以纯 Go 环境想快速体验请用方式 A（官方镜像），完整本地构建必须先产出前端资源。

## 5. 综合实践

把本讲三个模块串起来，完成一次「从源码到三端运行」的完整走查，并把每一步用到的构建参数记录成表：

1. **CLI 路线**：`go build -o bin/gopeed ./cmd/gopeed`，用 4.1.4 的本地 HTTP 服务完成一次下载。
2. **Web 路线**：按 4.2.4 的五步实验走完（含两次预期失败的构建），最后用 `-ldflags "-X ...=自定义版本"` 产出二进制，`curl /api/v1/info` 验证。
3. **Docker 路线**：按 4.3.4 方式 A 启动官方镜像，再次 `curl /api/v1/info` 对比。

最终交付一张「构建参数对照表」，参考格式：

| 路线 | 命令关键参数 | CGO | 产物 | `version` | `inDocker` |
| --- | --- | --- | --- | --- | --- |
| CLI | （无） | 默认开启 | `bin/gopeed` | dev | false |
| Web 本地 | `-tags web`（可选加 `-ldflags -X`） | 默认开启 | 单二进制 | dev 或注入值 | false |
| Docker | `-tags nosqlite,web` + `CGO_ENABLED=0` + 两个 `-X` | 关闭 | 镜像内 `/app/gopeed` | 镜像版本 | true |

表中 CLI/Web 两行的 `inDocker` 值与 CGO 状态**待本地验证**（`inDocker` 为 false 的原因：未注入时是空字符串，`"dev"` 兜底逻辑只作用于 `Version`，`InDocker == "true"` 的判断为假）。

## 6. 本讲小结

- Gopeed 是单一 Go module，通过「不同入口包 + 不同构建参数」产出五种形态：`cmd/gopeed`（CLI）、`cmd/api`（开发用最小服务）、`cmd/web`（嵌入 UI 的 Web 服务端）、`bind/desktop`（c-shared 动态库）、`bind/mobile`（gomobile 绑定）。
- `-tags web` 是仓库内唯一的 `web` 标签门禁，同时 `go:embed dist/*` 要求先构建 Flutter Web 并复制到 `cmd/web/dist`，二者共同决定了 Web 服务端的构建顺序。
- `-tags nosqlite` 不对应本仓库任何文件，它作用于依赖（`go.mod` 中 SQLite 包全为 indirect），配合 `CGO_ENABLED=0` 实现纯 Go 多架构交叉编译；仓库自己的 `cgo` 开关用例是 WebView 提供者的条件编译。
- `-ldflags "-X ..."` 在链接期把 `Version` / `InDocker` 注入 [pkg/base/info.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/info.go)，运行时经 `/api/v1/info` 暴露；`InDocker` 还会改变默认下载目录的业务分支（`/app/Downloads` vs `~/Downloads`）。
- Docker 部署是两阶段构建 + `entrypoint.sh` 权限映射（PUID/PGID/UMASK + su-exec）的组合；官方镜像 `liwei2633/gopeed` 覆盖 linux/386、amd64、arm64、arm/v7 四个架构。
- `cmd/web` 的配置加载优先级是「config.json → `GOPEED_` 前缀环境变量 → 命令行参数覆盖」。

## 7. 下一步学习建议

现在你已经能把项目跑起来，下一步（u1-l3「仓库地图：目录结构与模块职责」）会带你把 `cmd`、`bind`、`pkg`、`internal`、`ui/flutter` 各目录的职责逐一讲清，建立快速定位源码的索引能力。在此之前，建议你顺手做两件事：

1. 在本地仓库执行 `ls cmd/`，对照本讲的入口包清单，看看还有哪些入口（`host`、`updater`、`extmock`）是我们没讲到的，先猜猜它们的用途。
2. 打开 [go.mod](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/go.mod) 通读一遍 require 列表，标记你认识的第三方库（如 `gorilla/mux`、`anacrolix/torrent`、`dop251/goja`、`go.etcd.io/bbolt`），它们分别是后续 HTTP、BT、扩展系统、存储等单元的伏笔。
