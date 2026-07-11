# 注意力后端机制

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 LightLLM 为什么要把「注意力计算」抽象成一层可替换的「后端（backend）」，以及它解决了什么问题。
- 读懂 `base_att.py` 中 `BaseAttBackend`、`BasePrefillAttState` / `BaseDecodeAttState`、`AttControl` 三个抽象各自的职责。
- 读懂 `create_utils.py` 如何根据 **KV 数据类型 + 优先级列表 + 子进程真值校验**，自动为 prefill 和 decode 各挑选一个注意力后端。
- 理解 prefill 与 decode 两阶段为何要分别建一个「状态对象」，以及这个状态对象在一次前向中的生命周期。
- 能够定位 triton / fa3 / flashinfer / mla / nsa 五类后端实现，知道它们各自适用什么场景。

本讲承接 u3-l3（推理层模板），向下打开「注意力核」这一层；它也是 u3-l5 之后 u5-l5（MLA 注意力）、u6-l3（FP8 KV 量化）的前置知识。

## 2. 前置知识

### 2.1 什么是注意力后端

在大模型推理里，注意力（attention）是显存带宽和算力最敏感的算子之一。同一个「带因果掩码的缩放点积注意力」，在不同硬件、不同库下有截然不同的实现：

- **triton**：LightLLM 自己用 Triton 语言写的 kernel，纯软件、跨硬件兼容性最好，是兜底实现。
- **fa3（FlashAttention-3）**：面向 Hopper（H100 等）架构的高度优化实现，prefill 阶段性能极强。
- **flashinfer**：另一个高性能推理库，decode 阶段（长序列、单 token）性能突出。
- **mla / nsa**：分别对应 DeepSeek 的多头潜变量注意力（MLA）和原生稀疏注意力（NSA），它们改变了 Q/K/V 的语义，需要专门后端。

「注意力后端」就是把这套「同一个语义、多种实现」的差异藏在统一接口背后。上层模型（如 llama、deepseek）只需调用 `infer_state.prefill_att_state.prefill_att(q, k, v)`，至于底层跑的是 triton 还是 fa3，由后端选择决定。

### 2.2 prefill 与 decode 的本质差异

这点 u3-l2 已经讲过，这里只做回顾，因为它直接决定了后端为什么要拆两套：

- **prefill（context 阶段）**：一次性吃进一整段 prompt（几十到几千个 token），Q 序列长。
- **decode（token 阶段）**：每步只新算 1 个 token（MTP 下是少数几个），但要让它复用全部历史 KV，Q 序列极短、KV 序列长。

正因如此，最优 kernel 不同：prefill 偏好 fa3，decode 偏好 flashinfer。LightLLM 因此为 prefill 和 decode **分别挑选后端**，而不是一刀切用一个。

### 2.3 单例模式与一次前向的状态

注意力后端对象本身是**进程级常驻**的（单例，整个模型只一份），但它每次前向需要的临时张量（如 page table、cu_seqlens）是**每拍新建**的。这种「常驻后端 + 一次性状态」的二分，是本讲最核心的设计直觉。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `lightllm/common/basemodel/attention/base_att.py` | 后端抽象的「根」：`BaseAttBackend`（单例后端）、`AttControl`（入参控制包）、`BasePrefillAttState` / `BaseDecodeAttState`（两阶段状态抽象）。 |
| `lightllm/common/basemodel/attention/create_utils.py` | 后端**选择器**：按数据类型分组的映射表 + 优先级自动选择 + 子进程真值校验。 |
| `lightllm/common/basemodel/attention/__init__.py` | 统一出口，把基类、所有后端类、所有 `get_*_att_backend_class` 选择函数汇合导出。 |
| `lightllm/common/basemodel/attention/triton/fp.py` | triton 通用后端实现，是理解「状态对象如何实现 `prefill_att`/`decode_att`」的最佳样本。 |
| `lightllm/common/basemodel/attention/fa3/fp.py` | fa3 后端实现，展示后端持有常驻资源（共享 page table buffer）。 |
| `lightllm/utils/backend_validator.py` | 在**独立子进程**里用 PyTorch SDPA 真值校验每个后端是否真的可用，是自动选择的「守门员」。 |

另外两个「调用方」文件用于串起上下文，不属于后端本身：

