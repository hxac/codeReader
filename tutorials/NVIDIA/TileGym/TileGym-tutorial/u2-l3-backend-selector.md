# 后端选择与可用性 selector.py

## 1. 本讲目标

上一讲（u2-l2）我们看清楚了「分发器」是怎么把一次 `ops.softmax(x)` 调用路由到具体后端实现的。但分发器在查找实现时，有一个关键前提：**当前后端到底是哪一个？这个后端到底能不能用？**

这两个问题的答案全部由 `selector.py` 维护。本讲学完后，你应当能够：

- 说清楚 TileGym 对四个后端（`cutile` / `triton` / `tilecpp` / `cutile-rs`）分别用什么方式探测可用性，以及为什么探测策略各不相同；
- 掌握进程级当前后端状态 `_CURRENT_BACKENDS` 与 `set_backend()` 的切换逻辑；
- 理解 `CUTILE_TUTORIALS_BACKEND` 等环境变量如何在导入时决定初始后端；
- 理解 tilecpp 的 nvcc 版本检查为什么被设计成「延迟 + 缓存」；
- 列出影响后端选择与自动调优的全部环境变量及其含义。

## 2. 前置知识

### 2.1 「可用性」与「当前后端」是两件事

读到这里的读者，请先在脑海里分清两个完全不同的概念：

- **可用性（availability）**：这台机器上某个后端**能不能跑**。它取决于环境——`cuda.tile` 能不能 import、`nvcc` 版本够不够新、`cargo` 在不在 PATH 上。这是机器属性，不会因为你 `set_backend` 而改变。
- **当前后端（current backend）**：进程在**此刻**默认用哪个后端去执行算子。它是一个进程级的单值变量，初始为 `"cutile"`，可以被 `set_backend()` 或环境变量改写。

分发器查表时（u2-l2 的 `_REGISTRY`）用的是「当前后端」，而 `set_backend` / `is_backend_available` 会先校验「可用性」，二者通过 `_AVAILABLE_BACKENDS` 这个集合联系起来。

### 2.2 为什么每个后端的探测方式都不一样

四个后端的技术栈完全不同，因此「判断它能不能用」的成本也天差地别：

| 后端 | 实现语言 | 探测依据 | 成本 |
|------|---------|---------|------|
| `cutile` | Python（`@ct.kernel`） | 能否 `import cuda.tile` | 一次 import |
| `triton` | Triton / Tile-IR | 恒为 True（nvtriton 需额外开关） | 几乎为零 |
| `tilecpp` | CUDA Tile C++（`.cuh`） | 能否定位模块 + `nvcc` 版本 ≥ 13.3 | **要起子进程跑 `nvcc --version`** |
| `cutile-rs` | Rust（cdylib） | `cargo` 是否在 PATH 或预编译 `.so` 是否存在 | 文件系统查找 |

tilecpp 的探测最「重」，因为它要 fork 一个子进程调用 `nvcc --version`。正是这一点，决定了它必须被设计成延迟且缓存——本讲的核心问题之一。

### 2.3 Python 导入副作用

`selector.py` 末尾有两行「裸调用」：

```python
_initialize_available_backends()
_load_from_environment()
```

它们在模块被 import 时立即执行，把「探测可用性」和「从环境变量加载初始后端」变成导入副作用。理解这一点，才能理解为什么 `import tilegym` 之后 `_AVAILABLE_BACKENDS` 已经填好、`_CURRENT_BACKENDS` 已经定好。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/tilegym/backend/selector.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py) | 本讲主角。定义四个后端的可用性探测函数、进程级状态 `_AVAILABLE_BACKENDS` / `_CURRENT_BACKENDS`、`set_backend` / `get_current_backend` / `is_backend_available` 等公开 API。 |
| [src/tilegym/autotune.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/autotune.py) | 自动调优的全局开关。用单一环境变量 `TILEGYM_DISABLE_AUTOTUNE` 控制进程级调优策略。 |
| [src/tilegym/backend/dispatcher.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py) | 上一讲的主角。它**消费** selector 的 `get_current_backend()` 与 `is_tilecpp_available()`，是 selector 的主要调用方。 |
| [src/tilegym/backend/cutile_rs/utils.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py) | cutile-rs 的探测细节实现（`cargo` 判定、`.so` 新鲜度检查、autobuild）。`is_cutile_rs_available` 会复用它。 |

---

## 4. 核心概念与源码讲解

