# 加速器抽象层：一套代码支持多种硬件

## 1. 本讲目标

DeepSpeed 不只跑在 NVIDIA GPU 上。读完本讲，你应该能够：

1. 列举 `AbstractAccelerator`（`DeepSpeedAccelerator`）定义的 20+ 组设备无关接口，并按「设备 / RNG / 流与事件 / 显存 / 数据类型 / 图操作 / 张量 / pinned memory / 算子构建」分组归类。
2. 解释 `real_accelerator.py` 的探测加载机制：`DS_ACCELERATOR` 环境变量覆盖优先，其次按「装了哪个 torch 扩展」自动探测，探测顺序为什么必须把「伪装成 torch.cuda 的后端」放在 CUDA 之前。
3. 以 CUDA 与 CPU 两个参照实现为例，对比同一接口在不同硬件上的语义差异（同步设备、可用内存、通信后端、图捕获）。
4. 解释图捕获能力的**显式 opt-in** 设计：`is_graph_capture_supported()` 默认 `False`，后端验证通过后才覆写为 `True`；KLXPU 如何从「显式拒绝」演进到「继承 CUDA 的 opt-in」（#9042）。
5. 以 XPU 后端为例，看一个 accelerator 如何绕过 torch、用 `ctypes` 直接驱动 Level Zero 的 `ZE_extension_external_memmap_sysmem` 扩展注册页锁定主机内存——「torch 通道不可用时后端自力更生」的实现模式。

## 2. 前置知识

- **硬件后端（backend）**：DeepSpeed 把「一块可以执行张量计算的设备」抽象为 accelerator，例如 `cuda`（NVIDIA GPU）、`cpu`、`hpu`（Habana Gaudi）、`xpu`（Intel GPU）、`npu`（华为昇腾）、`klxpu`（昆仑芯 XPU）等。PyTorch 本身为每种硬件提供一组 `torch.cuda.*` / `torch.xpu.*` 风格的 API，但名字各不相同。
- **ABC 抽象基类**：Python 的 `abc.ABC` + `@abc.abstractmethod` 定义「子类必须实现」的接口。抽象方法只写 `...`（函数体为空），实现类必须逐一落实。
- **全局单例**：`real_accelerator.py` 用模块级全局变量 `ds_accelerator` 缓存当前选中的后端实例，首次 `get_accelerator()` 时探测并构造，之后直接复用。
- **pinned memory（页锁定主机内存）**：普通主机内存可能被操作系统换页（page out），设备异步 DMA 拷贝期间内存被搬走会得到脏数据。pinned memory 被锁在物理页上，且能被设备直接映射进设备页表，是高速 H2D/D2H 拷贝和 ZeRO-Offload 的基础。CUDA 里对应 `cudaHostRegister`/`cudaHostUnregister`。
- **CUDA Graph（图捕获）**：把一段 GPU 操作录制（capture）成一张图，之后一次 replay 就重放全部操作，省去逐 kernel 的 launch 开销。图捕获期间很多 host 端调用（如查询、同步）是非法的，所以「是否正处于捕获中」本身也需要一个查询接口。DeepSpeed 的 Hybrid Engine 用它加速推理 rollout（详见 u7-l4）。
- **ctypes / FFI**：Python 标准库 `ctypes` 可以直接 `CDLL` 加载动态库（如 `libze_loader.so.1`）、按 C 结构体布局定义 `Structure`、声明函数签名后直接调用 C 函数——不需要写任何 C 扩展。
- **符号链接双轨布局**（u1-l2 已建立）：git 实际跟踪的是仓库顶层的 `accelerator/` 目录；`deepspeed/accelerator` 是一个指向 `../accelerator/` 的符号链接。两个导入路径（`accelerator.xxx` 与 `deepspeed.accelerator.xxx`）解析到同一份源码文件，但在 Python 里是两个独立模块对象。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [accelerator/abstract_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py) | 抽象基类 `DeepSpeedAccelerator`，定义全部设备无关接口与若干带默认实现的「opt-in 能力位」 |
| [accelerator/real_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/real_accelerator.py) | 探测器：`get_accelerator()` / `set_accelerator()` / `is_current_accelerator_supported()`，维护全局单例 |
| [accelerator/cuda_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py) | CUDA 参照实现（NVIDIA GPU），也是多数后端继承的「最完整实现」 |
| [accelerator/cpu_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cpu_accelerator.py) | CPU 参照实现，展示「没有 GPU 语义时接口如何退化」 |
| [accelerator/klxpu_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/klxpu_accelerator.py) | 昆仑芯 XPU 后端：继承 CUDA、只覆写差异点，是「继承 + 显式能力声明」的样本 |
| [accelerator/xpu_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/xpu_accelerator.py) | Intel GPU 后端：`ctypes` 直驱 Level Zero 注册 pinned memory 的「自力更生」样本 |
| [deepspeed/accelerator/__init__.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/accelerator/__init__.py) | 门面：只导出 `DeepSpeedAccelerator`、`get_accelerator`、`set_accelerator`、`is_current_accelerator_supported` 四个名字 |
| [tests/unit/accelerator/test_accelerator_graph.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/accelerator/test_accelerator_graph.py) | 图操作接口的契约测试：支持图的后端做「捕获-回放」往返，不支持的后端必须「eager 执行 + replay 空操作」 |
| [docs/_tutorials/accelerator-abstraction-interface.md](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/accelerator-abstraction-interface.md) | 官方移植指南：`torch.cuda.xxx` → `get_accelerator().xxx` 的改写规则与图捕获 opt-in 契约 |

> 注意：`deepspeed/accelerator/` 在仓库里是符号链接（`ls -la deepspeed/` 可见 `accelerator -> ../accelerator/`），git 只跟踪顶层 `accelerator/`。因此本文永久链接统一使用 `accelerator/...` 这一真实路径；你在代码里看到 `from deepspeed.accelerator.cuda_accelerator import ...` 时，读到的就是同一个文件。

## 4. 核心概念与源码讲解

### 4.1 模块一：AbstractAccelerator——设备无关接口的总纲

#### 4.1.1 概念说明

DeepSpeed 的 runtime 代码（engine、ZeRO、通信……）要同时支持十种硬件，但它不想在任何业务文件里写 `if backend == "cuda": torch.cuda.xxx() elif ...`。解决办法是一层**端口-适配器（port-adapter）抽象**：

- `DeepSpeedAccelerator` 是「端口」，用抽象方法声明所有运行时需要的设备能力；
- 每种硬件的后端类（`CUDA_Accelerator`、`CPU_Accelerator`……）是「适配器」，把这些方法映射到各自的 torch 原生 API；
- 业务代码统一通过 `get_accelerator().xxx()` 调用，从此不知道脚下是哪种硬件。

接口分两类：

1. **`@abc.abstractmethod` 抽象方法**：每个后端必须实现，否则实例化时报错；
2. **带默认实现的「能力位 / 钩子」**：基类给出保守默认值（多数为 `False` 或空操作），后端**验证通过后显式覆写**才开启——`is_fp64_supported`、`prefer_triton_grouped_mm`、`is_graph_capture_supported`、`register_host_memory` 都属于这一类。

#### 4.1.2 核心流程

后端类实现接口的判定流程可以概括为：

```text
新后端 XYZ 接入
  ├─ 必须实现：全部 @abstractmethod（设备/RNG/流/内存/类型/图/张量/算子构建）
  ├─ 能力位逐项决定：
  │    ├─ 默认值（False / 空操作）→ 保守路径
  │    └─ 覆写为 True → 上层功能随之开启
  └─ get_accelerator() 校验 isinstance(XYZ_Accelerator, DeepSpeedAccelerator)
       通过 → 单例缓存，全局使用
```

