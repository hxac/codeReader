# JitSpec 与工作区环境（env.py）

## 1. 本讲目标

上一讲（u2-l1）你已经建立了 JIT 的「三层架构」框架：`JitSpec`（配料表）→ 代码生成（渲染 `.cu`）→ 编译加载（`build.ninja` + `.so`）。但当时为了讲清「大图」，我们对两个问题只是点到为止：

- **文件到底写在哪里、从哪里读？** 为什么有的目录能写、有的目录「安装后可能只读」？
- **`JitSpec` 怎么决定「用 JIT 现场编译」还是「用预编译包（AOT）」？** 这两个来源的优先级如何？

本讲就把这两个缺口补上。读完本讲，你应该能做到：

- 拿到一个 FlashInfer 安装，**不看源码**也能推算出「生成的源码在哪个目录、编译产物在哪个目录、预编译二进制从哪个目录读」；
- 理解 `env.py` 中 **cubin / AOT / JIT 工作区** 三类目录各自的「解析优先级」（预编译包 > 环境变量 > 默认缓存目录）；
- 掌握 **目录可写性规则**：哪些目录允许写（`generated/`、`cached_ops/`、cubin 缓存），哪些绝不能写（包内的 `csrc/`、`include/`、`aot/`）；
- 看懂 `JitSpec` 的 `get_library_path` / `is_aot` 如何在「AOT 预编译」与「JIT 现场编译」之间做路径选择。

本讲覆盖的最小模块有三个：**工作区路径全景（env.py）**、**三档优先级解析**、**目录可写性规则**，外加 `JitSpec` 的 AOT/JIT 路径选择。它是 u2-l1「总纲」之后、深入 JIT 系统的第一块「地基」——后续 u2-l3（代码生成）、u2-l4（编译上下文）、u2-l5（缓存失效）都要建立在本讲对路径的精确理解之上。

## 2. 前置知识

本讲假设你已掌握 u2-l1 的全部内容，尤其是：

- 三层架构（`JitSpec` → 代码生成 → 编译加载）的职责划分；
- `JitSpec` 是一个薄 dataclass，`is_compiled` 只是 `.so` 文件存在性检查（「登记 ≠ 编译」）；
- `build_and_load` 用 `FileLock` 守住「build + load」整体，并对 AOT 包做短路加载；
- 工作区默认在 `~/.cache/flashinfer/` 下，按「版本号/CUDA 架构」分层。

下面补充几个本讲会用到、但前面没细讲的术语：

- **工作区（workspace）**：FlashInfer 在磁盘上读写「生成的源码」和「编译产物」的目录树。它的「根」由环境变量 `FLASHINFER_WORKSPACE_BASE` 控制，默认是用户家目录。
- **预编译包（AOT 包）**：在打包发版时就编译好的二进制。FlashInfer 有两类：`flashinfer-jit-cache`（按 CUDA 版本预编译的 `.so` 缓存，加载快）和 `flashinfer-cubin`（全架构的 `.cubin` 二进制，离线用）。它们是 JIT 的「加速包」，让生产环境不必现场编译。
- **`data/` 命名空间**：editable 安装时，仓库里的 `include/`、`csrc/`、`3rdparty/cutlass` 等目录会被软链接（symlink）到 Python 包内的 `flashinfer/data/` 下，构成一个统一的「只读源码」入口。
- **`pathlib.Path`**：Python 的路径对象，`/` 运算符可拼接路径（如 `base / "a" / "b"`）。本讲的路径解析全程用它。
- **importlib 探测包**：`importlib.util.find_spec("pkg")` 用来判断某个 Python 包是否已安装，返回 `None` 表示没装。`env.py` 用它判断两个预编译包是否存在。

> 一个贯穿全讲的心智模型：`env.py` 里所有路径在**模块导入时一次性求值**为模块级常量（如 `FLASHINFER_JIT_DIR`）。也就是说，你设的环境变量必须在 **`import flashinfer` 之前** 生效；运行中改环境变量不会改变这些常量。

## 3. 本讲源码地图

本讲围绕两个文件展开，它们分工明确：

| 文件 | 作用 |
|------|------|
| [flashinfer/jit/env.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py) | **本讲主线之一**。定义全部工作区路径：根目录 `FLASHINFER_BASE_DIR`、缓存根 `FLASHINFER_CACHE_DIR`、JIT 工作区 `FLASHINFER_WORKSPACE_DIR`/`JIT_DIR`/`GEN_SRC_DIR`、cubin 目录、AOT 目录，以及只读的 `data/` 命名空间（`INCLUDE_DIR`/`CSRC_DIR`/`CUTLASS`/`SPDLOG`/`CCCL`）。决定了「写在哪、读哪」。 |
| [flashinfer/jit/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py) | **本讲主线之二**。`JitSpec` 的路径属性（`get_library_path`/`is_aot`/`aot_path`/`lock_path`）实现了「AOT 优先、否则 JIT」的选择；`get_tmpdir` 定义文件锁目录；模块顶部的 `os.makedirs` 落实「只建可写目录」。 |
| [flashinfer/__main__.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py) | `flashinfer show-config` 打印 `FLASHINFER_CACHE_DIR`、`FLASHINFER_CUBIN_DIR`、`TARGET_CUDA_ARCHS` 等，是本讲实践的「环境体检」入口。 |
| [flashinfer/jit/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/utils.py) | `write_if_different`（u2-l1 已讲）：决定 `generated/` 下 `.cu`/`.inc` 何时真正写盘。 |
| [flashinfer/jit/cpp_ext.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py) | `generate_ninja_build_for_op` 产出 `build.ninja` 文本（`cached_ops/` 下），是「`build.ninja` 如何产生」的源头（u2-l1 已讲，本讲只引用）。 |

> 阅读建议：先通读 [env.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py) 全文（它很短，只有约 165 行），把所有以 `FLASHINFER_` 开头的常量当成一张「目录对照表」记住；再回到 [core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py) 看 `JitSpec` 的路径属性如何消费这张表。

## 4. 核心概念与源码讲解

### 4.1 env.py 路径全景：从根目录到工作区

#### 4.1.1 概念说明

`env.py` 的全部职责只有一句话：**把一组「逻辑意图」（「我要放编译产物」「我要放生成的源码」「我要读 include 头文件」）映射到磁盘上的具体目录**。它不编译任何东西，只产出常量；真正读写文件的是 `core.py` 和各 `gen_*_module`。

