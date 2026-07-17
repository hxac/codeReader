# 加速器规格与可运行性判定 accelerator_spec.py

## 1. 本讲目标

本讲要回答一个核心问题：**当用户输入一个模型名时，OpenLLM 凭什么判断「这台机器跑得动 / 跑不动」？**

读完本讲，你应当能够：

- 说清 `ACCELERATOR_SPECS` 这张「加速器规格表」是如何把一个 GPU 别名（如 `nvidia-a100-80g`）翻译成显存规格的。
- 看懂 `get_local_machine_spec` 如何用 NVML（`pynvml`）枚举本机 GPU、处理 macOS / Windows / Linux 的平台差异，以及在计算能力不足时给出告警。
- 掌握 `can_run(bento, target)` 的打分公式，理解返回值「大于 0 即可运行」的约定，以及它如何驱动 `hello` 交互流程中的「打勾」与排序。
- 能手动构造 `BentoInfo` 与 `DeploymentTarget`，独立验证打分结果。

## 2. 前置知识

本讲建立在 [u2-l1](u2-l1-common-config-output.md)（`common.py` 的配置与上下文）和 [u2-l4](u2-l4-model-discovery.md)（`BentoInfo` 解析）之上，会直接用到其中定义的几个数据模型。先用通俗语言把它们串一遍：

- **Bento / `BentoInfo`**：一个「可运行的模型版本」。它的 `bento.yaml` 里声明了运行所需的资源（CPU、内存、GPU 数量与型号）和支持的平台。本讲只关心它的资源声明部分。
- **`DeploymentTarget`**：一个「运行目标」，可以理解为「一台机器的抽象」。它持有一组 `Accelerator`（GPU 列表）、一个 `platform`（`linux`/`macos`/`windows`）以及来源标记（`local` 或 `cloud`）。
- **`Accelerator`**：一块 GPU 的抽象，只有两个字段——`model`（型号名）与 `memory_size`（显存，单位 GB）。

一句话概括本讲的职责：`accelerator_spec.py` 负责「**把硬件世界翻译成分数**」——既翻译本地机器，也翻译云端实例，最后用一个 `float` 分数回答「这个 Bento 在这个目标上能不能跑、跑得多合身」。

> 名词小贴士：
> - **NVML**（NVIDIA Management Library）：NVIDIA 提供的查询显卡状态底层库；`pynvml` 是它的 Python 绑定，无需 CUDA 即可读取显存、型号、计算能力。
> - **计算能力（compute capability）**：NVIDIA 给每代 GPU 的一个版本号（如 `(7, 5)` 表示 Turing 架构）。vLLM 等推理后端通常要求 ≥ 7.5。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/openllm/accelerator_spec.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py) | 本讲主角：GPU 规格表、本机硬件探测、可运行性打分 |
| [src/openllm/common.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py) | 提供 `Accelerator`、`DeploymentTarget`、`BentoInfo` 数据模型与 `output` |

关键调用关系：

- `__main__.py` 的 `hello` 命令调用 `get_local_machine_spec()` 得到本机目标，再对每个 Bento 调 `can_run()` 决定是否打勾、如何排序。
- `model.py` 的 `ensure_bento` 用 `can_run() <= 0` 判断是否给出「资源不足」的黄色提醒。
- `cloud.py` 的 `get_cloud_machine_spec` 复用同一张 `ACCELERATOR_SPECS` 把云端实例类型也翻译成 `DeploymentTarget`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① 加速器规格表与 `Resource`；② 本地机器规格探测；③ `can_run` 打分算法。

### 4.1 加速器规格表与 Resource

#### 4.1.1 概念说明

模型在 `bento.yaml` 里只会用**字符串别名**声明自己需要什么 GPU，例如：

```yaml
resources:
  gpu: 2
  gpu_type: nvidia-a100-80g
```

但「`nvidia-a100-80g` 到底有多少显存」这件事，OpenLLM 自己得有一张「对照表」才能知道。这张表就是 `ACCELERATOR_SPECS`：它把一组标准化别名映射成 `Accelerator(model=..., memory_size=...)`。

与这张表配套的还有 `Resource`——一个用来**解析并承载 bento 资源声明**的小模型，它把 YAML 里的 `resources` 段落变成 Python 对象，并能把 `"80Gi"` 这样的字符串显存解析成浮点数。

