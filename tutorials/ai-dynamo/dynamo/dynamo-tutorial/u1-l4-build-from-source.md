# u1-l4 从源码构建与开发环境

## 1. 本讲目标

学完本讲，你应该能够：

1. 在一台干净的 Linux 机器上，把 Dynamo 从源码完整构建出来，并说清楚每条命令各自产出什么。
2. 解释 `maturin develop --uv` 背后发生了什么：Rust crate `dynamo-py3` 如何变成 Python 里可 `import` 的 `dynamo._core` 扩展模块，以及 `import` 瞬间模块初始化做了哪些事。
3. 读懂 `hatch_build.py` 这个构建钩子如何在打包时把 git 短 SHA 注入到每个组件的 `_version.py`。
4. 用 `cargo build -p dynamo-runtime` 单独编译 Rust 侧，并分清它与 Python 扩展构建是两条互相独立的产物线。
5. 理解 `container/render.py` + `container/Dockerfile.template` 的 Jinja2 模板体系如何派生出各框架的 Dockerfile。
6. 理解 `container/deps/` 的依赖固定体系，特别是 uv `--overrides` 如何解决「传递依赖把版本拉回地板以下」的问题。

本讲是整个手册的「地基课」：后面所有需要改代码、加日志、跑测试的实践，都依赖你先在本机（或容器里）完成这次构建。

> **本讲更新说明（对应 2c4ab6c → 7feb2b8）**：这次代码变化影响本讲的有三处——(1) `rust/lib.rs` 的模块初始化新增了「把 Tokio builder 预设给 pyo3 异步桥接」的逻辑，`register_core` 内注册语句行号整体下移，4.1 节已同步；(2) 新增 `container/deps/overrides.frontend.txt`，为此新增 4.5 节讲 deps overrides 机制；(3) 根 `pyproject.toml` 的 sglang extra 从 `sglang==0.5.17` 升到 `0.5.18`、`nixl` 从 `1.3.2` 升到 `1.4.0`（`container/context.yaml` 的镜像 tag 同步更新），不影响构建步骤。全部永久链接与行号已刷新到当前 HEAD。

## 2. 前置知识

本讲涉及几个构建工具链概念，先逐个讲清楚：

- **uv**：Astral 出品的极速 Python 包管理器，命令面与 `pip`/`venv` 基本对应。`uv venv .venv` 建虚拟环境，`uv pip install ...` 装包。Dynamo 官方构建链全部用 uv。
- **虚拟环境（venv）**：一个目录级隔离的 Python 解释器 + 依赖集合。后面 `maturin develop` 会把编译产物直接装进「当前激活的那个 venv」，所以先激活再构建很重要。
- **PyO3**：让 Rust 代码可以被 Python 调用的 FFI 框架。你在 Python 里写 `from dynamo._core import DistributedRuntime` 时，这个 `DistributedRuntime` 其实是 Rust 结构体经 PyO3 包装后的类。
- **cdylib 与 rlib**：Rust 的两种库产物。`cdylib` 生成 `.so` 共享库（给 Python `import` 用）；`rlib` 生成 Rust 静态库（给其他 Rust crate 链接用）。Dynamo 的绑定 crate 两者都开。
- **abi3 / 稳定 ABI**：CPython 的稳定应用二进制接口。开启 `abi3-py310` 后编译出的 `.so` 只要用 Python 3.10 编译一次，就能在 3.10 及以上所有 CPython 版本里使用，而不必每个小版本各编一次。
- **maturin**：PyO3 官方推荐的构建工具。它读 `pyproject.toml` 里的 `[tool.maturin]` 配置，调用 cargo 编译 Rust crate，再把产物打包/安装成 Python 包。`maturin develop` 是「编译 + 装进当前 venv（可编辑）」的开发流。
- **hatchling 与构建钩子**：hatchling 是一个 PEP 517 构建后端。它在打包过程的特定时机会调用项目自定义的「构建钩子」——Dynamo 用这个机制在打包前生成版本文件。
- **Cargo workspace**：Rust 的多 crate 工程组织方式，一根 `Cargo.toml` 列出所有成员，共享一份锁文件和依赖版本。上一讲（u1-l3）已经建立这个概念。
- **Jinja2 模板**：Python 的模板语言，用 `{% if %}`/`{% include %}` 把一份模板渲染成不同文本。Dynamo 用它管理几十种 Dockerfile 变体。
- **依赖解析与 override**：装包时，uv 会把「所有相关方对同一个包的版本要求」放在一起求解。requirements 文件里的约束只是「要求」，可能被别的传递依赖顶掉；而 uv 的 `--overrides` 是更强的「裁决」——它会**替换**掉该包的所有其他约束，对整个解析全程生效。4.5 节会看到为什么必须用它。

如果这些名词中有几个还模糊，不影响往下读——下面每一节都会结合源码再解释一遍。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/README.md) | 顶层说明，`Building from Source` 一节是官方最短构建路径 |
| [pyproject.toml](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/pyproject.toml) | 根 Python 包 `ai-dynamo` 的定义：依赖、后端 extra、构建后端、pytest 配置 |
| [hatch_build.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/hatch_build.py) | hatchling 自定义构建钩子，打包前为每个组件写入 `_version.py` |
| [lib/bindings/python/pyproject.toml](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/pyproject.toml) | `ai-dynamo-runtime` 包定义与 `[tool.maturin]` 配置 |
| [lib/bindings/python/Cargo.toml](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml) | Rust 绑定 crate `dynamo-py3`：被排除出 workspace、`_core` lib 名、feature 开关 |
| [lib/bindings/python/rust/lib.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs) | `_core` 扩展模块的入口：注册所有暴露给 Python 的类与函数，并在 import 时预设 pyo3 异步桥接的 Tokio 运行时 |
| [lib/bindings/python/src/dynamo/runtime/__init__.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/runtime/__init__.py) | Python 包装层：从 `dynamo._core` 再导出 `DistributedRuntime` 等 |
| [Cargo.toml](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/Cargo.toml) | Rust workspace 根：35 个成员 crate 的清单 |
| [container/Dockerfile.template](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/Dockerfile.template) | Jinja2 主模板：按 framework/target 组合 include 子模板 |
| [container/render.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/render.py) | 渲染器：解析参数、校验组合合法性、产出 rendered.Dockerfile |
| [container/templates/wheel_builder.Dockerfile](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/wheel_builder.Dockerfile) | 容器内 wheel 构建阶段：`maturin build` + `uv build` 的生产版命令 |
| [container/templates/frontend.Dockerfile](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/frontend.Dockerfile) | frontend 镜像模板：三处 `uv pip install` 都带 `--overrides` |
| [container/deps/README.md](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/README.md) | 容器 Python 依赖文件体系与版本固定策略说明 |
| [container/deps/overrides.frontend.txt](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/overrides.frontend.txt) | frontend 镜像的 uv override（本次新增）：固定 pillow |
| [container/deps/overrides.planner.txt](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/overrides.planner.txt) | planner 镜像的 uv override：固定 aiohttp 与 pillow |

## 4. 核心概念与源码讲解

本讲的五个最小模块是：**maturin**（4.1）、**hatch_build.py**（4.2）、**cargo 与 Rust workspace**（4.3）、**container templates**（4.4）、**container deps overrides**（4.5）。

先把全景图放在这里，后面逐个展开：

