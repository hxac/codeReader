# ZeRO 总览：三阶段与配置体系

## 1. 本讲目标

学完本讲，你应该能够：

1. 用显存公式 \( M = 2\Psi + 2\Psi + K\Psi \)（参数 + 梯度 + 优化器状态）解释 ZeRO 三个阶段分别切分了哪一项、每卡显存降到了多少。
2. 说出 `ZeroStageEnum` 四个取值的含义，并在 `engine.py` 的 `_configure_zero_optimizer` 中定位「stage 如何决定创建哪个优化器类」的分发代码。
3. 画出 `deepspeed/runtime/zero/` 包的文件地图：参数切分、梯度切分、优化器状态切分分别落在哪些文件、哪些类。
4. 列出 `zero/config.py` 与 `offload_config.py` 中每个配置类对应的 JSON 字段、默认值与适用阶段，包括本周期（#9044）新增的 `retain_gpu_grad_partition_until_step`（默认 `false`）。
5. 说清 `retain_gpu_grad_partition_until_step` 的精确适用范围：只影响「ZeRO-2 + CPU 优化器卸载 + fp32 梯度累积（紧凑分区）」这一条路径——默认在请求边界释放 GPU 上的 fp32 梯度分区、下次 backward 从 host 梯度恢复；置 `true` 则分区驻留到 `step()`，省掉每请求的主机拷贝/恢复，代价是请求间显存多占一个 fp32 分区。

本讲是 ZeRO 单元（u5）的 8 篇讲义中的第一篇，只建立框架，不深入任何一篇的内部细节。

## 2. 前置知识

- **混合精度训练的显存构成**：一个 `Ψ` 参数的模型用 fp16/bf16 训练时，显存里同时存在三份「模型状态」——16 位参数（`2Ψ` 字节）、16 位梯度（`2Ψ` 字节）、以及优化器状态。以混合精度 Adam 为例，优化器状态包括 fp32 主权重副本、一阶动量（momentum）、二阶动量（variance），各 `4Ψ` 字节，合计 `KΨ = 12Ψ` 字节。所以标准数据并行下每卡模型状态显存是 \( 16\Psi \) 字节——7.5B 参数的模型仅模型状态就要约 120 GB，远超单卡显存。这就是「显存墙」。
- **数据并行的冗余**：经典 DDP 中每张卡都完整持有这 `16Ψ` 字节，但每张卡其实只「负责」自己那份数据的梯度。ZeRO（Zero Redundancy Optimizer）的洞察是：优化器状态、梯度、参数这三样东西都可以按数据并行维度切开，每卡只留 `1/N_d`，需要聚合时再用集合通信拼回来——用通信换显存。
- **请求边界（request boundary / gradient accumulation boundary）**：梯度累积窗口内，每个 micro-batch 一次 backward；只有到达累积边界（对 `engine.step()` 的手动模式而言就是每次 step 前）才做梯度归约与优化器更新。本讲 4.5 节会反复用到这个概念。
- **前置讲义**：u2-l2 讲过 `DeepSpeedConfig` 如何把 JSON 展开为强类型配置对象——顶层标量走 `get_scalar_param`，嵌套块走 pydantic 模型 `DeepSpeedConfigModel`。`zero_optimization` 块正是后者的典型代表，本讲直接延续这套读取方式。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到 |
| --- | --- | --- |
| `deepspeed/runtime/zero/__init__.py` | zero 包公共导出（`Init`、`GatheredParameters`、`DeepSpeedZeRoOffload` 等） | 4.2 文件地图 |
| `deepspeed/runtime/zero/config.py` | `ZeroStageEnum` 阶段常量 + `DeepSpeedZeroConfig`（`zero_optimization` 块的 pydantic 模型）+ `get_zero_config` 解析入口 | 4.1 / 4.3 / 4.5 |
| `deepspeed/runtime/zero/offload_config.py` | `OffloadDeviceEnum` 与两个卸载配置类（参数卸载 / 优化器卸载） | 4.4 |
| `deepspeed/runtime/engine.py` | 引擎侧访问器与 `_configure_zero_optimizer` 的 stage 分发 | 4.1 / 4.2 / 4.5 |
| `deepspeed/runtime/zero/stage_1_and_2.py` | `DeepSpeedZeroOptimizer`（stage 1/2 优化器），本讲只读紧凑梯度分区相关的几个函数 | 4.5 |
| `tests/unit/v1/zero/test_zero_offload_accum_precision.py` | CPU 卸载累积精度与 retain 开关的回归测试 | 4.5 实践 |

其余文件（`partition_parameters.py`、`parameter_offload.py`、`stage3.py` 等）本讲只在地图中点名，后续讲义逐一精读。

## 4. 核心概念与源码讲解

### 4.1 ZeRO 的动机与显存公式

#### 4.1.1 概念说明

ZeRO 解决的问题是：数据并行在数学上只需要每卡处理自己的 micro-batch，但工程上每卡都复制了完整的模型状态。把模型状态按数据并行维度切成 \( N_d \) 份，每卡只留一片，就能在不改变训练语义（梯度仍是全局平均、参数仍保持一致）的前提下把显存降下来。

三项模型状态被切分的先后顺序是有讲究的——按「性价比」排列：

1. **优化器状态**（\( K\Psi \)，最大头）：fp32 主权重和动量只在优化器 step 时才整体使用，天然可以每卡只算自己那 \( 1/N_d \) 片参数的更新。
2. **梯度**（\( 2\Psi \)）：reduce-scatter 之后每卡只需要自己分片对应的梯度，其余的用完即可丢弃。
3. **参数**（\( 2\Psi \)）：最激进。前向/反向用到哪段参数就临时 all-gather 哪段，用完释放。

#### 4.1.2 核心流程

设参数量为 \( \Psi \)、数据并行度为 \( N_d \)、Adam 混合精度的优化器状态系数 \( K = 12 \)。每卡模型状态显存：

\[ M_{\text{stage}_0} = \underbrace{2\Psi}_{\text{参数}} + \underbrace{2\Psi}_{\text{梯度}} + \underbrace{K\Psi}_{\text{优化器状态}} = 16\Psi \;\text{字节} \]