要理解这张映射，先建立一个「目录树」心智模型。以默认配置（用户家目录 `~`）为例，`env.py` 描述的目录结构大致是：

```
~/                                 ← FLASHINFER_BASE_DIR（可由 FLASHINFER_WORKSPACE_BASE 覆盖）
└── .cache/flashinfer/             ← FLASHINFER_CACHE_DIR
    ├── cubins/                    ← FLASHINFER_CUBIN_DIR（默认；下载的 cubin）
    └── <version>/<arch>/          ← FLASHINFER_WORKSPACE_DIR（版本+排序后架构）
        ├── cached_ops/            ← FLASHINFER_JIT_DIR（编译产物：.so/.o/.ninja，可写）
        ├── generated/             ← FLASHINFER_GEN_SRC_DIR（渲染出的 .cu/.inc，可写）
        └── tmp/                   ← 文件锁目录（由 core.get_tmpdir 创建）

<python包>/flashinfer/data/        ← FLASHINFER_DATA（_package_root / "data"，只读）
├── include/                       ← FLASHINFER_INCLUDE_DIR（只读源码模板）
├── csrc/                          ← FLASHINFER_CSRC_DIR（只读，绑定层模板）
├── cutlass/, spdlog/, cccl/       ← 第三方依赖头文件（只读）
└── aot/                           ← FLASHINFER_AOT_DIR（预编译 .so 回退位置，只读）
```

这张树揭示了 `env.py` 最核心的「可写 vs 只读」二分：

- **可写区**（`BASE_DIR/.cache/flashinfer/...`）：在用户家目录（或 `FLASHINFER_WORKSPACE_BASE` 指定的位置）下，JIT 可以自由创建、覆盖、删除文件。
- **只读区**（`_package_root/data/...`）：在 Python 包安装目录内，是源码模板和预编译二进制的「事实源（source of truth）」，安装后通常只读。

`env.py` 顶部的注释点明了一个关键约束：**AOT 脚本会覆盖这些环境变量**，所以约定「不要 `from .jit.env import xxx`，而是 `from .jit import env as jit_env` 再用 `jit_env.xxx`」。这样 AOT 脚本可以 monkey-patch 整个 `env` 模块来重定向路径，而不会破坏已有的 `from ... import` 引用。

[flashinfer/jit/env.py:17-19](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L17-L19) —— 顶部约定：用 `env as jit_env` 而非直接 import 符号，以便 AOT 脚本能整体覆盖 `env`。

#### 4.1.2 核心流程

`env.py` 模块级常量的求值顺序（导入时自上而下执行）：

```
1. FLASHINFER_BASE_DIR = getenv("FLASHINFER_WORKSPACE_BASE") or ~
2. FLASHINFER_CACHE_DIR = BASE_DIR / ".cache" / "flashinfer"
3. _package_root = env.py 所在文件上溯两级 = flashinfer/ 包目录
4. FLASHINFER_CUBIN_DIR = _get_cubin_dir()      # 三档优先级（见 4.2）
5. FLASHINFER_AOT_DIR   = _get_aot_dir()        # 两档优先级（见 4.2）
6. FLASHINFER_WORKSPACE_DIR = CACHE_DIR / version / sorted_arch
7. FLASHINFER_JIT_DIR     = WORKSPACE_DIR / "cached_ops"     # 可写
8. FLASHINFER_GEN_SRC_DIR = WORKSPACE_DIR / "generated"      # 可写
9. FLASHINFER_DATA / INCLUDE_DIR / CSRC_DIR / CUTLASS / ... = _package_root / "data" / ...  # 只读
```

注意 `_package_root` 的推导方式——`pathlib.Path(__file__).resolve().parents[1]`：

- `__file__` 是 `flashinfer/jit/env.py` 的绝对路径；
- `parents[0]` 是 `flashinfer/jit/`；
- `parents[1]` 是 `flashinfer/`（即 Python 包根）。

所以 `FLASHINFER_DATA = flashinfer/ / "data"`，而 `data/` 正是 u1-l2 讲过的、由 PEP 517 构建后端铺设出来的「软链接农场」（editable 用软链、wheel 用拷贝）。

#### 4.1.3 源码精读

根目录与缓存根的定义，注意 `FLASHINFER_WORKSPACE_BASE` 是唯一能整体搬移工作区的开关：

[flashinfer/jit/env.py:51-56](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L51-L56) —— `FLASHINFER_BASE_DIR` 默认为家目录，可被 `FLASHINFER_WORKSPACE_BASE` 覆盖；`FLASHINFER_CACHE_DIR` 固定为 `BASE_DIR/.cache/flashinfer`；`_package_root` 上溯两级得到包根。

工作区目录名由「版本号 + 排序后的架构」拼成。这里 `sorted()` 不是可有可无的——注释专门说明了它的必要性：

[flashinfer/jit/env.py:135-148](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L135-L148) —— `_get_workspace_dir_name`：把 `CompilationContext().TARGET_CUDA_ARCHS`（一组 `(major, minor)` 元组）`sorted()` 后拼成 `75_80_89` 这样的字符串。注释强调：若不排序，同一组架构在不同运行里可能生成 `75_80_89` 或 `89_75_80` 两个不同目录名，导致**缓存碎片**（同一份 kernel 被编译两次、存两份）。架构集合如何得到是 u2-l4 的主题，这里只需知道它是一组 SM 元组。

> 这段用到了 [flashinfer/compilation_context.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/compilation_context.py) 的 `CompilationContext`，并在 `env.py` 第 23、136 行 import。它的 `TARGET_CUDA_ARCHS` 来自 GPU 探测或 `FLASHINFER_CUDA_ARCH_LIST`（u2-l4 详讲）。

随后是两个「可写子目录」与整个「只读 data 命名空间」的定义：

[flashinfer/jit/env.py:149-164](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L149-L164) —— `FLASHINFER_JIT_DIR`（编译产物，可写）、`FLASHINFER_GEN_SRC_DIR`（生成源码，可写），以及 `_package_root / "data" / ...` 下的一组只读目录：`INCLUDE_DIR`、`CSRC_DIR`、`CUTLASS_INCLUDE_DIRS`（注意是 list，含两个路径）、`SPDLOG_INCLUDE_DIR`、`CCCL_INCLUDE_DIRS`（含 cub/libcudacxx/thrust 三个路径）。

