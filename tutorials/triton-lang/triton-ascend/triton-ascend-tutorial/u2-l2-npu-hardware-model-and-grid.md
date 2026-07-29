# NPU 硬件模型与 Grid 核心分配

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚昇腾 NPU「AI Core」内部的两种计算单元（Cube Core 做矩阵、Vector Core 做向量）如何分工，以及「Vector 核数 = AI 核数 × 2」这条关系是怎么来的。
- 解释 Triton 的 `grid` 在 NPU 上为何是「物理核心绑定」而非 GPU 那样的「逻辑维度」，并能按算子类型（纯 vector vs 含 `tl.dot`）选择正确的并发任务数。
- 理解 `coreDim ≤ 65535` 这一硬上限，以及 `TRITON_ALL_BLOCKS_PARALLEL` / AutoBlockify 如何在编译期和运行时**协同**地把大于物理核数的逻辑 grid「折叠」到物理核数上，从而突破 65535 限制。

本讲承接 [u2-l1](u2-l1-python-side-migration.md)：那里我们只动了 Python 宿主侧的 device 替换，验证「能跑且结果正确」；本讲进入迁移指南的第二步——**调整 Grid 核心分配**（Adjust Grid Core Allocation），这是从「能跑」迈向「跑得对、跑得满」的关键一步。

## 2. 前置知识

- **GPU 的 grid 模型（对照基准）**：在 NVIDIA GPU 上，Triton 的 `grid=(n,)` 表示 `n` 个「逻辑 block」，硬件的 SM（Streaming Multiprocessor）会自动把这些 block 调度上去，一个 SM 可以跑多个 block，block 数和 SM 数没有硬性的 1:1 绑定关系。你可以把 grid 想象成「任务的逻辑份数」，硬件负责摊开。
- **program / block / 核**：在 Triton 里，`grid` 决定开启多少个 program，每个 program 对应一个 block（block 就是「一次 kernel 执行的最小调度单元」）。`tl.program_id(axis=0)` 让每个 program 拿到自己的编号，各处理一块数据。这些术语在 [u1-l4](u1-l4-first-kernel-vector-add.md) 已建立。
- **`tl.dot` 与算子分类**：`tl.dot` 是矩阵乘的核心算子。按是否含 `tl.dot`，Triton-Ascend 把 kernel 归类为不同类型，进而决定它跑在哪种计算核上。本讲会反复用到「纯 vector 算子」「含 `tl.dot` 的算子」这个二分。

> 一句话直觉：GPU 上 grid 是「逻辑任务份数，硬件随便摊」；NPU 上 grid 更接近「物理核的占用份数，得按算子类型数着核来排」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `docs/en/migration_guide/migrate_from_gpu.md` | 迁移指南，定义「Adjust Grid Core Allocation」步骤与 coreDim 限制解法。 |
| `docs/en/migration_guide/architecture_difference.md` | Ascend 与 GPU 的开发差异，给出「物理核心绑定」模型与 AutoBlockify 详解。 |
| `docs/en/programming_guide/cube_operator.md` | Cube 算子（`tl.dot`）开发示例，可作为「含 dot 算子」的样例。 |
| `third_party/ascend/backend/driver.py` | 运行时驱动，含 `NPUUtils`（核数探测）与生成 launcher 时计算 `num_physical_blocks`、做 grid 截断的核心逻辑。 |
| `third_party/ascend/backend/npu_utils.cpp` | 编译为 `npu_utils.so` 的 C 扩展，通过 CANN runtime API 探测 arch 与 AI 核数。 |
| `third_party/ascend/backend/compiler.py` | 编译后端，从 Linalg IR 解析出 `mix_mode`（aiv/aic/mix），并在编译期下发 `--enable-auto-blockify-loop`。 |
| `third_party/ascend/backend/utils.py` | `_is_auto_map_parallel_blocks_enabled()` 读取 `TRITON_ALL_BLOCKS_PARALLEL`，以及 AutoBlockify 的「黑名单算子」规则。 |

## 4. 核心概念与源码讲解

### 4.1 AI Core 与 Vector Core：昇腾 NPU 的计算单元模型

#### 4.1.1 概念说明

