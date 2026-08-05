# 安装与本地运行

## 1. 本讲目标

学完本讲，你应当能够：

- 区分 vLLM Semantic Router（以下简称 vLLM SR）的**两条安装/运行路径**：面向终端用户的 `install.sh` 一键安装器，与面向开发者的 `make` 目标体系；
- 读懂根 `Makefile` 如何用「sub-makefile 组合」的方式把三十多个 `tools/make/*.mk` 拼成一个统一的构建系统；
- 说出 `cpu-local` / `amd-local` / `nvidia-local` 三种本地环境的「构建命令 + 启动命令 + 默认镜像」三元组，并理解 `VLLM_SR_PLATFORM` 变量如何贯穿「构建期」与「运行期」；
- 理解仓库强制的 **local image flow（本地镜像流）** 开发约定：本地开发一律本地构建镜像、用 `--image-pull-policy never` 启动，绝不隐式拉取已发布的远程镜像。

本讲承接 u1-l2「仓库结构与目录组织」——你已经知道 `src/semantic-router/`(Go 路由内核) 与 `src/vllm-sr/`(Python CLI) 的入口定位，本讲就回答「这些入口怎么被构建出来、又怎么在本地跑起来」。

## 2. 前置知识

阅读本讲前，建议你先具备以下常识：

- **容器运行时（container runtime）**：本项目本地运行以 Docker（或 Podman）容器为载体。`vllm-sr serve` 最终会调用 `docker run` 拉起 Envoy、router、dashboard 等一组容器。你可以暂时把「容器」理解为「一个打包好运行环境的轻量虚拟机」。
- **Make 与 Makefile**：`make` 是一个经典的构建工具，通过读取 `Makefile` 里的「目标（target）: 依赖（dependency）」规则来执行命令。你只需记住：`make <目标名>` 会触发对应规则里写好的 shell 命令。
- **Python venv（虚拟环境）**：`install.sh` 会创建一个独立的 Python 虚拟环境来安装 `vllm-sr` CLI，避免污染系统 Python。
- **CGO**：Go 路由器依赖 Rust 写的推理绑定（`candle-binding` / `ml-binding` / `nlp-binding`），通过 CGO（Go 调用 C/Rust）链接进来，所以 Go 端构建前必须先编译 Rust 库。
- **平台（platform）概念**：本项目把硬件后端抽象成 `cpu` / `amd`(ROCm) / `nvidia`(CUDA) 三类平台。同一个 `--platform` 标志既影响「构建哪个 Dockerfile、用哪个镜像名」，也影响「启动时是否挂载 GPU」。

> 名词速查：`CLI` = 命令行工具（这里指 Python 写的 `vllm-sr` 命令）；`serve` = `vllm-sr` 的子命令，用来在本地拉起整套服务栈；`recipe` = 路由配方（一份 YAML 配置），后续 u3 会专门讲。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `install.sh` | 终端用户一键安装器：建 venv、装 `vllm-sr` CLI、可选地装 Docker、首次 `vllm-sr serve` 并打开 dashboard |
| `Makefile`（根） | 极薄的「分发器」，本身不含构建逻辑，只负责把所有 `tools/make/*.mk` 组合起来再委托执行 |
| `tools/make/*.mk` | 真正的构建/运行/测试规则，按职责拆成三十多个子 makefile（golang / rust / docker / envoy / milvus …） |
| `tools/make/docker.mk` | 容器镜像构建与 `vllm-sr-dev` / `vllm-sr-build` / `vllm-sr-start` 等本地开发目标，以及 `VLLM_SR_PLATFORM` 平台分支 |
| `tools/make/build-run-test.mk` | Go 路由器二进制构建（`build-router`）、单元测试（`test-semantic-router`）等 |
| `tools/make/envs.mk` | 全局环境变量定义，例如 `CONTAINER_RUNTIME`（docker 或 podman） |
| `tools/make/common.mk` | 公共定义，例如 `LOG_TARGET`（每个目标执行前打印的绿色提示行） |
| `AGENTS.md` | 仓库工作约定，含「支持的环境」清单与「local image flow」不可违背规则 |
| `tools/agent/docs/environments.md` | 三种本地环境（cpu/amd/nvidia）+ ci-k8s 的权威说明，本讲的「环境矩阵」就来自这里 |
| `README.md` | 项目首页，给出官方安装一行命令 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 安装方式**：`install.sh` 一键安装器；
- **4.2 Make 目标体系**：sub-makefile 组合架构；
- **4.3 本地环境矩阵**：cpu / amd / nvidia 三元组与 local image flow。