注意第 154 行有一行被注释掉的 `# FLASHINFER_SRC_DIR = ...`，说明历史上曾规划过更细的分层，现已收敛到 `data/` 统一命名空间——这是一个「目录结构演进」的小痕迹。

#### 4.1.4 代码实践

**目标**：源码阅读型实践——不运行任何 kernel，仅凭 `env.py` 推算出你本机的完整工作区路径，并用 `show-config` 验证。

**操作步骤**：

1. 先确认你的 flashinfer 版本（决定目录名中的 `<version>` 段）：
   ```bash
   python -c "import flashinfer; print(flashinfer.__version__)"
   ```
2. 确认本机目标架构集合（决定目录名中的 `<arch>` 段）。如果你设了 `FLASHINFER_CUDA_ARCH_LIST`，它就是那段；否则由 GPU 探测得到：
   ```bash
   python -c "from flashinfer.compilation_context import CompilationContext; print(CompilationContext().TARGET_CUDA_ARCHS)"
   ```
3. 运行体检命令，对照它打印的 `FLASHINFER_CACHE_DIR`：
   ```bash
   flashinfer show-config
   ```
4. 在脑中拼出完整路径：`<FLASHINFER_CACHE_DIR>/<version>/<arch>/`，例如 `~/.cache/flashinfer/0.6.0/80_89_90/`（架构段的具体拼接见步骤 2 的排序结果）。
5. （需要环境，待本地验证）`ls` 一下该路径，确认 `cached_ops/`、`generated/` 子目录存在。

**需要观察的现象**：

- `show-config` 里 `FLASHINFER_CACHE_DIR` 的值与步骤 4 推算的根一致；
- `<version>` 段等于步骤 1 的版本号；`<arch>` 段等于步骤 2 的架构**排序后**用 `_` 连接（如 `(8,0),(8,9),(9,0)` → `80_89_90`）。

**预期结果**：你能不看 `env.py`，仅凭「版本 + 排序架构」算出工作区根，并指出 `cached_ops`（产物）与 `generated`（源码）两个子目录。这是后续所有「定位产物」操作的基础。

> 说明：`show-config` 打印的是 `FLASHINFER_CACHE_DIR`（缓存根），不直接打印 `JIT_DIR`/`GEN_SRC_DIR`——后者需你在脑中再拼 `/<version>/<arch>/{cached_ops,generated}`。

#### 4.1.5 小练习与答案

**练习 1**：你把 `FLASHINFER_WORKSPACE_BASE` 设成了 `/scratch`，工作区根会变成什么？哪些目录会跟着搬，哪些不会？

> **参考答案**：`FLASHINFER_CACHE_DIR` 变成 `/scratch/.cache/flashinfer`，进而 `WORKSPACE_DIR`/`JIT_DIR`/`GEN_SRC_DIR` 都跟着搬过去。但 `_package_root/data/...` 下的只读目录（`INCLUDE_DIR`、`CSRC_DIR`、CUTLASS 等）**不会**搬——它们由包安装位置决定，与工作区根无关。

**练习 2**：为什么 `env.py:140` 的 `sorted()` 对「缓存命中率」很重要？

> **参考答案**：架构集合是无序得到的（来自 GPU 枚举），若不排序，同一台多卡机器在不同运行顺序下可能拼出 `75_80_89` 或 `89_75_80` 两种目录名，导致同一份 kernel 被编译两次、存两份（缓存碎片）。`sorted()` 保证目录名对「架构集合」确定性，从而命中同一份缓存。

---

### 4.2 三档优先级解析：cubin / AOT / JIT 工作区

#### 4.2.1 概念说明

`env.py` 里有三个目录的解析**不是一行赋值，而是函数调用**——它们遵循「优先级」逻辑：先看「更好的来源」在不在，在就用它；不在就降级到下一个来源。这三个目录是：

| 目录 | 一档（最优） | 二档 | 三档（兜底） |
|------|-------------|------|-------------|
| `FLASHINFER_CUBIN_DIR`（cubin 二进制） | `flashinfer-cubin` 包 | 环境变量 `FLASHINFER_CUBIN_DIR` | `FLASHINFER_CACHE_DIR / "cubins"` |
| `FLASHINFER_AOT_DIR`（预编译 `.so`） | `flashinfer-jit-cache` 包 | — | `_package_root / "data" / "aot"` |
| 工作区（`JIT_DIR`/`GEN_SRC_DIR`） | — | — | 固定 `WORKSPACE_DIR / {...}` |

「优先级」背后的设计动机是：**预编译包是「更快、更可控」的来源，应当优先；环境变量是「用户明确指定」的来源，其次；默认缓存目录是「无人指定时」的兜底。** 这套降级逻辑让 FlashInfer 在三种部署形态下都能工作：

- **开发机**（只装了 `flashinfer`）：走兜底，工作区在 `~/.cache/...`，现场 JIT；
- **生产机**（额外装了 `flashinfer-jit-cache`）：一档命中，预编译 `.so` 直接加载，启动飞快；
- **离线/气隙机**（额外装了 `flashinfer-cubin`，或设了 `FLASHINFER_CUBIN_DIR`）：cubin 从本地读，无需联网下载。

「判断包是否安装」靠的是 `importlib.util.find_spec`——返回非 `None` 即视为已安装：

[flashinfer/jit/env.py:27-48](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L27-L48) —— `has_flashinfer_jit_cache()` 与 `has_flashinfer_cubin()`：用 `find_spec` 探测两个可选预编译包是否存在。

#### 4.2.2 核心流程

cubin 目录的三档降级（`_get_cubin_dir`）：

```
_get_cubin_dir():
  if has_flashinfer_cubin():                    # 一档
      校验 flashinfer_cubin 版本 == flashinfer 版本（可被 FLASHINFER_DISABLE_VERSION_CHECK 跳过）
      return flashinfer_cubin.get_cubin_dir()
  elif getenv("FLASHINFER_CUBIN_DIR"):          # 二档
      return 该环境变量值
  else:                                          # 三档（兜底）
      return FLASHINFER_CACHE_DIR / "cubins"
```

AOT 目录的两档降级（`_get_aot_dir`）逻辑类似，但更简单——只有「包」和「默认目录」两档，没有环境变量档：

