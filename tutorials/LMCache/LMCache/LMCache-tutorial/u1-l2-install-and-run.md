# 安装、构建与运行方式

## 1. 本讲目标

上一篇我们建立了对 LMCache 的整体认知：它是一个横跨推理引擎与存储之间的「KV cache 管理层」。本篇解决一个非常现实的问题——**怎么把它装到机器上、怎么构建它的 C++/CUDA 扩展、装完之后有哪些命令可以用**。

学完本讲你应该能够：

1. 读懂 `pyproject.toml`，说清楚包名、Python 版本要求、构建依赖与三个命令行入口脚本分别指向哪个 Python 函数。
2. 区分 `lmcache`、`lmcache_server`、`lmcache_controller` 三个命令各自的用途与对应的 `main()` 函数路径。
3. 理解 `setup.py` + `setup_extensions/` 的「策略模式」构建系统，知道 `NO_NATIVE_EXT`、`NO_GPU_EXT`、`BUILD_WITH_HIP` 等环境变量开关的含义。
4. 在有 GPU 和无 GPU 的主机上分别选择正确的安装命令。

本讲全部基于真实源码，所有命令均来自 `pyproject.toml`、`setup.py`、`setup_extensions/` 与 `AGENTS.md`。

## 2. 前置知识

- **Python 包与 pip**：LMCache 是一个标准的 Python 包，通过 `pip install` 安装。如果你用过 `pip install numpy`，这里概念一致，只是它还附带需要编译的本地扩展。
- **本地扩展（native extension）**：一部分性能关键的代码用 C++/CUDA 写在 `csrc/` 目录里（例如显存拷贝内核、KV 压缩编码器），安装时需要用编译器把它们编译成 `.so` 动态库，再以 `lmcache.c_ops` 这个名字暴露给 Python 调用。这是它与纯 Python 包最大的区别。
- **构建隔离（build isolation）**：`pip install` 默认会为「构建过程」单独建一个临时环境（叫 build isolation），里面只装声明好的构建依赖。LMCache 推荐用 `--no-build-isolation` 关掉它，原因后面会讲。
- **TTFT / KV cache**：已在上一篇讲义（u1-l1）讲过，本篇不再重复。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [pyproject.toml](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pyproject.toml) | 包的「身份证 + 配置中心」：包名、Python 版本、构建依赖、三个命令行入口、版本号来源、lint/类型检查配置。 |
| [setup.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup.py) | 老式构建脚本入口。它本身不写死任何平台逻辑，而是把「编译哪些扩展」交给 `setup_extensions/` 决定。 |
| [setup_extensions/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions) | 「策略模式」构建框架：`BuildPolicy` 负责探测硬件、选择构建配置；`build_profiles/` 下每个文件代表一个硬件平台（CUDA / ROCm / SYCL / MUSA）。 |
| [requirements/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/requirements) | 运行依赖按用途分文件管理：`common.txt`（通用）、`cuda.txt`（GPU）、`build.txt`（构建期）、`nixl.txt`（可选传输层）等。 |
| [AGENTS.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/AGENTS.md) | 给 AI agent 和人类开发者的工作指南，其中「Build & Install」一节是官方推荐的安装命令速查表。 |
| [lmcache/cli/main.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/main.py) | `lmcache` 命令的 Python 入口函数 `main()`。 |
| [lmcache/v1/server/__main__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py) | `lmcache_server` 命令的入口，启动一个独立的 KV 存储 TCP 服务。 |
| [lmcache/v1/api_server/__main__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/api_server/__main__.py) | `lmcache_controller` 命令的入口，启动一个 FastAPI/uvicorn 编排服务。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：包结构与会装依赖、三个命令行入口、源码构建系统、安装开关与命令速查。

### 4.1 Python 包结构与依赖管理

#### 4.1.1 概念说明

一个现代 Python 包的「元信息」通常集中在 `pyproject.toml` 里：包叫什么名字、支持哪些 Python 版本、依赖哪些第三方库、安装后会注册哪些命令行工具。LMCache 的 `pyproject.toml` 还做了两件值得注意的设计决策：