```
源码仓库
├── lib/bindings/python          ← Rust crate "dynamo-py3"（不在 workspace 里）
│     ├── Cargo.toml             ← lib 名 = _core, crate-type = cdylib+rlib
│     ├── rust/lib.rs            ← #[pymodule] fn _core，注册所有 Python 可见类型
│     └── pyproject.toml         ← 包名 ai-dynamo-runtime，[tool.maturin]
│            │
│            │  maturin develop --uv
│            ▼
│   产物①  dynamo/_core.abi3.so  （装进 venv 的 C 扩展）
│
├── components/src/dynamo/...    ← 纯 Python 包源码（frontend、vllm、planner…）
│            │
│            │  uv pip install -e .   （hatchling + hatch_build.py 钩子）
│            ▼
│   产物②  可编辑安装的 ai-dynamo  （依赖 ai-dynamo-runtime==1.5.0）
│
├── lib/runtime 等 35 个 crate   ← 普通 Rust workspace 成员
│            │
│            │  cargo build -p dynamo-runtime
│            ▼
│   产物③  target/debug/libdynamo_runtime.rlib  （Rust 静态库）
│
└── container/                   ← Jinja2 模板体系 + 分组件依赖清单
      ├── Dockerfile.template + render.py
      │        │  python container/render.py ...
      │        ▼
      │   产物④  <framework>-<target>-...-rendered.Dockerfile
      └── deps/                  ← requirements.*.txt + overrides.*.txt
                （决定镜像里每个 Python 包最终装成什么版本）
```

### 4.1 maturin：把 Rust crate 编译成 Python 扩展

#### 4.1.1 概念说明

上一讲（u1-l3）我们已经知道：仓库产出两个 wheel——`ai-dynamo-runtime`（内含 Rust 编译出的 `_core` 扩展和 `dynamo.runtime`/`dynamo.llm` 包装层）和 `ai-dynamo`（frontend、各引擎后端、planner 等纯 Python 代码）。

本模块要回答的问题是：**Rust 源码是怎么变成 Python 里一个可 `import` 的模块的？**

答案是 PyO3 + maturin 这对组合：

- PyO3 负责「写」：在 Rust 里用 `#[pyclass]`、`#[pymodule]` 宏标注的类型，编译后会带上 Python C API 的包装代码。
- maturin 负责「建」：读 `pyproject.toml`，调用 cargo 编译，把得到的 `.so` 按正确的目录结构装进 Python 包。

关键在于：**这个 crate 被故意排除在 Rust workspace 之外**，必须由 maturin 单独构建。这是全仓库最容易被初学者踩坑的一点——你直接 `cargo build`（在仓库根目录）是编不到它的。

#### 4.1.2 核心流程

官方构建链（README `Building from Source`）共 6 步：

```
1. sudo apt install -y build-essential libhwloc-dev libudev-dev pkg-config \
      libclang-dev protobuf-compiler python3-dev cmake     # 系统依赖
2. curl ... https://sh.rustup.rs | sh && source $HOME/.cargo/env  # Rust 工具链
3. uv venv dynamo && source dynamo/bin/activate           # 虚拟环境
4. uv pip install pip 'maturin[patchelf]'                  # 装 maturin
5. cd lib/bindings/python && maturin develop --uv && cd -  # 编译并安装 Rust 扩展
6. uv pip install -e lib/gpu_memory_service && uv pip install -e .  # 装 Python 层
```

其中第 5 步内部发生的事情：

```
maturin develop --uv
  ├─ 读 lib/bindings/python/pyproject.toml 的 [tool.maturin]
  │    module-name = "dynamo._core"     ← 决定扩展模块的 import 路径
  │    python-source  = "src"           ← Python 源码在 src/ 下
  │    python-packages = ["dynamo"]     ← 要打进包的 Python 目录
  ├─ 调 cargo 编译 crate dynamo-py3（lib 名 _core，crate-type 含 cdylib）
  ├─ 得到 _core.abi3.so，放到 src/dynamo/ 下的正确位置
  └─ 以可编辑（editable）方式把 ai-dynamo-runtime 装进当前 venv
```

第 6 步 `uv pip install -e .` 装根目录的 `ai-dynamo`，它声明依赖 `ai-dynamo-runtime==1.5.0`——由于第 5 步已经装好了同版本的可编辑包，这一步不会去 PyPI 下载，而是直接用你本地的构建。

装好之后，第一次 `import dynamo._core` 时模块初始化（`register_core`）会做两件事：先初始化日志，再**读取运行时配置并把 Tokio builder 交给 pyo3 异步桥接层**——后一点是本次更新新增的逻辑，4.1.3 精读时会展开。

#### 4.1.3 源码精读

先看官方最短构建路径，README 的 `Building from Source` 一节：