```
_get_aot_dir():
  if has_flashinfer_jit_cache():                # 一档
      校验版本前缀匹配（jit-cache 版本带 cu 后缀，用 startswith 而非 ==）
      return flashinfer_jit_cache.get_jit_cache_dir()
  else:                                          # 二档（兜底）
      return _package_root / "data" / "aot"
```

两个版本校验有细微差别，值得对比：

- **cubin 用严格相等**（`==`）：`flashinfer_cubin_version != flashinfer_version` 即报错。因为 cubin 包不带 CUDA 后缀。
- **AOT 用前缀匹配**（`startswith`）：`flashinfer_jit_cache_version.startswith(flashinfer_version)`。因为 jit-cache 版本形如 `0.3.1+cu129`，带 CUDA 后缀，不能用 `==`。注释里写明了这个原因。

两者都允许用 `FLASHINFER_DISABLE_VERSION_CHECK=1` 跳过校验，并且对源码安装（`flashinfer_version == "0.0.0+unknown"`，即没有 `_build_meta.py`）也跳过校验，避免误报。

#### 4.2.3 源码精读

`_get_cubin_dir` 的三档降级与版本校验：

[flashinfer/jit/env.py:59-94](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L59-L94) —— 先查 `flashinfer-cubin` 包（并校验版本严格相等，可被 `FLASHINFER_DISABLE_VERSION_CHECK` 跳过），再查 `FLASHINFER_CUBIN_DIR` 环境变量，最后回退到 `CACHE_DIR / "cubins"`。注意第 86 行调用 `flashinfer_cubin.get_cubin_dir()`——预编译包自带一个告诉外界「我的 cubin 在哪」的函数。

`_get_aot_dir` 的两档降级与前缀匹配校验：

[flashinfer/jit/env.py:100-129](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L100-L129) —— 先查 `flashinfer-jit-cache` 包，用 `startswith` 校验版本（因 jit-cache 版本带 `+cuXXX` 后缀）；否则回退到 `_package_root / "data" / "aot"`。第 111-112 行注释专门解释了为何不使用精确匹配。

这两个函数的**返回值在导入时立即赋给模块常量**：

[flashinfer/jit/env.py:97](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L97) —— `FLASHINFER_CUBIN_DIR = _get_cubin_dir()`：模块导入即求值，之后再改环境变量无效。
[flashinfer/jit/env.py:132](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L132) —— `FLASHINFER_AOT_DIR = _get_aot_dir()`：同上。

`show-config` 正是把这些常量打印出来的入口：

[flashinfer/__main__.py:77-84](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L77-L84) —— `env_variables` 字典收集 `FLASHINFER_CACHE_DIR`、`FLASHINFER_CUBIN_DIR`、`TARGET_CUDA_ARCHS`、`CUDA_VERSION` 等，供 `show-config` 打印。注意它显示的是 `FLASHINFER_CACHE_DIR`（缓存根），`JIT_DIR` 需你自行再拼 `/<version>/<arch>/cached_ops`。

#### 4.2.4 代码实践

**目标**：观察三档优先级在你的环境里实际命中了哪一档。

**操作步骤**：

1. 运行 `show-config`，记录 `FLASHINFER_CUBIN_DIR` 的值：
   ```bash
   flashinfer show-config | grep -i cubin
   ```
2. 判断它命中了哪一档：
   - 若你装了 `flashinfer-cubin`：值应来自 `flashinfer_cubin.get_cubin_dir()`（通常在 site-packages 内）；
   - 若你设了 `FLASHINFER_CUBIN_DIR` 环境变量：值应等于该变量；
   - 否则：值应是 `<CACHE_DIR>/cubins`（如 `~/.cache/flashinfer/cubins`）。
3. 在 Python 里确认探测函数的返回（不触发任何编译）：
   ```bash
   python -c "from flashinfer.jit.env import has_flashinfer_cubin, has_flashinfer_jit_cache, FLASHINFER_AOT_DIR, FLASHINFER_CUBIN_DIR; print('cubin pkg:', has_flashinfer_cubin()); print('jit-cache pkg:', has_flashinfer_jit_cache()); print('AOT_DIR:', FLASHINFER_AOT_DIR); print('CUBIN_DIR:', FLASHINFER_CUBIN_DIR)"
   ```
4. （可选，待本地验证）若想人为切到「二档」，可设 `FLASHINFER_CUBIN_DIR=/tmp/my_cubins python -c "from flashinfer.jit.env import FLASHINFER_CUBIN_DIR; print(FLASHINFER_CUBIN_DIR)"`，确认它变成了你指定的路径（前提是你没装 `flashinfer-cubin` 包，否则一档仍优先）。

**需要观察的现象**：

- 步骤 3 的两个布尔值反映你装了哪个预编译包；
- `AOT_DIR` 要么指向 site-packages 内（装了 jit-cache），要么指向包内 `data/aot`（没装）。

**预期结果**：你能说清自己机器上 cubin/AOT 各命中了第几档，并解释为什么。这是诊断「为什么我的 kernel 启动这么快/这么慢」的关键——命中预编译包则几乎不编译，命中兜底则现场 JIT。

> 「待本地验证」：第 4 步能否切档取决于一档（`flashinfer-cubin` 包）是否存在；若存在，二档环境变量会被忽略。

#### 4.2.5 小练习与答案

**练习 1**：为什么 cubin 的版本校验用 `==`，而 AOT（jit-cache）用 `startswith`？

> **参考答案**：`flashinfer-cubin` 的版本号与 flashinfer 主版本严格一致，可用 `==`；而 `flashinfer-jit-cache` 的版本号带 CUDA 后缀（如 `0.3.1+cu129`），主版本只是其前缀，故用 `startswith(flashinfer_version)` 判断兼容性（[env.py:111-118](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L111-L118)）。

**练习 2**：你同时设了 `FLASHINFER_CUBIN_DIR=/a/b` 又装了 `flashinfer-cubin` 包，cubin 实际从哪读？

> **参考答案**：从 `flashinfer-cubin` 包读（一档优先）。`_get_cubin_dir` 先检查包、再检查环境变量（[env.py:66-91](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L66-L91)）。环境变量只在「没装包」时才生效。

**练习 3**：源码安装（`flashinfer_version == "0.0.0+unknown"`）时，版本校验为何被跳过？

> **参考答案**：源码安装若缺少 `_build_meta.py`，`__version__` 会回退到 `"0.0.0+unknown"`（u1-l2 讲过版本链），此时与任何预编译包都不可能相等，若强行校验必然误报。故代码在版本为 `"0.0.0+unknown"` 时跳过校验（[env.py:74-76](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L74-L76) 与 [env.py:114-117](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L114-L117)）。

