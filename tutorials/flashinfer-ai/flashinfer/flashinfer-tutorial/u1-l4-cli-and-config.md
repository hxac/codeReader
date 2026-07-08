# CLI 工具与配置查看

## 1. 本讲目标

学完本讲后，你应该能够：

1. 知道 `flashinfer` 命令行工具是从哪里「冒出来」的——即 pip 安装时注册的入口点（entry point）。
2. 列出 `flashinfer` CLI 的常用子命令（`show-config` / `list-modules` / `module-status` / `clear-cache` / `download-cubin` / `export-compile-commands` 等），并说出每个的作用。
3. 理解「JIT 模块注册」这件事到底发生在哪一行代码：为什么 `list-modules` 有时一开始是空的，需要先「兜底注册」才能看到模块。
4. 区分两类需要管理的产物：JIT 编译出的 `.so`（受 `clear-cache` 管理）和预编译下载的 cubin（受 `clear-cubin` / `download-cubin` 管理）。
5. 用 `export-compile-commands` 生成 `compile_commands.json`，理解它对「在 IDE 里跳转到 kernel 源码」的意义。

本讲只读 `flashinfer/__main__.py`（CLI 实现）和 `flashinfer/__init__.py`（包入口），并配合 `flashinfer/jit/core.py`、`flashinfer/aot.py`、`flashinfer/jit/env.py` 中支撑 CLI 的少量函数。**本讲不触发任何 kernel 编译**，是「环境诊断 + 工具认知」课，为后续 JIT 编译系统（第 2 单元）铺路。

## 2. 前置知识

本讲假设你已经完成 u1-l2（安装与首次运行）和 u1-l3（目录结构与代码分层）。在继续前，先回顾三个关键概念：

- **CLI（Command-Line Interface，命令行界面）**：在终端里敲的 `flashinfer xxx` 这种命令。它本质上是一个 Python 函数，被「注册」成系统命令后，pip 安装时会在你的环境里生成一个可执行入口。
- **入口点（entry point）**：Python 打包规范（PEP 517/518）里的机制。你在 `pyproject.toml` 里声明「这个包要提供一个叫 `flashinfer` 的命令，它指向 `flashinfer.__main__:cli` 这个函数」，pip 安装时就会在 `bin/`（或 Windows 的 `Scripts/`）目录生成同名脚本。敲 `flashinfer` 等价于 `python -m flashinfer`。
- **JIT 模块（JIT module）**：FlashInfer 里「一段会被现场编译的 CUDA 代码包」的单位。一个模块 = 一个名字 + 一组 `.cu` 源文件 + 一组编译选项（`JitSpec`）。FlashInfer 把所有「可能要编译的模块」登记到一个全局登记表（registry）里，CLI 的 `list-modules` 就是把这个登记表打印出来。