### 4.1 后端可用性探测：cuda.tile / nvcc / cargo 三种策略

#### 4.1.1 概念说明

「探测」要回答的问题是：在不真正运行一个内核的前提下，如何尽量便宜、尽量可靠地判断某个后端能不能跑。理想探测应当满足：

1. **便宜**：不要在 `import tilegym` 这种无关路径上拖慢启动；
2. **可靠**：探测通过，运行就该通过；探测失败，就该给出可读的修复提示；
3. **可关停**：允许用户/CI 强制把某个后端关掉（例如显式禁用 cuTile 来测 fallback）。

TileGym 对四个后端各下了一剂「探测猛药」，剂量与后端的依赖复杂度成正比。下面逐一拆解。

#### 4.1.2 核心流程

整个可用性探测在导入时由 `_initialize_available_backends()` 驱动，它调用 `_check_backends_availability()` 拿到一张 `{后端: 是否可用}` 的表，把可用的塞进 `_AVAILABLE_BACKENDS` 集合：

```python
def _check_backends_availability() -> Dict[str, bool]:
    availability = {
        "cutile": is_cutile_available(),
        "triton": True,
        "tilecpp": _TILECPP_MODULE_IMPORTABLE,   # 注意：这里只是「便宜检查」
        "cutile-rs": is_cutile_rs_available(),
    }
    return availability
```

注意一个关键设计：**tilecpp 在这里只做「便宜检查」**（`_TILECPP_MODULE_IMPORTABLE`），昂贵的 `nvcc --version` 并**没有**在这里跑。原因马上在 4.2 讲。先把四个探测函数看完。

#### 4.1.3 源码精读

**(a) cutile：一次 import 探测**

cuTile 是默认后端，它的依赖最简单——只要 `cuda.tile` 这个 Python 包能 import 即可。

[selector.py:L30-L44](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L30-L44) 在模块顶层用 try/except 包住 `import cuda.tile as ct` 和 `import cuda.tile.tune`，成功则置 `CUTILE_AVAILABLE = True`，失败则发一条 `UserWarning` 并置 `False`。注意 `cuda.tile.tune` 是**每个 `ops/cutile/` 下算子都需要的**，所以必须一起探测。

[selector.py:L47-L51](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L47-L51) 的 `is_cutile_available()` 在此基础上额外支持一个强制关停开关 `TILEGYM_DISABLE_CUTILE=1`，方便 CI 在「明明装了 cuTile、却想测 fallback」时把它关掉。

**(b) triton：恒为可用，但有两种「子口味」**

`_check_backends_availability()` 里 `"triton": True` 是写死的——triton 后端始终被视为可用。但 triton 内部其实有两种实现路径：`nvt`（nvtriton，走 Tile-IR）和 `oait`。`is_nvt_available()` 负责区分：

[selector.py:L20-L27](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L20-L27)：必须**同时**满足「能 import `triton.backends.tileir`」和「环境变量 `ENABLE_TILE` 恰好等于 `1`」才算 nvt 可用，否则 `get_available_triton_backend()` 返回 `"oait"`。注意 `int(os.environ.get("ENABLE_TILE", -1))` 在未设置时默认为 `-1`，即「未设置」等价于「不启用 nvt」。

**(c) tilecpp：先做便宜检查（nvcc 留到 4.2）**

[selector.py:L89-L113](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L89-L113) 的 `_check_tilecpp_module_importable()` 是「便宜检查」：它用 `importlib` 尝试加载 tilecpp 的 `_cuda_utils.py` 模块并验证其中存在 `TileCppKernel` 属性。关键点是它**不起任何子进程**，docstring 明确说它「safe to call at module load time even on hosts without nvcc / without CUDA」。其结果在导入时缓存到模块级常量 `_TILECPP_MODULE_IMPORTABLE`（[selector.py:L116](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L116)），供 `_check_backends_availability` 直接引用。

**(d) cutile-rs：看 cargo 或预编译 .so**

[selector.py:L149-L181](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L149-L181) 的 `is_cutile_rs_available()` 是「宽松探测」（代码注释称 "Permissive probe"）。它的逻辑是：

1. 先定位唯一的 cdylib crate 目录（找不到直接返回 False）；
2. 若 autobuild 被关掉（`CUTILE_RS_AUTOBUILD=0`）：只有已存在的 `.so` 才算可用；
3. 若 autobuild 开启（默认）：`cargo` 在 PATH 上就算可用（wrapper 会在 dispatch 时懒编译）；否则只在 `.so` 存在且「不陈旧」时才算可用。