---

### 4.3 目录可写性规则：为什么有的目录绝不能写

#### 4.3.1 概念说明

JIT 编译要写两类文件：**生成的源码**（`.cu`/`.inc`）和**编译产物**（`.o`/`..cuda.o`/`.so`/`build.ninja`）。直觉上，把它们写到「源码旁边」最方便，但 FlashInfer 偏偏不这么做——它把可写文件全部放进用户家目录下的工作区，而把源码模板（`include/`、`csrc/`）和预编译二进制（`aot/`）留在包目录里、当作只读。

原因有三：

1. **包目录可能只读**：用户可能把 flashinfer 装在系统目录（如 `/usr/lib/python3.x/site-packages/`）或只读容器镜像里，JIT 根本没有写权限。如果把产物写进包目录，在这些环境下直接失败。
2. **包目录可能被多用户共享**：一台机器多个用户共用同一个 site-packages，各自的工作区必须隔离，否则互相覆盖。
3. **可写区与事实源分离**：源码模板和预编译二进制是「只读的事实源」，不应被运行时污染；工作区是「可变的派生产物」，可以随时清空重建（`flashinfer clear-cache`）而不影响包。

因此 `core.py` 顶部在模块导入时，**只创建可写的工作区目录，刻意不创建只读的 `CSRC_DIR`**——注释直接点出了这条红线：

[flashinfer/jit/core.py:18-20](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L18-L20) —— 导入时 `os.makedirs(WORKSPACE_DIR, exist_ok=True)`，并注释「Do NOT create FLASHINFER_CSRC_DIR here — it's the package directory which may be read-only after installation」。

#### 4.3.2 核心流程

把 `env.py` 的所有路径按可写性归类：

| 目录常量 | 可写？ | 用途 | 清理命令 |
|---------|-------|------|---------|
| `FLASHINFER_GEN_SRC_DIR`（`generated/`） | ✅ 可写 | 渲染出的 `.cu`/`.inc` 源码 | （随 `cached_ops` 一起，但不单独清） |
| `FLASHINFER_JIT_DIR`（`cached_ops/`） | ✅ 可写 | 编译产物 `.o`/`.so`/`build.ninja` | `flashinfer clear-cache` |
| `FLASHINFER_JIT_DIR / "tmp"`（锁目录） | ✅ 可写 | `FileLock` 文件 | （自动） |
| `FLASHINFER_CUBIN_DIR`（默认 `cubins/`） | ✅ 可写（兜底档） | 下载的 cubin 缓存 | `flashinfer clear-cubin` |
| `FLASHINFER_INCLUDE_DIR`（`data/include/`） | ❌ 只读 | 框架无关 kernel 模板 | — |
| `FLASHINFER_CSRC_DIR`（`data/csrc/`） | ❌ 只读 | launcher + TVM-FFI 绑定模板 | — |
| `FLASHINFER_AOT_DIR`（`data/aot/`） | ❌ 只读 | 预编译 `.so` 回退 | — |
| `CUTLASS/SPDLOG/CCCL` 头目录 | ❌ 只读 | 第三方依赖头文件 | — |

关键洞察：**所有「可写」目录都派生自 `FLASHINFER_CACHE_DIR`（家目录下），所有「只读」目录都派生自 `_package_root`（包目录下）**。这条规则让你一眼就能判断一个路径能不能写——看它属于「缓存树」还是「包树」。

#### 4.3.3 源码精读

可写工作区在导入时被创建（且只创建这一个）：

[flashinfer/jit/core.py:18-20](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L18-L20) —— 仅 `makedirs(WORKSPACE_DIR)`，不碰 `CSRC_DIR`。这条注释是理解整个「可写性」设计的钥匙。

`generated/` 目录的具体写入发生在代码生成层（`gen_*_module` 用 `write_if_different`），而 `cached_ops/` 下的写入发生在 `JitSpec.write_ninja`（建 `build_dir`）和 `run_ninja`（编译产物）。注意 `write_ninja` 调用了 `self.build_dir.mkdir(parents=True, exist_ok=True)`，这是「按需创建」子目录：

[flashinfer/jit/core.py:271-283](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L271-L283) —— `write_ninja`：先 `self.build_dir.mkdir(parents=True, exist_ok=True)`（`build_dir` 即 `JIT_DIR / name`，在可写区内），再写 `build.ninja`。每个模块在自己的可写子目录下工作。

文件锁目录 `tmp/` 也在可写的 `JIT_DIR` 下：

[flashinfer/jit/core.py:487-492](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L487-L492) —— `get_tmpdir`：在 `JIT_DIR / "tmp"` 下放锁文件，注释提到未来想用 `/dev/shm` 以改善 NFS 上的锁行为。锁路径由 `JitSpec.lock_path` 引用：

[flashinfer/jit/core.py:267-269](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L267-L269) —— `lock_path = get_tmpdir() / f"{name}.lock"`：每个模块一把锁，多进程并发编译同一模块时互斥。

清理命令只动可写区，不碰只读区：

[flashinfer/jit/core.py:111-115](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L111-L115) —— `clear_cache_dir` 只 `rmtree(FLASHINFER_JIT_DIR)`（即 `cached_ops`），既不删 `generated/`，也不删只读的 `aot/`。这是 u2-l1 已强调的点，从可写性视角看更清晰：清理只发生在「派生产物」区，事实源永远不动。

#### 4.3.4 代码实践

**目标**：在磁盘上验证「可写区 vs 只读区」的二分，并理解一次 JIT 写入了哪些目录。

**操作步骤**：

1. 找到包目录与缓存目录，确认它们不在同一棵子树：
   ```bash
   python -c "from flashinfer.jit import env as e; print('pkg root:', e._package_root); print('cache root:', e.FLASHINFER_CACHE_DIR)"
   ```
2. 确认 `INCLUDE_DIR`/`CSRC_DIR` 在包目录下（只读区），`JIT_DIR`/`GEN_SRC_DIR` 在缓存目录下（可写区）：
   ```bash
   python -c "from flashinfer.jit import env as e; print('CSRC_DIR:', e.FLASHINFER_CSRC_DIR); print('JIT_DIR:', e.FLASHINFER_JIT_DIR); print('GEN_SRC_DIR:', e.FLASHINFER_GEN_SRC_DIR)"
   ```