- `lightllm/common/basemodel/basemodel.py`：在模型初始化时调用选择函数建后端（`_init_att_backend`），在每次前向时为 `infer_state` 建状态对象。
- `lightllm/common/basemodel/infer_struct.py`：`init_att_state()` 触发状态对象的 `init_state()`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**注意力后端抽象**、**后端选择**、**prefill/decode 状态**。

### 4.1 注意力后端抽象

#### 4.1.1 概念说明

LightLLM 的注意力层采用**策略模式**：定义一个抽象基类 `BaseAttBackend`，规定所有后端必须实现的「能干什么」；具体「怎么干」交给子类（triton/fa3/flashinfer…）。模型层只持有抽象基类的引用，运行时多态分派到具体实现。

这样做的好处是：新增一个注意力实现（比如未来接入 tilelang）只需写一个子类，**模型代码一行都不用改**。这正是 LightLLM「轻量、易扩展」理念在算子层的体现。

#### 4.1.2 核心流程

一个后端对象对外暴露三件事：

```text
BaseAttBackend (单例，常驻)
  ├── create_att_prefill_state(infer_state) -> BasePrefillAttState   # 生产 prefill 状态
  ├── create_att_decode_state(infer_state)  -> BaseDecodeAttState    # 生产 decode 状态
  └── _find_layer_index(k, v, att_state)    -> int                    # 工具：按内存指针定位层号
```

关键约束有两点：

1. **单例**：每个后端类全局只有一份实例。因为后端持有大块常驻显存（如 fa3 的共享 page table、flashinfer 的 workspace），重复建多份会浪费显存。
2. **状态与后端分离**：后端本身是常驻的「工厂」，真正每次前向的临时状态由 `create_att_*_state` 现场生产。这避免了「常驻对象里塞一次性数据」带来的 CUDA Graph 捕获困难。

#### 4.1.3 源码精读

先看后端抽象基类 `BaseAttBackend` 的定义：

[base_att.py:11-15](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L11-L15) —— 类注释说明这是单例基类，每种后端只有一个实例。

单例通过重写 `__new__` 实现：用一个类字典 `_instances` 缓存，已建过就直接返回旧实例：

[base_att.py:17-29](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L17-L29) —— `_instances` 以「类本身」为 key，`__new__` 保证 `Fa3AttBackend`、`TritonAttBackend` 等各自最多实例化一次。

`__init__` 只做一件事——持有模型引用，供子类读取 `model.config`、`model.graph_max_batch_size` 等信息：

[base_att.py:31-32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L31-L32) —— 注意：因为单例，`__init__` 在「再次构造」时仍会被 Python 调用一次，所以这里的初始化必须幂等（重复赋值同一个 `self.model` 无副作用）。

抽象方法 `create_att_prefill_state` / `create_att_decode_state` 规定子类必须实现「生产状态」的能力，默认抛 `NotImplementedError`：

[base_att.py:34-38](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L34-L38) —— 这两个方法是后端与「每拍状态」之间的桥梁。

再看一个常被忽略的工具方法 `_find_layer_index`，它体现了 LightLLM 「按内存指针定位」的技巧：

[base_att.py:40-48](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L40-L48) —— 它把 KV buffer 每一层的 `data_ptr()`（显存地址）建成字典，再用当前 k/v 的地址反查出这是第几层。因为同一块 KV 池地址是稳定的，这比层层传 `layer_index` 参数更省心，是 fa3/flashinfer 等后端内部常用的定位手段。

最后看一个具体的 triton 后端如何实现这两个工厂方法——非常薄，只是 `return` 一个状态对象：

