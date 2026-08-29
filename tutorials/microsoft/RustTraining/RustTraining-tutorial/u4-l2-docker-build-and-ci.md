# Docker 多阶段构建与 CI 冒烟测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `docker/Dockerfile` 的 builder / runtime 两阶段设计——为什么最终镜像里没有 Rust 工具链、没有 mdbook、也没有书的 Markdown 源码。
2. 讲清楚「优先下载预编译 mdbook 二进制、缺失时回退源码编译」这一策略背后的取舍，以及 `TARGETARCH` 分支如何让同一份 Dockerfile 支持 amd64 与 arm64。
3. 理解两层缓存：Dockerfile 内的 `RUN --mount=type=cache`（cargo registry / target 缓存挂载）与 CI 侧的 `cache-from/cache-to: type=gha`（镜像层缓存）。
4. 读懂 `.github/workflows/docker.yml` 的 build-only 策略与 curl 冒烟断言分别覆盖了哪些回归场景、又故意留下了哪些盲区，并能亲手为它扩充一条新断言。

## 2. 前置知识

本讲是部署篇第二节，默认你已完成 u2-l3（知道 `build_to` 如何把七本书批量构建进 `site/`）和 u4-l1（知道 GitHub Actions 的 workflow、job、step、触发器、`permissions` 是什么）。在此基础上补充几个 Docker 侧的概念：

- **镜像（image）与层（layer）**：Docker 镜像由只读层堆叠而成，Dockerfile 里每条 `RUN`、`COPY`、`FROM` 通常产生一层。层会被缓存：指令及其输入没变，下次构建直接复用，这是 Docker 构建快的根源。
- **构建上下文（build context）**：`docker build` 最后那个 `.` 不是「当前目录」这么简单——它是发送给守护进程的整个目录树，Dockerfile 里的 `COPY` 只能从中取文件。本仓库上下文必须是**仓库根目录**，因为构建既需要七本书的源码也需要 xtask crate。
- **多阶段构建（multi-stage build）**：一个 Dockerfile 里写多个 `FROM`，每个 `FROM` 开启一个新阶段；前一阶段装的工具链不会自动进入后一阶段，只有显式 `COPY --from=builder` 拷过去的文件才会。这是「编译环境与运行环境分离」的手段。
- **BuildKit**：Docker 现代构建引擎，`docker/dockerfile:1` 前端提供 `RUN --mount=type=cache`、自动注入 `TARGETARCH` 等特性；CI 里由 `docker/setup-buildx-action` 显式启用。
- **`TARGETARCH`**：BuildKit 在构建时自动注入的内置变量，取值如 `amd64`、`arm64`，表示目标平台的 CPU 架构——它让一份 Dockerfile 能按架构走不同分支。
- **构建即测试（build-time assertion）**：把 `test -f xxx` 这类检查写进 `RUN`，让「产物缺失」直接变成「镜像构建失败」，而不是等容器跑起来才发现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docker/Dockerfile](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile) | 两阶段镜像定义：builder 阶段装工具链并跑 `cargo xtask build`，runtime 阶段用非特权 nginx 服务静态产物 |
| [.github/workflows/docker.yml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/docker.yml) | Docker CI：路径过滤触发、buildx 构建（只构建不发布）、起容器做 curl 冒烟断言 |
| [docker/README.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/README.md) | 容器部署的使用说明与设计动机（自托管场景、版本钉住、预编译回退） |
| [.dockerignore](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.dockerignore) | 缩小构建上下文：排除 git 数据与本地产物，但刻意不排除 Markdown |
| [docker/nginx.conf](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf) | runtime 阶段的 nginx 配置（本讲只涉及 `try_files`，细节留给 u4-l3） |
| [docker/compose.yaml](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/compose.yaml) | Compose 包装：主机端口可配、容器恒听 8080、非特权加固 |
| [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs) | 承接 u2-l3：`cmd_build` 调 `build_to("site")`，是镜像内实际执行的构建引擎 |

## 4. 核心概念与源码讲解

### 4.1 多阶段构建：让运行时镜像不含工具链

#### 4.1.1 概念说明

u4-l1 讲的 GitHub Pages 流水线把 `docs/` 交给 GitHub 托管；Docker 路线则是**自托管**：适合防火墙内网、无外网访问的环境（见 docker/README.md 开头的定位说明）。但一旦要把站点装进容器，立刻面临一个问题——构建这个站点需要 Rust 工具链 + mdbook + mdbook-mermaid + 全部书源，而**服务**这个站点只需要 nginx + 一堆静态 HTML。如果用单阶段镜像，运行时镜像会背上几百 MB 的编译器和源码，体积大、攻击面大、还可能泄露内部内容。