这是一种「宽松」策略——它故意把 libclang / CUDA headers / tileiras 等「真正编译才需要」的依赖推迟到第一次 dispatch，避免在探测阶段过度判断。陈旧检查 `_so_stale` 的细节在 [cutile_rs/utils.py:L85-L98](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L85-L98)：只要任一 `.rs` / `.toml` 比 `.so` 新就算陈旧。

#### 4.1.4 代码实践：观察 get_available_backends

1. **实践目标**：直观看到「可用后端集合」与机器环境的对应关系。
2. **操作步骤**：在已安装 tilegym 的环境中运行：

   ```python
   import tilegym
   print("available:", tilegym.get_available_backends())
   print("current  :", tilegym.get_current_backend())
   ```

   再分别尝试 `TILEGYM_DISABLE_CUTILE=1 python -c "import tilegym; print(tilegym.get_available_backends())"`，观察 `cutile` 是否消失。
3. **需要观察的现象**：默认输出通常包含 `{'cutile', 'triton'}`，`tilecpp` / `cutile-rs` 是否出现取决于本机是否装了对应工具链；强制禁用后 `cutile` 不再出现在集合里。
4. **预期结果**：`get_available_backends()` 返回的正是 `_AVAILABLE_BACKENDS`（[selector.py:L218-L219](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L218-L219)），它只是这个集合的一个只读视图。**待本地验证**：具体集合内容随你的机器而变。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_check_backends_availability()` 里 tilecpp 的值用 `_TILECPP_MODULE_IMPORTABLE` 而不是直接调用 `is_tilecpp_available()`？

> **答**：因为 `is_tilecpp_available()` 内部会 fork 子进程跑 `nvcc --version`，代价高。若在导入时（每次 `import tilegym`）都跑，会让无 nvcc 的机器也承担子进程开销。所以导入时只做便宜检查，把昂贵检查推迟到「真正要 dispatch tilecpp」时（4.2 详述）。

**练习 2**：`is_cutile_rs_available()` 为什么被设计成「宽松探测」，把 libclang/CUDA headers 的校验留到 dispatch？

> **答**：探测的目标是「大概率能跑」，而不是「保证能编译成功」。cargo 在 PATH 上就认为可用，可以让 wrapper 在真正用到时才触发编译；若探测阶段就把 libclang 等都校验一遍，既慢又容易因为「装了但路径非标准」而误判不可用。宽松探测换来的是更少的误杀和更快的启动。

---

### 4.2 tilecpp 探测为何延迟且缓存：is_tilecpp_available 的设计

#### 4.2.1 概念说明

本模块直接回答本讲实践任务的第一问：**为什么 tilecpp 的 nvcc 版本检查被设计成「延迟（lazy）」且「缓存（cached）」？**

先看代价对比：

- cuTile 探测：一次 `import`，微秒级；
- cutile-rs 探测：几次文件系统 `stat`，毫秒级；
- **tilecpp 探测：fork 一个子进程执行 `nvcc --version`，几十到几百毫秒**，而且在没有 nvcc 的机器上还会失败、可能伴随 stderr 噪音。

如果把这个探测放在 `import tilegym` 的导入路径上，那么**所有用户**——包括那些只想用 cuTile、根本不打算碰 tilecpp 的用户——每次启动都要白白多等几百毫秒，甚至看到一条莫名其妙的 nvcc 警告。这是不可接受的开销。于是 TileGym 用两招化解它。

#### 4.2.2 核心流程

tilecpp 的可用性被拆成「两层」：

```
导入时（便宜）          dispatch/set_backend 时（昂贵，只跑一次）
─────────────         ──────────────────────────────
_check_tilecpp_        is_tilecpp_available()
module_importable()      ├─ 先看 _TILECPP_MODULE_IMPORTABLE（已是缓存）
  └ 不起子进程          ├─ 否则 _nvcc_version_supported()  ← fork nvcc
    只 import 模块       └  结果被 @functools.cache 永久缓存