### 4.1 安装方式：install.sh 一键安装器

#### 4.1.1 概念说明

`install.sh` 是面向**终端用户**的安装入口。它解决一个问题：让一个对项目内部结构毫无了解的人，用一条命令把 `vllm-sr` 这个 CLI 装好，并在本地跑起来、打开管理面板。

它的关键设计有三个：

1. **隔离安装**：不污染系统 Python。它在 `INSTALL_ROOT`（默认 `~/.local/share/vllm-sr`）下建一个独立 venv，再把 `vllm-sr` 装进去。
2. **薄启动器（thin launcher）**：在 `BIN_DIR`（默认 `~/.local/bin`）写一个 shell 脚本，内容只是 `exec` 指向 venv 里的真实可执行文件，从而让你能在任意目录敲 `vllm-sr`。
3. **可声明、可预测**：几乎所有行为都能用环境变量或命令行参数覆盖，安装器先打印一份「安装计划（install plan）」再动手，所见即所得。

> 注意区分：`install.sh` 安装的是 **Python CLI `vllm-sr`**，而不是直接编译 Go 路由器。CLI 在 `vllm-sr serve` 时会去用**容器镜像**（这些镜像由 4.2 的 `make` 目标或官方发布提供）来跑 Go 路由器。也就是说，`install.sh` 是「装一个能拉起容器的指挥官」，而真正的路由器二进制藏在容器镜像里。

#### 4.1.2 核心流程

`install.sh` 的 `main()` 函数清晰地列出了安装的七个阶段（顺序不能乱，后一步依赖前一步）：

```text
parse_args        # 解析 --mode/--runtime/--channel/--platform 等参数
  ↓
validate_args     # 校验参数取值合法（mode∈{cli,serve}、runtime∈{auto,docker,skip}…）
  ↓
detect_os         # 探测操作系统（仅支持 macOS / Linux）
  ↓
print_logo        # 打印 logo
print_install_plan# 打印「安装计划」：检测到的环境 + 即将执行的步骤
  ↓
install_cli       # 找/装 Python → 建 venv → 装 vllm-sr → 写 launcher → 装补全
  ↓
ensure_runtime    # 若是 serve 模式，确保有可用的容器运行时（Docker/Colima）
  ↓
launch_first_session # 自动首次 vllm-sr serve → 检查 dashboard → 打开浏览器
  ↓
print_next_steps  # 打印「下一步」：如何 stop/restart/访问 dashboard
```

其中两个分支值得记住：

- `--mode cli`：只装 CLI，跳过 `ensure_runtime` 和 `launch_first_session`，适合「我只想要命令行工具、自己管运行时」的场景。
- `--mode serve`（默认）：装好 CLI 后还会确保 Docker 可用，并自动跑一次首次 serve。

#### 4.1.3 源码精读

**安装计划的可声明变量**——开头这一组环境变量就是 `install.sh` 的全部「旋钮」，每个都给了默认值：

[install.sh:4-12](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L4-L12) 定义了 `MODE`、`REQUESTED_RUNTIME`、`INSTALL_ROOT`、`BIN_DIR`、`PIP_SPEC`、`REQUESTED_CHANNEL`、`PYTHON_BIN`、`REQUESTED_PLATFORM`、`AUTO_LAUNCH` 这些可覆盖变量。例如默认 `MODE=serve`、`REQUESTED_CHANNEL=dev`（装最新开发版）。

**主流程七步**——这就是 4.1.2 那张流程图在源码里的真身：

[install.sh:1003-1015](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L1003-L1015) 里的 `main()` 依次调用 `parse_args / validate_args / detect_os / print_logo / print_install_plan / install_cli / ensure_runtime / launch_first_session / print_next_steps`。读这十行就能掌握整个安装器的骨架。

**CLI 安装细节**——`install_cli` 是最核心的一步：

[install.sh:650-685](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L650-L685) 展示了完整动作：`find_python` 找一个 Python 3.10+（找不到就调 `install_python` 自动装）→ 在 `INSTALL_ROOT/venv` 建 venv → `pip install` 引导 `pip/setuptools/wheel` → `install_requested_package` 装 `vllm-sr` → `create_launcher` 写启动器 → 尝试装 shell 补全。

**薄启动器**——为什么装完能在任意目录敲 `vllm-sr`？答案在这里：