3. （需要 GPU 与一次成功编译，待本地验证）触发一次 activation 编译后，分别列出三个目录：
   ```bash
   find "$(python -c 'from flashinfer.jit import env as e; print(e.FLASHINFER_GEN_SRC_DIR)')" -name 'silu*'
   find "$(python -c 'from flashinfer.jit import env as e; print(e.FLASHINFER_JIT_DIR)')" -name 'silu*'
   ```
4. 验证清理只动可写区：先 `flashinfer clear-cache`，再确认 `cached_ops/` 下空了、但 `generated/` 与包内 `csrc/` 都还在。

**需要观察的现象**：

- 步骤 2 的 `CSRC_DIR` 路径前缀是包目录（如 `.../site-packages/flashinfer/data/csrc`），而 `JIT_DIR` 前缀是家目录（如 `~/.cache/flashinfer/.../cached_ops`）——两棵子树分离；
- 步骤 3 中 `generated/` 下有 `silu_and_mul.cu`，`cached_ops/silu_and_mul/` 下有 `build.ninja`、`.cuda.o`、`.so`；
- 步骤 4 中 `clear-cache` 只清空 `cached_ops`，不动其余。

**预期结果**：你用一句话总结「JIT 只往家目录写，从不往包目录写」，并能解释这为什么让 flashinfer 能装在只读环境里。

> 「待本地验证」：步骤 3、4 依赖一次真实 JIT 编译；若本机无 GPU，可改为纯阅读：对照 [core.py:271-283](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L271-L283) 指认 `build_dir` 派生自 `JIT_DIR`（可写区），从而确证写入位置。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `core.py` 导入时只 `makedirs(WORKSPACE_DIR)`，而不 `makedirs(CSRC_DIR)`？

> **参考答案**：`CSRC_DIR` 在包安装目录内，可能是只读的（系统安装/容器镜像）。`core.py:18-20` 的注释明确警告这一点。工作区在用户家目录下，必然可写，故只创建它。这也呼应了 u1-l3 的「框架分离原则」——`csrc/` 是只读的事实源，运行时不应被触碰。

**练习 2**：`flashinfer clear-cache` 之后，下次调用同一 API 会重新生成 `generated/silu_and_mul.cu` 吗？

> **参考答案**：不一定。`clear_cache_dir` 只删 `cached_ops/`，不删 `generated/`（[core.py:111-115](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L111-L115)）。下次调用时，代码生成层会重新渲染并 `write_if_different`——若渲染结果与磁盘上的旧 `.cu` 相同，则不写（u2-l1 的 `write_if_different`），文件保持原样；若不同则覆盖。无论如何，所有 `.o`/`.so` 都没了，必须全量重编译。

---

### 4.4 JitSpec 的 AOT vs JIT 路径选择

#### 4.4.1 概念说明

u2-l1 讲 `build_and_load` 时提到「AOT 优先、否则 JIT」的短路，但没有展开「AOT 路径」到底指哪个文件、怎么判断它存在。本节把这个选择机制讲透。

`JitSpec` 把「一个模块的库文件」抽象成两个候选位置：

- **JIT 路径**：`FLASHINFER_JIT_DIR / name / f"{name}.so"`——现场编译的产物，在可写区。
- **AOT 路径**：`FLASHINFER_AOT_DIR / name / f"{name}.so"`——预编译包提供的产物，在只读区（可能来自 `flashinfer-jit-cache` 包或包内 `data/aot/`）。

选择规则非常简单：**AOT 路径文件存在就用它（`is_aot` 为真），否则用 JIT 路径。** 这意味着只要装了 `flashinfer-jit-cache` 且它提供了对应模块的 `.so`，FlashInfer 就完全跳过编译，直接加载预编译库——这是生产环境「启动快」的根本原因。

这条规则的妙处在于它**完全基于文件存在性**，不需要复杂的「装了哪个包」状态判断：`is_aot` 就是 `aot_path.exists()`。无论 AOT 目录解析到包内 `data/aot/` 还是 `flashinfer-jit-cache` 的目录，只要那里有 `.so`，就走 AOT。

#### 4.4.2 核心流程

`JitSpec` 在「取库路径」时的决策树：

```
get_library_path():
  if is_aot:                   # aot_path.exists()？
      return aot_path           # 预编译包提供 → 走 AOT
  else:
      return jit_library_path   # 否则 → 走 JIT 现场编译路径

is_compiled = get_library_path().exists()
  └─ AOT 命中：查 aot_path 是否存在
  └─ AOT 未命中：查 jit_library_path 是否存在

build_and_load():
  if is_aot:  load(aot_path)    # 短路：完全跳过编译
  else:       build() + load(jit_library_path)
```

几个值得注意的点：

- **`is_compiled` 的语义随 AOT/JIT 变化**：装了预编译包时，它查的是 AOT 路径；没装时查 JIT 路径。所以「编译状态」取决于你装没装预编译包——这解释了为什么同一台机器，装了 `flashinfer-jit-cache` 后 `module-status` 里大片模块立刻变「Compiled」。
- **AOT 短路发生在 `build_and_load` 最开头**，连 `FileLock`、`write_ninja`、`run_ninja` 都不进，零编译开销。
- **JIT 路径下，`build` 与 `load` 共用一把锁**（u2-l1 已讲），避免竞态。

#### 4.4.3 源码精读

两个候选路径的定义——注意它们分别派生自可写区与只读区：

[flashinfer/jit/core.py:236-238](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L236-L238) —— `jit_library_path = FLASHINFER_JIT_DIR / name / f"{name}.so"`：可写区的 JIT 产物。
[flashinfer/jit/core.py:255-257](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L255-L257) —— `aot_path = FLASHINFER_AOT_DIR / name / f"{name}.so"`：只读区的预编译产物。

「是否 AOT」与「取库路径」的实现都极薄：

[flashinfer/jit/core.py:259-265](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L259-L265) —— `is_aot = aot_path.exists()`；`is_compiled = get_library_path().exists()`。一切判断都退化为「文件在不在」，无任何状态机。

[flashinfer/jit/core.py:240-243](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L240-L243) —— `get_library_path`：AOT 命中返回 `aot_path`，否则 `jit_library_path`。整个 AOT/JIT 二选一就浓缩在这三行。

`build_and_load` 最开头的 AOT 短路（u2-l1 已展示，这里聚焦 AOT 视角）：

