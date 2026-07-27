# 项目定位与 Spec-Driven 设计哲学

## 1. 本讲目标

本讲是 TileOPs 学习手册的第一篇。读完本讲，你应当能够：

- 用一句话说清 **TileOPs 是什么**，以及它和 **TileLang** 之间的依赖关系。
- 解释 **Op（L2，主机侧）** 与 **Kernel（L1，GPU 实现）** 的双层分离，并理解为什么要把用户接口和 GPU 策略隔开。
- 理解 **spec-driven（规约驱动）** 开发模型：为什么 `tileops/manifest/` 是算子接口的「唯一真相来源」，代码、测试、基准都从它派生。
- 在源码里找到 M1–M8 八个模块和四条数据流，并说出每条流当前的完成状态。

本讲不要求你懂 CUDA、TileLang 或任何具体算子的实现。它只建立心智模型，为后续每一讲打地基。

## 2. 前置知识

本讲面向「从零开始」的读者。你只需要具备以下基础概念：

- **算子（operator / op）**：在深度学习里，一个算子就是一次张量运算，比如矩阵乘（GEMM）、归一化（RMSNorm）、注意力（Attention）。你可以把它理解成一个「函数」：输入几个张量，输出几个张量。
- **主机侧（host）与设备侧（device/GPU）**：Python 代码运行在 CPU（主机）上；真正的大规模并行计算运行在 GPU 上。一个完整的算子往往既要做主机侧的校验、布局处理，又要调度 GPU 上的计算。
- **声明式规约（declarative spec）**：先用一种机器可读的格式「描述」一个东西应当长什么样（输入什么、输出什么、有哪些参数），再让程序去「实现」它。规约是「合同」，实现是「履约」。
- **TileLang**：一门用于编写高性能 GPU kernel 的领域专用语言（DSL）。TileOPs 不自己从零写 CUDA，而是站在 TileLang 的肩膀上。如果你现在不了解它也没关系，本讲只需知道「TileOPs 的 Kernel 层是用 TileLang 写的」。

后面遇到陌生术语（如 roofline、manifest、dispatch）时，本讲都会就地解释。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md) | 项目门面：定位、关键属性、安装方式、Quick Start 示例。 |
| [docs/design/architecture.md](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md) | 架构权威文档：M1–M8 八模块、四条数据流、双层分离、Agent 生产循环。 |
| [CLAUDE.md](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/CLAUDE.md) | 协作规约：把「design-first, spec-driven」作为最高开发原则固定下来。 |
| [docs/design/manifest.md](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md) | manifest 的格式规范与信任模型（人审 / Agent / 校验器三方）。 |
| [tileops/manifest/normalization.yaml](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/normalization.yaml) | 一个真实 family 的 manifest 文件，本讲用它做具体例子。 |

> 本讲为概念性首讲，引用的多是文档与规约文件；具体的 Python 实现代码会在后续讲义（U2 Op 层、U3 Kernel 层）深入。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 项目定位与关键属性**：TileOPs 到底是什么，靠哪几条「卖点」立身。
- **4.2 双层分离 Op/Kernel**：一个算子为什么被劈成两层。
- **4.3 Spec-driven 开发模型**：manifest 为什么是唯一真相来源。

### 4.1 项目定位与关键属性

#### 4.1.1 概念说明

先用一句话定位 TileOPs：