[install.sh:613-626](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L613-L626) 的 `create_launcher` 在 `$BIN_DIR/vllm-sr` 写一个 shell 脚本，其核心只有一行 `exec "$executable_path" "$@"`，其中 `executable_path` 指向 `INSTALL_ROOT/venv/bin/vllm-sr`。把 `$BIN_DIR` 放进 `PATH` 后，`vllm-sr` 这个名字就随处可用了。

**平台自动探测**——安装器如何知道你是 AMD 机器？

[install.sh:300-317](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L300-L317) 的 `resolve_launch_platform` 在 `REQUESTED_PLATFORM=auto` 时，通过探测 `rocm-smi` / `rocminfo` / `/dev/kfd` / `/opt/rocm` 这些 ROCm 特征来判断是否为 `amd` 平台；都不命中则返回空（即默认 CPU）。

**首次自动启动**——为什么安装完会自动弹出浏览器？

[install.sh:791-847](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L791-L847) 的 `launch_first_session` 在 `should_auto_launch` 为真时，执行一次 `vllm-sr serve [--platform ...]`，再用 `vllm-sr dashboard --no-open` 探活，最后 `open_dashboard_url` 尝试打开 `http://localhost:8700`。这就是 dashboard 默认端口 `8700` 的来源（见 [install.sh:26](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L26)）。

**官方一行命令**——README 把上面这一切封装成一句话：