```

两层 + 缓存共同保证了：**tilecpp 的子进程探测，最多在整个进程生命周期内执行一次，且仅当真的需要 tilecpp 时才执行。**

#### 4.2.3 源码精读

**第一层：便宜检查已在 4.1 讲过。** 它在导入时跑一次，结果存进 `_TILECPP_MODULE_IMPORTABLE`。

**第二层：昂贵的 nvcc 版本检查。** [selector.py:L57-L86](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L57-L86) 的 `_nvcc_version_supported()`：

1. 先解析 `nvcc` 路径——优先用环境变量 `TILECPP_NVCC_PATH`，否则用 PATH 上的 `nvcc`（`shutil.which`）；
2. 跑 `nvcc --version`，用正则 `release\s+(\d+)\.(\d+)` 抓版本号；
3. 与 `_TILECPP_MIN_NVCC = (13, 3)`（[selector.py:L54](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L54)）比较，要求 ≥ 13.3。

注意它对失败非常「安静」：路径找不到、子进程超时、版本正则不匹配，统统返回 `False`，不抛异常——因为这是「探测」，失败应当被优雅地解释为「不可用」而非崩溃。

**缓存与警告：** [selector.py:L119-L146](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L119-L146) 的 `is_tilecpp_available()` 用 `@functools.cache` 装饰，**保证昂贵的子进程在整个进程里最多跑一次**。它先把两层检查串起来（模块不可导入直接 False，否则才查 nvcc），失败时用 `warnings.warn(..., stacklevel=2)` 在**调用方**那一帧打出一条可读的修复提示（提示设置 `TILECPP_NVCC_PATH` 或安装 CUDA ≥ 13.3）。由于 `stacklevel=2` 加上 cache，这条警告也只会打一次。

**谁触发它？** 它不是被导入触发的，而是被两个「真正要用 tilecpp」的地方触发的：

- 分发器在 dispatch 到 tilecpp 时做兜底校验：[dispatcher.py:L91-L92](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L91-L92)「如果 tilecpp 不可用，就回退到 fallback」——这正是 u2-l2 讲过的「tilecpp 健康检查」决策点；
- `set_backend("tilecpp")` 与 `is_backend_available("tilecpp")` 都会调用它（见 4.3），让用户提前「快速失败」而不是等到 dispatch 才发现。

#### 4.2.4 代码实践：验证「延迟」与「缓存」

1. **实践目标**：用源码阅读 + 行为观察，确认 tilecpp 探测是「延迟到首次需要」且「只跑一次」。
2. **操作步骤**：
   - **阅读型实践（主要）**：打开三个文件对照——
     - [selector.py:L188-L195](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L188-L195)：确认 `_check_backends_availability()` 里 tilecpp 只取 `_TILECPP_MODULE_IMPORTABLE`，**没有**调用 `is_tilecpp_available()`，故导入时不会跑 `nvcc`；
     - [selector.py:L119](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L119)：确认 `@functools.cache` 保证只跑一次；
     - [dispatcher.py:L91-L92](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L91-L92)：确认首次 dispatch tilecpp 才触发它。
   - **行为型实践（可选，待本地验证）**：在装了 nvcc 的机器上，`python -c "import tilegym"` 后立即用 `strace -f -e trace=execve` 观察，应当**看不到** `nvcc --version`；只有在执行一次 tilecpp 算子后才出现一次。
3. **需要观察的现象**：导入阶段不出现 `nvcc` 子进程；首次 tilecpp dispatch 出现一次；再次 dispatch 不再出现。
4. **预期结果**：与 `@functools.cache` 语义一致——首次调用执行真实检查，后续调用直接返回缓存值。**待本地验证**：strace 结果取决于本机是否真有 nvcc。

#### 4.2.5 小练习与答案

**练习 1**：如果去掉 `@functools.cache`，会有什么后果？

> **答**：每次 dispatch 到 tilecpp 都会重新 fork `nvcc --version`。对于一个跑很多次 tilecpp 算子的训练/推理任务，这意味着每个算子都多花几十到几百毫秒在无谓的版本检查上，吞吐显著下降。cache 把这个开销摊销为「整个进程一次」。

**练习 2**：`_nvcc_version_supported()` 为什么把所有失败路径都写成 `return False` 而不是 `raise`？

> **答**：它是「探测函数」，语义是「能否找到合用的 nvcc」。机器上没装 nvcc、PATH 配错、子进程超时，这些都是「不可用」的正常分支，应当返回 `False` 让上层走 fallback；若 raise，会把环境问题变成程序崩溃，破坏「探测应当安静失败」的约定。

---

### 4.3 当前后端状态机：_CURRENT_BACKENDS、set_backend 与 CUTILE_TUTORIALS_BACKEND

#### 4.3.1 概念说明

可用性是「机器属性」，当前后端则是「进程状态」。`selector.py` 用两个模块级全局变量管理它：

```python
_AVAILABLE_BACKENDS: Set[str] = set()   # 哪些后端可用，导入时填好
_CURRENT_BACKENDS: str = "cutile"        # 此刻默认用哪个，进程级单值
```

（[selector.py:L184-L185](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L184-L185)。注意变量名虽以 `_S` 结尾其实是 `str` 类型，这是历史命名。）

当前后端有三个来源，优先级从高到低：

1. **调用级**：`ops.softmax(x, backend="triton")` 的显式 `backend=` 参数（u2-l1 已讲，最高优先级，且不改进程状态）；
2. **进程级**：`set_backend("triton")` 改写 `_CURRENT_BACKENDS`，影响后续所有调用；
3. **导入级**：环境变量 `CUTILE_TUTORIALS_BACKEND` 在 `import tilegym` 时决定初始值。

分发器在没有显式 `backend=` 时，就调 `get_current_backend()`（[dispatcher.py:L81](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L81)）读取进程级状态。

#### 4.3.2 核心流程

```
import tilegym
   │
   ├─ _initialize_available_backends()   填 _AVAILABLE_BACKENDS
   │
   └─ _load_from_environment()           读 CUTILE_TUTORIALS_BACKEND
          │
          ├─ 未设置 → _CURRENT_BACKENDS 保持 "cutile"
          ├─ 设置了且属于 _AVAILABLE_BACKENDS → 采用它
          └─ 设置了但不在集合里 → raise ValueError