[flashinfer/jit/core.py:307-319](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L307-L319) —— 第 308-309 行：`if self.is_aot: return self.load(self.aot_path)`。装了预编译包时，这里直接返回，根本不进入 `FileLock`/`build` 分支。

CLI 的 `module-status` 正是借助 `get_library_path` 来报告「已编译/未编译」，而 `--filter aot` 能筛出 AOT 模块：

[flashinfer/jit/core.py:177-192](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L177-L192) —— `get_spec_status`：用 `spec.get_library_path() if spec.is_compiled else None` 得到库路径。注意它把 `library_path` 设为 `None`（当未编译），这是 CLI 显示「Not Compiled」的来源。

#### 4.4.4 代码实践

**目标**：源码阅读型实践——给定模块名，推算它的 AOT 路径与 JIT 路径，并解释装/不装预编译包时 `is_compiled` 的差异。

**操作步骤**：

1. 在 Python 里（不触发编译）查两个路径：
   ```bash
   python -c "
   from flashinfer.jit.core import gen_jit_spec
   from flashinfer.jit import env as e
   s = gen_jit_spec('demo_only_path', ['/tmp/nonexistent.cu'])
   print('AOT path :', s.aot_path)
   print('JIT path :', s.jit_library_path)
   print('is_aot   :', s.is_aot)
   print('lib path :', s.get_library_path())
   "
   ```
   （这个 spec 不会真的编译——`gen_jit_spec` 只组装配料表并登记，不碰 `sources` 指向的文件，也不编译；它唯一可能抛错的是开头对 [core.py:413](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L413) 的 `check_cuda_arch()`，若无任何 ≥sm75 的架构会报 `FlashInfer requires GPUs with sm75 or higher`。本机无 GPU 时可设 `FLASHINFER_CUDA_ARCH_LIST="8.0"` 再跑，绕过探测。）
2. 对照步骤 1 的输出，确认：AOT 路径在 `FLASHINFER_AOT_DIR / demo_only_path / demo_only_path.so`，JIT 路径在 `FLASHINFER_JIT_DIR / demo_only_path / demo_only_path.so`。
3. 推理：若你装了 `flashinfer-jit-cache` 且它提供了 `silu_and_mul.so`，那么 `silu_and_mul` 模块的 `is_aot` 会是 `True`，`build_and_load` 走第 308-309 行短路；若没装，`is_aot` 为 `False`，走 JIT 分支现场编译。
4. （需要环境，待本地验证）用 CLI 验证：`flashinfer module-status --filter aot`（若装了预编译包）应列出若干 AOT 模块；`--filter jit` 列出 JIT 模块。

**需要观察的现象**：

- 步骤 1 的 AOT 路径前缀是 `_package_root/data/aot`（或 jit-cache 包目录），JIT 路径前缀是家目录下的 `cached_ops`；
- `is_aot` 取决于那个 AOT 路径下同名 `.so` 是否存在。

**预期结果**：你能不看源码说出「同一个模块名 `X`，AOT 候选在 `<AOT_DIR>/X/X.so`、JIT 候选在 `<JIT_DIR>/X/X.so`，谁存在就用谁」，并能解释为什么装预编译包能让 `module-status` 里大片模块瞬间变「Compiled」。

> 「待本地验证」：步骤 4 依赖预编译包是否安装；步骤 1-3 是纯路径计算，不触发编译，但步骤 1 的 `gen_jit_spec` 会先跑 `check_cuda_arch()`（[core.py:413](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L413)），本机若无 ≥sm75 的 GPU 需设 `FLASHINFER_CUDA_ARCH_LIST="8.0"` 才能通过。

#### 4.4.5 小练习与答案

**练习 1**：你没装 `flashinfer-jit-cache`，但 `cached_ops/silu_and_mul/silu_and_mul.so` 已存在（之前 JIT 编译过）。此时 `is_aot` 和 `is_compiled` 分别是什么？

> **参考答案**：`is_aot` 为 `False`（`aot_path` 不存在）；`is_compiled` 为 `True`（`get_library_path()` 走 JIT 分支，返回 `jit_library_path`，该文件存在）。即「不是 AOT，但 JIT 缓存命中」。

**练习 2**：装了 `flashinfer-jit-cache` 后，为什么 `flashinfer module-status` 里很多模块的状态从「Not Compiled」变成「Compiled」，但你**并没有**真的编译过它们？

> **参考答案**：因为 `is_compiled` 判定的是 `get_library_path().exists()`，而装了预编译包后 `is_aot` 变 `True`、`get_library_path()` 返回 `aot_path`，预编译包已经把那些 `.so` 放在 `aot_path` 下，于是存在性检查通过。这些模块是「AOT 提供的已编译」，并非本机 JIT 编译的。这正是 u1-l4「登记 ≠ 编译」的延续：`is_compiled` 只代表「库文件就绪」，不区分来源。

**练习 3**：AOT 与 JIT 的 `.so` 文件名都叫 `{name}.so`，会不会因为同名而互相覆盖？

> **参考答案**：不会。两者在不同目录：AOT 在只读的 `FLASHINFER_AOT_DIR / name /`，JIT 在可写的 `FLASHINFER_JIT_DIR / name /`，子树完全分离（4.3 节的可写性二分）。且 `build_and_load` 在 AOT 命中时根本不会进入写 JIT 目录的分支，不存在覆盖。

## 5. 综合实践

把本讲的「路径全景 + 三档优先级 + 可写性 + AOT/JIT 选择」串起来，完成下面这个「**画出你本机的 JIT 目录地图并标注来源**」的任务。

**任务背景**：你的同事在一台新机器上抱怨「flashinfer 启动特别慢，每次都要编译」，而你不知道他装没装预编译包、工作区在哪。请用本讲的方法快速诊断。

**操作步骤**：

