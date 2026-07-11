# FP8 KV Cache 量化

## 1. 本讲目标

本讲是第六单元「性能优化」里专门讲 **KV Cache 显存压缩** 的一篇。在 u4-l1 中我们已经知道：`MemoryManager` 拥有一块巨大的 `kv_buffer`，形状是 `(layer_num, size+1, 2*head_num, head_dim)`，每个 token 在每一层都要占 `2*head_num*head_dim` 个元素。长上下文、大并发下，这块缓冲区往往是显存第一大户。本讲解的就是「怎么把这块 buffer 的元素从 16 位（bf16/fp16）压成 8 位（FP8）」，从而在不改模型权重的前提下把 KV 显存砍掉一半。

学完后你应当能够：

- 说清 **FP8（`float8_e4m3fn`）** 的数值范围，以及「静态量化 / 离线校准」为什么是 LightLLM 的选择。
- 读懂 `FP8StaticPerTensorQuantMemManager` 与 `FP8StaticPerHeadQuantMemManager` 这两个量化内存管理器，理解它们如何**继承默认 `MemoryManager`、只换一个 `operator_class`** 就完成「写入时量化」。
- 解释 `--kv_quant_calibration_config_path` 指向的校准 JSON 文件长什么样、为什么要它、加载时做了哪些校验，以及在多卡（TP/DP）下全局 scale 如何被切分到每个 rank。
- 对比 `fp8kv_spt`（per-tensor，整张量一个 scale）与 `fp8kv_sph`（per-head，每个头一个 scale）在**精度与显存**上的取舍。

本讲依赖 u4-l1（KV Cache 内存管理）与 u3-l5（注意力后端），也为 u6-l4（多级 KV Cache）的「CPU/磁盘卸载」铺垫——因为量化后单 token 更小，能换出的上下文也更多。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **token 级 KV 管理**（u4-l1）：`kv_buffer` 是四维张量 `(layer_num, size+1, 2*head_num, head_dim)`；K 和 V 沿「head 维」拼在一起——前 `head_num` 个头是 K，后 `head_num` 个头是 V。分配器 `KvCacheAllocator.alloc/free` 只发**索引**，真正把 K/V 数据搬进 buffer 的是「搬运工」`operator`。
- **operator 策略模式**（u4-l1）：`MemoryManager` 在构造时执行 `self.operator = self.operator_class(self)`，默认 `operator_class = NormalMemOperator`。**子类只要改写 `operator_class`，就能改变「写入 KV」的行为**——这正是量化管理器的切入点。
- **「写后读」闭环**（u3-l3）：transformer 层模板里 `_post_cache_kv` 调 `mem_manager.operator.copy_kv_to_mem_manager(...)` 把新算出的 K/V 落池；之后注意力核再经 `get_att_input_params(layer_index)` 把 K/V 读回来做注意力。
- **注意力后端可选**（u3-l5）：注意力算子被抽象成可替换后端（fa3 / flashinfer / triton），其中 fa3、flashinfer **原生支持 FP8 KV**——也就是说，反量化（把 FP8 还原成高精度参与注意力）由注意力后端负责，内存管理器只管「按 FP8 存」。
- **FP8 是什么**：一种 8 位浮点格式。本讲用的是 `float8_e4m3fn`（1 位符号 + 4 位指数 + 3 位尾数），可表示范围是 \([-448, 448]\)，精度比 bf16 低、但比 INT8 这种定点数更贴近浮点分布。

> 关键直觉：量化 = 找一个 scale，把高精度数值 \(x\) 映射成 \(q = \mathrm{clamp}(x/s, -448, 448)\) 后用 FP8 存；用回来时近似还原为 \(x \approx q \cdot s\)。scale 越贴近数据的真实幅度，误差越小。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `lightllm/common/kv_cache_mem_manager/fp8_static_per_tensor_quant_mem_manager.py` | **per-tensor 量化管理器**（`fp8kv_spt`）。整层 K 一个 scale、V 一个 scale，每层共 2 个 scale。 |
| `lightllm/common/kv_cache_mem_manager/fp8_static_per_head_quant_mem_manager.py` | **per-head 量化管理器**（`fp8kv_sph`）。每个 KV 头独立一个 scale，每层共 `2*head_num` 个 scale。 |
| `lightllm/common/kv_cache_mem_manager/operator/fp8_quant.py` | **两个量化 operator**。`copy_kv_to_mem_manager` 调 Triton kernel 在写入时把 bf16 的 KV 量化成 FP8。 |
| `lightllm/common/kv_cache_mem_manager/operator/base.py` | operator 抽象基类 `BaseMemManagerOperator`，定义 `copy_kv_to_mem_manager` 等接口契约。 |
| `lightllm/common/kv_cache_mem_manager/mem_utils.py` | `select_mem_manager_class()` 按 `--llm_kv_type` 选管理器（u4-l1 已介绍，本讲聚焦 FP8 分支）。 |
| `lightllm/common/basemodel/triton_kernel/destindex_copy_kv_fp8.py` | **量化 Triton kernel** `destindex_copy_kv_fp8`，per-head 与 per-tensor 共用、用编译期常量切换分支。 |
| `lightllm/server/api_cli.py`、`lightllm/server/api_start.py` | `--llm_kv_type`、`--kv_quant_calibration_config_path` 两个启动参数及其启动期断言。 |