#### 4.1.3 源码精读

类的骨架与四个「同步设备」行为接口：

[accelerator/abstract_accelerator.py:L10-L32](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py#L10-L32) 定义抽象基类。开头四个抽象方法是一组「设备执行模型」声明：`is_synchronized_device`（设备是否与 host 天然同步执行）、`use_host_timers`（计时用 host 时钟还是设备事件）、`resolves_data_dependency`、`handles_memory_backpressure`。CUDA 这类异步设备对四者基本答「否」，CPU 这类同步设备答「是」——它们决定了上层是否需要额外的 stream 同步与流控逻辑。

接口按注释分组，共约 20 组、60+ 个方法：

| 分组（源码注释） | 行区间 | 代表方法 |
| --- | --- | --- |
| Device APIs | L34-L61 | `device_name`、`current_device`、`device_count`、`synchronize` |
| RNG APIs | L63-L91 | `manual_seed`、`get_rng_state`、`default_generator` |
| Streams/Events | L92-L113 | `Stream`、`Event`、`current_stream` |
| Memory management | L115-L166 | `memory_allocated`、`total_memory`、`available_memory` |
| Data types | L168-L183 | `is_bf16_supported`、`is_fp16_supported`、`supported_dtypes` |
| Misc | L185-L208 | `is_available`、`communication_backend_name`、`is_triton_supported` |
| 能力位（有默认实现） | L177-L179、L210-L216 | `is_fp64_supported`、`prefer_triton_grouped_mm` |
| Graph operations | L218-L241 | `is_graph_capture_supported`、`create_graph`、`replay_graph` |
| Tensor operations | L243-L277 | `HalfTensor`、`LongTensor` 等 |
| Memory pinning | L279-L383 | `pin_memory`、`pin_empty`、`register_host_memory` |
| Op builder / 编译后端 | L385-L425 | `create_op_builder`、`build_extension`、`get_compile_backend` |

两个「能力位」的默认实现，注意它们都是**显式 opt-in**（默认关闭）：

- [accelerator/abstract_accelerator.py:L210-L216](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py#L210-L216)：`prefer_triton_grouped_mm()` 默认 `False`，注释说明只有当 Triton grouped-GEMM 确实快于 `torch._grouped_mm` 的后端才覆写为 `True`。
- [accelerator/abstract_accelerator.py:L225-L229](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py#L225-L229)：`is_graph_capture_supported()` 默认 `False`、`is_current_stream_capturing()` 默认 `False`——这段注释是本讲 4.4 节的核心，先记住结论：**只暴露图 API 不等于支持图捕获，未验证的后端留在 eager 路径**。

pinned memory 的公共分发层（XPU 会覆写其中的 `register_host_memory`）：

- [accelerator/abstract_accelerator.py:L293-L303](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py#L293-L303)：`register_host_memory(address, num_bytes)` 默认返回 `False`（「本后端不支持向设备运行时注册已有主机内存」），`unregister_host_memory` 默认 `None`，`pin_memory_alignment` 默认 1（无对齐要求）。
- [accelerator/abstract_accelerator.py:L337-L352](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py#L337-L352)：`pin_memory()` 先查是否启用了原生 pinned 后端（经 `DS_PIN_MEMORY_BACKEND` 选择，见 [deepspeed/utils/pin_memory.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/pin_memory.py)），有则走原生通道并记账，没有则回退 torch 的 `tensor.pin_memory()`。原生通道内部最终就会调用后端的 `register_host_memory`——这就是 4.5 节 XPU 实现的接入点。

最后看门面导出：

- [deepspeed/accelerator/__init__.py:L6-L7](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/accelerator/__init__.py#L6-L7)：整个包对外只有四个名字，用户侧最常见的写法是 `from deepspeed.accelerator import get_accelerator`。

#### 4.1.4 代码实践

**实践目标**：亲手盘点接口面，验证「抽象方法必须实现、能力位可以只继承」。

**操作步骤**：

1. 打开 [accelerator/abstract_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py)，按上面的分组表数一遍抽象方法。
2. 在任意装了 deepspeed 的环境（CPU 机器即可）运行下面脚本（**示例代码**，非项目原有文件）：

```python
# inventory.py —— 盘点当前后端实现了哪些接口
from deepspeed.accelerator import get_accelerator
from deepspeed.accelerator.abstract_accelerator import DeepSpeedAccelerator

accel = get_accelerator()
print("backend:", accel._name)

abstract = [n for n in dir(DeepSpeedAccelerator)
            if getattr(getattr(DeepSpeedAccelerator, n, None), "__isabstractmethod__", False)]
print("抽象方法总数:", len(abstract))

# 能力位：直接看基类默认值 vs 当前后端的实际返回
for probe in ("is_fp64_supported", "prefer_triton_grouped_mm",
              "is_graph_capture_supported", "is_current_stream_capturing"):
    base = getattr(DeepSpeedAccelerator, probe)()
    curr = getattr(accel, probe)()
    overridden = "继承默认" if base == curr and probe != "is_fp64_supported" else "覆写/继承后不同"
    print(f"{probe}: 基类默认={base}, 当前后端={curr} ({overridden})")
```

3. 把脚本在 `DS_ACCELERATOR=cpu` 与（如果有 GPU）默认 CUDA 环境下各跑一次。

**需要观察的现象**：

- 抽象方法总数应为 50 上下（随版本略有浮动）；`__init__` 不会被计入。
- CPU 后端的 `is_graph_capture_supported()` 返回 `False`——CPU 类**没有覆写**它，这是继承基类默认值。

**预期结果**：你会得到一张「能力位 × 后端」的对照雏形，其中 `is_graph_capture_supported` 在 CPU 上为 `False`、在 CUDA 上为 `True`（后者待本地验证，需 GPU 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_fp64_supported` 不是抽象方法，而是带默认实现 `return True`？
**答案**：见 [accelerator/abstract_accelerator.py:L177-L179](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py#L177-L179) 的注释——几乎所有加速器都支持 fp64，只有少数不支持的后端才需要覆写；把它做成抽象方法会强迫所有后端写一模一样的样板代码。这也是抽象层的一个通用取舍：**越是「几乎人人都有」的能力越适合给默认实现，越是「人人不同」的能力越适合抽象方法**。

**练习 2**：`get_accelerator()` 返回的对象为什么不每次新建？
**答案**：探测器把实例缓存在模块级全局变量 `ds_accelerator`（见 4.2.3），首次构造后复用。后端对象自身带有状态（如 CUDA 的 pynvml 句柄、CPU 的 max_mem 统计），反复新建既浪费又会重复初始化原生库。

**练习 3**：`register_host_memory` 的默认返回值是 `False` 而不是抛 `NotImplementedError`，这传递了什么设计意图？
**答案**：`False` 表示「该后端不支持把已有主机内存注册进设备页表，但流程可以继续」——上层（原生 pinned memory 通道）据此回退到 mlock-only 的保守实现。抛异常则会让「后端不支持」变成「功能崩溃」。能力位钩子的约定是**报能力、不报错误**。

### 4.2 模块二：real_accelerator——运行时探测器

#### 4.2.1 概念说明

`get_accelerator()` 要回答一个问题：**当前进程该用哪个后端类？** 它按两级策略决定：

1. **显式覆盖**：环境变量 `DS_ACCELERATOR=klxpu` 直接指定，并**预检对应的 torch 扩展是否安装**（装不上就立刻报 `ValueError`，而不是等到运行中神秘崩溃）；
2. **自动探测**：依次尝试导入各硬件的 torch 扩展（`torch_npu`、`torch_sdaa`、`habana_frameworks`、`torch_mlu`、`torch_supa`、`torch_xmlir`……），谁导入成功就用谁，最后兜底 `cpu`。

探测完成后构造单例并做 `isinstance` 校验。

#### 4.2.2 核心流程

```text
get_accelerator()
  ├─ 全局 ds_accelerator 已有？ → 直接返回（缓存）
  ├─ ① DS_ACCELERATOR 环境变量？
  │     ├─ 在 SUPPORTED_ACCELERATOR_LIST 中 → 预检扩展可用性 → accelerator_name 确定
  │     └─ 非法值 → ValueError
  ├─ ② 自动探测（按固定顺序逐个 try-import）
  │     xpu(torch.xpu) → npu → sdaa → mps → hpu → mlu
  │     → supa → klxpu → cuda → 全部失败 → cpu（打 warning）
  ├─ ③ 按 accelerator_name import 对应后端类并实例化
  ├─ ④ _validate_accelerator：isinstance 双基类校验
  └─ 日志 "Setting ds_accelerator to <name> (<override|auto detect>)"
```

探测顺序是**有序的**，两条注释专门解释了两处顺序敏感：`torch_supa` 和 `torch_xmlir`（KLXPU）都会**把自己伪装成 torch.cuda**（即 `torch.cuda.is_available()` 会为真），所以它们的检查必须排在真正的 CUDA 检查之前，否则会被误判成 `cuda` 后端。

#### 4.2.3 源码精读

支持的后端清单与全局缓存变量：

- [accelerator/real_accelerator.py:L23-L25](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/real_accelerator.py#L23-L25)：`SUPPORTED_ACCELERATOR_LIST = ['cuda', 'cpu', 'xpu', 'npu', 'mps', 'hpu', 'mlu', 'sdaa', 'supa', 'klxpu']` 共 10 种；`ds_accelerator = None` 就是单例缓存。

「双基类」校验——符号链接布局的直接后果：

- [accelerator/real_accelerator.py:L28-L44](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/real_accelerator.py#L28-L44)：`_validate_accelerator` 同时导入两条路径的基类（`accelerator.abstract_accelerator` 与 `deepspeed.accelerator.abstract_accelerator`），实例只要是其**之一**的实例即算合格。原因写在注释里：安装期用 `accelerator.*` 路径、运行期扩展用 `deepspeed.accelerator.*` 路径，符号链接让两个路径指向同一文件，但 Python 会为它们生成两个不同的类对象——单一路径的 `isinstance` 会误拒合法后端。

环境变量覆盖与扩展预检（以 klxpu 分支为例）：

- [accelerator/real_accelerator.py:L58-L114](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/real_accelerator.py#L58-L114)：`DS_ACCELERATOR` 存在时逐后端预检，例如 `klxpu` 分支（L106-L110）尝试 `import torch_xmlir`，失败即抛 `ValueError("KLXPU_Accelerator requires torch_xmlir, ...")`；不在清单里的值（L111-L113）直接拒绝。注意 `cpu` 分支（L68-L69）什么都不检——CPU 永远可用。

自动探测链与两处「必须在 CUDA 之前」的顺序：

- [accelerator/real_accelerator.py:L175-L194](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/real_accelerator.py#L175-L194)：`supa` 与 `klxpu` 的探测注释都写明「spooofs/presents itself through torch.cuda, so this check must come before the CUDA detection below」。`torch_xmlir` 导入成功即选 `klxpu`。
- [accelerator/real_accelerator.py:L195-L216](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/real_accelerator.py#L195-L216)：真正的 CUDA 判定要求 `torch.cuda.device_count() > 0 and torch.cuda.is_available()` 两个条件（注释引用 PR#6810：`device_count` 先行可以避免无设备时的 CUDA 调用，`is_available` 保证没有初始化错误）；全部失败则 warning「Setting accelerator to CPU...」并兜底 `cpu`。代码里散落的 `#ignore-cuda` 注释是仓库标注「此处故意使用 torch.cuda」的记号——运行时代码按文档约定应改写为 `get_accelerator().xxx()`，探测器与后端实现内部属于例外。

按名字实例化并记录日志：

- [accelerator/real_accelerator.py:L220-L264](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/real_accelerator.py#L220-L264)：一长串 `elif` 把名字映射到 `from .xxx_accelerator import XXX_Accelerator` 的**延迟导入**（只有选中的后端才被 import，避免装不上无关扩展），最后 `_validate_accelerator` 并打日志 `Setting ds_accelerator to {name} ({ds_set_method})`。
- [accelerator/real_accelerator.py:L267-L272](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/real_accelerator.py#L267-L272)：`set_accelerator(accel_obj)` 允许外部（例如测试、或扩展包）替换单例，同样先做双基类校验。

文件尾部（L275-L317）还保留了一段用法示例字符串，演示 `get_accelerator()` 与 `set_accelerator()` 的典型输出，可直接阅读。

#### 4.2.4 代码实践

**实践目标**：观察覆盖与自动探测两条路径的行为差异。

**操作步骤**（CPU 机器即可，**示例代码**）：

```bash
# 1. 自动探测：无 GPU 机器上应回退 cpu，并打出 warning
python -c "
import logging; logging.basicConfig(level=logging.INFO)
from deepspeed.accelerator import get_accelerator
a = get_accelerator()
print('name =', a._name)
print('backend =', a.communication_backend_name())
"

# 2. 显式覆盖
DS_ACCELERATOR=cpu python -c "
from deepspeed.accelerator import get_accelerator
print(get_accelerator()._name)
"

# 3. 非法值
DS_ACCELERATOR=ttnn python -c "from deepspeed.accelerator import get_accelerator; get_accelerator()"
```

**需要观察的现象**：第 1 条命令的日志出现 `Setting ds_accelerator to cpu (auto detect)` 与 warning「Setting accelerator to CPU. If you have GPU or other accelerator, we were unable to detect it.」；第 2 条日志为 `(override)`；第 3 条抛出 `ValueError: DS_ACCELERATOR must be one of [...]`。

**预期结果**：确认「override 优先于 auto detect、非法值 fail-fast、探测结果全局缓存」。第 1、2、3 条在纯 CPU 环境即可全部复现；GPU 环境下第 1 条应选出 `cuda`（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DS_ACCELERATOR=klxpu` 时要在探测器里 `import torch_xmlir`，而不是等后端类用到时再失败？
**答案**：显式指定后端的用户预期「配错了立刻知道」。预检把「扩展未安装」从运行中段的随机位置提前到 `get_accelerator()` 首次调用处，错误信息也带修复建议（装 torch_xmlir）。这是 fail-fast 原则在配置边界的应用。

**练习 2**：把 `klxpu` 的探测分支移到 `cuda` 分支之后会发生什么？
**答案**：由于 `torch_xmlir` 会以 torch.cuda 的面目呈现（`torch.cuda.device_count() > 0 and torch.cuda.is_available()` 为真），CUDA 分支会先命中，`accelerator_name` 被定为 `cuda`，KLXPU 后端永远不会被自动选中——这正是源码注释「must come before the CUDA detection below」守护的场景。顺序本身就是协议的一部分。

**练习 3**：为什么第 ③ 步实例化用一串延迟 `import` 而不是文件顶部统一 import 全部 10 个后端类？
**答案**：顶部统一 import 意味着「使用 CUDA 的机器也必须装 torch_npu、habana_frameworks 等全部扩展」——任何一个缺失都会让 `import deepspeed` 直接失败。延迟导入保证只有被选中的后端（及其依赖）才被加载，也顺带满足了「import deepspeed 不得做重活」的项目纪律（见 AGENTS.md：import 不得初始化 CUDA、重导入要避免）。

### 4.3 模块三：两个参照实现——CUDA 与 CPU

#### 4.3.1 概念说明

`CUDA_Accelerator` 是「最完整」的实现：直连 `torch.cuda`，能展示接口的全部语义。`CPU_Accelerator` 则演示**接口的退化艺术**：没有设备、没有流的硬件上，每个接口仍要给出语义正确的答案（哪怕答案是 `None` 或空操作）。两个实现对照着读，最能看清抽象边界画在哪里。

KLXPU 等后端更进一步：**直接继承 `CUDA_Accelerator`**，只覆写差异点（见 4.4 节）——因为 `torch_xmlir` 把整套 torch.cuda API 都接管了。

#### 4.3.2 核心流程

读这对实现的固定套路：

```text
对每个接口问三个问题
  1. 委托给谁？（torch.cuda.xxx / torch.xpu.xxx / psutil / 返回常量）
  2. 语义差异是什么？（同步 vs 异步、真实设备 vs 主机进程）
  3. 错误/缺失时怎么办？（pynvml 不在 → 回退 total - allocated）
```

#### 4.3.3 源码精读

先看构造函数里两个重要字段：

- [accelerator/cuda_accelerator.py:L29-L37](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py#L29-L37)：CUDA 后端 `_name='cuda'`，通信后端为 `'nccl'`（Windows 下降级 `'gloo'`），编译后端 `"inductor"`；pynvml 延迟初始化（CUDA 不可用时 import 也不炸）。
- [accelerator/cpu_accelerator.py:L165-L179](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cpu_accelerator.py#L165-L179)：CPU 后端的通信后端是 `'ccl'`（装了 oneccl 绑定时）或 `'gloo'`，并用 `psutil` 记录进程 RSS 作为内存统计基础。

**对比一：执行模型四连问。**

- [accelerator/cuda_accelerator.py:L51-L61](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py#L51-L61)：CUDA 答 `is_synchronized_device() == False`，其余三个方法全部转发给它——GPU 异步执行，所以计时要用设备事件、数据依赖要显式 stream 同步、显存反压要自己处理。
- [accelerator/cpu_accelerator.py:L182-L192](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cpu_accelerator.py#L182-L192)：CPU 全部答 `True`。子类只需覆写 `is_synchronized_device` 一个方法，其余继承——**把四个问题归约为一个事实**是这对实现里最值得学习的技巧。

**对比二：`synchronize`。**

- [accelerator/cuda_accelerator.py:L87-L92](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py#L87-L92)：CUDA 版注释解释了一个真实兼容性坑——Torch 2.14 的 Dynamo 拒绝 `torch.cuda.synchronize(None)`，而 `None` 语义上是「当前设备」，所以要显式省略参数。
- [accelerator/cpu_accelerator.py:L230-L231](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cpu_accelerator.py#L230-L231)：CPU 版就是 `return`——同步设备无需同步。

**对比三：`available_memory`（本讲实践任务指定的对比方法之一）。**

- [accelerator/cuda_accelerator.py:L178-L204](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py#L178-L204)：`total_memory` 读设备属性；`available_memory` 优先用 pynvml 拿驱动视角的**整机空闲显存**（还要经 `_get_nvml_gpu_id` 做 `CUDA_VISIBLE_DEVICES` 的 id 重映射，因为 pynvml 不认这个环境变量），pynvml 不可用时回退到近似：

  \[ \text{available} \approx \text{total\_memory} - \text{memory\_allocated} \]

  （近似在于没算缓存与碎片，只用于降级路径。）
- [accelerator/cpu_accelerator.py:L333-L346](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cpu_accelerator.py#L333-L346)：CPU 版用 `psutil.virtual_memory()`，并与 **cgroup 限额**取 `min`——容器里「机器有 512G 但容器限额 64G」时，必须报 64G 口径的可用量，否则 ZeRO 的内存规划会严重超卖。

**对比四：`communication_backend_name`（实践任务指定的对比方法之二）。**

- [accelerator/cuda_accelerator.py:L263-L264](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py#L263-L264) 与 [accelerator/cpu_accelerator.py:L365-L366](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cpu_accelerator.py#L365-L366)：两者都返回构造期写好的 `self._communication_backend_name`，但取值不同（CUDA → `nccl`；CPU → `ccl`/`gloo`）。上层 `init_process_group(get_accelerator().communication_backend_name())` 由此做到后端无关。

**对比五：图操作（实践任务指定的对比方法之三，详见 4.4）。**

- [accelerator/cuda_accelerator.py:L290-L305](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py#L290-L305)：CUDA 是真图——`CUDAGraph`、`torch.cuda.graph` 上下文、`graph.replay()`。
- [accelerator/cpu_accelerator.py:L388-L397](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cpu_accelerator.py#L388-L397)：CPU 的 `create_graph()` 返回 `None`、`capture_to_graph()` 返回空上下文、`replay_graph()` 直接返回——**暴露图 API 的形状，但语义是「假装捕获、立即执行、回放空操作」**。

#### 4.3.4 代码实践

**实践目标**：完成「5 个关键方法在 CUDA 与 CPU 上的差异对照表」（本讲指定实践任务的第一部分）。

**操作步骤**：

1. 通读 [accelerator/abstract_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py) 的方法签名，从中挑 5 个：建议 `is_synchronized_device`、`available_memory`、`communication_backend_name`、`is_graph_capture_supported`、`synchronize`。
2. 逐个打开 cuda_accelerator.py 与 cpu_accelerator.py 的对应实现，填出下表（参考答案已给出，请自行核对源码行号）：

| 方法 | CUDA 实现 | CPU 实现 | 语义差异要点 |
| --- | --- | --- | --- |
| `is_synchronized_device` | `False`（L51-L52） | `True`（L182-L183） | 异步 vs 同步执行模型，决定上层流控 |
| `synchronize` | `torch.cuda.synchronize(...)`（L87-L92） | 空操作（L230-L231） | 需要屏障 vs 天然串行 |
| `available_memory` | pynvml 空闲显存，回退 total−allocated（L196-L204） | psutil 可用内存 ∧ cgroup 限额（L339-L346） | 驱动口径 vs 进程/容器口径 |
| `communication_backend_name` | `nccl`（构造期，L32-L35） | `ccl`/`gloo`（构造期，L170-L174） | 集合通信库随硬件走 |
| `is_graph_capture_supported` | `True`（L291-L292） | 未覆写，继承基类 `False`（L225-L226） | 真 CUDA Graph vs eager 空操作（4.4 详解） |

3. 在 CPU 机器上实际验证其中两行（**示例代码**）：

```python
from deepspeed.accelerator import get_accelerator
a = get_accelerator()
print(a.is_synchronized_device())        # True
print(a.communication_backend_name())    # gloo 或 ccl
print(a.is_graph_capture_supported())    # False
print(a.available_memory() > 0)          # True（psutil 口径）
```

**需要观察的现象**：表格中 CPU 列的 5 项输出与上一步脚本一致；CUDA 列需要在 GPU 机器复跑（待本地验证）。

**预期结果**：你会得出本模块的核心结论——**同一份接口契约下，实现可以委托原生库、可以查操作系统、可以返回常量甚至空操作；抽象层只约束「问什么」，不约束「怎么答」**。

#### 4.3.5 小练习与答案

**练习 1**：`CPU_Accelerator.device()` 返回 `None` 而不是 `torch.device('cpu')`，为什么上层没有崩？
**答案**：见 [accelerator/cpu_accelerator.py:L198-L199](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cpu_accelerator.py#L198-L199)。接口约定「返回当前设备对象」，而 CPU 场景下张量创建惯用 `device=get_accelerator().current_device_name()`（即字符串 `'cpu'`）或默认设备；`None` 传给 torch 的多数 API 恰好表示「默认设备」。约定俗成的配合点，移植新后端时要照抄语义而不是照抄返回值。

**练习 2**：CUDA 的 `available_memory` 为什么不直接用 `torch.cuda.mem_get_info()` 之类的 torch API，而引入 pynvml？
**答案**：pynvml 给的是**驱动视角的整机空闲显存**（包含其他进程占用的排除），torch 的 allocated 统计只算本进程张量缓存池。对「还差多少显存才能放得下参数分区」这类问题，驱动口径更接近真相；pynvml 缺失时才退回 `total - allocated` 的近似（L203-L204）。代价是必须处理 pynvml 与 torch 的设备编号不一致（`_get_nvml_gpu_id` 的 `CUDA_VISIBLE_DEVICES` 重映射，L181-L194）。

**练习 3**：`use_host_timers` 为什么等于 `is_synchronized_device`？
**答案**：异步设备上 host 时钟与 kernel 执行不对齐——host 记时的起止点之间 GPU 可能还在排队，测出来的「耗时」包含排队噪声。同步设备（CPU）上计算与时钟同源，host 计时既准又便宜。所以这组派生关系（L54-L55、L185-L186）把「用哪种计时」归约到「设备是否同步」这一个事实上。KLXPU 后端覆写 `use_host_timers` 的理由也在此：它的 CUDA 事件计时器默认返回 0.0，不得不用 host 计时（见 4.4.3）。

### 4.4 模块四：图捕获能力的显式 opt-in 声明（含 KLXPU 演进，#9042）

#### 4.4.1 概念说明

图捕获（CUDA Graph capture）是 Hybrid Engine 推理加速的关键路径（u7-l4 详述），但它在图录制期间有严格限制：很多 host 端查询（同步、某些张量检查）在捕获中是**非法**的，调用会直接报错。因此 DeepSpeed 把「支持图捕获」设计成一个**显式 opt-in 的能力位**，而不是「实现了 create_graph 就等于支持」：

- 基类默认 `is_graph_capture_supported() -> False`：只暴露图 API 形状的后端（如 CPU）留在 eager 路径；
- 后端必须**同时**实现 `is_current_stream_capturing()`（供静态 KV cache 等组件在捕获期间跳过非法的 host 端检查），并经验证后才覆写为 `True`；
- 该能力**可被继承**，所以「继承自 CUDA 但尚未验证」的子类必须显式覆写回 `False`（default-deny）。

KLXPU 正是这条规则的标准案例：它继承 `CUDA_Accelerator`（因为 `torch_xmlir` 接管了 torch.cuda API），图捕获未经验证期间在子类里显式 `return False`；#9042 完成验证后**删除了这个覆写**，于是随 CUDA 一起进入图捕获路径。整个演进过程中，「是否支持」始终是后端自己显式声明的决定，而不是隐式继承的副作用。

#### 4.4.2 核心流程

```text
上层想用图捕获（HybridEngine 图缓存 / rollout 生成路径）
  └─ 查 get_accelerator().is_graph_capture_supported()
       ├─ True  → 走图捕获路径
       │          （捕获期间组件用 is_current_stream_capturing()
       │            跳过 host 端检查）
       └─ False → warning_once + 回退 eager 生成（不报错）

后端侧的生命周期：
  暴露图 API（create_graph/capture_to_graph/replay_graph）   ← 必须实现（抽象）
  实现 is_current_stream_capturing                            ← opt-in 前提
  验证通过 → 覆写 is_graph_capture_supported = True           ← 显式 opt-in
  继承自已 opt-in 的基类但未验证 → 子类显式覆写回 False        ← default-deny
  验证完成 → 删除子类覆写，恢复继承                            ← KLXPU 的 #9042
```

#### 4.4.3 源码精读

**基类契约**：

- [accelerator/abstract_accelerator.py:L218-L241](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/abstract_accelerator.py#L218-L241)：注释逐条写明契约——后端**显式** opt-in；且只有实现 `is_current_stream_capturing` 之后才允许 opt-in（静态 KV cache 靠它跳过图捕获期间非法的 host 端检查）；默认 `False` 让「只暴露图 API」的后端留在 eager 路径直到验证完成。`create_graph/capture_to_graph/replay_graph` 是抽象方法（人人要有形状），`is_graph_capture_supported/is_current_stream_capturing` 是能力位（人人默认关闭）。

**CUDA 的 opt-in**：

- [accelerator/cuda_accelerator.py:L290-L305](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cuda_accelerator.py#L290-L305)：`is_graph_capture_supported() -> True`，`is_current_stream_capturing()` 委托 `torch.cuda.is_current_stream_capturing()`，图三件套直接映射 `torch.cuda.CUDAGraph` / `torch.cuda.graph` / `graph.replay()`。
- [accelerator/npu_accelerator.py:L249-L263](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/npu_accelerator.py#L249-L263)：NPU 是另一个合法 opt-in 样本——`True` + `torch.npu.is_current_stream_capturing()` + `NPUGraph`，结构与 CUDA 完全对称。

**CPU 的「有形状、无能力」**：

- [accelerator/cpu_accelerator.py:L388-L397](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/cpu_accelerator.py#L388-L397)：`create_graph()` 返回 `None`，`capture_to_graph()` 返回空上下文（函数体立即 eager 执行），`replay_graph()` 为空操作；同时**不覆写** `is_graph_capture_supported`，继承基类 `False`。

**消费方：查能力位、优雅回退**：

- [deepspeed/runtime/rollout/hybrid_engine_rollout.py:L208-L215](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/rollout/hybrid_engine_rollout.py#L208-L215)：rollout 路径在 `use_graph_capture` 为真时先查 `is_graph_capture_supported()`，不支持则 `logger.warning_once`「falling back to eager generation」并置 `use_graph_capture = False`——**回退而非报错**。同类检查也出现在 [deepspeed/runtime/hybrid_engine_graph.py:L201](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/runtime/hybrid_engine_graph.py#L201) 的图缓存校验里。

**KLXPU：从 default-deny 到继承 opt-in（#9042）**：

- [accelerator/klxpu_accelerator.py:L9-L35](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/klxpu_accelerator.py#L9-L35)：现在的 KLXPU 类继承 `CudaAccelerator`，类注释说明原因——`torch_xmlir` 以 torch.cuda 的面目呈现，设备字符串、流、RNG、内存管理全部继承 CUDA；子类只覆写真正的差异点（host 计时、nvtx、triton、算子目录、环境变量）。**注意现在的文件里已经没有 `is_graph_capture_supported` 的覆写**——这就是 #9042 的结果。
- 历史（[commit de6fcf18b](https://github.com/deepspeedai/DeepSpeed/commit/de6fcf18ba1d3395dbcf0c6d48060da263b82da8)，`git show de6fcf18b` 可复现）：删除前子类里有这样一段（**历史代码，当前版本已不存在**）：

```python
def is_graph_capture_supported(self):
    # Presents itself through the CUDA runtime, but HybridEngine graph
    # capture is not validated on this backend. Keep the explicit
    # default-deny instead of inheriting the CUDA opt-in.
    return False
```

  注释把动机写得非常清楚：通过 CUDA runtime 呈现 ≠ 图捕获已验证，所以宁可显式拒绝也不隐式继承 CUDA 的 opt-in。同一提交还删掉了配套的回归测试 `test_klxpu_keeps_explicit_graph_capture_opt_out`（原 tests/unit/accelerator/test_accelerator_graph.py 尾部，断言 KLXPU 实例的 `is_graph_capture_supported() is False`）——能力位翻转后，守护旧契约的测试一并退役。
- 契约的文档化表述：[docs/_tutorials/accelerator-abstraction-interface.md:L51-L57](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/accelerator-abstraction-interface.md#L51-L57) 写明「opt-in 可被继承，因此未验证的子类必须覆写为 False；静态 KV cache 用该查询跳过录制期间的 host 端边界检查，缺任一钩子的后端保持 eager 生成」。

**契约测试**：

- [tests/unit/accelerator/test_accelerator_graph.py:L11-L44](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/accelerator/test_accelerator_graph.py#L11-L44)：`test_graph_capture_replay_roundtrip` 在「当前后端暴露图」时做完整的捕获-回放往返（含 side-stream 预热——与 `graph_process` 相同的模式），并断言捕获体内 `is_current_stream_capturing()` 为真。
- [tests/unit/accelerator/test_accelerator_graph.py:L47-L67](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/accelerator/test_accelerator_graph.py#L47-L67)：`test_graph_ops_run_eagerly_without_graph_support` 固化 CPU 契约：`create_graph()` 为 `None`、捕获体立即执行、`replay_graph` 既不报错也不改变值——注释点明这是 CPU-only CI 上跑的契约。

#### 4.4.4 代码实践

**实践目标**：亲手验证「默认关闭、显式开启、优雅回退」三段契约，并用 git 历史复现 KLXPU 的演进（本讲指定实践任务的第三部分）。

**操作步骤**：

1. **在 CPU 机器上跑契约测试**（无需 GPU）：

```bash
python -m pytest tests/unit/accelerator/test_accelerator_graph.py -v
```

预期 `test_graph_capture_replay_roundtrip` 被 skip（CPU 不支持图），`test_graph_ops_run_eagerly_without_graph_support` 通过。

2. **手动复现回退行为**（**示例代码**）：

```python
from deepspeed.accelerator import get_accelerator

use_graph_capture = True
if use_graph_capture and not get_accelerator().is_graph_capture_supported():
    print("warning: falling back to eager generation")
    use_graph_capture = False
print("use_graph_capture =", use_graph_capture)   # CPU 上为 False
```

3. **复现 KLXPU 演进**：

```bash
git show de6fcf18b                     # 查看被删除的 default-deny 覆写与配套测试
git show de6fcf18b^:accelerator/klxpu_accelerator.py | grep -n -A4 is_graph_capture_supported
grep -n is_graph_capture_supported accelerator/klxpu_accelerator.py   # 当前版本：无匹配
```

4. 阅读 [docs/_tutorials/accelerator-abstraction-interface.md:L46-L57](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/accelerator-abstraction-interface.md#L46-L57)，对照源码注释逐句确认契约表述。

**需要观察的现象**：第 3 步里，父提交（`de6fcf18b^`）的文件里有 `return False` 的覆写，当前 HEAD 的文件里 `grep` 无输出——**能力位从「子类显式拒绝」变为「继承 CUDA 的 True」只删除了 6 行**。同时 tests/unit/accelerator/test_accelerator_graph.py 里守护旧契约的测试也被删除（而不是被改绿），这是「契约测试随契约退役」的示范。

**预期结果**：CPU 上三条命令全部可复现；GPU 机器上第 1 步的 roundtrip 测试应通过（待本地验证）。据此你可以回答：一个新后端从「拒绝图捕获」到「进入图捕获路径」需要做的事是——实现 `is_current_stream_capturing`、完成 HybridEngine 图路径验证、删除 default-deny 覆写、（若原有守护测试）一并移除。

#### 4.4.5 小练习与答案

**练习 1**：为什么不把 `is_graph_capture_supported` 设计成「`create_graph()` 返回非 None 即支持」的自动推断？
**答案**：CPU 的存在就是反例——它可以实现完整的图 API 形状（`create_graph` 可返回对象），但语义是 eager 假捕获；反过来，一个后端可能有图 API 却没实现 `is_current_stream_capturing`，捕获期间静态 KV cache 的 host 端检查会非法。能力位把「有 API」与「验证过、能安全捕获」拆成两个正交声明，基类注释（abstract_accelerator.py L218-L224）对此有明确表述。

**练习 2**：#9042 删除覆写后，如何防止「KLXPU 图捕获回归」被无意引入？
**答案**：守护方向翻转了：旧测试断言「必须是 False」，删除后由通用的 `test_graph_capture_replay_roundtrip`（在任何暴露图的后端上做真实捕获-回放往返）与 HybridEngine rollout 的图路径测试（u7-l4 涉及）兜底——一旦图捕获在 KLXPU 上真正失效，往返测试会失败。能力位的语义责任从「显式声明」交还给「真实能力」。

**练习 3**：设想你要新增 `XYZ_Accelerator(CUDA_Accelerator)`，图捕获尚未验证，你该写什么？
**答案**：按文档契约「未验证的子类必须覆写为 False」：实现 `def is_graph_capture_supported(self): return False` 并加注释说明「继承 CUDA 但未验证」。这与 KLXPU 在 #9042 之前的写法完全一致（见 4.4.3 的历史代码）。验证完成后再删除覆写。

### 4.5 模块五：XPU 的 Level Zero pinned memory 注册——绕过 torch 的自力更生

#### 4.5.1 概念说明

pinned memory 的「注册」语义：把一块**已经存在**的主机内存（例如原生 pinned 后端用 mlock 锁住的缓冲）映射进设备页表，使设备 DMA 可以直接安全访问它。CUDA 有 `cudaHostRegister`，但 Intel GPU 的 torch 通道（`torch.xpu`）**没有暴露等价 API**。XPU 后端的解法是：既然 torch 不给通道，就直接用 `ctypes` 加载 Level Zero 的用户态驱动加载器 `libze_loader.so.1`，走 `ZE_extension_external_memmap_sysmem` 扩展完成同一件事。

这是后端适配里「**torch 通道不可用时自力更生**」的典型模式：

1. 用 `ctypes.CDLL` 直接加载厂商驱动库；
2. 按 C 头文件手写结构体布局与函数签名（`ze_api.h` 的稳定 ABI）；
3. 可选扩展先探测（probe）再使用，失败路径一律降级（warn_once + 回退 mlock-only），绝不崩溃。

#### 4.5.2 核心流程

```text
register_host_memory(address, num_bytes)   ← 原生 pinned 通道调用（4.1.3 的分发层）
  ├─ _l0_device_context(current_device())
  │    ├─ _l0_loader()                       （lru_cache 全局一次）
  │    │    CDLL("libze_loader.so.1")
  │    │    zeInit → zeDriverGet
  │    │    zeDriverGetExtensionProperties 探测
  │    │      "ZE_extension_external_memmap_sysmem" 不在 → None（不可用）
  │    │    声明 zeMemAllocHost / zeMemFree 等签名
  │    └─ 按 UUID 匹配 torch 设备 → zeContextCreateEx 单设备上下文
  │         （多设备上下文只映射首个设备的页表 → 其他设备拷贝报
  │          OUT_OF_DEVICE_MEMORY，故必须单设备上下文；
  │          设备页表跨上下文共享，映射后任何上下文可用）
  ├─ 无 Level Zero → warning_once + return False（回退 mlock-only）
  ├─ zeMemAllocHost(host_desc ⇐ memmap_desc → pSystemMemory=address)
  │    ├─ rc != SUCCESS → warning_once + return False
  │    └─ mapped != address（非恒等映射）→ zeMemFree + return False
  └─ 记录 (_l0_registrations[address]) → return True

unregister_host_memory(address)
  └─ zeMemFree(context, address)；失败 → raise RuntimeError
     （保持 CPU 分配存活，防止释放驱动仍映射的页）
```

#### 4.5.3 源码精读

文件头的设计注释与 ABI 常量：

- [accelerator/xpu_accelerator.py:L19-L35](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/xpu_accelerator.py#L19-L35)：注释先讲清机制——`zeMemAllocHost` 挂上 `ze_external_memmap_sysmem_ext_desc_t` 链式描述符后，可把**已存在的页锁定主机缓冲**映射进设备页表（返回指针等于输入指针，恒等映射），`zeMemFree` 只解除映射不触碰主机内存本身；「这是 cudaHostRegister/cudaHostUnregister 的 XPU 等价物」。随后是从 `ze_api.h` 抄出的稳定 ABI 常量（结构体类型码、结果码、扩展名长度）。L38-L63 的四个 `ctypes.Structure`（`_ZeContextDesc`、`_ZeExternalMemmapSysmemDesc`、`_ZeHostMemAllocDesc`、`_ZeDriverExtensionProperties`）与 C 布局逐字段对应。

加载器与扩展探测：

- [accelerator/xpu_accelerator.py:L66-L125](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/xpu_accelerator.py#L66-L125)：`_l0_loader()` 带 `@functools.lru_cache(maxsize=None)`——进程内只加载一次。流程：`CDLL("libze_loader.so.1")`（失败返回 `None`）→ `zeInit` → `zeDriverGet` 枚举驱动 → `zeDriverGetExtensionProperties` **探测**扩展名列表，找不到 `ZE_extension_external_memmap_sysmem` 就返回 `None`（注释明确「带描述符的 zeMemAllocHost 是可选扩展，用前先探测」）→ 逐个声明 `zeContextCreateEx`/`zeMemAllocHost`/`zeMemFree` 的 argtypes/restype。

单设备上下文与 UUID 匹配：

- [accelerator/xpu_accelerator.py:L128-L170](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/xpu_accelerator.py#L128-L170)：`_l0_device_context(device_index)` 的 docstring 解释了两个非显然的坑：其一，多设备上下文里驱动只为**首个**设备映射内存范围，其他设备发起的拷贝会报 `ZE_RESULT_ERROR_OUT_OF_DEVICE_MEMORY`，所以要用 `zeContextCreateEx` 建**只含目标设备**的上下文；其二，`zeDeviceGet` 的设备顺序不保证与 torch 一致，所以拿 `torch.xpu.get_device_properties(idx).uuid` 与 Level Zero 读出的 UUID 逐个比对（并格式化成相同的 `8-4-4-4-12` 连字符形式），**不做位置等同假设**。设备页表跨上下文共享，映射建好后 torch 自己的运行时上下文也能用。

注册与注销：

- [accelerator/xpu_accelerator.py:L400-L434](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/xpu_accelerator.py#L400-L434)：`register_host_memory` 把 `pSystemMemory=address, size=num_bytes` 填进 memmap 描述符、链到 host 分配描述符上、调 `zeMemAllocHost`。三条失败路径全部**优雅降级**：无 Level Zero → warning_once + `False`；调用失败 → warning_once + `False`；**非恒等映射**（`mapped.value != address`）→ 释放映射 + `False`（调用方持有原指针，映射搬了地址就没法用）。成功则登记 `_l0_registrations[address]`（L174 的模块级 dict，按地址记账）并返回 `True`。
- [accelerator/xpu_accelerator.py:L436-L447](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/xpu_accelerator.py#L436-L447)：`unregister_host_memory` 用 `zeMemFree` 解除映射，**失败时 raise RuntimeError**——注释解释了与注册侧不对称的原因：报错能让 `NativePinnedMemory.unpin` 保留 CPU 分配存活，避免释放掉驱动可能仍映射着的页。注册侧「降级」而注销侧「报错」，是因为前者缺了功能仍可跑（mlock-only），后者静默失败会造成真正的内存安全隐患。

#### 4.5.4 代码实践

**实践目标**：以源码阅读型实践走通「torch 通道缺失 → ctypes 自力更生 → 失败优雅降级」的完整链条（本讲指定实践任务的第二部分；XPU 硬件相关现象待本地验证）。

**操作步骤**：

1. 通读 [accelerator/xpu_accelerator.py:L19-L63](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/xpu_accelerator.py#L19-L63)，列出「CUDA 概念 → Level Zero 对应物」映射表：

| CUDA / 通用概念 | Level Zero 对应（源码位置） |
| --- | --- |
| 驱动加载 | `ctypes.CDLL("libze_loader.so.1")`（L70） |
| `cudaHostRegister(ptr, size, flags)` | `zeMemAllocHost` + memmap 扩展描述符链（L409-L417） |
| `cudaHostUnregister(ptr)` | `zeMemFree(context, ptr)`（L441） |
| 恒等映射保证 | `mapped.value != address` 检查（L424-L432） |
| 可选能力探测 | `zeDriverGetExtensionProperties`（L89-L103） |

2. 在**任意机器**（包括 CPU）上梳理失败路径：`_l0_loader()` 第一步 `CDLL` 就会因找不到 `libze_loader.so.1` 返回 `None`，随后 `_l0_device_context` 返回 `None`，最终 `register_host_memory` 打出 warning_once「Level Zero host-memory registration is unavailable ... native pinned memory stays mlock-only」并返回 `False`。用下列**示例代码**在 CPU 上验证这条降级链不需要任何 Intel 硬件：

```python
# 只验证降级链，不需要 XPU 硬件
from deepspeed.accelerator.cpu_accelerator import CPU_Accelerator
accel = CPU_Accelerator()
print(accel.register_host_memory(0, 0))   # 基类默认：False（CPU 未覆写）
# XPU 版本在无 libze_loader 的机器上同样返回 False 并 warning_once，
# 该行为需要 torch.xpu 环境，待本地验证。
```

3. 对照 4.1.3 的分发层（abstract_accelerator.py L337-L352）画出完整调用图：`accelerator.pin_memory(t)` → 原生 pinned 通道（`DS_PIN_MEMORY_BACKEND` 启用时）→ `register_host_memory(addr, nbytes)` → Level Zero 或 mlock-only。

**需要观察的现象**：第 2 步在 CPU 上返回 `False` 且不抛异常；如果你在 Intel GPU 机器上运行（装好 torch.xpu 与 Level Zero），应看到注册返回 `True`、`_l0_registrations` 里出现一条记录（待本地验证）。

**预期结果**：你总结出的模式应当是——**探测（probe）先行、可选能力不假设、失败路径全部有降级、资源注销失败宁可报错也不静默**。这四条同样适用于你为任何新硬件写后端适配。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `_l0_loader` 与 `_l0_device_context` 都加 `lru_cache`，而 `_l0_registrations` 是普通 dict？
**答案**：前两者是**纯函数式的环境事实**——加载器句柄与设备上下文在进程内不变，缓存避免每次注册都重复 `CDLL`/`zeInit`/UUID 扫描。`_l0_registrations` 是**可变状态**（注册了什么、注销了什么随运行变化），必须允许修改，dict 按地址作为 key 记账（L174、L433、L437）。

**练习 2**：`register_host_memory` 要求恒等映射（返回指针 == 输入指针），为什么这是硬性要求？
**答案**：调用方（原生 pinned 通道）已经持有并继续使用**原始**指针——张量数据结构里存的就是它。若驱动返回一个搬了家的映射地址，设备访问映射区与 host 访问原地址会落在两块内存上，数据悄无声息地不一致；而调用方无法整体换用新地址。所以非恒等映射只能释放掉并按「不可用」降级（L424-L432 的注释原文：callers hold the original pointer）。

**练习 3**：注册失败走 warning + 降级，注销失败却 raise，这个不对称合理吗？
**答案**：合理，按「失败后能否安全继续」划分。注册失败 = 功能缺失，mlock-only 的 pinned 内存仍然正确（少了设备页表映射的性能收益），可以继续跑；注销失败 = 驱动可能仍持有对这段主机内存的映射，此时把 CPU 侧内存释放掉，后续任意写入都可能污染尚在进行的设备 DMA——静默继续是不安全的，报错让上层保留分配（L442-L447 注释）才是安全默认。

## 5. 综合实践

**任务：写一个「后端能力探针」脚本，并用它复盘本讲全部知识点。**

在 CPU 机器即可完成。脚本（**示例代码**）：

```python
# probe_backend.py —— 后端能力探针
from deepspeed.accelerator import get_accelerator, set_accelerator
from deepspeed.accelerator.cpu_accelerator import CPU_Accelerator

# ① 当前后端的基本能力报告
a = get_accelerator()
report = {
    "name": a.device_name(),
    "comm_backend": a.communication_backend_name(),
    "is_synchronized_device": a.is_synchronized_device(),
    "use_host_timers": a.use_host_timers(),
    "graph_capture_supported": a.is_graph_capture_supported(),
    "bf16": a.is_bf16_supported(),
    "fp16": a.is_fp16_supported(),
    "fp64": a.is_fp64_supported(),
    "triton": a.is_triton_supported(),
    "prefer_triton_grouped_mm": a.prefer_triton_grouped_mm(),
    "total_memory_bytes": a.total_memory(),
    "available_memory_bytes": a.available_memory(),
    "device_count": a.device_count(),
}
for k, v in report.items():
    print(f"{k:28s} = {v}")

# ② 图操作契约：CPU 上应是「假捕获 + 空回放」
import torch
g = a.create_graph()
print("create_graph() ->", g)            # CPU: None
t = torch.zeros(1)
with a.capture_to_graph(g):
    t.fill_(1.0)                          # 捕获体立即 eager 执行
print("after capture:", t.item())         # 1.0
a.replay_graph(g)                         # 空操作，不报错
print("after replay:", t.item())          # 仍是 1.0

# ③ set_accelerator 替换单例（模拟扩展包注册自定义后端）
class MyCPUAccelerator(CPU_Accelerator):
    def __init__(self):
        super().__init__()
        self._name = "mycpu"

mine = MyCPUAccelerator()
set_accelerator(mine)
print("swapped to:", get_accelerator()._name)   # mycpu
```

完成后再做三件复盘：

1. 把报告输出与 4.3.4 的对照表并排，逐项确认 CPU 列与源码一致；
2. 对照 4.4 的契约解释 `graph_capture_supported=False` 时脚本②的行为为什么是「安全降级」而非「功能缺失报错」；
3. 用 `git show de6fcf18b` 复现 KLXPU 能力位翻转，回答：如果 `MyCPUAccelerator` 继承的是 `CUDA_Accelerator` 且图捕获未验证，你应该在类里加哪三行代码？（答案见 4.4.5 练习 3。）

**预期结果**：CPU 环境下脚本全绿；③证明单例可替换且替换经过 `_validate_accelerator` 双基类校验。GPU/XPU 环境下同一脚本输出的差异（`nccl`、`graph_capture_supported=True`、pynvml 口径显存、Level Zero 注册成功）即为各后端语义差异的活样本（待本地验证）。

## 6. 本讲小结

- **抽象层的形状**：`DeepSpeedAccelerator` 用约 20 组、60+ 个方法覆盖设备/RNG/流/内存/类型/图/张量/pinned memory/算子构建；抽象方法强制「人人实现」，能力位（`is_graph_capture_supported`、`prefer_triton_grouped_mm`、`register_host_memory`…）默认保守值、后端显式 opt-in。
- **探测协议**：`DS_ACCELERATOR` 覆盖优先（带扩展预检、非法值 fail-fast），否则按固定顺序 try-import 自动探测；`torch_supa`/`torch_xmlir` 会伪装成 torch.cuda，必须在 CUDA 之前检查；结果缓存在全局单例，`set_accelerator` 可替换并经双基类 isinstance 校验（符号链接双轨布局的必然产物）。
- **参照实现的取舍**：CUDA 直连 `torch.cuda`（pynvml 空闲显存、nccl、真图捕获），CPU 全面退化（psutil + cgroup 口径、ccl/gloo、`None` 图 + eager 空操作）；`is_synchronized_device` 一个事实派生出计时/依赖/反压三个问题。
- **图捕获是显式声明而非隐式继承**：基类默认 `False`；CUDA/NPU 验证后 opt-in 且必须先实现 `is_current_stream_capturing`；消费方（rollout、图缓存）查到不支持就 warning + 回退 eager。KLXPU 先 default-deny、#9042 验证后删除覆写恢复继承——支持与否始终是后端自己的显式决定。
- **自力更生模式**：XPU 在 torch 无对应通道时用 ctypes 直驱 Level Zero——lru_cache 的加载器、可选扩展先探测、UUID 匹配设备、单设备上下文、恒等映射校验、失败一律 warn + 降级、注销失败宁可报错。

## 7. 下一步学习建议

- **下一讲 u3-l2（deepspeed.comm）**：`communication_backend_name()` 返回的 `nccl`/`gloo`/`ccl` 字符串正是 comm 层初始化进程组的输入——加速器抽象回答「用哪个后端」，comm 层回答「怎么通信」。
- **u3-l3（进程组管理）**：ZeRO 与各种并行的坐标系建立在这些基础之上。
- **u7-l4（Hybrid Engine）**：本讲的 `is_graph_capture_supported` 在那里被两条生成路径消费（回退 eager 的 warning 就来自那里），静态 KV cache 与 `is_current_stream_capturing` 的配合也将展开。
- **延伸阅读**：[docs/_tutorials/accelerator-abstraction-interface.md](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/_tutorials/accelerator-abstraction-interface.md)（移植规则全表）与 [deepspeed/utils/pin_memory.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/utils/pin_memory.py)（`DS_PIN_MEMORY_BACKEND` 原生 pinned 通道，4.5 的调用上游）；有兴趣可以对照读 [accelerator/hpu_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/hpu_accelerator.py) 与 [accelerator/supa_accelerator.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/accelerator/supa_accelerator.py)，看其余后端如何组合这两种策略。