1. **画地图**：运行下面的「一键诊断」脚本，收集所有关键路径与状态：
   ```bash
   python -c "
   from flashinfer.jit import env as e
   from flashinfer.jit.core import jit_spec_registry
   print('== 根与缓存 ==')
   print('BASE_DIR  :', e.FLASHINFER_BASE_DIR)
   print('CACHE_DIR :', e.FLASHINFER_CACHE_DIR)
   print('WORKSPACE :', e.FLASHINFER_WORKSPACE_DIR)
   print('== 可写区 ==')
   print('JIT_DIR     :', e.FLASHINFER_JIT_DIR)
   print('GEN_SRC_DIR :', e.FLASHINFER_GEN_SRC_DIR)
   print('CUBIN_DIR   :', e.FLASHINFER_CUBIN_DIR)
   print('== 只读区 ==')
   print('AOT_DIR     :', e.FLASHINFER_AOT_DIR)
   print('CSRC_DIR    :', e.FLASHINFER_CSRC_DIR)
   print('INCLUDE_DIR :', e.FLASHINFER_INCLUDE_DIR)
   print('== 预编译包探测 ==')
   print('has cubin pkg   :', e.has_flashinfer_cubin())
   print('has jit-cache   :', e.has_flashinfer_jit_cache())
   "
   ```
2. **标注来源档位**：对照 4.2 节，在地图上为 `CUBIN_DIR` 和 `AOT_DIR` 各标注「命中第几档」（包 / 环境变量 / 兜底）。
3. **诊断「启动慢」**：基于「预编译包探测」结果给出结论——
   - 若两个布尔都为 `False`：机器没装任何预编译包，每次首次调用都要现场 JIT（慢是必然），建议装 `flashinfer-jit-cache`；
   - 若 `has_flashinfer_jit_cache` 为 `True`：AOT 应短路大部分模块，启动应快，慢可能来自「jit-cache 未覆盖的少数模块」或「版本不匹配导致降级」——检查 `FLASHINFER_DISABLE_VERSION_CHECK` 与版本一致性。
4. **打开一个 generated 目录（呼应本讲实践任务）**：定位 `FLASHINFER_GEN_SRC_DIR`，`ls` 其中一个子目录（如某个 activation 或 attention 模块名），观察里面的 `.cu`/`.inc` 文件。
   - 说明这些文件**如何产生**：它们由代码生成层（各 `gen_*_module`）用 Jinja 渲染后，经 `write_if_different` 写盘（u2-l1 的五步模式）。
   - 注意：`build.ninja` **不在 `generated/` 里**，而在 `FLASHINFER_JIT_DIR / <模块名> / build.ninja`（即可写区的 `cached_ops/` 下），由 `JitSpec.write_ninja` → `generate_ninja_build_for_op` 产生。请务必把「生成的源码（`generated/`）」与「构建脚本（`cached_ops/`）」分清，这是初学者最易混淆的点。
   - `ls` 一个 `cached_ops/<模块名>/`，确认 `build.ninja`、`.cuda.o`、`.so` 都在这里。
5. （需要 GPU 与一次成功编译，待本地验证）触发一次编译后，重做步骤 4，确认文件确实生成。

**预期产出**：一张标注了「根/缓存/可写区/只读区/来源档位」的本机目录地图，外加一段诊断结论。完成本任务后，你应该能在任何一台陌生机器上，用不到一分钟判断出「flashinfer 会从哪里读 kernel、会不会现场编译」，并能精准定位「启动慢」的根因。

## 6. 本讲小结

- `env.py` 把「逻辑意图」映射为磁盘路径：**可写区**（`FLASHINFER_CACHE_DIR` 下：`JIT_DIR`/`GEN_SRC_DIR`/cubin 缓存）派生自家目录，**只读区**（`_package_root/data/` 下：`INCLUDE_DIR`/`CSRC_DIR`/`AOT_DIR`/CUTLASS/SPDLOG/CCCL）派生自包目录。
- 工作区目录名 = `CACHE_DIR / <version> / <sorted_arch>`，`sorted()` 保证架构集合的目录名确定性，避免缓存碎片；`FLASHINFER_WORKSPACE_BASE` 可整体搬移工作区，但不影响只读区。
- `FLASHINFER_CUBIN_DIR` 与 `FLASHINFER_AOT_DIR` 走**优先级降级**：预编译包（`flashinfer-cubin`/`flashinfer-jit-cache`）> 环境变量（仅 cubin）> 默认目录；两者都带版本校验（cubin 严格 `==`、AOT 用 `startswith` 容忍 `+cuXXX` 后缀），可用 `FLASHINFER_DISABLE_VERSION_CHECK` 跳过，源码安装（`0.0.0+unknown`）自动跳过。
- **目录可写性红线**：`core.py` 导入时只创建工作区目录，刻意不创建可能只读的 `CSRC_DIR`；JIT 永远只往家目录写，包目录是只读事实源，可随时 `clear-cache` 重建而不影响包。
- `JitSpec` 的 AOT/JIT 选择极简：`is_aot = aot_path.exists()`，`get_library_path` 据此在只读区 AOT 与可写区 JIT 间二选一；`build_and_load` 在 AOT 命中时短路加载、零编译，这是「装预编译包后启动飞快」的根本机制；`is_compiled` 只看库文件存在性，不区分来源，故装包后大片模块瞬间变「Compiled」。

## 7. 下一步学习建议

本讲把「文件写在哪、从哪读」彻底搞清楚了，接下来建议按依赖顺序深入第 2 单元剩余三讲：

- **u2-l3（gen_*_module 代码生成模式）**：现在你知道生成的源码落在 `FLASHINFER_GEN_SRC_DIR`，下一步就该看「源码是怎么被生成出来的」——以一个**带类型特化**的生成器（如 norm 或 sampling）为例，精读 Jinja 渲染的完整五步，理解 `write_if_different` 在更复杂场景下如何避免无谓重编译。
- **u2-l4（编译上下文与 CUDA 架构目标）**：本讲反复出现的 `TARGET_CUDA_ARCHS` 与 `<sorted_arch>` 目录名，其来源正是 `CompilationContext`——它如何探测 GPU、如何读 `FLASHINFER_CUDA_ARCH_LIST`、`supported_major_versions` 如何限制编译范围，都在这里展开。
- **u2-l5（模块缓存与失效机制）**：本讲的 `is_aot`/`is_compiled` 只回答「库在不在」，而「什么时候会重新编译」要靠 URI 与两级缓存的失效机制——这是「实时重载」与「缓存命中」的终极一讲，建议在做过 u2-l3、u2-l4 后压轴阅读。

如果你想跳到实战，也可以先做 u3-l1（注意力基础），在真实 attention wrapper 上验证本讲的路径结论（比如观察一次 `plan`/`run` 在 `cached_ops/` 与 `generated/` 下产生了什么）；但建议至少先读完 u2-l4，否则对「为什么目录名里是这个架构段」的理解会有缺口。