这就是学习目标里「参数 \( \Psi \) 项 + \( K\Psi \) 优化器状态项 + \( 2\Psi \) 梯度项」的三项分解。三个阶段依次把第三、二、一项除以 \( N_d \)：

| stage | 切分对象 | 每卡模型状态显存 | 相对 stage 0 |
| --- | --- | --- | --- |
| 0（disabled） | 无 | \( 2\Psi + 2\Psi + K\Psi \) | \( 16\Psi \) |
| 1（optimizer_states） | 优化器状态 | \( 2\Psi + 2\Psi + \dfrac{K\Psi}{N_d} \) | \( 4\Psi + \dfrac{12\Psi}{N_d} \) |
| 2（gradients） | + 梯度 | \( 2\Psi + \dfrac{(2 + K)\Psi}{N_d} \) | \( 2\Psi + \dfrac{14\Psi}{N_d} \) |
| 3（weights） | + 参数 | \( \dfrac{(4 + K)\Psi}{N_d} \) | \( \dfrac{16\Psi}{N_d} \) |

两个工程上的补充：

- 上式只是**模型状态**。实际峰值显存还要加上激活值（activation），以及 ZeRO 自己的通信缓冲（stage 1/2 的 `reduce_bucket_size`、stage 3 的 `allgather_bucket_size`，默认都是 `5e8` 个元素，fp16 下约 1 GB）。所以实测值只会渐进逼近公式，不会精确相等。
- 通信代价随阶段上升：stage 1/2 只在梯度上多做 reduce-scatter（stage 1 甚至可以沿用 all-reduce），stage 3 前向/反向期间要不断 all-gather 参数。「以通信换显存」的兑换率由 bucket 尺寸与重叠（`overlap_comm`）等配置决定——这正是 4.3 节字段表的主体。

#### 4.1.3 源码精读

阶段常量定义在 `ZeroStageEnum`，它是一个 `int` 枚举，所以引擎里可以直接用 `<=`、`>=` 比较：