#### 4.1.2 核心流程

1. 从 `bento_yaml['services'][0]['config']['resources']` 读出资源声明。
2. 用 `Resource(**resources)` 构造：其中 `memory` 字段经过 `parse_memory_string` 校验器，把 `"60Gi"` 之类的字符串转成 `60.0`。
3. 当需要判断 GPU 时，用 `resource_spec.gpu_type` 去 `ACCELERATOR_SPECS` 查表，拿到所需的显存规格。

#### 4.1.3 源码精读

先看显存字符串解析器，它用一条正则匹配以 `Gi` 结尾的字符串：

[src/openllm/accelerator_spec.py:11-18](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L11-L18) —— 把 `"60Gi"` 解析为 `60.0`，其余原样放行（交给 Pydantic 做标准浮点转换）。

再看 `Resource` 模型本身：

[src/openllm/accelerator_spec.py:21-32](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L21-L32) —— 定义了 `memory / cpu / gpu / gpu_type` 四个字段。其中 `memory` 用 `typing.Annotated[float, BeforeValidator(parse_memory_string)]`，让 YAML 里的字符串在赋值前先被 `parse_memory_string` 处理。`__hash__` 把四元组整体哈希（为后续缓存服务）；`__bool__` 判断「是否有任何一个字段非 `None`」。

> ⚠️ 一个值得注意的细节：`__bool__` 用的是 `value is not None`，而 `Resource` 的默认值是 `0.0 / 0 / 0 / ''`——**它们都不是 `None`**。所以一个用空字典构造的 `Resource()` 仍然是真值。这一点会直接影响 `can_run` 的分支走向（见 4.3.3）。

然后是本模块的核心——规格表：

[src/openllm/accelerator_spec.py:35-61](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L35-L61) —— `ACCELERATOR_SPECS` 是一个 `dict[str, Accelerator]`，键是标准化别名，值是 `Accelerator(model=展示名, memory_size=显存GB)`。

读这张表时注意几处设计：

- 同一款卡可能有多个别名：`nvidia-a100-80g` 与 `nvidia-a100-80gb` 都指向 `A100(80GB)`；而 `nvidia-tesla-a100` 指向 `A100(40GB)`（40GB 版本）。
- 覆盖从消费级（`gtx-1650` 4GB）到顶级（`blackwell-b100` 192GB）的常见型号。
- 它被两处复用：本地探测后做对比（`can_run`），以及 `cloud.py` 把云端实例类型翻译成 `Accelerator` 列表：