1. **运行期依赖里故意不锁死 `torch` 版本**。因为 LMCache 要嵌入到 vLLM、SGLang 等推理引擎里，而这些引擎自带特定版本的 torch；如果 LMCache 强行装一个固定版本，会和引擎冲突。所以它让用户/引擎自己决定 torch 版本。
2. **构建期依赖里反而锁死 `torch==2.11.0`**。这是给「打包发 wheel」用的隔离构建环境，保证发布产物可复现。

#### 4.1.2 核心流程

包元信息的加载流程是：

```text
pip 读取 pyproject.toml
  ├── [project] 段 → 包名 lmcache、Python 版本要求、描述
  ├── dynamic = ["dependencies", "optional-dependencies", "version"]
  │     └── 说明：依赖、可选依赖、版本号都不写死在 toml 里，而是「动态生成」
  ├── 调用 setup.py
  │     ├── setup.py 读 requirements/*.txt → 生成 install_requires
  │     └── setuptools_scm 从 git tag → 生成版本号写入 lmcache/_version.py
  └── [project.scripts] 段 → 注册 3 个命令行工具
```

关键是 `dynamic` 字段：它告诉打包工具「这三项别从 toml 里读，去问 `setup.py`」。于是依赖列表实际来自 `requirements/common.txt` 等文件，版本号来自 git tag。

#### 4.1.3 源码精读

包的基本身份与版本策略在 `[project]` 段：

