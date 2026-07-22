# 统一算子体系：kernels 注册/选择与 sgl-kernel/JIT

## 1. 本讲目标

本讲承接 u5-l3（注意力后端）和 u11-l1（量化方案），打开 SGLang 高性能推理的「最底层」——**算子（kernel）从哪里来、如何被组织、如何被挑选**。

在 RFC #29630 之前，SGLang 的算子散落在三个地方：进程内即时编译的 `sglang.jit_kernel`（轻量 CUDA，运行时用 nvcc/hipcc 编译）、预编译进 `sgl_kernel` wheel 的 AOT C++/CUDA 算子、以及散落在各处的 Triton/CuTe DSL 脚本。调用方各自 `import`，新增算子要在多处登记，设备适配（CUDA / ROCm / Ascend）靠零散的 `if` 分支。这套体系难盘点、难测试、难回退。

RFC #29630 的目标是建一个**统一算子命名空间 `sglang.kernels`**：每个算子用「纯元数据」登记进一个进程级清单（registry），由一个设备感知的解析器（selector）按设备能力在 AOT 与 JIT 之间挑选，多后端融合算子用一个 `BaseFusedOp` 基类把若干实现聚合在同一个 `forward()` 后面。

学完本讲，你应该能够：

1. 说出 `KernelSpec` / `KernelBackend` / `CapabilityRequirement` / `PlatformInfo` 各自描述什么，以及它们之间的「元数据契约」。
2. 画出 `register_kernel` → `registry` → `select_kernel` / `get_kernel` 的解析链路，并解释为什么「登记不算入 torch、调用才导入」。
3. 读懂 `BaseFusedOp.forward` 的多后端分发与默认优先级，会用 `SGLANG_FORCE_FUSED_OP_BACKEND` 一键回退到参考实现做数值排查。
4. 区分三种算子来源：`sgl-kernel`（AOT wheel）、`kernels/jit`（JIT 基础设施）、legacy `jit_kernel`（兼容 shim），并能判断一个新算子该放哪。
5. 数清 `kernels/ops/` 当前的算子组（**实测 19 组**），并说明 Phase 4 batch-3（#32045）把哪些原 `jit_kernel` 子系统迁了进来。

## 2. 前置知识

本讲假设你已经了解：

- **算子（kernel）**：在 GPU 上执行的一段计算，比如 RMSNorm、量化、注意力。SGLang 的推理延迟几乎完全由这些算子决定。
- **AOT 与 JIT**：AOT（Ahead-Of-Time，提前编译）指在打包阶段就把 CUDA/C++ 源码编译成机器码，装进 wheel；JIT（Just-In-Time，即时编译）指在进程运行时才用编译器（nvcc / hipcc）现场编译。AOT 首次调用快但要预编译分发，JIT 灵活可针对实际硬件架构特化但首次调用慢。
- **CUDA / ROCm / HIP / Ascend NPU**：CUDA 是 NVIDIA GPU 的编程模型；ROCm/HIP 是 AMD GPU 的（HIP 在语法上接近 CUDA，故 JIT 算子可同时被 nvcc 和 hipcc 编译）；Ascend NPU 用 `torch_npu` 运行时。
- **进程级单例与惰性导入**：用一个模块级全局对象保存「全进程唯一」的状态；导入模块时不立即导入重依赖（如 torch），等真正用到才导入，以保持「`import sglang.kernels` 在纯 CPU 机器上也能跑」。

如果你还不熟悉「为什么推理引擎要把算子分成这么多后端」，可以先读 u5-l3（注意力后端的可插拔机制）作为对照——本讲是同一思想在「裸算子」层面的体现。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [python/sglang/kernels/spec.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/spec.py) | 元数据描述层：`KernelBackend`（来源）、`DeviceType`、`PlatformInfo`（运行期平台快照）、`CapabilityRequirement`（设备能力要求）、`KernelSpec`（一个算子实现的完整登记条目）。**全程不导入 torch。** |
| [python/sglang/kernels/registry.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/registry.py) | 进程级清单 `KernelRegistry` 与模块级单例 `registry`、登记函数 `register_kernel`。 |
| [python/sglang/kernels/selector.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/selector.py) | 设备感知的固定路径解析：`select_kernel`（按设备能力过滤）、`get_kernel`（解析并缓存可调用对象）。 |
| [python/sglang/kernels/fused_op.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/fused_op.py) | 多后端融合算子基类 `BaseFusedOp`：一个逻辑算子 + 多个 `forward_<backend>`，由 `forward()` 按优先级 + 能力过滤选一个跑。 |
| [python/sglang/kernels/ops/layernorm/__init__.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/layernorm/__init__.py) | 一个完整的 `BaseFusedOp` 范例：`RMSNormOp` 拥有 native / aot / jit / aiter / npu 五个后端。 |
| [python/sglang/kernels/ops/quantization/__init__.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/quantization/__init__.py) | 另一种风格的范例：直接 `register_kernel(KernelSpec(...))` 把 AOT 与 JIT 两个实现登记在同一算子名下。 |
| [python/sglang/kernels/ops/__init__.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/__init__.py) | 算子组总入口，列出全部算子组并 eagerly 导入以填充 registry。 |
| [python/sglang/jit_kernel/norm.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/jit_kernel/norm.py) | legacy `jit_kernel` 兼容 shim：一行 `globals().update(...)` 把实现转发到 `kernels.ops`。 |
| [sgl-kernel/pyproject.toml](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-kernel/pyproject.toml) | AOT wheel 的构建配置（`sglang-kernel`，scikit-build-core）。 |

> 一句话定位：`spec.py` 是「词汇表」，`registry.py` 是「清单本」，`selector.py` 是「查表员」，`fused_op.py` 是「一个算子的多个化身」，`ops/` 是「按类别上架的货架」，`jit_kernel/` 是「旧货架的转发牌」，`sgl-kernel` 是「另一个仓库的预包装商品」。

## 4. 核心概念与源码讲解

本讲按 5 个最小模块拆分：先讲元数据契约（4.1），再讲清单与登记（4.2），再讲设备感知解析（4.3），再讲多后端融合算子（4.4），最后讲算子组布局与 legacy 关系（4.5）。

### 4.1 元数据契约：KernelSpec / KernelBackend / CapabilityRequirement / PlatformInfo

#### 4.1.1 概念说明

统一算子体系的核心思想是：**把「一个算子实现」抽象成一条纯元数据记录 `KernelSpec`**。这条记录只描述「这个算子叫什么、由哪种来源提供、能在哪些设备上跑、真正的代码在哪个导入路径」，而**不包含任何 torch 或编译动作**。