[deepspeed/runtime/zero/config.py:L81-L87](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py#L81-L87)

```python
class ZeroStageEnum(int, Enum):
    """ Enum class for possible zero stages """
    disabled = 0
    optimizer_states = 1
    gradients = 2
    weights = 3
    max_stage = 3
```

这四个名字本身就是三阶段语义的权威定义：1=切优化器状态，2=再切梯度，3=再切参数。

引擎侧读取阶段的是一个薄访问器，把配置对象的值原样透出：

[deepspeed/runtime/engine.py:L1614-L1615](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1614-L1615)

```python
def zero_optimization_stage(self):
    return self._config.zero_optimization_stage
```

而「是否启用 ZeRO 包装」与「用哪个包装」由 `_do_optimizer_sanity_check` 决定——只有 `zero_optimization()` 为真（stage > 0）才返回 `ZERO_OPTIMIZATION`：

[deepspeed/runtime/engine.py:L2327-L2344](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2327-L2344)

```python
def _do_optimizer_sanity_check(self, basic_optimizer):
    model_dtype, grad_accum_dtype = self.get_data_types()
    zero_enabled = self.zero_optimization()
    if zero_enabled:
        if not is_zero_supported_optimizer(basic_optimizer):
            ...
        if model_dtype == torch.bfloat16 and grad_accum_dtype == torch.float32 and self.zero_optimization_stage(
        ) == 1 and not self.zero_cpu_offload():
            ...
            return BFLOAT16
        return ZERO_OPTIMIZATION
```

注意 stage 0 并不进入 ZeRO 包装——它走 dtype 分支（`None`/`FP16`/`BFLOAT16`/`DDP_BFLOAT16`），即 ZeRO 关闭时由普通优化器包装接管。另有一个特例：bf16 模型 + fp32 梯度累积 + stage 1 + 无卸载时返回 `BFLOAT16`，交给专门的 ZeRO-1 BF16 优化器（u4-l3 讲过的 `BF16_Optimizer` 路径）。

#### 4.1.4 代码实践

**实践：用公式预测再验证 stage 枚举的读取链**

1. 实践目标：不启动分布式、不碰 GPU，验证「JSON 里的 stage 数字 → `ZeroStageEnum` → 引擎访问器」这条读取链，并对显存公式做一次手算。
2. 操作步骤（示例代码，可直接在仓库根目录用 `python -I -c` 或保存为脚本运行）：

   ```python
   from deepspeed.runtime.zero.config import get_zero_config, ZeroStageEnum

   cfg = get_zero_config({"zero_optimization": {"stage": 2}})
   print(int(cfg.stage))                       # 预期 2
   print(cfg.stage == ZeroStageEnum.gradients) # 预期 True（int 枚举可直接比较）
   print(int(ZeroStageEnum.weights))           # 预期 3
   ```

   再手算一遍：`Ψ = 1e9`（10 亿参数）、`N_d = 8`、Adam 混合精度，代入 4.1.2 的表格算出四个阶段的每卡模型状态字节数。
3. 需要观察的现象：`stage` 被解析为枚举值且与 `int` 兼容；手算表中 stage 3 每卡应得 \( 16 \times 10^9 / 8 = 2 \) GB。
4. 预期结果：打印 `2 / True / 3`；手算 stage 0/1/2/3 分别为 16 GB、5.5 GB、3.75 GB、2 GB。（输出为依据源码默认值推导的预期值，具体运行结果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 ZeRO 先切优化器状态而不是先切参数？
**答案**：优化器状态占 \( K\Psi = 12\Psi \)，是三项中最大的一块，且只在 step 时整体使用，切它收益最大、对前向/反向计算图的干扰最小；而参数在前向/反向全程被用到，切参数（stage 3）需要引入参数收集/释放机制，工程复杂度和通信代价都最高。

**练习 2**：`ZeroStageEnum` 为什么继承 `int`？
**答案**：这样枚举值可以直接参与数值比较，例如引擎里 `zero_stage <= ZeroStageEnum.gradients`（覆盖 stage 0/1/2）和 `self.zero_optimization_stage() >= ZeroStageEnum.weights`（判断是否切参数）这类区间判断才成立。

**练习 3**：7.5B 参数、`N_d = 64`、stage 3 下每卡模型状态显存是多少？如果用 stage 2 呢？
**答案**：stage 3：\( 16\Psi/N_d = 16 \times 7.5\text{B} / 64 \approx 1.875 \) GB；stage 2：\( 2\Psi + 14\Psi/N_d = 15 + 1.64 \approx 16.6 \) GB——16 位参数不切分仍是每卡 15 GB，这就是大模型必须上 stage 3 的原因。

### 4.2 zero 包文件布局与三类切分的落点

#### 4.2.1 概念说明

`deepspeed/runtime/zero/` 是 ZeRO 的实现主体。它不是一个大文件，而是按「切什么」分工的一组模块。读源码前先建立文件地图，后续 7 篇讲义（u5-l2 到 u5-l8）分别深入其中一至两个文件。

#### 4.2.2 核心流程

三类切分在代码中的落点：

| 切分对象 | 主要文件 | 主要类/机制 |
| --- | --- | --- |
| 优化器状态（stage 1/2/3） | `stage_1_and_2.py`、`stage3.py` | `single_partition_of_fp32_groups`：每卡只持有自己分片的 fp32 主权重与动量 |
| 梯度（stage 2） | `stage_1_and_2.py` | IPG（独立并行梯度）bucket + reduce-scatter；本周期新增的「紧凑梯度分区」缓冲（见 4.5） |
| 参数（stage 3） | `partition_parameters.py`、`parameter_offload.py`、`partitioned_param_coordinator.py` | `zero.Init()` 让参数出生即切分（`ds_tensor`）；module hook 按需 all-gather；协调器负责预取与释放 |

分发发生在引擎的 `_configure_zero_optimizer`：

```
_configure_zero_optimizer(optimizer)
├── zero_stage <= 2（stage 0/1/2）
│   └── DeepSpeedZeroOptimizer（stage_1_and_2.py）
│       partition_grads = (stage == 2)          ← stage 1 与 2 用同一个类
│       retain_gpu_grad_partition_until_step=…  ← 本周期新增，见 4.5
│       （zenflow 开启时换成 ZenFlowZeroOptimizer）
└── zero_stage == 3
    ├── 无 client 优化器 → DeepSpeedZeRoOffload（parameter_offload.py，纯参数卸载）
    └── 有 client 优化器 → DeepSpeedZeroOptimizer_Stage3（stage3.py）
        （super_offload 开启时换成 SuperOffloadOptimizer_Stage3）
```

#### 4.2.3 源码精读

zero 包的公共导出清楚地标出了用户直接接触的符号——`Init`（参数出生即切分的上下文）、`GatheredParameters`（临时收集全量参数）、`DeepSpeedZeRoOffload`（参数卸载器）：

[deepspeed/runtime/zero/__init__.py:L6-L17](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/__init__.py#L6-L17)

```python
from .partition_parameters import ZeroParamType
from .partition_parameters import ZeroParamStatus
from .partition_parameters import Init
from .partition_parameters import GatheredParameters
from .partition_parameters import register_external_parameter
from .parameter_offload import DeepSpeedZeRoOffload
...
from .stage3 import unwrap_model_for_generation
```

stage 分发的关键代码在引擎里。stage 0/1/2 共用 `DeepSpeedZeroOptimizer`，靠 `partition_grads` 这个布尔区分 1 和 2：

[deepspeed/runtime/engine.py:L2687-L2728](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2687-L2728)

```python
if zero_stage <= ZeroStageEnum.gradients:
    ...
    Stage1And2ZeroOptimizer = DeepSpeedZeroOptimizer if not self.zenflow else ZenFlowZeroOptimizer.create(...)
    optimizer = Stage1And2ZeroOptimizer(
        optimizer,
        ...
        partition_grads=zero_stage == ZeroStageEnum.gradients,
        round_robin_gradients=round_robin_gradients,
        ...
```

stage 3 则分成「无用户优化器（纯推理/参数卸载）」与「有用户优化器（训练）」两条支路：

[deepspeed/runtime/engine.py:L2742-L2784](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2742-L2784)

```python
elif zero_stage == ZeroStageEnum.weights:
    self._validate_zero3_moe_compatibility()
    ...
    if isinstance(optimizer, DummyOptim):
        log_dist("Creating ZeRO Offload", ranks=[0])
        ...
        optimizer = DeepSpeedZeRoOffload(self.module, ...)
    else:
        ...
        Stage3ZeroOptimizer = DeepSpeedZeroOptimizer_Stage3 if not self.super_offload(
        ) else SuperOffloadOptimizer_Stage3
        optimizer = Stage3ZeroOptimizer(self.module, optimizer, ...)
```

超出 0-3 范围直接报错（[deepspeed/runtime/engine.py:L2830-L2831](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2830-L2831)）。

#### 4.2.4 代码实践

**实践：给分发地图标注行号（源码阅读型）**

1. 实践目标：把 4.2.2 的伪代码树变成带真实行号的地图，作为后续 7 篇讲义的导航页。
2. 操作步骤：打开 `deepspeed/runtime/engine.py` 的 `_configure_zero_optimizer`（从 L2665 开始），为以下每个分支记录行号区间：stage 0/1/2 分支入口、`partition_grads` 传参行、stage 3 分支入口、`DeepSpeedZeRoOffload` 构造、`DeepSpeedZeroOptimizer_Stage3` 构造。再打开 `deepspeed/runtime/zero/` 目录，为 4.2.2 表格的每个文件补一句「它定义了哪个类」。
3. 需要观察的现象：stage 1 与 stage 2 走完全相同的构造调用，唯一差别是 `partition_grads`；stage 3 的两条支路以「用户是否传了优化器」为分界。
4. 预期结果：一张行号标注的分发地图（本讲的引用行号即示例答案）。

#### 4.2.5 小练习与答案

**练习 1**：stage 1 和 stage 2 为什么可以共用 `DeepSpeedZeroOptimizer` 一个类？
**答案**：两者的差别只是「梯度归约后是否只保留本卡分片」，由构造参数 `partition_grads=(stage == 2)` 控制即可，不需要两套实现。这是「一个类 + 一个布尔」压缩平行实现的典型例子（对比之下 DeepSpeed 里 fp16/bf16 优化器是平行实现）。

**练习 2**：`DeepSpeedZeRoOffload` 与 `DeepSpeedZeroOptimizer_Stage3` 都出现在 stage 3 分支，它们的分工是什么？
**答案**：前者在用户没有提供优化器时创建（`optimizer is None` 会被包装成 `DummyOptim` 的场景），只负责参数的分区/收集/卸载（推理或纯参数卸载）；后者是完整的 stage 3 训练优化器，在参数卸载之上再做梯度 reduce-scatter 与优化器状态分区更新。

### 4.3 zero config 类：JSON 字段地图

#### 4.3.1 概念说明

`zero_optimization` 是 DeepSpeed JSON 里字段最多的块之一。它的载体是 pydantic 模型 `DeepSpeedZeroConfig`——字段名即 JSON 键名，字段的 docstring 即官方文档。理解这块配置的阅读方法是：**以类定义为准，而不是以 `ZERO_FORMAT` 示例字符串为准**（那个字符串只是 deprecated 格式的提示文案，并不枚举全部字段——例如本周期新增的 `retain_gpu_grad_partition_until_step` 就不在其中，见 [deepspeed/runtime/zero/config.py:L18-L55](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py#L18-L55)）。

#### 4.3.2 核心流程

解析链只有一步——`get_zero_config` 把 `zero_optimization` 块的字典直接灌进 pydantic 模型，未知字段会被 `DeepSpeedConfigModel`（禁止额外字段）拒绝，从而在配置期就暴露拼写错误：

[deepspeed/runtime/zero/config.py:L71-L78](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py#L71-L78)

```python
def get_zero_config(param_dict):
    if ZERO_OPTIMIZATION in param_dict:
        zero_config_dict = param_dict[ZERO_OPTIMIZATION]
        if isinstance(zero_config_dict, bool):
            zero_config_dict = read_zero_config_deprecated(param_dict)
    else:
        zero_config_dict = {}
    return DeepSpeedZeroConfig(**zero_config_dict)
```

（布尔形式的 `zero_optimization: true` 是远古兼容写法，等价于 stage 1。）

#### 4.3.3 源码精读

`DeepSpeedZeroConfig` 的字段按用途分组（完整默认值见 [deepspeed/runtime/zero/config.py:L90-L390](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py#L90-L390)）：

**全阶段通用（梯度归约与通信）**

| JSON 字段 | 默认 | 含义 |
| --- | --- | --- |
| `stage` | `0` | ZeRO 阶段（L95） |
| `contiguous_gradients` | `true` | 梯度产生时拷入连续缓冲，防碎片（L102） |
| `reduce_scatter` | `true` | 用 reduce-scatter 代替 all-reduce 平均梯度（L108） |
| `reduce_bucket_size` | `5e8` | 每次归约的元素数上限（L113） |
| `use_multi_rank_bucket_allreduce` | `true` | 合并多 rank 的桶做一次 all-reduce（L119） |
| `allgather_partitions` | `true` | step 末用 allgather（而非逐 rank broadcast）收集更新后参数（L126） |
| `allgather_bucket_size` | `5e8` | 每次 all-gather 的元素数上限（L132） |
| `overlap_comm` | 动态 | 通信与 backward 计算重叠；validator 默认仅 stage 3 为 `true`（L138、L393-L397） |
| `compute_grad_norm` | `true` | stage 1/2 是否计算并保留全局梯度范数（L143） |
| `retain_gpu_grad_partition_until_step` | `false` | **本周期新增**，见 4.5（L150） |
| `elastic_checkpoint` | `false` | 已废弃的弹性检查点（L168） |
| `sub_group_size` | `1e9` | 万亿模型的参数处理tile大小（L192） |
| `parameter_alignment` | `false` | stage 1/2 扁平缓冲 16 字节对齐（L327） |
| `zero_hpz_partition_size` | `1` | 参数分区二级分组大小（L334） |
| `zero_quantized_weights` / `zero_quantized_nontrainable_weights` / `zero_quantized_gradients` | `false` | 量化通信/存储（L338-L353） |
| `enable_sanity_checks` | `false` | 内部一致性检查（L375） |

**卸载相关（stage 1/2/3）**

| JSON 字段 | 默认 | 含义 |
| --- | --- | --- |
| `offload_optimizer` | `null` | 优化器状态/计算卸载到 CPU/NVMe，stage 1/2/3 均有效（L181） |
| `offload_param` | `null` | 参数卸载，仅 stage 3 有效（L174） |
| `round_robin_gradients` | `false` | stage 1/2 CPU 卸载时多 rank 轮流拷梯度（L319） |
| `cpu_offload` / `cpu_offload_param` / `cpu_offload_use_pin_memory` | — | 已废弃，自动迁移到上面的新字段（L198-L230） |

**stage 1/2 专属**

| JSON 字段 | 默认 | 含义 |
| --- | --- | --- |
| `ignore_unused_parameters` | `true` | 动态网络中未用参数是否容忍（仅 stage 2 使用，L304） |
| `legacy_stage1` | `false` | 旧版 stage 1，构造时直接抛异常拒绝（L313、engine.py L2682） |

**stage 3 专属（多数带 `stage3_` 别名）**

| JSON 字段 | JSON 别名 | 默认 | 含义 |
| --- | --- | --- | --- |
| `prefetch_bucket_size` | `stage3_prefetch_bucket_size` | `5e7` | 预取参数上限（L232） |
| `param_persistence_threshold` | `stage3_param_persistence_threshold` | `1e5` | 小于该值的参数不切分（L238） |
| `model_persistence_threshold` | `stage3_model_persistence_threshold` | `sys.maxsize` | 不切分参数总量上限（L245） |
| `max_live_parameters` | `stage3_max_live_parameters` | `1e9` | 驻留参数上限（L255） |
| `max_reuse_distance` | `stage3_max_reuse_distance` | `1e9` | 复用距离内不释放（L261） |
| `gather_16bit_weights_on_model_save` | `stage3_gather_16bit_weights_on_model_save` | `false` | 保存前收集 16 位权重（L267） |
| `module_granularity_threshold` | `stage3_module_granularity_threshold` | `0` | 细粒度模块整块取参（L275） |
| `use_all_reduce_for_fetch_params` | `stage3_use_all_reduce_for_fetch_params` | `false` | 取参用 all-reduce（L283） |
| `allgather_sequential` | `stage3_allgather_sequential` | `false` | 逐参数 all-gather、省拷贝与临时缓冲（L289） |
| `memory_efficient_linear` | — | `true` | stage 3 线性层省显存实现（L356） |
| `override_module_apply` | — | `true` | 覆盖 `nn.Module.apply`（L365） |
| `pipeline_loading_checkpoint` | — | `false` | 流水线式加载检查点（L363） |
| `save_muon_momentum_buffer_in_memory` | — | `false` | Muon 动量缓冲不换出到 NVMe（L380） |
| `leaf_module` | — | 见下 | 叶子模块（绕过 hook 安装）配置（L387） |

**嵌套子配置类**：`zenflow`（`ZenFlowConfig`，u9-l3 讲）、`leaf_module`（`DeepSpeedZeroLeafModuleConfig`：`classes`/`names`/`name_suffixes` 三个列表字段，默认包含 Mixtral/Qwen2-MoE/Qwen3-MoE 的 MoE 块类名，让这些模块作为整体取参）。

模型还带四个 `model_validator`（[deepspeed/runtime/zero/config.py:L393-L418](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py#L393-L418)）：

- `overlap_comm_valid`：未显式指定时默认 `stage == 3` 才开重叠；
- `compute_grad_norm_valid`：`compute_grad_norm=false` 仅允许 stage 1/2；
- `offload_ratio_check`：部分卸载（`ratio < 1.0`）仅允许 stage 3；
- `elastic_checkpoint_deprecated`：stage 3 + 弹性检查点打警告。

#### 4.3.4 代码实践

**实践：用一份 JSON 走完解析链并故意触发校验器**

1. 实践目标：确认字段表与默认值，并亲眼看一次配置期报错（而不是训练到一半才炸）。
2. 操作步骤（示例代码）：

   ```python
   from deepspeed.runtime.zero.config import get_zero_config

   cfg = get_zero_config({"zero_optimization": {
       "stage": 3,
       "reduce_bucket_size": int(2e8),
   }})
   print(cfg.overlap_comm)            # 未显式给值
   print(cfg.prefetch_bucket_size)    # stage3 别名对应的字段
   print(cfg.retain_gpu_grad_partition_until_step)  # 本周期新增字段

   # 触发 offload_ratio_check：部分卸载只允许 stage 3
   try:
       get_zero_config({"zero_optimization": {
           "stage": 2,
           "offload_optimizer": {"device": "cpu", "ratio": 0.5},
       }})
   except Exception as e:
       print(type(e).__name__, e)
   ```

3. 需要观察的现象：第一段应打印 stage 3 下 `overlap_comm` 的动态默认、`5e7` 的预取桶默认、`False` 的新字段默认；第二段应抛出断言错误。
4. 预期结果：`True / 50000000.0(或等价整数) / False`，以及一条 "Partial offloading only supported for ZeRO Stage 3." 的报错。（打印值依据源码默认值推导，待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：把 `reduce_bucket_size` 调大，显存和通信分别怎么变？
**答案**：桶更大意味着一次归约搬运更多元素，通信次数更少、吞吐更好，但归约期间桶本身占的临时显存更大（fp16 下 `5e8` 元素约 1 GB）。这是一个典型的「缓冲大小换通信效率」旋钮。

**练习 2**：为什么 `overlap_comm` 的默认值要靠 validator 动态决定，而不是直接写死？
**答案**：它取决于 `stage`（默认仅 stage 3 开启），而 pydantic 字段默认值在定义期拿不到其他字段的最终值，`model_validator(mode="after")` 在整个模型实例化完成后运行，才能基于 `stage` 补默认。

**练习 3**：JSON 里写 `"stage3_prefetch_bucket_size": 100` 与 `"prefetch_bucket_size": 100` 效果一样吗？
**答案**：一样。字段用 `alias="stage3_prefetch_bucket_size"` 声明了别名，两 种键名都指向同一字段（`Field(..., alias=...)`，见 L232）。

### 4.4 offload 参数结构

#### 4.4.1 概念说明

ZeRO 切分解决的是「卡内显存不够分」；**卸载（offload）** 解决的是「显存总量不够」——把优化器状态或参数搬到 CPU 内存（ZeRO-Offload）甚至 NVMe（ZeRO-Infinity），用时再搬回来。配置上是 `zero_optimization` 块里两个嵌套字典：`offload_optimizer`（stage 1/2/3 均可）与 `offload_param`（仅 stage 3）。

#### 4.4.2 核心流程

两个配置类共享一个设备枚举。卸载决策的粒度是「设备 + 路径 + 缓冲池」：

```
OffloadDeviceEnum: none / cpu / nvme
                     │
     ┌───────────────┴────────────────┐
offload_optimizer（stage 1/2/3）    offload_param（仅 stage 3）
  优化器状态与计算搬到 CPU             模型参数搬到 CPU/NVMe
  device=cpu → ZeRO-Offload           前向用到时按需搬回
  device=nvme → ZeRO-Infinity
```

优化器计算（如 Adam 的更新）始终在 CPU 上执行（`DeepSpeedCPUAdam`），与 `device` 指的是「状态存哪」是两回事——`device` 决定状态驻留的层级。

#### 4.4.3 源码精读

设备枚举只有三个值：

[deepspeed/runtime/zero/offload_config.py:L14-L18](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/offload_config.py#L14-L18)

```python
class OffloadDeviceEnum(str, Enum):
    """ Enum for valid offload devices """
    none = "none"
    cpu = "cpu"
    nvme = "nvme"
```

参数卸载配置（仅 stage 3 有效）：

[deepspeed/runtime/zero/offload_config.py:L21-L52](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/offload_config.py#L21-L52)

| JSON 字段 | 默认 | 含义 |
| --- | --- | --- |
| `device` | `"none"` | 卸载目标：`cpu` / `nvme` |
| `nvme_path` | `null` | NVMe 路径 |
| `buffer_count` | `5` | NVMe 缓冲池数量 |
| `buffer_size` | `1e8` | 每个缓冲的字节数（元素数） |
| `max_in_cpu` | `1e9` | NVMe 卸载时保留在 CPU 的元素数上限 |
| `pin_memory` | `true` | 页锁定主机内存，异步满带宽拷贝必需 |

优化器卸载配置（stage 1/2/3 有效）：

[deepspeed/runtime/zero/offload_config.py:L55-L112](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/offload_config.py#L55-L112)

| JSON 字段 | 默认 | 含义 |
| --- | --- | --- |
| `device` | `"none"` | 状态卸载目标 |
| `nvme_path` | `null` | NVMe 路径 |
| `buffer_count` | `4` | 状态缓冲数（Adam 有 4 种状态：参数/梯度/动量/方差） |
| `pin_memory` | `true` | pinned host memory |
| `pipeline_read` / `pipeline_write` | `false` | ZeRO-Infinity 的 tile 级读写流水 |
| `fast_init` | `false` | NVMe 卸载时快速优化器初始化 |
| `ratio` | `1.0` | 部分卸载比例，仅 stage 3（validator 强制） |
| `super_offload` | `false` | Superchip 高性能卸载，仅 stage 3 |
| `cpuadam_cores_perc` | `0.8` | CPU Adam 用的核比例，仅 stage 3 + super_offload |

注意 `pin_memory` 默认 `true` 的原因写在 docstring 里：pinned 内存无法被操作系统换出，异步满带宽 GPU↔CPU 拷贝与「卸载和计算重叠」都依赖它；只有主机 `ulimit -l` 很紧或内存吃紧时才考虑关掉。

引擎侧用两个访问器把配置透出（[deepspeed/runtime/engine.py:L1586-L1590](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1586-L1590)），随后作为 `offload_optimizer_config` / `offload_param_config` 传进优化器构造（见 4.2.3 的两段构造代码）。

#### 4.4.4 代码实践

**实践：拼出三种典型卸载配置（源码阅读型）**

1. 实践目标：把「ZeRO-Offload」「ZeRO-Infinity」「不卸载只切分」三种形态的 JSON 差异写清楚。
2. 操作步骤：基于 4.4.3 的两张字段表，分别写出：stage 2 + CPU 优化器卸载（`offload_optimizer: {device: cpu}`）、stage 3 + 双重卸载到 NVMe（`offload_optimizer` 与 `offload_param` 都指到 `nvme` + `nvme_path`）、stage 3 纯切分（两个 offload 块都不写）。用 `get_zero_config` 依次解析，打印 `cfg.offload_optimizer.device` 等字段确认。
3. 需要观察的现象：第三种配置下 `offload_optimizer`/`offload_param` 为 `None`；第一种为 `<OffloadDeviceEnum.cpu: 'cpu'>`。
4. 预期结果：三份可复用的 JSON 片段与解析输出（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `offload_param` 只在 stage 3 有效？
**答案**：参数卸载的前提是参数已经被切分（stage 3）；stage 1/2 每卡仍持有全量 16 位参数，没有「别人的分片」可卸，卸载自己的全量参数意味着每次前向都要整模型搬运，不可行。`offload_ratio_check` validator 也只对 stage 3 放开部分卸载。

**练习 2**：`offload_optimizer.device` 设为 `cpu` 和 `nvme` 的本质区别是什么？优化器计算在哪里跑？
**答案**：两者优化器计算都在 CPU 上跑；区别只在**状态驻留的存储层级**——`cpu` 是常驻主机内存（ZeRO-Offload），`nvme` 是常驻磁盘、按 tile 换入换出（ZeRO-Infinity，配合 `pipeline_read/write` 流水）。

**练习 3**：`buffer_count` 对优化器卸载为什么默认是 4？
**答案**：docstring 写明 Adam 维护 4 种状态（参数、梯度、动量、方差），缓冲池需要至少能同时容纳一个参数的全部状态，4 是与之匹配的最小默认。

### 4.5 本周期新增：retain_gpu_grad_partition_until_step

#### 4.5.1 概念说明

这是本周期（PR #9044，配合 #9018 的累积器重构）引入的新字段，也是本讲义此次更新的主因。它只作用于一条很具体的路径——**ZeRO-2 + CPU 优化器卸载 + fp32 梯度累积（紧凑分区）**：

- 16 位模型 + `data_types.grad_accum_dtype: "fp32"` 时，ZeRO-2 在 GPU 上为本卡分片维护一块 **fp32 紧凑梯度分区**（compact gradient partition），窗口内多次 backward 的梯度都累加在这里（`#9018` 引入，u5-l3 详讲）。
- **默认行为（`false`）**：每到请求边界，把紧凑分区的运行和拷贝到 host 梯度（fp32 分区张量的 `.grad`），然后**释放 GPU 上的这块分区**；下一个 backward 需要时再从 host 恢复。请求间 GPU 显存占用低。
- **置 `true`**：分区驻留 GPU 直到 `step()`。省掉每个请求的主机拷贝与恢复，代价是请求间显存多占一个 fp32 分区。
- 对不用紧凑分区的配置（例如梯度累积 dtype 与模型同精度、或 Muon 优化器组、或非 CPU 卸载）**完全无效**——配置 docstring 明确写了 "Has no effect on configurations that do not use the compact gradient partition"。

它面向的场景是 RLHF/推理式训练循环：一个 optimizer window 内有大量「请求边界」（每次 backward 都到边界，GAS=1），默认行为会造成高频的主机往返。

#### 4.5.2 核心流程

紧凑分区在一个累积窗口内的生命周期（省略非关键分支）：

```
backward（请求边界）
  └─ independent_gradient_partition_epilogue
       └─ (cpu_offload 且是边界) _offload_compact_grad_partitions
            1. 逐参数把紧凑分区运行和拷到 host 梯度（fp32→host dtype，检查溢出）
            2. 决定是否释放 GPU 分区，三个条件全过才释放：
               a. host dtype 能逐位恢复（_host_grad_holds_accum_exactly）
               b. 梯度安全机制开启（_offload_gradient_safety_enabled）
               c. retain_gpu_grad_partition_until_step == False   ← 新开关
               → 释放：averaged_gradients[group] = None，记入 _host_owned_compact_partitions
下一次 backward 需要分区
  └─ _get_compact_grad_partition_buffer
       └─ (group 在 _host_owned_compact_partitions 中) _restore_compact_grad_partition
            从 host 梯度逐参数拷回 GPU 分区
step()
  └─ 消费 averaged_gradients / host 梯度，更新权重
```

紧凑分区本身的启用条件（`_use_compact_grad_partition_buffer`）：梯度累积 dtype 为 fp32 且不等于模型 dtype（即 16 位模型），且该参数组不是 Muon 组。

#### 4.5.3 源码精读

配置字段的定义与语义说明（本周期 diff 中唯一新增的配置面变化）：

[deepspeed/runtime/zero/config.py:L150-L158](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py#L150-L158)

```python
retain_gpu_grad_partition_until_step: bool = False
"""
With ZeRO-2 CPU optimizer offload and fp32 gradient accumulation on a 16-bit model, the fp32
gradient partition is freed from GPU memory after each request boundary and restored from the host
gradient for the next backward. This is the default and keeps GPU memory use between requests low.
Set to ``true`` to keep the partition in GPU memory until ``step()``: GPU memory use between
requests rises by one fp32 partition per rank, and the per-request host copy and restore are avoided.
Has no effect on configurations that do not use the compact gradient partition.
"""
```

引擎侧访问器与传参点（stage 1/2 优化器构造的最后一个参数）：

[deepspeed/runtime/engine.py:L1645-L1646](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1645-L1646)

```python
def zero_retain_gpu_grad_partition_until_step(self):
    return self._config.zero_config.retain_gpu_grad_partition_until_step
```

[deepspeed/runtime/engine.py:L2735-L2740](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2735-L2740)

```python
                gradient_accumulation_dtype=gradient_accumulation_dtype,
                communication_data_type=self.communication_data_type,
                elastic_checkpoint=self.zero_elastic_checkpoint(),
                check_grad_overflow=check_grad_overflow,
                compute_grad_norm=self.zero_compute_grad_norm(),
                retain_gpu_grad_partition_until_step=self.zero_retain_gpu_grad_partition_until_step())
```

注意它只传给 stage 1/2 优化器——stage 3 分支没有这个参数，印证了「只影响 ZeRO-2 路径」。

释放判定在 `_offload_compact_grad_partitions`，三个 `continue` 就是三道闸门，最后一道就是新开关：

[deepspeed/runtime/zero/stage_1_and_2.py:L1392-L1402](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1392-L1402)

```python
# Free only when the host copy can restore the device sum bit for bit. Otherwise the
# device buffer stays resident until step(), and ZenFlow orders its own copies.
if not self._host_grad_holds_accum_exactly(host_grad.dtype, grad_group.flat_partition.dtype):
    continue
if not self._offload_gradient_safety_enabled:
    continue
if self.retain_gpu_grad_partition_until_step:
    continue
self._record_gradient_stream(grad_group.flat_partition, stream)
self.averaged_gradients[group_idx] = None
self._host_owned_compact_partitions.add(group_idx)
```

「逐位恢复」的判定是纯 dtype 精确比较（host 与设备同 dtype，或 fp32 host 持有从 bf16/fp16 加宽来的值）：

[deepspeed/runtime/zero/stage_1_and_2.py:L1308-L1314](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1308-L1314)

恢复路径：下次 backward 要用分区时发现它归 host 所有，从 host 梯度拷回：

[deepspeed/runtime/zero/stage_1_and_2.py:L1351-L1360](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1351-L1360)

```python
def _get_compact_grad_partition_buffer(self, group_idx):
    grad_group = self.averaged_gradients.get(group_idx)
    if grad_group is not None and getattr(grad_group, "flat_partition", None) is not None:
        return grad_group
    grad_group = self._build_compact_grad_partition_buffer(group_idx)
    if self.cpu_offload and group_idx in self._host_owned_compact_partitions:
        self._restore_compact_grad_partition(group_idx, grad_group)
    ...
```

而边界处的发布/释放动作挂在 backward 尾声的 `cpu_offload` 分支（[deepspeed/runtime/zero/stage_1_and_2.py:L1072-L1074](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1072-L1074)）——这也解释了为什么该开关只在 CPU 卸载下有意义：不卸载时根本没有「发布到 host」这一步。

开关行为的契约由测试固化（`_retain_option_run` 跑三次边界 backward + 一次 step，记录每次边界后 `averaged_gradients` 是否还在）：

[tests/unit/v1/zero/test_zero_offload_accum_precision.py:L200-L211](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero_offload_accum_precision.py#L200-L211)

```python
def test_release_option_controls_residency_without_changing_results(self):
    ...
    resident_released, err_released, w_released = _retain_option_run(False)
    resident_kept, err_kept, w_kept = _retain_option_run(True)
    assert resident_released == [False, False, False], ...  # 默认：每边界后都释放
    assert resident_kept == [True, True, True], ...         # 开关开：一直驻留
    assert err_kept == 0.0 and err_released == 0.0          # 两条路径梯度都精确
    assert torch.equal(w_released, w_kept)                  # 最终权重完全一致
```

#### 4.5.4 代码实践

**实践：亲测 retain 开关的显存驻留与数值等价**

1. 实践目标：验证「默认释放 / 置 true 驻留」两个行为，并确认开关不改变训练结果。
2. 操作步骤：直接运行上面的契约测试（无需自己写脚本）：

   ```bash
   pytest tests/unit/v1/zero/test_zero_offload_accum_precision.py::TestZeroOffloadReleaseOption -v
   ```

   若想自己观察，可仿照 `_retain_option_run`（[tests/unit/v1/zero/test_zero_offload_accum_precision.py:L171-L194](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero_offload_accum_precision.py#L171-L194)）写脚本，配置取自 `_config`（stage 2 + `offload_optimizer: {device: cpu}` + bf16 + `data_types.grad_accum_dtype: "fp32"`），在每次边界 backward 后打印 `engine.optimizer.averaged_gradients.get(0) is not None` 与 `torch.cuda.memory_allocated()`，再对 `retain=true/false` 各跑一遍，比较每步 backward 耗时与最终权重。
3. 需要观察的现象：默认（false）时每次边界后 `averaged_gradients` 变 `None`、请求间显存回落；true 时一直非 `None`、请求间显存高出一个 fp32 分区、省掉恢复拷贝；两组最终权重逐位相等。
4. 预期结果：与测试断言一致——`[False]*n` 对 `[True]*n`、误差为 0、`torch.equal` 成立。GPU 计时部分需要真实硬件，待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么释放前置了「host dtype 能逐位恢复」这道闸门？
**答案**：如果 host 梯度 dtype 装不下设备上的 fp32 累积和（比如 host 是 fp16 而和超范围），释放后再恢复就会丢精度甚至读到 inf。此时宁可让分区驻留 GPU 到 `step()`，也不做有损往返（判定函数只允许同 dtype 或 fp32 host 持 bf16/fp16 值两种情形）。

**练习 2**：一个 GAS=8 的常规视觉训练（每 8 个 micro-batch 才有一次边界）与一个 GAS=1 的 RLHF rollout 循环，谁更该开这个开关？
**答案**：GAS=1 的 rollout 循环。每个请求边界都会触发一次「发布+释放+恢复」，频率极高，省掉主机往返收益大；而 GAS=8 时窗口内只有最后一次到边界，往返频率低，多占一个 fp32 分区的显存反而不划算。

**练习 3**：把 `grad_accum_dtype` 改成与模型一致的 bf16，这个开关还有效吗？
**答案**：没有。紧凑分区的启用条件是「梯度累积 dtype 为 fp32 且不等于模型 dtype」（`_use_compact_grad_partition_buffer`），bf16 累积下根本不会创建紧凑分区，开关无从生效——这正是 docstring "Has no effect..." 的具体含义。

## 5. 综合实践

把本讲的三块知识（公式、配置、新开关）串成一个显存实测实验。**需要 GPU 与两卡环境，完整结果待本地验证；在纯 CPU 环境可只做第 1 步的公式推算。**

1. **推算**：取一个 `Ψ ≈ 1e9` 参数的模型（例如 `hidden=4096` 的堆叠 MLP），在 `N_d = 2` 下手算 stage 0/1/2/3 的每卡模型状态显存（对照 4.1.2 表格）。
2. **实测 stage 扫描**：写一个最小训练脚本（参照 u2-l1 的三步接入法），分别用 stage 0/1/2/3 四份配置各跑 3 步：

   ```python
   torch.cuda.reset_peak_memory_stats()
   # … 3 次 (forward → engine.backward(loss) → engine.step())
   print(stage, torch.cuda.max_memory_allocated() / 1024**3, "GiB")
   ```

   做成「stage | 公式预测 | 实测峰值 | 差值解释」四列对照表；差值主要来自激活值、bucket 缓冲（`reduce_bucket_size` 默认 `5e8` 元素）与 CUDA 上下文。
3. **retain 开关对照**：换用 4.5.4 的 ZeRO-2 + CPU 卸载 + fp32 累积配置，`retain_gpu_grad_partition_until_step` 分别为 `false`/`true`，各跑 5 个「边界 backward」，用 `torch.cuda.memory_allocated()` 画出每个边界后的显存轨迹，并对比每步 backward 平均耗时与最终权重。
4. **结论模板**：一句话总结你的机器上「每阶段省多少显存、retain 开关用多少显存换多少时间」，并指出与公式预测的偏差来源。

## 6. 本讲小结

- ZeRO 把每卡 \( 16\Psi \) 字节模型状态按「优化器状态（\( K\Psi \)）→ 梯度（\( 2\Psi \)）→ 参数（\( 2\Psi \)）」的顺序逐级除以 \( N_d \)，得到 stage 1/2/3；`ZeroStageEnum`（int 枚举）是这套语义的源码权威定义。
- stage 分发在 `_configure_zero_optimizer`：stage 0/1/2 共用 `DeepSpeedZeroOptimizer`（`partition_grads` 区分 1 与 2），stage 3 用 `DeepSpeedZeRoOffload`（无优化器）或 `DeepSpeedZeroOptimizer_Stage3`。
- `zero_optimization` 块的每个 JSON 字段都对应 `DeepSpeedZeroConfig` 的一个 pydantic 字段，字段 docstring 即文档；`offload_param`（仅 stage 3）与 `offload_optimizer`（stage 1/2/3）是两个嵌套卸载配置类。
- 本周期新增 `retain_gpu_grad_partition_until_step`（默认 `false`）：只影响 ZeRO-2 + CPU 优化器卸载 + fp32 梯度累积的紧凑分区路径——默认请求边界释放 GPU 分区、下次 backward 从 host 恢复；置 `true` 则驻留到 `step()`，以一个 fp32 分区的请求间显存换取主机拷贝/恢复的免除，且不改变训练结果（有契约测试固化）。
- 读 ZeRO 配置要以类定义为准，`ZERO_FORMAT` 示例字符串不是完整字段清单。

## 7. 下一步学习建议

- 下一讲 **u5-l2（zero.Init：参数从出生就切分）**：精读 `partition_parameters.py`，看 stage 3 的参数切分如何在模型构建期就完成。
- 之后 **u5-l3（ZeRO-1/2：梯度切分与 IPG 流水线）** 会展开本讲 4.5 背后的完整机制：IPG bucket、`#9018/#9044` 引入的按分区组主机梯度累计器、空闲梯度清零（`#8991`）。
- 想先补上下文时：显存公式的原始出处是 ZeRO 论文（arXiv:1910.02054）；`docs/_pages/zero.md` 与 `zero_optimization` 字段的 docstring 是配置面的官方参考。
- 本讲只点名未展开的 `stage3.py`、`parameter_offload.py`、`partitioned_param_coordinator.py` 分别由 u5-l5、u5-l6 精读。