[pyproject.toml:L20-L43](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pyproject.toml#L20-L43) —— 包名 `lmcache`，要求 Python `>=3.10,<3.14`，并把 `dependencies`、`optional-dependencies`、`version` 标记为 `dynamic`（动态生成，不在 toml 写死）。

构建期依赖（仅用于编译扩展，不影响运行）在 `[build-system]` 段：

[pyproject.toml:L10-L17](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pyproject.toml#L10-L17) —— 构建期需要 `ninja`、`setuptools`、`setuptools_scm`，并锁死 `torch==2.11.0`。注意 toml 顶部注释说明：这套锁死的构建依赖**只用于 cibuildwheel 发布 wheel**；本地推荐安装走 `--no-build-isolation`，绕开它以便灵活选择 torch 版本。

运行期依赖「不锁 torch」的设计意图，作者在 `common.txt` 里写了一大段注释解释：

[requirements/common.txt:L32-L43](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/requirements/common.txt#L32-L43) —— 四点理由的核心：不锁版本，这样 `pip install lmcache` 不会覆盖用户已有的 torch，也避免和 vLLM 等引擎自带的 torch 冲突；若用户没装 torch，再装最新版。

版本号通过 `setuptools_scm` 从 git tag 推导，写入 `_version.py`：

[pyproject.toml:L55-L61](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pyproject.toml#L55-L61) —— `version_file = "lmcache/_version.py"`，并且 `tag_regex` 只认 `vX.Y.Z` 形式的 tag，nightly tag 不算版本锚点。

> 顺带一提：`requirements/build.txt` 是 `pyproject.toml` 构建依赖的「镜像」，方便在不走 pip 隔离构建时手动装：[requirements/build.txt:L1-L11](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/requirements/build.txt#L1-L11)（注意它里面特意不含 torch，注释说明让用户自己选）。

#### 4.1.4 代码实践

**目标**：亲手确认「运行依赖不锁 torch、构建依赖锁 torch」这件事。

**步骤**：

1. 打开 `pyproject.toml`，在 `[build-system].requires` 里找到 `torch==2.11.0`。
2. 打开 `requirements/common.txt`，找到最后一行 `torch`（无版本号）。
3. 在仓库根目录运行下面这条命令，把 common.txt 里所有「带版本约束」的依赖列出来（注意：这不是项目命令，只是用 grep 帮你观察，**待本地验证**具体输出）：

   ```bash
   grep -vE '^\s*#|^\s*-' requirements/common.txt | grep '>=' 
   ```

**观察现象**：你会看到 `huggingface_hub>=1.5.0`、`pyzmq >= 25.0.0` 等带版本下限的依赖，但**看不到 torch 的版本约束**。

**预期结果**：`torch` 这一行没有任何 `>=` / `==`，印证了「运行期不锁 torch」的设计。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pyproject.toml` 的 `[build-system].requires` 锁死 `torch==2.11.0`，而 `requirements/common.txt` 里 torch 不锁版本？

**参考答案**：构建期锁版本是为了让 cibuildwheel 发布的 wheel 可复现（产物一致）；运行期不锁是因为 LMCache 要寄生在 vLLM/SGLang 等引擎里，强行装固定 torch 会和引擎冲突，所以把 torch 版本决定权交给用户或引擎。

**练习 2**：LMCache 支持 Python 3.9 吗？支持 3.14 吗？

**参考答案**：都不支持。`requires-python = ">=3.10,<3.14"`，即只支持 3.10、3.11、3.12、3.13（classifiers 也只列了这四个）。

### 4.2 三个命令行入口脚本

#### 4.2.1 概念说明

`pyproject.toml` 的 `[project.scripts]` 段是一个「命令 → Python 函数」的映射表。pip 安装时，会为每一条在 `bin/`（或 Windows 的 `Scripts/`）下生成一个可执行脚本，运行该命令就等于调用对应的 Python 函数。LMCache 注册了三个命令，对应三种完全不同的运行形态：

| 命令 | 入口函数 | 是什么 |
| --- | --- | --- |
| `lmcache` | `lmcache.cli.main:main` | 运维/诊断 CLI（带子命令，例如 ping）。 |
| `lmcache_server` | `lmcache.v1.server.__main__:main` | 一个独立的 KV 存储 TCP 服务（原始二进制协议）。 |
| `lmcache_controller` | `lmcache.v1.api_server.__main__:main` | 一个 FastAPI/uvicorn 编排服务（HTTP API）。 |

> ⚠️ 一个容易被误解的点：`lmcache_controller` 这个命令指向的是 `lmcache.v1.api_server.__main__:main`，而**不是** `mp_coordinator`。项目里另有一个 `lmcache/v1/mp_coordinator/` 模块（多进程协调器），它有自己的职责，会在后续 u3 单元专门讲；本讲只忠实记录 `pyproject.toml` 实际声明的映射。

#### 4.2.2 核心流程

```text
pip install 时
  └── 读 [project.scripts]，为每条生成一个 wrapper 脚本

用户在终端敲 lmcache_server 0.0.0.0 9999
  └── wrapper 调用 lmcache.v1.server.__main__.main()
        └── 解析 sys.argv → LMCacheServer(host, port, device).run()
```

三个入口的「输入」差异很大：CLI 用 `argparse`，server 直接读 `sys.argv` 的位置参数，controller 用 `argparse` 读 `--config/--host/--port`。

#### 4.2.3 源码精读

入口映射表本身只有三行：

[pyproject.toml:L45-L48](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pyproject.toml#L45-L48) —— 三个命令行工具的注册：`lmcache`、`lmcache_server`、`lmcache_controller`，分别指向三个 `main` 函数。

**入口 1：`lmcache` CLI。** 它用 argparse，把所有子命令注册进去再分发：

[lmcache/cli/main.py:L20-L44](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/main.py#L20-L44) —— `main()` 先打印一次 banner，构建 argparse，遍历 `ALL_COMMANDS` 让每个子命令注册自己，解析参数后调用 `args.func(args)`。

子命令列表 `ALL_COMMANDS` 是**自动发现**的，不是手写清单：

[lmcache/cli/commands/__init__.py:L15-L38](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/__init__.py#L15-L38) —— `_discover_commands()` 扫描 `commands/` 包下的所有子模块，收集 `BaseCommand` 的具体子类。这意味着新增一个子命令文件就会被自动注册（这个机制会在 u4-l1 详讲）。

**入口 2：`lmcache_server`。** 它是原始的 socket TCP 服务，参数直接来自命令行位置：

[lmcache/v1/server/__main__.py:L150-L166](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L150-L166) —— `main()` 要求 `sys.argv` 长度为 3 或 4：`<host> <port> <storage>(默认 cpu)`，然后构造 `LMCacheServer` 并 `run()`。

它的服务循环每来一个客户端就开一个线程，按 `PUT/GET/EXIST/HEALTH` 命令处理 KV：[lmcache/v1/server/__main__.py:L24-L32](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L24-L32)（`__init__` 里绑定 socket 并 `listen()`）。

**入口 3：`lmcache_controller`。** 它是 FastAPI + uvicorn，参数走 argparse 的命名参数：

[lmcache/v1/api_server/__main__.py:L418-L533](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/api_server/__main__.py#L418-L533) —— `main()` 解析 `--config/--host/--port/--monitor-ports/--health-check-interval` 等参数，加载控制器配置，构造 FastAPI app，最后 `uvicorn.run(app, host=..., port=...)`（见 L528）。

它对外暴露 `/lookup`、`/clear`、`/pin`、`/health`、`/move` 等 HTTP 端点：[lmcache/v1/api_server/__main__.py:L85-L89](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/api_server/__main__.py#L85-L89)（`create_app(...)` 构建带这些端点的应用）。

#### 4.2.4 代码实践

**目标**：在不安装项目的情况下，也能用 `python -m` 直接验证三个入口函数存在。

**步骤**：在仓库根目录依次执行（这些都是**待本地验证**的命令）：

```bash
# 1. 列出 lmcache CLI 的帮助（需要先 pip install -e .，见 4.4）
lmcache --help

# 2. 不安装，直接用模块方式查看 server 入口的用法说明
python -c "import sys; sys.argv=['lmcache_server']; from lmcache.v1.server.__main__ import main; main()"
# 预期：打印 "Usage: ... <host> <port> <storage>(default:cpu)" 后 exit(1)

# 3. 查看 controller 的参数
python -m lmcache.v1.api_server --help
```

**观察现象**：

- 第 2 条会因参数不足而打印 usage 并以退出码 1 退出，这正好对应 `__main__.py` L154-L156 的 `if len(sys.argv) not in [3, 4]: ... exit(1)`。
- 第 3 条会列出 `--config/--host/--port` 等参数。

**预期结果**：你能确认三个入口函数都能被定位、且各自接受的参数形态不同（位置参数 vs 命名参数 vs argparse 子命令）。

#### 4.2.5 小练习与答案

**练习 1**：用一句话区分 `lmcache_server` 和 `lmcache_controller`。

**参考答案**：`lmcache_server` 是一个原始 socket、按位置参数 `host port storage` 启动的 KV 存储 TCP 服务；`lmcache_controller` 是一个 FastAPI/uvicorn、按 `--host/--port` 启动的 HTTP 编排服务。

**练习 2**：`lmcache --help` 列出的子命令是写死在代码里的吗？

**参考答案**：不是。它们来自 `cli/commands/__init__.py` 的 `_discover_commands()`，通过扫描该包下所有 `BaseCommand` 子类自动发现。新增一个命令文件即可自动出现。

### 4.3 从源码构建 C++/CUDA 扩展

#### 4.3.1 概念说明

LMCache 的性能关键路径（显存拷贝、KV 压缩编解码、位置编码内核）写在 `csrc/` 下的 `.cu`/`.cpp` 文件里，安装时必须把它们编译成 Python 可调用的扩展模块（CUDA 平台下叫 `lmcache.c_ops`）。

要支持多种硬件（NVIDIA CUDA、AMD ROCm、Intel SYCL、摩尔线程 MUSA），如果用 `if/else` 写在一个 `setup.py` 里会非常臃肿。LMCache 用了**策略模式（strategy pattern）**：每个平台一个「构建配置（BuildProfile）」，一个「编排器（BuildPolicy）」负责探测硬件、选出合适的配置、驱动编译。`setup.py` 本身几乎不含平台逻辑。

#### 4.3.2 核心流程

```text
pip install -e . 触发 setup.py
  │
  ├── policy = BuildPolicy()                 # 自动发现所有平台 profile
  ├── profile = policy.resolve_profile()     # 三段式选择：
  │       1. 若设了 BUILD_WITH_* 环境变量 → 用它（不回退）
  │       2. 否则逐个 profile.detect() 自动探测 → 第一个命中的
  │       3. 都没命中 → 警告并跳过扩展
  └── policy.collect_extensions(profile)
          ├── build_common_cpp()              # 公共 C++ 扩展（Redis/FS/存储管理）
          ├── profile.build()                 # 平台特有扩展（如 c_ops）
          └── 可选 L2 存储后端扩展（自动发现）
```

三个「构建开关」环境变量控制是否编译、编译哪部分：

| 环境变量 | 作用 |
| --- | --- |
| `NO_NATIVE_EXT=1` | 完全跳过所有本地扩展（纯 Python 源码安装）。 |
| `NO_GPU_EXT=1` | 只跳过 GPU 扩展，公共 C++ 扩展仍编译（即 CPU-only slim 安装）。 |
| `BUILD_WITH_CUDA=1` / `BUILD_WITH_HIP=1` 等 | 显式指定一个平台 profile，不做自动探测。 |

#### 4.3.3 源码精读

`setup.py` 极其精简，只做「编排 + 读依赖」：

[setup.py:L37-L62](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup.py#L37-L62) —— 顶部策略说明「policy-driven，每个平台一个文件，新增平台零改动本文件」。`__main__` 里依次 `resolve_profile()` → `collect_extensions(profile)`，依赖从 `requirements/common.txt` 读取，包用 `find_packages(exclude=("csrc",))`。

构建框架的总入口和用法说明：

[setup_extensions/__init__.py:L1-L10](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions/__init__.py#L1-L10) —— docstring 直接给出三行标准用法，正是 `setup.py` 里写的样子。

`BuildPolicy` 的三段式选择逻辑：

[setup_extensions/policy.py:L127-L137](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions/policy.py#L127-L137) —— 类文档说明了选择顺序：显式 `BUILD_WITH_*` 优先（不回退）、否则自动探测、否则跳过扩展。

[setup_extensions/policy.py:L156-L192](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions/policy.py#L156-L192) —— Phase 1 收集所有 `is_explicitly_requested()` 的 profile（多于一个就报错）；Phase 2 逐个 `detect()` 自动探测；Phase 3 都没找到则警告并返回 `None`。

`collect_extensions` 决定到底编译哪些扩展：

[setup_extensions/policy.py:L194-L229](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions/policy.py#L194-L229) —— 先 `build_common_cpp(profile)` 编公共 C++ 扩展；若 profile 存在且 GPU 扩展未禁用，再 `profile.build()`；最后再追加可选 L2 存储后端扩展。

构建开关的精确定义在 `BuildProfile` 基类里：

[setup_extensions/build_profiles/__init__.py:L56-L84](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions/build_profiles/__init__.py#L56-L84) —— `is_native_ext_disabled()`：`NO_NATIVE_EXT=1` 为真；`NO_CUDA_EXT` 是遗留别名（已弃用，等价于 `NO_NATIVE_EXT`），都控制**全部**本地扩展。

[setup_extensions/build_profiles/__init__.py:L86-L99](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions/build_profiles/__init__.py#L86-L99) —— `is_gpu_ext_disabled()`：`NO_GPU_EXT=1` 为真，只跳过 GPU 扩展，公共 C++ 扩展仍编译。

以 CUDA profile 为例，看一个平台具体怎么编译：

[setup_extensions/build_profiles/cuda.py:L23-L37](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions/build_profiles/cuda.py#L23-L37) —— `name="cuda"`、`env_var="BUILD_WITH_CUDA"`；`detect()` 通过 `shutil.which("nvcc")` 判断，**故意不用** `torch.cuda.is_available()`，因为无头 CI 构建机往往没有运行时驱动但有完整工具链。

[setup_extensions/build_profiles/cuda.py:L39-L74](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions/build_profiles/cuda.py#L39-L74) —— `build()` 把 `csrc/` 下的一组 `.cu/.cpp` 源文件（`mem_kernels.cu`、`ac_enc.cu`、`pos_kernels.cu` 等）编译成一个名为 `lmcache.c_ops` 的 `CUDAExtension`，并设置 CXX11 ABI 标志。

[setup_extensions/build_profiles/cuda.py:L100-L114](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/setup_extensions/build_profiles/cuda.py#L100-L114) —— `_cuda_major()` 从 `LMCACHE_CUDA_MAJOR` 环境变量决定编译目标是 CUDA 12 还是 13（默认 13），并据此选择 `cuda12_core.txt` 或 `cuda13_core.txt` 依赖。

#### 4.3.4 代码实践

**目标**：在不真正编译的情况下，跟踪「敲下安装命令后，编译了哪些源文件」。

**步骤**：

1. 打开 `setup_extensions/build_profiles/cuda.py` 的 `build()` 方法（L50-L62），把 `cuda_sources` 列表里的每个文件名抄下来。
2. 在仓库根目录确认这些源文件确实存在（**待本地验证**）：

   ```bash
   ls csrc/mem_kernels.cu csrc/ac_enc.cu csrc/ac_dec.cu csrc/pos_kernels.cu csrc/pybind.cpp
   ```

3. 阅读 `csrc/pybind.cpp`，找到把 C++ 函数绑定到 Python 模块 `c_ops` 的 `PYBIND11_MODULE` 宏，确认扩展模块名确实是 `lmcache.c_ops`。

**观察现象**：`cuda_sources` 列表里的每个文件都能在 `csrc/` 下找到；`pybind.cpp` 是 C++ 与 Python 的「粘合层」。

**预期结果**：你能画出 `csrc/*.cu → nvcc 编译 → lmcache.c_ops 扩展 → Python 调用` 的链路。

#### 4.3.5 小练习与答案

**练习 1**：在一台装了 CUDA 工具链但没装 GPU 驱动的 CI 机器上，`CudaProfile.detect()` 会返回 True 还是 False？为什么？

**参考答案**：返回 True。因为它用 `shutil.which("nvcc") is not None` 判断，只看编译器 `nvcc` 是否在 PATH 里，不探测运行时驱动。源码注释明确说这是为了适配「有工具链但无驱动」的无头构建机。

**练习 2**：`NO_GPU_EXT=1` 和 `NO_NATIVE_EXT=1` 的区别是什么？

**参考答案**：`NO_NATIVE_EXT=1` 跳过**所有**本地扩展（公共 C++ 和 GPU 都不编译），得到纯 Python 安装；`NO_GPU_EXT=1` 只跳过 GPU 扩展，公共 C++ 扩展（如 Redis/FS/存储管理相关）仍然编译，这是常见的 CPU-only slim 安装方式。

### 4.4 安装方式与构建开关速查

#### 4.4.1 概念说明

把前面的依赖、入口、构建系统串起来，就得到了「在不同机器上怎么装」的速查表。LMCache 官方在 `AGENTS.md` 的「Build & Install」一节给出了四条命令，分别对应：标准 GPU 安装、纯源码安装、CPU-only 安装、AMD ROCm 安装。

#### 4.4.2 核心流程

选择安装命令的决策树：

```text
你的机器有 NVIDIA GPU 且装了 torch？
  └── 是 → pip install -e . --no-build-isolation           （标准）
你的机器没有 GPU / 只想读代码？
  └── NO_GPU_EXT=1 pip install -e . --no-build-isolation    （CPU-only slim）
你完全不想编译任何 C++？
  └── NO_NATIVE_EXT=1 pip install -e .                      （纯源码）
你用的是 AMD GPU (ROCm)？
  └── BUILD_WITH_HIP=1 pip install -e .                     （HIP）
只想快速试用，不想从源码装？
  └── pip install lmcache                                   （从 PyPI 装 wheel）
```

`--no-build-isolation` 的意义：跳过 pip 的隔离构建环境，直接用当前环境里**已经装好的 torch**来编译扩展。这样 torch 版本由你（或你的推理引擎）决定，而不是被 `[build-system].requires` 里的 `torch==2.11.0` 覆盖。

#### 4.4.3 源码精读

官方推荐用 `uv` 管理环境（这是 `AGENTS.md` 给 agent 的标准流程）：

[AGENTS.md:L17-L25](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/AGENTS.md#L17-L25) —— 先 `uv venv --python 3.12`，再 `uv pip install torch`（编译 CUDA 扩展的前置），最后 `uv pip install -e . --no-build-isolation`。

四条构建命令的权威清单：

[AGENTS.md:L29-L41](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/AGENTS.md#L29-L41) —— 标准（`pip install -e . --no-build-isolation`）、纯源码（`NO_NATIVE_EXT=1 pip install -e .`）、CPU-only（`NO_GPU_EXT=1 pip install -e . --no-build-isolation`）、HIP/ROCm（`BUILD_WITH_HIP=1 pip install -e .`）。

对普通用户，README 给了最简入口：

README（[README.md:L86-L92](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/README.md#L86-L92)）—— `pip install lmcache` 直接从 PyPI 装，并指向官方安装文档 `docs.lmcache.ai/getting_started/installation.html`。

发 wheel 时的硬件目标范围（了解即可）：

[pyproject.toml:L157-L173](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pyproject.toml#L157-L173) —— cibuildwheel 配置：构建 `cp3*-manylinux_x86_64` wheel，`TORCH_CUDA_ARCH_LIST` 覆盖 7.5 到 12.0 的计算能力（T4/A100/L4/H100/B200 等），基于 CUDA 13.0 的 manylinux 镜像。

#### 4.4.4 代码实践

**目标**：在一个干净环境里完成一次 slim 安装并验证命令可用。

**步骤**（**待本地验证**；无 GPU 主机请用 slim 方式）：

```bash
# 1. 建虚拟环境
uv venv --python 3.12 && source .venv/bin/activate

# 2-A. 有 GPU 主机：先装 torch，再标准安装
uv pip install torch
uv pip install -e . --no-build-isolation

# 2-B. 无 GPU 主机：CPU-only slim 安装（跳过 GPU 扩展）
NO_GPU_EXT=1 uv pip install -e . --no-build-isolation

# 3. 验证三个命令都已注册
lmcache --help
which lmcache lmcache_server lmcache_controller
```

**观察现象**：

- 步骤 2-A 会触发 `Building CUDA extensions` 日志（来自 `cuda.py` 的 `print("Building CUDA extensions")`）；步骤 2-B 不会。
- 步骤 3 的 `lmcache --help` 会打印 banner，并列出所有自动发现的子命令。

**预期结果**：`which` 能定位到三个命令脚本；`lmcache --help` 正常输出子命令列表。若 `lmcache --help` 报找不到命令，说明 `pip install -e .` 未成功或虚拟环境未激活。

#### 4.4.5 小练习与答案

**练习 1**：为什么 LMCache 推荐用 `--no-build-isolation` 安装？

**参考答案**：为了用当前环境里已存在的 torch 来编译扩展，避免 pip 隔离构建环境按 `[build-system].requires` 装一个 `torch==2.11.0` 而覆盖/冲突用户或推理引擎自带的 torch 版本。

**练习 2**：你在一台只有 CPU 的笔记本上想跑通 `lmcache --help`，该用哪条命令？为什么不用标准安装？

**参考答案**：用 `NO_GPU_EXT=1 pip install -e . --no-build-isolation`（或更彻底的 `NO_NATIVE_EXT=1 pip install -e .`）。因为标准安装会尝试编译 CUDA 扩展，在没有 `nvcc`/GPU 的机器上会失败；slim 安装跳过 GPU 扩展，仍能注册 CLI 命令。

## 5. 综合实践

把本讲四个模块串起来，完成一次「从安装到验证入口」的完整链路。

**任务**：在一台主机上安装 LMCache，确认三个命令行入口，并填写下面的「入口对照表」。

**步骤**：

1. 根据你的机器类型，从 4.4 选择正确的安装命令完成安装（有 GPU 走标准，无 GPU 走 slim）。
2. 运行 `lmcache --help`，把列出的子命令记下来。
3. 用 `python -c "import lmcache; print(lmcache.__file__)"` 确认包确实安装到了你的 venv（**待本地验证**）。
4. 填写下面这张表（答案已在 4.2 给出，这里要求你**用源码验证**而非背诵）：

   | 命令 | 入口模块:函数 | 启动形态 | 关键参数来源 |
   | --- | --- | --- | --- |
   | `lmcache` | `lmcache.cli.main:main` | argparse 子命令 CLI | `ALL_COMMANDS` 自动发现 |
   | `lmcache_server` | ？ | ？ | ？ |
   | `lmcache_controller` | ？ | ？ | ？ |

5. **进阶**：用 `python -m lmcache.v1.server` 不带参数运行，观察它打印的 usage 行，回到源码 `__main__.py` L154-L156 找到产生这行输出的代码，确认「参数个数不对就 `exit(1)`」的行为。

**预期结果**：你能不查讲义，只看 `pyproject.toml` 与三个 `__main__.py`，准确说出每个命令的入口函数、运行形态与参数来源。

## 6. 本讲小结

- `pyproject.toml` 是包的元信息中心：`[project.scripts]` 注册了 `lmcache`、`lmcache_server`、`lmcache_controller` 三个命令；运行依赖**故意不锁 torch 版本**，而构建期为发 wheel 锁死 `torch==2.11.0`。
- 三个命令分别对应三种进程：CLI 诊断工具、原始 socket KV 存储服务、FastAPI/uvicorn 编排服务。注意 `lmcache_controller` 实际指向 `v1/api_server/__main__:main`。
- `setup.py` 用策略模式把「编译哪些扩展」委托给 `setup_extensions/`：`BuildPolicy` 三段式选择（显式 `BUILD_WITH_*` → 自动 `detect()` → 跳过），`BuildProfile` 子类代表每个硬件平台。
- CUDA profile 通过 `nvcc` 是否存在来探测，把 `csrc/*.cu` 编译成 `lmcache.c_ops` 扩展；`LMCACHE_CUDA_MAJOR` 决定 CUDA 12 还是 13。
- 三个核心构建开关：`NO_NATIVE_EXT=1`（纯源码）、`NO_GPU_EXT=1`（CPU-only slim）、`BUILD_WITH_HIP=1`（ROCm）；推荐用 `--no-build-isolation` 以保留对 torch 版本的控制。

## 7. 下一步学习建议

- **下一篇 u1-l3（代码目录结构与组织）**：本讲只碰到了 `lmcache/cli/`、`lmcache/v1/server/`、`lmcache/v1/api_server/` 几个入口目录。下一篇会系统梳理整个 `lmcache/` 模块树，区分新的 `v1/` 架构、legacy `storage_backend/`、`integration/` 集成层与 `sdk/`、`cli/` 的职责边界。
- **u1-l4（进程入口与启动方式）**：如果你对三个入口的启动细节意犹未尽，那一篇会深入每种进程的「输入 → 启动」对照，并介绍 `mp_coordinator` 这个本讲特意留到后面讲的组件。
- **继续阅读源码建议**：装好之后，可以直接读 [lmcache/cli/commands/base.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/base.py)，理解子命令的 `BaseCommand` 契约，为将来自己加一个 `lmcache` 子命令做准备（这在 u4-l1 会详讲）。