运行时：
   set_backend(b)  ─→ 校验 b ∈ _AVAILABLE_BACKENDS ─→ tilecpp 再加做 is_tilecpp_available
                                                          ─→ 通过则 _CURRENT_BACKENDS = b
```

#### 4.3.3 源码精读

**(a) 导入级：环境变量决定初始后端**

[selector.py:L208-L215](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L208-L215) 的 `_load_from_environment()`：

```python
backend = os.environ.get("CUTILE_TUTORIALS_BACKEND", _CURRENT_BACKENDS)
if backend in _AVAILABLE_BACKENDS:
    _CURRENT_BACKENDS = backend
else:
    raise ValueError(f"Unknown backend: {backend}, available backends: {_AVAILABLE_BACKENDS}")
```

注意两点：一是环境变量名是历史遗留的 `CUTILE_TUTORIALS_BACKEND`（项目曾叫 cutile-tutorials），并非 `TILEGYM_BACKEND`；二是它必须出现在 `_AVAILABLE_BACKENDS` 里，否则导入直接抛错——比如本机没有 cargo 却设成 `cutile-rs`，`import tilegym` 就会失败。

**(b) 进程级：set_backend 的双重校验**

[selector.py:L232-L248](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L232-L248) 的 `set_backend()` 做了两层校验：

1. `backend not in _AVAILABLE_BACKENDS` → 抛错（挡住拼错或不可用的后端）；
2. 若是 tilecpp，**额外**调用 `is_tilecpp_available()` → 不可用也抛错。

第二层是 4.2 那个「快速失败」理念的应用：因为 `_AVAILABLE_BACKENDS` 里 tilecpp 的入场券只是便宜检查通过（`_TILECPP_MODULE_IMPORTABLE`），它并不能保证 nvcc 版本够新。所以 `set_backend("tilecpp")` 在此刻就把昂贵的 nvcc 检查跑一次（且被 cache），让用户**立刻**拿到清晰的报错，而不是等到第一个算子 dispatch 时才在 fallback 警告里发现。

[selector.py:L251-L261](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L251-L261) 的 `is_backend_available()` 沿用同样的「tilecpp 特判」逻辑：它对 tilecpp 会调用 `is_tilecpp_available()`，对其他后端只要在集合里就返回 True。这让测试可以写 `if is_backend_available("tilecpp"): ...` 来在没有 nvcc 的机器上自动跳过 tilecpp 用例。

**(c) 模块初始化的执行顺序**

[selector.py:L270-L271](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L270-L271) 是整条链的扳机：先 `_initialize_available_backends()`（填集合），再 `_load_from_environment()`（读环境变量，依赖集合已填好）。顺序不能反——否则环境变量校验时集合还是空的。

#### 4.3.4 代码实践：手动切换后端并观察状态

1. **实践目标**：验证 `_CURRENT_BACKENDS` 是进程级单值，且 `set_backend` 会对不可用后端报错。
2. **操作步骤**：

   ```python
   import tilegym
   print("init:", tilegym.get_current_backend())          # 通常 "cutile"

   tilegym.set_backend("triton")
   print("after set:", tilegym.get_current_backend())      # "triton"

   try:
       tilegym.set_backend("tilecpp")                      # 本机若无 nvcc≥13.3 会抛错
   except ValueError as e:
       print("rejected:", e)
   ```

3. **需要观察的现象**：`set_backend("triton")` 后，`get_current_backend()` 变成 `triton`，且影响**后续所有**未带 `backend=` 的算子调用；切到不可用的 tilecpp 时抛出带「nvcc ≥ 13.3 is required」提示的 `ValueError`。
4. **预期结果**：与源码两段校验一致。**待本地验证**：tilecpp 那一步的行为取决于本机 nvcc。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把 `set_backend("tilecpp")` 的 nvcc 检查省掉，让它在 dispatch 时自然回退？

> **答**：因为那样用户会以为已经切到 tilecpp 了，实际却静默回退到 cuTile/triton，调试时很难发现「我设的后端根本没生效」。`set_backend` 是用户的**显式意图**，在这里快速失败、给出清晰提示，比静默 fallback 体验好得多。这也正是它和「dispatch 时静默回退」（[dispatcher.py:L91-L92](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L91-L92)）的分工区别。

**练习 2**：变量 `_CURRENT_BACKENDS` 是 `str` 但名字带 `S`，是 bug 吗？

> **答**：是历史命名遗留——它曾经可能是个集合或计划支持「多后端并存」，但当前实现就是单个字符串。读源码时以类型注解 `str` 为准，不要被名字误导。

---

### 4.4 自动调优全局开关 autotune.py 与环境变量总览

#### 4.4.1 概念说明

`autotune.py` 是本讲第二份必读源码。它的职责很窄：用一个环境变量 `TILEGYM_DISABLE_AUTOTUNE` 控制**进程级**的自动调优（autotune）开关。自动调优指的是 cuTile/tilecpp 内核在多个候选配置（tile 大小、occupancy、num_ctas 等）里挑最快的那个——这个过程很慢（要逐个实测），所以需要能关掉。

这份文件之所以和 `selector.py` 放在一起讲，是因为它们共享同一种设计哲学：**用一个模块级、进程级的单一开关 + 一个统一查询函数，把「策略」和「读环境变量」彻底分离。** 业务代码（各个算子内核）永远只调 `is_autotune_enabled()` / `is_autotune_disabled()`，从不直接读环境变量。这一点在 docstring 里写得很硬：「Operator code must not read environment variables directly」。

#### 4.4.2 核心流程

```
内核代码：enable_autotune = is_autotune_enabled()
                              │
                              └─ not is_autotune_disabled()
                                      │
                                      └─ 读 TILEGYM_DISABLE_AUTOTUNE
                                            ├─ 未设置 / 0/false/no/off → 默认开启
                                            ├─ 1/true/yes/on           → 关闭
                                            └─ 非法值                  → raise ValueError
