# u8-l8 确定性执行：deterministic_mode 从配置到内核

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `deterministic_mode` 的设计定位：它是一个 **opt-in（显式开启）的执行策略开关**，只决定「以什么方式执行计算」，与「数值兼容性」（fp16/bf16/TF32 等精度策略）完全正交；当前只支持 NVIDIA CUDA，KLXPU、ROCm 等后端开启时会直接抛 `ValueError`。
2. 完整追踪配置到生效的链路：`DeepSpeedConfig` 构造期 `configure_determinism` 只解析布尔开关（不改任何全局状态）→ 引擎构造最早期 `apply_determinism` 才真正应用 `torch.use_deterministic_algorithms(True)` 与 TF32/cuDNN 开关。
3. 解释 `CUBLAS_WORKSPACE_CONFIG` 为什么必须在 CUDA 初始化**之前**由用户设置、DeepSpeed 为什么只「校验」而不「代设」。
4. 理解确定性要求如何传导到 PyTorch 检查覆盖不到的地方：Triton grouped GEMM 的 `USE_IEEE` 路径（`input_precision="ieee"` 替代 TF32 tensor core）与固定 tile 配置（绕过计时型 autotune）、legacy transformer 内核对 `stochastic_mode` 的拒绝、FPDT 对 FlashAttention backward 的传递。
5. 准确辨析该开关的**边界**：不改变精度、NCCL 算法、并行与重叠；不保证自定义内核与分布式调度的比特级可重复；进程全局设置销毁引擎后也不恢复。

本讲是「高级机制」单元的一篇，依赖 u2-l2（配置系统）、u2-l3（引擎构造流程）与 u8-l3（跨拓扑迁移对可复现实验的诉求）的认知。

## 2. 前置知识

### 2.1 什么是「确定性执行」

确定性（determinism）指的是：**相同的种子、相同的软硬件栈与拓扑下，两次运行产生逐位相同（bitwise identical）的结果**。它与「正确性」无关——每次结果都可能是数值上完全正确的，但两次运行之间最后几位小数不同，这就是非确定。

深度学习训练里非确定的三个典型来源：

1. **原子加的归约顺序**。`index_add`、`scatter_add` 以及许多 backward 内核用 `atomicAdd` 累加梯度。浮点加法不满足结合律：

   \[ (a + b) + c \neq a + (b + c) \quad \text{（一般情形）} \]

   而 atomicAdd 的到达顺序每次运行都可能不同，于是累加结果的舍入路径不同。
2. **计时型的算法选择**。cuDNN benchmark 模式（`torch.backends.cudnn.benchmark=True`）会实测各卷积算法后挑最快的；Triton 的 `@triton.autotune` 同样实测各 tile 配置。机器负载、时钟频率的抖动会让「最快的」每次不同，而不同算法/配置对应不同的累加分组，结果就不同。
3. **TF32 精度**。Ampere 及以后的 NVIDIA GPU 上，fp32 矩阵乘默认可走 TF32 tensor core：输入先舍入到 1 位符号 + 8 位指数 + 10 位显式尾数的 TF32 格式再相乘，单次乘积对输入的相对舍入误差量级为 \(2^{-11}\)，与 IEEE fp32 逐位乘不同（详见 4.2.2 节）。

### 2.2 cuBLAS workspace 是什么

cuBLAS（CUDA 的 BLAS 矩阵乘库）在初始化句柄时会按环境变量 `CUBLAS_WORKSPACE_CONFIG` 分配工作区。PyTorch 文档规定：要让 `torch.use_deterministic_algorithms(True)` 对 cuBLAS 操作生效，必须在 **CUDA/cuBLAS 初始化之前** 把该变量设为 `:4096:8` 或 `:16:8`——一旦句柄已建立，workspace 尺寸就定死了，之后再改环境变量无效。这是理解 4.2 节「只校验、不代设」的关键。

### 2.3 本讲用到的 DeepSpeed 既有机制

- **配置两阶段设计**（u2-l2）：`DeepSpeedConfig` 构造期只做解析与校验，不碰进程全局状态；应用推迟到引擎构造期。`deterministic_mode` 是这一设计的典型样本。
- **引擎八阶段骨架**（u2-l3）：`DeepSpeedEngine.__init__` 从「基础属性与确定性」开始分阶段构造，`apply_determinism` 就在第一阶段。
- **加速器抽象层**（u3-l1）：`get_accelerator()` 返回当前后端单例；`device_name()` 是 torch 设备字符串，`_name` 是 DeepSpeed 自己的身份名——两者不一定一致（KLXPU 前者报 `"cuda"`、后者报 `"klxpu"`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/runtime/determinism.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/determinism.py) | 全部核心逻辑只有 39 行：`configure_determinism`（解析）、`_is_nvidia_cuda`（后端判别）、`apply_determinism`（应用） |
| [deepspeed/runtime/config.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/config.py) | `DeepSpeedConfig.__init__` 中调用 `configure_determinism` 的挂载点（L482-L483） |
| [deepspeed/runtime/engine.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/engine.py) | `DeepSpeedEngine.__init__` 最早期调用 `apply_determinism` 的应用点（L551-L553） |
| [deepspeed/ops/triton_ops/group_gemm_triton.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/group_gemm_triton.py) | Triton grouped GEMM：`USE_IEEE` 精度分支与固定 tile 配置绕过 autotune |
| [deepspeed/ops/transformer/transformer.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/transformer/transformer.py) | legacy `DeepSpeedTransformerLayer`：`stochastic_mode` 与确定性互斥的校验 |
| [deepspeed/sequence/fpdt_layer.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/fpdt_layer.py) | FPDT 把 PyTorch 确定性标志传给 FlashAttention backward |
| [tests/unit/runtime/test_determinism.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/test_determinism.py) | 契约测试：后端拒绝、workspace 校验、no-op、独立进程 CUDA 训练逐位对比 |
| [docs/_tutorials/deterministic-mode.md](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/deterministic-mode.md) | 官方契约声明：能保证什么、不能保证什么 |
| [docs/_pages/config-json.md](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_pages/config-json.md) | 面向用户的配置项文档（L7-L19） |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**configure_determinism 配置解析** → **apply_determinism 引擎应用点与后端限制** → **确定性要求的内核传导（Triton IEEE 路径等）** → **确定性边界与文档声明**。