[README.md:36-42](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/README.md#L36-L42) 给出 `curl -fsSL https://vllm-sr.ai/install.sh | bash`，它会远程拉取并执行本仓库的 `install.sh`。

#### 4.1.4 代码实践

**实践目标**：不真正改动机器，仅通过「阅读」`install.sh` 还原它在你机器上将执行的步骤；然后再用 `--help` 触发一次纯打印、零副作用的预演。

**操作步骤**：

1. 在仓库根目录查看安装器的全部可配置项：

   ```bash
   bash install.sh --help
   ```

   预期能看到 `usage()` 输出的完整参数表（对应 [install.sh:186-228](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L186-L228)）。

2. 用「只装 CLI、不启动」模式做一次最小化预演（仍会真实建 venv、装包，但跳过自动 serve）：

   ```bash
   bash install.sh --mode cli --no-launch
   ```

3. 观察 `print_install_plan` 打印的「Detected environment」与「Install plan」两块——它会列出你当前的 platform、python、runtime、package channel 等。

**需要观察的现象**：

- `--help` 只打印帮助后退出（`exit 0`），不创建任何文件；
- `--mode cli --no-launch` 会真实创建 `~/.local/share/vllm-sr/venv` 并在 `~/.local/bin/vllm-sr` 写入启动器，但因为 `--no-launch`，不会触发 `vllm-sr serve`。

**预期结果**：执行 `~/.local/bin/vllm-sr --version` 能打印版本号；执行 `vllm-sr --help` 能列出子命令组（serve / validate / config / status / logs / dashboard 等，详见下一讲 u1-l4）。

> 待本地验证：以上命令涉及真实的 pip 安装与网络下载，耗时与是否成功取决于你的网络与 Python 环境。若 `~/.local/bin` 不在 `PATH` 中，安装器末尾会提示你 `export PATH="$HOME/.local/bin:$PATH"`（见 [install.sh:849-864](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L849-L864)）。

#### 4.1.5 小练习与答案

**练习 1**：默认安装走的是 `dev` 还是 `stable` 渠道？想装稳定版该加什么参数？

**答案**：默认 `dev`（[install.sh:9](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L9) `REQUESTED_CHANNEL=dev`）。装稳定版加 `--channel stable`；或者用 `--pip-spec` 指定任意包规格覆盖渠道。

**练习 2**：为什么 `--mode serve` 默认会自动打开浏览器，而 `--mode cli` 不会？请从源码指出判断点。

**答案**：`should_auto_launch()` 要求 `MODE = serve` 才返回真（[install.sh:106-110](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L106-L110)）；`cli` 模式下 `launch_first_session` 直接 `return`，自然不会打开浏览器。

---

### 4.2 Make 目标体系：sub-makefile 组合架构

#### 4.2.1 概念说明

面向**开发者**的入口是 `make`。但根 `Makefile` 有意做得极薄——它本身几乎不含任何构建规则，只做一件事：把 `tools/make/` 下三十多个「子 makefile」用 `-f` 全部加载进来，组合成一个统一的规则集，再把你要执行的目标委托进去。

这种「sub-makefile 组合」设计的好处是：

- **按职责切分**：golang 相关规则放 `golang.mk`，rust 放 `rust.mk`，docker 放 `docker.mk`，milvus/qdrant/redis/valkey 各自一个文件……单个文件不会膨胀到不可读。
- **顶层统一调度**：你永远只敲 `make <目标>`，不需要关心目标定义在哪个子文件里——根 Makefile 帮你把所有文件拼好。
- **变量集中定义**：公共变量（如 `CONTAINER_RUNTIME`、`LOG_TARGET`）在 `envs.mk` / `common.mk` 集中声明，被所有子文件共享。

> 类比：根 Makefile 像一个「总目录」，`tools/make/*.mk` 像一本本分册；你查任何条目都从总目录进，但实际内容在分册里。

#### 4.2.2 核心流程

当你敲 `make vllm-sr-dev` 时发生的事：

```text
make vllm-sr-dev
   │
   │  根 Makefile 里没有名为 vllm-sr-dev 的规则，
   │  但有一条「通配委托规则」:  $(MAKECMDGOALS): %: _run
   ↓
匹配到 _run 这个「静态模式规则」
   ↓
_run 用 $(MAKE) -f tools/make/common.mk -f tools/make/envs.mk -f ... -f tools/make/docker.mk ...
   （把全部 30+ 子 makefile 用 -f 全部加载）执行 $(MAKECMDGOALS)（即 vllm-sr-dev）
   ↓
在合并后的规则集里，vllm-sr-dev 定义在 docker.mk 中，于是被执行
```

关键点是根 Makefile 的最后两行：`_run` 目标列出了所有 `-f` 子文件，而那条模式规则把**任意用户目标**都重定向进 `_run`。

#### 4.2.3 源码精读

**根 Makefile 全貌**——它真的很短，注释也点明了设计意图：

[Makefile:1-38](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/Makefile#L1-L38) 中，`_run` 目标（[Makefile:4-34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/Makefile#L4-L34)）用一连串 `-f tools/make/xxx.mk` 加载所有子 makefile，最后传入 `$(MAKECMDGOALS)`（即你在命令行写的目标）。而 [Makefile:38](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/Makefile#L38) 那一行 `$(if $(MAKECMDGOALS),$(MAKECMDGOALS): %: _run)` 是精髓：它声明「任何命令行目标都依赖 `_run`」，从而把执行权转交给组合后的子 makefile 体系。

**共享变量**——所有子文件都用得到的全局开关集中在这里：

[tools/make/envs.mk:10](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/envs.mk#L10) 定义 `CONTAINER_RUNTIME ?= docker`，这是「用 docker 还是 podman」的总开关（`?=` 表示「未被外部赋值时才设默认值」，所以你可以 `CONTAINER_RUNTIME=podman make ...` 覆盖）。

[tools/make/common.mk:25](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/common.mk#L25) 定义 `LOG_TARGET`，也就是你在每个 make 目标执行前看到的那行绿色 `==================> Running xxx ============> ...`。

**Go 路由器构建**——开发者最常用的「从源码编译路由器二进制」目标：

[tools/make/build-run-test.mk:15-23](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/build-run-test.mk#L15-L23) 的 `build-router` 先依赖 `rust`（编译 Rust 绑定），再用 `CGO_LDFLAGS="-L$(PWD)/candle-binding/target/release"` 与 `-tags=milvus` 调 `go build`，产出 `bin/router`。注意它依赖 Rust 库——这就是「Go 端构建前必须先编译 Rust」的体现。`build`（[build-run-test.mk:8-9](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/build-run-test.mk#L8-L9)）是更上层的入口，等于 `rust + build-router`。

**单元测试**——本地跑 Go 单测的标准入口：

[tools/make/build-run-test.mk:74-91](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/build-run-test.mk#L74-L91) 的 `test-semantic-router` 默认把 Milvus/Qdrant/Redis/Valkey/Llama Stack 这些需要外部服务的测试跳过（`SKIP_*_TESTS=true`），只跑纯单元逻辑；想跑某类集成测试就把对应变量设为 `false`。

**本地开发镜像构建（本讲的主目标）**：

[tools/make/docker.mk:340-431](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L340-L431) 的 `vllm-sr-dev` 是本地开发「一键就绪」目标：清理旧容器 → 构建 router 镜像（除非 `SKIP_ROUTER_IMAGE=1`）→ 在 `split` 拓扑下确保 Envoy 镜像、构建 dashboard 镜像、构建 sim 镜像 → 以 editable 模式 `pip install` 安装 `vllm-sr` 和 `vllm-sr-sim` CLI。结束时还会提示 `Next steps: cd src/vllm-sr && vllm-sr serve --config config.yaml`。

**从构建到启动的衔接**：

[tools/make/docker.mk:471-481](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L471-L481) 的 `vllm-sr-start` 依赖 `vllm-sr-dev`（即先构建），再带上一组 `VLLM_SR_IMAGE/VLLM_SR_ROUTER_IMAGE/...` 环境变量调用 `vllm-sr serve`，最后 `vllm-sr dashboard`。这就是「构建 + 启动」的一条龙目标。

#### 4.2.4 代码实践

**实践目标**：动手执行本讲指定的实践任务——`make vllm-sr-dev` 完成本地镜像构建，并记录产物。

**操作步骤**：

1. 在仓库根目录执行（需要可用的 Docker）：

   ```bash
   make vllm-sr-dev
   ```

2. 观察输出里的关键阶段提示（对应 [docker.mk:340-431](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L340-L431)）：
   - `Topology: split`
   - `2. Rebuilding vLLM-SR router Docker image...` 与 `Router image built: <镜像名>`
   - `3. Ensuring the official Envoy image is available...`
   - `4. Rebuilding dashboard Docker image...` / `Dashboard image built: ...`
   - `vLLM-SR CLI and vLLM-SR-Sim installed`

3. 构建完成后列出本地产物：

   ```bash
   docker images | grep -E 'vllm-sr|envoy|dashboard'
   ```

**需要观察的现象**：

- 会出现本地构建的 router、dashboard、sim 镜像，以及拉取下来的官方 envoy 镜像；
- 由于 `vllm-sr-dev` 还会 `pip install -e src/vllm-sr`，你会在当前 Python 环境里得到一个可运行的 `vllm-sr` 命令。

**预期结果**：执行 `vllm-sr --version` 能打印版本；docker images 能看到本地镜像。

> 待本地验证：首次冷构建可能耗时数十分钟（多阶段镜像、Rust 编译）。`vllm-sr-dev` 会按 [environments.md:5-9](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/environments.md#L5-L9) 的约定默认重建 router 镜像；只有当本地镜像已含你最新代码时，才用 `make vllm-sr-dev SKIP_ROUTER_IMAGE=1` 复用旧镜像以加速。

#### 4.2.5 小练习与答案

**练习 1**：根 `Makefile` 自己定义了 `vllm-sr-dev` 这个目标吗？如果没有，`make vllm-sr-dev` 为什么能工作？

**答案**：没有。根 Makefile 只有 `_run` 一个真实目标，外加 [Makefile:38](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/Makefile#L38) 的静态模式规则 `$(MAKECMDGOALS): %: _run`。该规则把任意命令行目标转交给 `_run`，`_run` 再用 `-f` 把所有子 makefile（包括定义了 `vllm-sr-dev` 的 `docker.mk`）加载后执行它。

**练习 2**：想用 podman 代替 docker 跑所有容器目标，最少要做什么？

**答案**：在命令前加 `CONTAINER_RUNTIME=podman make ...`，因为 [envs.mk:10](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/envs.mk#L10) 用 `?=` 赋默认值 `docker`，允许被环境覆盖。

**练习 3**：`build-router` 依赖 `rust` 是为什么？

**答案**：Go 路由器通过 CGO 链接 Rust 推理绑定（`candle-binding/target/release` 等产物，见 [build-run-test.mk:15-23](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/build-run-test.mk#L15-L23) 的 `CGO_LDFLAGS`），所以编译 Go 之前必须先 `cargo build` 出 Rust 动态库。

---

### 4.3 本地环境矩阵：cpu / amd / nvidia 与 local image flow

#### 4.3.1 概念说明

vLLM SR 把本地开发抽象成几个「环境（environment）」概念，记录在 [AGENTS.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md) 与 [tools/agent/docs/environments.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/environments.md) 里。每个环境其实是一个**三元组**：

```text
( 构建命令 ,  启动命令 ,  默认镜像 / Dockerfile )
```

三种本地环境分别是：

| 环境 | 构建命令 | 启动命令 | 默认镜像 |
| --- | --- | --- | --- |
| `cpu-local`（默认） | `make vllm-sr-dev` | `vllm-sr serve --image-pull-policy never` | `vllm-sr`（`src/vllm-sr/Dockerfile`） |
| `amd-local`（ROCm） | `make vllm-sr-dev VLLM_SR_PLATFORM=amd` | `vllm-sr serve --image-pull-policy never --platform amd` | `vllm-sr-rocm`（`Dockerfile.rocm`） |
| `nvidia-local`（CUDA） | `VLLM_SR_PLATFORM=nvidia make vllm-sr-build` | `vllm-sr serve --platform nvidia --config <recipe>` | 已发布 `vllm-sr-cuda` 镜像或 `vllm-sr-cuda:local` |

贯穿三者的是同一个变量 `VLLM_SR_PLATFORM`（构建期）与同一个标志 `--platform`（运行期）。

**local image flow（本地镜像流）** 是一条不可违背的工作约定（见 [AGENTS.md:51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L51)：「Use the local image flow for local-dev behavior. Do not invent another serve path.」）。它的含义是：

> 本地开发时，永远**本地构建镜像**，启动时用 `--image-pull-policy never` 强制只用本地镜像——绝不让 `vllm-sr serve` 在本地开发场景偷偷去拉一个已发布的远程镜像。这样能保证你测的就是你刚改的代码，避免「我改了代码但行为没变」的玄学问题。

#### 4.3.2 核心流程

平台选择如何同时影响构建期与运行期：

```text
                         VLLM_SR_PLATFORM=amd|nvidia（或留空=cpu）
                                     │
            ┌────────────────────────┴────────────────────────┐
            ▼（构建期 docker.mk）                              ▼（运行期 vllm-sr serve）
 VLLM_SR_PLATFORM_NORMALIZED := 小写化             --platform amd|nvidia
            │                                              │
 匹配 amd / nvidia 平台块                       选择运行时镜像、是否 --gpus all
            │                                              │
 改写 VLLM_SR_IMAGE / VLLM_SR_DOCKERFILE /         use_cpu 翻转为 false（GPU 模块）
 VLLM_SR_TARGETARCH / VLLM_SR_BUILDPLATFORM
            │
 构建（docker build -f <Dockerfile> -t <镜像>）
```

注意一个微妙差异（这是 `nvidia-local` 与前两者最大的不同）：

- `cpu-local` / `amd-local` 用 **`make vllm-sr-dev`** 构建（还会顺带装 CLI）；
- `nvidia-local` 官方文档用 **`VLLM_SR_PLATFORM=nvidia make vllm-sr-build`**（只构建镜像），且运行期默认直接用**已发布的** `ghcr.io/.../vllm-sr-cuda:latest` 镜像，本地构建标签为 `vllm-sr-cuda:local`。

#### 4.3.3 源码精读

**支持的环境清单（权威定义）**：

[AGENTS.md:42-47](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L42-L47) 一字一句列出了四个环境及其「构建命令 + 启动命令」：cpu-local、amd-local、nvidia-local、ci-k8s。这是本节那张三元组表的原始出处。

**local image flow 不可违背规则**：

[AGENTS.md:51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L51) 明确：「Use the local image flow for local-dev behavior. Do not invent another serve path.」——本地开发不得另造启动路径。

**三个环境的展开说明**：

[tools/agent/docs/environments.md:3-17](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/environments.md#L3-L17) 是 `cpu-local`（默认本地 Docker 流，split 拓扑，`--image-pull-policy never` 启动）；
[environments.md:19-32](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/environments.md#L19-L32) 是 `amd-local`（ROCm/AMD，构建带 `VLLM_SR_PLATFORM=amd`，启动带 `--platform amd`）；
[environments.md:34-43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/environments.md#L34-L43) 是 `nvidia-local`（CUDA，`--platform nvidia` 会选 CUDA 镜像、注入 `--gpus all`、把 `use_cpu` 翻为 false）。
[environments.md:50-55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/environments.md#L50-L55) 的「Selection Rule」给出选择建议：默认 cpu-local，涉及 ROCm 用 amd-local，涉及 CUDA/GPU 模块用 nvidia-local。

**平台分支如何改写镜像与 Dockerfile（构建期核心）**：

[tools/make/docker.mk:233-239](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L233-L239) 先把 `VLLM_SR_PLATFORM` 小写化为 `VLLM_SR_PLATFORM_NORMALIZED`，并定义默认 Dockerfile：cpu 用 `src/vllm-sr/Dockerfile`、amd 用 `Dockerfile.rocm`、nvidia 用 `Dockerfile.cuda`。

[tools/make/docker.mk:270-306](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L270-L306) 是两段平台块：当 `VLLM_SR_PLATFORM_NORMALIZED=amd` 时，把 `VLLM_SR_IMAGE` 换成 ROCm 镜像、Dockerfile 换成 `Dockerfile.rocm`、目标架构锁为 `amd64`；`nvidia` 同理换成 CUDA 镜像与 `Dockerfile.cuda`。注意这里用了 `$(origin ...)` 判断变量来源，只有当变量「未被外部显式赋值」时才套平台默认值——这就是你能用 `VLLM_SR_IMAGE=...` 覆盖镜像的原因。

**三个镜像名的定义**：

[tools/make/docker.mk:217-222](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L217-L222) 定义了 `VLLM_SR_IMAGE`（cpu：`vllm-sr`）、`VLLM_SR_IMAGE_ROCM`（amd：`vllm-sr-rocm`）、`VLLM_SR_IMAGE_CUDA`（nvidia：`vllm-sr-cuda`）三个默认镜像名，都基于 `DOCKER_REGISTRY`/`DOCKER_TAG`。

#### 4.3.4 代码实践

**实践目标**：不要求你真有 GPU，而是学会「按需选择环境」并完成一次 cpu-local 的最小闭环。

**操作步骤（cpu-local，无 GPU 即可）**：

1. 构建（4.2 已做过，这里复用）：

   ```bash
   make vllm-sr-dev
   ```

2. 用 local image flow 启动（关键在 `--image-pull-policy never`）：

   ```bash
   cd src/vllm-sr && vllm-sr serve --image-pull-policy never --config config.yaml
   ```

   或直接用 make 的一条龙目标（[docker.mk:471-481](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L471-L481)）：

   ```bash
   make vllm-sr-start
   ```

**操作步骤（amd-local / nvidia-local，需相应硬件）**：

- AMD：`make vllm-sr-dev VLLM_SR_PLATFORM=amd` 然后 `vllm-sr serve --image-pull-policy never --platform amd`
- NVIDIA：`VLLM_SR_PLATFORM=nvidia make vllm-sr-build` 然后 `vllm-sr serve --platform nvidia --config <recipe>`

**需要观察的现象**：

- cpu-local：`docker ps` 能看到 `vllm-sr-router-container`、`vllm-sr-envoy-container`、`vllm-sr-dashboard-container` 等容器，dashboard 在 `http://localhost:8700`。
- nvidia-local：router 日志里会出现多行 `Using CUDA execution provider (NVIDIA GPU) — verified`，`nvidia-smi` 显存占用上升约 3GB（详见 [tools/agent/docs/nvidia-local.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/nvidia-local.md)）。

**预期结果**：`curl` 向 `http://localhost:8801/v1/chat/completions`（Envoy 入口）发一个 `"model": "auto"` 的请求能返回结果（参考 [build-run-test.mk:113-125](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/build-run-test.mk#L113-L125) 的测试目标 `test-auto-prompt-reasoning`）。

> 待本地验证：以上均需本机具备相应运行时与（GPU 场景）硬件。端口、容器名取决于拓扑与配置；`--platform nvidia` 还要求宿主装好 `nvidia-container-toolkit`。无 GPU 时请只做 cpu-local。

#### 4.3.5 小练习与答案

**练习 1**：为什么 cpu-local / amd-local 的启动命令都带 `--image-pull-policy never`，而 nvidia-local 默认不带？

**答案**：`never` 是 local image flow 的强制要求——本地开发只用刚构建的本地镜像，绝不拉远程镜像。cpu/amd 都强调本地构建 + 本地启动。nvidia-local 比较特殊：它默认直接用**已发布的** `vllm-sr-cuda` 镜像（拉取即可，见 [nvidia-local.md:104-113](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/nvidia-local.md#L104-L113)），因此不强制 `never`；若你改用本地构建的 `vllm-sr-cuda:local`，则应通过 `--image` 显式指定。

**练习 2**：`VLLM_SR_PLATFORM=amd` 这一个变量在构建期改写了哪几样东西？

**答案**：在 [docker.mk:270-287](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L270-L287) 的 amd 平台块里，它把 `VLLM_SR_IMAGE` 换成 ROCm 镜像、`VLLM_SR_DOCKERFILE` 换成 `Dockerfile.rocm`、`VLLM_SR_TARGETARCH` 与 `VLLM_SR_BUILDPLATFORM` 锁为 `amd64`/`linux/amd64`。

**练习 3**：你在本地改了 Go 路由器代码，`make vllm-sr-dev SKIP_ROUTER_IMAGE=1` 然后启动，发现行为没变。最可能的原因是什么？

**答案**：`SKIP_ROUTER_IMAGE=1` 会**复用旧 router 镜像**而不重建（[docker.mk:350-372](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L350-L372)）。你的新代码没被打进镜像，自然行为不变。改了路由器代码就必须去掉 `SKIP_ROUTER_IMAGE=1` 重新构建。

---

## 5. 综合实践

把三个模块串起来，完成一次「从安装到本地跑通」的完整闭环：

1. **装 CLI**：用 4.1 的方式预演一次安装——先 `bash install.sh --help` 读参数，再 `bash install.sh --mode cli --no-launch` 只装 CLI。确认 `vllm-sr --version` 可用，并指出它最终 `exec` 的是哪个 venv 里的可执行文件（提示：[install.sh:613-626](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/install.sh#L613-L626)）。

2. **构建镜像**：用 4.2 的 `make vllm-sr-dev` 构建本地镜像，用 `docker images` 记录出现的镜像名（router / envoy / dashboard / sim），并对照 [docker.mk:340-431](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/docker.mk#L340-L431) 说明每一步产物对应目标里的哪个阶段。

3. **本地启动**：用 4.3 的 cpu-local 方式启动（`vllm-sr serve --image-pull-policy never` 或 `make vllm-sr-start`），打开 `http://localhost:8700` 确认 dashboard 可达。

4. **验证路由**：参考 [build-run-test.mk:113-125](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/make/build-run-test.mk#L113-L125) 的 `test-auto-prompt-*` 目标，向 Envoy 入口 `http://localhost:8801/v1/chat/completions` 发一条 `"model": "auto"` 的请求，确认能拿到响应。

5. **画出三元组**：最后，把 cpu/amd/nvidia 三个环境的「构建命令 + 启动命令 + 默认镜像」整理成一张表，并标注「哪两个环境强调 `--image-pull-policy never`、哪一个默认用已发布镜像」。

> 若本机无 Docker 或无 GPU，步骤 2–4 标注为「待本地验证」，但步骤 1 的 `--help` 与步骤 5 的表格整理可无条件完成。

## 6. 本讲小结

- vLLM SR 有**两条**安装/运行路径：`install.sh`（终端用户、装 Python CLI、可自动 serve）与 `make`（开发者、编译 Go 路由器、构建镜像、跑测试）。
- `install.sh` 的主流程是 `parse_args → validate_args → detect_os → print_install_plan → install_cli → ensure_runtime → launch_first_session → print_next_steps`，默认装 dev 渠道、默认 serve 模式、默认自动首次启动并打开 dashboard（:8700）。
- 根 `Makefile` 是个**薄分发器**：靠一条静态模式规则 `$(MAKECMDGOALS): %: _run` 把任意目标委托给「用 `-f` 加载全部 `tools/make/*.mk` 后的组合规则集」。
- `make vllm-sr-dev` 是本地开发一键就绪目标：构建 router/envoy/dashboard/sim 镜像 + editable 安装 CLI；`vllm-sr-start` 在其基础上直接 `vllm-sr serve`。
- 本地环境分 `cpu-local`（默认）/ `amd-local`（ROCm）/ `nvidia-local`（CUDA）三种，统一由 `VLLM_SR_PLATFORM`（构建期）与 `--platform`（运行期）驱动；`docker.mk` 的平台块据此改写镜像名、Dockerfile 与目标架构。
- **local image flow** 是不可违背的约定：本地开发一律本地构建、`--image-pull-policy never` 启动，保证「测的就是你改的」。

## 7. 下一步学习建议

- **下一讲 u1-l4「vllm-sr CLI 命令体系」**：本讲你已经把 `vllm-sr` 装好并 `serve` 起来，下一讲将系统讲解 `serve / validate / config / status / logs / dashboard` 等子命令的组织方式，让你真正会用这个 CLI。
- **u3「配置体系与 Recipe」**：本讲多次出现 `--config <recipe>` 与 `config.yaml`，如果你想知道 recipe 到底是什么、怎么写，就去 u3。
- **延伸阅读（源码）**：想深入 GPU 路径，读 [tools/agent/docs/nvidia-local.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/nvidia-local.md) 与 [tools/agent/docs/environments.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/environments.md)；想看官方安装文档，读 [website/docs/installation/](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/installation) 下的安装指南。