```

#### 4.4.3 源码精读

[autotune.py:L5-L7](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/autotune.py#L5-L7) 定义开关名和两组合法值：

```python
DISABLE_AUTOTUNE_ENV = "TILEGYM_DISABLE_AUTOTUNE"
_DISABLE_AUTOTUNE_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_DISABLE_AUTOTUNE_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
```

[autotune.py:L10-L33](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/autotune.py#L10-L33) 的 `is_autotune_disabled()` 有三个细节值得注意：

1. **未设置即默认开启**：`os.environ.get(...)` 为 `None` 时返回 `False`（即「不禁用」→ autotune 开），所以生产默认走自动调优；
2. **大小写/空白归一化**：`disable_flag.strip().lower()`，让 `YES`、` On ` 等写法都生效；
3. **非法值直接抛错**：既不在 true 集合也不在 false 集合时 raise，避免「拼错了却静默按默认处理」的隐患。

调用方遍布整个 cuTile 算子目录，例如 [src/tilegym/ops/cutile/attention.py:L759](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L759)、[src/tilegym/ops/cutile/rope.py:L202](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rope.py#L202) 都有 `if is_autotune_disabled(): ...` 分支——关闭时直接取搜索空间里的第一个合法配置，跳过耗时的实测挑选。

> 说明：autotune 的具体机制（按架构产出候选、exhaustive search、tune cache）属于 U5 的内容，本讲只关注它的「全局开关」。

#### 4.4.4 代码实践：列出影响后端与自动调优的环境变量

这是本讲实践任务的第二问。结合本讲读到的全部源码，把相关环境变量整理成下表（请逐条回到源码核对）：

| 环境变量 | 取值 | 作用 | 出处 |
|---------|------|------|------|
| `CUTILE_TUTORIALS_BACKEND` | 任意后端名 | 导入时决定初始当前后端，必须在 `_AVAILABLE_BACKENDS` 内 | [selector.py:L208-L215](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L208-L215) |
| `TILEGYM_DISABLE_CUTILE` | `1` | 强制把 cuTile 后端标记为不可用（测 fallback 用） | [selector.py:L48-L49](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L48-L49) |
| `ENABLE_TILE` | `1` | 启用 nvt（nvtriton / Tile-IR）triton 子后端；未设置或非 1 则用 oait | [selector.py:L27](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L27) |
| `TILECPP_NVCC_PATH` | nvcc 绝对路径 | 指定 tilecpp 探测与编译要用的 nvcc（优先于 PATH） | [selector.py:L68](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L68) |
| `DISABLE_FALLBACK` | `1` | 分发时禁止从当前后端回退到 fallback/default（只报错） | [dispatcher.py:L23-L25](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L23-L25)（u2-l2 已讲） |
| `CUTILE_RS_AUTOBUILD` | `0` 关闭 / 默认开启 | cutile-rs 是否在 `.so` 陈旧时自动 `cargo build` | [cutile_rs/utils.py:L78-L82](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L78-L82) |
| `CUTILE_RS_KERNELS_DIR` | 目录路径 | 覆盖 cutile_kernels crate 的位置 | [cutile_rs/utils.py:L60](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L60) |
| `TILEGYM_DISABLE_AUTOTUNE` | `1/true/yes/on` 关 / `0/false/no/off` 开 | 进程级自动调优总开关；非法值会抛错 | [autotune.py:L5-L33](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/autotune.py#L5-L33) |

操作步骤：

1. 把上表当作「核对清单」，用 `Grep` 或编辑器在 `src/tilegym` 下逐个搜索这些变量名，确认它们确实只在你看到的这一处被读取（业务代码不直接读）。
2. 重点验证 `TILEGYM_DISABLE_AUTOTUNE`：搜索会发现几十处 `is_autotune_disabled()` / `is_autotune_enabled()` 调用，但 `os.environ.get("TILEGYM_DISABLE_AUTOTUNE")` **只在 autotune.py 出现一次**——这正是「策略集中、读法唯一」设计的证据。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `autotune.py` 禁止业务代码直接 `os.environ.get("TILEGYM_DISABLE_AUTOTUNE")`？

> **答**：为了让「解析合法值 / 大小写归一 / 非法值报错」这套逻辑只存在一份。如果几十个内核各自读环境变量，必然出现「有的识别 `YES`、有的不识别」「有的对拼错静默按默认、有的报错」等不一致。集中到 `is_autotune_disabled()` 一个函数，行为统一、好维护、好测试。

**练习 2**：`TILEGYM_DISABLE_AUTOTUNE=tru`（拼错）会发生什么？和 `TILEGYM_DISABLE_CUTILE=tru` 的行为有何不同？

> **答**：前者会 `raise ValueError`（[autotune.py:L32-L33](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/autotune.py#L32-L33)，因为 `tru` 不在任何合法集合里）；后者则**不会**生效——[selector.py:L48](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L48) 只用 `== "1"` 精确比较，`tru` 不等于 `"1"`，于是 cuTile 不会被禁用、也不报错。两个开关的「严格度」不同，autotune 的更严。

---

## 5. 综合实践：画出一次「切后端并执行算子」的完整决策链

把本讲和 u2-l2 串起来，做一次端到端的追踪。请准备一张纸或一个文本文件，画出从「设置环境变量」到「内核真正执行」的完整决策链，并标注每一步用的是哪个函数、读了哪个全局变量/环境变量。

场景：环境为 `CUTILE_TUTORIALS_BACKEND=triton TILEGYM_DISABLE_AUTOTUNE=1`，然后运行：

```python
import tilegym                       # 步骤 A：导入
y = tilegym.ops.softmax(x)           # 步骤 B：调用算子（x 是 CUDA 张量）
```

请按顺序回答/画出：

1. **步骤 A 的导入副作用**：`_initialize_available_backends()` 对四个后端各调了哪个函数？其中 tilecpp 走的是便宜检查还是 nvcc 检查？`_AVAILABLE_BACKENDS` 最终包含哪些后端？接着 `_load_from_environment()` 读到 `CUTILE_TUTORIALS_BACKEND=triton`，`_CURRENT_BACKENDS` 被设成什么？
2. **步骤 B 的分发**：分发器 `wrapper` 先 `kwargs.pop("backend")`（本例为 None），于是调用 `get_current_backend()`（[dispatcher.py:L81](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L81)）拿到什么？由于不是 tilecpp，跳过 `is_tilecpp_available()`；然后在 `_REGISTRY["softmax"]` 里查 `triton` 键（结合 u2-l2，softmax 的 `fallback_backend="pytorch"`，且 triton 是否注册了 softmax 实现取决于该后端是否被 import）。若查到则直接执行，否则走 fallback 链。
3. **autotune 的作用点**：如果执行路径最终落到某个 cuTile/tilecpp 内核，内核内部会调 `is_autotune_enabled()`——本例因 `TILEGYM_DISABLE_AUTOTUNE=1` 返回 False，于是跳过实测挑选、直接用首个候选配置。

完成后，你应当得到一张包含「环境变量 → selector 全局变量 → dispatcher 查表 → 内核 autotune 分支」四层的完整流程图。这正好把 u2-l1（接口）、u2-l2（分发）、u2-l3（状态）三讲连成一条线。

> 提示：第 2 步里 softmax 是否有 triton 实现，需要你实际去 `src/tilegym/ops/triton/` 下确认（结合 u1-l4 的目录地图）。如果当前没有，就走 fallback——这正是 `fallback_backend` 设计的意义。

---

## 6. 本讲小结

- **可用性 ≠ 当前后端**：前者是机器属性（四个后端各有探测策略），后者是进程级单值 `_CURRENT_BACKENDS`，初始 `"cutile"`。
- **四种探测策略差异巨大**：cutile 靠一次 import、triton 恒为可用（nvt 需 `ENABLE_TILE=1`）、cutile-rs 看 cargo 或预编译 `.so`、tilecpp 最重（要跑 `nvcc --version`）。
- **tilecpp 探测刻意延迟且缓存**：导入时只做便宜的模块可导入检查（`_TILECPP_MODULE_IMPORTABLE`），昂贵的 `nvcc --version` 推迟到首次 dispatch/set_backend 时用 `is_tilecpp_available()`（`@functools.cache`）跑，且整个进程最多一次。
- **当前后端有三层优先级**：调用级 `backend=` > 进程级 `set_backend` > 导入级 `CUTILE_TUTORIALS_BACKEND`；`set_backend` 对 tilecpp 会额外做 nvcc 校验以实现「快速失败」。
- **`get_available_backends()` 是只读视图**：直接返回导入时填好的 `_AVAILABLE_BACKENDS` 集合。
- **autotune 全局开关集中管理**：`TILEGYM_DISABLE_AUTOTUNE` 是唯一开关，业务代码只调 `is_autotune_enabled()`，从不直接读环境变量；这和 selector 的「策略集中」是同一种工程哲学。

## 7. 下一步学习建议

至此，U2「统一算子接口与后端调度」全部讲完：你已经掌握了**接口层**（u2-l1 的 `ops.py` stub）、**分发层**（u2-l2 的 `_REGISTRY` + `dispatch` wrapper）、**状态层**（本讲的 selector + autotune 开关）。这三层构成了 TileGym「同一算子名、多后端实现」的完整骨架。

接下来建议进入 **U3「cuTile 内核编程模型」**，从 [u3-l1 cuTile 内核基础](u3-l1-cutile-kernel-basics.md) 开始。U2 回答的是「调用如何被路由」，U3 回答的是「被路由到的 cuTile 实现内部到底怎么写」——也就是 `@ct.kernel`、`ct.bid`/`ct.num_blocks`、`ct.launch` 这些本讲反复提到的「具体后端实现」长什么样。届时你会发现，U2 讲的 `is_autotune_disabled()` 分支正好对应 U5（[u5-l3 自动调优机制](u5-l3-autotuning.md)）里内核挑选配置的入口，可以前后对照阅读。