一句话概括分工：**管理器管「scale 从哪来、buffer 用什么 dtype」，operator 管「写入时怎么量化」，Triton kernel 干「逐元素除以 scale 再截断成 FP8」这件真正的活**。

## 4. 核心概念与源码讲解

### 4.1 FP8 量化与离线（静态）方案

#### 4.1.1 概念说明

把 KV 从 16 位压到 8 位，核心问题不是「能不能压」，而是「用多少个 scale」以及「scale 什么时候算」。

- **scale 的粒度**：对一段 KV 张量，可以用**一个 scale** 覆盖整张量（per-tensor），也可以给**每个注意力头**一个 scale（per-head）。粒度越细，每个 scale 越贴近该头的数据幅度、误差越小，但要存的 scale 也越多。
- **scale 的时机**：
  - *动态量化（dynamic）*：每次写入新 token 时，现场统计这段 KV 的最大绝对值再算 scale。精度好，但有运行时统计开销，且 scale 要随 KV 一起存、一起传。
  - *静态量化（static / 离线）*：**提前**用一批校准数据跑模型，离线统计出每层/每头的 scale，固化成一个 JSON 文件；推理时直接加载这组固定 scale。无运行时统计开销，scale 也不必和 KV 在线一起存——这正是 LightLLM 的选择。

源码里有一句注释把方案讲得很直白：

> 「这里用 uint8 存储量化后的 kv，方便兼容各种 torch 算子。fp8 量化目前采用离线方案，kv_buffer 不存储 scale」

也就是说：`kv_buffer` 里**只存量化后的 8 位 KV**，scale 单独放在 `self.scales` 里、不进 buffer。这也意味着——**没有校准文件就没有 scale，量化无从谈起**（这点会在 4.3 展开）。

#### 4.1.2 核心流程

量化的数学本质：

\[
q = \mathrm{clamp}\!\left(\frac{x}{s},\; q_{\min},\; q_{\max}\right) \quad\text{然后转 FP8}
\]

其中 \(q_{\min}=-448\)、\(q_{\max}=448\) 是 `float8_e4m3fn` 的范围。理想 scale 取数据最大绝对值除以 \(q_{\max}\)，使最大值正好顶满：

\[
s = \frac{\max|x|}{448}
\]

这样数值动态范围被充分用满、相对误差最小。校准文件里的 `scales` 字段，就是离线统计出的这组 \(s\)。

写入时量化的执行流程（一次 `copy_kv_to_mem_manager`）：

1. 拿到本批新算出的 bf16 KV（形状 `(seq_len, 2*head_num, head_dim)`）和它们要写入的目标槽位索引 `mem_index`。
2. 取出本层的 scale（per-tensor 是长度 2 的向量 `[s_K, s_V]`；per-head 是长度 `2*head_num` 的向量）。
3. 对每个元素：除以对应 scale → 截断到 \([-448,448]\) → 转成 FP8。
4. 按 `mem_index` 散写到 `kv_buffer[layer]` 的对应 token 槽位（`kv_buffer` 以 `uint8` 存储，写入时 `.view(torch.float8_e4m3fn)`）。

#### 4.1.3 源码精读

真正的量化发生在 Triton kernel 里。它被 per-head 与 per-tensor 两种 operator 共用，用编译期常量 `IS_PER_TENSOR_QUANT` 切换「scale 怎么取」：