这样设计有两个直接好处：

1. **导入零成本**：`import sglang.kernels` 在一台纯 CPU、没装 GPU 驱动的机器上也能成功——因为登记只是往字典里塞字符串。
2. **可盘点**：所有算子的实现来源、设备支持范围都被显式记录下来，工具可以扫一遍 registry 就画出「哪个算子在哪个设备上有几个后端」的全景图。

`KernelSpec` 里有四个关键概念需要分清：

- **`KernelBackend`（来源 / provenance）**：描述这个实现是**怎么造出来的**，不是**在哪个硬件上跑**。比如 `AOT`（预编译进 wheel）、`JIT`（运行时编译）、`TRITON`、`FLASHINFER`、`AITER`（AMD 库）、`TORCH_NPU`（昇腾运行时）、`TORCH`（纯 torch 参考）。
- **`DeviceType`**：硬件族（`CUDA` / `HIP` / `NPU` / `CPU`）。
- **`CapabilityRequirement`（能力要求）**：一条「这个后端能跑在哪种设备（可选地限定 CUDA 架构上下界）」的约束。一个后端可以带**一组**这种约束，语义是 **OR**——只要其中任意一条被满足就算可用。
- **`PlatformInfo`（平台快照）**：当前进程实际运行的设备是什么，由 `PlatformInfo.detect()` 在运行期探测。

这里有一个 RFC #29630 后期（#31292）的重要解耦：**来源（backend）与设备（device）是正交的**。同一个 `AOT` 来源既能为 CUDA 造、也能为 ROCm 造；同一个 `JIT` 来源既会被 nvcc 编译、也会被 hipcc 编译。所以「这个后端支持哪些设备」不是从 backend 名字推断的，而是由它自带的 `CapabilityRequirement` 集合显式声明的。

#### 4.1.2 核心流程

元数据层的判定流程是一个纯逻辑表达式：

1. 进程启动时调用一次 `PlatformInfo.detect()` 得到当前平台 \( P \)（含 device_type 与可选的 CUDA 架构主版本号）。
2. 对某条 `KernelSpec` 的能力集合 \( C = \{c_1, c_2, \dots\} \) 做 OR 判定：

\[
\text{eligible}(spec, P) \;=\; \bigvee_{c \in C} c.\text{is\_satisfied\_by}(P)
\]

空集合 \( C = \varnothing \) 表示「无限制，任何设备都能跑」。单条约束 \( c \) 的判定是：设备族必须相等；若是 CUDA，则架构必须落在 \([ \text{min\_cuda\_arch}, \text{max\_cuda\_arch} ]\) 闭区间内。

3. 能力判定**只看元数据、不导入任何后端**，因此可以在清单筛选阶段无副作用地反复调用。

`KernelSpec` 的「真正的可调用对象」用 `target` 字段以 `"module:attr"` 字符串保存，只有在调用 `KernelSpec.load()` 时才 `importlib.import_module` 真正导入——这是「登记不导入」的最后一道闸。

#### 4.1.3 源码精读

先看「来源」枚举，注意每个值只是个字符串标签：