[src/openllm/cloud.py:197-201](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/cloud.py#L197-L201) —— 云端实例的 `gpu_type` 若在表中，就展开成对应数量的 `Accelerator`；否则给空列表。这让「本地机器」和「云端实例」最终都变成同一种 `DeploymentTarget`，可被同一个 `can_run` 评估。

最后，`Accelerator` 与 `DeploymentTarget` 定义在 `common.py`：

[src/openllm/common.py:293-306](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L293-L306) —— `Accelerator` 用显存大小来定义大小比较（`__gt__`/`__eq__`），`__repr__` 形如 `A100(80.0GB)`。

[src/openllm/common.py:309-328](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L309-L328) —— `DeploymentTarget` 持有 `accelerators` 列表与 `platform`；`__hash__` 只按 `source` 哈希（同源视为同一缓存键）；`accelerators_repr` 把 GPU 列表渲染成 `A100 x2` 这样的展示串，供 `hello` 表格使用。

#### 4.1.4 代码实践

**实践目标**：亲手查表，理解别名 → 显存的映射，并观察同卡多别名。

**操作步骤**（示例代码，可在装好 `openllm` 的环境里运行）：

```python
# 示例代码
from openllm.accelerator_spec import ACCELERATOR_SPECS, Resource

# 1) 查表：看几个常见型号的显存
for slug in ['nvidia-tesla-t4', 'nvidia-a100-80g', 'nvidia-a100-80gb', 'nvidia-tesla-a100']:
    print(slug, '->', ACCELERATOR_SPECS[slug])

# 2) 用 Resource 解析 bento 的资源声明，观察字符串显存被转换
r = Resource(memory='60Gi', cpu=4, gpu=2, gpu_type='nvidia-a100-80g')
print('memory 已被解析为:', r.memory, type(r.memory).__name__)
print('bool(Resource()):', bool(Resource()))   # 重点观察
```

**需要观察的现象**：

- `nvidia-a100-80g` 与 `nvidia-a100-80gb` 输出完全相同；`nvidia-tesla-a100` 显存只有 `40.0`。
- `r.memory` 是 `60.0`（`float`），证明 `parse_memory_string` 生效。
- `bool(Resource())` 为 `True`，印证 4.1.3 中关于 `__bool__` 的细节。

**预期结果**：四条查表输出 + `memory=60.0` + `bool(Resource())=True`。

> 若运行时报错（如 `openllm` 未安装），可只阅读源码并口述上述输出，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nvidia-a100-80g` 和 `nvidia-tesla-a100` 显存不同，但都叫 A100？
**答**：A100 有 40GB 和 80GB 两个物理版本。表里 `nvidia-a100-80g`/`nvidia-a100-80gb` 指 80GB 版本，`nvidia-tesla-a100` 指 40GB 版本，靠别名区分。

**练习 2**：`Resource(memory='60Gi')` 与 `Resource(memory=60.0)` 在 `can_run` 后续逻辑里有区别吗？
**答**：没有。`parse_memory_string` 会把 `'60Gi'` 归一成 `60.0`，两者得到的 `memory` 字段相等。实际上 `can_run` 当前只用到 `gpu` 与 `gpu_type`，并未直接使用 `memory` 字段。

---

### 4.2 本地机器规格探测

#### 4.2.1 概念说明

有了规格表还不够——我们还需要知道**当前这台机器到底装了什么 GPU**。`get_local_machine_spec()` 就是干这件事的：它调用 NVML 枚举本机所有 NVIDIA 显卡，把每块卡的型号与显存装进一个 `DeploymentTarget`，供后续 `can_run` 使用。

它有三个关键设计：

1. **平台分叉**：macOS 直接返回空加速器（因为 macOS 不支持 NVIDIA CUDA 驱动走 NVML 这条路）；Windows/Linux 才尝试 NVML。
2. **`lru_cache`**：探测有成本，整个进程里只做一次。
3. **优雅降级**：任何 NVML 异常都不致命——给出告警后返回空加速器列表，让流程继续（最多就是「本地跑不动，只能 deploy」）。

#### 4.2.2 核心流程

```
get_local_machine_spec()
├─ macOS?            → 返回 accelerators=[], platform='macos'
├─ 判定 platform     → 'windows' / 'linux' / NotImplementedError
├─ nvmlInit()
│  ├─ device_count = nvmlDeviceGetCount()
│  └─ for 每块卡:
│       ├─ 名称、显存 → 追加一个 Accelerator
│       │   （显存 = ceil(总字节数 / 1024**3)，向上取整到 GB）
│       └─ 计算能力 < (7,5) → 黄色告警「可能不支持」
├─ nvmlShutdown()
└─ 返回 DeploymentTarget(accelerators, source='local', platform)
   任何异常 → 黄色提示 + 红色错误(level=20) + 返回空 accelerators
```

显存换算用了一个小数学：NVML 返回的是字节数，要转成 GB 并向上取整，确保「16.0GB 卡」不会被舍入成 15.x 而误判资源不足：

\[
\text{memory\_size} = \left\lceil \frac{\text{total\_bytes}}{1024^{3}} \right\rceil
\]

#### 4.2.3 源码精读

[src/openllm/accelerator_spec.py:64-113](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L64-L113) —— 这是 `get_local_machine_spec` 的全部实现。逐段说明：

- 第 64 行 `@functools.lru_cache`（无参数）让本函数在进程内只执行一次；测试或脚本里可用 `get_local_machine_spec.cache_clear()` 清缓存重测。
- 第 66-67 行：macOS 直接短路，返回空加速器 + `platform='macos'`。这同时意味着：若某 Bento 的 `platforms` 标签只含 `linux`，则它在 macOS 上 `can_run` 必为 `0.0`。
- 第 76-84 行：`from pynvml import ...` 写在函数内部而非模块顶部——这是**惰性导入**，避免在没有 NVIDIA 驱动 / 未装 `pynvml` 的机器上 `import openllm` 就崩。
- 第 95 行：`math.ceil(int(memory_info.total) / 1024**3)` 把字节向上取整为 GB。
- 第 97-104 行：`nvmlDeviceGetCudaCompute_capability(handle)` 返回形如 `(7, 5)` 的元组；小于 `(7, 5)` 时调用 `output(..., style='yellow')` 告警，但不阻断。
- 第 107-113 行：`except Exception` 兜底——打印黄色提示与红色错误（`level=20`，只有 `--verbose` 时可见），然后返回空加速器。这正是「无 GPU 机器也能跑 `openllm hello`」的原因。

注意第 112 行的 `output(f'Error: {e}', style='red', level=20)`：根据 [u2-l1](u2-l1-common-config-output.md)，`output` 只在 `level <= VERBOSE_LEVEL.get()` 时打印，所以默认情况下你看不到这行错误，只有加 `--verbose`（`VERBOSE_LEVEL` 被设为 20）才会显示。

#### 4.2.4 代码实践

**实践目标**：在本机调用 `get_local_machine_spec()`，观察探测结果与降级行为。

**操作步骤**（示例代码）：

```python
# 示例代码
from openllm.accelerator_spec import get_local_machine_spec

target = get_local_machine_spec()
print('platform    :', target.platform)
print('source      :', target.source)
print('accelerators:', target.accelerators)
print('repr        :', target.accelerators_repr)
```

**需要观察的现象**：

- **有 NVIDIA GPU 的 Linux 机器**：`accelerators` 是非空列表，形如 `[A100(80.0GB), ...]`，`platform='linux'`。
- **无 GPU 的机器**：通常会触发 `except` 分支——默认不报错（因为错误是 `level=20`），`accelerators=[]`。若想看到降级提示，把 `VERBOSE_LEVEL` 调到 20：

```python
# 示例代码：观察降级分支的报错
from openllm.common import VERBOSE_LEVEL
VERBOSE_LEVEL.set(20)
get_local_machine_spec.cache_clear()      # 清缓存才能重跑
print(get_local_machine_spec())
```

- **macOS**：无需 NVML，直接返回 `platform='macos'`、空加速器。

**预期结果**：在有 GPU 机器上得到 GPU 列表；无 GPU 机器上得到空列表（加 verbose 后可见红色 `Error:` 与黄色提示）。

> 若你无法确定本机结果（如不确定是否装了驱动），请标注「待本地验证」，不要假装已运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pynvml` 的 `import` 写在函数体内，而不是文件顶部？
**答**：为了惰性导入。很多用户机器没有 NVIDIA 驱动或没装 `pynvml`，顶部导入会让 `import openllm` 直接失败；放进函数体后，只有真正调用 `get_local_machine_spec()` 时才会触发，且失败会被 `except` 捕获降级。

**练习 2**：为什么显存要用 `math.ceil` 向上取整而不是四舍五入？
**答**：为了保守。若一张「接近 16GB」的卡被舍入成 `15.x`，可能在 `can_run` 的显存比较中误判为不够。向上取整保证「物理 16GB 卡」至少被记为 `16.0`，避免因字节到 GB 的换算损失而冤枉硬件。

---

### 4.3 can_run 打分算法

#### 4.3.1 概念说明

`can_run(bento, target)` 是本模块的「最终裁决函数」。它返回一个 `float`，约定是：

- **返回 `0.0`** → 跑不动（平台不匹配，或 qualifying GPU 数量不够）。
- **返回 `> 0`** → 跑得动；数值越大表示「越合身」（机器越贴近模型需求）。

这个「是否大于 0」的布尔语义被 `hello` 命令直接用来打勾（见 [u1-l4](u1-l4-hello-interactive-flow.md)），而具体数值则被 `_select_target` 用来给云端实例排序——**优先选最贴身、最不浪费的那台**。

它同样带 `@functools.lru_cache(typed=True)`，因为 `hello` 流程里会对「每个 Bento × 每个目标」组合大量调用。

#### 4.3.2 核心流程

```
can_run(bento, target=None)
├─ target 为 None → 取 get_local_machine_spec()
├─ resource_spec = bento 的 resources 段落 → Resource(...)
├─ platforms = bento.labels.get('platforms', 'linux').split(',')
│
├─ target.platform 不在 platforms        → 0.0   # 平台不符
├─ (not resource_spec)                   → 0.5   # 无资源声明（见 4.3.3 注意点）
│
├─ 需要 GPU (resource_spec.gpu > 0):
│     required_gpu = ACCELERATOR_SPECS[gpu_type]
│     qualifying   = 显存 >= required_gpu.memory_size 的卡数
│     ├─ gpu > qualifying                → 0.0   # 够格的卡不够多
│     └─ 否则返回:
│           required_gpu.memory_size * gpu
│           / Σ(所有卡的 memory_size)
│
├─ 不需要 GPU 但 target 有加速器         → 0.01 / Σ(所有卡的显存)
└─ 不需要 GPU 且 target 无加速器         → 1.0
```

打分的「合身度」直觉是这样的：分子是「模型理想需要的总显存」，分母是「机器实际拥有的总显存」。于是越贴身的机器得分越接近 1，越「大材小用」的机器得分越低。

\[
\text{score} = \frac{\text{required\_gpu.memory\_size} \times \text{gpu\_count}}{\displaystyle\sum_{a \in \text{target.accelerators}} a.\text{memory\_size}}
\]

举例（模型需要 `1 × A100(80GB)`）：

| 目标机器 | 计算 | 得分 |
| --- | --- | --- |
| `1 × A100(80GB)` | `80×1 / 80` | `1.0`（最贴身） |
| `2 × A100(80GB)` | `80×1 / 160` | `0.5` |
| `1 × T4(16GB)` | qualifying=0 < 1 | `0.0`（跑不动） |

这就是 `_select_target` 把「刚好够用」的实例排在前面的原理——一种成本优化启发式。

#### 4.3.3 源码精读

[src/openllm/accelerator_spec.py:116-149](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L116-L149) —— `can_run` 全部逻辑。逐行说明：

- 第 116 行 `@functools.lru_cache(typed=True)`：按 `(bento, target)` 缓存结果。能缓存的前提是两者都可哈希——`BentoInfo.__hash__` 是 `md5(str(self.path))`（见 [common.py:167-169](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L167-L169)），`DeploymentTarget.__hash__` 是 `hash(self.source)`。
- 第 121-122 行：`target` 缺省时回退到本机规格。
- 第 124 行：从 `bento_yaml['services'][0]['config']` 读 `resources`，缺省给空字典，再用 `Resource(**{})` 构造。
- 第 126 行：`platforms` 缺省为 `'linux'`（与 `BentoInfo.platforms` 属性一致，见 [common.py:202-204](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L202-L204)）。
- 第 128-129 行：平台不符直接 `0.0`。这是 macOS 上 Linux-only 模型判 0 的根因。
- 第 132-133 行：`if not resource_spec: return 0.5`。
- 第 135-146 行：GPU 分支。第 137-139 行用列表推导统计「显存达标的卡数」（`qualifying`），第 140-141 行若 `gpu > qualifying` 返回 `0.0`，否则用上面的合身度公式算分。
- 第 147-148 行：模型不要 GPU，但机器有 GPU → 返回一个极小的正数 `0.01 / 总显存`，表示「能跑但不太合身」。
- 第 149 行：模型不要 GPU、机器也没 GPU（如纯 CPU）→ `1.0`。

> ⚠️ 关于 `return 0.5` 的可达性（承接 4.1.3 的观察）：`can_run` 第 124 行总是用 `Resource(**resources)` 构造 `resource_spec`，即使 `resources` 为空字典，得到的 `Resource()` 因默认值非 `None` 而为真值，于是 `not resource_spec` 为 `False`，**`return 0.5` 这一分支在常规路径下并不会被命中**。空资源声明的 Bento 实际会走到第 147/149 行（取决于机器有没有 GPU）。这点可以在 4.3.4 的实践中验证——它的存在更像是「语义上的兜底声明」。

#### 4.3.4 代码实践

**实践目标**：手动构造 `BentoInfo` 与 `DeploymentTarget`，验证 `can_run` 在三种场景下的返回值。

**操作步骤**（示例代码）。由于真正的 `BentoInfo` 需要磁盘上的 `bento.yaml`，这里用一个常用技巧——构造对象后直接往 `__dict__` 里塞 `bento_yaml`，绕过 `cached_property` 的文件读取：

```python
# 示例代码
import pathlib
from openllm.accelerator_spec import can_run, ACCELERATOR_SPECS
from openllm.common import BentoInfo, RepoInfo, DeploymentTarget

def fake_bento(gpu, gpu_type, platforms='linux'):
    # RepoInfo 字段全部必填，这里给占位值
    repo = RepoInfo(name='default', path=pathlib.Path('.'), url='', server='',
                    owner='', repo='', branch='')
    bento = BentoInfo(repo=repo, path=pathlib.Path('.'), alias='')
    # 绕过真实 bento.yaml 文件，直接注入 cached_property 的底层值
    bento.__dict__['bento_yaml'] = {
        'services': [{'config': {'resources': {'gpu': gpu, 'gpu_type': gpu_type}}}],
        'labels': {'platforms': platforms},
    }
    return bento

A100 = ACCELERATOR_SPECS['nvidia-a100-80g']
T4   = ACCELERATOR_SPECS['nvidia-tesla-t4']

# 场景 A：机器 1×A100，模型需要 1×A100 —— 期望 1.0（最贴身）
machine_a = DeploymentTarget(accelerators=[A100], platform='linux')
print('A:', can_run(fake_bento(1, 'nvidia-a100-80g'), machine_a))

# 场景 B：机器 2×A100，模型需要 1×A100 —— 期望 0.5（大材小用）
machine_b = DeploymentTarget(accelerators=[A100, A100], platform='linux')
print('B:', can_run(fake_bento(1, 'nvidia-a100-80g'), machine_b))

# 场景 C：机器只有 1×T4(16GB)，模型需要 A100(80GB) —— 期望 0.0（跑不动）
machine_c = DeploymentTarget(accelerators=[T4], platform='linux')
print('C:', can_run(fake_bento(1, 'nvidia-a100-80g'), machine_c))

# 场景 D：模型不要 GPU，机器是纯 CPU —— 期望 1.0
machine_d = DeploymentTarget(accelerators=[], platform='linux')
print('D:', can_run(fake_bento(0, ''), machine_d))
```

**需要观察的现象**：

- A=1.0、B=0.5、C=0.0、D=1.0，与 4.3.2 的表格与分析一致。
- 把任一 `machine` 的 `platform` 改成 `'macos'`，而 Bento 的 `platforms='linux'`，分数会立刻变成 `0.0`（平台门禁生效）。

**预期结果**：四个分数分别约为 `1.0 / 0.5 / 0.0 / 1.0`。

> 若想验证 4.3.3 中「`return 0.5` 不可达」的观察，可构造 `fake_bento(0, '')`（空资源）并在一台有 GPU 的机器上调用，观察它返回的是 `0.01/总显存`（走第 148 行）而非 `0.5`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `can_run` 在 GPU 分支的分母用的是「所有加速器的显存总和」，而不是「达标的加速器总和」？
**答**：这是为了让分数反映「机器整体的富余程度」——机器越大材小用（总显存越多），分数越低，从而在 `_select_target` 排序时优先挑出最贴身、最省钱的实例。注意它是先靠 `qualifying`（达标卡数）做门禁，再用总显存算合身度。

**练习 2**：一个模型声明 `gpu: 0`，在本机（有 1×T4 16GB）上 `can_run` 会返回多少？含义是什么？
**答**：返回 `0.01 / 16 = 0.000625`。它是正数，所以「能跑」（会被打勾）；但数值极小，表示「机器有 GPU 但模型用不上，不太合身」。如果机器没有 GPU，则返回 `1.0`（纯 CPU 跑 CPU 模型，最贴身）。

**练习 3**：`can_run` 用了 `lru_cache`，如果运行期间机器插上了一块新 GPU，结果会变吗？
**答**：不会立刻变。`get_local_machine_spec` 和 `can_run` 都被 `lru_cache` 缓存，进程内不会重新探测。需要调用 `get_local_machine_spec.cache_clear()` 和 `can_run.cache_clear()` 清掉缓存才会重算——这在 CLI 的一次运行内通常不是问题。

---

## 5. 综合实践

把三个模块串起来，做一个「**模拟一台机器并自检可运行性**」的小任务。

**任务**：写一个脚本，定义两台「虚拟机器」（一台 `2×A100`、一台 `1×T4`），并准备三个 Bento（分别需要 `2×A100`、`1×A100`、不要 GPU），打印出「Bento × 机器」的可运行性矩阵（分数与「可运行/不可运行」结论）。

**参考骨架**（示例代码，基于 4.3.4 的 `fake_bento`）：

```python
# 示例代码
machines = {
    '2xA100': DeploymentTarget(accelerators=[A100, A100], platform='linux'),
    '1xT4'  : DeploymentTarget(accelerators=[T4], platform='linux'),
}
models = {
    'big-llm (2xA100)': fake_bento(2, 'nvidia-a100-80g'),
    'mid-llm (1xA100)': fake_bento(1, 'nvidia-a100-80g'),
    'cpu-only (0 GPU)' : fake_bento(0, ''),
}

for mname, machine in machines.items():
    for bname, bento in models.items():
        score = can_run(bento, machine)
        verdict = '可运行' if score > 0 else '不可运行'
        print(f'{mname:8} × {bname:18} -> score={score:.4f}  {verdict}')
```

**完成后你应该能解释**：

1. `big-llm` 在 `1xT4` 上为什么是 `0.0`（qualifying 达标卡数为 0 < 2）。
2. `big-llm` 在 `2xA100` 上的分数为什么是 `1.0`（`80×2/160`）。
3. `cpu-only` 在两台机器上分别得到什么值，为什么不同（有 GPU 的机器给极小正数，无 GPU 给 1.0；但本例两台都有 GPU，所以都是 `0.01/总显存`）。

> 拓展（可选）：把 `1xT4` 机器的 `platform` 改成 `'macos'`，再让某个 Bento 的 `platforms='linux'`，验证平台门禁会让整列变 `0.0`。

## 6. 本讲小结

- `ACCELERATOR_SPECS` 是 OpenLLM 自带的「GPU 别名 → 显存规格」对照表，同时服务于本地探测对比与云端实例翻译，是 `accelerator_spec.py` 的数据基石。
- `Resource` 用 `BeforeValidator(parse_memory_string)` 把 `"60Gi"` 这类字符串显存解析为浮点数；其 `__bool__` 基于「字段非 None」判断，导致空 `Resource()` 仍为真值。
- `get_local_machine_spec` 用 NVML 枚举本机 GPU，显存按字节向上取整为 GB；macOS 短路、`pynvml` 惰性导入、`except` 兜底三者共同保证「无 GPU 也不崩」，并带 `lru_cache` 全进程只探测一次。
- `can_run` 返回 `float`：`0.0` 表示跑不动（平台不符或达标 GPU 数不足），`>0` 表示可运行，数值用「所需显存×数量 / 机器总显存」衡量合身度，越大越贴身。
- 「分数是否大于 0」是 `hello` 打勾与 `ensure_bento` 资源提醒的判定依据；具体数值是 `_select_target` 给云端实例排序（优先最省）的依据。
- 一个隐藏细节：`can_run` 的 `return 0.5`「无资源声明」分支在常规路径下因 `Resource()` 真值而不可达，空资源 Bento 实际走 GPU/非 GPU 后续分支。

## 7. 下一步学习建议

- 接下来读 [u2-l6](u2-l6-venv-management.md)「虚拟环境与依赖管理 `venv.py`」，看 `can_run` 判定「能跑」之后，OpenLLM 如何用 `uv` 为该 Bento 准备独立虚拟环境。
- 想看完整的本地运行链路，可跳到 [u3-l1](u3-l1-local-serve-run.md)「本地 serve 与 run 的完整链路 `local.py`」，理解 `get_local_machine_spec` / `can_run` 在真实 `serve`/`run` 命令中的位置。
- 对云端感兴趣可读 [u3-l2](u3-l2-cloud-deploy.md)「云端部署 `cloud.py`」，那里会展开 `get_cloud_machine_spec` 如何把实例类型变成 `DeploymentTarget` 并参与 `can_run` 排序。
- 建议同步翻看 `__main__.py` 中 `_select_bento_name` / `_select_target` 的源码，对照本讲的分数语义，理解「打勾」与「排序」两种用法。