昇腾 NPU 的一个 **AI Core**（AI 核）并不是「一种」计算单元，而是**一组**计算单元的集合，内部至少包含两类：

- **Cube Core（矩阵核/立方核）**：专做矩阵乘法（GEMM），吞吐极高，是 `tl.dot` 这类算子的归宿。
- **Vector Core（向量核）**：专做逐元素向量运算（加减乘除、激活函数、规约等），对应 `tl.load`/`tl.store` 之外的纯 elementwise/规约逻辑。

一个物理 AI 核内部，Cube 与 Vector 是**协同**工作的：矩阵部分交给 Cube，前后处理交给 Vector。这正是后面 [u8](../manifest.json) 要讲的「Cube-Vector 融合」的基础。

关键的数量关系是：**每颗 AI 核里，Vector 通道数通常是 Cube 通道数的 2 倍**，所以在软件层面，Triton-Ascend 把「Vector 核数」定义为「AI 核数 × 2」。这条 2 倍关系不是文档约定，而是写在探测代码里的——下文源码会看到 `num_aiv = num_aic * 2`。

#### 4.1.2 核心流程

核数探测与暴露给上层的流程：

```text
CANN runtime (rtGetAiCoreCount)        ← 物理上真实存在的 AI 核数 N
        │
        ▼
npu_utils.cpp: get_aicore_num()        ← 把 N 返回给 Python
        │
        ▼
driver.py: NPUUtils.get_device_properties()
   num_aic = N                          ← AI 核数
   num_aiv = N * 2                      ← Vector 核数（= AI 核数 × 2）
        │
        ▼  （可选）NPU_DEVICE_LIMIT 环境变量裁剪
   num_aic' , num_aiv'  (≤ 硬件上限)
        │
        ▼
上层 grid 规划 / num_physical_blocks 截断使用
```

`NPU_DEVICE_LIMIT` 是一个可选的「裁剪」开关：多租户、性能调优或资源隔离场景下，你可以让 Triton「以为」核数比实际少，格式为 `cube_core_num,vector_core_num`（如 `14,28`）。

#### 4.1.3 源码精读

**① C 扩展通过 CANN API 拿到真实 AI 核数**

[npu_utils.cpp:135-148](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L135-L148) 调用 CANN runtime 的 `rtGetAiCoreCount`，把核数作为 `uint32_t` 返回（`get_arch` 则用 `rtGetSocVersion` 拿到 SoC 型号字符串，如 `Ascend910B3`）：

```cpp
static PyObject *getAiCoreNum(PyObject *self, PyObject *args) {
  uint32_t aiCoreCnt;
  rtError_t rtRet = rtGetAiCoreCount(&aiCoreCnt);
  ...
  return Py_BuildValue("I", aiCoreCnt);
}
```

这段 C 代码会被 `NPUUtils.__init__` 编译成 `npu_utils.so` 并动态加载（编译机制见 [u5-l1](u5-l1-npu-driver-and-utils.md)）。

**② Python 侧派生出 Vector 核数，并处理 NPU_DEVICE_LIMIT**

[driver.py:132-136](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L132-L136) 的 `get_device_properties` 是「2 倍关系」的源头——它先算出 `num_aiv = num_aic * 2`，再交给裁剪逻辑：

```python
def get_device_properties(self, device):
    num_aic, num_aiv = self._get_npu_device_limit_form_env()
    return {"max_shared_mem": 1, "num_aicore": num_aic, "num_vectorcore": num_aiv}
```

而 `_get_npu_device_limit_form_env` 的「无环境变量」分支明确写出默认关系 [driver.py:100-104](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L100-L104)：

```python
npu_device_limit_str = os.getenv("NPU_DEVICE_LIMIT")
num_aic = self.get_device_aicore()
num_aiv = num_aic * 2          # ← Vector 核数 = AI 核数 × 2
if npu_device_limit_str is None:
    return num_aic, num_aiv
```

若设置了 `NPU_DEVICE_LIMIT`，则会校验格式 `^\d+ *, *\d+$`、两个值都为正、且不超过硬件上限，否则抛 `ValueError` [driver.py:106-123](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L106-L123)。最终 `get_aicore_num()` / `get_aivector_core_num()` 分别返回这两个值 [driver.py:142-147](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L142-L147)。

