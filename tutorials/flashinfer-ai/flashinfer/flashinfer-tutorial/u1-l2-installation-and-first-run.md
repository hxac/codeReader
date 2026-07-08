# 安装与首次运行

## 1. 本讲目标

上一篇（u1-l1）我们认识了 FlashInfer 是什么、能做什么。本篇解决一个更落地的问题：**怎么把它装到机器上，并确认它真的能用。**

学完本篇，你应当能够：

- 说清 `flashinfer-python`、`flashinfer-cubin`、`flashinfer-jit-cache` 这三个包各自的作用与差别，知道在不同场景下该装哪一个。
- 用「源码 + editable 模式」完成开发环境安装，并理解 `--recursive` 子模块、`--no-build-isolation` 这两个关键标志背后的原因。
- 读懂 `build_backend.py` 这个自定义构建后端在安装时到底做了哪几件事（写版本元数据、铺好 `flashinfer/data` 目录）。
- 用 `flashinfer show-config` 验证安装，并能从输出里指出 JIT 工作区目录和 cubin 目录分别指向哪里。

本篇只读项目级文件（`README.md`、`pyproject.toml`、`build_backend.py`、`version.txt`）以及配套的 `flashinfer/__main__.py`、`flashinfer/jit/env.py`，**不进入任何具体 kernel 代码**。具体的 kernel 调用与 JIT 编译细节留到后续单元。

## 2. 前置知识

在动手之前，先建立几个直觉。

### 2.1 Python 包的「构建后端」是什么

你在终端敲下 `pip install xxx` 时，pip 并不是自己直接把源码搬进 site-packages，而是先读取项目根目录的 `pyproject.toml` 里的 `[build-system]` 段，找到它声明使用的**构建后端（build backend）**，然后把控制权交给这个后端。后端负责：生成 wheel、生成元数据、处理 editable（可编辑）安装等。

绝大多数普通 Python 项目用的是 setuptools 自带的后端（`setuptools.build_meta`）。FlashInfer 也基于 setuptools，但它把 setuptools **包了一层**，做成了一个名为 `build_backend` 的自定义后端。原因稍后会讲——它需要在打包时执行一些额外动作（写版本号、铺设 JIT 需要的源码目录）。

> 术语：**editable 安装**（`pip install -e .`）指把包以「软链接 / 符号引用」的方式装进环境，源码改动立即生效，无需重新安装。这对 FlashInfer 这种「改了 kernel 源码就希望下次运行自动重编译」的开发循环至关重要。

### 2.2 为什么 FlashInfer 安装比普通库「讲究」

普通库：装完就能 `import`。FlashInfer：装完只是装了一个「kernel 生成器」，真正的 GPU 代码（`.so`）要等到**第一次调用某个 API 时才按需编译（JIT）**。这带来两个推论：

1. 安装时必须把 kernel 的**源码**（`csrc/`、`include/`）以及第三方头文件库（`3rdparty/cutlass` 等）放到一个能被运行时找到的位置——这就是 `flashinfer/data` 目录。
2. 首次运行会比较慢（在编译），之后会被缓存。为了缓解「首次慢」和「离线可用」问题，FlashInfer 又提供了两个可选包（cubin、jit-cache）。

理解了这两点，本篇后面的所有细节就都串起来了。

### 2.3 PEP 517 / 518 与 build isolation

- **PEP 518** 规定用 `pyproject.toml` 的 `[build-system].requires` 声明构建依赖。
- **PEP 517** 规定了构建后端的一组标准钩子函数名，如 `build_wheel`、`build_editable`、`prepare_metadata_for_build_editable` 等。
- **build isolation（构建隔离）**：默认情况下，pip 会新建一个干净的临时环境，只装 `[build-system].requires` 里声明的依赖，再在里面执行构建。这能避免「构建依赖污染用户环境」，但对 FlashInfer 这类需要读用户环境里 PyTorch/CUDA 的项目反而是麻烦——所以它推荐 `--no-build-isolation`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md) | 安装命令、三种包选项、源码安装、验证命令的「官方说法」 |
| [pyproject.toml](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml) | 声明包名、动态版本、可选依赖、`flashinfer` 命令入口、构建后端 |
| [build_backend.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py) | 自定义 PEP 517 构建后端：写版本元数据、铺设 `flashinfer/data` 目录 |
| [version.txt](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/version.txt) | 单行纯文本，存放当前版本号 |
| [flashinfer/__main__.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py) | `flashinfer` CLI 入口，`show-config` 子命令的实现 |
| [flashinfer/jit/env.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py) | 定义 JIT 工作区、cubin 目录、AOT 目录等所有运行时路径 |