[triton/fp.py:7-12](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/triton/fp.py#L7-L12) —— `TritonAttBackend` 自身没有 `__init__`（继承基类的单例与模型引用），把所有干活逻辑都下放到状态类。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证「单例」确实成立。

1. 打开 [base_att.py:17-29](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L17-L29)。
2. 追踪 `_instances` 这个类属性：它定义在 `BaseAttBackend` 上，但被所有子类共享吗？思考一下：`Fa3AttBackend` 调用 `__new__` 时，`cls` 是 `Fa3AttBackend`，所以 key 是 `Fa3AttBackend` 这个类；`TritonAttBackend` 的 key 是 `TritonAttBackend`。因此不同后端互不干扰、各自单例。
3. 思考一个边界情况：既然 `_instances` 缓存了实例，那么如果 LightLLM 在同一进程里先后加载两个模型（两个 `TpPartBaseModel`），同一个后端类会返回**同一个实例**，导致 `self.model` 被后加载的模型覆盖。结合 [base_att.py:31-32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L31-L32) 的 `self.model = model`，说明这个单例设计**隐含假设：一个进程只服务一个模型**（这与 LightLLM 每 GPU 一个 backend 进程的架构一致，见 u2-l4）。

**需要观察的现象**：理解单例的 key 是「类」而非「类+模型」，从而明白为什么单进程单模型是必要前提。

**预期结果**：能用自己的话解释「为什么 LightLLM 的多进程架构（每 GPU 一个 model backend 进程）与注意力后端单例设计是配套的」。结果待本地验证（若想在本地复现单例行为，可写一个最小 Python 片段 `class A(BaseAttBackend): pass`，连续两次 `A(model)` 观察返回同一对象）。

#### 4.1.5 小练习与答案

**练习 1**：`BaseAttBackend` 的 `__init__` 接收 `model` 参数，但子类 `TritonAttBackend` 没有写 `__init__`。这合法吗？为什么？

> **答案**：合法。`TritonAttBackend` 继承自 `BaseAttBackend`，会自动复用基类的 `__init__(self, model)`。triton 后端不需要额外的常驻资源，所以没必要重写。

**练习 2**：为什么 `create_att_prefill_state` 不直接在后端对象上缓存状态，而要每次新建一个返回？

> **答案**：因为状态对象里装的是「每拍都不同」的临时张量（如 cu_seqlens、page table），且 decode 阶段要被 CUDA Graph 录制。缓存在单例后端上会导致多 batch 之间相互覆盖，也无法满足 CUDA Graph 对地址稳定的要求。状态对象「每拍新建」正是 u6-l1 CUDA Graph 能工作的前提。

---

### 4.2 后端选择

#### 4.2.1 概念说明

有了抽象基类，接下来要回答：**给定一个具体的运行环境，该选哪个后端？** 这个决策依赖三个维度：

1. **KV 数据类型（`llm_kv_type`）**：是普通浮点（`None`）、还是 int8/int4 量化 KV、还是 FP8 量化 KV？不同类型能用的后端不同。例如 FP8 per-tensor 量化（`fp8kv_spt`）只有 flashinfer 支持。
2. **阶段**：prefill 和 decode 用不同的优先级。
3. **环境可用性**：fa3 需要 Hopper 架构、flashinfer 需要装好对应版本的库。LightLLM 不会假设它们一定可用，而是**实际跑一遍真值校验**来确认。

还有一个隐式维度：模型变体。普通模型走 `data_type_to_backend`；MLA 模型走 `mla_data_type_to_backend`；NSA 模型走 `nsa_data_type_to_backend`。三套映射表互不通用。

#### 4.2.2 核心流程

选择流程的伪代码：

```text
get_prefill_att_backend_class(index, priority_list):
    dtype   = args.llm_kv_type              # 从启动参数取 KV 类型
    backend_str = args.llm_prefill_att_backend[index]   # 用户显式指定 or "auto"
    if backend_str != "auto":
        return 映射表[dtype][backend_str]    # 用户兜底指定，直接用
    else:
        return _auto_select_backend(dtype, 映射表, priority_list)

_auto_select_backend(dtype, 映射表, priority_list):
    if 开启了 ep_moe:
        从 priority_list 里剔除 flashinfer
    for name in priority_list:              # 按优先级逐个试
        if name in 映射表[dtype] and validate(name):   # 真值校验通过
            return 映射表[dtype][name]
    return 映射表[dtype]["triton"]          # 实在不行兜底 triton
```

两个关键优先级（也是 `--help` 文档里写明的）：

- prefill 默认 `["fa3", "flashinfer", "triton"]` —— fa3 优先。
- decode 默认 `["flashinfer", "fa3", "triton"]` —— flashinfer 优先。

softmax 缩放系数用标准公式：

\[
\text{sm\_scale} = \frac{1}{\sqrt{d_k}}
\]

其中 \(d_k\) 为 head 维度。各后端内部都按此计算（见 fa3 实现 [fa3/fp.py:93-94](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/fa3/fp.py#L93-L94)）。

#### 4.2.3 源码精读

**第一组：三套映射表。** 先看普通模型的 `data_type_to_backend`，它是一个「数据类型 → {后端名: 后端类}」的二级字典：

[create_utils.py:22-45](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L22-L45) —— 注意几个细节：
- 键 `"None"` 是字符串（对应 `--llm_kv_type` 默认值），不是 Python 的 `None`。
- `int4kv`/`int8kv` 目前只保留 triton 后端，fa3/flashinfer 行被注释掉（说明量化 KV 暂不支持这两个后端）。
- `fp8kv_sph`（per-head 静态量化）只有 fa3；`fp8kv_spt`（per-tensor 静态量化）只有 flashinfer。这正是 u6-l3 FP8 量化讲义中「两种模式对应不同后端」的根因。

MLA 与 NSA 各自一张表，可用后端更少：

[create_utils.py:47-53](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L47-L53) —— MLA 三件套（triton/fa3/flashinfer）。

[create_utils.py:55-63](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L55-L63) —— NSA 目前只有 `flashmla_sparse`，注释里预告了未来会加 fa3/tilelang/aiter。

**第二组：自动选择核心 `_auto_select_backend`。** 这是本模块的心脏：

[create_utils.py:66-90](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L66-L90) —— 三个要点：
1. **EP-MoE 排除 flashinfer**（第 79-81 行）：专家并行 MoE 开启时，flashinfer 注意力会有兼容问题，直接从候选列表里剔除。这是一个真实的「特性互斥」约束。
2. **逐个优先级试探 + 真值校验**（第 83-86 行）：循环里 `validate(backend_name)` 才是真正的守门员——光在映射表里存在还不够，必须能跑通。
3. **兜底 triton 但不校验**（第 89-90 行）：所有校验都失败时回退到 triton，此时不再校验（triton 是纯软件实现，理论上一定可用），并打 warning。

**第三组：6 个对外选择函数。** 它们的结构完全对称，区别只在「用哪张映射表」和「默认优先级」：

[create_utils.py:93-100](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L93-L100) —— `get_prefill_att_backend_class`：读 `args.llm_prefill_att_backend[index]`，非 auto 就直接查普通映射表；auto 就交给 `_auto_select_backend`。

[create_utils.py:103-110](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L103-L110) —— `get_decode_att_backend_class`：唯一区别是默认 `priority_list=["flashinfer", "fa3", "triton"]`，decode 优先 flashinfer。

MLA 版（[create_utils.py:113-130](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L113-L130)）与 NSA 版（[create_utils.py:133-150](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L133-L150)）同样模式，只是换了映射表，NSA 的优先级列表只有 `["flashmla_sparse"]`。

**第四组：真值校验 `validate`。** 它是「自动选择」安全性的关键，且用 `lru_cache` 保证只校验一次：

[backend_validator.py:264-273](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/backend_validator.py#L264-L273) —— `validate` 被 `@lru_cache` 装饰，结果在整个进程内缓存；且 rank 0 校验后用 `broadcast_object_list` 广播给所有 rank，保证张量并行各 rank 结论一致。

校验在**独立子进程**里跑，并用 PyTorch SDPA 作为「标准答案」对比，避免污染主进程的 CUDA 状态：

[backend_validator.py:276-306](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/backend_validator.py#L276-L306) —— 用 `spawn` 起子进程，30 分钟超时（`_VALIDATION_TIMEOUT`），崩溃/超时/结果不一致一律视为不可用。

以 fa3 的校验为例，它显式检查「必须是 Hopper 架构」：

[backend_validator.py:21-29](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/backend_validator.py#L21-L29) —— 非 Hopper 直接返回 `False`，这解释了为什么在 A100/V100 上自动选择会跳过 fa3 落到 flashinfer 或 triton。

**第五组：调用方。** basemodel 在初始化时调一次选择函数，把后端实例存起来：

[basemodel.py:254-257](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L254-L257) —— `get_prefill_att_backend_class(index=0)` 返回的是**类**（如 `Fa3AttBackend`），加 `(model=self)` 才是实例化。`index=0` 对应 `--llm_prefill_att_backend` 这个 nargs 列表的第 0 个，是为「不同层用不同后端」预留的扩展位。

而命令行参数本身的定义如下，注意 `choices` 只列了四种、默认是 `["auto"]`：

[api_cli.py:386-405](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L386-L405) —— 这两个参数的 help 文档直接写明了 prefill/decode 的默认优先级，是理解选择行为最快的入口。

KV 类型参数则列出了所有合法取值：

[api_cli.py:416-432](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L416-L432) —— `None`/`int8kv`/`int4kv`/`fp8kv_sph`/`fp8kv_spt`/`fp8kv_dsa`，它们正是映射表外层字典的 key（`fp8kv_dsa` 专供 NSA 的 deepseek_v32 路径）。

#### 4.2.4 代码实践

**实践目标**：在不实际启动服务的前提下，推断出「某台机器 + 某个模型」最终会选中哪个后端。

操作步骤：

1. 假设你在一张 **H100**（Hopper 架构）上跑普通 llama 模型，KV 类型默认 `None`，两个 att backend 参数都默认 `auto`。
2. 按 [create_utils.py:93-100](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L93-L100) 的 prefill 路径：dtype=`None`，priority=`["fa3","flashinfer","triton"]`。
3. 进入 [create_utils.py:83-86](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L83-L86)：第一个候选 fa3 在 `data_type_to_backend["None"]` 里存在吗？存在（[create_utils.py:27](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L27)）；`validate("fa3")` 在 H100 上为 True（[backend_validator.py:26](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/backend_validator.py#L26)）。所以 **prefill 选 fa3**。
4. 同理 decode 路径 priority=`["flashinfer","fa3","triton"]`，第一个候选 flashinfer 存在且校验通过，**decode 选 flashinfer**。
5. 再假设你在 **A100**（非 Hopper）上：fa3 校验失败（`is_hopper()` 为 False），prefill 会落到 flashinfer；若 flashinfer 也没装，最终落到 triton 并打 warning。

**需要观察的现象**：理解「优先级 + 真值校验」是短路求值——一旦某个候选通过就立即返回，不会继续试后面的。

**预期结果**：能用一张表填出「H100+None」「A100+None」「任意机器+fp8kv_spt」三种组合下的 prefill/decode 选中结果。其中 `fp8kv_spt` 因为映射表里只有 flashinfer（[create_utils.py:42-44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L42-L44)），prefill 也会被「强制」选 flashinfer。结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：用户启动时加 `--llm_prefill_att_backend triton`，会触发真值校验吗？

> **答案**：不会。当 `backend_str != "auto"` 时，走 [create_utils.py:97-98](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L97-L98) 直接查映射表返回，**跳过 `_auto_select_backend` 和 `validate`**。即用户显式指定即视为「我知道自己在干什么」，代价是若环境不支持会在运行期才报错。

**练习 2**：为什么 `validate` 要用 `lru_cache` 还要 broadcast 给所有 rank？

> **答案**：`lru_cache` 避免同一后端在同一进程内被反复校验（校验要起子进程、跑 CUDA，开销大）；broadcast 保证张量并行的各 rank 拿到一致的结论，避免「rank0 选 fa3、rank1 校验失败选 triton」导致各 rank 后端不一致的灾难。

---

### 4.3 prefill/decode 状态对象

#### 4.3.1 概念说明

后端选定后，真正「干活」的不是后端对象本身，而是它生产的**状态对象**。状态对象是「一次性」的——每次前向新建一个，承载这一拍所有注意力相关的临时张量和派生参数。

为什么要单独抽象 `BasePrefillAttState` 和 `BaseDecodeAttState` 两个基类？因为 prefill 和 decode 的：

- **入参形状不同**：prefill 的 Q 是一整段（可能几百 token），decode 的 Q 是 1 个（或 MTP 下少数几个）token。
- **需要的派生张量不同**：fa3 prefill 要建 page_table 和 cu_seqlens；decode 还要考虑 CUDA Graph 的 page buffer 复用。
- **CUDA Graph 处理方式不同**：decode 状态有专门的 `copy_for_decode_cuda_graph` 把新计算的张量拷进图录制的固定地址里。

把这两类状态分开，让上层调度代码只需根据 `is_prefill` 选一个，不用关心内部差异。

#### 4.3.2 核心流程

一次前向中，状态对象的生命周期是：

```text
basemodel.forward(model_input)
  └── is_prefill?
        ├── True:  prefill_att_backend.create_att_prefill_state(infer_state) -> state
        │          state.init_state()        # 现算派生张量（在 init_att_state 里调用）
        │          layer: state.prefill_att(q, k, v, att_control)   # 每层都调一次
        └── False: decode_att_backend.create_att_decode_state(infer_state) -> state
                   state.init_state()
                   layer: state.decode_att(q, k, v, att_control)
                   （若走 CUDA Graph: copy_for_decode_cuda_graph 把新 state 拷进旧 state）
```

注意一个细节：`init_state()` 不是在 `create_att_*_state` 里立刻调，而是由 `InferStateInfo.init_att_state()` 统一触发——这是因为某些派生张量依赖 infer_state 里更晚才算好的字段（如 `b1_cu_q_seq_len`）。

#### 4.3.3 源码精读

**第一组：两个状态抽象基类。** 它们都用 `@dataclass` 装饰，持有 `backend` 和 `infer_state` 两个公共字段：

[base_att.py:75-94](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L75-L94) —— `BasePrefillAttState` 用 `@abstractmethod` 规定子类必须实现 `init_state` 和 `prefill_att`。`prefill_att` 的签名固定为 `(q, k, v, att_control, alloc_func)`，其中 `att_control` 默认是一个新建的 `AttControl()`，`alloc_func` 默认 `torch.empty`（让上层传入 CUDA Graph 专用的分配器）。

[base_att.py:97-122](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L97-L122) —— `BaseDecodeAttState` 多了一个 `copy_for_decode_cuda_graph` 方法（第 106-111 行）：它遍历新状态的所有张量属性，若旧状态里同名字段的**显存地址不同**，就 `copy_` 过去。这正是 CUDA Graph 重放要求「地址不变、内容可变」的标准做法（u6-l1 详讲）。

**第二组：`AttControl` 入参控制包。** 它是一个 dataclass，承载所有「影响注意力内部选路」的开关：

[base_att.py:51-72](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L51-L72) —— 字段分四组：
- 通用：`use_alibi`/`tp_alibi`（ALiBi 位置编码，bloom 用）、`use_sliding_window`/`sliding_window`（滑动窗口注意力，gemma 用）、`use_att_sink`/`sink_weight`（attention sink）。
- MLA 专用：`mla_prefill`/`mla_decode` + 对应 dict。
- NSA 专用：`nsa_prefill`/`nsa_decode` + 对应 dict。

`AttControl` 的本质是「同一个 `prefill_att`/`decode_att` 接口，靠入参开关分派到不同的内部 kernel 分支」。这样接口签名稳定，扩展新特性只加字段不改签名。

以 triton decode 为例，`att_control` 直接驱动内部 if 分支：

[triton/fp.py:102-125](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/triton/fp.py#L102-L125) —— `decode_att` 先看 `use_alibi`，再按 `q_head_num` vs `k_head_num` 分 MHA/GQA（flash-decoding），分别走不同 kernel。`AttControl` 的开关在这里被真正消费。

**第三组：上层如何调用。** llama 模型的注意力核只写了两行，把活全委托给状态对象：

[llama/layer_infer/transformer_layer_infer.py:40-56](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L40-L56) —— `_context_attention_kernel` 先用 `mem_manager.get_att_input_params` 取回本层的 K/V（u3-l3 讲过的「写后读」闭环），再调 `prefill_att_state.prefill_att`。注意 llama 这里**没传 `att_control`**，于是用默认的空 `AttControl()`——因为 llama 既不用 alibi 也不用滑动窗口。

[llama/layer_infer/transformer_layer_infer.py:58-67](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L58-L67) —— decode 路径同理调 `decode_att_state.decode_att`。

对比之下，DeepSeek2 的 MLA 模型则**主动构造 `AttControl`** 来开启 MLA 分支：

[deepseek2/layer_infer/transformer_layer_infer.py:86-92](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L86-L92) —— `AttControl(mla_prefill=True, mla_prefill_dict={"softmax_scale": self.softmax_scale})` 告诉后端「这是一次 MLA prefill，用我给的 scale」。注意这里 `k` 是元组 `(k_nope, k_rope)`，与 llama 的单张量 k 不同——后端据此识别 MLA 语义。这是 u5-l5 MLA 讲义的入口。

[deepseek2/layer_infer/transformer_layer_infer.py:106-112](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L106-L112) —— decode 路径用 `mla_decode=True`。

**第四组：状态的生命周期挂载点。** basemodel 在构造 `infer_state` 时把状态对象挂上去：

[basemodel.py:391-402](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L391-L402) —— 根据 `is_prefill` 只建对应那一个状态对象（prefill 建在 `prefill_att_state`，decode 建在 `decode_att_state`）；`_att_backend1` 系列是给「不同层用不同后端」预留的第二槽位（目前为 None）。

随后 `init_att_state` 触发 `init_state()`：

[infer_struct.py:129-137](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L129-L137) —— 这里同样按 `is_prefill` 分派，并对 `_state1`（第二后端）也做初始化。`init_state` 内部会现算 `cu_seqlens`、`page_table` 等派生张量，例如 fa3 的 [fa3/fp.py:46-58](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/fa3/fp.py#L46-L58)。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 llama decode 的注意力调用链，验证「状态对象」如何把模型层和后端 kernel 解耦。

操作步骤：

1. 从模型层入口看起：[llama/layer_infer/transformer_layer_infer.py:58-67](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L58-L67) 的 `_token_attention_kernel` 调 `decode_att_state.decode_att`。
2. 注意 `decode_att_state` 的类型由「decode 后端 + create_att_decode_state」决定。假设 decode 选了 triton，它就是 `TritonDecodeAttState`。
3. 进入 [triton/fp.py:102-125](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/triton/fp.py#L102-L125)：llama 是 GQA（q_head_num > k_head_num），且没开 alibi，所以会走 `_normal_decode_gqa_flash_decoding_att` 分支。
4. 该分支最终调 `gqa_token_decode_attention_flash_decoding`（[triton/fp.py:185-187](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/triton/fp.py#L185-L187)），完成真正的 GPU 计算。
5. 想象把 decode 后端换成 flashinfer：步骤 1 的模型层代码**完全不变**，只是 `decode_att_state` 变成了 `FlashInferDecodeAttState`，`decode_att` 内部换成 flashinfer 的 wrapper。这就是抽象的价值。

**需要观察的现象**：体会「同一份模型层代码，底层后端可热替换」——模型层只依赖 `infer_state.decode_att_state.decode_att(...)` 这个抽象接口。

**预期结果**：能画出一条从「llama `_token_attention_kernel` → `TritonDecodeAttState.decode_att` → `_normal_decode_gqa_flash_decoding_att` → `gqa_token_decode_attention_flash_decoding`」的调用链，并指出 `AttControl` 在这条链里如何控制分支。结果待本地验证（可加 `print(type(infer_state.decode_att_state))` 在模型层打印实际后端类型，需真实 GPU 环境）。

#### 4.3.5 小练习与答案

**练习 1**：`BaseDecodeAttState.copy_for_decode_cuda_graph` 为什么要逐字段比较 `data_ptr()` 再 `copy_`，而不是直接赋值整个对象？

> **答案**：CUDA Graph 录制时，张量的**显存地址被固化**进图里。重放时必须向「同一地址」写入新内容，所以要从新 state 把张量内容 `copy_` 到旧 state 的固定地址上，而不是替换引用（替换引用会让图里记录的旧地址失效）。`data_ptr()` 比较是为了跳过那些地址未变的字段，省掉无谓拷贝。

**练习 2**：llama 的 `_context_attention_kernel` 调 `prefill_att` 时没传 `att_control`，会出问题吗？

> **答案**：不会。`prefill_att` 的签名里 `att_control: AttControl = AttControl()`（见 [base_att.py:86-93](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L86-L93)），不传就用一个全默认值（所有开关 False）。triton 的 `prefill_att` 据此走普通 `_nomarl_prefill_att` 分支，对 llama 是正确的。

**练习 3**：DeepSeek2 传给 `prefill_att` 的 `k` 是元组 `(k_nope, k_rope)`，而 llama 传的是单张量。后端如何区分？

> **答案**：靠后端实现内部的类型/形状判断。MLA 后端（如 `MlaTritonPrefillAttState.prefill_att`）的签名本就声明 `k: Tuple[torch.Tensor, torch.Tensor]`（见 [triton/mla.py:26-32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/triton/mla.py#L26-L32)），并 `assert att_control.mla_prefill`。即「模型选 MLA 后端 + 传 MLA 专用 AttControl + 传元组 k」三者配套，由模型层和选择机制共同保证一致性。

## 5. 综合实践

**任务**：为「一台 H100 上跑 DeepSeek2 MLA 模型、KV 类型默认 None、不开启 EP-MoE」推断出完整的注意力后端配置，并画出一次 prefill 的注意力调用链。

步骤：

1. **确定后端**。DeepSeek2 是 MLA 模型，故走 `mla_data_type_to_backend`（[create_utils.py:47-53](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L47-L53)）。prefill 默认优先级 `["fa3","flashinfer","triton"]`，H100 上 fa3 校验通过，故 **prefill = `MlaFa3AttBackend`**；decode 默认 `["flashinfer","fa3","triton"]`，故 **decode = `MlaFlashInferAttBackend`**。（注意：MLA 的后端类与普通的不同，来自 `fa3/mla.py`、`flashinfer/mla.py`，见 [create_utils.py:13,16](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L13-L16)。）
2. **确定状态对象**。basemodel 调 `prefill_att_backend.create_att_prefill_state(infer_state)`（[basemodel.py:392](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L392)），得到 MLA 版 prefill 状态。
3. **画出调用链**：
   - 模型层：[deepseek2/layer_infer/transformer_layer_infer.py:86-92](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L86-L92) 构造 `AttControl(mla_prefill=True, ...)` 并调 `prefill_att`。
   - 状态层：MLA 后端的 `prefill_att` 读 `att_control.mla_prefill_dict["softmax_scale"]`，调对应 MLA kernel。
4. **验证一致性**：检查 `AttControl` 的 `mla_prefill=True` 与后端内部的 `assert att_control.mla_prefill`（[triton/mla.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/triton/mla.py)）是否对应——若你推断的后端是 fa3 而非 triton，需打开 `fa3/mla.py` 确认同样的断言存在。

**预期产出**：一张「阶段 → 选择函数 → 映射表 → 选中后端类 → 状态类 → AttControl 开关 → 内部 kernel」的对照表，体现本讲三个模块如何串成一条完整的注意力执行路径。结果待本地验证（真实运行需 H100 + 已装 flashinfer/sgl-kernel）。

## 6. 本讲小结

- LightLLM 用**策略模式**把注意力算子抽象成可替换的后端：`BaseAttBackend`（单例常驻）+ `BasePrefillAttState`/`BaseDecodeAttState`（一次性状态）+ `AttControl`（入参开关包）。
- 后端选择由 `create_utils.py` 完成，依赖 **KV 数据类型映射表 + prefill/decode 各自的优先级列表 + `backend_validator` 的子进程真值校验**三层；用户可用 `--llm_prefill_att_backend`/`--llm_decode_att_backend` 显式指定绕过自动选择。
- prefill 与 decode **分别挑选**后端（prefill 偏 fa3、decode 偏 flashinfer），因为两阶段 Q/KV 长度特征不同、最优 kernel 不同。
- 状态对象**每次前向新建**，承载这一拍的派生张量；`copy_for_decode_cuda_graph` 保证 decode 阶段 CUDA Graph 重放所需的「地址不变、内容可变」。
- `AttControl` 通过 `use_alibi`/`use_sliding_window`/`mla_*`/`nsa_*` 等开关，在不改接口签名的前提下让同一 `prefill_att`/`decode_att` 分派到不同内部 kernel。
- 普通 / MLA / NSA 三类模型各用一套映射表，后端类也分别实现于 `triton|fa3|flashinfer` 下的 `fp`/`mla`/`fp8` 与 `nsa/` 目录。

## 7. 下一步学习建议

- **u5-l2（Llama 完整模型）**：看一个普通模型如何把默认 `AttControl` 与 triton/fa3 后端组合起来，巩固本讲的调用链。
- **u5-l5（MLA 注意力）**：深入 DeepSeek2 的 MLA 实现，理解 `AttControl(mla_prefill=True)` 背后的 KV 压缩与 weight absorption 逻辑。
- **u6-l1（CUDA Graph）**：理解 `copy_for_decode_cuda_graph` 为何如此设计，以及后端常驻资源（如 fa3 的 `_shared_page_table_buffer`）如何配合图捕获。
- **u6-l3（FP8 KV 量化）**：回到 `data_type_to_backend` 的 `fp8kv_sph`/`fp8kv_spt` 两项，理解量化模式如何强制改变后端选择。
- 建议继续阅读 [create_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py) 与 [backend_validator.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/backend_validator.py)，并对照 `attention/` 下各后端子目录的 `fp.py` / `mla.py` 体会「同一接口、多种实现」。