**③ 环境变量参考文档对 NPU_DEVICE_LIMIT 的描述**

[environment_variable_and_compiler_options_reference.md:49](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/environment_variable_and_compiler_options_reference.md#L49) 把它归入「运行调度类」，格式为逗号分隔的 `cube_core_num,vector_core_num`。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「Vector 核数 = AI 核数 × 2」，并理解 `NPU_DEVICE_LIMIT` 如何裁剪核数。

**操作步骤**（源码阅读型，无需设备；带设备部分标注待本地验证）：

1. 在脚本里导入并实例化探测类：

```python
# 示例代码（需在装好 torch_npu / CANN 的 NPU 环境运行）
from triton.backends.ascend.driver import NPUUtils
u = NPUUtils()
print("arch        =", u.get_arch())          # 如 Ascend910B3
print("AI 核数     =", u.get_aicore_num())     # num_aic
print("Vector 核数 =", u.get_aivector_core_num())  # 应 == num_aic * 2
```

2. （可选）设置 `export NPU_DEVICE_LIMIT=14,28` 后再跑，观察打印是否变成 `14` 与 `28`，并验证 `num_aiv == num_aic * 2` 仍成立。

**需要观察的现象 / 预期结果**：`Vector 核数` 恰好是 `AI 核数` 的两倍；设置 `NPU_DEVICE_LIMIT` 后两个值被裁剪到你指定的值。

> 待本地验证：实际核数因硬件代际而异（如 Atlas 800T A2 系列），请在你的设备上确认具体数值。若当前环境无 NPU，可改为阅读 `_get_npu_device_limit_form_env` 的返回逻辑来理解关系。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Triton-Ascend 要把「Vector 核数」单独暴露为「AI 核数 × 2」，而不是直接用一个核数？

> **参考答案**：因为不同算子占用的是 AI 核内不同的计算通道。纯 vector 算子只用 Vector 通道（数量翻倍），所以并发任务数可以按 Vector 核数规划；含 `tl.dot` 的算子要占用 Cube 通道，只能按 AI 核数规划。单一核数无法区分这两种并发上限。

**练习 2**：若硬件实际 AI 核数为 24，有人设置 `NPU_DEVICE_LIMIT=30,48`，会发生什么？

> **参考答案**：会抛 `ValueError`。因为 `num_aic_env=30 > 24`（硬件上限），违反了「不得超过设备实际属性」的校验规则（见 [driver.py:114-117](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L114-L117)）。

---

### 4.2 Grid 规划：按算子类型映射并发任务数

#### 4.2.1 概念说明

这是迁移指南里最核心的一条建议，原文是：

> For Vector-only operators, organize concurrent tasks around the **Vector Core count**. For operators containing `tl.dot`, organize concurrent tasks around the **AI Core count**.

翻译成操作：**grid 的大小（并发任务数）要按算子类型对齐到不同的物理核数**。原因是 NPU 采用「强物理核心绑定」模型——与 GPU「逻辑维度 + 硬件自动摊开」不同，NPU 上 grid 直接对应物理核的占用，每个核一次只跑一个 block（但可以被重复调度）。

迁移指南用一张表点明了这种差异 [migrate_from_gpu.md:26-29](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L26-L29)：纯 vector 算子并发数取 Vector 核数，含 `tl.dot` 的算子并发数取 AI 核数；而 GPU 的并发通常由编译器和硬件决定。

为了在代码里表达「这个 kernel 属于哪一类」，Triton-Ascend 用了 `mix_mode` 这个元数据，取值有三种：

| `mix_mode` | 含义 | 算子特征 |
|------------|------|----------|
| `aiv` | AI-Vector，纯向量 | 无 `tl.dot`（load/store/elementwise/规约） |
| `aic` | AI-Cube，纯矩阵 | 仅矩阵乘为主 |
| `mix` | Cube-Vector 融合 | 既有 Cube 又有 Vector（如带非平凡 epilogue 的 matmul） |

#### 4.2.2 核心流程

「算子类型 → 并发任务数上限」的映射，发生在生成 launcher 的阶段：

```text
编译期: Linalg IR 里带 mix_mode 属性
        │  compiler.py: _parse_linalg_metadata 用正则提取
        ▼
   metadata["mix_mode"] ∈ {aiv, aic, mix}
        │
        ▼  传给 make_launcher
运行期: driver.py 计算
   num_physical_blocks = Vector核数   if mix_mode=="aiv"
                      = AI核数        otherwise   ← aic / mix 都走 AI 核数
        │
        ▼
   作为 grid 截断 / 警告的「物理上限」注入生成的 C++ launcher
```

注意两点：
1. **优先 1D grid**：迁移指南建议优先用 1D grid；若写成 2D（如 `(4,5)`），运行时会把它**合并成等价的 1D**（等价于 `(20,)`）。
2. `aic` 和 `mix` 在「并发上限」上**都按 AI 核数**算（只有 `aiv` 走 Vector 核数）。

#### 4.2.3 源码精读

**① 编译期：从 Linalg IR 正则解析出 mix_mode**

[compiler.py:372-399](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L372-L399) 用两条正则从 Linalg IR 里抓出 `mix_mode` 与 `parallel_mode`：

```python
# Example: mix_mode = "aiv" -> aiv
MIX_MODE_REGEX = r'mix_mode\s*=\s*"([^"]+)"'
...
metadata["mix_mode"] = re.search(MIX_MODE_REGEX, linalg).group(1)
metadata["parallel_mode"] = re.search(PARALLEL_MODE_REGEX, linalg).group(1)
```

> 特例：当输入是裸 TTIR（走 `ttir_to_npubin` 纯 SIMT 路径）时，[compiler.py:432](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L432) 直接假定 `metadata["mix_mode"] = "aiv"`——因为该路径目前只支持 vector kernel。

**② 运行期：mix_mode 决定物理块上限**

这是本模块最关键的一行代码，[driver.py:547](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L547)：

```python
num_physical_blocks = npu_utils.get_aivector_core_num() if mix_mode == "aiv" else npu_utils.get_aicore_num()
```

读法：纯 vector（`aiv`）→ 用 Vector 核数（= AI 核数 × 2）；其余（`aic`/`mix`）→ 用 AI 核数。这正是迁移指南「按算子类型选并发数」的代码实现。`num_physical_blocks` 随后会被注入到生成的 C++ launcher，用作截断与告警阈值（详见 4.3）。

**③ grid 维度被乘成单一 block 数（2D 自动合并为 1D）**

生成的 launcher 里，[driver.py:913](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L913) 直接把三个维度相乘：

```cpp
// only 1D parallelization is supported for NPU
uint32_t blockNum = gridX * gridY * gridZ;
```

这行注释 `only 1D parallelization is supported` 就是「2D/3D grid 会被合并成等价 1D」的实现依据——`(4,5,1)` 的 `blockNum` 与 `(20,1,1)` 完全相同。

**④ launch 参数即「占用核数」**

[architecture_difference.md:20-24](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/architecture_difference.md#L20-L24) 给出最直接的用法：`triton_gelu[n, 1, 1](...)` 里第一个参数 `n` 就是「启用 n 个核」，并提醒「在没有 auto-blockify 时，grid 里的核数不得超过 65535」。

#### 4.2.4 代码实践

**实践目标**：用一个含 `tl.dot` 的 kernel（cube/mix）和一个纯 vector kernel（aiv），分别体会「按 AI 核数」与「按 Vector 核数」规划 grid。

**操作步骤**：

1. 准备两个 kernel（均来自项目示例，含 `tl.dot` 的矩阵乘取自 [cube_operator.md:16-42](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/programming_guide/cube_operator.md#L16-L42)）：

```python
# 示例代码：矩阵乘（含 tl.dot，属 cube/mix）
# 来自 docs/en/programming_guide/cube_operator.md
@triton.jit
def matmul_kernel(a_ptr, b_ptr, c_ptr, M, N, K, ...,
                  BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr, BLOCK_K: tl.constexpr):
    pid = tl.program_id(0)          # 1D grid
    num_pid_n = tl.cdiv(N, BLOCK_N)
    pid_m = pid // num_pid_n
    pid_n = pid % num_pid_n
    ...
    acc = tl.dot(a, b, acc)          # ← 含 tl.dot
    ...

# 启动：grid 大小 = 输出分块数；并发上限按 AI 核数
grid = (triton.cdiv(M, BLOCK_M) * triton.cdiv(N, BLOCK_N), )
matmul_kernel[grid](...)
```

```python
# 示例代码：逐元素 GELU（纯 vector，属 aiv）
@triton.jit
def gelu_kernel(in_ptr, out_ptr, NUMEL: tl.constexpr):
    idx = tl.arange(0, NUMEL)
    x = tl.load(in_ptr + idx)
    ret = x * 0.5 * (1.0 + tl.erf(x / tl.sqrt(2.0)))   # 无 tl.dot
    tl.store(out_ptr + idx, ret)
```

2. 在启动前，先按本设备的核数预判并发上限：含 `tl.dot` 的 matmul 上限 = `get_aicore_num()`；纯 vector 的 GELU 上限 = `get_aivector_core_num()`。
3. 把 matmul 的 grid 故意设成略大于 AI 核数（仍 ≤ 65535），再设 `export TRITON_GRID_WARN_PRINT=1` 跑一次。

**需要观察的现象 / 预期结果**：matmul 在 grid > AI 核数时，标准错误会打印类似 `WARNING: Grid <N> > physical limit <AI核数>, performance maybe reduced.`（告警逻辑见 4.3 源码）；纯 vector kernel 的告警阈值则是 Vector 核数。

> 待本地验证：告警阈值取 AI 核数还是 Vector 核数取决于该 kernel 的 `mix_mode`，请在设备上分别跑两个 kernel 比对告警里的 `physical limit` 数值。

#### 4.2.5 小练习与答案

**练习 1**：同一个矩阵乘 kernel，分别用 `grid=(20,)` 和 `grid=(4,5)` 启动，结果是否等价？为什么？

> **参考答案**：等价。因为生成的 launcher 用 `blockNum = gridX * gridY * gridZ` 把多维 grid 合并成单一 block 数（[driver.py:913](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L913)），`(4,5)` 与 `(20,)` 的 blockNum 相同。这也是迁移指南建议「优先 1D」的原因。

**练习 2**：一个「matmul + softmax epilogue」的 kernel，其 `mix_mode` 最可能是哪个？它的并发任务数上限该按哪种核数算？

> **参考答案**：最可能是 `mix`（Cube-Vector 融合）。按 [driver.py:547](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L547) 的逻辑，`mix` 走 `else` 分支，并发上限 = AI 核数（不是 Vector 核数）。

---

### 4.3 TRITON_ALL_BLOCKS_PARALLEL 与 AutoBlockify：突破 65535 的逻辑块上限

#### 4.3.1 概念说明

NPU 的「物理核心绑定」带来一个硬上限：**`coreDim`（即 block 数）不能超过 `UINT16_MAX = 65535`**。迁移指南的 FAQ 把典型报错写作 `coreDim=xxxx can't be greater than UINT16_MAX`。

当数据规模很大、`BLOCK_SIZE` 又小，分块数很容易超过 65535。例如 \(N = 1073741824\)、`BLOCK_SIZE = 2048` 时：

\[
\text{coreDim} = \left\lceil \frac{N}{\text{BLOCK\_SIZE}} \right\rceil = \left\lceil \frac{1073741824}{2048} \right\rceil = 524288 \gg 65535
\]

有两种解法（见 [migrate_from_gpu.md:149-181](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L149-L181)）：

- **解法 A：开 `TRITON_ALL_BLOCKS_PARALLEL=1`**，启用 AutoBlockify，让大于物理核数的逻辑 grid「自动折叠」。
- **解法 B：增大 `BLOCK_SIZE`**，把分块数压到 65535 以内。由约束 \(\lceil N/\text{BLOCK\_SIZE}\rceil \le 65535\) 解得 \(\text{BLOCK\_SIZE} \ge \lceil N/65535\rceil\)，再向上取到 2 的幂。

AutoBlockify 的本质（[architecture_difference.md:26-41](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/architecture_difference.md#L26-L41)）：编译期用一个 `scf.for` 把 kernel body 包起来，让**每个物理 block 在循环里遍历多个逻辑 block id**；运行期则把传给 launcher 的 block 数从「逻辑 grid」截断到「物理核数」。两侧用同一份开关元数据保持同步——「绝不会出现编译期按一种模式、运行期按另一种模式启动」。

> **重要前提**：AutoBlockify 要求逻辑 block **对执行顺序不敏感**（因为循环会按 chunk 顺序访问逻辑块 id）。含跨块强顺序假设（如依赖特定顺序的跨块同步）的 kernel 不能用它，否则可能死锁。

#### 4.3.2 核心流程

AutoBlockify 的「编译期 + 运行期」双协同：

```text
环境变量 TRITON_ALL_BLOCKS_PARALLEL  +  黑名单算子检查
        │
        ├──► 编译期 (compiler.py)
        │      下发 --enable-auto-blockify-loop 给 bishengir-compile
        │      → kernel body 被 scf.for 包裹，每物理块遍历多逻辑块
        │
        └──► 运行期 (driver.py 生成的 launcher)
               blockNum = min(blockNum, num_physical_blocks)
               → 把逻辑 grid 截断到物理核数
```

两者用同一个开关 `_is_auto_map_parallel_blocks_enabled()`（读环境变量）+ `has_auto_blockify_blacklist_op`（黑名单）来 gating，因此永远一致。

#### 4.3.3 源码精读

**① 开关：环境变量默认值的真实行为**

[utils.py:349-350](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L349-L350)：

```python
def _is_auto_map_parallel_blocks_enabled() -> bool:
    return os.getenv("TRITON_ALL_BLOCKS_PARALLEL", "true").lower() in ("true", "1")
```

注意代码里 `os.getenv(..., "true")` 的默认值是 `"true"`——**即代码层面，该环境变量未设置时默认按「启用」处理**。

> 文档出入提示：[environment_variable_and_compiler_options_reference.md:45](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/environment_variable_and_compiler_options_reference.md#L45) 的表格把默认值标注为「0 或未设置（禁用）」，与上述代码默认值（启用）存在出入。实际行为以代码为准；若你希望显式关闭，请 `export TRITON_ALL_BLOCKS_PARALLEL=0`。单个 kernel 还可用 `enable_auto_blockify` 选项覆盖（解析顺序：该选项 > 环境变量）。

**② 黑名单：哪些算子会强制关闭 AutoBlockify**

[utils.py:53-64](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L53-L64) 列出四类「顺序敏感/不安全」的算子，命中即把 `has_auto_blockify_blacklist_op` 置真并打印告警：

```python
AUTO_BLOCKIFY_BLACKLIST_RULES = (
    (re.compile(r"\btt\.atomic_(?:rmw|cas)\b"), "atomic operations"),
    (re.compile(r"\btt\.elementwise_inline_asm\b"), "inline elementwise assembly"),
    (re.compile(r"\btt\.load\b[^\n]*\bisVolatile\s*=\s*true\b"), "loads with volatile"),
    (re.compile(r"\btt\.(?:load|store)\b[^\n]*\bcacheModifier\s*="), "loads or stores with cache modifiers"),
)
```

此外，[compiler.py:395-396](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L395-L396) 还会因 `sync_block_lock`（跨块读改写锁）的出现强制关闭 AutoBlockify——因为顺序折叠与跨块互斥冲突。

**③ 编译期：下发 AutoBlockify loop 选项**

SIMD/Linalg 路径在 [compiler.py:690-691](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L690-L691) 下发开关：

```python
if _is_auto_map_parallel_blocks_enabled() and not metadata.get("has_auto_blockify_blacklist_op", False):
    _compile_option_list += ["--enable-auto-blockify-loop"]
```

纯 SIMT 路径（`ttir_to_npubin`）有对称逻辑 [compiler.py:1166-1187](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1166-L1187)，并叠加 `enable_auto_blockify` 选项的覆盖判断。

**④ 运行期：把逻辑 grid 截断到物理核数**

在 `make_launcher` 里先算出开关 [driver.py:545](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L545)：

```python
enable_auto_map_parallel_blocks = (_is_auto_map_parallel_blocks_enabled() and not has_auto_blockify_blacklist_op)
```

再把这段 C++ 注入 launcher（[driver.py:915-922](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L915-L922)）：先按 `TRITON_GRID_WARN_PRINT` 告警，再用 `std::min` 截断——

```cpp
#ifdef ENABLE_GRID_WARN_PRINT
  static bool warned = false;
  if (!warned && blockNum > (uint32_t){num_physical_blocks}) {
    printf("WARNING: Grid %u > physical limit {num_physical_blocks}, performance maybe reduced.\n", blockNum);
    warned = true;
  }
#endif
  blockNum = std::min(blockNum, (uint32_t){num_physical_blocks});  // 仅当 enable_auto_map_parallel_blocks 时生成
```

`_launch` 本地打包路径在 [driver.py:1027-1034](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1027-L1034) 有完全相同的两段。这里的 `num_physical_blocks` 正是 4.2 里按 `mix_mode` 选出的 AI/Vector 核数——两个模块由此闭环。

#### 4.3.4 代码实践

**实践目标**：亲手触发 `coreDim > 65535`，分别用「解法 A（AutoBlockify）」和「解法 B（增大 BLOCK_SIZE）」解决。

**操作步骤**（沿用迁移指南的 `zeros_kernel` 场景）：

1. 构造大 N、小 BLOCK 触发超限：

```python
# 示例代码：N 很大、BLOCK_SIZE 很小 → coreDim 超过 65535
N = 1073741824
BLOCK_SIZE = 2048
grid = (triton.cdiv(N, BLOCK_SIZE), )   # = 524288 > 65535
zeros_kernel[grid](out, N, BLOCK_SIZE=BLOCK_SIZE)
```

2. **解法 B（先验证公式）**：按公式手算最小 BLOCK_SIZE，与迁移指南对照：

```text
ceil(N / 65535) = ceil(1073741824 / 65535) = 16385
next_power_of_2(16385) = 32768
```

即 `BLOCK_SIZE` 至少取 `32768`，此时 `coreDim = ceil(N/32768) = 32768 ≤ 65535`。

3. **解法 A**：保持小 `BLOCK_SIZE`，改为 `export TRITON_ALL_BLOCKS_PARALLEL=1`（或确认默认启用），再跑；dump launcher 源码确认生成了 `blockNum = std::min(...)` 截断行。

**需要观察的现象 / 预期结果**：解法 B 下报错消失；解法 A 下不报错，且（开 `TRITON_GRID_WARN_PRINT=1` 时）会看到一条 `Grid ... > physical limit ...` 告警后正常完成——说明逻辑 grid 被折叠到物理核数。

> 待本地验证：真实是否打印告警、是否自动截断，取决于该 kernel 是否命中黑名单（如含 atomic）以及环境变量实际取值，请在设备上确认。

#### 4.3.5 小练习与答案

**练习 1**：某 kernel 含 `tl.atomic_add`，作者设了 `TRITON_ALL_BLOCKS_PARALLEL=1` 希望突破 65535，能如愿吗？

> **参考答案**：不能。`tt.atomic_rmw/cas` 命中 [utils.py:54](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L54) 的黑名单，`has_auto_blockify_blacklist_op` 被置真，编译期不会下发 `--enable-auto-blockify-loop`，运行期也不会截断（[driver.py:545](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L545)），并会打印「AutoBlockify disabled ... Unsafe ops: atomic operations」告警。这是出于正确性保护——原子操作通常对顺序敏感。

**练习 2**：为什么 AutoBlockify 的「编译期 loop 包裹」和「运行期 block 数截断」必须用同一份开关、且必须同步？

> **参考答案**：编译期 loop 把「每个物理块遍历 chunk 个逻辑块」写进了 kernel body（chunk = ⌈逻辑块数/物理核数⌉）；运行期必须把 block 数从逻辑值截断到物理核数，kernel 才会按预期循环覆盖全部逻辑块。若两侧不同步（如编译期包了 loop、运行期却按逻辑 block 数启动），会导致重复计算或漏算——前者浪费、后者结果错误。代码用 `_is_auto_map_parallel_blocks_enabled()` + `has_auto_blockify_blacklist_op` 这同一份元数据同时 gating 两侧，正是为此。

## 5. 综合实践

**任务**：把本讲三个模块串起来，完成一次「从算子分类到 grid 规划再到超限处理」的完整推演。

1. **分类**：取一个「矩阵乘 + 逐元素缩放 epilogue」的 kernel，判断它的 `mix_mode`（提示：含 `tl.dot` 又有 vector epilogue → `mix`）。
2. **核数**：在你的设备上用 `NPUUtils` 打印 AI 核数与 Vector 核数，并据此写出该 kernel 的并发任务数上限（应取 AI 核数）。
3. **grid 设计**：按输出分块数设计一个 1D grid，刻意让 `cdiv` 结果落在「AI 核数 < grid ≤ 65535」区间。
4. **验证**：`export TRITON_GRID_WARN_PRINT=1` 运行，确认告警里的 `physical limit` 等于 AI 核数（而非 Vector 核数）——以此证明 `mix` 走的是 AI 核数分支。
5. **超限分支**：把某个纯 vector kernel 的 `BLOCK_SIZE` 调到极小，使 grid > 65535，观察默认（启用 AutoBlockify）下的行为；再故意在 kernel 里加一个 `tl.atomic_add`，观察黑名单如何让 AutoBlockify 失效。

> 待本地验证：第 2、4、5 步需要真实 NPU 设备与 CANN 环境。无设备时，可改为：阅读 `_parse_linalg_metadata` 与 [driver.py:547](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L547)，画出「算子 → mix_mode → 物理块上限」的映射表作为交付物。

## 6. 本讲小结

- 昇腾 NPU 的一个 AI 核内含 **Cube Core（矩阵）与 Vector Core（向量）**；软件层把 Vector 核数定义为 **AI 核数 × 2**（[driver.py:102](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L102)）。
- NPU 是「**强物理核心绑定**」：grid 直接对应物理核占用，每个核一次跑一个 block；这与 GPU「逻辑维度 + 硬件摊开」根本不同。
- 并发任务数按算子类型映射：**纯 vector（`aiv`）→ Vector 核数；含 `tl.dot`（`aic`/`mix`）→ AI 核数**，实现见 [driver.py:547](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L547)。
- 优先 **1D grid**；2D/3D 会被 `blockNum = gridX*gridY*gridZ` 合并成等价 1D（[driver.py:913](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L913)）。
- `coreDim` 硬上限 **65535**；超限可「开 `TRITON_ALL_BLOCKS_PARALLEL`（AutoBlockify）」或「增大 `BLOCK_SIZE`」解决。
- AutoBlockify 由 **编译期 `--enable-auto-blockify-loop`** 与 **运行期 `std::min(blockNum, 物理核数)`** 双协同实现，二者用同一开关，且对 atomic/volatile 等顺序敏感算子有黑名单保护。

## 7. 下一步学习建议

- **继续迁移深水区**：本讲只解决了「grid 核心分配」与「coreDim 上限」；下一讲 [u2-l3](u2-l3-memory-alignment-and-ub-constraints.md) 进入「内存对齐、UB（Unified Buffer）溢出、coreDim 与 BLOCK_SIZE 的复合约束」——你会发现增大 `BLOCK_SIZE` 解了 coreDim 却可能撞上 UB 溢出，需要 `BLOCK_SIZE_SUB` 二级分块。
- **理解 mix_mode 的更深来源**：若想看清 `mix_mode` 是如何由编译期 pass 写进 Linalg IR 的，可预习 [u4](../manifest.json) 的 Ascend pass 流水线讲义，尤其是 TritonToLinalg。
- **CV 融合进阶**：`mix` 模式下的 Cube-Vector 协同、mix_mode 与并发任务数/对齐（512B）的关系，是 [u8](../manifest.json)「Cube-Vector 融合与流水线优化」单元的主题。
- **运行时细节**：`num_physical_blocks` 如何被注入 C++ launcher、`rtKernelLaunch` 如何用 blockNum，将在 [u5-l3](u5-l3-kernel-launch-and-resources.md) 展开。