- [python/sglang/kernels/spec.py:29-50](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/spec.py#L29-L50)：`KernelBackend` 枚举，注释明确点出「来源 ≠ 设备」，`JIT` 与 `AOT` 都是跨设备的。

再看能力要求与 OR 语义：

- [python/sglang/kernels/spec.py:119-170](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/spec.py#L119-L170)：`CapabilityRequirement`。`is_satisfied_by`（L156-165）做设备族匹配与 CUDA 架构区间检查；类末尾（L168-170）提供 `CapabilityRequirement.CUDA` / `.HIP` / `.NPU` 三个最常用的「仅设备」快捷常量。
- [python/sglang/kernels/spec.py:173-188](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/spec.py#L173-L188)：`capabilities_satisfied` 把「单条 / 集合」统一成 OR 判定，空集合返回 `True`。

`PlatformInfo.detect()` 是唯一会导入 torch 的地方，且它被严格隔离在「运行期探测」里：

- [python/sglang/kernels/spec.py:89-116](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/spec.py#L89-L116)：先试 `torch.version.hip` 判 ROCm，再试 `torch.npu` 判昇腾，最后落到 CUDA 并取出 `get_device_capability()` 的架构号；任何异常都兜底回 CPU，**永不抛错**。

最后是主角 `KernelSpec`：

- [python/sglang/kernels/spec.py:203-263](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/spec.py#L203-L263)：`msgspec.Struct(frozen=True)`，字段含 `op`（`"<group>.<name>"` 查找键）、`backend`、`target`（`"module:attr"` 懒导入路径）、`capabilities`（默认空 = 任意设备）。`is_available`（L244-246）只是 `capabilities_satisfied` 的包装；`load`（L248-263）才真正 `importlib.import_module` 并按点号逐层 `getattr` 取出可调用对象。

> 注意：`KernelSpec` 用 `msgspec.Struct(frozen=True)` 而不是 `@dataclass`，这是项目的统一约定（见仓库 `.claude/rules/no-dataclasses.md`），便于严格类型检查与未来的多语言迁移。

#### 4.1.4 代码实践

**实践目标**：亲手验证「登记不导入 torch」这条核心不变量。

**操作步骤**：

1. 在一台装了 Python 但**没有** GPU（或暂不可见 CUDA）的环境里，运行下面这段脚本（**示例代码**）：

```python
# 示例代码：验证元数据层零 torch 依赖
import sys
from sglang.kernels.spec import PlatformInfo, CapabilityRequirement as CR, KernelSpec, KernelBackend

# 1) detect() 应永不抛错，无 GPU 时回退 CPU
p = PlatformInfo.detect()
print("platform:", p.device_type)        # 期望: cpu（无 GPU 时）

# 2) 纯元数据判定，不导入任何后端
spec = KernelSpec(
    op="mygroup.myop",
    backend=KernelBackend.AOT,
    target="sgl_kernel:nonexistent",      # 故意写一个不存在的导入路径
    capabilities=frozenset({CR.cuda(min_sm=(10, 0))}),
)
print("eligible:", spec.is_available(p))  # 期望: False（CPU 不满足 SM100+ CUDA）
print("torch imported?", "torch" in sys.modules)  # 期望: False（detect 之外没碰 torch）
```

2. 重点关注最后两行打印。

**需要观察的现象**：

- `platform` 在无 GPU 时为 `cpu`；`eligible` 为 `False`。
- `torch imported?` 在构造 `KernelSpec` 与调用 `is_available` 之后**仍然是 `False`**（`detect()` 内部即便短暂 import 了 torch，元数据构造本身不依赖它；若想更严格，可在 `detect()` 前后对比）。

**预期结果**：你证明了「登记 + 能力判定」全过程不需要导入真实后端，这正是统一命名空间能在任何机器上做盘点的基础。

**待本地验证**：若你的机器有 GPU，`platform` 会变成 `cuda` 或 `hip`，`eligible` 行为相应改变；脚本本身不依赖运行结果即可说明机制。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KernelBackend` 注释里强调「来源不等于设备」？如果它们不分清，会在哪里出错？

> **参考答案**：因为同一个来源（如 `AOT`）既能为 CUDA 也能为 ROCm 预编译，一个 wheel 也可能只装了部分算子的某一面。若把来源当设备，就会错误地认为「`AOT` 只能在 CUDA 跑」，从而在 ROCm 上漏掉可用实现、或在 CUDA 上误选一个只编了 ROCm 的实现。把「来源」与「设备」解耦后，设备适配由 `CapabilityRequirement` 集合显式声明，selector 才能正确按设备过滤。

**练习 2**：`capabilities=frozenset()`（空集合）与 `capabilities=frozenset({CR.CUDA, CR.HIP})` 各表示什么？

> **参考答案**：空集合表示「无限制，任意设备都能跑」（OR 语义下空析取为真）；`{CUDA, HIP}` 表示「CUDA 或 ROCm 都行，但 CPU/NPU 不行」。

---

### 4.2 算子清单：KernelRegistry 与 register_kernel

#### 4.2.1 概念说明

有了 `KernelSpec` 这条「记录格式」，还需要一个**进程级清单**来收集所有算子的所有实现——这就是 `KernelRegistry`。它是一个 `"<group>.<name>"` 算子 id 到「该算子的全部后端实现列表」的映射。

为什么是「一个 id → 多条 spec 的列表」而不是「一个 id → 一条 spec」？因为**同一个算子可以有多个后端实现**（如 RMSNorm 既有 AOT 也有 JIT）。清单允许同名算子挂多个来源，由解析器在调用时按设备挑一个。

登记入口是模块级函数 `register_kernel(spec)`，它只是把 spec 塞进进程唯一的 `registry` 单例。所有 `ops/<group>/__init__.py` 在被导入时都会调用它——因此「导入算子组」就等于「填清单」。

#### 4.2.2 核心流程

登记与查询流程：

1. 进程启动时，`kernels/__init__.py` 触发 `from sglang.kernels import ops`。
2. `ops/__init__.py` 遍历全部算子组名，对每个组 `import_module(...)`。
3. 每个组的 `__init__.py` 执行模块顶层的 `register_kernel(KernelSpec(...))`，元数据进 `registry._by_op`。
4. 同一个 `(op, backend)` 二元组重复登记时，**后者覆盖前者**（保证测试中模块重载幂等）。
5. 查询时 `registry.get(op)` 返回该算子的全部 spec；`registry.get_backend(op, backend)` 返回指定来源的那一条。

#### 4.2.3 源码精读

- [python/sglang/kernels/registry.py:17-62](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/registry.py#L17-L62)：`KernelRegistry`。`register`（L23-35）的覆盖逻辑——遍历已有列表，找到同 `backend` 的就原地替换，否则追加；`get`（L37-39）返回副本避免外部篡改；`get_backend`（L41-49）在找不到时抛 `KeyError`；`ops()`（L54-56）与 `all_specs()`（L58-62）供盘点工具使用。
- [python/sglang/kernels/registry.py:66](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/registry.py#L66)：模块级单例 `registry = KernelRegistry()`，全进程唯一。
- [python/sglang/kernels/registry.py:69-71](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/registry.py#L69-L71)：`register_kernel(spec)` 一行转发到 `registry.register`，是所有算子组登记的唯一入口。

一个典型的「直接登记」式算子组——量化算子，同时为同一算子名登记 AOT 与 JIT 两个来源：

- [python/sglang/kernels/ops/quantization/__init__.py:21-33](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/quantization/__init__.py#L21-L33)：登记 `quantization.sgl_per_token_quant_fp8`，`backend=AOT`，`target="sgl_kernel:sgl_per_token_quant_fp8"`（指向 AOT wheel）。
- [python/sglang/kernels/ops/quantization/__init__.py:55-72](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/quantization/__init__.py#L55-L72)：登记 `quantization.per_token_group_quant`，`backend=JIT`，`target="sglang.kernels.ops.quantization._jit_per_token_group_quant:per_token_group_quant"`（指向 JIT 实现），并带 `capabilities=_CUDA` 约束。两条记录共享不同来源，是「同 op 多 backend」的活样本。

#### 4.2.4 代码实践

**实践目标**：用 registry 亲手盘点「某算子有几个后端」。

**操作步骤**（**示例代码**）：

```python
# 示例代码：盘点量化算子的全部后端
import sglang.kernels  # 触发 ops 全量导入，填满 registry
from sglang.kernels.registry import registry

for op_id in ("quantization.sgl_per_token_quant_fp8",
              "quantization.per_token_group_quant"):
    specs = registry.get(op_id)
    print(op_id, "->", [(s.backend.value, s.target) for s in specs])
```

**需要观察的现象**：`sgl_per_token_quant_fp8` 只有一个 AOT 后端；`per_token_group_quant` 只有一个 JIT 后端——说明「同 op 多 backend」是**可选**的，并非每个算子都多个。

**预期结果**：打印出每个算子的来源与懒导入路径。如果某算子有多个后端（如 layernorm.rmsnorm 既有 AOT 又有 JIT），列表里会有多条。

**待本地验证**：若你想看真正「多后端」的算子，把 `op_id` 换成 `layernorm.rmsnorm`（见 4.4），会看到 AOT/JIT 等多条。

#### 4.2.5 小练习与答案

**练习 1**：`register` 为什么要对相同 `(op, backend)` 做「覆盖」而非「报错」？

> **参考答案**：为了在测试和热重载场景下保持幂等——同一个模块被重复导入时，不应让 registry 里堆积重复条目或抛错。覆盖语义让「重新登记 = 刷新这条记录」，模块重载安全。

**练习 2**：`registry.get(op)` 返回的是一个新 list（`list(...)`），而不是内部列表本身。为什么？

> **参考答案**：防止调用方拿到内部引用后就地修改清单、污染 registry 状态。返回副本是「保持清单不可变」的防御性写法，符合项目「prefer immutable」的代码风格。

---

### 4.3 设备感知解析：selector.get_kernel / select_kernel

#### 4.3.1 概念说明

registry 只是把算子「摆上架」，真正「按设备挑出一个可调用对象」的是 `selector`。它提供两个层次：

- `select_kernel(op, backend=None)`：返回**选中的 `KernelSpec`**（还没导入真实代码）。
- `get_kernel(op, backend=None)`：在 `select_kernel` 基础上**再 `load()` 成可调用对象，并用 `lru_cache` 缓存**，是公共 `ops.*` 包装函数实际调用的快路径。

selector 的设计原则很克制——**没有优先级排序或偏好启发式**。解析是确定性的：

1. 某算子只有 1 个后端 → 直接选它。
2. 某算子有多个后端 → 用 `PlatformInfo` 做**硬能力过滤**（不是偏好）：筛掉当前设备跑不了的后端。若恰好剩 1 个 → 选它；若一个不剩 → 抛 `ValueError`；若仍剩多个 → 要求调用方显式指定 `backend=`。

> 关键区别：**按设备过滤是「资格硬门槛」，不是 `BaseFusedOp` 那种「按优先级自动选最优」**。selector 的多后端场景需要调用方点名；`BaseFusedOp` 的多后端是自动挑。两者分工不同（见 4.4）。

#### 4.3.2 核心流程

解析流程（伪代码）：

```
select_kernel(op, backend=None):
    specs = registry.get(op)
    if not specs: raise KeyError
    if backend is not None:
        return 匹配 backend 的那条 spec（找不到则 KeyError）
    if len(specs) == 1:
        return specs[0]
    # 多后端：硬过滤
    platform = PlatformInfo.detect()          # 进程级缓存
    eligible = [s for s in specs if s.is_available(platform)]
    if len(eligible) == 1: return eligible[0]
    if not eligible: raise ValueError("无可用后端")
    raise ValueError("多个可用，请显式指定 backend=")

get_kernel(op, backend=None) = lru_cache(select_kernel(...).load())
```

平台探测 `_platform()` 用 `@lru_cache(maxsize=1)` 缓存——因为一个进程的设备族是恒定的，没必要反复探测。`_resolve` 用 `@lru_cache(maxsize=None)` 缓存「(op, backend) → 可调用对象」，所以首次解析 + 导入最贵，之后命中缓存。

#### 4.3.3 源码精读

- [python/sglang/kernels/selector.py:33-35](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/selector.py#L33-L35)：`_platform()` 进程级缓存的平台快照。
- [python/sglang/kernels/selector.py:38-84](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/selector.py#L38-L84)：`select_kernel` 全文。注意 L70-79 的多后端过滤分支：`eligible = [s for s in specs if s.is_available(platform)]`，分支后要么唯一返回、要么报「无可用」、要么报「多个可用请点名」。
- [python/sglang/kernels/selector.py:87-98](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/selector.py#L87-L98)：`_resolve`（带 `lru_cache`）调用 `select_kernel(...).load()`；`get_kernel` 一行转发。注释点明「这是公共 `ops.*` 包装实际调用的，首次解析并导入，之后命中缓存」。
- [python/sglang/kernels/selector.py:101-103](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/selector.py#L101-L103)：`clear_cache` 仅供测试重置用。

公共包装函数怎么用 selector？看量化算子：

- [python/sglang/kernels/ops/quantization/__init__.py:75-83](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/quantization/__init__.py#L75-L83)：`sgl_per_token_quant_fp8(...)` 一行 `return get_kernel("quantization.sgl_per_token_quant_fp8", KernelBackend.AOT)(...)`——**显式 pin 死 AOT 后端**。因为该包装函数的签名文档就是 AOT 版的，所以点名。
- [python/sglang/kernels/ops/quantization/__init__.py:120-153](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/quantization/__init__.py#L120-L153)：`per_token_group_quant(...)` 同样点名 `KernelBackend.JIT`。两条包装各 pin 各的后端，签名差异由包装函数自身吸收，调用方无需感知。

#### 4.3.4 代码实践

**实践目标**：观察 selector 在「多后端、需点名」时如何报错，理解它为何不做自动择优。

**操作步骤**（**示例代码**）：

```python
# 示例代码：模拟多后端解析
from sglang.kernels.selector import select_kernel
from sglang.kernels.registry import registry, register_kernel
from sglang.kernels.spec import KernelSpec, KernelBackend, CapabilityRequirement as CR

# 手动塞两条同名、不同 backend、都「任意设备可用」的假 spec
for bk in (KernelBackend.AOT, KernelBackend.JIT):
    register_kernel(KernelSpec(op="demo.toy", backend=bk, target="os:path"))

try:
    select_kernel("demo.toy")          # 两个都 eligible，又不点名
except ValueError as e:
    print("预期的报错:", e)
# 显式点名后正常返回
print("点名 AOT:", select_kernel("demo.toy", KernelBackend.AOT).backend.value)
```

**需要观察的现象**：不点名时抛 `ValueError`，信息会列出所有 eligible 的后端并提示「pass backend=...」；点名后正常返回。

**预期结果**：印证 selector「多后端必须点名」的设计。注意这是纯元数据操作（`target` 指向 `os:path` 不会被真正调用，因为我们只 `select_kernel` 没 `load`）。

**待本地验证**：这条脚本不依赖 GPU 即可运行，因为它不触发 `load()` 导入真实后端。

#### 4.3.5 小练习与答案

**练习 1**：selector 为什么不像 `BaseFusedOp` 那样自动按优先级选最优后端？

> **参考答案**：职责分工。selector 面向「直接登记式」算子（一个算子名下挂几个来源），它只保证「按设备资格挑出能跑的」，至于是 AOT 还是 JIT 由调用方根据签名决定——因为不同来源的调用约定可能不同（参数个数、是否有 `out=` 等），不能盲目替调用方选。`BaseFusedOp` 则是专门为「同签名、可互换」的融合算子设计的，才能安全地自动择优。

**练习 2**：`get_kernel` 的缓存键是 `(op, backend)`。如果在运行期切换了 GPU 可见性（例如热插拔），缓存会不会给出错误结果？

> **参考答案**：会。「设备族进程内恒定」是缓存的前提。`PlatformInfo.detect()` 只在首次调用并缓存；运行期切换 GPU 可见性属于极端边界，不在设计预期内，需要调用 `clear_cache()` 手动重置。正常推理进程不会遇到。

---

### 4.4 多后端融合算子：BaseFusedOp.forward 与后端优先级

#### 4.4.1 概念说明

4.2 的「直接登记式」适合「一个算子名 pin 一个后端」的场景。但有一类算子——如 RMSNorm、激活函数——它们**有多个实现、调用签名完全一致、可以自动择优**。为这类算子手写「按设备选后端」的 `if/else` 很啰嗦，于是有了 `BaseFusedOp`。

`BaseFusedOp` 是**每个逻辑算子的实现对象**：子类实现一个 `forward_native`（纯 torch 参考实现，必填）加任意个 `forward_<backend>`（如 `forward_aot` / `forward_jit` / `forward_triton` / `forward_aiter` / `forward_npu`）。统一的 `forward(*args, **kwargs)` 会：

1. 按 `priority`（best → fallback）遍历该算子可用的后端；
2. 逐个用 `backend_eligible` 做**能力过滤**（子类还能加 per-call 的形状/dtype 门槛）；
3. 选第一个 eligible 的后端跑；都不行就退化到 `native` 参考实现。

「可用」的定义是结构性的：子类**重写了**对应的 `forward_<backend>` 方法就算「实现了这个后端」（`native` 与 `torch_compile` 永远可用，后者基类直接给成 `torch.compile(forward_native)`）。

这套设计带来四个工程红利（README 列举）：统一正确性测试（每个后端都对齐 `forward_native`）、一键回退排查（`SGLANG_FORCE_FUSED_OP_BACKEND=torch` 把所有融合算子切回参考实现）、安全降级（缺优化内核时退到 `native` 而非散落 `if`）、渐进优化（先上 `native`，后加 triton/aot 不改调用方）。

#### 4.4.2 核心流程

`forward` 的分发流程（伪代码）：

```
forward(*args, backend=None, **kwargs):
    if backend is None:
        backend = _resolve_backend(*args, **kwargs)
    method = getattr(self, BACKEND_METHODS[backend])  # e.g. forward_aot
    ...记录 trace（若开启）...
    return method(*args, **kwargs)

_resolve_backend(*args, **kwargs):
    forced = SGLANG_FORCE_FUSED_OP_BACKEND 全局开关
    if forced is not None: return forced
    for b in self._ordered:              # priority 顺序 × 仅可用后端
        if self.backend_eligible(b, *args, **kwargs):
            return b
    return KernelBackend.TORCH           # 全不行就退到参考实现
```

`__init__` 里预计算两个集合以避开热路径上的内省：

- `_available`：所有「结构性实现了」的后端（含 `native` / `torch_compile`）。
- `_ordered`：`priority` 与 `_available` 的交集，按优先级排序。

默认优先级 `DEFAULT_PRIORITY`（best → fallback）：

```
AOT → JIT → FLASHINFER → DEEPGEMM → CUTE_DSL → AITER → TORCH_NPU → TRITON → TORCH
```

注意 `torch_compile` **故意不在**默认优先级里——自动选择绝不应在服务进程里触发一次意外的 `torch.compile`，要它得显式指定。

#### 4.4.3 源码精读

- [python/sglang/kernels/fused_op.py:63-74](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/fused_op.py#L63-L74)：`BACKEND_METHODS` 把来源映射到 `forward_<backend>` 方法名。
- [python/sglang/kernels/fused_op.py:79-89](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/fused_op.py#L79-L89)：`DEFAULT_PRIORITY`。注释（L76-78）解释为何排除 `torch_compile`。
- [python/sglang/kernels/fused_op.py:108-126](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/fused_op.py#L108-L126)：全局强制开关 `get_fused_op_backend` / `set_fused_op_backend`，从环境变量 `SGLANG_FORCE_FUSED_OP_BACKEND` 解析（解析一次后缓存）。
- [python/sglang/kernels/fused_op.py:181-321](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/fused_op.py#L181-L321)：`BaseFusedOp` 主体。`__init__`（L217-230）预算 `_available` / `_ordered`；`_overrides`（L232-238）用 MRO 判断某方法是否被子类重写；`forward_native`（L242-244）是 `@abstractmethod`；各 `forward_<backend>` 默认抛 `NotImplementedError`（L253-275）；`backend_eligible`（L283-293）做能力过滤；`_resolve_backend`（L295-302）按优先级选第一个 eligible，否则退 `TORCH`；`forward`（L306-321）做最终派发并在开启 trace 时记录。
- [python/sglang/kernels/fused_op.py:324-344](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/fused_op.py#L324-L344)：`register_fused_op` 把一个 `BaseFusedOp` 实例的**每个可用后端**都登记成 registry 里的一条 `KernelSpec`（target 形如 `"<module>:<attr>.forward_aot"`），让 `select_kernel(..., backend=...)` 与清单盘点仍然可用——这是「融合算子」与「直接登记式」两个世界互通的桥。

最有教学价值的范例是 layernorm 的 `RMSNormOp`：

- [python/sglang/kernels/ops/layernorm/__init__.py:40-46](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/layernorm/__init__.py#L40-L46)：`_NORM_PRIORITY` 覆盖默认优先级为 `AOT → JIT → AITER → TORCH_NPU → TORCH`。
- [python/sglang/kernels/ops/layernorm/__init__.py:55-62](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/layernorm/__init__.py#L55-L62)：`capabilities` 把每个后端钉到设备——`AOT`/`JIT` 钉 CUDA、`AITER` 钉 HIP、`TORCH_NPU` 钉 NPU。结合优先级，结果是「CUDA 选 AOT、ROCm 选 AITER、昇腾选 TORCH_NPU、都没就退 native」，每条都恰好是该设备的生产默认。
- [python/sglang/kernels/ops/layernorm/__init__.py:75-155](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/layernorm/__init__.py#L75-L155)：五个后端实现。`forward_native`（L75-92）是纯 torch 参考；`forward_aot`（L94-104）`import sgl_kernel` 后调 `sgl_kernel.rmsnorm`；`forward_jit`（L106-121）走 `kernels.ops.layernorm._jit_norm`；`forward_aiter`（L123-139）走 AMD `aiter.rmsnorm2d_fwd`；`forward_npu`（L141-155）走 `torch_npu.npu_rms_norm`。**注意每个后端都在方法体内才 `import` 对应库**——构造与登记时不导入。
- [python/sglang/kernels/ops/layernorm/__init__.py:443](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/layernorm/__init__.py#L443)：`_RMSNORM = register_fused_op(RMSNormOp(), __name__, "_RMSNORM")` 同时建实例、登记全部后端、绑定到模块级名字；公开函数 `rmsnorm(...)`（L453-461）只是 `_RMSNORM(...)` 的薄包装。

#### 4.4.4 代码实践

**实践目标**：读懂 RMSNorm 的后端优先级如何随设备变化，并会用环境变量一键切回参考实现。

**操作步骤**：

1. 读 [python/sglang/kernels/ops/layernorm/__init__.py:40-62](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/layernorm/__init__.py#L40-L62)，在纸上推导：在 CUDA、HIP、NPU、CPU 四种平台上，`_resolve_backend` 会分别走到 `_ordered` 里的第几个后端。
2. 若有 GPU 环境（**示例代码**），用强制开关对比：

```python
# 示例代码：强制切回 native 参考实现，用于数值排查
import os
os.environ["SGLANG_FORCE_FUSED_OP_BACKEND"] = "torch"  # 必须在 import 之前设置
import torch
from sglang.kernels.ops.layernorm import _RMSNORM, rmsnorm
from sglang.kernels.fused_op import get_fused_op_backend

print("forced backend:", get_fused_op_backend())   # 期望: KernelBackend.TORCH
x = torch.randn(4, 8, dtype=torch.bfloat16, device="cuda")
w = torch.ones(8, dtype=torch.bfloat16, device="cuda")
print(rmsnorm(x, w).dtype)                          # 走 forward_native
```

**需要观察的现象**：无论实际设备是什么，`get_fused_op_backend()` 都返回 `TORCH`，`rmsnorm` 走 `forward_native`——这就是「一键把所有融合算子切回参考实现」的排查开关。

**预期结果**：在怀疑某个优化后端有数值 bug 时，设这个环境变量即可让全进程回退到纯 torch 参考实现做对比，无需改任何调用方代码。

**待本地验证**：无 GPU 时去掉 `device="cuda"` 改用 CPU 张量，机制不变；但 `forward_native` 之外的后端在 CPU 上不会被选中（能力过滤会排除它们）。

#### 4.4.5 小练习与答案

**练习 1**：`DEFAULT_PRIORITY` 里为什么没有 `torch_compile`？

> **参考答案**：自动选择绝不应在一个在线服务进程里意外触发 `torch.compile`——那会引起首次调用的长时间编译、意外的图捕获与内存占用。要它必须显式指定（`backend=KernelBackend.TORCH_COMPILE` 或环境变量强制），把「惊喜编译」变成「知情选择」。

**练习 2**：`RMSNormOp` 的 `forward_aot` 在方法体里才 `import sgl_kernel`。如果把这句 import 提到模块顶部，会破坏哪条设计不变量？

> **参考答案**：会破坏「登记与构造不导入真实后端」的不变量。模块顶部 import 会让「`import sglang.kernels.ops.layernorm`」就触发 `sgl_kernel` 导入，于是在没装 wheel 的纯 CPU 机器上整个算子组无法导入，盘点工具也就失效了。方法内懒导入把重依赖推迟到「真正调用该后端」的那一刻。

---

### 4.5 算子组布局与 legacy 关系：ops/ 算子组 + sgl-kernel vs jit vs jit_kernel shim

#### 4.5.1 概念说明

前面四个模块讲的是「机制」，本模块讲「现状」：这套统一体系在仓库里**长什么样**，以及它和三个容易混淆的目录是什么关系。

**四个目录，三种算子来源，务必分清**：

1. **`python/sglang/kernels/`（统一命名空间，本讲主角）**：RFC #29630 之后的新家。`ops/<group>/` 按**算子类别**组织（layernorm、quantization、moe、attention……），每个组用 `register_kernel` / `register_fused_op` 把实现登记进 registry。**这是运行时代码与测试应当 import 的地方**（仓库 README 明确的 review 规则）。
2. **`sgl-kernel/`（AOT wheel，独立包）**：另一个 Python 包 `sglang-kernel`（当前版本 0.4.5），用 scikit-build-core 把 C++/CUDA 源码预编译成机器码打进 wheel，安装后 `import sgl_kernel` 即可用。它是 `KernelBackend.AOT` 的来源，`target="sgl_kernel:xxx"` 就指向这里。它是**最稳定的 wheel 边界、形状支持最广**的来源。
3. **`python/sglang/kernels/jit/`（JIT 基础设施新家）**：JIT 编译的共享构建/运行时工具（`load_jit`、`make_cpp_args`、`KERNEL_PATH`、`get_jit_cuda_arch`、`is_arch_support_pdl` 等）迁到了这里。csrc/include/operators 会逐步迁入。
4. **`python/sglang/jit_kernel/`（legacy 兼容 shim）**：RFC #29630 之前的 JIT 算子老家。迁移后它**没有消失**，而是退化为「转发牌」：每个文件只剩一行 docstring + `globals().update(...)` 把符号从 `kernels.ops` 重新导出，保证老调用方 `from sglang.jit_kernel.norm import rmsnorm` 仍然能用。

一句话总结迁移方向：**实现搬进 `kernels/ops`，老路径用 shim 转发，AOT 实现留在 `sgl-kernel` 不动**。

#### 4.5.2 核心流程

迁移是分阶段（Phase）进行的（见 git 历史）：

- Phase 2/2.5：建 spec/registry/selector 骨架，先把 activation/gemm/kvcache/layernorm/moe/quantization 等组填上；散落的 Triton/CuTe 算子登记为 inventory。
- Phase 3+4（#31666）：把 JIT 基础设施（`kernels/jit/utils`）与若干算子组迁入 `kernels.ops`。
- Phase 4 batch-2（#32015）：继续迁移算子组（无 shim，纯搬迁）。
- Phase 4 batch-3（#32045）：迁移「纠缠的 JIT 子系统 + 新组」——具体见 4.5.3。

迁移后，老 `jit_kernel` 文件变成 shim 的模式固定：docstring 指明转发目标 → `from sglang.kernels.ops.<group> import <impl> as _impl` → `globals().update({k: getattr(_impl, k) for k in dir(_impl) if not k.startswith("__")})`。

#### 4.5.3 源码精读

先看统一命名空间的总入口与算子组清单：

- [python/sglang/kernels/ops/__init__.py:17-37](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/__init__.py#L17-L37)：`_GROUPS` 元组列出**全部算子组**。
- [python/sglang/kernels/ops/__init__.py:39-40](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/__init__.py#L39-L40)：循环 `import_module` 每个组以填满 registry。

> **关于「算子组数量」的诚实说明**：本讲义规格与大纲里多处写作「20 组」，但**当前 HEAD（`40b2119b`）实际只有 19 个算子组**（`_GROUPS` 元组 19 项、`ops/` 下 19 个组目录，二者一致）。下表按**真实源码**列出这 19 组。「20」是大纲规划时的近似说法，不作为本讲的依据。

实测 19 个算子组（与 `_GROUPS` 一致）：

| 序号 | 组名 | 大致职责 |
| --- | --- | --- |
| 1 | activation | 激活函数（silu_and_mul 等） |
| 2 | attention | 各类注意力算子与元数据 kernel |
| 3 | communication | 分布式通信辅助算子 |
| 4 | diffusion | 扩散模型专用算子（conv3d、qknorm、rope 等） |
| 5 | embeddings | 嵌入相关算子 |
| 6 | gemm | 通用矩阵乘 |
| 7 | grammar | 结构化输出文法算子 |
| 8 | kvcache | KV cache 写入/reshape |
| 9 | layernorm | RMSNorm 系列（本讲范例） |
| 10 | mamba | 状态空间模型（Mamba）算子 |
| 11 | memory | 内存搬运/索引算子 |
| 12 | moe | 混合专家路由/分发/topk |
| 13 | quantization | 量化算子（per_token / per_token_group） |
| 14 | sampling | 采样相关算子 |
| 15 | spatial | 空间类算子 |
| 16 | speculative | 投机解码算子 |
| 17 | lplb | 负载均衡相关（Phase 4 batch-3 迁入） |
| 18 | kv_canary | KV canary 校验（Phase 4 batch-3 迁入） |
| 19 | model | 模型级杂项算子（Phase 4 batch-3 新增） |

Phase 4 batch-3（PR #32045，标题「migrate tangled JIT subsystems + new groups」）具体做了什么，可用 `git show --name-status 74338e94f1` 核对：

- **整组搬迁（纯 rename，R100）**：`jit_kernel/kv_canary/__init__.py` → `kernels/ops/kv_canary/__init__.py`；`jit_kernel/lplb/__init__.py` → `kernels/ops/lplb/__init__.py`。
- **新增组**：`kernels/ops/model/__init__.py`（A，全新）。
- **纠缠子系统迁入既有组**：`diffusion/__init__.py`、`moe/__init__.py`、`layernorm/__init__.py`（M，修改——把原本纠缠在别处的 JIT 算子归并进来）。

迁移后老路径变成 shim：

- [python/sglang/jit_kernel/norm.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/jit_kernel/norm.py)：docstring 写明「Compatibility shim (RFC #29630 Phase 4) -> sglang.kernels.ops.layernorm._jit_norm」，随后 `from ... import _jit_norm as _impl` + `globals().update(...)`。
- [python/sglang/jit_kernel/per_token_group_quant.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/jit_kernel/per_token_group_quant.py)：同样模式，转发到 `sglang.kernels.ops.quantization._jit_per_token_group_quant`。

JIT 基础设施新家：

- [python/sglang/kernels/jit/__init__.py](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/jit/__init__.py)：说明它镜像 legacy `jit_kernel` 树，共享构建/运行时设施在 `kernels.jit.utils`。
- [python/sglang/kernels/jit/utils/__init__.py:3-17](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/jit/utils/__init__.py#L3-L17)：导出 `load_jit`、`make_cpp_args`、`KERNEL_PATH`、`get_jit_cuda_arch`、`is_arch_support_pdl`、`is_hip_runtime` 等——这是 JIT 算子「现场编译」的公共底座。

AOT wheel 的构建配置：

- [sgl-kernel/pyproject.toml:9-24](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-kernel/pyproject.toml#L9-L24)：`name = "sglang-kernel"`、`version = "0.4.5"`，`dependencies = []`（零运行时 Python 依赖），用 scikit-build-core 构建，打包 `python/sgl_kernel`。这就是 `KernelBackend.AOT` 那个 `sgl_kernel:` 导入路径背后实实在在的 wheel。

#### 4.5.4 代码实践

**实践目标**：判断一个新算子该放哪，并核对 Phase 4 batch-3 的迁移范围。

**操作步骤**：

1. **决策练习**：假设你要加一个「fused swiglu + per-token FP8 量化」的新算子。请按下表判断它应放在 `kernels/ops` 还是 `sgl-kernel`：

| 判据 | 放 `kernels/ops`（登记 + JIT/Triton 实现或转发） | 放 `sgl-kernel`（AOT C++/CUDA） |
| --- | --- | --- |
| 是否需要预编译、跨进程稳定 wheel 边界 | 否 | 是 |
| 是否需要针对运行期实际架构特化、轻量 | 是 | 否 |
| 实现语言 | Python/Triton/CuTe DSL | C++/CUDA（csrc） |

> 参考判断：若是「轻量、可在进程内用 nvcc/hipcc/Triton 编译、且要登记进 registry 供 selector/盘点使用」→ 放 `kernels/ops`（实现可放 `kernels/ops/<group>/_jit_xxx.py`，再用 `register_kernel` 登记 JIT 后端，或用 `BaseFusedOp` 同时给 native 参考）；若是「重型 C++/CUDA、需要预编译成 wheel、追求最快首次调用」→ 放 `sgl-kernel` 的 `csrc/` + `python/sgl_kernel/`，然后在 `kernels/ops` 里用 `target="sgl_kernel:xxx"` 登记 AOT 后端。**通常两者配合**：AOT 实现在 sgl-kernel，登记与包装在 kernels.ops。

2. **核对迁移**（只读 git）：

```bash
# 列出 Phase 4 batch-3 涉及的算子组 __init__
git show 74338e94f1 --name-status --format="" | grep "kernels/ops/.*/__init__"
# 确认 kv_canary / lplb 是从 jit_kernel rename 来的
git show 74338e94f1 --name-status --format="" | grep -E "kv_canary|lplb"
```

**需要观察的现象**：第 1 步印证「实现位置」与「登记位置」可以分离——AOT 实现在 sgl-kernel，但**登记一定在 kernels.ops**。第 2 步的 git 输出应显示 `R100 jit_kernel/kv_canary/__init__.py kernels/ops/kv_canary/__init__.py`，证明是整组搬迁而非复制。

**预期结果**：你能说清「新算子决策树」，并能用 git 证据说明 batch-3 把 `kv_canary`、`lplb` 从 `jit_kernel` 搬进 `kernels.ops`、新增了 `model` 组、并把纠缠 JIT 子系统归并进 `diffusion` / `moe` / `layernorm`。

**待本地验证**：git 命令在本仓库任意 clone 上均可运行，不依赖 GPU。

#### 4.5.5 小练习与答案

**练习 1**：迁移完成后为什么还保留 `jit_kernel/`？直接删掉不行吗？

> **参考答案**：为了兼容现有调用方。仓库内外可能有大量 `from sglang.jit_kernel.norm import rmsnorm` 这样的老 import，直接删除会全部报错。保留 shim（一行 `globals().update`）让老路径无感转发到新家，实现可以全部迁走而老调用方零改动，迁移才能渐进推进。新代码则应直接 import `sglang.kernels.ops.*`。

**练习 2**：规格里写的「20 组」和你数的「19 组」矛盾，作为讲义作者该怎么处理？

> **参考答案**：以真实源码为准。`ops/__init__.py` 的 `_GROUPS` 元组和目录数都是 19，就写 19，并如实标注规格里的「20」是规划期近似。讲义的第一原则是「不编造」，宁可和规格措辞不一致也要反映当前 HEAD 的真实状态，并给出核对方法（`grep _GROUPS` 或 `ls ops/`）让读者自查。

---

## 5. 综合实践

把本讲五个模块串起来，完成一次「**从登记到解析到回退的全链路追踪**」。

**任务**：选择 layernorm 组的 `rmsnorm` 算子，回答四个问题，每个问题给出源码行号证据。

1. **登记链路**：`RMSNormOp` 经 `register_fused_op` 后，在 registry 里产生了哪几条 `KernelSpec`？它们的 `backend` 和 `target` 分别是什么？（提示：看 [fused_op.py 的 register_fused_op](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/fused_op.py#L324-L344) 与 [layernorm 的 capabilities](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/layernorm/__init__.py#L55-L62)）

2. **selector 解析**：在 CUDA 平台上，`select_kernel("layernorm.rmsnorm")` 会因为「多个 eligible」而要求点名吗？为什么？（提示：注意每个后端的 `capabilities` 把不同来源钉到了不同设备，结果在单一设备上往往只剩一个 eligible）

3. **BaseFusedOp 优先级**：在 ROCm 平台上，`_RMSNORM.forward(x, w)` 实际调用的是哪个 `forward_<backend>`？为什么不是 `forward_aot`？（提示：[AOT 在 capabilities 里被钉成 CUDA-only](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/ops/layernorm/__init__.py#L57-L58)，HIP 上 AOT 不 eligible，优先级链滑到 AITER）

4. **一键回退**：设 `SGLANG_FORCE_FUSED_OP_BACKEND=torch` 后，上述第 3 问的结果会变成什么？为什么？（提示：[_resolve_backend 先看 forced](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/fused_op.py#L295-L302)，强制优先于一切能力过滤）

**交付物**：一份简短笔记，画出「`RMSNormOp` 类 → `register_fused_op` → registry（4~5 条 spec）→ `select_kernel` 设备过滤 → `BaseFusedOp.forward` 优先级 → `forward_<backend>`」的完整链路图，并在每个节点标注你引用的源码行号。完成这个练习，你就把本讲的「元数据 → 清单 → 解析 → 多后端分发 → 现状」整条主线彻底打通了。

## 6. 本讲小结

- SGLang 用 RFC #29630 的统一算子命名空间 `sglang.kernels`，把散落的 JIT/AOT/Triton 算子收敛到「元数据登记 + 设备感知解析 + 多后端融合」一套体系。
- **`KernelSpec`** 是纯元数据记录（op / backend / target / capabilities），登记不导入 torch、不触发编译；`KernelBackend` 描述「来源」、`CapabilityRequirement` 描述「设备能力」（OR 语义）、`PlatformInfo` 是运行期平台快照——来源与设备被刻意解耦。
- **`KernelRegistry`** 是进程级清单，`register_kernel` 是唯一登记入口，`(op, backend)` 重复登记时覆盖以保幂等；同名算子可挂多个来源。
- **`selector`** 做设备感知的**固定路径解析**（无偏好启发式）：单后端直接选；多后端先按 `CapabilityRequirement` 硬过滤，唯一则选、为零则报错、仍多则要求点名；`get_kernel` 在其上加 `lru_cache` 成为公共包装的快路径。
- **`BaseFusedOp`** 是「一个算子多个可互换后端」的聚合器：`forward_native` 必填、其余 `forward_<backend>` 按需重写，`forward()` 按 `priority` + 能力过滤自动选最优并安全退到 native；`SGLANG_FORCE_FUSED_OP_BACKEND` 一键全量回退做数值排查。
- **现状**：当前 HEAD 下 `kernels/ops/` 实测 **19 个算子组**（不是规格里说的 20）；AOT 实现在独立的 `sgl-kernel` wheel，JIT 基础设施在 `kernels/jit/utils`，legacy `jit_kernel/` 退化为 `globals().update` 转发 shim；Phase 4 batch-3（#32045）把 `kv_canary` / `lplb` 整组搬入、新增 `model` 组、并把纠缠 JIT 子系统归并进 diffusion/moe/layernorm。

## 7. 下一步学习建议

本讲把「算子怎么来、怎么选」讲清了，接下来两条路：

- **向上看调用方**：回到 u5-l3（注意力后端）和 u11-l1（量化方案），观察这些上层模块如何 `from sglang.kernels.ops.* import` 取用算子——你会看到本讲的 registry/selector/fused_op 正是它们的底座。可以顺着 `layernorm` 在 `srt/layers/layernorm.py` 里的调用，验证「同一算子在不同设备自动换后端」的真实效果。
- **向深看算子实现**：若要新增算子，先读仓库内的两个 skill——`add-jit-kernel`（加轻量 JIT CUDA 算子）和 `add-sgl-kernel`（加重型 AOT C++/CUDA 算子，含测试与 benchmark）。它们会告诉你新算子的实现放哪、如何在本讲的 `kernels/ops` 里登记、如何为它写对齐 `forward_native` 的正确性测试。

建议下一步通读 [python/sglang/kernels/README.md](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/kernels/README.md) 与 RFC #29630 的讨论，把「统一正确性测试 / 一键排查 / 安全降级 / 渐进优化」这四个设计目标与本讲源码一一对应起来。