## 4. 核心概念与源码讲解

按「最小模块」拆成三块：**4.1 包选项**、**4.2 源码安装与子模块**、**4.3 构建后端**，最后补一节 **4.4 验证安装** 作为收尾。

---

### 4.1 包选项：flashinfer-python / cubin / jit-cache

#### 4.1.1 概念说明

FlashInfer 官方提供**三个**相关 pip 包。它们不是「同一个包的不同名字」，而是分工不同的协作包：

| 包 | 一句话定位 | 谁需要 |
|----|-----------|--------|
| **flashinfer-python** | 核心包。提供全部 Python API；kernel 在**首次调用时按需 JIT 编译或下载** | 所有用户必装 |
| **flashinfer-cubin** | 预编译好的 kernel 二进制（cubin），覆盖**所有受支持架构** | 想离线运行 / 想跳过编译的人可选 |
| **flashinfer-jit-cache** | 预编译好的 **JIT 缓存**（`.so`），按 **CUDA 版本**分发 | 想让启动更快的人可选 |

为什么需要后两个？因为默认的 `flashinfer-python` 走 JIT：第一次调用某算子时，要先在现场生成 CUDA 源码、用 nvcc 编译、链接成 `.so`、加载——这一过程对冷启动不友好，而且在无网络/无 nvcc 的环境里行不通。cubin 和 jit-cache 把这些成果**提前做好**分发，安装后直接命中，跳过编译。

#### 4.1.2 核心流程

三种包在运行时的优先级（简化版）：

```text
调用某个 FlashInfer API
        │
        ▼
需要某个 kernel 的 .so
        │
   ┌────┴─────────────────────────────┐
   ▼                                  ▼
是否已有 JIT 缓存(.so)？           是否有 cubin（二进制）？
(FLASHINFER_JIT_DIR)              (flashinfer-cubin / FLASHINFER_CUBIN_DIR)
   │                                  │
 有→直接加载                         有→直接加载
 无→现场 JIT 编译并缓存               无→现场 JIT 编译并缓存
```

也就是说：**JIT 是兜底机制**，cubin/jit-cache 是加速机制。三者可以同时装，也可以只装核心包。

#### 4.1.3 源码精读

README 里对三个包有明确说明（[README.md:L95-L99](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L95-L99)），这是包选项的「权威定义」：

> 这段列出了三个包的名称和各自职责：核心包首次使用时编译/下载 kernel；cubin 是全架构预编译二进制；jit-cache 是针对特定 CUDA 版本的预编译缓存。

README 进一步给出「为更快启动 / 离线使用」同时安装核心包与可选包的命令（[README.md:L101-L107](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L101-L107)），其中 jit-cache 需要按你的 CUDA 版本选择 index（如 `cu129`）。

包名本身在 `pyproject.toml` 里锁定（[pyproject.toml:L15-L18](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L15-L18)）：核心包名是 `flashinfer-python`，描述为 `FlashInfer: Kernel Library for LLM Serving`。

运行时如何判断「cubin 包/jit-cache 包是否已安装」？答案在 `flashinfer/jit/env.py` 里用 `importlib.util.find_spec` 探测（[flashinfer/jit/env.py:L27-L48](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L27-L48)）。这两个函数 `has_flashinfer_jit_cache()` / `has_flashinfer_cubin()` 是后面决定目录指向哪里的关键。

#### 4.1.4 代码实践

**实践目标**：在自己机器上确认当前装了哪几个包。

**操作步骤**：

1. 终端执行（不需要 GPU）：

   ```bash
   pip list 2>/dev/null | grep -i flashinfer
   ```