[README.md:200-217](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/README.md#L200-L217)

这段是仓库权威的构建入口：第 206 行列 Ubuntu 24.04 系统依赖（`libhwloc-dev`、`libclang-dev`、`protobuf-compiler` 等，缺了会报链接错误）；第 212 行建名为 `dynamo` 的 venv；第 214 行是核心命令 `cd lib/bindings/python && maturin develop --uv`——注意它**必须在 `lib/bindings/python` 目录下执行**，因为 maturin 要读那个目录的 `pyproject.toml`。

maturin 读的配置在绑定 crate 自己的 `pyproject.toml` 里：

[lib/bindings/python/pyproject.toml:48-56](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/pyproject.toml#L48-L56)

这里定义了 `module-name = "dynamo._core"`——这就是为什么 Python 侧的 import 路径是 `dynamo._core` 而不是 `_core`；`build-backend = "maturin"` 说明这个包由 maturin（而非 hatchling/pip）负责构建；`requires` 里的 `patchelf` 用于修 Linux 上扩展库的 rpath。

再看 Rust 侧，为什么这个 crate 不在 workspace 里：

[lib/bindings/python/Cargo.toml:4-10](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L4-L10)

第 4-6 行是一个**空的 `[workspace]` 节**。Cargo 规定：子目录 crate 如果自带 `[workspace]` 键，就不会向上寻找父 workspace——这是「主动退出 workspace」的标准写法。注释写明原因是 pyo3 扩展模块的构建问题。对照根 [Cargo.toml:5-41](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/Cargo.toml#L5-L41) 的 members 列表（35 项），里面只有 `lib/bindings/python/codegen`（第 39 行），没有 `lib/bindings/python` 本身。

接着是这个 crate 的产物类型定义：

[lib/bindings/python/Cargo.toml:17-22](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L17-L22)

`name = "_core"` 决定编译产物文件名（Linux 上是 `_core.abi3.so`）；`crate-type = ["cdylib", "rlib"]` 的注释解释得很清楚：`cdylib` 产出给 Python import 的共享库，`rlib` 支持仓库里的 doctest。

pyo3 依赖的两个关键 feature：

[lib/bindings/python/Cargo.toml:101-110](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L101-L110)

`extension-module` 告诉 pyo3 这是个扩展模块（不链接 `libpython.so`，避免符号冲突）；`abi3-py310` 用稳定 ABI、最低支持 Python 3.10——这与根 `pyproject.toml` 第 14 行的 `requires-python = ">=3.10"` 对齐。

最后看扩展模块的入口。`rust/lib.rs` 末尾有两个条件编译的 `#[pymodule]`：

[lib/bindings/python/rust/lib.rs:344-356](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L344-L356)

`#[pymodule]` 宏把 `fn _core` 生成成 Python 模块初始化函数。这里有两个变体：开启了 `custom-policy` feature 时走 `register_core_with_custom_worker_selection_policy`（第 345-349 行，「带自定义路由策略目录」的注册路径），默认（stock）构建走 `register_core`（第 352-356 行）。函数名 `_core` 与上面 `module-name = "dynamo._core"` 对应——这就是 `import dynamo._core` 时 Python 实际加载的那个模块。

`register_core` 的开头，是本次更新最值得注意的新增逻辑——**import 时预设 pyo3 异步桥接的运行时规模**：

[lib/bindings/python/rust/lib.rs:181-198](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L181-L198)

这段代码解决的问题：Python 与 Rust 之间的异步调用靠 `pyo3_async_runtimes` 桥接，桥接层需要一个 Tokio 运行时。以前如果某段 Python 代码（比如 `dynamo.sglang`）抢在 `DistributedRuntime` 构造之前就触发了桥接，桥接层会按 Tokio 自己的默认值自建运行时——每个 CPU 一个工作线程、512 个阻塞线程上限，`DYN_RUNTIME_*` 环境变量完全被忽略。现在模块初始化（我们的代码最早能执行的时刻）就调用 `RuntimeConfig::from_settings()` 读配置，把 `config.tokio_builder()` 交给 `pyo3_async_runtimes::tokio::init`——之后无论谁建运行时，规模都由 `DYN_RUNTIME_*` 决定。配置读取失败（第 194-197 行）只打 warning 而不让 import 失败，把报错留给有上下文的地方。

配套地，`DistributedRuntime` 构造函数也改了：先 `Worker::ensure_process_runtime()` 确保进程有配置好的运行时，再尝试把它交给桥接层；如果桥接层已经持有别的运行时，只告警「进程里有两个运行时、线程数翻倍」而不失败：

[lib/bindings/python/rust/lib.rs:1180-1207](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L1180-L1207)

这两个改动的完整语义会在 u2-l2（PyO3 绑定）和 u3-l1（Runtime 与 RuntimeConfig）展开，这里你只需要记住：**`import dynamo._core` 不只是「加载符号」，它已经决定了进程里 Tokio 运行时的线程规模**。

`register_core` 里能看到所有暴露给 Python 的类型。摘几行关键的：

[lib/bindings/python/rust/lib.rs:200-234](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L200-L234)

第 212 行注册 `make_engine` 函数（第五单元会讲它如何装配引擎）；第 219 行注册 `DistributedRuntime` 类；第 221 行 `Endpoint`、第 224 行 `Client`；第 229 行 `EntrypointArgs`、第 233-234 行 `EngineConfig`/`EngineType` 是引擎装配的参数对象。第 275 行（在 [lib.rs:275](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L275)）把编译期版本常量 `env!("CARGO_PKG_VERSION")` 挂成模块属性 `__version__`。

而 Python 包装层只是把这些再导出一次：

[lib/bindings/python/src/dynamo/runtime/__init__.py:14-18](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/runtime/__init__.py#L14-L18)

`from dynamo._core import DistributedRuntime as DistributedRuntime`——你在 Python 里写 `from dynamo.runtime import DistributedRuntime`，拿到的是同一个 Rust 类，只是多套了一层包路径。第 43-45 行（[lib/bindings/python/src/dynamo/runtime/__init__.py:43-45](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/runtime/__init__.py#L43-L45)）还展示了它如何从环境变量 `DYN_REQUEST_PLANE` / `DYN_DISCOVERY_BACKEND` 读默认值再构造 `DistributedRuntime`。

#### 4.1.4 代码实践

**实践目标**：亲手完成一次「Rust → Python 扩展」的构建，并找到那个 `.so` 产物文件。

**操作步骤**：

1. 按 [README.md:204-217](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/README.md#L204-L217) 依次执行（Ubuntu 24.04；其他发行版需自行替换等价系统包）。
2. 构建完成后，在激活的 venv 里执行：

```bash
python -c "import dynamo._core; print(dynamo._core.__version__)"
python -c "from dynamo.runtime import DistributedRuntime; print(DistributedRuntime)"
python -c "import dynamo._core as m; print(m.__file__)"
```

3. 对第 3 条命令输出的路径执行 `ls -lh`，查看该目录下的 `.so` 文件与其体积。
4. 验证完成后再执行 `python3 -m dynamo.frontend --help`，确认 frontend 也能跑（它依赖刚装的 `ai-dynamo` 层）。

**需要观察的现象**：

- 第 1 条命令应打印 `1.5.0`（来自 [lib/bindings/python/Cargo.toml:10](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L10) 的 crate 版本，经 `env!("CARGO_PKG_VERSION")` 注入）。
- 第 2 条命令应打印类似 `<class 'builtins.DistributedRuntime'>` 或 `_core.DistributedRuntime` 的类对象——证明它是 Rust 类型而非纯 Python 类。
- 第 3 条命令指向 venv 的 `site-packages/dynamo/` 目录，其中有一个 `_core.abi3.so`（文件名后缀可能因平台而异），体积通常在几十 MB 量级。

**预期结果**：三条命令都不报 `ModuleNotFoundError`，即说明 `maturin develop --uv` 成功把 Rust 代码编译并安装成了 Python 扩展。具体输出内容与 `.so` 体积**待本地验证**（取决于编译 feature 与平台）。

> 提示：若 `maturin develop` 报链接错误，优先检查系统依赖是否装全（README 第 206 行那一串），这是官方文档指出的最常见失败原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么在仓库根目录直接运行 `cargo build` 编译不到 `dynamo-py3`？

**答案**：因为 [lib/bindings/python/Cargo.toml:4-6](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L4-L6) 里写了空的 `[workspace]` 节，Cargo 会因此不再向上把它并入根 workspace（members 列表里也没有它）。它必须由 maturin 在 `lib/bindings/python` 目录下单独构建。

**练习 2**：`extension-module` 和 `abi3-py310` 这两个 pyo3 feature 各自解决什么问题？

**答案**：`extension-module` 让 pyo3 以「Python 扩展模块」方式编译——不链接 `libpython.so`，由解释器在加载时提供符号；`abi3-py310` 启用 CPython 稳定 ABI 并把最低版本定为 3.10，这样一个 `.so` 可以同时服务 3.10 及以上版本，不必每个小版本各编译一次。见 [lib/bindings/python/Cargo.toml:101-110](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L101-L110) 的注释。

**练习 3**：`from dynamo.runtime import DistributedRuntime` 和 `from dynamo._core import DistributedRuntime` 拿到的是同一个对象吗？

**答案**：是。`dynamo.runtime` 包的 `__init__.py` 里就是 `from dynamo._core import DistributedRuntime as DistributedRuntime` 的再导出，二者指向同一个 PyO3 包装的 Rust 类。

**练习 4**：为什么 `register_core` 里读运行时配置失败时只打 warning，而不是让 `import dynamo._core` 直接报错？

**答案**：源码注释（[lib/bindings/python/rust/lib.rs:192-193](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L192-L193)）写明：`Worker::ensure_process_runtime` 之后会读同一份配置，并在有上下文的地方报告错误；如果在模块初始化时硬失败，用户只会看到一个莫名其妙的 `ImportError`，拿不到任何排障线索。

### 4.2 hatch_build.py：ai-dynamo wheel 的版本注入钩子

#### 4.2.1 概念说明

装好扩展后，`uv pip install -e .` 安装根目录的 `ai-dynamo` 包。这个包用 hatchling 作为构建后端（见 [pyproject.toml:167-169](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/pyproject.toml#L167-L169)），并且挂了一个**自定义构建钩子** `hatch_build.py`。

它解决的问题很实际：`ai-dynamo` 的源码分布在 `components/src/dynamo/` 下的十几个子目录（frontend、vllm、sglang、planner、router……），每个子目录都是一个可独立 import 的包。运维排障时经常需要回答「线上跑的到底是哪个 commit 构建的」——静态版本号 `1.5.0` 不够用，需要带上 git 短 SHA。

但 `_version.py` 又不能手工维护（会忘改、会有合并冲突），所以 Dynamo 选择在**构建时自动生成**它。

#### 4.2.2 核心流程

```
uv pip install -e .  /  uv build --wheel
  └─ hatchling 启动
       ├─ 读 pyproject.toml
       │    [tool.hatch.build.hooks.custom] path = "hatch_build.py"
       │    [tool.hatch.build.targets.wheel] packages = ["components/src/dynamo"]
       ├─ 构建开始前调用钩子的 initialize(version, build_data)
       │    ├─ full_version = 元数据版本(1.5.0)
       │    ├─ subprocess 跑 `git rev-parse --short HEAD` → 7feb2b8
       │    ├─ full_version = "1.5.0+7feb2b8"
       │    └─ 遍历 components/src/dynamo/ 下每个组件目录，
       │       写入 _version.py：__version__ = "1.5.0+7feb2b8"
       └─ 正常打包（此时 _version.py 已在源码树里，会被收进 wheel）
```

伪代码化的关键决策：

- git 命令失败（比如源码不是 git checkout，比如打了 tar 包）→ 静默降级，版本号就是纯 `1.5.0`；
- 组件目录为空或不存在 → 直接抛 `RuntimeError`，让构建尽早失败而不是产出残缺包。

#### 4.2.3 源码精读

先看钩子是如何被声明的：

[pyproject.toml:167-177](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/pyproject.toml#L167-L177)

第 167-169 行指定构建后端为 hatchling；第 171-172 行注册自定义钩子，`path = "hatch_build.py"`；第 174-177 行告诉 hatchling wheel 的内容来自 `components/src/dynamo`。

顺带一提：根 `pyproject.toml` 的 sglang extra 在本次更新中升级为 `sglang[diffusion]==0.5.18`、`nixl[cu13]==1.4.0`（[pyproject.toml:78-90](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/pyproject.toml#L78-L90)）——这只影响「装了 sglang extra 的环境装什么版本」，不影响本讲的构建步骤。

钩子本体的第一部分，扫描组件目录：

[hatch_build.py:10-31](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/hatch_build.py#L10-L31)

`get_components()` 用 `os.listdir` 列出 `components/src/dynamo` 下所有非隐藏目录，返回完整路径；目录不存在（第 19-20 行）或没有任何组件（第 28-29 行）都抛 `RuntimeError`。这就是「ai-dynamo 的实际内容 = components/src/dynamo 的直接子目录」这一定义的机器可读版本。

钩子的第二部分，版本生成与写入：

[hatch_build.py:34-64](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/hatch_build.py#L34-L64)

`VersionWriterHook` 继承 hatchling 的 `BuildHookInterface`，实现 `initialize`（第 39 行）——hatchling 会在构建开始前调用它。第 44 行从元数据取基础版本；第 46-52 行用 `subprocess.run` 执行 `git rev-parse --short HEAD` 拿短 SHA，`check=True` 保证失败即抛异常；第 54-55 行拼接成 `1.5.0+7feb2b8` 这种本地版本号格式（PEP 440 的 local version segment）；第 56-57 行的 `except` 捕获 git 失败并 `pass`，实现优雅降级。第 59 行拼出文件内容（含 SPDX 版权头和 `__version__ = "..."`），第 61-64 行对每个组件目录写入 `_version.py`。

两个佐证细节：

- 该文件是生成物，不进版本库：[.gitignore:118](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/.gitignore#L118) 明确忽略 `components/**/_version.py`。
- mypy 也知道它不存在于源码树：[pyproject.toml:414-417](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/pyproject.toml#L414-L417) 对 `dynamo.*._version` 模块设置 `ignore_missing_imports`，注释写明「_version.py 在构建时生成，源码树里没有」。

顺带一提，容器里的生产构建同样会触发这个钩子：[container/templates/wheel_builder.Dockerfile:589](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/wheel_builder.Dockerfile#L589) 用 `uv build --wheel` 打根包（走 hatchling），紧接着第 590-591 行 `cd /opt/dynamo/lib/bindings/python && maturin build --release ...` 打 runtime wheel——与我们本地的两步构建完全同构，只是 feature 集更全、并做了 manylinux 修复。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `_version.py` 被构建钩子生成，并验证其中的版本号格式。

**操作步骤**：

1. 确认当前短 SHA：`git rev-parse --short HEAD`（在仓库根目录）。
2. 触发一次构建（不必真的发布）：

```bash
uv build --wheel --out-dir /tmp/dynamo-wheel-test .
```

3. 构建后查看生成文件：

```bash
cat components/src/dynamo/frontend/_version.py
cat components/src/dynamo/planner/_version.py
```

4. 解包 wheel 验证文件确实被收进去了：

```bash
cd /tmp/dynamo-wheel-test && unzip -l ai_dynamo-*.whl | grep _version | head
```

**需要观察的现象**：

- 两个不同组件目录下的 `_version.py` 内容完全一致，`__version__` 形如 `"1.5.0+<短SHA>"`。
- 拼接的短 SHA 与第 1 步 `git rev-parse --short HEAD` 的输出一致。
- wheel 文件名形如 `ai_dynamo-1.5.0+<短SHA>-py3-none-any.whl`。

**预期结果**：钩子在打包前写入 `_version.py`，且该文件被收进 wheel。文件名与版本号具体值**待本地验证**。注意：`_version.py` 已被 gitignore（[.gitignore:118](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/.gitignore#L118)），构建产生的这个文件不会被误提交。

> 补充实验（可选）：把源码目录复制到 `/tmp` 后去掉 `.git`，再执行第 2 步——此时 `git rev-parse` 失败，钩子走降级路径，`__version__` 应只有 `1.5.0` 而没有 `+SHA` 后缀。这验证了 [hatch_build.py:56-57](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/hatch_build.py#L56-L57) 的 try/except 分支。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `initialize` 里对 git 命令的失败选择 `pass` 而不是让构建报错？

**答案**：源码可能以 tar 包、导出快照等非 git 形式存在（容器构建、离线环境都会出现）。git 信息只是「锦上添花」的溯源后缀，拿不到时退回纯语义版本 `1.5.0`，包仍然完整可用；如果因此硬失败，会让所有非 git 场景都无法构建。见 [hatch_build.py:45-57](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/hatch_build.py#L45-L57)。

**练习 2**：如果新增了一个 `components/src/dynamo/mycomponent/` 目录，需要改 `hatch_build.py` 才能让它拿到 `_version.py` 吗？

**答案**：不需要。`get_components()` 是用 `os.listdir` 动态扫描 `components/src/dynamo` 的所有非隐藏子目录（[hatch_build.py:23-26](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/hatch_build.py#L23-L26)），新目录会被自动覆盖。

**练习 3**：`ai-dynamo` 与 `ai-dynamo-runtime` 两个包的版本号是如何保持一致的？

**答案**：靠人工约定同时改。根 [pyproject.toml:6](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/pyproject.toml#L6) 声明 `ai-dynamo` 为 1.5.0、第 16 行用精确约束 `ai-dynamo-runtime==1.5.0` 锁住依赖；绑定包 [lib/bindings/python/pyproject.toml:19](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/pyproject.toml#L19) 也是 1.5.0，其 Cargo.toml 第 10 行同样是 1.5.0——这正是 u1-l3 讲过的「版本四处对齐」。

### 4.3 cargo 与 Rust workspace：独立构建与测试

#### 4.3.1 概念说明

第三个最小模块是纯 Rust 侧。上一讲已经建立了 workspace 的地图，这里补上「怎么编、怎么测」。

要建立的核心认知是：**Rust 侧和 Python 扩展是两条独立的产物线**。

- `cargo build`（在仓库根）只编 workspace 的 35 个成员 crate，产出 Rust 库/二进制，放在 `target/` 下；它**不会**编 `dynamo-py3`（被排除了）。
- `maturin develop` 只编 `dynamo-py3`（它会连带把依赖的 workspace crate 一起编掉，因为 `dynamo-runtime`、`dynamo-llm` 等是它的 path 依赖），产物装进 venv。

所以「验证 Rust 侧可单独编译」和「验证 Python 扩展装好了」是两个互不替代的检查。

#### 4.3.2 核心流程

```
仓库根目录
  cargo build                     ← 编整个 workspace（35 个成员）
  cargo build -p dynamo-runtime   ← 只编 lib/runtime 这一个 crate
  cargo test                      ← 跑全部 Rust 测试
  cargo test -p dynamo-llm        ← 只测一个 crate
  cargo fmt --all && cargo clippy --workspace   ← 格式化 + lint

依赖关系（dynamo-py3 视角）：
  dynamo-py3 (不在 workspace)
    ├─ dynamo-runtime  ← lib/runtime      （workspace 成员）
    ├─ dynamo-llm      ← lib/llm          （workspace 成员）
    ├─ dynamo-kv-router← lib/kv-router    （workspace 成员）
    └─ ...
  因此 maturin develop 会顺带编译这些 crate，
  但产物组织方式（venv vs target/）完全不同。
```

macOS 注意事项（来自仓库 AGENTS.md）：`dynamo-llm` 的默认 feature 启用了 Linux/CUDA 取向的 NIXL/NUMA/`O_DIRECT` 代码，在 macOS 上应使用 `--no-default-features` 做校验，除非目标确实需要 block-manager。

#### 4.3.3 源码精读

workspace 成员清单：

[Cargo.toml:5-42](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/Cargo.toml#L5-L42)

注意第 39 行只有 `lib/bindings/python/codegen`（一个代码生成工具 crate）在 members 里，绑定 crate 本身不在。第 42 行 `resolver = "3"` 是 edition 2024 要求的新版本解析器。

workspace 级版本与本地依赖：

[Cargo.toml:44-71](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/Cargo.toml#L44-L71)

`[workspace.package]` 让所有成员默认继承 `version = "1.5.0"` 等元数据（这就是为什么各 crate 的 Cargo.toml 里写 `version.workspace = true`）；`[workspace.dependencies]` 统一管理依赖版本，例如第 59-70 行的 `dynamo-runtime`/`dynamo-llm` 等本地 path 依赖，成员引用时写 `dynamo-runtime.workspace = true` 即可，避免版本漂移。

绑定 crate 对 workspace 成员的 path 依赖（注意它不在 workspace 里，所以是「普通写法」而不是 `.workspace = true`）：

[lib/bindings/python/Cargo.toml:63-67](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L63-L67)

这里能看到 `dynamo-runtime = { path = "../../runtime" }` 等四条本地依赖。第 87-92 行还有一段值得注意的注释：因为被排除在 workspace 外，某些版本 pin（`tracing-opentelemetry`、`opentelemetry`）必须在两边重复声明，要求与根保持一致，否则会在 FFI 边界出现桥接不匹配。

`lib/runtime` 这个 crate 本身没有显式 `[lib] name`，包名即 `dynamo-runtime`：

[lib/runtime/Cargo.toml:5-8](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/Cargo.toml#L5-L8)

因此 `cargo build -p dynamo-runtime` 的产物是 `target/debug/libdynamo_runtime.rlib`（lib 目标默认名由包名派生，下划线替换连字符）。

容器模板里也留了一条「dev 容器里如何从源码构建」的注释，等于官方在镜像里复述了本讲流程：

[container/templates/wheel_builder.Dockerfile:699-705](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/wheel_builder.Dockerfile#L699-L705)

第 700 行说明 dev/local-dev 目标不带预编译 wheel 和源码；第 702-704 行给出容器内构建三步：`cargo build --features dynamo-llm/block-manager`、`cd /workspace/lib/bindings/python && maturin develop --uv`、`uv pip install --no-deps -e /workspace`。这可以当作你本机构建出问题时的「容器对照环境」。

#### 4.3.4 代码实践

**实践目标**：验证 Rust 侧可独立于 Python 构建，并分清两类产物。

**操作步骤**：

1. 在仓库根目录执行：

```bash
cargo build -p dynamo-runtime
ls -lh target/debug/libdynamo_runtime.rlib
```

2. 再跑一个最小测试集（可选，耗时）：

```bash
cargo test -p dynamo-runtime --no-run    # 只编译测试，不执行
```

3. Python 单元测试（依赖 4.1 的构建已完成）：

```bash
pytest -m unit tests/
```

4. 在笔记里回答：「`target/debug/libdynamo_runtime.rlib` 和 `site-packages/dynamo/_core.abi3.so` 各自是什么、分别被谁消费？」

**需要观察的现象**：

- 第 1 步 cargo 正常完成编译，`target/debug/` 下出现 `libdynamo_runtime.rlib`；
- 第 3 步 pytest 能收集到测试且 marker 合法（仓库使用 `--strict-markers`，未注册的 marker 会直接报错，见 [pyproject.toml:223-245](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/pyproject.toml#L223-L245)）。

**预期结果**：`.rlib` 是 Rust 静态库，消费者是**其他 Rust crate**（链接期使用）；`_core.abi3.so` 是 C 扩展动态库，消费者是 **Python 解释器**（`import dynamo._core` 时 dlopen）。编译时长与测试通过数**待本地验证**（首次全量编译可能需要较长时间）。

> macOS 用户：如需校验 `dynamo-llm`，请按仓库约定加 `--no-default-features`，否则默认 feature 里的 Linux/CUDA 取向代码不是有效的通用校验路径。

#### 4.3.5 小练习与答案

**练习 1**：`cargo build -p dynamo-runtime` 和 `maturin develop --uv` 都会编译 `lib/runtime` 的代码，这两次编译是共享缓存的吗？

**答案**：不完全共享。两者用同一份 target 目录与 cargo 编译缓存时，相同的 feature 集与编译选项可以命中缓存；但 maturin 构建的是 `dynamo-py3`，它对依赖的 feature 请求（如 [lib/bindings/python/Cargo.toml:122-126](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L122-L126) 中非 Linux 平台对 `dynamo-llm` 用 `default-features = false`）可能触发不同 feature 组合的重复编译。这就是「明明刚编过还要再编」的常见原因。

**练习 2**：为什么 `dynamo-py3` 的 Cargo.toml 里要重复声明 `tracing-opentelemetry` 的版本 pin？

**答案**：因为它退出了 workspace，拿不到 `[workspace.dependencies]` 的统一管理；而该 crate 的类型会跨 FFI 边界使用，两边版本不一致会导致 tracing-opentelemetry 桥接不匹配。注释明确要求与根保持 lockstep，见 [lib/bindings/python/Cargo.toml:87-92](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L87-L92)。

**练习 3**：如果不小心把 `lib/bindings/python` 加进了根 workspace 的 members，会发生什么？

**答案**：会与「空 `[workspace]` 节」的主动排除语义冲突，且正是仓库注释里说明要避免的情形（pyo3 扩展模块的构建问题，[lib/bindings/python/Cargo.toml:4-6](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L4-L6)）。实际操作中 cargo 会报 workspace membership 冲突错误，构建无法进行——这也是为什么不要「顺手修掉」这个看似奇怪的空节。

### 4.4 container templates：一个模板派生所有 Dockerfile

#### 4.4.1 概念说明

第四个模块是容器镜像体系。Dynamo 要发布 vLLM / SGLang / TensorRT-LLM / dynamo 四种框架、runtime / dev / local-dev / frontend / planner / wheel_builder / base 等多种 target、cuda / xpu / cpu 三种设备、amd64 / arm64 两种架构——如果每种组合手写一份 Dockerfile，会有几十份高度重复、极易漂移的文件。

仓库的解法是**单模板 + 渲染器**：

- `container/Dockerfile.template` 是唯一的 Jinja2 主模板，用 `{% if framework == ... %}` 和 `{% include %}` 组合子模板；
- `container/render.py` 是渲染器 CLI，负责解析参数、**校验组合合法性**、从 `context.yaml` 取版本号等上下文，最后写出 rendered Dockerfile。

理解这套体系后，你能回答「生产镜像里的 wheel 是怎么编出来的」——答案就写在 `wheel_builder.Dockerfile` 模板里，命令与我们本机构建的命令同构。

#### 4.4.2 核心流程

```
python container/render.py --framework dynamo --target local-dev ...
  ├─ parse_platform()      把 linux/amd64 → "amd64"，多架构 → "multi"
  ├─ validate_args()       查表校验 (framework, device, target, cuda_version) 组合
  ├─ 读 container/context.yaml   版本号、基础镜像等上下文
  ├─ _render_context()     组装 Jinja2 变量（device_key、合规扫描参数等）
  ├─ env.get_template("Dockerfile.template").render(...)
  ├─ _inject_python_index_mounts()  给每个装包的 RUN 注入 PyPI secret 挂载
  └─ 写出 <framework>-<target>-<device><cuda>-<arch>-rendered.Dockerfile
```

主模板的分发逻辑则是一棵决策树：

```
framework != dynamo（vllm/sglang/trtllm）→ dynamo_base + wheel_builder + 框架自己的 runtime 模板
framework == dynamo：
   target == frontend      → dynamo_base + wheel_builder + frontend
   target == planner       → dynamo_base + wheel_builder + planner
   target ∈ {runtime,dev,local-dev} → dynamo_base + wheel_builder + dynamo_runtime
   target == wheel_builder → dynamo_base + wheel_builder
   target == base          → dynamo_base
dev/local-dev 额外叠 dev.Dockerfile（local-dev 再叠 local_dev.Dockerfile）
```

#### 4.4.3 源码精读

主模板全文只有 70 行左右，全部是 include 与条件：

[container/Dockerfile.template:5-30](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/Dockerfile.template#L5-L30)

第 5 行先 include `args.Dockerfile`（公共构建参数）；第 8-30 行是 framework/target 的分发核心——注意第 11 行 `{% elif framework == "dynamo" %}` 分支下按 target 精确组合子模板，例如 `frontend` 目标（第 12-15 行）= dynamo_base + wheel_builder + frontend 三段。开发镜像在第 62-72 行叠加：

[container/Dockerfile.template:61-72](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/Dockerfile.template#L61-L72)

`dev` 与 `local-dev` 都会 include `dev.Dockerfile`；`local-dev` 额外叠加 `local_dev.Dockerfile`（第 67-69 行）。这解释了「dev 容器里还要自己 maturin develop」的原因：这些 target 的镜像不带预编译产物（见 4.3.3 引用的 [container/templates/wheel_builder.Dockerfile:699-705](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/wheel_builder.Dockerfile#L699-L705)）。

渲染器的参数与校验：

[container/render.py:59-117](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/render.py#L59-L117)

`--framework` 限定四个取值（第 69 行 choices），`--device` 三个（第 75 行），`--target` 是自由字符串但随后会被查表（第 82 行），`--cuda-version` 限定 13.0/13.1（第 99 行 choices）。

[container/render.py:120-194](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/render.py#L120-L194)

`validate_args` 用一张 `valid_inputs` 字典表达所有合法组合——例如 `dynamo` 框架（第 155-167 行）允许 runtime/dev/local-dev/frontend/planner/wheel_builder/base 七种 target，但只允许 cuda 设备。这相当于把「镜像矩阵」变成了一份可执行规格，非法组合在渲染期就被拒绝，而不是在 40 分钟的 Docker 构建中途失败。

渲染与输出：

[container/render.py:307-330](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/render.py#L307-L330)

第 309-310 行加载模板并渲染（Jinja2 环境在第 197-203 行用 `StrictUndefined` 创建——模板里引用未定义变量会直接报错，防止静默产出坏 Dockerfile）；第 312 行把 3 个以上连续换行压成 2 个；第 313 行注入 PyPI secret 挂载；第 318 行拼出文件名。`main` 的收尾在 [container/render.py:333-355](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/render.py#L333-L355)：第 343-344 行读 `context.yaml`，第 348-354 行对 local-dev 提醒传入 `USER_UID`/`USER_GID` 构建参数（让容器用户与宿主 UID 对齐，避免挂载卷权限问题）。

生产 wheel 的真实构建命令（与本讲 4.1/4.2 的本地命令对照）：

[container/templates/wheel_builder.Dockerfile:588-617](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/wheel_builder.Dockerfile#L588-L617)

第 589 行 `uv build --wheel` 打根包（触发 hatch_build.py 钩子）；第 590 行进入绑定目录；第 615 行 `maturin build --release --features "kv-indexer,slot-tracker,select-service,mm-routing,aic-forward-pass,request-trace-s3" --out /opt/dynamo/dist`——即生产 wheel 比本地 `maturin develop` 多开了一批 feature（对应 [lib/bindings/python/Cargo.toml:24-53](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L24-L53) 的 feature 表）。这也解释了「本地构建能跑、生产镜像多出 kv-indexer 等能力」的差异来源。

顺带留意：渲染上下文 `context.yaml` 也随本次更新把 SGLang 运行镜像 tag 升到 `v0.5.18-cu130-runtime`、NIXL 引用升到 `v1.4.0`（[container/context.yaml:112-142](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/context.yaml#L112-L142)），与根 `pyproject.toml` 的 sglang extra 升级保持联动——这是「Python 依赖版本与容器基础镜像版本对齐」的实例。

#### 4.4.4 代码实践

**实践目标**：零成本渲染一份 Dockerfile，读懂模板组合逻辑（不需要 Docker，也不需要 GPU）。

**操作步骤**：

1. 确认渲染依赖可用（jinja2、pyyaml）：`uv pip install jinja2 pyyaml`。
2. 渲染一个 local-dev 镜像的 Dockerfile 并打印到屏幕：

```bash
python container/render.py --framework dynamo --target local-dev --show-result \
    --output-short-filename
```

3. 阅读屏幕输出（或生成的 `container/rendered.Dockerfile`），找三类东西：
   - `=== BEGIN templates/xxx.Dockerfile ===` 风格的分段注释，数一数一共 include 了几个子模板；
   - 与 `maturin`、`uv pip` 相关的行；
   - 结尾是否有 `USER_UID` / `USER_GID` 相关的 `ARG`。
4. 再渲染一个不存在的组合，观察校验行为：

```bash
python container/render.py --framework dynamo --target frontend --device cpu
```

**需要观察的现象**：

- 第 2 步应输出完整 Dockerfile，末尾打印 `INFO: Generated Dockerfile written to ...`，并额外打印两行 local-dev 的 UID/GID 提醒（对应 [container/render.py:348-354](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/render.py#L348-L354)）；
- 第 4 步应抛 `ValueError: Invalid input combination ...`——因为 dynamo 框架只允许 cuda 设备（[container/render.py:155-167](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/render.py#L155-L167)）。

**预期结果**：渲染成功生成 `rendered.Dockerfile`（已被 gitignore，[.gitignore:20-21](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/.gitignore#L20-L21)），非法组合被渲染期校验拦截。生成的文件名与分段数量**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Dockerfile.template` 里 dev/local-dev 分支没有把 wheel_builder 的产物带进镜像？

**答案**：第 700-705 行的注释说明 dev/local-dev 目标不带预编译 wheel 和源码，进入容器后需要按注释给的三条命令从源码构建（`cargo build` + `maturin develop --uv` + `uv pip install --no-deps -e /workspace`）。开发镜像的定位是「带齐工具链的空环境」，让开发者编译自己工作区里的代码，而不是固化一份可能已过期的 wheel。

**练习 2**：`_render_context` 里为什么要单独计算 `device_key` 而不在模板里 `{% set %}`？

**答案**：注释写明 Jinja2 的默认作用域规则下，被 include 文件里的 `{% set %}` 不会传播到同级其他 include；所以在 Python 侧算好（cuda 设备时为 `cuda + 版本号`，如 `cuda13.0`；非 cuda 时就是设备名）再传入上下文，所有子模板都能看到。见 [container/render.py:227-244](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/render.py#L227-L244)。

**练习 3**：本地 `maturin develop --uv` 与容器里 `maturin build --release --features ...` 产出的 wheel 有什么本质差别？

**答案**：功能集合与发布形态都不同。本地 develop 默认 feature 为空（[lib/bindings/python/Cargo.toml:25](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/Cargo.toml#L25) `default = []`），直接装进 venv 供调试；容器构建用 `--release` 并显式开启 kv-indexer、mm-routing、request-trace-s3 等一批 feature（[container/templates/wheel_builder.Dockerfile:615](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/wheel_builder.Dockerfile#L615)），产出 manylinux 修复过的可分发 wheel。排查「本地有/镜像没有（或相反）的行为」时应首先对比 feature 列表。

### 4.5 container deps overrides：把镜像依赖「钉」在期望版本上

> 本节对应本次更新新增的 [container/deps/overrides.frontend.txt](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/overrides.frontend.txt)。

#### 4.5.1 概念说明

容器镜像里的 Python 依赖不是「一个 requirements 文件装到底」。`container/deps/` 下按组件拆了一组文件（`requirements.common.txt`、`requirements.frontend.txt`、`requirements.planner.txt`、`requirements.benchmark.txt`……），每个镜像只装自己需要的组合，而且一个镜像里往往有**多次** `uv pip install`（先装公共依赖，再装 wheelhouse 里的自产 wheel，最后装 benchmarks/ 这类工具）。

这就带来一个微妙的问题：**每次 `uv pip install` 都是一次独立的依赖解析，而 requirements 文件里的版本要求只是「约束」，不是「裁决」**。如果后面某次安装引入的传递依赖对同一个包声明了更窄的要求，uv 重新解析时可能把已经装好的新版本**降回去**。

本次更新正是这样一个真实案例：frontend 镜像最后一步安装 `benchmarks/`，它依赖 `aiperf==0.10.0`，而 aiperf 声明了 `pillow~=12.2.0`——这个约束钉死了 patch 系列，等于禁止 `requirements.common.txt` 已经抬到的 `12.3.0` 地板，于是重新解析把镜像拉回 `pillow 12.2.0`。

解法是 uv 的 **override 机制**：`--overrides <file>` 里的条目会**替换**掉该包在本次解析中的所有其他约束，并对整个解析全程生效。这就是新增 `overrides.frontend.txt` 的动机。

#### 4.5.2 核心流程

frontend 镜像的依赖安装共三步，每一步都要带上同一份 override：

```
frontend.Dockerfile 的三次 uv pip install：
  ① requirements.common.txt + requirements.frontend.txt     （公共 + 前端依赖）
  ② wheelhouse 里的 ai_dynamo_runtime*.whl + ai_dynamo*.whl （自产 wheel）
  ③ cd /workspace/benchmarks && uv pip install .              （装入 aiperf —— 冲突源头）

冲突产生机制（无 override 时）：
  ① 装 pillow 12.3.0（requirements.common.txt 的地板）
  ③ aiperf==0.10.0 声明 pillow~=12.2.0（只允许 12.2.x）
       → 重新解析：pillow 12.2.0 ── 地板被传递依赖击穿

加 --overrides overrides.frontend.txt 后：
  override 条目 pillow>=12.3.0,<13 「替换」所有其他约束
       → ③ 的解析也必须落在 [12.3.0, 13) ── 地板守住
```

关键设计点（也是两个文件注释里反复强调的）：

- **override 必须作用于每一次 install**，所以三处 `uv pip install` 都挂了 `--overrides /tmp/overrides.frontend.txt`；
- **上界 `<13` 不是装饰**：override 生效时它会替换掉 pyproject/requirements 里原有的所有上界约束，如果只写 `pillow>=12.3.0`，这个镜像就成了全仓库唯一可能接受 13.x 的地方；
- 文件注释里明确写了「一旦 aiperf 放宽自己的约束就删掉这条」——override 是债，要还。

#### 4.5.3 源码精读

先看 deps 体系的总纲——每个镜像按组件拆文件、以及「为什么绝不用裸 `>=`」的版本固定策略：

[container/deps/README.md:18-26](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/README.md#L18-L26)

纯 Python 且经过充分测试的包用 `==` 精确固定；可能有平台差异构建的包用 `<=`/`<`；**禁止裸 `>=`**——它会放进未测试过的未来版本，破坏可复现性。理解这条策略，才能理解为什么 override 文件里写的都是「区间」（`>=12.3.0,<13`）而不是单点。

本次新增的 frontend override 文件，注释把整个因果链讲得一清二楚：

[container/deps/overrides.frontend.txt:1-20](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/overrides.frontend.txt#L1-L20)

第 6-12 行解释冲突来源（benchmarks → aiperf==0.10.0 → `pillow~=12.2.0` 击穿 12.3.0 地板，已发布的 1.4.1 frontend 正是因为这个才带着 pillow 12.2.0）；第 14 行点出 override 与 requirements 的本质区别——「override 作用于整个解析，能贯穿后续所有安装步骤，requirements 的地板做不到」；第 16-18 行强调上界是必需的，并写明删除条件（aiperf 放宽约束后删掉这条）。第 20 行就是唯一的有效条目：`pillow>=12.3.0,<13`。

再看模板侧怎么用它。frontend 镜像的第一次安装（挂载 + 传参）：

[container/templates/frontend.Dockerfile:143-151](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/frontend.Dockerfile#L143-L151)

第 145 行把 `overrides.frontend.txt` bind-mount 进构建容器，第 149 行 `--overrides /tmp/overrides.frontend.txt` 与两个 requirements 并列传给 `uv pip install`。

第二次安装（装自产 wheel）同样要带，否则这次解析又可能把 pillow 拉回去：

[container/templates/frontend.Dockerfile:158-165](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/frontend.Dockerfile#L158-L165)

第三次（冲突源头 benchmarks/ 的安装）更必须带：

[container/templates/frontend.Dockerfile:182](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/frontend.Dockerfile#L182)

planner 镜像走同一套机制，只是文件不同（它先前的 aiohttp 案例与本次新增的 pillow 案例并存）：

[container/deps/overrides.planner.txt:22-32](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/overrides.planner.txt#L22-L32)

第 22-31 行的注释说明 planner 因为「跑 aiperf 的 thorough profiler 路径」而引入同样的 pillow 冲突；第 32 行是条目本体。planner 模板中的用法在 [container/templates/planner.Dockerfile:70-81](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/planner.Dockerfile#L70-L81)——注意它只有一次 `uv pip install`（requirements + wheelhouse 一次性装完），所以只需挂载一处。

#### 4.5.4 代码实践

**实践目标**：搞清楚「为什么同一份 override 要挂在三次 `uv pip install` 上」，并亲手验证 override 对解析结果的影响。

**操作步骤**：

1. 通读 [container/deps/overrides.frontend.txt](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/overrides.frontend.txt)（20 行，其中 15 行是注释），用自己的话写下：aiperf 是怎么把 pillow 拉回 12.2.0 的？
2. 在 `container/templates/frontend.Dockerfile` 里 grep 三处 `--overrides`，确认每一次 `uv pip install`（requirements、wheelhouse、benchmarks）都带了同一份文件，并回答：如果只在第①步带，会发生什么？
3. （动手实验，需要网络访问 PyPI）在临时目录里做一个最小复现：

```bash
mkdir -p /tmp/uv-override-lab && cd /tmp/uv-override-lab
cat > reqs.txt <<'EOF'
pillow>=12.3.0
EOF
cat > overrides.txt <<'EOF'
pillow>=12.3.0,<13
EOF
# 先看没有 override、再叠加一个模拟 aiperf 的窄约束：
uv pip compile --python-version 3.12 reqs.txt -o /dev/stdout
printf 'pillow~=12.2.0\n' >> reqs.txt
uv pip compile --python-version 3.12 reqs.txt -o /dev/stdout          # 观察冲突/回退
uv pip compile --python-version 3.12 reqs.txt --override overrides.txt -o /dev/stdout
```

**需要观察的现象**：

- 第 2 步应看到 `--overrides /tmp/overrides.frontend.txt` 出现在 [frontend.Dockerfile:149](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/frontend.Dockerfile#L149)、[frontend.Dockerfile:163](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/frontend.Dockerfile#L163)、[frontend.Dockerfile:182](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/templates/frontend.Dockerfile#L182) 三处。
- 第 3 步第二次 compile（`pillow>=12.3.0` 与 `pillow~=12.2.0` 同时在场且无 override）会报告无解或选出 12.2.x；第三次加上 `--override` 后应稳定解析出 `pillow 12.3.x`。具体行为**待本地验证**（取决于 uv 版本与 PyPI 上可用的 wheel）。

**预期结果**：你能向别人解释清楚 requirements 约束与 uv override 的区别——前者参与「协商」，可能被传递依赖顶掉；后者是「替换」，对整个解析一票决定。这也回答了为什么仓库要用单独的 `overrides.*.txt` 文件而不是把版本写得更死塞进 requirements。

#### 4.5.5 小练习与答案

**练习 1**：既然 `requirements.common.txt` 已经写了 pillow 的地板，为什么还需要 override？

**答案**：requirements 里的约束只在那一次解析中参与协商。frontend 镜像有三步安装，第三步装入的 aiperf 声明 `pillow~=12.2.0`，它与地板 `>=12.3.0` 冲突时，重新解析会让步回 12.2.0——地板被击穿。override 会**替换**该包的所有约束并对整个解析生效，所以能贯穿后续安装步骤。见 [container/deps/overrides.frontend.txt:13-15](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/overrides.frontend.txt#L13-L15) 的注释。

**练习 2**：为什么 override 条目 `pillow>=12.3.0,<13` 的上界 `<13` 是「必需的，不是装饰」？

**答案**：uv 的 override 生效时会替换该包的所有其他约束——包括 pyproject 和 requirements 里原本承担封顶职责的上界。如果 override 只写 `>=12.3.0`，那么一旦 override 在场，就没有任何东西阻止 13.x 被解析进来，这个镜像就成了全仓库唯一可能接受 pillow 13 的地方。两个 overrides 文件的注释都专门强调了这一点（[container/deps/overrides.frontend.txt:16-18](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/overrides.frontend.txt#L16-L18)）。

**练习 3**：frontend 和 planner 为什么用两份独立的 overrides 文件，而不是合用一份？

**答案**：每个镜像的依赖集合不同：planner 装 aiperf 走的是 thorough profiler 路径，它需要的 aiohttp 固定对 frontend 没有意义；反过来 frontend 有 benchmarks/ 的三步安装序列。deps 体系的设计原则就是「按组件拆分，每个镜像只装自己需要的」，overrides 作为依赖体系的一部分遵循同样的拆分（对照 [container/deps/README.md:5-16](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/README.md#L5-L16) 的文件表）。

## 5. 综合实践

**综合任务：完成一次全链路源码构建，并产出一份「构建产物清单」。**

1. **准备**：按 4.1 完成 README 的六步构建；按 4.3 跑通 `cargo build -p dynamo-runtime`；按 4.4 渲染一份 local-dev Dockerfile。
2. **验证 Python 层**：

```bash
python - <<'EOF'
import dynamo._core
from dynamo.runtime import DistributedRuntime
print("_core version :", dynamo._core.__version__)
print("_core path    :", dynamo._core.__file__)
print("DistributedRuntime:", DistributedRuntime)
EOF
```

3. **验证版本注入**：按 4.2 的实践触发 `uv build --wheel`，确认 `components/src/dynamo/frontend/_version.py` 的 `__version__` 为 `1.5.0+<短SHA>`。
4. **验证 Rust 层**：`cargo build -p dynamo-runtime` 后 `ls target/debug/libdynamo_runtime.rlib`。
5. **验证依赖固定**：按 4.5 的实践，在渲染出的 frontend Dockerfile（可用 `python container/render.py --framework dynamo --target frontend` 生成，或直接读模板）里找到三处 `--overrides`，并在笔记里写下一句话结论——「如果删掉第三处会发生什么」。
6. **冒烟**：`python3 -m dynamo.frontend --help` 能打印帮助（这是 AGENTS.md 给出的构建验证命令）。
7. **产出**：写一张三列表格——「命令 / 产物路径 / 产物的消费者」，至少覆盖：`_core.abi3.so`、`ai_dynamo-*.whl`（或可编辑安装）、`libdynamo_runtime.rlib`、`rendered.Dockerfile`。这张表就是你后续调试「改了 Rust 没生效」「装的到底是不是我的构建」这类问题时最先翻开的备忘录。

所有步骤的具体输出**待本地验证**；若在某一步失败，优先核对：系统依赖是否齐全（README 第 206 行）、是否在正确目录执行 `maturin develop`、venv 是否处于激活状态。

## 6. 本讲小结

- Dynamo 的构建是**两条产物线**：maturin 把被排除出 workspace 的 `dynamo-py3` 编成 Python 扩展 `dynamo/_core.abi3.so`（装进 venv），cargo 把 35 个 workspace 成员编成 `target/` 下的 Rust 库；两者互不替代。
- `dynamo._core` 模块由 `rust/lib.rs` 里的 `#[pymodule] fn _core` 定义，`register_core` 注册了 `DistributedRuntime`、`make_engine`、`EngineConfig` 等全部 Python 可见类型；`dynamo.runtime` 只是它的再导出包装层。**import 即初始化**：模块加载时就会读 `DYN_RUNTIME_*` 配置并预设 pyo3 异步桥接的 Tokio 运行时规模（本次更新新增）。
- `hatch_build.py` 是 hatchling 构建钩子：打包前扫描 `components/src/dynamo` 的全部子目录，为每个组件写入带 git 短 SHA 的 `_version.py`（`1.5.0+<sha>`），git 不可用时优雅降级为纯版本号。
- `container/render.py` + `Dockerfile.template` 用单模板派生全部镜像变体，`validate_args` 把合法组合变成可执行规格；生产 wheel 的构建命令（`uv build --wheel` + `maturin build --release --features ...`）与本地流程同构，只是 feature 更全。
- 镜像内的 Python 依赖由 `container/deps/` 的 requirements 按组件拆分供给，而 uv `--overrides`（本次新增 `overrides.frontend.txt`，planner 同机制）负责把「被传递依赖击穿的地板」钉回去——override 替换所有其他约束、贯穿整个解析，因此上界必须自带，且每一步 `uv pip install` 都要挂上。

## 7. 下一步学习建议

你已经拥有了一个可构建、可 import 的开发环境。下一讲进入 **u2-l1（Hello World：用 dynamo.runtime 写最小 worker）**：用刚构建出的 `DistributedRuntime` 写第一个 worker，跑通 endpoint 注册与 Client 调用。

如果想在构建体系上再深入，建议按这个顺序阅读源码：

1. [lib/bindings/python/rust/lib.rs:175-280](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L175-L280) —— 完整读一遍 `register_core`，数一数暴露了多少类，这会成为你后续学 Python API 的「目录页」；特别留意开头那段「预设桥接运行时」的注释，它是 u2-l2 的伏笔。
2. [container/context.yaml](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/context.yaml) —— 看渲染上下文里都装了哪些版本号，理解镜像的可配置维度。
3. [container/deps/](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/container/deps/README.md) 目录 —— 通读 README 与两个 overrides 文件的注释，它们是「依赖解析实战教科书」级别的注释样本。
4. [docs/fern/pages/developer-guide/advanced-customizations/building-from-source.md](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/docs/fern/pages/developer-guide/advanced-customizations/building-from-source.md) —— 官方完整构建指南，含故障排查（maturin 链接错误、cargo clean 等），可作为本讲的查阅手册。