多阶段构建解决的就是这个「编译环境 ≠ 运行环境」的矛盾：

- **builder 阶段**：装齐工具链，跑和本地完全相同的 `cargo xtask build`，产出 `site/`。
- **runtime 阶段**：从一个干净的非特权 nginx 基础镜像开始，只 `COPY --from=builder` 把 `site/` 拷进来。

最终镜像 = nginx + 静态文件，别的什么都没有。

#### 4.1.2 核心流程

```text
docker build -f docker/Dockerfile .        ← 上下文 = 仓库根
│
├── [builder] rust:1-slim-bookworm
│     apt 装 curl / ca-certificates
│     按 TARGETARCH 装 mdbook + mdbook-mermaid（预编译或回退编译，见 4.2）
│     COPY 整个仓库 → /build
│     cargo run --release --package xtask -- build   → 产出 /build/site/
│     test -f site/index.html（构建期自检，失败即 build 失败）
│
├── [runtime] nginxinc/nginx-unprivileged:1.27-alpine
│     COPY docker/nginx.conf → /etc/nginx/conf.d/default.conf
│     COPY --from=builder /build/site → /usr/share/nginx/html（属主 nginx:nginx）
│     EXPOSE 8080 + HEALTHCHECK（容器内 wget 自检）
│
└── 产物镜像：只有 nginx + HTML，无 Rust、无 mdbook、无书源
```

#### 4.1.3 源码精读

版本钉住在文件头的构建参数里完成，四个版本里三个钉死小版本号：

[docker/Dockerfile:L13-L16](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L13-L16)

这段用 `ARG` 声明可覆盖的默认值：`RUST_VERSION=1`（跟随最新稳定大版本）、mdbook 0.4.52、mdbook-mermaid 0.14.0、nginx 1.27。docker/README.md 特别指出 Pages 流水线当前**未钉** mdbook 版本，所以容器与发布站点可能在上游发版后出现版本差——这是双部署路线的一个已知漂移点。

builder 阶段从 Rust 官方 slim 基础镜像开始，先装最小工具集：

[docker/Dockerfile:L21-L31](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L21-L31)

`slim-bookworm` 是 Debian bookworm 的精简变体，自带 cargo/rustc；`apt` 只装 `ca-certificates`（让后续 HTTPS 下载信任证书链）和 `curl`（下载工具），装完立即清掉 apt 列表——这是控制层体积的标准三连。

接着把整个仓库复制进去并执行构建：

[docker/Dockerfile:L68-L78](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L68-L78)

三个细节值得停下来看：