2. 也可以直接在 Python 里探测（模拟库内部行为）：

   ```python
   import importlib.util
   for name in ("flashinfer", "flashinfer_cubin", "flashinfer_jit_cache"):
       print(name, "→", importlib.util.find_spec(name) is not None)
   ```

**需要观察的现象**：

- 至少能看到 `flashinfer-python`（import 名是 `flashinfer`）。
- `flashinfer_cubin` / `flashinfer_jit_cache` 多半为 `False`（除非你专门装过）。

**预期结果**：你会直观看到「核心包几乎总在，可选包默认不在」，从而理解为什么默认安装后第一次调用 kernel 会触发编译。

> 如果当前环境没装 flashinfer，相关行为即为「待本地验证」——重点是理解探测逻辑，而不是一定要跑出 `True`。

#### 4.1.5 小练习与答案

**练习 1**：假如你的服务器没有外网、也没有 nvcc，只装了 `flashinfer-python`，调用一个 attention 算子会发生什么？

**参考答案**：JIT 编译既需要生成源码（离线可做）也需要 nvcc 编译（无 nvcc 则失败），所以大概率报错。正确做法是预装 `flashinfer-cubin`（纯二进制，不需要编译器），让运行时直接命中 cubin。

**练习 2**：`flashinfer-jit-cache` 的 wheel 名字里常带 `+cu129` 这样的后缀，为什么它要按 CUDA 版本分发，而 cubin 不强调这一点？

**参考答案**：jit-cache 里是编译好的 `.so`，`.so` 与编译时链接的 CUDA runtime 版本绑定，所以按 CUDA 版本区分；cubin 是更底层的 GPU 指令二进制（与驱动/CUDA runtime 解耦程度更高），覆盖的是 GPU 架构维度，所以强调「全架构」而非 CUDA 版本。

---

### 4.2 源码安装与 --recursive 子模块

#### 4.2.1 概念说明

对开发者来说，标准安装方式是从 GitHub 克隆源码后 editable 安装。这里有两个**必须理解**的标志：

1. **`--recursive`**：FlashInfer 用 git submodule 管理三个重量级第三方依赖（`3rdparty/cutlass`、`3rdparty/spdlog`、`3rdparty/cccl`）。`cutlass` 提供 CUDA 模板（GEMM/tensor core 等），没有它 kernel 根本编译不过。`--recursive` 在克隆时一并拉取这些子模块。
2. **`--no-build-isolation`**：关闭 pip 默认的「构建隔离」，让构建后端能直接看到你当前环境里已经装好的 PyTorch、CUDA 等。

#### 4.2.2 核心流程

```text
git clone --recursive   # 拉源码 + 3rdparty 子模块
       │
       ▼
pip install --no-build-isolation -e . -v
       │
       ▼
pip 读取 pyproject.toml [build-system] → 用 build_backend 后端
       │
       ▼
build_backend 的 build_editable() 钩子被调用：
   1) 写 flashinfer/_build_meta.py（版本号 + git commit）
   2) 用软链接铺设 flashinfer/data/{csrc,include,cutlass,spdlog,cccl}
       │
       ▼
editable wheel 装入环境，import flashinfer 可用
```

为什么 editable 模式用**软链接**而不是拷贝？因为 editable 的精髓就是「源码改了立即生效」。JIT 编译时读的是 `flashinfer/data/csrc` 与 `flashinfer/data/include`，用软链接指回你工作树里的真实文件，你改一行 `.cuh`，下次 JIT 编译读到的就是新内容。

#### 4.2.3 源码精读

README 给出的源码安装与 editable 命令（[README.md:L137-L149](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L137-L149)）：`git clone ... --recursive` 之后，开发用 `python -m pip install --no-build-isolation -e . -v`。