### 4.1 configure_determinism：配置解析期什么都不改

#### 4.1.1 概念说明

`deterministic_mode` 是一个 **JSON 顶层布尔字段**，默认 `false`。设计上的第一原则是「解析与应用分离」：

- `DeepSpeedConfig` 构造时只做三件事——读出布尔值、强制类型检查、挂到 `config.deterministic_mode` 属性上；
- 不修改用户传入的配置字典，也不碰任何进程全局 PyTorch 设置。

为什么这么设计？因为 `DeepSpeedConfig` 的构造可能发生在「还没决定要不要真的初始化引擎」的场合（比如仅做配置校验、日志打印）。如果解析期就全局打开 `torch.use_deterministic_algorithms`，一个只读配置的操作会污染整个进程。教程文档明确写了这条契约：`Parsing does not change input configuration or process-global PyTorch settings.`（[docs/_tutorials/deterministic-mode.md:21](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/deterministic-mode.md#L21)）

#### 4.1.2 核心流程

```text
DeepSpeedConfig.__init__(param_dict)
  ├─ 解析输入（dict / hjson 文件 / base64）
  ├─ _reject_removed_config_keys
  ├─ _initialize_params(param_dict)         # 各顶层字段
  ├─ _configure_train_batch_size()          # batch 三选二
  ├─ configure_determinism(self, param_dict)  # ← 本模块：只解析+挂属性
  └─ _do_sanity_check()
```

注意挂载点在 `_do_sanity_check` **之前**、batch size 推导**之后**——与 u2-l2 总结的顺序一致。

#### 4.1.3 源码精读

解析函数本体只有 5 行：

[deepspeed/runtime/determinism.py:10-14](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/determinism.py#L10-L14) —— `configure_determinism` 从 `param_dict` 读取 `deterministic_mode`（缺省 `False`）；用 `type(enabled) is not bool` 严格校验必须是 JSON 布尔（写 `1`、`0`、`"true"` 都会抛 `ValueError`，这比 `if not enabled` 严格——`1` 会被误当 `True`）；最后只做一件事：把结果写到 `config.deterministic_mode` 属性。

调用挂载点：

[deepspeed/runtime/config.py:478-484](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/config.py#L478-L484) —— `DeepSpeedConfig.__init__` 先 `copy.copy(self._param_dict)` 传副本（注释说明是为了不修改用户 json，例如用于日志），随后依次 `_initialize_params` → `_configure_train_batch_size` → `configure_determinism(self, param_dict)` → `_do_sanity_check()`。确定性解析被插在 batch size 推导之后、一致性检查之前。

配套的 no-op 测试证明「关闭时完全无副作用」：

[tests/unit/runtime/test_determinism.py:64-68](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/test_determinism.py#L64-L68) —— `test_disabled_runtime_is_noop` 先删掉 `CUBLAS_WORKSPACE_CONFIG` 环境变量，记录 `torch.are_deterministic_algorithms_enabled()` 的当前值，构造一个不带 `deterministic_mode` 的配置并调用 `apply_determinism`，断言全局开关前后不变。这个测试同时覆盖了「解析」与「应用」两层的 no-op 语义。

#### 4.1.4 代码实践

**实践目标**：验证解析期的类型严格性与无副作用。

操作步骤（以下为示例代码，非项目原有文件）：

```bash
python - <<'EOF'
import torch
from deepspeed.runtime.config import DeepSpeedConfig

# 1) 合法布尔：解析成功，且不碰全局状态
before = torch.are_deterministic_algorithms_enabled()
cfg = DeepSpeedConfig({"train_batch_size": 4, "deterministic_mode": True})
assert cfg.deterministic_mode is True
assert torch.are_deterministic_algorithms_enabled() == before  # 解析不改全局

# 2) 非布尔值：严格拒绝
for bad in (1, 0, "true", None):
    try:
        DeepSpeedConfig({"train_batch_size": 4, "deterministic_mode": bad})
        print(f"{bad!r}: 意外通过")
    except ValueError as e:
        print(f"{bad!r}: ValueError({e})")
EOF
```

需要观察的现象：第 1 步两个断言都通过；第 2 步对 `1`、`0`、`"true"`、`None` 全部抛出 `deterministic_mode must be a JSON boolean`。

预期结果：解析只挂属性、不改全局；类型校验严格。本实践可在纯 CPU 环境（无任何 GPU）执行。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么用 `type(enabled) is not bool` 而不是 `if not isinstance(enabled, bool)`？两者有实际差别吗？

**答案**：对 `True/False` 两者等价。但 `type(...) is bool` 连 `bool` 的子类实例也拒绝（`isinstance` 会放行子类），是更严格的写法；实践中主要防线是拒绝 `1`/`0`（`int` 会被 `if enabled` 误判为真）与字符串。核心点：JSON 里 `true` 解析成 Python `bool`，而手写 dict 里很容易误传 `1`，这里在配置边界就拦住。

**练习 2**：如果用户在 JSON 里同时写了 `deterministic_mode: true` 和一个不存在的顶层键，哪个先报错？

**答案**：都不报「不存在的键」——DeepSpeed 顶层标量历史上是宽容的；真正会先触发的是 `_reject_removed_config_keys`（若命中已移除键）与 `_do_sanity_check`（在 `configure_determinism` 之后执行）。所以布尔类型错误的报错时机早于 sanity check。

**练习 3**：`configure_determinism` 为什么不顺便检查 `CUBLAS_WORKSPACE_CONFIG`？

**答案**：检查的语义不同。解析期检查会在「用户只想校验配置」时就因环境变量缺失而失败，且此时报告为时过早——用户可能稍后、在启动引擎前设置。环境变量校验属于「应用期前置条件」，放在 `apply_determinism` 里才能保证「检查通过的那一刻真的要应用了」。

### 4.2 apply_determinism：引擎应用点与后端限制

#### 4.2.1 概念说明

`apply_determinism` 是真正改变进程全局执行策略的地方。它做四件事，顺序严格：

1. **早退**：`deterministic_mode` 为假直接返回（任何后端、任何环境变量状态都不受影响——「关闭即 no-op」）。
2. **后端门卫**：只允许 NVIDIA CUDA。KLXPU（设备名报 `"cuda"` 但身份是 `"klxpu"`）和 ROCm（`torch.version.hip` 非空）都会被明确拒绝。
3. **环境变量前置校验**：`CUBLAS_WORKSPACE_CONFIG` 必须已经是 `:4096:8` 或 `:16:8`，否则报错。**只校验、不代设**。
4. **应用全局开关**：`torch.use_deterministic_algorithms(True, warn_only=False)`（严格模式：遇到已知非确定且无替代实现的算子直接抛异常），并关掉 TF32（matmul 与 cuDNN 两个开关）、关掉 cuDNN benchmark、打开 cuDNN deterministic。

为什么在引擎构造的**最早期**应用？因为引擎构造的后续阶段（模型搬运到设备、优化器状态初始化、首个 forward）马上就要执行 CUDA 计算；确定性策略必须在任何一次 CUDA 矩阵乘之前就位，否则「前面的计算已经非确定了」。

#### 4.2.2 核心流程

```text
DeepSpeedEngine.__init__(...)
  ├─ ...基础属性...
  ├─ self._config = config_class
  ├─ if self._config.deterministic_mode:      # ← 应用点（L551-L553）
  │     apply_determinism(self._config)
  │       ├─ enabled? --否--> return（no-op）
  │       ├─ _is_nvidia_cuda(accelerator)? --否--> ValueError
  │       ├─ CUBLAS_WORKSPACE_CONFIG ∈ {:4096:8, :16:8}? --否--> ValueError
  │       └─ torch.use_deterministic_algorithms(True, warn_only=False)
  │          torch.backends.cuda.matmul.allow_tf32 = False
  │          torch.backends.cudnn.allow_tf32 = False
  │          torch.backends.cudnn.benchmark = False
  │          torch.backends.cudnn.deterministic = True
  └─ ...后续 7 个阶段（分布式、优化器包装、检查点...）
```

关于 TF32 的数值含义：TF32 把 fp32 输入舍入到 10 位显式尾数后再进 tensor core。对输入 \(x\) 的相对舍入误差为

\[ |x - \mathrm{tf32}(x)| \le 2^{-11} \cdot |x| \]

（尾数 10 位，ulp 为 \(2^{-10}\)，最近舍入误差不超过半个 ulp）。所以 TF32 乘积 \(a \cdot b\) 与 IEEE fp32 乘积逐位不同；确定性与「回到 IEEE fp32 乘」在这里是同一件事：`allow_tf32 = False`。

#### 4.2.3 源码精读

引擎侧应用点：

[deepspeed/runtime/engine.py:549-553](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/engine.py#L549-L553) —— `DeepSpeedEngine.__init__` 在 `self._config = config_class` 赋值（L550）之后**立即**检查 `deterministic_mode` 并调用 `apply_determinism`，此刻连 `self.dist_backend`（L558）都还没查询，更没有任何模型搬运或 CUDA 计算发生。这就是 u2-l3 所讲八阶段骨架中「阶段 1：基础属性与确定性」的位置。

后端判别函数：

[deepspeed/runtime/determinism.py:17-21](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/determinism.py#L17-L21) —— `_is_nvidia_cuda` 同时要求三个条件：`device_name() == "cuda"`（torch 设备字符串）、`_name == "cuda"`（DeepSpeed 身份名）、`torch.version.hip is None`（非 ROCm）。注释直接说明了两个反例：KLXPU 从 CUDA 继承 `device_name()` 返回 `"cuda"` 但身份名是 `"klxpu"`；ROCm 也报 `"cuda"` 但 `torch.version.hip` 非空。**两个名字空间必须同时匹配**，这正是 u3-l1 讲过的「torch 通道 vs DeepSpeed 身份」区分在此处的实际应用。

应用函数本体：

[deepspeed/runtime/determinism.py:24-38](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/determinism.py#L24-L38) —— `apply_determinism` 依次执行：非 NVIDIA CUDA 抛 `ValueError`（L29-L30）；`CUBLAS_WORKSPACE_CONFIG` 不在 `(":4096:8", ":16:8")` 之内抛 `ValueError`（L32-L33），L31 的注释解释了为什么**不在这里代设**：`Do not set this here: CUDA may already have initialized cuBLAS workspaces.`——若进程里 CUDA 已经初始化过 cuBLAS，此刻再写环境变量也来不及了，静默设置只会制造「看起来开了实际没生效」的假象，不如把失败显式抛给用户；最后四行应用严格确定性算法与 TF32/cuDNN 开关（L34-L38）。注意 `get_accelerator` 是函数内延迟导入（L27），符合「`import deepspeed` 不得初始化 CUDA」的项目纪律。

后端拒绝的两个测试：

[tests/unit/runtime/test_determinism.py:18-31](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/test_determinism.py#L18-L31) —— `test_runtime_rejects_rocm`：用 monkeypatch 伪造 `_name="cuda"`、`device_name()="cuda"` 的加速器，并把 `torch.version.hip` 设为 `"6.0"`，断言 `apply_determinism` 抛出含 `NVIDIA CUDA` 的 `ValueError`——单靠设备名无法过关，hip 检测是独立防线。

[tests/unit/runtime/test_determinism.py:34-49](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/test_determinism.py#L34-L49) —— `test_runtime_rejects_cuda_compatible_accelerator`：伪造 KLXPU 形态（`_name="klxpu"`、`device_name()="cuda"`、hip 为 None），断言同样抛 `ValueError`，并且**额外断言 `torch.are_deterministic_algorithms_enabled()` 前后不变**——后端拒绝发生在触碰任何全局开关之前，拒绝是干净的、不留半启用状态。

workspace 校验测试：

[tests/unit/runtime/test_determinism.py:52-61](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/test_determinism.py#L52-L61) —— `test_runtime_requires_workspace` 参数化 `None`（删掉变量）、`""`、`"invalid"` 三种非法值，断言抛含 `CUBLAS_WORKSPACE_CONFIG` 的 `ValueError`；非 NVIDIA CUDA 环境用 `pytest.skip` 跳过——这个测试需要真 CUDA 后端，mock 不了 workspace 检查前的早退路径（它排在后端检查之后，真机才有意义）。

#### 4.2.4 代码实践

**实践目标**：在 CPU 后端复现后端拒绝的 `ValueError`，并观察「拒绝不留全局副作用」。

操作步骤（示例代码）：

```bash
python - <<'EOF'
import torch
from deepspeed.runtime.config import DeepSpeedConfig
from deepspeed.runtime.determinism import apply_determinism

cfg = DeepSpeedConfig({"train_batch_size": 4, "deterministic_mode": True})
before = torch.are_deterministic_algorithms_enabled()
try:
    apply_determinism(cfg)
except ValueError as e:
    print("ValueError:", e)
assert torch.are_deterministic_algorithms_enabled() == before
print("全局确定性开关未被触碰:", before)
EOF
```

需要观察的现象：在没有 NVIDIA GPU 的机器上（CPU 后端 `device_name()` 返回 `"cpu"`），打印 `ValueError: deterministic_mode currently supports NVIDIA CUDA only`，且最后一个断言通过。

预期结果：与 `test_runtime_rejects_cuda_compatible_accelerator` 的断言方式一致——「抛错 + 全局状态不变」。若在 NVIDIA CUDA 机器上运行本脚本且已设 `CUBLAS_WORKSPACE_CONFIG=:4096:8`，则会**成功应用**五个全局开关而不是抛错（那是 4.4 节综合实践的场景）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply_determinism` 不自己 `os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"` 帮用户设好？

**答案**：环境变量只在 cuBLAS 句柄创建时读取一次。到引擎构造时，进程里很可能已经跑过 CUDA 操作（模型构建、`deepspeed.initialize` 之前的用户代码），workspace 已经按旧配置分配完；此时代设环境变量毫无效果，却会让用户误以为确定性已生效——静默假阳性比显式报错危险得多。所以 DeepSpeed 选择「校验而非安装」，并把失败信息写清楚（教程原句：`Workspace settings are validated, not installed too late.`）。

**练习 2**：`torch.use_deterministic_algorithms(True, warn_only=False)` 里 `warn_only=False` 意味着什么？改成 `True` 会怎样？

**答案**：`warn_only=False`（严格模式）下，PyTorch 遇到已知非确定且无确定性替代实现的算子（如某些 `index_add` CUDA 路径）直接抛 `RuntimeError`；`warn_only=True` 则只发警告继续跑。DeepSpeed 选严格模式，理由是「半确定的确定性模式」会误导调试。教程还注明：即便是 warn-only 模式（用户自己改的），Triton grouped GEMM 的确定性路径依然会被选中，因为它们查的是 `are_deterministic_algorithms_enabled()` 而非 warn_only 标志。

**练习 3**：一个用户在 KLXPU 集群上设置 `deterministic_mode: true` 训练，报错信息是什么？如果他想「先跑通再说」该怎么办？

**答案**：`ValueError: deterministic_mode currently supports NVIDIA CUDA only`。正确做法是把配置里的 `deterministic_mode` 去掉或设为 `false`——文档明确「disabled 行为完全不变（no-op）」，而不是去伪造环境变量或加速器身份。

### 4.3 确定性要求的内核传导：Triton IEEE 路径与 stochastic_mode 拒绝

#### 4.3.1 概念说明

`torch.use_deterministic_algorithms(True)` 只能管住 **PyTorch 原生算子**。DeepSpeed 有一批自己的内核，PyTorch 的检查鞭长莫及，确定性要求必须由各内核**自行查询并响应**。目前的传导点有三处：

1. **Triton grouped GEMM（正向传导）**：两个非确定来源都处理了——(a) `@triton.autotune` 是计时型选择，绕过之；(b) `tl.dot` 对 fp32 输入默认用 TF32 tensor core，而 `torch.backends.cuda.matmul.allow_tf32=False` **管不到 Triton**，所以显式传 `input_precision="ieee"`。
2. **legacy `DeepSpeedTransformerLayer`（反向拒绝）**：其 `stochastic_mode=True` 的高性能 CUDA 内核本身就是随机化的，与确定性互斥，直接抛 `RuntimeError`。
3. **FPDT（参数传递）**：把 PyTorch 确定性标志原样传给 FlashAttention backward 的 `deterministic` 形参，由 flash-attn 内核选择确定性的 dq/dk/dv 累加实现。

顺带说明：`deepspeed/ops/triton_ops/swiglu_triton.py` 既不含 `tl.dot`（无 TF32 问题）也不含计时型 autotune，是逐元素内核，因此不在确定性关键路径上、无需特殊处理。

#### 4.3.2 核心流程

grouped GEMM 的确定性选择发生在每次 kernel launch 的公共入口：

```text
_launch_group_gemm(kernel, configs, grid, ...)
  ├─ deterministic = torch.are_deterministic_algorithms_enabled()   # 查询全局开关
  ├─ kwargs["USE_IEEE"] = deterministic                             # 精度位传入内核
  └─ if deterministic:
        config = configs[0]          # 固定取第一个配置
        kernel.fn[grid](..., **config.kwargs, num_warps=..., num_stages=...)  # 绕过 autotune
     else:
        kernel[grid](...)            # 正常走 @triton.autotune（计时选择）
```

内核内部（前向与权重梯度两个内核同构）：

```text
if USE_IEEE:                                    # tl.constexpr，编译期特化
    acc += tl.dot(a_tile, b_tile, input_precision="ieee")   # IEEE fp32 乘加
else:
    acc += tl.dot(a_tile, b_tile)               # fp32 默认走 TF32 tensor core
```

为什么 autotune 非确定：`@triton.autotune(configs=..., key=[...])` 会在每个新 key（此处是 `KC/NO/NUM_GROUPS` 等形状参数）首次出现时实测全部 6 个候选配置并缓存最优者。实测耗时受机器瞬时负载影响，两次运行可能选中不同的 `BLOCK_K`，而 `BLOCK_K` 决定 K 维归约的分组方式——`acc += tl.dot(...)` 的累加分组变了，舍入路径就变了。

#### 4.3.3 源码精读

launch 公共入口（传导的枢纽）：

[deepspeed/ops/triton_ops/group_gemm_triton.py:311-319](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/group_gemm_triton.py#L311-L319) —— `_launch_group_gemm` 每次发射前查询 `torch.are_deterministic_algorithms_enabled()`，把结果作为 `USE_IEEE` constexpr 传入；确定性模式下 `config = configs[0]` 固定取候选表第一项，并通过 `kernel.fn[grid]` 直接发射——注释写明这是 `Bypass timing-dependent autotuning without changing its shared cache/configs`（绕过计时型 autotune，且不动 autotune 的共享缓存与配置表）。非确定模式照常走 `kernel[grid]` 的 autotune 路径。

前向内核的精度分支：

[deepspeed/ops/triton_ops/group_gemm_triton.py:147-151](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/group_gemm_triton.py#L147-L151) —— `_group_gemm_kernel` 的 K 维循环里，`USE_IEEE` 为真时 `tl.dot(a_tile, b_tile, input_precision="ieee")` 强制 IEEE fp32 乘加；否则 `tl.dot` 默认路径，注释标明 `The regular FP32 path uses TF32 tensor cores for performance.`。`USE_IEEE: tl.constexpr`（L89 声明）使分支在编译期特化，不付运行时判断代价。

权重梯度内核的同构分支：

[deepspeed/ops/triton_ops/group_gemm_triton.py:237-241](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/group_gemm_triton.py#L237-L241) —— `_group_gemm_dw_kernel`（计算 `out[e] = a_e.T @ g_e`）对 `tl.dot(tl.trans(a_tile), g_tile, ...)` 做同样的 IEEE/TF32 分支。前向、输入梯度（复用 `_group_gemm`）、权重梯度（本内核）三条路径因此全部覆盖——与教程声明一致。

被绕过的 autotune 装饰器与候选表：

[deepspeed/ops/triton_ops/group_gemm_triton.py:49-70](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/group_gemm_triton.py#L49-L70) —— `_gmm_configs()` 给出 6 个 `(BLOCK_M, BLOCK_N, BLOCK_K, num_warps, num_stages)` 候选；`@triton.autotune(configs=_gmm_configs(), key=["KC", "NO", "NUM_GROUPS"])` 按形状键缓存计时选优结果。确定性模式绕过它、固定 `configs[0]`（即 `(32, 128, 32, 8, 4)`），代价是可能选不到该形状下最快的配置——教程如实声明 `may reduce performance`。

transformer 内核的互斥拒绝：

[deepspeed/ops/transformer/transformer.py:143-145](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/transformer/transformer.py#L143-L145) —— `_validate_stochastic_mode`：`config.stochastic_mode` 与 `torch.are_deterministic_algorithms_enabled()` 同时为真即抛 `RuntimeError`。

[deepspeed/ops/transformer/transformer.py:154-156](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/transformer/transformer.py#L154-L156) —— 前向入口处调用校验，L154 的注释点出关键时序：`Engines can enable deterministic execution after the layer was constructed.`（引擎可能在层构造**之后**才打开确定性，所以不能只在构造期查一次）。backward 侧在 [deepspeed/ops/transformer/transformer.py:259-260](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/transformer/transformer.py#L259-L260) 再查一次，加上模块前向路径的一处（L322），构成构造/前向/反向多道把关。

FPDT 的参数传递：

[deepspeed/sequence/fpdt_layer.py:421-426](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/sequence/fpdt_layer.py#L421-L426) —— FPDT 反向按 flash-attn 版本分三档调用 `_flash_attn_backward`，每档都把 `deterministic=torch.are_deterministic_algorithms_enabled()` 作为关键字实参传入（另两档见 L407、L940 附近同型调用），让 flash-attn 自己切换确定性的梯度累加实现。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——确认「PyTorch TF32 开关管不到 Triton」这一传导设计的必要性，并追踪 `USE_IEEE` 的完整数据流。

操作步骤：

1. 读 [deepspeed/ops/triton_ops/group_gemm_triton.py:311-319](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/ops/triton_ops/group_gemm_triton.py#L311-L319)，回答：`USE_IEEE` 的取值从哪个函数查询来？如果不传这个 constexpr，`tl.dot` 对 fp32 输入会用什么精度？
2. 全局搜索 `are_deterministic_algorithms_enabled` 在整个 `deepspeed/` 包里的全部命中（应有 `group_gemm_triton.py`、`transformer.py`、`fpdt_layer.py`、`determinism.py` 四个文件），把每个命中归类为「查询以选择路径」还是「查询以拒绝组合」。
3. （可选，需 GPU + Triton）对比 IEEE 与 TF32 的数值差：对同一组 fp32 输入分别以 `deterministic` 开/关各跑一次 `group_gemm_triton`，断言两次输出的最大绝对差为 0 之外的有限值（TF32 舍入差异量级约 \(2^{-11} \cdot \|A\|\|B\|\)），再开确定性跑两次断言逐位相等。

需要观察的现象：步骤 2 应找到 6 处左右命中，全部是对 `torch.are_deterministic_algorithms_enabled()` 的**读查询**——内核响应确定性的统一模式是「查全局开关、各自让步」，不存在任何内核去「设置」该开关（设置只属于 `apply_determinism` 单点）。

预期结果：能画出「`apply_determinism` 设置 → 各内核查询」的星型传导图。步骤 3 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`torch.backends.cuda.matmul.allow_tf32 = False` 已经关了 TF32，为什么 Triton 内核还要单独的 `input_precision="ieee"`？

**答案**：PyTorch 的 TF32 开关只作用于 PyTorch 调用的 cuBLAS/cuDNN 矩阵乘。Triton 内核里的 `tl.dot` 由 Triton 编译器直接生成 tensor core 指令，fp32 输入下默认仍用 TF32 精度，完全不经过 PyTorch 的后端开关。所以必须用 Triton 自己的 `input_precision="ieee"` 参数显式覆盖——这正是教程那句 `disabling PyTorch TF32 does not control Triton input precision` 的含义。

**练习 2**：固定 `configs[0]` 而不是「跑一次计时后锁定最优」有什么取舍？

**答案**：跑一次计时再锁定，结果仍依赖当次机器状态，跨进程/跨节点不可复现（每个进程都要重新计时，不同节点可能锁定不同配置）；固定 `configs[0]` 是纯函数式的确定选择，任何机器任何时刻都一样，代价是可能不是该形状的最优 tile——用性能换可复现，且教程已声明 `may reduce performance`。

**练习 3**：为什么 legacy transformer 选「拒绝」而不是「自动关闭 stochastic_mode 继续跑」？

**答案**：`stochastic_mode=True` 是用户显式要的高性能随机化执行，静默改写用户意图（自动降级）会让性能与行为都偏离预期。确定性模式下「拒绝并报错」把选择权交还用户：要么关 `stochastic_mode`，要么关确定性。教程原句：`legacy stochastic execution raises rather than silently proceeding`。

### 4.4 确定性边界：能保证什么、不能保证什么

#### 4.4.1 概念说明

这是本讲最重要、也最容易被误解的模块。`deterministic_mode` 的契约边界（全部来自官方文档声明）：

**它能保证/做到的**：

1. PyTorch 原生算子走确定性实现；已知非确定且无替代的算子在严格模式下直接抛异常。
2. TF32（cuBLAS matmul、cuDNN、Triton grouped GEMM 三处）被关闭，回到 IEEE fp32 乘。
3. cuDNN benchmark 关闭、deterministic 打开——算法选择不再依赖计时。
4. Triton grouped GEMM 的 tile 配置固定、FPDT 传确定性标志、stochastic_mode 组合被拒绝。

**它不保证/不做的**：

1. **不改变数值精度策略**：不改变梯度精度、GAS、TP/EP、overlap、NCCL 算法、AutoEP 算术——「开确定性」不等于「换 Megatron 数值方案」。
2. **不覆盖自定义内核与分布式调度**：PyTorch 的检查管不到 DeepSpeed/用户自定义 CUDA 内核与集合通信的调度；配置被接受 ≠ 负载比特级可复现，需要在固定软硬件栈与拓扑上独立验证整个负载。
3. **不追认历史**：引擎初始化不能让更早的模型构建或 CUDA 操作变确定；种子、数据顺序、checkpoint 的 RNG/dataloader 状态都由调用方负责。
4. **进程全局且不可逆**：设置在引擎销毁或后续创建禁用引擎时**不恢复**——它是进程级策略，不是引擎级作用域。
5. 不提供 Megatron 比特级兼容承诺；不因「配置没测过」而拒绝配置（现有校验照常执行）。

#### 4.4.2 核心流程

官方契约测试如何验证「跨进程逐位可复现」：

```text
test_independent_cuda_training(stage ∈ {0, 2, 3})
  for repeat in 0..1:
      subprocess: torchrun --standalone --nproc_per_node=1 本测试文件
                  env: CUBLAS_WORKSPACE_CONFIG=":4096:8", OMP_NUM_THREADS="1"
      worker(): 种子 1234 → Linear-Tanh-Linear + AdamW
                → deepspeed.initialize(deterministic_mode=True, zero stage)
                → 断言 5 个全局开关已就位
                → 6 个 microstep，边界处快照 output/grad/master/exp_avg/exp_avg_sq
                → torch.save(snapshots)
  assert_equal(两次快照)   # uint8 视图逐位比较
```

两个设计要点：**独立子进程**（不是同进程跑两遍——同进程会共享 cuBLAS workspace、autotune 缓存等隐藏状态，独立进程才是真可复现性的最小检验单位）；**uint8 逐位比较**（`assert_equal` 把张量 reshape 成 uint8 视图后 `torch.equal`，不做任何容差比较）。

#### 4.4.3 源码精读

教程中的边界声明（原文即契约）：

[docs/_tutorials/deterministic-mode.md:21-32](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/deterministic-mode.md#L21-L32) —— 逐句给出：解析不改配置与全局设置；引擎初始化启用严格确定性算法、禁 TF32 与 cuDNN benchmark、启用确定性 cuDNN；非 NVIDIA CUDA（含 KLXPU 这类 CUDA 兼容加速器）开启即抛错、禁用是 no-op；**不改梯度精度/GAS/TP/EP/overlap/NCCL 算法/AutoEP 算术**；自定义内核、集合通信与调度不被 PyTorch 检查完全覆盖——需在固定软硬件栈与拓扑上独立验证完整负载的可复现性；不隐含 Megatron 比特保证。

[docs/_tutorials/deterministic-mode.md:40-54](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/deterministic-mode.md#L40-L54) —— 自定义路径的三条传导（FPDT/legacy transformer/Triton grouped GEMM，含 IEEE 精度的理由），并声明固定内核选择不是对所有 shape/后端的认证、可能降性能；warn-only 模式同样选中这些确定性路径；legacy 随机执行抛错而非静默继续。文末注明无 OpGuard/Transformer Engine 运行时依赖，更广的兼容性提案见 issue #8915。

用户侧配置文档：

[docs/_pages/config-json.md:7-19](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_pages/config-json.md#L7-L19) —— 配置 JSON 文档开篇即以此参数打头：JSON 布尔、默认 false、NVIDIA CUDA 上启用即选择严格 PyTorch 确定性算法并关 TF32/cuDNN benchmark、需先设 `CUBLAS_WORKSPACE_CONFIG=:4096:8` 或 `:16:8`；明确「不改精度/NCCL 算法/并行或重叠」「配置接受不认证负载比特级可复现」「设置是进程全局且销毁时不恢复」。

逐位比较工具：

[tests/unit/runtime/test_determinism.py:71-85](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/test_determinism.py#L71-L85) —— `assert_equal` 递归比较 dict/tuple/list/标量；对张量先断言 shape 与 dtype 相同，再 `contiguous().reshape(-1).view(torch.uint8)` 后 `torch.equal`——uint8 视图比较就是比特级比较，任何一位不同都会失败，不存在容差。

跨进程训练测试：

[tests/unit/runtime/test_determinism.py:88-107](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/test_determinism.py#L88-L107) —— `test_independent_cuda_training` 参数化 ZeRO stage 0/2/3；每个 repeat 用 `subprocess.run` 拉起 `torch.distributed.run.py --standalone --nproc_per_node=1` 执行**本文件自身**（worker 在同文件 L110 起），环境注入 `CUBLAS_WORKSPACE_CONFIG=":4096:8"` 与 `OMP_NUM_THREADS="1"`（后者把 CPU 侧并行归约也钉死）；两次结果 `assert_equal` 逐位对比。

worker 的断言方式（实践任务要求对照的部分）：

[tests/unit/runtime/test_determinism.py:110-173](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/runtime/test_determinism.py#L110-L173) —— `worker` 设备选择与 `torch.manual_seed(1234)` 后构建 `Sequential(Linear(8,16), Tanh(), Linear(16,4))` + AdamW；`deepspeed.initialize` 的 config dict 含 `deterministic_mode: True` 与 `zero_allow_untested_optimizer: True`；**紧接着 initialize 就断言全部五个全局开关**（L131-L136）：确定性算法开、非 warn-only、matmul/cudnn TF32 关、cudnn benchmark 关、cudnn deterministic 开——这是「应用点确实生效」的直接证据；随后 6 个 microstep 中在梯度累积边界快照输出、梯度（stage 0 用 `parameter.grad`，stage 2/3 用 `safe_get_full_grad` 等工具取全量视图）、fp32 master 权重与 Adam 的 `exp_avg/exp_avg_sq`，连同 `global_steps/micro_steps` 一起 `torch.save`；最后 `engine.destroy()` 与 `dist.destroy_process_group()` 干净收尾。

#### 4.4.4 代码实践

**实践目标**：亲手验证「独立进程 + 相同种子 ⇒ 逐位一致」，并用权重哈希做快速判等。

操作步骤（示例代码，两个文件放同一目录）：

`det_repro.py`（训练 worker，改写自测试文件的 worker）：

```python
# 示例代码：改编自 tests/unit/runtime/test_determinism.py 的 worker
import hashlib
import json
import os
import sys

import torch


def weight_digest(model):
    sha = hashlib.sha256()
    for p in model.parameters():
        sha.update(p.detach().cpu().contiguous().reshape(-1).view(torch.uint8).numpy().tobytes())
    return sha.hexdigest()


def main():
    import deepspeed
    torch.manual_seed(1234)
    model = torch.nn.Sequential(torch.nn.Linear(8, 16), torch.nn.Tanh(), torch.nn.Linear(16, 4))
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.001)
    engine, _, _, _ = deepspeed.initialize(
        model=model, optimizer=optimizer,
        config={"train_micro_batch_size_per_gpu": 2,
                "gradient_accumulation_steps": 2,
                "deterministic_mode": True,
                "zero_allow_untested_optimizer": True,
                "zero_optimization": {"stage": int(sys.argv[1]), "overlap_comm": False}})
    losses = []
    for microstep in range(6):
        out = engine(torch.randn(2, 8, device=engine.device))
        loss = out.square().mean()
        engine.backward(loss)
        boundary = engine.is_gradient_accumulation_boundary()
        engine.step()
        if boundary:
            losses.append(loss.item())
    print(json.dumps({"losses": losses, "digest": weight_digest(engine.module)}))


if __name__ == "__main__":
    main()
```

运行两次并对比（bash，需 NVIDIA CUDA 环境）：

```bash
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export OMP_NUM_THREADS=1
python det_repro.py 0 > run1.json
python det_repro.py 0 > run2.json
diff run1.json run2.json && echo "BITWISE REPRODUCIBLE"
```

需要观察的现象：`diff` 无输出、打印 `BITWISE REPRODUCIBLE`——两次独立进程的逐步 loss 列表与最终权重 SHA-256 完全一致；对照实验：去掉 `export CUBLAS_WORKSPACE_CONFIG=:4096:8` 再跑，应在引擎初始化处抛 `deterministic_mode requires CUBLAS_WORKSPACE_CONFIG=:4096:8 or :16:8 before CUDA use`。

预期结果：CUDA 机器上逐位一致；环境变量缺失时显式报错。测试文件本身用 `pytest tests/unit/runtime/test_determinism.py -k independent` 可跑 stage 0/2/3 三组参数化用例（自动跳过非 CUDA 环境）。待本地验证（本讲义撰写环境无 GPU）。

#### 4.4.5 小练习与答案

**练习 1**：为什么测试要拉起**独立子进程**跑两遍，而不是同一个进程里循环两次？

**答案**：同进程两遍会共享隐藏的全局状态——cuBLAS workspace、Triton autotune 缓存、cuDNN 算法选择缓存等在第二遍时直接复用第一遍的结果，「看起来逐位一致」可能是缓存而非确定性。独立子进程从零初始化全部状态，是跨进程/重启可复现性的最小可信检验。生产中「断点续训能否完全复现」对应的正是独立进程场景。

**练习 2**：用户开了 `deterministic_mode`，但两次运行的 loss 第 500 步开始分叉。给出排查清单。

**答案**：按契约逐项排查：(a) 种子与数据顺序是否两次一致（调用方责任）；(b) 是否有 checkpoint 恢复且 RNG/dataloader 状态没保存（调用方责任）；(c) `deterministic_mode` 是否在**任何 CUDA 操作之前**生效（模型构建早于引擎初始化的部分不受保护）；(d) 是否使用了自定义内核/算子（PyTorch 检查不覆盖）；(e) 分布式调度（NCCL 算法、overlap 顺序）是否两次不同——文档明确不保证这部分；(f) 两次软硬件栈是否完全一致（驱动、torch、GPU 型号）。

**练习 3**：一个进程里先建了确定性引擎 A，销毁后又建了普通引擎 B。B 的执行是否「恢复非确定」？

**答案**：不恢复。文档明确 `Process-global runtime settings are not restored by engine teardown or a subsequent disabled engine.`——`use_deterministic_algorithms(True)` 等设置是进程级的，A 销毁后依然生效，B 也在确定性模式下运行（且 Triton grouped GEMM 等内核查询的是全局开关，同样走确定路径）。想要回到非确定，需要在进程里显式重置这些全局开关，或换一个进程。

## 5. 综合实践

把本讲四个模块串成一个完整的「可复现性审计」小任务（需 NVIDIA CUDA 环境；纯 CPU 环境可完成第 1、4 步）：

1. **复现边界**（对应 4.2）：写一个脚本先不设 `CUBLAS_WORKSPACE_CONFIG`、以 `deterministic_mode=true` 初始化引擎，记录报错；再设 `:4096:8`、以 `deterministic_mode` 缺省初始化，记录成功且全局开关未变——确认「校验前置条件」与「关闭即 no-op」两个行为。
2. **复现应用**（对应 4.2）：初始化成功后立即断言测试 worker 的五连断言（确定性开、非 warn-only、两个 TF32 关、benchmark 关、cudnn deterministic 开）。
3. **复现传导**（对应 4.3）：对你的模型（含 MoE 专家层更佳）分别在开/关确定性下各跑两次前向 + 反向，用 `torch profiler` 或简单计时对比 grouped GEMM 的耗时差异，体会「固定 configs[0] 可能降性能」的代价；若有 legacy `DeepSpeedTransformerLayer`，构造 `stochastic_mode=True` 并开启确定性，验证构造/前向抛 `RuntimeError`。
4. **复现可复现性**（对应 4.4）：按 4.4.4 的脚本以独立进程跑两遍完整训练（ZeRO stage 0 与 2 各一组），比较逐步 loss 与权重 SHA-256；然后**故意破坏一个条件**（如第二次运行前改一个比特的数据顺序、或用不同 micro_batch_size），确认对比能发现差异——验证你的判等方法本身是有效的。
5. **对照断言方式**：通读 `tests/unit/runtime/test_determinism.py`，把你的脚本与其对齐：独立进程、uint8 逐位比较、边界快照（梯度/master/exp_avg）、`OMP_NUM_THREADS=1`。产出一份 1 页的《本环境确定性边界报告》：哪些条件验证通过、哪些（如多卡、NCCL、自定义内核）属于文档声明不保证、需要额外验证的范围。

## 6. 本讲小结

- `deterministic_mode` 是 opt-in 的**执行策略**开关，与数值精度策略正交；默认 false 且关闭即完全 no-op；当前仅支持 NVIDIA CUDA，KLXPU（设备名 cuda、身份 klxpu）与 ROCm（hip 非空）都会被 `_is_nvidia_cuda` 的三重条件拒绝。
- 链路是严格的「解析与应用分离」：`DeepSpeedConfig.__init__` 里 `configure_determinism` 只做严格布尔类型检查并挂属性（挂在 batch size 推导后、sanity check 前）；`DeepSpeedEngine.__init__` 最早期（`self._config` 赋值后紧邻）才由 `apply_determinism` 应用五个进程全局开关。
- `CUBLAS_WORKSPACE_CONFIG` 必须在 CUDA 初始化前由用户设为 `:4096:8` 或 `:16:8`；DeepSpeed 只校验不代设——引擎构造时 cuBLAS workspace 可能已按旧配置分配，代设只会制造假阳性。
- PyTorch 检查覆盖不到的地方靠「星型传导」：各内核查询 `torch.are_deterministic_algorithms_enabled()` 自行响应——Triton grouped GEMM 固定 `configs[0]` 绕过计时型 autotune 并用 `input_precision="ieee"` 替代 TF32 `tl.dot`（PyTorch 的 TF32 开关管不到 Triton）；legacy transformer 拒绝 `stochastic_mode` 组合；FPDT 把标志传给 FlashAttention backward。
- 边界清晰：不改精度/NCCL/并行/overlap；不保证自定义内核与分布式调度的比特级可复现；不追认引擎初始化之前的操作；进程全局设置销毁不恢复；种子、数据顺序、checkpoint RNG 状态是调用方的责任。
- 契约测试的三板斧：mock 加速器验证后端拒绝且不留全局副作用；参数化非法 workspace 值；独立子进程 + uint8 逐位比较验证跨进程可复现（stage 0/2/3 全覆盖）。

## 7. 下一步学习建议

- **u9-l2（CUDA 算子实战）**：本讲看到 `stochastic_mode` 与确定性互斥只是「拒绝」，下一讲会深入 `DeepSpeedTransformerLayer` 的 CUDA 内核本体，理解随机化到底随机在哪、以及 `_validate_stochastic_mode` 在构造/前向/反向三处把关的完整细节。
- **u9-l5（低比特量化内核）**：本讲的 TF32/IEEE 之辨是「精度 vs 可复现」的一个切片；FP8/FP4 量化内核是同一矛盾更极端的形态，两讲对照阅读会加深对数值契约的理解。
- **源码延伸阅读**：issue [#8915](https://github.com/deepspeedai/DeepSpeed/issues/8915)（教程文末引用的更广兼容性提案）可以了解这个 opt-in 设计未来扩展到更多后端的路线；`deepspeed/sequence/fpdt_layer.py` 中三档 flash-attn 版本的调用分支是「适配第三方库确定性接口」的实用范本。