1. **为什么 `COPY . .` 复制整个仓库**——注释直接回答了：xtask 用编译期宏 `env!("CARGO_MANIFEST_DIR")` 定位项目根（u2-l3 精读过 `project_root`），所以它必须在「源码所在的原始位置」被编译和运行，不能把 xtask 单独抽出来构建。而 `.dockerignore` 保证这个「整个仓库」其实很小：
   [.dockerignore:L4-L12](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.dockerignore#L4-L12)
   排除 `.git`、`target/`、`site/`、`docs/` 与各书本地产物 `**/book/`；文件头注释特意警告「不要排除 `*.md`，书的源码就是 Markdown」。
2. **为什么写 `cargo run --release --package xtask -- build` 而不是 `cargo xtask build`**——这正是 u2-l1 讲过的别名机制：`.cargo/config.toml` 里的别名等价于 `run --package xtask --`，**不带 `--release`**。Dockerfile 需要优化构建的 xtask 自身，于是绕过别名、使用显式完整形式加上 `--release`。命令末尾的 `build` 就是 u2-l2 精读过的 match 分发动词。
3. **构建期自检**——`&& test -f site/index.html`：如果 xtask 因任何原因没生成落地页，`docker build` 在这一层直接失败，不会产出一个「空站点镜像」。最后一行 `echo "==> built $(find site ...)"` 把书目录数量打进构建日志，构建成功时应显示 7。

runtime 阶段切换到非特权 nginx 镜像，只带走两样东西：

[docker/Dockerfile:L85-L93](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L85-L93)

`nginxinc/nginx-unprivileged` 是官方 nginx 镜像的改造版：以 uid 101 运行、监听 8080（>1024，无需 root 与 `NET_BIND_SERVICE` 能力）。`COPY --from=builder --chown=nginx:nginx /build/site` 是两个阶段之间**唯一的物质通道**——builder 里几百 MB 的工具链就此被丢弃。`HEALTHCHECK` 让容器每 30 秒用 wget 自检一次首页，编排器可据此判断容器健康。

#### 4.1.4 代码实践

**实践目标**：亲手完成一次「镜像构建 → 容器启动 → 落地页验证」的完整循环，确认多阶段产物的行为。

**操作步骤**：

```bash
# 1. 必须在仓库根目录（构建上下文需要书源和 xtask）
docker build -f docker/Dockerfile -t rust-training:local .

# 2. 后台启动容器：主机 3000 → 容器 8080
docker run -d --name books -p 3000:8080 rust-training:local

# 3. 验证落地页与一本书
curl -fsS http://localhost:3000/ | grep "<title>Rust Training Books</title>"
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:3000/async-book/

# 4. 观察镜像体积与层内容
docker images rust-training:local
docker history rust-training:local --format '{{.Size}}\t{{.CreatedBy}}' | head

# 5. 清理
docker rm -f books
```

**需要观察的现象**：

- 构建日志中 builder 阶段出现 `==> installed ... from prebuilt binary`（amd64 机器上两行都是；arm64 机器上 mdbook-mermaid 那行是 `compiling from source`）。
- 构建日志末尾出现 `==> built 7 books`。
- `docker history` 里 runtime 阶段的层只有 nginx 基础镜像 + 配置 + site 目录，找不到任何 `cargo`/`rustc` 痕迹。

**预期结果**：第二条 curl 输出 `200`；镜像内无工具链。构建与运行耗时因机器而异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 runtime 阶段的 `COPY --from=builder` 改成 `COPY --from=builder /usr/local/bin/mdbook /usr/local/bin/`，镜像还能正常服务吗？

**答案**：能正常服务——mdbook 只是静态站点的**构建期**工具，nginx 服务的是纯 HTML，运行期根本不需要它。这恰恰说明把 mdbook 拷进运行时镜像是无意义的负担，仓库没这么做是对的。

**练习 2**：为什么 Dockerfile 里跑的是 `xtask build`（输出到 `site/`）而不是 `xtask deploy`（输出到 `docs/`）？

**答案**：[docker/README.md:L44-L46](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/README.md#L44-L46) 明确回答：两条命令产出内容完全一致，`deploy` 只是把输出目录换成 `docs/` 并多打印一段 GitHub Pages 发布指引——在容器场景毫无意义。这是 u2-l3「`build` 与 `deploy` 共用 `build_to` 引擎、仅差出口目录」结论的直接应用。

**练习 3**：`.dockerignore` 排除了 `site/`，但 Dockerfile 里又执行了会生成 `site/` 的构建命令，两者矛盾吗？

**答案**：不矛盾。`.dockerignore` 只影响**进入构建上下文**的文件（防止把你本地的旧 `site/` 拷进镜像造成污染）；builder 阶段在容器内新生成的 `/build/site/` 是 `RUN` 的产物，与上下文无关。这保证了镜像内永远是干净构建。

### 4.2 预编译回退策略：install_tool 与 TARGETARCH 分支

#### 4.2.1 概念说明

builder 阶段要装两个外部工具：mdbook 与 mdbook-mermaid。装法有两种：

- `cargo install mdbook`：从源码编译。通用，任何有 Rust 工具链的架构都能成，但冷构建要多花几分钟。
- 下载上游 GitHub Release 的**预编译二进制**：秒级，但上游不一定为每个架构都发布了产物。

Dockerfile 的策略是「先试预编译，404 就回退源码编译」。注释把动机和现状写得很直白：

[docker/Dockerfile:L33-L40](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L33-L40)

——`cargo install` 会「给每次冷构建增加数分钟」；上游产物不齐：mdbook 在 amd64 有 linux-gnu、arm64 有 linux-musl，而 **mdbook-mermaid 完全没有发布 arm64 Linux 二进制**；musl 构建是静态链接的，放在 glibc 基础镜像上也能跑。

#### 4.2.2 核心流程

```text
RUN 开始
├── set -eux                    # 打印每条命令；出错立即中止
├── case "${TARGETARCH:-amd64}" # 按目标架构选「目标三元组」
│     amd64 → mdbook/mermaid 都用 x86_64-unknown-linux-gnu
│     arm64 → 都用 aarch64-unknown-linux-musl
│     其他  → 报错退出（显式不支持，而不是静默走错分支）
├── install_tool mdbook         <release-url>  crate名  版本
│     ├── curl 下载 tar.gz 并解到 /usr/local/bin 成功 → 用预编译
│     └── 失败（如 404）→ cargo install <crate> --version <ver> --locked --root /usr/local
├── install_tool mdbook-mermaid（同上）
└── mdbook --version; mdbook-mermaid --version   # 装完立即验证可执行
```

「目标三元组」（u3-l6 讲过：`arch-vendor-os-env`）在这里的作用是把 `TARGETARCH` 这种粗粒度架构名翻译成上游 Release 资产文件名里的精确三元组。

#### 4.2.3 源码精读

架构分支用 BuildKit 自动注入的 `TARGETARCH` 驱动：

[docker/Dockerfile:L41-L46](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L41-L46)

`${TARGETARCH:-amd64}` 的 `:-` 是 shell 的「未定义则取默认值」写法，兼容未走 BuildKit 的场景。`*)` 分支不静默兜底而是 `exit 1`——遇到不认识的架构就明确失败，好过用错误的三元组拼出一个必然 404 的 URL 再莫名走源码编译。

回退逻辑封装在一个 shell 函数里：

[docker/Dockerfile:L48-L56](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L48-L56)

`install_tool` 收四个参数（二进制名、Release 下载 URL、crate 名、版本号）。核心是那条 `if curl ... | tar ...` 判断：`curl -fsSL` 在 404 时返回非零（`-f` 的作用），管道整体失败即落入 `else` 分支执行 `cargo install --locked --root /usr/local`——`--locked` 保证按 crate 的锁定文件编译依赖，`--root /usr/local` 让二进制直接进 PATH。两处 `2>/dev/null` 把「探测失败」的报错噪音吞掉，因为失败是预期内的正常路径。

随后两次调用安装两个工具并验证：

[docker/Dockerfile:L58-L66](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L58-L66)

URL 由版本 ARG 与三元组变量拼出。在 amd64 上两次都命中预编译（日志出现 `==> installed ... from prebuilt binary`）；在 arm64 上 mdbook-mermaid 的 URL 落空，回退源码编译（日志出现 `==> no prebuilt ... compiling from source`）。末尾两条 `--version` 是廉价断言：解包出的 musl 二进制若真的无法在 glibc 镜像里运行，这里立刻暴露，而不是等到构建书的时候。

#### 4.2.4 代码实践

**实践目标**：不动 Docker，直接验证「预编译资产是否存在」，用 HTTP 状态码亲眼确认回退策略针对的现实。

**操作步骤**：

```bash
# amd64 的 mdbook 资产：预期 200
curl -fsSIL -o /dev/null -w '%{http_code}\n' \
  "https://github.com/rust-lang/mdBook/releases/download/v0.4.52/mdbook-v0.4.52-x86_64-unknown-linux-gnu.tar.gz"

# arm64 的 mdbook-mermaid 资产：Dockerfile 注释声称不存在，预期 404（curl -f 会非零退出）
curl -fsSIL -o /dev/null -w '%{http_code}\n' \
  "https://github.com/badboy/mdbook-mermaid/releases/download/v0.14.0/mdbook-mermaid-v0.14.0-aarch64-unknown-linux-musl.tar.gz"
```

**需要观察的现象**：第一个命令打印 `200`；第二个命令以非零退出（因为 `-f` 遇 404 报错），加 `-v` 或去掉 `-f` 能看到 `HTTP/1.1 404 Not Found`。

**预期结果**：与 Dockerfile L38-L39 的注释一致。上游 Release 资产随版本变化，**待本地验证**；若你拿到 200，说明上游后来补发了 arm64 资产，Dockerfile 的注释与回退分支就该更新了——这正是「注释也会烂」的活例。

#### 4.2.5 小练习与答案

**练习 1**：为什么 amd64 用 `linux-gnu`、arm64 的 mdbook 却选 `linux-musl`？

**答案**：不是技术偏好，是**上游发布了什么就用什么**——Dockerfile L38-39 注明 mdbook 在 amd64 发布 linux-gnu、在 arm64 只发布 linux-musl。musl 版是静态链接，不依赖 glibc 符号，所以在 glibc 的 bookworm 基础镜像上照常运行（L40 注释）。

**练习 2**：把 `install_tool` 里 `curl` 的 `-f` 去掉，回退机制会发生什么？

**答案**：`-f` 让 curl 在 HTTP ≥400 时返回失败。去掉后，404 响应体（GitHub 的错误页 HTML）会被当成合法内容交给 `tar` 解包——`tar` 大概率仍然解不动而失败，回退还能触发；但若错误体恰好是合法 gzip，`tar` 可能解出垃圾文件而「成功」，跳过回退且留下坏的 `/usr/local/bin/mdbook`，直到 L65 的 `--version` 才暴露。`-f` 是让失败尽早、语义准确地发生。

**练习 3**：这套策略为什么写在 Dockerfile 里，而不是让 CI 先 `cargo install` 再 docker build？

**答案**：工具安装属于镜像构建的一部分，写进 Dockerfile 意味着**任何人**在任何机器 `docker build` 都得到同样结果，不依赖 CI 环境；同时安装层会被 Docker 层缓存（版本 ARG 不变就跳过），CI 侧只需缓存层即可复用。若挪到 CI 外部安装，镜像的可复现性就寄挂在流水线脚本上了。

### 4.3 buildx 缓存：RUN --mount 与 type=gha

#### 4.3.1 概念说明

预编译解决了 mdbook 的安装耗时，但 4.1 的构建层还有一处必须编译的东西：xtask 自己（及其唯一外部依赖 ctrlc，u2-l1 讲过）。`cargo run --release` 的冷编译要拉取 registry、编译依赖——在「改一行书稿就重建镜像」的迭代场景下每次都重来是不可接受的。本模块讲两层互补的缓存：

1. **Dockerfile 内的缓存挂载**（`RUN --mount=type=cache`）：把 cargo 的 registry 与 `target/` 目录挂成跨构建持久的缓存卷。与「层缓存」的区别：缓存挂载**不进镜像层**，纯粹是构建过程中的加速旁路，所以既不撑大镜像，也不因「`COPY . .` 任何文件变了导致层失效」而丢失——哪怕书源变化让整条 `RUN` 重跑，编译缓存还在。
2. **CI 侧的镜像层缓存**（`type=gha`）：把构建产生的**层**（包括 builder 阶段的中间层）存进 GitHub Actions 的缓存后端，下次构建 `cache-from` 取回，未变的层直接复用。

#### 4.3.2 核心流程

```text
本地/CI 构建
├── 第 1 行 # syntax=docker/dockerfile:1        ← 声明用 dockerfile:1 前端（解锁 --mount 等）
├── RUN --mount=type=cache,target=/usr/local/cargo/registry   ← 依赖源码缓存卷
│       --mount=type=cache,target=/build/target               ← 编译产物缓存卷
│       cargo run --release --package xtask -- build
└─ CI 额外：
      setup-buildx-action 启用 BuildKit builder
      cache-from: type=gha        ← 从 GHA 缓存导入层
      cache-to: type=gha,mode=max ← 导出层缓存，max = 连中间层一起存
```

#### 4.3.3 源码精读

Dockerfile 首行的 parser 指令与构建层的两个缓存挂载：

[docker/Dockerfile:L74-L78](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L74-L78)

（配套的前置声明在 [docker/Dockerfile:L1](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L1)：`# syntax=docker/dockerfile:1` 确保 BuildKit 使用 dockerfile:1 前端，`RUN --mount` 这类特性才可用。）两个挂载点分别对应 cargo 的两个缓存目录：`/usr/local/cargo/registry`（官方 Rust 镜像里 CARGO_HOME 在 `/usr/local/cargo`，存下载的依赖源码）与 `/build/target`（编译中间产物）。注意它们挂在 `RUN` **内部**而非镜像里——这正是缓存挂载与 `COPY` 的本质差异：加速构建但不污染产物。

CI 侧由 buildx action 与 build-push-action 的参数完成同样的使命：

[.github/workflows/docker.yml:L32-L43](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/docker.yml#L32-L43)

`docker/setup-buildx-action@v3` 创建 BuildKit builder（这是使用 `type=gha` 缓存后端的前提）；`docker/build-push-action@v6` 是 buildx 的官方 Action 封装，关键参数：

- `context: .` + `file: docker/Dockerfile`——与本地构建同上下文、同 Dockerfile，CI 与本地单一事实来源（和 u4-l1 里「CI 复用 `cargo xtask deploy`」是同一设计哲学）。
- `push: false` + `load: true`——**只构建不发布**：镜像经 `--load` 进 runner 本地 daemon，供下一步冒烟测试 `docker run`；不触碰任何 registry。
- `cache-from/cache-to: type=gha`——层缓存走 GitHub Actions cache；`mode=max` 把**所有中间层**（含 builder 阶段那些体积可观的层）都导出，默认的 `mode=min` 只存最终镜像层，对多阶段构建几乎没用。

#### 4.3.4 代码实践

**实践目标**：直观感受缓存挂载对重复构建的加速。

**操作步骤**：

```bash
# 第一次：冷构建，计时
time docker build -f docker/Dockerfile -t rust-training:t1 .

# 改动一个无关文件（例如往 README.md 追加一行空行），触发 COPY 层失效
echo "" >> README.md

# 第二次：COPY 层失效 → RUN 重跑，但 cargo 缓存挂载命中
time docker build -f docker/Dockerfile -t rust-training:t2 .

# 还原 README（不要把无关改动留在工作区）
git checkout -- README.md
```

**需要观察的现象**：第二次构建里 `cargo run --release` 那一步明显快于第一次（依赖已编译，缓存在 `/build/target` 缓存卷中直接复用）；构建日志中 `Caching ...` / `Restoring ...` 相关的 BuildKit 输出。

**预期结果**：第二次构建该步骤从「完整编译 xtask + ctrlc」缩短为「近乎直接链接执行」。具体耗时取决于机器与网络，**待本地验证**。注意：`echo "" >> README.md` 会修改仓库文件，实验后务必用 `git checkout` 还原（上面第 4 步已包含）。

#### 4.3.5 小练习与答案

**练习 1**：缓存挂载和 Docker 层缓存都加速构建，为什么不只用层缓存？

**答案**：层缓存键包含指令与输入。构建层是 `COPY . .` 之后的 `RUN`——**任何**文件变化（改一行书稿）都会让 `COPY` 层失效，进而连带后面所有层重跑，层缓存对高频内容迭代无效。缓存挂载独立于层失效逻辑：`RUN` 重跑时挂载点里的旧产物依然可用。两者是互补关系，不是替代关系。

**练习 2**：CI 里如果把 `mode=max` 去掉（回到默认 `min`），会发生什么？

**答案**：`mode=min` 只导出最终镜像的层。本镜像的最终层是 runtime 阶段的 nginx + 静态文件，而真正昂贵的 builder 阶段中间层**不会被缓存**——下次 CI 里 `cargo run --release` 与工具安装全部重来。对多阶段构建，`mode=max` 几乎是必须的。

**练习 3**：`type=gha` 缓存放满了 GitHub Actions 的配额会有什么后果？构建会失败吗？

**答案**：通常不会让构建失败——GHA 缓存超配额时按 LRU 逐出旧缓存，构建照常进行，只是下次可能缓存未命中而变慢。这也是为什么缓存键的设计（此处由 build-push-action 自动以构建输入为键）比「担心溢出」更重要。

### 4.4 CI 冒烟测试：build-only 策略与 curl 断言

#### 4.4.1 概念说明

docker.yml 的存在理由写在文件最前面：**构建出来的镜像不发布到任何地方**。那这条流水线图什么？注释自答：防止 Dockerfile 「悄悄烂掉」（silently rot）——Dockerfile 依赖外部 URL、基础镜像、xtask 行为，任何一环漂移都可能让它在某人某天急需自托管时才发现已经构建不了。一条每次相关改动都跑一遍的 build-only 流水线，就是最便宜的防腐剂；而且不发布意味着**零新增攻击面**：不需要 registry 凭据、不产生镜像分发。

构建之后，流水线在同一 runner 上起容器做**冒烟测试**（smoke test）：源自「插上电看看冒不冒烟」的硬件测试思路，指最小化的「还活着吗」验证——这里用几条 curl 断言确认镜像能起、站点能看。

#### 4.4.2 核心流程

```text
触发（push 到 main / PR / 手动），且改动命中：
  docker/** · .dockerignore · xtask/** · **/book.toml · docker.yml 自身
    ↓ permissions: contents: read（最小权限）
job build:
  ① checkout
  ② setup-buildx
  ③ build-push-action：构建 rust-training:ci，--load 进本地 daemon，GHA 层缓存
  ④ smoke test：
     docker run -d -p 3000:8080 rust-training:ci
     轮询最多 30×2s 等 nginx 就绪，超时则 ::error:: + docker logs + exit 1
     断言 A：落地页含 <title>Rust Training Books</title>
     断言 B：/async-book/ → 200（至少一本书在）
     断言 C：/async-book/ch00-introduction（无扩展名）→ 200（try_files 生效）
     docker rm -f books
```

#### 4.4.3 源码精读

触发器用 `paths` 过滤，只在镜像相关面变化时才跑：

[.github/workflows/docker.yml:L5-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/docker.yml#L5-L24)

五类路径：`docker/**`（镜像定义）、`.dockerignore`（上下文）、`xtask/**`（构建引擎）、`**/book.toml`（书的构建配置——会影响产物结构）与 workflow 自身。注意 `**/*.md` 的书稿内容**不在其中**：改文案不改变镜像结构（HTML 会变，但断言只查状态码与 title 标签），跳过以省资源。对比 u4-l1 的 pages.yml（push 到 main 全量触发），两条流水线的触发面是按各自产物特性裁剪的。`permissions: contents: read` 是比 pages.yml 三连权限更小的子集——因为这条流水线连部署都不做。

冒烟测试段是本模块的核心：

[.github/workflows/docker.yml:L45-L61](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/docker.yml#L45-L61)

逐段拆解：

- **就绪轮询**（L47-L54）：`docker run -d` 后台起容器（`-p 3000:8080` 与本地用法一致，容器恒听 8080 是 nginx-unprivileged 的约束，见 [docker/compose.yaml:L13-L16](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/compose.yaml#L13-L16) 的注释与映射）。`for i in $(seq 1 30)` + `sleep 2` 给 nginx 最多 60 秒启动窗口；`${ok:-0}` 用默认值规避「变量未定义时 set -u 报错」的隐患。超时路径三件套：`::error::`（GitHub Actions 的注解命令，把消息直接标红在 PR 的 Annotations 区）+ `docker logs books`（把容器日志吐进构建日志，便于排障）+ `exit 1`。
- **断言 A——落地页存在且是「我们的」**（L57）：`grep -q "<title>Rust Training Books</title>"`。L56 的 `NB:` 注释解释了为什么匹配 `<title>` 而不是 `<h1>`：u2-l4 讲过落地页的 `<h1>` 里嵌着带颜色的 `<span>` 标签，直接 grep 会被标签切开而失配。这条断言同时覆盖了「xtask 构建成功」与「write_landing_page 没有变形」两件事。
- **断言 B——至少一本书构建成功**（L58）：`curl -fsS` 的 `-f` 再次出场：HTTP ≥400 时 curl 以非零退出，`grep -q 200` 双保险校验状态码字面量。它精准补上了 u2-l3 遗留的盲区——`build_to` 对单本书失败只打印告警、退出码仍是 0，镜像会「绿绿地」缺书构建成功；这条断言让 **async-book 缺失**的情形在 CI 变红。但注释明说策略是 "at least one book"（L55）：另外六本书缺失依旧检测不到——全面性与 CI 时长的折衷。
- **断言 C——无扩展名链接可达**（L60）：`/async-book/ch00-introduction` 不带 `.html`。mdbook 为 `src/ch00-introduction.md` 生成 `ch00-introduction.html`，而 mdbook 的内部链接常省略扩展名，靠 nginx 的 `try_files $uri $uri/ $uri.html =404` 兜住（[docker/nginx.conf:L15-L16](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L15-L16)，完整解读留给 u4-l3）。这条断言守护的是「容器行为与 GitHub Pages 行为对齐」——注释原话是保持与 Pages 一致的解析习惯。

**覆盖面小结**（这也是「读懂 CI 断言覆盖的回归场景」的答案）：

| 断言 | 守护的回归 |
| --- | --- |
| 就绪轮询 | 镜像起不来、nginx 崩溃、端口配错 |
| A：title | xtask 构建失败致落地页缺失、落地页模板被改坏 |
| B：/async-book/ → 200 | async-book 构建失败但 build_to 退出码为 0 的静默缺书 |
| C：无扩展名 → 200 | nginx.conf 的 try_files 被改坏、链接解析与 Pages 失配 |
| （盲区） | 其余六本书的缺失、页面内容的正确性 |

#### 4.4.4 代码实践

**实践目标**：为冒烟测试扩充一条断言——验证**另一本书**的具体章节页可达（现有断言只覆盖 async-book）。

**操作步骤**：

1. 打开 `.github/workflows/docker.yml`，在 L60（`ch00-introduction` 断言）之后加一行（**示例代码**，加在自己 fork 的分支上，不要直接提交到仓库主干）：

```yaml
          # A chapter page of another book must resolve too.
          curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:3000/python-book/ch02-getting-started | grep -q 200
```

2. URL 依据：`python-book/src/ch02-getting-started.md` 真实存在（本讲 4.4.3 已核对各书章节文件名），mdbook 输出 `python-book/ch02-getting-started.html`，无扩展名请求经 try_files 第三候选命中。
3. 本地预演（不推 CI，直接用 4.1 构建好的镜像模拟整段脚本）：

```bash
docker run -d --name books -p 3000:8080 rust-training:local
# 等 2 秒后逐条执行你想验证的 curl，包括新加的那条
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:3000/python-book/ch02-getting-started
docker rm -f books
```

**需要观察的现象**：新 curl 输出 `200`；把 URL 里 `ch02-getting-started` 故意改成 `ch99-not-exist` 再跑一次，观察 `curl -fsS` 因 404 而非零退出——证明这条断言真的具备「让它红」的能力。

**预期结果**：正确 URL 得 200；伪造 URL 得 404 且 curl 退出码 22。若在 CI 里验证，需推送到 fork 并让改动命中 L8-L13 的 paths 过滤（改了 docker.yml 自身就会命中）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么冒烟脚本要自己写轮询循环，而不是起完容器立刻 curl？

**答案**：`docker run -d` 返回只代表容器**创建并启动**了，不代表 nginx 已完成监听。立刻 curl 会撞上「连接拒绝」，得到假阴性。轮询 30×2 秒给了启动窗口，兼顾慢 runner 上的稳定性——和 u4-l1 里 pages.yml 依赖 Actions 原语不同，这里是脚本层的手写重试。

**练习 2**：断言 B 用了 `-f` 又用 `grep -q 200`，`-f` 不就够了吗？

**答案**：`-f` 只在 ≥400 时失败，若服务器返回 3xx（比如错误的重定向），`-f` 会放行（curl 默认不跟随重定向时 3xx 不算错误）。`-w '%{http_code}'` 显式打印状态码再用 `grep -q 200` 收紧到「必须是 200」，双重校验比单靠 `-f` 更精确。

**练习 3**：这条流水线叫「Docker image」却从不 push 镜像，怎么理解它的价值？

**答案**：它是**回归防线**而非交付通道：保证 Dockerfile、.dockerignore、xtask、book.toml 的任意相关改动后镜像依然可构建、可启动、可访问。不 push 反而是优点——不引入 registry 凭据与镜像分发面（L3-L4 注释原意）。真正对外发布站点的是 pages.yml 那条流水线（u4-l1）。

## 5. 综合实践

**任务：本地完整复刻 docker.yml 的「构建 → 冒烟 → 扩充断言 → 验证失败路径」全过程。**

1. **构建**：在仓库根执行 `docker build -f docker/Dockerfile -t rust-training:local .`，在构建日志里找出三处证据：`==> installed mdbook from prebuilt binary`（或回退编译）、`==> built 7 books`、runtime 阶段仅两条 `COPY`。
2. **起容器并复刻轮询**：`docker run -d --name books -p 3000:8080 rust-training:local`，然后用 shell 循环复刻 docker.yml L48-L54 的等待逻辑，确认 `ok=1` 分支被走到。
3. **跑全部既有断言**：依次执行 docker.yml L57-L60 的三条 curl/grep，确认全部通过。
4. **扩充断言**：加入 4.4.4 的新行，验证 `/python-book/ch02-getting-started` 返回 200。
5. **验证失败路径**：把新断言的 URL 改成不存在的章节，确认整段脚本以非零退出——一条不能失败的断言等于没有断言。
6. **清理与还原**：`docker rm -f books`；确认 `git status` 干净（本实践只读仓库文件，任何对 docker.yml 的练习性修改都应只存在于你的 fork 或已被丢弃）。

完成后你应当能回答：这条流水线守护了哪四类回归、留了哪两类盲区（其余六本书、内容正确性），以及为什么它选择 build-only。**整个流程的运行结果待本地验证。**

## 6. 本讲小结

- **多阶段构建**把「编译环境」与「运行环境」分离：builder（rust:1-slim-bookworm）装工具链、跑与本地完全相同的 xtask 构建；runtime（nginx-unprivileged）只通过一条 `COPY --from` 拿走 `site/`，最终镜像无 Rust、无 mdbook、无书源，且以 uid 101 监听 8080 非特权运行。
- **预编译回退策略**：`install_tool` 先试 GitHub Release 预编译二进制，404 则 `cargo install --locked` 源码编译；`TARGETARCH` 分支按架构选目标三元组，musl 静态二进制兼容 glibc 基础镜像——速度优先、可用性兜底。
- **两层缓存**：Dockerfile 内 `RUN --mount=type=cache` 缓存 cargo registry 与 target（不进镜像层、不怕 COPY 失效）；CI 侧 `cache-to: type=gha,mode=max` 把含 builder 中间层在内的镜像层存进 Actions 缓存。
- **build-only CI**：docker.yml 只构建不发布（`push: false` + `load: true`），零 registry 凭据、零发布面，价值在于防止 Dockerfile 悄悄烂掉；触发用五类 paths 精确裁剪。
- **冒烟断言覆盖**：nginx 就绪轮询、落地页 title、async-book 目录 200（堵住 build_to「缺书仍退出 0」的盲区）、无扩展名章节 200（守护 try_files 与 Pages 行为对齐）；盲区是其余六本书与页面内容正确性。

## 7. 下一步学习建议

本讲只借用了 nginx.conf 的一条 `try_files`，而 runtime 阶段的完整加固——安全响应头为何在每个 `location` 重复声明、静态资源缓存与 gzip 策略、compose.yaml 的 `no-new-privileges` 与只读探索——正是下一讲 **u4-l3《nginx 运行时与安全加固》**的主题。

随后建议进入 **u4-l4《综合实战：向仓库添加一本新书》**：你会看到新书只需注册进 BOOKS，本讲的两条流水线（Pages 与 Docker）就自动收录它——`**/book.toml` 的 paths 过滤会命中、镜像内的 `find site` 计数会从 7 变 8，无需改任何 CI 代码。若想对照阅读另一条流水线，可回看 u4-l1 的 pages.yml，比较两条流水线在触发策略、权限、缓存与「部署 vs 不部署」上的取舍。