README 还有一段重要 Note 解释 `--no-build-isolation` 的副作用（[README.md:L151-L154](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md#L151-L154)）：关闭隔离后，pip 不会自动装构建依赖，FlashInfer 要求 `setuptools>=77`；若报 `AttributeError: module 'setuptools.build_meta' has no attribute 'prepare_metadata_for_build_editable'`，需先 `pip install --upgrade pip setuptools`。这正好对应 `pyproject.toml` 里 `requires = ["setuptools>=77", ...]`（[pyproject.toml:L42-L45](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L42-L45)）。

> 为什么非得 `--no-build-isolation`？CLAUDE.md 的一句话点明了主因：**「The `--no-build-isolation` flag prevents pip from pulling incompatible PyTorch/CUDA versions from PyPI.」**——隔离环境会从 PyPI 拉一份可能与本机 CUDA 不匹配的 PyTorch，破坏后续编译。此外，`build_backend.py` 内部还有第二层原因：构建钩子要在用户环境里安装 moe_ep（NIXL-EP）相关 wheel，隔离环境里这些 wheel 装完即丢、根本到不了用户环境（见 [build_backend.py:L107-L119](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L107-L119) 的注释与 `_in_isolated_build_env` 判断）。

子模块在打包配置里被显式声明为包数据目录（[pyproject.toml:L64-L68](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L64-L68)）：`flashinfer.data.cutlass → 3rdparty/cutlass`、`spdlog`、`cccl` 三个映射，说明这三个子模块是「包的一部分」，必须随包分发。

#### 4.2.4 代码实践

**实践目标**：确认子模块的存在，并理解 editable 软链接的指向。

**操作步骤**：

1. 在仓库根目录查看子模块状态（只读 git 命令）：

   ```bash
   git submodule status
   ```

2. 如果当初克隆时忘了 `--recursive`，补拉：

   ```bash
   git submodule update --init --recursive
   ```

3. editable 安装后，查看 `flashinfer/data` 是否是软链接（示例命令，需在已安装环境运行）：

   ```bash
   ls -l $(python -c "import flashinfer, pathlib; print(pathlib.Path(flashinfer.__file__).parent/'data')")
   ```

**需要观察的现象**：

- `git submodule status` 列出 cutlass / spdlog / cccl（以及可能的 nixl 等）。
- 安装后的 `flashinfer/data/csrc`、`flashinfer/data/include`、`flashinfer/data/cutlass` 都是符号链接，指向你的工作树。

**预期结果**：你会看到 editable 安装**没有复制源码**，而是用软链接把工作树「嫁接」进了包目录——这正是「改源码立即生效」的物理基础。

> 若尚未安装，步骤 3 的现象即为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：克隆时漏了 `--recursive`，之后直接 `pip install -e .`，会在哪个环节出问题？

**参考答案**：`3rdparty/cutlass` 等目录是空的，构建后端铺设 `flashinfer/data/cutlass` 软链接时虽然不报错（链接到空目录），但后续任何 kernel 的 JIT 编译都会因为找不到 CUTLASS 头文件而失败。补救方法是 `git submodule update --init --recursive` 后重新触发编译（或 `flashinfer clear-cache`）。

**练习 2**：把 `--no-build-isolation` 换成默认（带隔离）的安装，最可能踩到哪个坑？

**参考答案**：隔离环境会按 `requires` 安装构建依赖，但不会带入你本机的 PyTorch/CUDA；构建钩子若尝试在隔离环境里装 moe_ep wheel，这些 wheel 装完即丢、到不了目标环境，可能报错或导致 EP 后端缺失。这就是 README/CLAUDE.md 都强调要用 `--no-build-isolation` 的原因。

---

### 4.3 构建后端：PEP 517 build_backend.py

#### 4.3.1 概念说明

FlashInfer 不直接用 setuptools，而是把 setuptools 的标准后端 `setuptools.build_meta`（别名 `orig`）包了一层，做成自定义后端 `build_backend`。这个后端在每次构建前多做两件事：

1. **生成版本元数据文件** `flashinfer/_build_meta.py`，写入 `__version__` 和 `__git_version__`。
2. **铺设 `flashinfer/data` 目录**，让运行时能找到 kernel 源码与第三方头文件。

为什么版本号要「动态生成」而不是写死在 `pyproject.toml`？因为 FlashInfer 的版本是「单一事实源 `version.txt` + 可选的 dev/local 后缀 + git commit」，需要在构建时动态计算。

#### 4.3.2 核心流程

构建后端的几个标准钩子（PEP 517）与 FlashInfer 的实现：

| PEP 517 钩子 | FlashInfer 实现 | 安装前先做 |
|--------------|-----------------|-----------|
| `build_wheel` | 调 `_prepare_for_wheel()`（拷贝文件） | 写版本 + 铺 data + 可选编译 EP |
| `build_editable` | 调 `_prepare_for_editable()`（软链接） | 同上，但用软链接 |
| `build_sdist` | 调 `_prepare_for_sdist()`（拷贝，但不编译 EP） | 只铺 data |
| `prepare_metadata_for_build_*` | 先 prepare 再交给 `orig` | 同上 |

`_create_build_metadata()` 在 **模块导入时立即执行一次**（文件末尾直接调用），保证无论走哪个钩子，版本元数据都已就绪。

#### 4.3.3 源码精读

后端在 `pyproject.toml` 里注册（[pyproject.toml:L42-L45](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L42-L45)）：`build-backend = "build_backend"`，`backend-path = ["."]`，表示在仓库根目录找 `build_backend.py`。

`build_backend.py` 开头导入真正的 setuptools 后端并取得仓库根与 data 目录（[build_backend.py:L25-L29](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L25-L29)）：

> 这里的 `orig = setuptools.build_meta as orig` 就是「被包裹的原生后端」，所有最终打包动作最后都委托给它。

版本元数据的生成逻辑在 `_create_build_metadata`（[build_backend.py:L743-L786](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L743-L786)）。关键步骤：

- 读取 `version.txt` 拿到基础版本（[build_backend.py:L745-L750](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L745-L750)），若文件不存在则用 `0.0.0+unknown`。
- 支持 `FLASHINFER_DEV_RELEASE_SUFFIX` 加 `.devN` 后缀、`FLASHINFER_LOCAL_VERSION` 加 `+local` 本地标识（[build_backend.py:L753-L764](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L753-L764)）。
- 调 `get_git_version()` 取当前 commit（[build_backend.py:L757-L758](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L757-L758)），其实现就是 `git rev-parse HEAD`，失败回退 `unknown`（[build_utils.py:L24-L46](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_utils.py#L24-L46)）。
- 把结果写进 `flashinfer/_build_meta.py`（[build_backend.py:L780-L783](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L780-L783)），并在导入时立刻调用一次（[build_backend.py:L789-L790](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L789-L790)）。

`version.txt` 本身只有一行（[version.txt:L1](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/version.txt#L1)）：当前版本是 `0.6.14`。这就是 `flashinfer show-config` 里看到的版本号来源。

运行时如何读到这个版本？`flashinfer/version.py` 用 try/except 容错导入（[flashinfer/version.py:L18-L23](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/version.py#L18-L23)）：能导入 `_build_meta` 就用里面的 `__version__`，否则回退 `0.0.0+unknown`。而 `pyproject.toml` 又把 `version` 声明为动态、从 `_build_meta.__version__` 读取（[pyproject.toml:L55-L57](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L55-L57)）。于是形成链路：

```text
version.txt (0.6.14)
   └─(build_backend 读取并写出)─▶ flashinfer/_build_meta.py (__version__)
        ├─(pyproject dynamic=attr)─▶ pip 安装时的包版本
        └─(flashinfer/version.py 导入)─▶ 运行时 __version__
```

`flashinfer/data` 目录的铺设在 `_create_data_dir`（[build_backend.py:L800-L826](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L800-L826)）：内部函数 `ln()` 根据参数决定用**软链接**（editable）还是**拷贝**（wheel），然后把 `3rdparty/{cutlass,spdlog,cccl}`、`csrc`、`include` 五项分别链到 `flashinfer/data/` 下（[build_backend.py:L821-L825](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L821-L825)）。

editable 与 wheel 两条路径的区别就在 prepare 函数里（[build_backend.py:L846-L853](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L846-L853) 用 `use_symlinks=True`；[build_backend.py:L828-L834](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L828-L834) 用 `use_symlinks=False`）。最终 PEP 517 钩子只是「先 prepare 再交给 orig」（[build_backend.py:L889-L891](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L889-L891) 的 `build_editable`、[build_backend.py:L899-L901](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/build_backend.py#L899-L901) 的 `build_wheel`）。

#### 4.3.4 代码实践

**实践目标**：亲手追踪「版本号从哪里来」。

**操作步骤**：

1. 查看版本源头：

   ```bash
   cat version.txt
   ```

2. editable 安装后，查看生成的元数据文件（示例代码）：

   ```bash
   cat flashinfer/_build_meta.py
   ```

3. 在 Python 里读取运行时版本：

   ```python
   import flashinfer
   print(flashinfer.__version__)
   ```

**需要观察的现象**：

- `version.txt` = `0.6.14`。
- `_build_meta.py` 里 `__version__ = "0.6.14"`，且 `__git_version__` 是一串 commit 哈希。
- `flashinfer.__version__` 与之一致。

**预期结果**：你会看到三处版本号完全一致，从而验证「`version.txt` → `_build_meta.py` → 运行时 `__version__`」这条单一事实源链路。

> 若 `flashinfer.__version__` 显示 `0.0.0+unknown`，说明 `_build_meta.py` 未生成（常见于直接从 sdist 装且没跑构建钩子），需要重新 editable 安装。该现象为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FlashInfer 用 `_build_meta.py` 这个生成文件，而不是直接在 `pyproject.toml` 写死 `version = "0.6.14"`？

**参考答案**：因为版本需要「`version.txt` 基础值 + dev/local 后缀 + git commit」动态合成，并在构建期、安装期、运行期三处共享同一个值。生成文件 + `dynamic = {attr = ...}` 让这三处都从同一来源读取，避免版本漂移。

**练习 2**：`build_editable` 和 `build_wheel` 对 `flashinfer/data` 的处理有何本质区别？为什么？

**参考答案**：editable 用软链接，让 `data` 始终指向工作树的真实源码，改了 `.cuh` 立即被下次 JIT 编译看到；wheel 用拷贝，因为 wheel 要被打包发到别的机器，目标机器上没有你的工作树，必须把源码实物塞进包里。

---

### 4.4 验证安装：flashinfer show-config

> 本节是为本篇「实践任务」收尾的桥梁模块，承接前三节。

#### 4.4.1 概念说明

装完之后怎么确认「装对了」？FlashInfer 提供了 CLI 子命令 `flashinfer show-config`。它由 `pyproject.toml` 注册的入口脚本暴露（[pyproject.toml:L39-L40](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml#L39-L40)）：`flashinfer = "flashinfer.__main__:cli"`，即命令 `flashinfer` 调用 `flashinfer/__main__.py` 里的 `cli` 函数。

`show-config` 一次性打印六类信息：版本（含 cubin/jit-cache 是否安装）、Torch 与 CUDA runtime 是否可用、关键环境变量、artifact 路径、已下载 cubin 数量、模块编译状态。

#### 4.4.2 核心流程

```text
flashinfer show-config
   │
   ├─ 版本：__version__ + 探测 flashinfer-cubin / flashinfer-jit-cache
   ├─ Torch：torch.__version__ + torch.cuda.is_available()
   ├─ 环境变量：FLASHINFER_CACHE_DIR / FLASHINFER_CUBIN_DIR / CUDA_ARCH_LIST / CUDA_HOME ...
   ├─ Artifact Path：各产物目录
   ├─ Downloaded Cubins：N/M
   └─ Module Status：total / compiled / not_compiled
```

#### 4.4.3 源码精读

`show_config_cmd` 的完整实现在 [flashinfer/__main__.py:L93-L176](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L93-L176)。其中它打印的环境变量集合定义在文件顶部（[flashinfer/__main__.py:L77-L90](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L77-L90)），关键三项是：

- `FLASHINFER_CACHE_DIR`：JIT 总缓存根目录。
- `FLASHINFER_CUBIN_DIR`：cubin 二进制目录。
- `FLASHINFER_CUDA_ARCH_LIST`（取自 `current_compilation_context.TARGET_CUDA_ARCHS`）：目标 GPU 架构列表。

这两个「目录」的真实来源在 `flashinfer/jit/env.py`：

- 缓存根 = `$FLASHINFER_WORKSPACE_BASE/.cache/flashinfer`（默认 `$HOME/.cache/flashinfer`），见 [flashinfer/jit/env.py:L51-L55](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L51-L55)。
- **JIT 工作区**按「版本 + 排序后的架构」分层：`CACHE_DIR/<version>/<arch>`，其下再分 `cached_ops`（编译产物 `.so`）与 `generated`（生成的 `.cu`/`.inc`），见 [flashinfer/jit/env.py:L135-L150](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L135-L150)。注意 `sorted()` 是为了保证目录名确定（否则同一组架构可能生成 `75_80_89` 或 `89_75_80` 两种顺序导致缓存碎片化）。
- **cubin 目录**的解析有三档优先级（[flashinfer/jit/env.py:L59-L97](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L59-L97)）：装了 `flashinfer-cubin` 包就用包内目录 → 否则看 `FLASHINFER_CUBIN_DIR` 环境变量 → 否则回退到 `CACHE_DIR/cubins`。选 `flashinfer-cubin` 时还会做版本一致性校验，不匹配会直接报错（除非设 `FLASHINFER_DISABLE_VERSION_CHECK`）。
- 类似地，**AOT 目录**（jit-cache）也有三档优先级（[flashinfer/jit/env.py:L100-L132](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L100-L132)）：装了 `flashinfer-jit-cache` 包 → 否则回退到包内 `data/aot`。

#### 4.4.4 代码实践（本讲主实践任务）

**实践目标**：跑通 `flashinfer show-config`，从输出里指出 **JIT 工作区目录**与 **cubin 目录**分别指向哪里，并解释其推导过程。

**操作步骤**：

1. 确认已 editable 安装（见 4.2）。然后在终端：

   ```bash
   flashinfer show-config
   ```

2. 在输出的 `=== Environment Variables ===` 段，记录两项：
   - `FLASHINFER_CACHE_DIR`
   - `FLASHINFER_CUBIN_DIR`

3. 用只读命令验证 JIT 工作区是否按「版本/架构」分层（示例，按你机器替换版本与架构）：

   ```bash
   ls -la "$HOME/.cache/flashinfer/"
   ```

**需要观察的现象**：

- `FLASHINFER_CACHE_DIR` 形如 `/home/<you>/.cache/flashinfer`。
- 其下出现按版本命名的子目录（如 `0.6.14/`），再往下是按架构命名的子目录（如 `80_89_90/`），里面才有 `cached_ops/`（编译好的 `.so`）和 `generated/`（生成的源码）。
- `FLASHINFER_CUBIN_DIR`：
  - 若装了 `flashinfer-cubin` → 指向包内目录；
  - 若设了 `FLASHINFER_CUBIN_DIR` 环境变量 → 指向该值；
  - 否则 → 指向 `$HOME/.cache/flashinfer/cubins`。

**预期结果**：你能用一句话说清——「JIT 工作区 = `CACHE_DIR/<flashinfer版本>/<排序后的目标架构>/{cached_ops,generated}`；cubin 目录 = 优先用 cubin 包，其次环境变量，最后 `CACHE_DIR/cubins`」。

> 若当前环境没有 GPU 或未完成安装，`show-config` 仍可运行（部分项会显示 Not installed / No），重点是读懂目录推导逻辑。具体目录值以你本地输出为准（「待本地验证」）。

#### 4.4.5 小练习与答案

**练习 1**：你把 `FLASHINFER_CUDA_ARCH_LIST` 从 `"8.0 9.0a"` 改成 `"8.0"`，JIT 工作区目录名会怎么变？

**参考答案**：架构段由 `80_90a` 变成 `80`，于是工作区目录变成 `CACHE_DIR/0.6.14/80/`，与之前的 `80_90a` 目录互不共享缓存。这正是按架构分层、避免不同架构产物混淆的设计。

**练习 2**：`show-config` 里 `flashinfer-cubin: Not installed`，但你想用 cubin，且不想装包、只想指向一个本地目录，怎么做？

**参考答案**：设置环境变量 `export FLASHINFER_CUBIN_DIR=/path/to/my/cubins`。`_get_cubin_dir()` 的第二档优先级会读取它（[flashinfer/jit/env.py:L88-L94](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L88-L94)）。

## 5. 综合实践

把本讲三块知识串起来，完成一次「从零到验证」的安装。

**任务**：在一台有 NVIDIA GPU 的机器上，从源码 editable 安装 FlashInfer，并用 CLI 验证，最后用一句话解释你看到的每个关键目录的来历。

**步骤**：

1. 克隆并初始化子模块：

   ```bash
   git clone https://github.com/flashinfer-ai/flashinfer.git --recursive
   cd flashinfer
   ```

2. editable 安装（注意两个标志）：

   ```bash
   python -m pip install --no-build-isolation -e . -v
   ```

3. 确认版本链路：

   ```bash
   cat version.txt                       # 源头：0.6.14
   cat flashinfer/_build_meta.py         # 构建期生成
   python -c "import flashinfer; print(flashinfer.__version__)"   # 运行期
   ```

4. 确认 `flashinfer/data` 是软链接嫁接：

   ```bash
   ls -l flashinfer/data
   ```

5. 验证安装并解读输出：

   ```bash
   flashinfer show-config
   ```

**交付物**（写成一段话或一张表）：

- 你装了哪几个 flashinfer 包；
- `FLASHINFER_CACHE_DIR` 与 `FLASHINFER_CUBIN_DIR` 各指向哪里、为什么；
- JIT 工作区目录的完整路径，并说明其中「版本段」和「架构段」分别由谁决定；
- `flashinfer.__version__` 是否与 `version.txt` 一致，若不一致可能的原因。

**预期结果**：完成后你应当能向别人讲清「FlashInfer 的安装本质上做了两件事——把版本号算出来写进 `_build_meta.py`，把 kernel 源码（含 CUTLASS 头）软链到 `flashinfer/data`；运行时 JIT 会按版本+架构在工作区里缓存编译产物，cubin/jit-cache 是可选的加速包」。这一理解是下一单元（JIT 编译系统）的直接前置。

> 任何无法在本地复现的步骤（如无 GPU、无 nvcc），请标注「待本地验证」并写出你预期的现象，不要假装已跑通。

## 6. 本讲小结

- FlashInfer 有三个协作包：`flashinfer-python`（核心，必装，JIT 兜底）、`flashinfer-cubin`（全架构预编译二进制）、`flashinfer-jit-cache`（按 CUDA 版本的预编译缓存）；后两者用于加速启动与离线运行。
- 源码安装必须 `--recursive` 以拉取 `3rdparty/{cutlass,spdlog,cccl}` 子模块，否则 kernel 编译找不到头文件。
- editable 安装推荐 `--no-build-isolation`：避免 pip 从 PyPI 拉不兼容的 PyTorch/CUDA，也让构建钩子能在用户环境里安装 moe_ep wheel。
- `build_backend.py` 是自定义 PEP 517 后端，包裹了 setuptools：导入时即生成 `flashinfer/_build_meta.py`（版本来自 `version.txt`，当前 `0.6.14`），并在 `build_editable`/`build_wheel` 前铺设 `flashinfer/data` 目录（editable 用软链接、wheel 用拷贝）。
- 版本是单一事实源：`version.txt → _build_meta.py → pyproject dynamic attr / 运行时 __version__`，三处一致。
- `flashinfer show-config` 是验证安装的入口，能打印版本、Torch/CUDA、环境变量、artifact 路径、cubin 下载量、模块编译状态；JIT 工作区按「版本/排序架构」分层，cubin 目录有三档优先级解析。

## 7. 下一步学习建议

本篇解决了「装上、验过」，但**还没真正触发一次 JIT 编译**。建议下一步：

- **进入第 2 单元（JIT 编译系统）**，从 `u2-l1-jit-overview.md` 开始，理解三层 JIT 架构（JitSpec → 代码生成 → 编译加载）与「编辑 `.cuh` 自动重编译」的原理。本篇提到的 `FLASHINFER_GEN_SRC_DIR`、`FLASHINFER_JIT_DIR` 会在那里被深入使用。
- 在进入 JIT 之前，也可先读 `u1-l3-directory-structure.md`（仓库目录与代码分层）与 `u1-l5-first-attention-kernel.md`（第一个注意力算子实践），亲手感受一次「首次调用触发编译」。
- 想了解 cubin 下载/校验机制的读者，可在学完 JIT 后回看 `flashinfer/jit/cubin_loader.py` 与 `flashinfer/aot.py`（对应大纲 u9-l4）。