[destindex_copy_kv_fp8.py:7-49](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/destindex_copy_kv_fp8.py#L7-L49) — 量化 kernel 主体。下面是关键两段。

per-head 分支：每个头读一个 scale（`scale + offs_h`，每个头一个）：

```python
if not IS_PER_TENSOR_QUANT:
    scale_ptrs = scale + offs_h
    scales = tl.load(scale_ptrs, mask=offs_h < head_num, other=1.0)
```

per-tensor 分支：K 的所有头共享下标 0、V 的所有头共享下标 1（注意这里的 `head_num` 是「K 头数 + V 头数」，所以 `head_num//2` 把 K、V 分开）：

```python
else:
    # k, v 各一个 scale
    scale_ptrs = scale + tl.where(offs_h < head_num // 2, 0, 1)
    scales = tl.load(scale_ptrs)
```

统一的两步运算——「除以 scale」再「截断并转 FP8」：

```python
kv = tl.load(kv_ptrs, mask=offs_h[:, None] < head_num, other=0.0)
kv_scale = kv / scales[:, None]
kv_fp8 = tl.clamp(kv_scale, min=FP8_MIN, max=FP8_MAX).to(tl.float8e4nv)
```

> 注意 `kv / scales[:, None]`：除以 scale 是把高精度数值「缩进」\([-448,448]\) 窗口；反量化时由注意力后端做「乘回 scale」。

kernel 文件末尾自带一个 `__main__` 自测，可以当作「最简示例」读——它构造一个随机 KV、用 `amax/448` 现场算 scale、量化后再反量化对比，断言 `allclose`，正好演示了 4.1.2 的公式：

[destindex_copy_kv_fp8.py:88-106](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/destindex_copy_kv_fp8.py#L88-L106) — 自测里 `scale = kv.abs().amax(dim=(0,2)) / 448` 就是 \(s=\max|x|/448\)。

#### 4.1.4 代码实践（源码阅读 + 手算）

这是一个**源码阅读型实践**，无需 GPU：

1. **实践目标**：用纸笔验证 kernel 的量化数学。
2. **操作步骤**：
   - 打开 [destindex_copy_kv_fp8.py:88-106](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/destindex_copy_kv_fp8.py#L88-L106) 的自测段。
   - 假设某头某维度的最大绝对值是 `224`，按公式手算 scale = `224/448 = 0.5`。
   - 取一个值 `x = 100`，手算 `q = clamp(100/0.5, -448, 448) = 200`，反量化得 `200*0.5 = 100`，无误差。
   - 再取 `x = 300`（超出该头动态范围会如何？scale 是按 `max|x|` 算的，正常不会出现，但若推理时遇到比校准集更大的激活，`clamp` 会把它压到 448，反量化只得 `448*0.5=224`，发生截断误差）。
3. **需要观察的现象**：scale 越大（校准集动态范围越大），数值越远离 448、量化越稀疏、相对误差越大；scale 越小且贴近真实幅度，精度越好。这正是「校准集要尽量贴近真实业务分布」的根因。
4. **预期结果**：理解为什么静态量化高度依赖校准数据的质量——校准集偏离真实分布，scale 就偏，精度直接掉。

#### 4.1.5 小练习与答案

**练习 1**：为什么 kernel 用 `kv / scales`（除）而不是 `kv * scales`（乘）？
> **答**：scale \(s=\max|x|/448\) 通常是一个小于 1 的数（因为典型 KV 幅度远小于 448）。量化要把高精度数值「压缩」进 \([-448,448]\) 窗口，所以除以一个偏小的 scale 使结果落入窗口；反量化时才乘回 scale。若写成乘，数值会被放大到远超 FP8 范围、几乎全部被 `clamp` 成 448，信息全丢。

**练习 2**：`float8_e4m3fn` 的 `qmin/qmax` 分别是多少？代码里如何获取？
> **答**：`-448 / 448`。代码用 `torch.finfo(torch.float8_e4m3fn).min/.max` 获取（见两个管理器的 `self.qmin/self.qmax`），并把它作为编译期常量 `FP8_MIN/FP8_MAX` 传进 kernel。

---

### 4.2 量化内存管理器（替换默认 MemoryManager）

#### 4.2.1 概念说明

u4-l1 讲过，`MemoryManager` 用「**换 operator_class**」的策略模式来支持不同 KV 存储方式，默认 `operator_class = NormalMemOperator`（搬运 bf16/fp16 KV、不量化）。两个 FP8 管理器**几乎不重写父类逻辑**，只做三件事：

1. 把 `operator_class` 换成自己的量化 operator；
2. 强制 `kv_buffer` 的 dtype 为 `torch.uint8`（8 位存储）；
3. 准备好 `self.scales`（从校准文件加载，或退化成全 1）。

`NormalMemOperator` 写 KV 时调的是「不量化的搬运 kernel」`destindex_copy_kv`；量化 operator 写 KV 时调「量化 kernel」`destindex_copy_kv_fp8`。**整个 `kv_buffer` 的形状、分配器、`alloc/free` 全部复用父类**——所以 u4-l1 学的 token 级分配、RadixCache 复用索引等机制，在 FP8 模式下完全不变，变的只是「每个槽位里存的元素从 16 位变成了 8 位」。这正是 LightLLM 设计的优雅之处。

#### 4.2.2 核心流程

**选型**发生在 `select_mem_manager_class()`（u4-l1 已介绍全貌），FP8 相关分支：

```python
elif get_env_start_args().llm_kv_type == "fp8kv_sph":
    memory_manager_class = FP8StaticPerHeadQuantMemManager
elif get_env_start_args().llm_kv_type == "fp8kv_spt":
    memory_manager_class = FP8StaticPerTensorQuantMemManager
```

即：启动参数 `--llm_kv_type fp8kv_sph` → per-head 管理器；`fp8kv_spt` → per-tensor 管理器。

**写入路径**（量化发生的时机）仍走 u3-3 的「写后读」闭环：

1. transformer 层算出新 K/V；
2. 模板 `_post_cache_kv` 调 `mem_manager.operator.copy_kv_to_mem_manager(layer_index, mem_index, kv)`；
3. 此时 `operator` 已是量化 operator，于是 KV 在落池瞬间被量化成 FP8；
4. 之后注意力后端（fa3/flashinfer）读 `get_att_input_params(layer_index)` 返回的（K, V）视图 + `self.scales`，做 FP8 注意力并内部反量化。

**读取出口** `get_att_input_params` 两个管理器都覆写了一版（其实和父类完全相同，列出来是为了强调「读回来的就是 FP8 视图」）：

```python
def get_att_input_params(self, layer_index: int) -> Tuple[Any, Any]:
    k = self.kv_buffer[layer_index][:, : self.head_num, :]
    v = self.kv_buffer[layer_index][:, self.head_num :, :]
    return k, v
```

#### 4.2.3 源码精读

先看 **per-tensor 管理器**。注意第 21 行强制 `torch.uint8`、第 33/35 行 scale 的两条来源（有校准 / 无校准）：

[fp8_static_per_tensor_quant_mem_manager.py:15-38](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_tensor_quant_mem_manager.py#L15-L38) — 类定义与构造。要点：

```python
class FP8StaticPerTensorQuantMemManager(MemoryManager):
    operator_class = FP8StaticPerTensorQuantMemOperator   # 只换这一个

    def __init__(self, size, dtype, head_num, head_dim, layer_num, always_copy=False, mem_fraction=0.9):
        # 强制用 uint8 存量化后的 kv；fp8 离线方案，kv_buffer 不存 scale
        super().__init__(size, torch.uint8, head_num, head_dim, layer_num, always_copy, mem_fraction)
        self.qmax = torch.finfo(torch.float8_e4m3fn).max
        self.qmin = torch.finfo(torch.float8_e4m3fn).min
        ...
        if get_env_start_args().kv_quant_calibration_config_path is not None:
            cfg = self._load_and_check_config()
            self.scales = torch.tensor(cfg["scales"], ...).view(cfg["scales_shape"])   # [layer_num, 2]
        else:
            self.scales = torch.ones((self.kv_buffer.shape[0], 2), ...)                # 退化：全 1
        self.cpu_scales = self.scales.detach().cpu().numpy()
```

> `scales_shape` 为 `[layer_num, 2]`——每层两个数：K 的 scale 与 V 的 scale。`cpu_scales` 是它的 CPU numpy 副本，便于某些注意力后端序列化/读取。

再看 **per-head 管理器**，构造逻辑结构相同，区别只在 scale 的形状与多卡切分（第 34-49 行）：

[fp8_static_per_head_quant_mem_manager.py:16-50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_head_quant_mem_manager.py#L16-L50) — per-head 构造。无校准时退化成 `ones((layer_num, 2*head_num))`。

接着看两个 **operator**，它们都只是「把量化 kernel 套进 `copy_kv_to_mem_manager`」的薄壳，区别仅是 `is_per_tensor_quant` 这个开关：

[operator/fp8_quant.py:11-48](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/operator/fp8_quant.py#L11-L48) — 两个量化 operator：

```python
class FP8StaticPerHeadQuantMemOperator(BaseMemManagerOperator):
    def copy_kv_to_mem_manager(self, layer_index, mem_index, kv):
        ...
        destindex_copy_kv_fp8(kv, mem_index, scales[layer_index],
                              mem_manager.kv_buffer[layer_index].view(torch.float8_e4m3fn))   # 默认 per-head

class FP8StaticPerTensorQuantMemOperator(BaseMemManagerOperator):
    def copy_kv_to_mem_manager(self, layer_index, mem_index, kv):
        ...
        destindex_copy_kv_fp8(kv, mem_index, mem_manager.scales[layer_index],
                              mem_manager.kv_buffer[layer_index].view(torch.float8_e4m3fn),
                              is_per_tensor_quant=True)                                     # 切 per-tensor 分支
```

> 对比默认搬运工 [operator/normal.py:16-25](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/operator/normal.py#L16-L25)：它调的是 `destindex_copy_kv(kv, mem_index, kv_buffer[layer])`——同样的「散写到指定槽位」，但不量化。把这三段放在一起，就能看清「换 operator = 换搬运方式」的策略全貌。

最后看 operator 的接口契约在抽象基类里只定义了一个必须实现的 `copy_kv_to_mem_manager`，其余（CPU cache 卸载、跨 DP 拷贝）默认 `NotImplementedError`：

[operator/base.py:10-17](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/operator/base.py#L10-L17) — `BaseMemManagerOperator` 的抽象方法。

#### 4.2.4 代码实践（跟踪调用链）

1. **实践目标**：确认「换 operator_class 后，整条写入链路只换了一个 kernel 调用」。
2. **操作步骤**：
   - 在 [mem_manager.py:27](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L27) 看到默认 `operator_class = NormalMemOperator`，在 [mem_manager.py:51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L51) 看到 `self.operator = self.operator_class(self)`——这是多态挂载点。
   - 在 [transformer_layer_infer_template.py:35-42](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L35-L42) 看到写入侧 `_post_cache_kv` 调 `mem_manager.operator.copy_kv_to_mem_manager(...)`。
   - 顺着 `select_mem_manager_class()` 的 FP8 分支 [mem_utils.py:49-52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_utils.py#L49-L52) 切到 FP8 管理器后，再回到 [operator/fp8_quant.py:11-48](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/operator/fp8_quant.py#L11-L48)，确认唯一的差别就是 `destindex_copy_kv` → `destindex_copy_kv_fp8`。
3. **需要观察的现象**：从「bf16 直存」切到「FP8 量化存」，模型层、分配器、RadixCache 都没改一行。
4. **预期结果**：理解「量化是内存管理器的局部替换，对上层透明」这一架构结论。它也意味着 FP8 KV 同样能进 RadixCache 复用、能被多级缓存卸载（u6-l4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么两个 FP8 管理器都不重写 `alloc`/`free`？
> **答**：因为量化只改变「槽位里存什么」（8 位 FP8 而非 16 位），不改变「槽位怎么分配」。分配器只发索引、与 dtype 无关；`kv_buffer` 形状 `(layer_num, size+1, 2*head_num, head_dim)` 也不变，只是每个元素从 2 字节变 1 字节。所以 u4-l1 的分配回收逻辑原样复用。

**练习 2**：`kv_buffer` 用 `torch.uint8` 存，但 kernel 里又 `.view(torch.float8_e4m3fn)`——为什么这么绕？
> **答**：`uint8` 和 `float8_e4m3fn` 都是 8 位，底层字节完全相同，`.view` 是零拷贝的重新解释。用 `uint8` 存储是为了「兼容各种 torch 算子」（部分算子/序列化路径对 fp8 dtype 支持不全），真正做运算时再 view 成 fp8。

---

### 4.3 校准配置（kv_quant_calibration_config_path）

#### 4.3.1 概念说明

静态量化必须事先有 scale，scale 来自**校准文件**。LightLLM 用 `--kv_quant_calibration_config_path` 指向一个 JSON，里面记录了离线用校准数据跑模型统计出的每层/每头 scale。这就是为什么启动 FP8 推理时这个参数是**必填**的——没有它，管理器只能把 scale 退化成全 1（`torch.ones(...)`），而「除以 1」意味着数值几乎全部被 `clamp` 到 448，精度会塌掉。所以全 1 分支只是占位，**真正可用必须配校准文件**。

校准文件可以从三处获得（见官方文档 [docs/EN/source/tutorial/fp8_kv_quantization.rst](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/tutorial/fp8_kv_quantization.rst)）：

- 仓库自带：`test/advanced_config/fp8_calibration_per_head/` 与 `.../per_tensor/` 下有 Qwen2.5、Qwen3 等的现成文件；
- 用 [LightCompress](https://github.com/ModelTC/LightCompress) 工具自行导出；
- 自己生成兼容格式的文件。

#### 4.3.2 核心流程

校准文件的关键字段（以仓库自带的 Qwen2.5-14B per-tensor 文件为例）：

```json
{
  "version": "1.0",
  "architectures": "Qwen2ForCausalLM",
  "quant_type": "per_tensor",        // 或 "per_head"
  "qmin": -448.0, "qmax": 448.0,
  "num_layers": 48,
  "num_head": 8,                     // 校准时的总 KV 头数（全局）
  "scales_shape": [48, 2],           // per_tensor 是 [layers, 2]；per_head 是 [layers, 2*head_num]
  "scales": [[0.0574768, 0.0129743], ...]
}
```

加载与校验流程（`_load_and_check_config`）：

1. 读 JSON；
2. **四道校验**：`qmin/qmax` 是否等于 `float8_e4m3fn` 的范围；`architectures` 是否与当前 `--model_dir` 的 config 一致；`num_layers` 是否与模型实际层数一致；`quant_type` 是否与所用管理器匹配（per-tensor 管理器要求 `"per_tensor"`、per-head 要求 `"per_head"`）；
3. 把 `scales` 按 `scales_shape` 还原成张量；
4. （per-head 额外步骤）按 TP/DP 把全局 scale 切分到当前 rank。

启动期还有一道总闸门在 `api_start.py`：

```python
# FP8 KV cache mode checks
if args.llm_kv_type in ["fp8kv_sph", "fp8kv_spt"]:
    assert (
        args.kv_quant_calibration_config_path is not None
    ), "fp8kv inference mode requires --kv_quant_calibration_config_path. "
```

即：选了 FP8 模式却没给校准文件，**直接启动失败**，而不是带着全 1 scale 勉强跑。

#### 4.3.3 源码精读

先看 per-tensor 的加载与校验（逻辑最简，scale 不需要切分）：

[fp8_static_per_tensor_quant_mem_manager.py:40-65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_tensor_quant_mem_manager.py#L40-L65) — `_load_and_check_config`：四道校验里最后一道断言 `quant_type == "per_tensor"`。

[fp8_static_per_tensor_quant_mem_manager.py:27-37](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_tensor_quant_mem_manager.py#L27-L37) — scale 加载：`self.scales = torch.tensor(cfg["scales"]).view(cfg["scales_shape"])`，形状 `[layer_num, 2]`。

per-head 的校验同构（断言换成 `"per_head"`）：

[fp8_static_per_head_quant_mem_manager.py:52-77](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_head_quant_mem_manager.py#L52-L77) — per-head 的 `_load_and_check_config`。

**重点看 per-head 的多卡切分**——这是 per-head 比 per-tensor 复杂的地方。校准文件给的是**全局**所有 KV 头的 scale，但每个 rank 只持有其中一段头（TP 切分），GQA 场景下头还会被复制，所以需要「对齐 + 切片」：

[fp8_static_per_head_quant_mem_manager.py:33-50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_head_quant_mem_manager.py#L33-L50) — per-head scale 的切分。关键几行：

```python
all_head_num = cfg["num_head"]                                    # 校准文件里的全局头数
all_scales  = torch.tensor(cfg["scales"], ...).view(cfg["scales_shape"])

factor = (get_dp_world_size() * head_num) // all_head_num         # 处理 GQA 下头被复制的情况
all_scales = torch.repeat_interleave(input=all_scales, repeats=factor, dim=-1)
rank_in_dp = get_current_rank_in_dp()

v_offset = all_scales.shape[1] // 2                               # 前一半是 K，后一半是 V
start_head = rank_in_dp * head_num
end_head   = start_head + head_num
k_scales = all_scales[:, start_head:end_head].contiguous()
v_scales = all_scales[:, v_offset + start_head : v_offset + end_head].contiguous()
self.scales = torch.cat((k_scales, v_scales), dim=-1)             # 重新拼成 [layer_num, 2*head_num]
```

> 解读：
> - `get_dp_world_size()` 返回的是「一个模型副本内的 TP 规模」（= 全局 world size / DP 数，详见 `dist_utils.get_dp_world_size`）。`dp_world_size * head_num` = 本模型副本持有的 KV 头总数，它应 ≥ 校准的全局 `all_head_num`；`factor` 是 GQA 在 TP 下头被复制时的向上倍数（不复制时为 1）。
> - 校准文件把所有 K 的 scale 放在前半段、所有 V 的 scale 放在后半段，用 `v_offset` 分开。
> - `start_head/end_head` 按 `rank_in_dp`（本 rank 在副本内的序号）切出自己负责的连续 `head_num` 个头。
> - 最后把本 rank 的 K scale 与 V scale 重新拼回 `[layer_num, 2*head_num]`，与 `kv_buffer` 的「K 头在前、V 头在后」布局对齐。

per-tensor 为什么不用切分？因为它的 scale 只有 `[layer_num, 2]`（每层 K、V 各一个），与头数、与 TP 都无关，每个 rank 直接整份用即可。这也是两种模式实现复杂度的主要差别所在。

启动参数与启动校验：

[api_cli.py:416-432](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L416-L432) — `--llm_kv_type` 定义，choices 含 `fp8kv_sph`/`fp8kv_spt`/`fp8kv_dsa`，help 文本里就写明「`fp8kv_sph` 和 `fp8kv_spt` 需要 `--kv_quant_calibration_config_path`」。

[api_cli.py:721-726](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L721-L726) — `--kv_quant_calibration_config_path` 定义，默认 `None`。

[api_start.py:185-189](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L185-L189) — 启动期强制断言：FP8 模式必须有校准路径。

#### 4.3.4 代码实践（本讲主实践：为何要校准 + 两种模式取舍）

这是本讲的核心实践，包含**源码阅读分析**与（可选）**真机跑通**两部分。

**Part A — 源码阅读分析（必做，无需 GPU）**

1. **实践目标**：说清 FP8 为何依赖校准配置，并对比 per-tensor 与 per-head 的取舍。
2. **操作步骤**：
   - 阅读 [fp8_static_per_tensor_quant_mem_manager.py:27-37](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_tensor_quant_mem_manager.py#L27-L37) 与 [fp8_static_per_head_quant_mem_manager.py:28-50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_head_quant_mem_manager.py#L28-L50)，对比「有校准」与「无校准（全 1）」两个分支。
   - 打开两个示例校准文件，对比 scale 数量：
     - `test/advanced_config/fp8_calibration_per_tensor/test_kv_cache_calib_per_tensor_qwen2.5_14b.json`（`scales_shape [48, 2]`，共 96 个 scale）
     - `test/advanced_config/fp8_calibration_per_head/test_kv_cache_calib_per_head_qwen2.5_14b.json`（`scales_shape [48, 16]`，共 768 个 scale）
   - 结合 4.1.2 的公式，思考「全 1 scale」会让量化发生什么。
3. **需要观察的现象 / 预期结果**（请自己用一段话写出，对照下面的参考答案）：
   - **为何必须校准**：静态量化的 scale 必须离线统计；不给校准文件，scale 退化为全 1，`x/1` 后几乎所有真实 KV（幅度通常远小于 448）会被 `clamp`/极度稀疏化，精度崩坏；且 [api_start.py:185-189](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L185-L189) 会直接拦下不让启动。
   - **精度取舍**：per-head 每头独立 scale，更贴合各头实际幅度 → 精度更好；per-tensor 整层 K/V 共用一个 scale，必须迁就「最大幅度的那个头」，其余头动态范围被压缩 → 精度略差。
   - **显存取舍**：两者 `kv_buffer` 都是 uint8（8 位），**KV 主体的显存节省完全相同**（相对 bf16 省 50%）。差别只在 scale 侧开销：per-tensor 每层 2 个 fp32 scale，per-head 每层 `2*head_num` 个。以 48 层、8 个 KV 头计，per-head 的 scale 多占 `(768-96)*4 ≈ 2.7 KB`——相比动辄数 GB 的 `kv_buffer` 可忽略不计。所以**实践中显存差异微乎其微，per-head 几乎是「精度更好、显存代价可忽略」的选择**；per-tensor 的真正吸引力在于它配套的 flashinfer 后端在某些硬件/库版本下更快。
4. **如果无法确定运行结果**：Part A 的结论可纯靠源码阅读得出；Part B 的精度数字「待本地验证」。

**Part B — 真机跑通（可选，需 GPU + 校准文件 + lm_eval）**

仓库已经备好对照测试脚本，直接复现 per-head / per-tensor 的精度差异：

- per-head：[test/acc/test_qwen2.5_fp8kv_sph.sh](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/test/acc/test_qwen2.5_fp8kv_sph.sh)
- per-tensor：[test/acc/test_qwen2.5_fp8kv_spt.sh](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/test/acc/test_qwen2.5_fp8kv_spt.sh)

启动命令形如（以 per-head 为例）：

```bash
python -m lightllm.server.api_server \
    --model_dir /path/to/Qwen2.5-14B-Instruct \
    --tp 2 --port 8089 \
    --llm_kv_type fp8kv_sph \
    --kv_quant_calibration_config_path ./test/advanced_config/fp8_calibration_per_head/test_kv_cache_calib_per_head_qwen2.5_14b.json
```

随后用 `lm_eval` 跑 gsm8k（脚本第二步），记录两种模式的 accuracy，对比 per-head 是否如预期略高于 per-tensor。注意官方文档提醒：`fp8kv_spt` 需 `pip install flashinfer-python==0.6.5`（默认 0.6.3 可能有运行时问题）。

> 待本地验证：实际精度差值随模型与任务而变，文档未给出固定数字，需自行跑出。

#### 4.3.5 小练习与答案

**练习 1**：若把一个 `per_tensor` 校准文件误喂给 `fp8kv_sph`（per-head）模式，会在哪一步、以什么报错失败？
> **答**：在 `_load_and_check_config` 的最后一道断言失败，[fp8_static_per_head_quant_mem_manager.py:70-72](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_head_quant_mem_manager.py#L70-L72) 抛出 `quant type ... in config not match per-head backend`。这也是文档「Common Issues」里 `quant_type not match` 报错的来源。

**练习 2**：per-head 模式在 8 卡 TP 下，校准文件需要每个 rank 各一份吗？
> **答**：不需要。代码 [fp8_static_per_head_quant_mem_manager.py:33-50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/fp8_static_per_head_quant_mem_manager.py#L33-L50) 会从**同一份全局**校准文件里，按 `rank_in_dp` 自动切出本 rank 负责的头、并处理 GQA 复制。文档「Multi-GPU Note」也明确：只需提供一份完整的 `kv_cache_calib.json`。

**练习 3**：为什么 per-tensor 管理器加载 scale 后没有 per-head 那段「按 rank 切分」的代码？
> **答**：per-tensor 的 scale 形状是 `[layer_num, 2]`，与头数、与 TP 都无关（整层 K 一个、V 一个），每个 rank 都用同一份完整 scale 即可，所以无需切分。

## 5. 综合实践

把三个模块串起来，完成一次「**从启动参数到量化落池**」的完整追踪：

1. **选型**：从 [api_cli.py:416-432](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L416-L432) 的 `--llm_kv_type` 出发，经 [api_start.py:185-189](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L185-L189) 的启动断言，到 [mem_utils.py:49-52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_utils.py#L49-L52) 选出具体管理器类。画出这条「参数 → 断言 → 管理器类」的决策链。
2. **scale 来源**：在选定的管理器 `__init__` 里，标出「校准文件加载 / 全 1 退化」两条分支与 `_load_and_check_config` 的四道校验。
3. **写入量化**：从 [transformer_layer_infer_template.py:35-42](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L35-L42) 的 `_post_cache_kv` 出发，经 `operator.copy_kv_to_mem_manager`，到 [destindex_copy_kv_fp8.py:35-48](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/destindex_copy_kv_fp8.py#L35-L48) 的「除 scale → clamp → 转 FP8」。
4. **产出**：写一张表，列出「per-tensor vs per-head」在 `scales_shape`、scale 总数、是否需按 rank 切分、配套注意力后端（flashinfer / fa3）、预期精度、KV 显存节省六个维度的对比，并给出你会如何为一个新模型在两种模式间做选择的判断依据。

> 提示：判断依据通常是「先看模型有没有现成的 per-head 校准文件与可用 fa3 后端 → 有则优先 per-head（精度更好）；只有 per-tensor 文件或后端受限时退而求其次用 per-tensor」。两者 KV 主显存节省一致，决策几乎只看精度与生态支持。

## 6. 本讲小结

- **FP8 静态量化**用 `float8_e4m3fn`（范围 \([-448,448]\)）存 KV，核心公式是 \(q=\mathrm{clamp}(x/s,-448,448)\)，scale \(s=\max|x|/448\) 离线统计、固化进校准文件。
- 两个量化管理器**继承 `MemoryManager`、只换 `operator_class`**：`fp8kv_spt` → `FP8StaticPerTensorQuantMemManager`，`fp8kv_sph` → `FP8StaticPerHeadQuantMemManager`；`kv_buffer` 强制 `uint8`、形状不变，u4-l1 的分配/回收/RadixCache 全部原样复用。
- 量化发生在「写入 KV」的瞬间——`operator.copy_kv_to_mem_manager` 调 Triton kernel `destindex_copy_kv_fp8`，per-head 与 per-tensor 共用一个 kernel、用编译期常量 `IS_PER_TENSOR_QUANT` 切分支；反量化交由 FP8 原生注意力后端（fa3 / flashinfer）负责。
- **校准配置是必填项**：`--kv_quant_calibration_config_path` 指向的 JSON 提供 scale，`api_start.py` 在启动期强制断言其存在，`_load_and_check_config` 再校验 qmin/qmax、architectures、num_layers、quant_type 四项；per-head 还需按 TP/GQA 把全局 scale 切分到每个 rank。
- **取舍**：per-head 每头一个 scale、精度更好，per-tensor 每层 K/V 各一个 scale、实现更简、配套 flashinfer 后端；两者 KV 主显存节省相同（约省 50%），scale 侧开销都可忽略。

## 7. 下一步学习建议

- **u6-l4 多级 KV Cache**：量化后单 token 更小，配合 CPU/磁盘卸载能进一步放大可承载的上下文。注意 FP8 模式下「CPU cache 卸载/回填」目前由 `NormalMemOperator` 等实现的 `load_cpu_cache_to_gpu`/`offload_gpu_kv_to_cpu_cache` 承担，FP8 operator 暂未覆写这两个接口，阅读时可以关注这一边界。
- **u3-l5 注意力后端机制**：反量化（FP8 → 高精度注意力）完全由注意力后端负责，建议结合 fa3 / flashinfer 后端读取 `mem_manager.scales` 的代码，把「量化存 / 反量化读」的闭环看全。
- **u7-l1 PD 分离与 KV 迁移**：FP8 KV 同样要跨节点迁移，迁移的是 uint8 的 `kv_buffer` 与 scale，可结合 `kv_trans` 与 `write_to_shm` 路径理解量化对迁移数据量的缩减。
- 若要为新模型生成校准文件，参考 [LightCompress](https://github.com/ModelTC/LightCompress) 与 `docs/EN/source/tutorial/fp8_kv_quantization.rst` 的 schema 说明。