如果你对 `click`（一个流行的命令行解析库）不熟悉，不用怕：本讲会用「装饰器把普通函数变成子命令」这一句话解释它，重点放在 FlashInfer 自己的逻辑上。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [`pyproject.toml`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml) | 打包配置 | `[project.scripts]` 里注册的 CLI 入口 |
| [`flashinfer/__main__.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py) | **CLI 全部实现** | `click` 命令组与所有子命令 |
| [`flashinfer/__init__.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__init__.py) | 包入口 | 只是被 `__main__.py` 间接依赖，本身不含 CLI 逻辑 |
| [`flashinfer/jit/core.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py) | JIT 核心 | `jit_spec_registry`（登记表）、`JitSpecStatus`、`clear_cache_dir()` |
| [`flashinfer/aot.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/aot.py) | AOT 预编译 | `register_default_modules()`——CLI 兜底注册的来源 |
| [`flashinfer/jit/env.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py) | 工作区路径 | `FLASHINFER_CACHE_DIR` / `FLASHINFER_CUBIN_DIR` 等路径常量 |

> 一句话地图：`pyproject.toml` 把命令 `flashinfer` 指向 `__main__.py:cli`；`__main__.py` 里每个 `@cli.command(...)` 是一个子命令；子命令们围绕「登记表 `jit_spec_registry`」和「两类产物路径」做查询与清理。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：CLI 入口、`show-config` 体检、模块注册机制、`list-modules`/`module-status` 查询、缓存管理与 `export-compile-commands`。

### 4.1 CLI 入口：`flashinfer` 命令是怎么来的

#### 4.1.1 概念说明

你装完 FlashInfer 后，终端里就能敲 `flashinfer --help`。这并不是魔法，而是两步：

1. **打包时**：`pyproject.toml` 的 `[project.scripts]` 表声明了一个命令。
2. **安装时**：pip 读到这个声明，在 Python 环境的可执行目录里生成一个叫 `flashinfer` 的小脚本，它干的事等价于「调用 `flashinfer.__main__` 模块里的 `cli` 函数」。

FlashInfer 的 CLI 用 [`click`](https://click.palletsprojects.com/) 库实现。`click` 的用法可以浓缩成一句：**用 `@click.group` 定义一个「命令组」，用 `@组名.command("名字")` 往组里挂子命令**。每个子命令就是一个普通 Python 函数，参数由 `@click.option` / `@click.argument` 声明。

#### 4.1.2 核心流程

```text
用户敲: flashinfer show-config
   │
   ▼
pip 生成的入口脚本 → 调用 flashinfer/__main__.py:cli()
   │
   ▼
click 解析 "show-config" → 路由到 @cli.command("show-config") 装饰的函数
   │
   ▼
show_config_cmd() 执行，打印配置
```

如果只敲 `flashinfer`（不带子命令），`click` 会打印帮助信息——这正是「命令组」的默认行为。

#### 4.1.3 源码精读

**入口点声明**——这是「`flashinfer` 命令存在」的唯一原因，位于 [`pyproject.toml:39-40`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L39-L40)：

```toml
[project.scripts]
flashinfer = "flashinfer.__main__:cli"
```

> 这一行告诉 pip：「生成一个叫 `flashinfer` 的命令，它调用 `flashinfer/__main__.py` 文件里的 `cli` 对象」。

**命令组定义**——[`flashinfer/__main__.py:63-73`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L63-L73) 用 `@click.group` 定义了根命令组 `cli`，并支持一个 `--download-cubin` 快捷开关；不带任何子命令时打印帮助：

```python
@click.group(invoke_without_command=True)
@click.option(
    "--download-cubin", "download_cubin_flag", is_flag=True, help="Download artifacts"
)
@click.pass_context
def cli(ctx, download_cubin_flag):
    """FlashInfer CLI"""
    if download_cubin_flag:
        _download_cubin()
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
```

**子命令的全景**——本文件里每个 `@cli.command(...)` 就是一个子命令。本讲重点关注的几个，连同它们在文件中的位置：

| 子命令 | 代码位置 | 一句话作用 |
|--------|---------|-----------|
| `show-config` | [`__main__.py:93-175`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L93-L175) | 打印版本/环境/路径/模块统计的「体检报告」 |
| `list-cubins` | [`__main__.py:178-189`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L178-L189) | 列出 cubin 文件及下载状态 |
| `download-cubin` | [`__main__.py:193-196`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L193-L196) | 下载预编译 cubin |
| `clear-cache` | [`__main__.py:199-206`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L199-L206) | 清理 JIT 编译产物 |
| `clear-cubin` | [`__main__.py:209-216`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L209-L216) | 清理已下载的 cubin |
| `module-status` | [`__main__.py:219-284`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L219-L284) | 查看模块编译状态（支持过滤） |
| `list-modules` | [`__main__.py:287-331`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L287-L331) | 列出/检视已注册模块 |
| `export-compile-commands` | [`__main__.py:334-383`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L334-L383) | 导出 `compile_commands.json` |
| `generate-tactics-blocklist` | [`__main__.py:386-440`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L386-L440) | 生成 autotuner 黑名单（需 GPU，进阶用） |
| `replay` | [`__main__.py:443-513`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L443-L513) | 回放 API dump（进阶用） |

> 注意：`show-config` 里打印的「版本」来自 [`flashinfer/__main__.py:35`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L35) 的 `from .version import __version__`，这就是 u1-l2 讲过的「单一事实源版本链」的终点之一。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `flashinfer` 命令与 `__main__.py:cli` 的对应关系，并浏览所有子命令。

**操作步骤**：

1. 在装好 flashinfer 的环境里执行：
   ```bash
   flashinfer --help
   ```
2. 对照上面的表格，确认帮助里列出的子命令与源码里的 `@cli.command(...)` 一一对应。
3. 用 `python -m flashinfer --help` 重复一次。这两个应该等价——因为 `__main__.py` 文件末尾的 [`if __name__ == "__main__": cli()`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L516-L517) 让「作为模块运行」也会调用同一个 `cli`。

**需要观察的现象**：`--help` 输出里包含 `show-config`、`list-modules`、`module-status`、`clear-cache`、`download-cubin`、`export-compile-commands` 等条目。

**预期结果**：两行命令的输出一致，子命令清单与上表吻合。

**待本地验证**：不同版本的 FlashInfer 可能增减子命令；以你本机 `--help` 的实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果不安装包，直接在源码目录运行 `python -m flashinfer --help`，会得到一样的输出吗？为什么？

> **参考答案**：会得到一样的子命令清单（前提是依赖 `click`、`tabulate` 等已安装）。因为 `python -m flashinfer` 会执行 `flashinfer/__main__.py`，而该文件末尾 `if __name__ == "__main__": cli()` 与 pip 注册的入口点指向同一个 `cli` 对象。区别只在于「命令名」——一个是 `flashinfer`，一个是 `python -m flashinfer`。

**练习 2**：`@click.group(invoke_without_command=True)` 里的 `invoke_without_command=True` 去掉会怎样？

> **参考答案**：`click` 的 group 默认在「没有子命令」时会报错退出。设了 `invoke_without_command=True`，才允许 `cli` 函数体在「无子命令」时被执行，于是 [`__main__.py:72-73`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L72-L73) 的 `click.echo(ctx.get_help())` 才有机会打印帮助。

---

### 4.2 `show-config`：一份「环境体检报告」

#### 4.2.1 概念说明

`flashinfer show-config` 是排查问题时的第一站。它把「我的环境到底配成什么样了」一次性打印出来，分若干小节：

- **版本信息**：flashinfer 主版本，以及可选的 `flashinfer-cubin`、`flashinfer-jit-cache` 两个加速包是否安装（u1-l2 讲过的三包协作）。
- **Torch 信息**：PyTorch 版本、CUDA runtime 是否可用。
- **环境变量**：缓存目录、cubin 目录、目标 CUDA 架构、CUDA 版本、CUDA_HOME、nvcc 是否找得到。
- **Artifact 路径**：各类后端 cubin 在仓库里的相对路径前缀。
- **已下载 cubin**：`已下载/总数`。
- **模块状态**：登记表里「已编译 / 未编译 / 总数」的统计。

#### 4.2.2 核心流程

```text
show_config_cmd()
  ├── 打印版本（__version__ + 两个可选包）
  ├── 打印 Torch 版本与 cuda.is_available()
  ├── 遍历 env_variables 字典打印环境变量
  ├── 遍历 ArtifactPath 的类属性打印路径
  ├── get_artifacts_status() → 打印 cubin 下载数量
  └── _ensure_modules_registered() → get_stats() → 打印模块统计
```

#### 4.2.3 源码精读

**环境变量字典**——[`flashinfer/__main__.py:77-90`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L77-L90) 在模块导入时就组装好了一个 `env_variables` 字典，`show-config` 直接遍历它打印：

```python
env_variables = {
    "FLASHINFER_CACHE_DIR": FLASHINFER_CACHE_DIR,
    "FLASHINFER_CUBIN_DIR": FLASHINFER_CUBIN_DIR,
    "FLASHINFER_CUDA_ARCH_LIST": current_compilation_context.TARGET_CUDA_ARCHS,
    "FLASHINFER_CUDA_VERSION": get_cuda_version(),
    "FLASHINFER_CUBINS_REPOSITORY": FLASHINFER_CUBINS_REPOSITORY,
    "CUDA_VERSION": get_cuda_version(),
}
```

> 这里的 `FLASHINFER_CACHE_DIR` 和 `FLASHINFER_CUBIN_DIR` 是 [`flashinfer/jit/env.py:55`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L55) 与 [`flashinfer/jit/env.py:97`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L97) 解析出来的实际路径（不是原始字符串环境变量），所以你能直接看到缓存落在磁盘的哪里。

**可选包探测**——[`__main__.py:102-119`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L102-L119) 用 `importlib.metadata.version(...)` 探测 `flashinfer-cubin` 和 `flashinfer-jit-cache`，找不到就标 `Not installed`：

```python
cubin_version = importlib.metadata.version("flashinfer-cubin")
```

**cubin 下载数量**——[`__main__.py:158-165`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L158-L165) 调用 `get_artifacts_status()`（来自 `flashinfer/artifacts.py`），它返回一个 `[(文件名, 是否已下载), ...]` 的元组列表（见 [`artifacts.py:318-332`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/artifacts.py#L318-L332)），然后统计：

```python
status = get_artifacts_status()
num_downloaded = sum(1 for _, exists in status if exists)
total_cubins = len(status)
click.secho(f"Downloaded {num_downloaded}/{total_cubins} cubins", fg="cyan")
```

**模块统计**——最末尾 [`__main__.py:167-175`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L167-L175) 调用 `_ensure_modules_registered()`（4.3 节详解）确保登记表非空，再取 `get_stats()` 打印总数。

#### 4.2.4 代码实践

**实践目标**：跑一次 `show-config`，对照源码确认每个小节来自哪段代码。

**操作步骤**：

1. 执行：
   ```bash
   flashinfer show-config
   ```
2. 在输出里定位 `=== Environment Variables ===` 小节。
3. 把其中 `FLASHINFER_CACHE_DIR:` 后面的路径，用 `ls` 看一下是否真实存在；它的结构应是 `0.6.x/<arch>/cached_ops` 与 `0.6.x/<arch>/generated`（u1-l2 已讲过分层）。

**需要观察的现象**：`flashinfer-cubin` 与 `flashinfer-jit-cache` 两行显示 `Not installed`（如果你只装了主包），或具体版本（如果装了加速包）。

**预期结果**：所有 `=== ... ===` 小节都能在 [`__main__.py:93-175`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L93-L175) 里找到对应的 `click.secho("=== ... ===")`。

**待本地验证**：具体版本号、架构字符串、cubin 数量取决于你的机器与已下载情况。

#### 4.2.5 小练习与答案

**练习 1**：`show-config` 里 `NVCC found: No` 但 `CUDA runtime available: Yes`，这种组合可能意味着什么？

> **参考答案**：PyTorch 自带了 CUDA *runtime*（一组动态库），所以 `torch.cuda.is_available()` 为 Yes；但 `nvcc` 是 CUDA *toolkit* 的编译器，没装 toolkit 或 `CUDA_HOME` 指错了路径就会找不到 nvcc。没有 nvcc 意味着 **JIT 编译无法进行**——任何首次调用 kernel 都会失败。这正是 `show-config` 要同时检查两者的原因。

**练习 2**：`ArtifactPath` 里那些带哈希的路径（如 `158f6fa11ef139a098cfddcdddce73ca99d164ad/fmha/trtllm-gen/`）是干什么用的？

> **参考答案**：它们是各类后端（trtllm-gen / cuDNN SDPA / DeepGEMM / CuTe-DSL 等）预编译 cubin 在 Artifactory 仓库里的相对路径前缀（见 [`artifacts.py:131-149`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/artifacts.py#L131-L149)）。`download-cubin` 下载、`list-cubins` 检查的就是这些路径下的二进制。本讲只需知道它们是「远端预编译产物的索引」，下载机制会在 u9-l4 深入。

---

### 4.3 模块注册机制：登记表 `jit_spec_registry`

#### 4.3.1 概念说明

这是本讲最关键的概念，也是 `list-modules` / `module-status` / `show-config` 三个命令的共同基石。

FlashInfer 把「一个待编译的 CUDA 模块」抽象成 `JitSpec`（名字 + 源文件 + 编译选项）。但散落各处的 `gen_*_module` 函数每被调用一次，就「生产」出一个 `JitSpec`。需要一个地方把这些 `JitSpec` 统一登记下来，CLI 才能查询「我到底有多少个模块、各自编没编译」。

这个地方就是 **全局登记表 `jit_spec_registry`**——一个进程级的单例 `JitSpecRegistry` 实例。

> 关键直觉：**「登记」不等于「编译」**。一个模块被 `register` 进登记表，只是说明「我知道有这么个模块、它的源文件在哪」，并不意味着它已经被编译成 `.so`。`is_compiled` 是否为真，取决于对应的 `.so` 文件是否存在于磁盘上。

#### 4.3.2 核心流程

模块进入登记表有两条路径：

```text
路径 A：用户真正调用某个 API（如 single_decode_with_kv_cache）
   → 该 API 的 @functools.cache 装饰的加载函数
   → gen_*_module(...) 生成 JitSpec
   → gen_jit_spec(...) 内部调用 jit_spec_registry.register(spec)
   → 模块进入登记表

路径 B：CLI 兜底注册（_ensure_modules_registered）
   → 若登记表为空
   → from .aot import register_default_modules
   → register_default_modules() → gen_all_modules(...)
   → 内部同样调用 gen_jit_spec → register
   → 把"默认全套"模块一次性塞进登记表
```

路径 A 是「按需注册」（用到哪个算子才注册哪个）；路径 B 是 CLI 的「兜底批量注册」，让你不跑任何模型也能看到完整模块清单——这正是 `list-modules` 一开始可能为空、需要兜底的原因。

#### 4.3.3 源码精读

**登记表本体**——[`flashinfer/jit/core.py:160-214`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L160-L214) 定义了 `JitSpecRegistry`，并在模块层创建全局单例：

```python
class JitSpecRegistry:
    """Global registry to track all JitSpecs"""

    def __init__(self):
        self._specs: Dict[str, JitSpec] = {}
        self._creation_times: Dict[str, datetime] = {}

    def register(self, spec: "JitSpec") -> None:
        """Register a new JitSpec"""
        if spec.name not in self._specs:
            self._specs[spec.name] = spec
            self._creation_times[spec.name] = datetime.now()
    ...
# Global registry instance
jit_spec_registry = JitSpecRegistry()
```

> `register` 用 `if spec.name not in self._specs` 做去重——同名模块只登记一次，多次调用 `gen_xxx` 是幂等的。

**统计接口**——`get_stats()` 是 CLI 打印「总数/已编译/未编译」的依据，[`core.py:203-210`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L203-L210)：

```python
def get_stats(self) -> Dict[str, int]:
    statuses = self.get_all_statuses()
    return {
        "total": len(statuses),
        "compiled": sum(1 for s in statuses if s.is_compiled),
        "not_compiled": sum(1 for s in statuses if not s.is_compiled),
    }
```

其中 `is_compiled` 是 `JitSpec` 的属性，[`core.py:263-265`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L263-L265) 定义——它纯粹看 `.so` 文件是否存在：

```python
@property
def is_compiled(self) -> bool:
    return self.get_library_path().exists()
```

**真正的注册动作发生在 `gen_jit_spec`**——任何 `gen_*_module` 最终都汇聚到 `gen_jit_spec(...)`，它在 [`core.py:481-482`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L481-L482) 把构造好的 spec 塞进登记表：

```python
    # Register the spec in the global registry
    jit_spec_registry.register(spec)
    return spec
```

**CLI 的兜底注册**——当登记表为空时，`_ensure_modules_registered()` 会调用 AOT 模块的 `register_default_modules()`，见 [`__main__.py:47-60`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L47-L60)：

```python
def _ensure_modules_registered():
    """Helper function to ensure modules are registered"""
    statuses = jit_spec_registry.get_all_statuses()
    if not statuses:
        click.secho("No modules found. Registering default modules...", fg="yellow")
        try:
            from .aot import register_default_modules
            num_registered = register_default_modules()
            click.secho(f"✅ Registered {num_registered} modules", fg="green")
            statuses = jit_spec_registry.get_all_statuses()
        except Exception as e:
            click.secho(f"❌ Module registration failed: {e}", fg="red")
    return statuses
```

`register_default_modules()` 本体在 [`flashinfer/aot.py:961-982`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/aot.py#L961-L982)，它读取一份默认配置（dtype/head_dim/要不要 MoE/要不要 comm 等），调 `gen_all_modules(...)` 一次性生成全套 spec——**注意这里只生成和登记，并不编译**，编译发生在真正调用时：

```python
def register_default_modules() -> int:
    """Register the default set of modules"""
    config = get_default_config()
    sm_capabilities = detect_sm_capabilities()
    jit_specs = gen_all_modules(
        config["f16_dtype"], config["f8_dtype"], ...
    )
    return len(jit_specs)
```

#### 4.3.4 代码实践

**实践目标**：亲手观察「登记表为空 → 兜底注册 → 登记表被填满」的过程。

**操作步骤**（源码阅读型，无需 GPU 编译）：

1. 打开一个 Python 交互环境（`python`），按以下顺序操作：
   ```python
   import flashinfer
   from flashinfer.jit import jit_spec_registry

   # 此时还没调用任何算子，登记表是什么状态？
   print("初始模块数:", jit_spec_registry.get_stats()["total"])
   ```
2. 若上面输出为 `0`，手动触发兜底注册（模拟 CLI 的行为）：
   ```python
   from flashinfer.aot import register_default_modules
   n = register_default_modules()
   print("本次注册了:", n)
   print("注册后模块数:", jit_spec_registry.get_stats()["total"])
   ```
3. 用 `jit_spec_registry.get_all_statuses()[0]` 取第一个模块，查看它的 `.name`、`.is_compiled`、`.sources` 字段。

**需要观察的现象**：初始为 0（或少量）；调用 `register_default_modules()` 后总数变为成百上千；绝大多数模块的 `is_compiled` 为 `False`（因为只是登记，没编译）。

**预期结果**：`get_stats()` 返回 `{'total': <很大>, 'compiled': <很小或 0>, 'not_compiled': <很大>}`，验证「登记 ≠ 编译」。

**待本地验证**：具体 `total` 数值取决于版本、SM 能力探测结果、默认配置项（如是否启用 MoE/comm），不同机器会不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_ensure_modules_registered()` 要写成「先查再补」，而不是无条件每次都调 `register_default_modules()`？

> **参考答案**：因为路径 A（用户调用算子）会按需把模块登记进去，这些「真正会用到的模块」信息更精确（带实际 dtype/head_dim）。如果每次 CLI 都强行 `register_default_modules()`，虽然 `register` 内部有去重（幂等），但会浪费时间去生成一大堆用户根本用不到的 spec。先查再补，既保证「表非空」让 CLI 有东西可显示，又不破坏已登记的精确模块。

**练习 2**：一个模块 `is_compiled == False`，可能是因为还没用过，也可能是 `.so` 被删了。`JitSpec` 的代码能区分这两种情况吗？

> **参考答案**：不能。`is_compiled` 只看 `get_library_path().exists()`（[`core.py:264-265`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L264-L265)），「从没编译过」和「编译过又被 `rm` 了」在它眼里都是「文件不存在」。区分二者没有意义——下次用到时会自动重新编译（这正是 JIT 的设计）。所以 `clear-cache` 之后 `module-status` 会显示一大片 `Not Compiled`，是正常现象。

---

### 4.4 `list-modules` 与 `module-status`：查询登记表

#### 4.4.1 概念说明

有了登记表，查询命令就很直观了：

- **`list-modules [模块名]`**：不带参数 → 列出所有已注册模块的名字与状态；带参数 → 显示某个模块的详情（库路径、源文件列表、是否需要 device linking）。
- **`module-status [--detailed] [--filter all|aot|jit|compiled|not-compiled]`**：以表格（或详细文本）形式展示编译状态，并支持按状态过滤，末尾附统计摘要。

#### 4.4.2 核心流程

```text
list_modules_cmd(module_name):
  _ensure_modules_registered()
  if module_name:
     get_spec_status(module_name) → 打印该模块详情
  else:
     get_all_statuses() → 排序 → 逐行打印 "名字 - 状态"

module_status_cmd(detailed, filter):
  _ensure_modules_registered()
  按 filter 过滤（compiled/not-compiled/...）
  if detailed: 逐模块详尽打印
  else:        tabulate 表格打印
  末尾 get_stats() 摘要
```

#### 4.4.3 源码精读

**`module-status` 的过滤能力**——[`__main__.py:219-284`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L219-L284) 用 `@click.option` 声明了 `--detailed` 开关和 `--filter` 限定选项（`click.Choice` 限制只能从给定值里选）：

```python
@cli.command("module-status")
@click.option("--detailed", is_flag=True, help="Show detailed information")
@click.option(
    "--filter",
    type=click.Choice(["all", "aot", "jit", "compiled", "not-compiled"]),
    default="all",
    help="Filter modules by compilation type or status",
)
def module_status_cmd(detailed, filter):
    ...
    filter_map = {
        "compiled": lambda s: s.is_compiled,
        "not-compiled": lambda s: not s.is_compiled,
    }
    if filter in filter_map:
        statuses = [s for s in statuses if filter_map[filter](s)]
```

> 注意：`filter_map` 只显式实现了 `compiled` / `not-compiled` 两个过滤谓词；`all`/`aot`/`jit` 落到默认（不过滤，等价 all）。表格视图用 `tabulate` 渲染（表头 `Module Name / Type / Status / Sources / Device Linking`）。

**`list-modules` 的详情视图**——当传入模块名时，[`__main__.py:296-318`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L296-L318) 打印该模块的源文件清单等：

```python
status = jit_spec_registry.get_spec_status(module_name)
...
click.secho("Source Files:", fg="white")
for i, source in enumerate(status.sources, 1):
    click.secho(f"  {i}. {source}", fg="white")
```

> 这个 `sources` 字段来自 `JitSpecStatus`（[`core.py:141-157`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L141-L157)），它记录了该模块参与编译的全部 `.cu` 文件路径——这是把 CLI 与具体 kernel 源码联系起来的关键信息（见 4.4.4 实践）。

#### 4.4.4 代码实践

**实践目标**：统计注册模块数量，并找到一个注意力模块背后的源文件。

**操作步骤**：

1. 先确保登记表非空并统计：
   ```bash
   flashinfer module-status
   ```
   记下末尾 `=== Summary ===` 里的 `Total modules:` 数值。
2. 只看未编译的：
   ```bash
   flashinfer module-status --filter not-compiled
   ```
3. 浏览全部模块名，找一个名字里含 `decode` 的模块，用详情视图查看它的源文件：
   ```bash
   flashinfer list-modules <那个decode模块名>
   ```
   （模块名以 `flashinfer list-modules`（无参数）列出的为准。）

**需要观察的现象**：第 3 步打印的 `Source Files:` 列表里，应能看到形如 `.../generated/<uri>/batch_decode.cu`、`.../batch_decode_jit_binding.cu` 等路径——这些就是 u1-l3 讲过的「csrc launcher + FFI 绑定」层文件。

**预期结果**：`Total modules` 是一个较大的数（成百上千），且与 4.3.4 里 Python 实验得到的数字一致；详情视图的源文件能对应到 `csrc/` 或 `generated/` 目录。

**待本地验证**：具体模块名（URI 哈希）与确切数量因机器/版本而异，不要照抄。

#### 4.4.5 小练习与答案

**练习 1**：`flashinfer module-status --filter aot` 想筛出「AOT 预编译」的模块，但源码里 `filter_map` 没有 `aot` 分支，会发生什么？

> **参考答案**：会落到默认分支——不过滤，等价于显示全部模块。要判断某个模块是不是 AOT（预编译），应看 `JitSpec.is_aot` 属性（[`core.py:260-261`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L260-L261)，看 `aot_path` 是否存在），但 CLI 的 `--filter aot` 目前并未真正实现该谓词。这是阅读源码才能发现的细节。

**练习 2**：`list-modules` 详情里的 `Device Linking: Required/Not required` 是什么含义？

> **参考答案**：某些 kernel 在 `.cu` → `.o` 之后，还需要一个「device link」步骤把设备端符号（如 dynamic parallelism、`__device__` 全局变量）链接进最终的 `.so`。`needs_device_linking=True` 的模块编译更慢、流程更长（见 `JitSpec.needs_device_linking`，[`core.py:226`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L226)）。知道哪些模块需要它，有助于理解为何有些模块首次编译特别久。

---

### 4.5 缓存管理与 `export-compile-commands`

#### 4.5.1 概念说明

本模块收尾两件事：**清理产物** 与 **导出编译数据库**。

FlashInfer 的磁盘上有两类产物，必须分清（u1-l2 也强调过）：

| 产物 | 位置 | 由谁产生 | 清理命令 |
|------|------|---------|---------|
| JIT 编译的 `.so` + `generated/` 源码 | `FLASHINFER_JIT_DIR`（`cached_ops`）与 `FLASHINFER_GEN_SRC_DIR`（`generated`） | 首次调用算子时现场编译 | `flashinfer clear-cache` |
| 下载的预编译 cubin | `FLASHINFER_CUBIN_DIR` | `flashinfer download-cubin` | `flashinfer clear-cubin` |

另一件利器是 `export-compile-commands`：它把所有已注册模块的「nvcc 编译命令」导出成标准格式的 `compile_commands.json`。这是 [Clangd / IDE 通用的编译数据库格式](https://clang.llvm.org/docs/JSONCompilationDatabase.html)，导入后编辑器就能在 `.cu`/`.cuh` 文件里做「跳转定义、自动补全、错误提示」——对阅读 FlashInfer 这种巨量模板代码极为有用。

#### 4.5.2 核心流程

```text
clear_cache_cmd():
  clear_cache_dir()  # 删 FLASHINFER_JIT_DIR 整个目录

export_compile_commands_cmd(path="compile_commands.json"):
  _ensure_modules_registered()
  all_specs = jit_spec_registry.get_all_specs()
  for spec in all_specs.values():
      spec.get_compile_commands()   # 每个模块生成若干条 nvcc 命令
  json.dump(合并后的命令列表, output_path)
```

#### 4.5.3 源码精读

**清理 JIT 缓存**——[`__main__.py:199-206`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L199-L206) 的 `clear-cache` 直接调用 `clear_cache_dir()`，而后者 [`core.py:111-115`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L111-L115) 是一把「铲掉整个 JIT 目录」的暴力操作：

```python
def clear_cache_dir():
    if os.path.exists(jit_env.FLASHINFER_JIT_DIR):
        import shutil
        shutil.rmtree(jit_env.FLASHINFER_JIT_DIR)
```

> 注意它删的是 `FLASHINFER_JIT_DIR = FLASHINFER_WORKSPACE_DIR / "cached_ops"`（[`env.py:149-150`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L149-L150)），即只删「本版本 + 本架构」那一层。下次再调用算子，会触发重新 JIT 编译。

**清理 cubin** 与下载相对——[`__main__.py:209-216`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L209-L216) 的 `clear-cubin` 走的是另一条路（`clear_cubin()`，见 [`artifacts.py:335-340`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/artifacts.py#L335-L340)），删的是 `FLASHINFER_CUBIN_DIR`。两个 `clear-*` 互不干扰，对应 u1-l2 讲的「JIT 缓存 vs cubin 缓存」两套独立体系。

**导出编译数据库**——[`__main__.py:334-383`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L334-L383) 遍历所有 spec，调用每个 spec 的 `get_compile_commands()`，合并写入 JSON：

```python
_ensure_modules_registered()
all_specs = jit_spec_registry.get_all_specs()
...
all_compile_commands = []
for spec in all_specs.values():
    try:
        compile_commands = spec.get_compile_commands()
        all_compile_commands.extend(compile_commands)
    except Exception as e:
        click.secho(f"Warning: Failed to generate compile commands for {spec.name}: {e}", ...)
with open(output_path, "w") as f:
    json.dump(all_compile_commands, f, indent=2)
```

> `get_compile_commands()` 本体在 [`core.py:321`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L321) 起，它会复用与真实编译完全一致的 cflags/cuda_cflags（通过 `build_common_cflags` / `build_cflags` / `build_cuda_cflags`），所以导出的命令与实际 `nvcc` 调用一致——这也是它能驱动 IDE 正确解析的原因。

#### 4.5.4 代码实践

**实践目标**：导出 `compile_commands.json` 并用它打开一个 kernel 文件，同时验证 `clear-cache` 的效果。

**操作步骤**：

1. 导出编译数据库（默认写到当前目录的 `compile_commands.json`）：
   ```bash
   flashinfer export-compile-commands
   ```
   也可以用 `-o` 指定路径：`flashinfer export-compile-commands -o ~/compile_commands.json`。
2. 用编辑器打开仓库里的某个 kernel 头文件，例如：
   ```bash
   # 在支持 clangd 的编辑器（VS Code + clangd 扩展）里打开
   code include/flashinfer/attention/decode.cuh
   ```
   把第 1 步生成的 `compile_commands.json` 放到仓库根目录（或让 clangd 能找到），观察「跳转到定义」「自动补全」是否生效。
3. 验证 `clear-cache`：先记录 `module-status` 里 `Compiled` 的数量，再执行：
   ```bash
   flashinfer clear-cache
   flashinfer module-status
   ```
   对比前后 `Compiled` 数量变化。

**需要观察的现象**：第 1 步输出 `✅ Successfully exported N compile commands`；第 2 步在 `.cuh` 里可以跳转到 CUTLASS 等头文件；第 3 步清缓存后 `Compiled` 数下降（甚至归零）。

**预期结果**：`export-compile-commands` 成功生成 JSON；`clear-cache` 后 `module-status` 显示大量模块变为 `Not Compiled`。

**待本地验证**：导出的命令条数 `N`、IDE 是否真的解析成功，取决于你装的 clangd/扩展配置以及是否已 `git submodule update --init`（CUTLASS 头文件要存在）。

> ⚠️ 注意：`clear-cache` 会删除已编译产物，下次运行任何 FlashInfer 算子都会**重新编译**（耗时）。生产环境慎用。

#### 4.5.5 小练习与答案

**练习 1**：执行 `flashinfer clear-cache` 后，cubin 还在吗？为什么？

> **参考答案**：在。`clear_cache_dir()` 只删 `FLASHINFER_JIT_DIR`（`cached_ops`），而 cubin 在 `FLASHINFER_CUBIN_DIR`（[`env.py:55`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L55) 与 [`env.py:97`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L97)），两者是不同目录。要清 cubin 必须用 `flashinfer clear-cubin`。这正是 FlashInfer 把「JIT 产物」与「预编译 cubin」分开管理的体现。

**练习 2**：`export-compile-commands` 生成的命令，和真正 JIT 编译时用的命令，会一模一样吗？

> **参考答案**：核心 flags（include 路径、`-gencode` 架构、宏定义）一致，因为 `get_compile_commands()` 复用了 [`cpp_ext`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py) 里同一套 `build_*_cflags`。但细节上，真正的 ninja 构建还包含 `.o` → `.so` 的链接步骤、文件锁、可能的 launcher（`FLASHINFER_NVCC_LAUNCHER`）等，这些不会出现在 `compile_commands.json` 里。所以它对 IDE 解析足够准确，但不等于「逐字可复现的编译命令」。

---

## 5. 综合实践

把本讲的知识串起来，完成一次「环境诊断 → 模块清单 → 源码定位 → 缓存治理」的全流程：

**任务背景**：假设你要在新机器上向同事说明「这台机器能跑哪些 FlashInfer 模块、它们都还没编译、相关 kernel 源码在哪」。

**步骤**：

1. 跑 `flashinfer show-config`，截取并保存输出。在报告里用一句话解释「为什么 `NVCC found` 这一栏必须为 Yes 才能正常用 FlashInfer」。
2. 跑 `flashinfer module-status`，记下 `Total modules` 与 `Not compiled` 数量；说明「为什么 `Not compiled` 那么多，却不是 bug」（提示：登记 ≠ 编译，见 4.3）。
3. 跑 `flashinfer list-modules`，挑一个名字含 `prefill` 或 `decode` 的模块，再用 `flashinfer list-modules <模块名>` 查看它的 `Source Files`，打开其中某个 `csrc/*.cu` 文件，确认它属于 u1-l3 讲过的「绑定层」。
4. 跑 `flashinfer export-compile-commands`，把生成的 `compile_commands.json` 移到仓库根目录，用支持 clangd 的编辑器打开第 3 步那个 `.cu` 文件，验证能否跳转到 `include/flashinfer/` 下的 kernel 头文件。
5. （可选，注意会触发重编译）跑 `flashinfer clear-cache`，再次 `module-status`，确认 `Compiled` 归零。

**验收标准**：你能向别人讲清——「`flashinfer` 命令来自 `pyproject.toml` 的入口点 → 它操作的是 `jit_spec_registry` 这个全局登记表 → 表里的模块由 `gen_jit_spec` 在生成时登记、或由 `register_default_modules` 兜底批量登记 → CLI 把这些信息以 `show-config`/`list-modules`/`module-status` 三种视角呈现 → `clear-cache` 与 `export-compile-commands` 分别管清理与 IDE 集成」。

## 6. 本讲小结

- `flashinfer` 命令的存在，源于 [`pyproject.toml`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L39-L40) 的 `[project.scripts]` 入口点 `flashinfer = "flashinfer.__main__:cli"`，实现全部在 `flashinfer/__main__.py`，用 `click` 把普通函数挂成子命令。
- `show-config` 是环境体检报告：版本、Torch、环境变量路径、cubin 下载数、模块统计，覆盖排查问题所需的全部信息。
- 所有模块查询命令的共同基石是全局登记表 `jit_spec_registry`（单例 `JitSpecRegistry`）；模块在 `gen_jit_spec` 里被 `register`，CLI 在表空时用 `register_default_modules()` 兜底批量登记。
- **「登记」≠「编译」**：`is_compiled` 只看 `.so` 文件是否存在（[`core.py:264-265`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L264-L265)），所以 `module-status` 里大片 `Not Compiled` 是正常现象。
- 两类产物要分清：JIT 编译产物（`cached_ops`，由 `clear-cache` 清理）与下载的 cubin（`FLASHINFER_CUBIN_DIR`，由 `clear-cubin` 清理），互不影响。
- `export-compile-commands` 把每个 `JitSpec` 的 nvcc 命令导出成 `compile_commands.json`，是阅读 FlashInfer 巨量模板代码时让 IDE 正确解析的关键利器。

## 7. 下一步学习建议

本讲只是「环境诊断 + 工具认知」，**全程没有触发一次真正的 JIT 编译**。下一步建议：

1. **进入第 2 单元（JIT 编译系统）**，从 u2-l1「JIT 编译概览：三层架构」开始，理解本讲反复出现的 `JitSpec` 是如何被「定义 → 生成 → 编译加载」的。本讲的 `jit_spec_registry` / `JitSpec.is_compiled` / `clear_cache_dir` 都会在第 2 单元得到完整解释。
2. 在阅读 u2-l2「JitSpec 与工作区环境」时，回头看本讲的 `show-config` 环境变量小节——你会把 `FLASHINFER_JIT_DIR` / `FLASHINFER_GEN_SRC_DIR` 与目录可写性规则对应起来。
3. 想深入了解 cubin 下载/校验机制（本讲只点到 `download-cubin` / `ArtifactPath`），可跳读 u9-l4「AOT 编译与预编译包」，但建议先完成 2、3 单元打好基础。