> TileOPs 是一个**面向大语言模型（LLM）训练与推理的 GPU 算子库**，构建在 [TileLang](https://github.com/tile-ai/tilelang) 之上。

这句话有两个关键词：

1. **算子库（operator library）**：它不是训练框架，也不替你拼模型；它只提供一个个高性能算子，供框架或推理引擎调用。
2. **构建在 TileLang 之上**：TileOPs 的 GPU kernel 不是手写 CUDA，而是用 TileLang DSL 编写，再由 TileLang 编译到目标硬件（当前主攻 **Hopper / SM_90** 架构）。

但 TileOPs 真正的特别之处不在于「又多了一个算子库」，而在于它探索的一种**开发模型**——README 把它称作「为 AI 代理（agents）设计」的 spec-driven 模式：AI 代理读声明式的算子规约、生成 kernel 实现、再拿硬件理论上限去评估，全程几乎不需要人工搭脚手架。这一点会在 4.3 节展开。

#### 4.1.2 核心流程

TileOPs 的「卖点」可以直接从 README 的 **Key Properties** 一节读出来，共四条，构成它的产品立身之本：

1. **Spec-driven（规约驱动）**：每个算子先在 `tileops/manifest/` 里声明（签名、workloads、roofline 公式），再据此生成代码、跑校验。
2. **Roofline-evaluated（用硬件上限评估）**：kernel 的性能不是和某个基线比，而是和硬件「光速（Speed-of-Light, SOL）」理论上限比。
3. **Auto-tuning（自动调优）**：内置对 tile 尺寸、流水线、调度参数的搜索。
4. **Lightweight（轻量）**：只依赖 TileLang、PyTorch 和 einops。

其中第 2 条值得用一点点数学来建立直觉。Roofline 模型的核心是比较「实际耗时」与「理论上最快耗时」：

\[ \text{SOL 效率} = \frac{\text{理论最短时间}(\text{SOL time})}{\text{实际运行时间}(\text{actual time})} \]

由于实际时间不可能快于理论上限，效率始终落在 \( (0, 1] \)（即 0%–100%），**越接近 100% 越好**。这和「比基线快 1.2 倍」是两种完全不同的评价口径——后者取决于基线选得好不好，前者取决于硬件物理极限。这个差异是理解 TileOPs 性能哲学的钥匙。

#### 4.1.3 源码精读

README 顶部就把定位说得很清楚。这一行副标题点明了「为代理设计 + 规约驱动」两个核心：

[README.md:4](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L4) —— 副标题 "Spec-driven GPU operator library for LLMs — designed for AI agents to build, evaluate, and optimize"。

[README.md:5](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L5) —— 明确「Built on TileLang」的依赖关系。

Overview 一段把 spec-driven 的愿景展开：

[README.md:24](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L24) —— 解释 TileOPs 是一个探索性项目：AI 代理读声明式规约、生成实现、对照硬件理论上限评估。

四条关键属性在这里：

[README.md:37-40](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L37-L40) —— Spec-driven / Roofline-evaluated / Auto-tuning / Lightweight 四个 bullet。

依赖与硬件要求同样来自 README，这是你跑通项目前必须知道的硬约束：

[README.md:46-52](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L46-L52) —— Python ≥ 3.10、PyTorch ≥ 2.1（< 2.11）、CUDA 12.x、NVIDIA **Hopper (SM_90)**、TileLang ≥ 0.1.9（< 0.2.0）。注意当前主攻架构是 Hopper，没有合适的 GPU 就无法实际运行算子（但本讲的练习不依赖 GPU）。

#### 4.1.4 代码实践

**实践目标**：把「四条关键属性」从 README 翻译成自己的语言，并判断每一条对使用者意味着什么。

**操作步骤**：

1. 打开 [README.md:35-40](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L35-L40) 的 Key Properties。
2. 在一张纸上画一张 4 行 2 列的表：左列写属性名，右列用「对一个只想调用算子的使用者来说，这条意味着______」的句式各填一句。
3. 重点想清楚：Roofline-evaluated 这一条，对一个「拿 TileOPs 和 cuDNN 比速度」的人会带来什么认知冲击？

**需要观察的现象**：你会发现自己很难把「roofline」翻译成「比某某快 X 倍」——这正是它和传统基准对比的根本不同。

**预期结果**：四条属性应当分别指向「接口可信」「性能有物理依据」「省人工调优」「依赖少易装」。这是后续每一讲的隐性背景。

#### 4.1.5 小练习与答案

**练习 1**：TileOPs 当前主攻哪种 GPU 架构？如果不具备该架构的 GPU，能否运行算子？

> **答案**：主攻 **Hopper（SM_90）**，见 [README.md:51](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L51)。不具备合适 CUDA GPU 则无法实际运行算子（README 验证命令注明 "requires a CUDA GPU"，见 [README.md:69-70](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L69-L70)），但阅读源码和规约不受影响。

**练习 2**：Roofline-evaluated 与「和某个 baseline 比速度」的根本区别是什么？

> **答案**：Roofline 的参照系是**硬件物理理论上限（Speed-of-Light）**，效率 = 理论最短时间 / 实际时间，上限是 100%；baseline 对比的参照系是另一个实现，结果取决于 baseline 选得好不好。前者回答「离极限还有多远」，后者回答「比别人快还是慢」。

**练习 3**：TileOPs 的三个硬依赖（Lightweight）分别是什么？

> **答案**：TileLang、PyTorch、einops，见 [README.md:40](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L40)。

### 4.2 双层分离 Op/Kernel

#### 4.2.1 概念说明

TileOPs 里**每一个算子都被精确地劈成两层**，这是它最核心的架构决策之一：

- **Op（L2，主机侧入口）**：无状态（stateless）的 Python 类。负责输入校验、dtype 转换、内存布局处理，以及对 CUDA-Graph 和 `torch.compile` 的兼容性。它是用户唯一打交道的对象。
- **Kernel（L1，GPU 实现）**：用 TileLang 写的、针对特定硬件（Hopper）优化的 GPU kernel。

两者之间有一条**严格的边界**：Op 层永远不含 TileLang 代码；Kernel 层永远不做用户输入校验。这条边界的好处是——你可以单独替换某一层而不影响另一层：换 GPU 策略只改 Kernel，改用户接口只改 Op。

#### 4.2.2 核心流程

一次算子调用（比如 `gemm(a, b)`）的数据流可以概括为：

```text
用户代码  →  Op 实例（L2）
              │  · 校验 dtype / shape
              │  · 选择并构造 Kernel
              │  · 处理布局（如转置语义）
              ▼
           Kernel（L1，TileLang 实现）
              │  · 在 GPU 上跑真正的并行计算
              ▼
           输出张量  →  返回用户
```

关键点：

- **形状与 dtype 在「调用时」推断，而非「构造时」固定**。也就是 `GemmOp()` 构造时不传形状，等真正 `gemm(a, b)` 时才根据传入张量推断。这一点在 README 的 Quick Start 里有注释说明。
- **Op 是无状态的**：同一个 `Op` 实例可以被不同形状/dtype 反复调用（内部会按 cache key 复用或重新 JIT 编译 kernel，细节留到 U2）。
- **`torch.compile(fullgraph=True)` 支持是逐算子声明的**：并不是所有 op 都保证可编译，而是通过 manifest 里的 `torch_compile_fullgraph` 字段逐个声明。

#### 4.2.3 源码精读

README 的 Architecture 小节直接给出双层定义：

[README.md:28-33](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L28-L33) —— Op(L2) 负责校验/dtype/layout 且 CUDA-Graph 兼容；Kernel(L1) 是 TileLang 的硬件相关实现；并点明「这种分离让用户可见行为与 GPU 策略彼此独立」。

architecture.md 用一张表把边界写死：

[docs/design/architecture.md:98-107](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L98-L107) —— L2 Op 是「无状态调度器，硬件无关入口，CUDA-Graph 兼容，`torch.compile` 支持逐 op 声明」；L1 Kernel 是「针对特定硬件优化的 TileLang 实现」。并强调「Op 层永不含 TileLang 代码，Kernel 层永不校验用户输入」。

Quick Start 的 GEMM 示例是「调用时推断」最直观的证据：

[README.md:81](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L81) —— `gemm = GemmOp()` 构造时不传任何形状/dtype，注释写明 "shapes and dtype are inferred at call time"。

[README.md:84-86](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L84-L86) —— 输入 `b` 形状是 `(N, K)` 而非 `(K, N)`，因为 `trans_b=True` 是默认语义；最终 `d = gemm(a, b)` 等价于 `a @ b.T`。这说明「布局处理」这件主机侧的活，正是 Op 层的职责。

#### 4.2.4 代码实践

**实践目标**：从源码里亲眼确认「Op 在 `tileops/ops/`、Kernel 在 `tileops/kernels/`」，并理解 Quick Start 背后的布局语义。

**操作步骤**：

1. 浏览目录，确认 `tileops/ops/`（Op 层）与 `tileops/kernels/`（Kernel 层）是两个独立子树。对照 architecture.md 的 Module reference（[docs/design/architecture.md:72](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L72)）——M2 同时拥有这两个目录。
2. 阅读 Quick Start（[README.md:74-87](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L74-L87)），回答：为什么 `b` 要写成 `(N, K)` 而不是 `(K, N)`？
3. （**待本地验证**，需 Hopper GPU）若有环境，把 Quick Start 代码跑一遍，再用 `torch.matmul(a, b.T)` 对照结果：

   ```python
   import torch
   from tileops.ops import GemmOp

   M, N, K = 1024, 1024, 512
   dtype = torch.float16
   gemm = GemmOp()
   a = torch.randn(M, K, device="cuda", dtype=dtype)
   b = torch.randn(N, K, device="cuda", dtype=dtype)
   d = gemm(a, b)
   ref = a @ b.T
   print("max abs diff:", (d - ref).abs().max().item())
   ```

**需要观察的现象**：输出与 `a @ b.T` 数值接近（浮点误差范围内），证明 `trans_b=True` 默认语义下的布局处理正确。

**预期结果**：最大绝对误差是一个很小的浮点数（fp16 下通常在 1e-2 量级以内）。若没有 GPU，本步骤标注为「待本地验证」，仅完成第 1、2 步的源码阅读即可。

#### 4.2.5 小练习与答案

**练习 1**：Op 层和 Kernel 层各自的「禁区」是什么？

> **答案**：Op 层永远不含 TileLang 代码；Kernel 层永远不做用户输入校验。见 [docs/design/architecture.md:107](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L107)。

**练习 2**：Quick Start 里 `GemmOp()` 构造时没有传 `M/N/K`，这些形状信息从哪里来？

> **答案**：在调用 `gemm(a, b)` 时，从实际传入的张量 `a`、`b` 的 shape 推断，构造时不固定。见 [README.md:81](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L81) 的注释。

**练习 3**：为什么要把用户接口和 GPU 策略分到两层？

> **答案**：让用户可见行为（接口、校验、布局）与 GPU 策略（tile、流水线、硬件特化）彼此独立，修改任一层不会产生副作用。见 [README.md:33](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L33)。

### 4.3 Spec-driven 开发模型

#### 4.3.1 概念说明

Spec-driven（规约驱动）是 TileOPs 的灵魂。它的含义可以用一句话概括：

> **`tileops/manifest/` 是算子接口的唯一真相来源（single source of truth）；代码服从规约，而不是反过来。**

这句话直接写在 CLAUDE.md 的最高原则里：

> "design docs and `tileops/manifest/` are the authoritative spec; **code conforms to the spec, not the other way around.**"

具体来说，每个算子在写任何一行实现代码之前，必须先在 manifest 里声明：

- **signature（签名）**：输入/输出张量的 dtype、shape 规则，以及参数。
- **workloads（负载）**：基准测试用的形状/dtypes（注意：仅用于基准参数化，**不是**单元测试覆盖）。
- **roofline（性能模型）**：算子的 flops 与 bytes 公式，用来和硬件上限比。
- **source（实现路径）**：登记 kernel / op / test / bench 文件在哪。

这份声明一旦确定，就成了三方协作的「合同」：

- **人审（Human reviewer）**：唯一能修改 manifest 的角色，所有改动必须走 PR 评审。
- **AI 代理（Agent）**：只读 manifest，据此生成 Op 代码、测试、基准；**绝不**反向修改 manifest。
- **校验器（Validator）**：在 CI 里强制 manifest 与代码保持一致。

#### 4.3.2 核心流程

spec-driven 的工作流可以用四个阶段（信任模型）来理解，详见 [docs/design/manifest.md:24-34](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md#L24-L34)：

```text
[人审]  ──写/批准──▶  [manifest（规约）]
                            │
              ┌─────────────┼──────────────┐
              ▼             ▼              ▼
        [Agent 读规约]  [Validator 校验]   （代码层）
              │                              ▲
              └──产出 Op/测试/基准──────────┘
```

它的不变量（invariants）包括：

1. manifest 是算子接口的**唯一**真相来源。
2. 校验规则**派生自** manifest，而不是派生自生成代码的代理（防止「自己查自己」）。
3. `workloads` 定义的是基准形状/dtypes，**不是**单元测试覆盖。
4. 算子参数必须与 manifest 声明一致（参数子集关系 + 顺序匹配），CI 强制检查。

这套模型的本质目的是**让规约成为仲裁者**：当代码和规约冲突时，不是改规约迁就代码，而是改代码（或先标 `status: spec-only`，再在后续 PR 里修代码）。这把「接口正确性」从「看代码实现」提升到了「看声明文件」，极大降低了代理自动化的风险。

#### 4.3.3 源码精读

CLAUDE.md 把 spec-driven 钉死为最高原则：

[CLAUDE.md:5-7](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/CLAUDE.md#L5-L7) —— "design-first, spec-driven development: ... code conforms to the spec, not the other way around."

manifest.md 开篇即定义 manifest 的地位：

[docs/design/manifest.md:3](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md#L3) —— "The `tileops/manifest/` package is the **source of truth** for op interfaces, benchmark workloads, and roofline metadata."

信任模型三方与不变量：

[docs/design/manifest.md:24-26](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md#L24-L26) —— 人审写/批准 manifest，Agent 只读、产出代码，Validator 在 CI 校验两者一致。

[docs/design/manifest.md:28-34](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md#L28-L34) —— 五条不变量，其中第一条「manifest 是算子接口的唯一真相来源」是整个模型的地基。

来看一个**真实**的完整 manifest 条目，建立感性认识。以 `RMSNormFwdOp` 为例：

[tileops/manifest/normalization.yaml:7-10](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/normalization.yaml#L7-L10) —— 条目键名就是 Op 的 Python 类名 `RMSNormFwdOp`；声明 `ref_api`（对照 PyTorch 的 `torch.nn.functional.rms_norm`）、`family: normalization`、`status: implemented`。

[tileops/manifest/normalization.yaml:12-25](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/normalization.yaml#L12-L25) —— `signature`：输入 `x`（fp16|bf16）和 `weight`（`same_as(x)`，即与 x 同 dtype），输出 `output`；`shape_rules` 用 Python 表达式约束形状关系（任意秩算子用 `shape_rules`，而非固定 shape）。

[tileops/manifest/normalization.yaml:27-36](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/normalization.yaml#L27-L36) —— `workloads`：基准负载（如 llama-3.1 各规格的 prefill/decode 形状），仅用于基准参数化。

[tileops/manifest/normalization.yaml:38-45](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/normalization.yaml#L38-L45) —— `roofline`：用 `vars` 定义中间变量 M、N，再给出 `flops`、`bytes` 公式。这就是 4.1 节 roofline 评估所需的「理论量」来源。

[tileops/manifest/normalization.yaml:47-54](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/normalization.yaml#L47-L54) —— `source`：登记 kernel 文件、`kernel_map`（dispatch key → Kernel 类名）、op/test/bench 文件路径。注意 `kernel_map` 是「登记表」，描述 Op 用了哪些 Kernel，**不**描述运行时调度策略。

这条目完美体现了 spec-driven：签名、负载、性能模型、实现路径，全部先于代码声明，代码只是「履约」。

#### 4.3.4 代码实践

**实践目标**：用 `RMSNormFwdOp` 这个真实条目，验证「manifest 驱动代码/测试/基准」并非空话。

**操作步骤**：

1. 打开 [tileops/manifest/normalization.yaml:7-54](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/normalization.yaml#L7-L54)，逐字段填下面这张表：

   | manifest 字段 | 值（摘自 yaml） | 它驱动了什么 |
   | --- | --- | --- |
   | `signature.inputs.x.dtype` | ? | Op 的 `_validate_dtypes` 校验逻辑 |
   | `signature.outputs.output.dtype` | ? | Op 的输出 dtype 推断 |
   | `workloads` | ? | 基准测试的形状参数化 |
   | `roofline.flops` | ? | M5 roofline 效率计算 |
   | `source.kernel` | ? | Kernel 实现文件位置 |
   | `source.test` / `source.bench` | ? | 测试/基准文件位置 |

2. 思考：如果某天发现 RMSNorm 的 kernel 实现和 `signature` 声明不一致，按 spec-driven 原则，应该改哪一边？

**需要观察的现象**：你会发现 manifest 的每一个字段都能对应到下游某个产物（校验代码、基准参数、性能模型、文件路径），没有「悬空」字段。

**预期结果**：第 2 步的答案应当是「改代码（或先标 `status: spec-only`）」，**而不是**改 manifest 迁就代码——这正是 [CLAUDE.md:7](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/CLAUDE.md#L7) 的原则。

#### 4.3.5 小练习与答案

**练习 1**：在 spec-driven 模型里，谁是唯一能修改 manifest 的角色？AI 代理能改吗？

> **答案**：只有**人审（Human reviewer）**能修改 manifest，且必须走 PR 评审；AI 代理只读、绝不修改。见 [docs/design/manifest.md:24-25](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md#L24-L25)。

**练习 2**：`workloads` 字段定义的形状，是用来做单元测试覆盖的吗？

> **答案**：不是。`workloads` 仅用于**基准参数化**，不是单元测试覆盖。见 [docs/design/manifest.md:33](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md#L33) 与不变量第 3 条。

**练习 3**：当 kernel 实现与 manifest 声明冲突时，正确做法是什么？

> **答案**：改代码以符合规约；若一时无法修复，先把该 op 标为 `status: spec-only`，再在后续 PR 里修代码。绝不为了迎合代码而改 manifest。见 [CLAUDE.md:7](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/CLAUDE.md#L7)。

## 5. 综合实践

本讲的综合实践把三个模块串起来：用一张手绘图，把 TileOPs 的「八模块 + 四数据流」全景画出来。这是后续每一讲都会反复引用的「地图」。

**实践目标**：建立 TileOPs 系统拓扑的全局心智模型，并标注每条数据流的当前状态。

**操作步骤**：

1. 先读 [docs/design/architecture.md:7-9](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L7-L9)，确认「8 个模块（M1–M8）+ 4 条数据流」的总框架。
2. 在纸上画出 8 个模块方块，建议按以下分组排版：
   - 主线：**M1 Spec** → **M2 Op+Kernel** → **M3 Correctness** → **M7 CI Gate**
   - 调优环（画一个虚线框圈起来）：**M4 Perf Tuning** ↔ **M5 Roofline**
   - 硬件标定：**M6 HW Profile**（数据来自 HW Microbench）
   - 产出：**M8 Docs**
3. 用四种颜色的笔，分别画出四条数据流（颜色含义见 [docs/design/architecture.md:56](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L56)）：
   - 🟢 **Op Delivery**：M1 → M2 → M3 → M7
   - 🔵 **Perf Tuning**：M1 → M4 → M5 → M2（含调优环）
   - 🟠 **HW Calibration**：HW Microbench → M6 → M5
   - 🟣 **Publish**：M2 / M7 → M8
4. 对照 [docs/design/architecture.md:60-66](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L60-L66) 的 Flow status 表，在每条流旁边标注当前状态（done / partial）和缺口（Gap）。

**需要观察的现象**：你会看到 Op Delivery 是唯一 `done` 的流；其余三条都是 `partial`，各自有明确缺口（如 Perf Tuning 缺效率计算闭环、HW Calibration 缺 tensor core 标定、Publish 缺 API reference 生成）。这说明 TileOPs 是一个**仍在积极演进**的项目。

**预期结果**：一张标注完整的拓扑图，至少包含 8 个模块、4 条彩色数据流、每条流的状态与缺口。这张图建议保留——本手册后续每一讲的「本讲源码地图」都可以回填到这张图的某个模块上。

> 说明：本实践为「源码阅读 + 手绘」型，不依赖 GPU，所有人都能完成。无需运行任何命令。

## 6. 本讲小结

- **TileOPs 是构建在 TileLang 之上的 LLM GPU 算子库**，当前主攻 Hopper（SM_90）架构，依赖只有 TileLang、PyTorch、einops。
- 它有四条立身属性：**Spec-driven、Roofline-evaluated、Auto-tuning、Lightweight**；其中 roofline 用「硬件理论上限」而非基线来评价性能。
- 每个算子被**精确劈成两层**：Op（L2，主机侧无状态入口，负责校验/布局/编译兼容）与 Kernel（L1，TileLang 的硬件相关实现）；两层有严格边界，互不越界。
- **形状与 dtype 在调用时推断**，而非构造时固定；`torch.compile(fullgraph=True)` 支持逐 op 声明。
- **spec-driven** 意味着 `tileops/manifest/` 是算子接口的唯一真相来源，**代码服从规约**；人审改 manifest、Agent 只读生成、Validator 在 CI 强制一致。
- 系统由 **M1–M8 八模块** 与 **四条数据流**（Op Delivery / Perf Tuning / HW Calibration / Publish）组成；目前只有 Op Delivery 流 `done`，其余 `partial`。

## 7. 下一步学习建议

本讲建立了全局心智模型，建议按以下顺序继续：

1. **先动手跑通项目**：进入 [u1-l2 环境搭建与首次运行](u1-l2-install-and-first-run.md)，完成 `make install` 并用 `GemmOp` 跑通第一个算子（需 Hopper GPU）。
2. **建立目录直觉**：进入 [u1-l3 目录结构与模块全景](u1-l3-directory-and-modules.md)，把 M1–M8 映射到具体目录。
3. **学会调用任意算子**：进入 [u1-l4 算子的公开 API 与调用方式](u1-l4-public-api-and-usage.md)，掌握 `from tileops.ops import ...` 的用法。
4. **想深入 spec-driven 细节**：可先跳读 [docs/design/manifest.md](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md) 的 Trust Model 与 Rules 一节，但完整理解 manifest 格式会在 **U4（Manifest：规约即真理）** 系统讲解。
5. **想理解双层分离的实现**：Op 层细节在 **U2**，Kernel 层（TileLang）细节在 **U3**，本讲只讲了「为什么分」，那两单元讲「怎么分」。

> 一句话总结：本讲覆盖了三个最小模块——**项目定位与关键属性**、**双层分离 Op/Kernel**、**Spec-driven 开发模型**，并用 M1–M8 八模块与四数据流的全景图把它们串成了 TileOPs 的全局心智地图。
