# Patch 机制总览与两阶段应用

## 1. 本讲目标

vllm-ascend 最核心的「集成武器」不是重写模型，而是**打补丁（Patch）**。本讲学完后，你应该能够：

1. 说清楚 **为什么** vllm-ascend 要大量使用 Monkey-patch，而不是直接修改上游 vLLM 的源码。
2. 掌握 `adapt_patch()` 这个「总闸」如何把补丁分成 **platform patch（平台级、全局、引擎核心子进程生效）** 与 **worker patch（每个 worker 进程生效）** 两个阶段，并理解各自的应用**时机**。
3. 读懂 `vllm_ascend/patch/__init__.py` 这份「补丁登记簿」里的 `What / Why / How / Related PR / Future Plan` 文档化规范，并能照着规范为一个补丁整理出「四要素」。

本讲是单元 3（Patch 机制）的总纲，后续 u3-l2、u3-l3 会分别深入 platform 与 worker 两类补丁的具体实现。

## 2. 前置知识

阅读本讲前，你最好已经建立以下认知（来自 u1、u2）：

- **可插拔硬件插件**：vllm-ascend 不 fork vLLM，而是通过 vLLM 的 entry points 被发现，并返回一个 `NPUPlatform` 作为平台身份（见 u1-l5、u2-l1）。
- **进程模型**：vLLM v1 在线服务通常有一个**引擎核心子进程（engine-core）**负责调度，以及若干 **worker 子进程**负责真正跑模型前向；这些子进程往往用 `spawn` 方式启动，彼此不共享内存与已打的补丁。
- **Python Monkey-patch**：在运行时把某个模块的函数/类替换成自己的实现，例如 `module.some_func = my_func`。这是 vllm-ascend 改造上游 vLLM 行为的基本手法。
- **`is_global_patch` 之前**：你应该知道 `NPUPlatform.pre_register_and_update` 是插件被选中后最早执行的平台钩子之一（见 u2-l1）。

> 术语速查：
> - **Patch（补丁）**：在运行时替换上游 vLLM 的某个函数/类/属性，使其行为适配 NPU。
> - **Platform Patch**：影响调度器、引擎核心、配置等**全局**逻辑的补丁，在引擎核心子进程生效。
> - **Worker Patch**：影响模型前向、采样、算子等**单卡执行**逻辑的补丁，在每个 worker 子进程生效。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `vllm_ascend/patch/__init__.py` | 补丁的「登记簿」：用大段注释记录每一个补丁的 What/Why/How/Related PR/Future Plan。 |
| `vllm_ascend/utils.py` | 提供 `adapt_patch(is_global_patch)`——两阶段补丁的**分流总闸**。 |
| `vllm_ascend/patch/platform/__init__.py` | platform 补丁的聚合入口：`import` 即触发各个平台级补丁。 |
| `vllm_ascend/patch/worker/__init__.py` | worker 补丁的聚合入口：`import` 即触发各个 worker 级补丁。 |
| `vllm_ascend/__init__.py` | 插件入口：`register()` / `register_connector()` 等，内含 `_ensure_global_patch()` 幂等闸门。 |
| `vllm_ascend/platform.py` | `NPUPlatform.pre_register_and_update()` 在平台选中后调用 `adapt_patch(True)`。 |
| `vllm_ascend/worker/worker.py` | `NPUWorker.__init__` 在初始化早期调用 `adapt_patch()`（默认 worker 阶段）。 |
| `vllm_ascend/patch/platform/patch_fused_moe.py` | 一个典型补丁实例：把 vLLM 的 `FusedMoE` 工厂重定向到 `AscendMoERunner`。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Patch 框架**：为什么用 Monkey-patch、一个补丁长什么样。
- **4.2 两阶段补丁**：`adapt_patch` 如何分流 platform / worker，各自的时机。
- **4.3 补丁文档化规范**：读懂 `patch/__init__.py` 的登记格式。

### 4.1 Patch 框架：为什么大量使用 Monkey-patch

#### 4.1.1 概念说明

vllm-ascend 的目标是让上游 vLLM **不改一行代码**就能跑在昇腾 NPU 上。但 vLLM 内部有大量逻辑是「硬编码指向 CUDA」的，例如：

- `vllm.model_executor.layers.fused_moe` 里的 `FusedMoE` 工厂直接 `from ... import` 并调用，没有给硬件插件留「换实现」的钩子。
- `torch.distributed` 的 `all_reduce`/`broadcast` 在 310P 上需要对齐张量，但上游没有这个逻辑。
- `torch.accelerator.memory_stats()` 在 vLLM 某次重构后被改用，但它不会路由到 NPU 后端。

vllm-ascend 面对这些「上游没有留接口、但又必须改行为」的场景，主要有四种手段（见 u1-l1）：**向 vLLM 上游贡献**、**继承重写（Inheritance）**、**直接在 `vllm_ascend/models/` 实现新模型**、以及 **Monkey-patch**。其中 Monkey-patch 是「见效最快、侵入上游为零」的方式：它不修改 vLLM 源文件，而是在运行时把上游的函数/类替换成 Ascend 版本。

> 直觉理解：Monkey-patch 就像「在图书馆的书架上，把某本书悄悄换成你自己的译本」。读者（vLLM 的调用方）照常从同一个书架（同一个 import 路径）取书，拿到的却是你的版本。关键约束是：**替换必须在读者取书之前完成**，否则他读到的还是原版。这个「时机」正是本讲 4.2 要解决的核心问题。

为什么不直接改 vLLM 源码？因为 vllm-ascend 是**解耦**的硬件插件：

- 上游 vLLM 可以独立升级，插件只要跟版本对齐即可。
- 一个补丁往往对应上游的一个 PR/计划，等上游合并后补丁即可**移除**，代码维护负担可控。
- 每个补丁都明确标注「补了什么、为什么、何时可移除」，形成一个**可追踪、可退场**的适配层。

#### 4.1.2 核心流程

一个补丁模块的典型生命周期：

```text
[1] 捕获原始对象        _original_X = upstream_module.X
[2] 定义替换实现        def _ascend_X(...): ...  （或一个新类）
[3] 重绑定              upstream_module.X = _ascend_X
        ↓
[4] 上游调用方按原路径 import / 调用 X → 实际跑的是 _ascend_X
```

注意三个要点：

1. **捕获原始对象**：先把上游原版保存下来，替换实现内部往往还要调用原版（包装而非完全推翻）。
2. **重绑定到原模块**：必须在「上游真正会去取的那个名字空间」上重新赋值。像 `FusedMoE` 这种被 `from ... import` 进来的名字，光改它定义的模块还不够，还得改**已经 import 了它的那些模块**里的绑定（见 4.1.3）。
3. **import 即生效**：vllm-ascend 的补丁模块顶层就是「捕获 + 重绑定」代码，所以 `import` 这个补丁模块就等于「打上补丁」。

#### 4.1.3 源码精读

我们以最典型的 `patch_fused_moe.py` 为例，它把 vLLM 的 `FusedMoE` 工厂重定向到 Ascend 的 MoE Runner。

先看文件顶部的注释，它讲清楚了「为什么要这么早打补丁」：

[vllm_ascend/patch/platform/patch_fused_moe.py:L18-L29](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L18-L29) —— 说明 vLLM 的 `FusedMoE` 是**工厂函数**（不是类），模型在 `from vllm.model_executor.layers.fused_moe import FusedMoE` 时就直接拿到绑定，所以必须在任何模型被 import 之前就把绑定替换掉。

接着是「捕获原始对象」：

[vllm_ascend/patch/platform/patch_fused_moe.py:L32-L53](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L32-L53) —— `import` 上游的 fused_moe 包与 layer 模块，并保存 `_original_FusedMoE = _fused_moe_layer.FusedMoE`（第 39 行）。同时根据 `is_310p()` 选择不同的 Ascend Runner 实现。

然后是「定义替换实现」：

[vllm_ascend/patch/platform/patch_fused_moe.py:L56-L97](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L56-L97) —— `_ascend_FusedMoE` 这个新工厂函数：当调用方没指定 `runner_cls` 时，默认换成 `AscendMoERunner`；处理 EPLB 冗余专家、`tid2eid` 等 Ascend 专属参数；最后**仍然调用原始工厂** `_original_FusedMoE(...)`（第 90 行）来真正构造对象。这是一个典型的「包装（wrap）」而非「推翻」的补丁。

最后是「重绑定」：

[vllm_ascend/patch/platform/patch_fused_moe.py:L100-L101](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L100-L101) —— 同时改写 `_fused_moe_layer.FusedMoE` 和 `_fused_moe_pkg.FusedMoE` 两处绑定。为什么要改两处？因为有的模型写 `from vllm.model_executor.layers.fused_moe import FusedMoE`（从包拿），有的写 `from ...fused_moe.layer import FusedMoE`（从子模块拿）。两处都改，才能保证无论哪种 import 路径拿到的都是替换后的版本。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：验证「补丁必须早于模型 import」这一时序约束。

**操作步骤**：

1. 打开 `vllm_ascend/worker/worker.py` 第 107-113 行，注意 `adapt_patch()`（worker 阶段补丁）在 `from vllm_ascend import ops` 与模型加载**之前**被调用。
2. 再回到 `patch_fused_moe.py` 顶部注释列出的「Import order in worker.__init__」三步。
3. 想象如果把 `adapt_patch()` 移到模型加载之后会发生什么。

**需要观察的现象**：补丁代码注释里明确画出了三步顺序：`adapt_patch() → from vllm_ascend import ops → 模型加载`。这说明补丁是模型正确加载的**前置依赖**。

**预期结果**：你能用自己的话解释——一旦顺序颠倒，模型在 import 时就会拿到**未替换的原版 `FusedMoE`**，导致 MoE 走了 CUDA 路径而在 NPU 上出错。这正是补丁「时机」至关重要的具体体现。无需运行，结论可从注释直接读出。

#### 4.1.5 小练习与答案

**练习 1**：`_ascend_FusedMoE` 为什么要保留对 `_original_FusedMoE` 的调用，而不是自己从头实现一个 MoE 层？

> **参考答案**：上游 `FusedMoE` 工厂负责大量通用逻辑（参数校验、权重注册、量化方法绑定等）。vllm-ascend 只想替换「用哪个 Runner」，不想重复造轮子，所以用包装方式把 `runner_cls` 默认值改成 Ascend 实现，其余仍交给原工厂。这也让补丁在上游工厂演进时更不容易坏。

**练习 2**：补丁为什么要在 `_fused_moe_layer` 和 `_fused_moe_pkg` 两个模块上都重绑定 `FusedMoE`？

> **参考答案**：因为 Python 的 `from X import Y` 会把 `Y` 当时的值拷贝到调用方自己的命名空间。不同模型可能从包（`fused_moe`）或子模块（`fused_moe.layer`）两个入口 import，两处都改才能覆盖所有调用方。

### 4.2 两阶段补丁：adapt_patch 的分流总闸

#### 4.2.1 概念说明

vLLM 是多进程架构：引擎核心（EngineCore）负责调度，worker 负责前向。不同进程关心的事情不一样：

- **引擎核心**关心：调度策略、KV cache block size 推导、配置校验、权重传输后端注册……这些是**全局/平台级**逻辑。
- **worker**关心：模型 forward、采样、Triton 算子、CUDA/ACL Graph……这些是**单卡执行级**逻辑。

如果所有补丁都在一个地方一次性打完，会带来两个问题：一是 worker 子进程用 `spawn` 启动，**不继承父进程已打的补丁**（每个子进程是全新解释器）；二是有些补丁依赖 worker 侧才能 import 的重型模块（如 `torch_npu`、模型实现），在引擎核心进程里过早 import 反而出错。

因此 vllm-ascend 设计了**两阶段补丁**：

- **Platform Patch（全局/平台阶段）**：`is_global_patch=True`，在引擎核心子进程生效，影响调度与配置等全局逻辑。
- **Worker Patch（worker 阶段）**：`is_global_patch=False`（默认），在每个 worker 子进程 `__init__` 时生效，影响模型前向与算子。

两阶段通过同一个函数 `adapt_patch(is_global_patch)` 分流，它就是整个补丁体系的「总闸」。

#### 4.2.2 核心流程

两阶段的触发点与作用范围可以用下面这张时序草图概括：

```text
vLLM 启动
  │
  ├─ 发现插件 → register() 返回 NPUPlatform
  │
  ├─ 通用插件回调（在 engine-core 子进程）
  │     register_connector / register_model_loader / ...
  │        └─→ _ensure_global_patch()   ← 幂等闸门
  │               └─→ adapt_patch(is_global_patch=True)
  │                     └─→ import vllm_ascend.patch.platform   ★ 平台补丁生效
  │                          （影响调度/KV cache/配置/权重传输后端 …）
  │
  └─ NPUPlatform.pre_register_and_update()
        └─→ adapt_patch(is_global_patch=True)   ★ 同一套平台补丁（再次幂等）

  ……spawn 出若干 worker 子进程（全新解释器，不继承上面的补丁）……

  每个 worker 子进程:
  └─ NPUWorker.__init__()
        └─→ adapt_patch()              # is_global_patch 默认 False
               └─→ import vllm_ascend.patch.worker    ★ worker 补丁生效
                    （影响模型 forward/采样/Triton/Graph …）
        └─→ from vllm_ascend import ops  （之后再加载模型）
```

要点：

1. **平台补丁可能被触发多次**（通用插件回调 + `pre_register_and_update`），所以需要**幂等**保证（4.2.3 的 `_ensure_global_patch`）。
2. **worker 补丁在每个 worker 子进程独立触发一次**，因为 `spawn` 子进程是全新解释器。
3. `adapt_patch` 本身不「打」补丁，它只是**触发对应子包的 import**；真正的「打补丁」发生在每个补丁模块的顶层代码里（import 即打补丁）。

#### 4.2.3 源码精读

先看「总闸」本体，非常简洁：

[vllm_ascend/utils.py:L533-L537](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L533-L537) —— `adapt_patch(is_global_patch=False)`：`is_global_patch=True` 时 import `vllm_ascend.patch.platform`，否则 import `vllm_ascend.patch.worker`。整个函数只做一件事——**决定触发哪个子包**。补丁的实际副作用藏在子包 `__init__.py` 的 import 链里。

再看平台阶段的两处触发点。

第一处：`NPUPlatform.pre_register_and_update`：

[vllm_ascend/platform.py:L183-L187](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L183-L187) —— 平台被选中后最早的钩子之一，直接调用 `adapt_patch(is_global_patch=True)`。

第二处：通用插件回调（`register_connector` / `register_model_loader` / `register_service_profiling`）经 `_ensure_global_patch` 触发：

[vllm_ascend/__init__.py:L56-L70](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L56-L70) —— `_ensure_global_patch()` 用模块级 `_GLOBAL_PATCH_APPLIED` 标志做**每进程一次的幂等闸门**：已打过就直接返回，否则调 `adapt_patch(is_global_patch=True)` 再置位。注释解释了原因——vLLM 在 engine-core 子进程里加载通用插件，而 E2E 测试的 conftest 钩子不会在那里运行，所以全局补丁必须也通过这些插件入口补打一次。

[vllm_ascend/__init__.py:L79-L86](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L79-L86) —— `register_connector` 第一行就是 `_ensure_global_patch()`，确保连接器、权重传输引擎相关的平台补丁已就绪。

再看 worker 阶段的触发点：

[vllm_ascend/worker/worker.py:L107-L113](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L107-L113) —— `NPUWorker.__init__` 在打印 `COMPILE_CUSTOM_KERNELS` 警告之后、`from vllm_ascend import ops` 与模型加载之前，调用 `adapt_patch()`（注意这里没传参，`is_global_patch` 默认 `False`，即 worker 阶段）。这个位置保证了模型加载时所有 worker 补丁已生效。

最后，两个子包 `__init__.py` 的 import 链展示了「import 即打补丁」的批量触发，并且都带有**条件分支**：

[vllm_ascend/patch/platform/__init__.py:L19-L46](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/__init__.py#L19-L46) —— 平台补丁聚合入口。注意三个条件分支：

- 第 26-29 行：按 `is_310p()` 在 `patch_mamba_config`（非 310P）与 `patch_mamba_config_310`（310P）之间二选一。
- 第 37-38 行：仅当 `DYNAMIC_EPLB` 或 `EXPERT_MAP_RECORD` 环境变量打开时才 import `patch_multiproc_executor`（否则不需要改子进程 daemon 行为）。
- 其余补丁无条件 import。

[vllm_ascend/patch/worker/__init__.py:L22-L49](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/__init__.py#L22-L49) —— worker 补丁聚合入口。同样有条件分支：

- 第 22-24 行：仅当 `HAS_TRITON` 为真时才打 Triton 相关补丁（CPU-UT 或 310P 无 Triton 环境下跳过）。
- 第 35-40 行：按 `is_310p()` 二选一地 import `patch_qwen3_5/patch_qwen3_dflash/patch_qwen3vl` 或 `patch_idex_310`。
- 第 46-49 行：`patch_npugraph_ex_triton` 用 `try/except ImportError` 容错，保证 CPU-only 环境（如无 `torch_npu` 的 UT runner）import 此模块时不会崩溃。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：追踪「spawn 子进程为何需要重打补丁」这条因果链。

**操作步骤**：

1. 读 `vllm_ascend/__init__.py` 第 56-70 行 `_ensure_global_patch` 的 docstring，找出它解释「为什么要在 engine-core 子进程里通过插件入口补打」的那句话。
2. 读 `vllm_ascend/worker/worker.py` 第 107-110 行，确认 worker 阶段补丁是在「每个 worker 自己的进程」里调用的。
3. 对照两处：平台补丁由引擎核心进程（经插件回调）触发，worker 补丁由 worker 进程（经 `__init__`）触发，二者进程不同。

**需要观察的现象**：你会看到平台补丁与 worker 补丁的触发点位于**不同的进程角色**里。

**预期结果**：你能解释——因为 `spawn` 出的 worker 是全新解释器、不继承父进程已打的补丁，所以 worker 必须在自己的 `__init__` 里重新调一次 `adapt_patch()`；而 `_ensure_global_patch` 的幂等标志只在「同一个进程内」有效，跨进程不共享。结论可直接从源码注释推出，**待本地验证**的是具体运行时各进程的日志输出。

#### 4.2.5 小练习与答案

**练习 1**：`adapt_patch(is_global_patch=True)` 和 `adapt_patch(is_global_patch=False)` 分别 import 哪个子包？为什么不合并成一个？

> **参考答案**：前者 import `vllm_ascend.patch.platform`，后者 import `vllm_ascend.patch.worker`。不合并是因为两者生效的进程角色与时机不同：平台补丁要在引擎核心进程、且在任何模型加载前生效，影响调度/配置等全局逻辑；worker 补丁要在每个 worker 子进程的 `__init__` 生效，影响前向/算子。合并会导致引擎核心进程被迫 import worker 侧的重型模块，或 worker 进程漏打平台补丁。

**练习 2**：`_ensure_global_patch` 里的 `_GLOBAL_PATCH_APPLIED` 标志能否防止「同一个 worker 子进程内重复打平台补丁」？

> **参考答案**：能防止「同一进程内」重复，但**跨进程无效**。`_GLOBAL_PATCH_APPLIED` 是模块级全局变量，`spawn` 出的子进程是全新解释器，该变量会被重置为 `False`。所以 worker 子进程若需要平台补丁，得靠自己进程里再次调用（这正是 worker 补丁聚合入口存在的意义）。

### 4.3 补丁文档化规范：读懂 patch/__init__.py 登记簿

#### 4.3.1 概念说明

由于补丁是「在运行时悄悄替换上游行为」，如果不加约束，很快会变成无人能维护的黑魔法。vllm-ascend 的做法是把 `patch/__init__.py` 变成一份**补丁登记簿**：每个补丁都必须在这里用固定格式记录五项信息：

- **补丁目标（What）**：替换了上游哪个函数/类。
- **原因（Why）**：为什么必须打这个补丁（上游缺什么、NPU 有什么特殊需求）。
- **做法（How）**：怎么打的、用户怎么开启。
- **关联 PR（Related PR）**：对应的上游 PR；若没有，说明为什么。
- **未来计划（Future Plan）**：在什么条件下可以移除这个补丁。

这份登记簿既是给读者的「地图」，也是给维护者的「退场清单」——当上游合并了对应 PR，就能按图索骥地删掉补丁。`AGENTS.md` 也明确要求：所有新补丁必须经过严格架构评审，验证「补丁目标正确、最小且聚焦、性能影响被理解、存在长期上游贡献计划」。

#### 4.3.2 核心流程

登记簿的整体结构是两大段注释：

```text
patch/__init__.py
├─ 模块说明：两个子目录 platform / worker 的职责与调用点
├─ * Platform Patch:        ← 平台补丁登记（按文件名字母序）
│     ** N. File: platform/patch_xxx.py **
│       1. <被替换的上游符号>
│       Why / How / Related PR / Future Plan
│     ...
└─ * Worker Patch:          ← worker 补丁登记（按文件名字母序）
      ** N. File: worker/patch_xxx.py **
        1. <被替换的上游符号>
        Why / How / Related PR / Future Plan
      ...
```

每个条目都遵循同一套五要素模板，便于检索与对照。

#### 4.3.3 源码精读

先看登记簿的「总说明」，它一句话点明两个子目录的分工与调用点：

[vllm_ascend/patch/__init__.py:L17-L27](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py#L17-L27) —— 说明 `platform/` 由 `adapt_patch(is_global_patch=True)` 在 `NPUPlatform.pre_register_and_update()` 中触发；`worker/` 由 `adapt_patch(is_global_patch=False)` 在每个 worker 的 `__init__` 中触发；并要求**新增补丁时必须同步在此登记**。

再看平台补丁段的开头与一个完整条目：

[vllm_ascend/patch/__init__.py:L29-L33](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py#L29-L33) —— 平台补丁段标题，并注明「按文件名字母序」排列。

[vllm_ascend/patch/__init__.py:L87-L104](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py#L87-L104) —— `patch_fused_moe.py` 的完整登记。注意它如何把五要素写齐：What（`FusedMoE` 工厂被重定向到 `AscendMoERunner`）、Why（FusedMoE 是工厂函数、模型直接 import 调用，必须在 import 前重定向）、How（同时改包与 layer 两处绑定，并说明 worker 侧 `patch_fused_moe.py` 复用此补丁以避免重复包装）、Related PR（无，vllm-ascend 专属集成）、Future Plan（等上游暴露 MoE runner 后端分发钩子后移除）。

最后看 worker 补丁段的开头与一个典型条目：

[vllm_ascend/patch/__init__.py:L560-L562](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py#L560-L562) —— worker 补丁段标题，同样按文件名字母序。

[vllm_ascend/patch/__init__.py:L564-L576](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py#L564-L576) —— `patch_cudagraph.py` 登记：What（替换 `CudagraphDispatcher._create_padded_batch_descriptor`）、Why（vLLM FULL 模式会报错，打补丁绕过后才能开启 FULL）、How（运行时替换该方法并改 if 条件）、Related PR（vLLM #34880）、Future Plan（上游合并后移除）。这是一个简洁标准的五要素范例。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：从 `patch/__init__.py` 里挑一个补丁，整理出它的「四要素」卡片。

**操作步骤**：

1. 打开 `vllm_ascend/patch/__init__.py`，在 Platform Patch 或 Worker Patch 段中**任选一个**补丁条目（例如上面的 `patch_cudagraph.py`，或 `patch_kv_cache_utils.py`、`patch_rejection_sampler.py` 等）。
2. 阅读该条目的注释，提炼出四要素：**补了什么（What）/ 为什么（Why）/ 关联上游 PR（Related PR）/ 何时可移除（Future Plan）**。
3. 再打开对应的补丁源文件（如 `vllm_ascend/patch/worker/patch_cudagraph.py`），核对登记簿描述与实际代码是否一致。

**需要观察的现象**：登记簿里的 Why/Future Plan 与代码里的实际替换目标应当对应得上。

**预期结果**：产出一张类似下面的小卡片（以 `patch_cudagraph.py` 为例）：

```text
补丁：worker/patch_cudagraph.py
- 补了什么：替换 vllm.v1.cudagraph_dispatcher.CudagraphDispatcher._create_padded_batch_descriptor
- 为什么：vLLM 的 FULL 模式在 NPU 上会报错，绕过后才能启用 FULL
- 关联 PR：vllm-project/vllm#34880
- 何时可移除：当 vLLM 合并该 PR 后即可删除
```

**注意**：不要假装运行了命令；本实践是源码阅读型，结论来自对注释与代码的对照阅读。

#### 4.3.5 小练习与答案

**练习 1**：登记簿里有些补丁的 Related PR 写「No, ...」并给了一段解释（如 `patch_fused_moe` 写「vllm-ascend-specific MoE runner integration」）。这说明什么？

> **参考答案**：并非所有补丁都对应一个现成的上游 PR。有些是 vllm-ascend 专属的能力（如自定义 MoE runner、310P 对齐），上游暂时没有对应改动。规范要求此时必须**解释为什么没有 PR**，而不是留空，这样维护者能判断该补丁是「等上游」还是「本就不应上游」。

**练习 2**：如果你要新增一个 worker 补丁，按规范应同时做哪两件事？

> **参考答案**：（1）在 `vllm_ascend/patch/worker/__init__.py` 里加一行 `import` 该补丁模块（必要时带条件分支）；（2）在 `patch/__init__.py` 的 Worker Patch 段按字母序补一条登记，写齐 What/Why/How/Related PR/Future Plan 五要素。

## 5. 综合实践

把本讲三个模块串起来，完成一次「补丁全链路阅读」小任务：

1. **选定一个补丁**：在 `patch/__init__.py` 中挑一个既有平台登记、又在代码里能找到实现的补丁（推荐 `platform/patch_fused_moe.py`，因为它同时出现在平台补丁链和 worker 补丁复用中）。
2. **整理五要素卡片**（4.3 的方法）。
3. **判定它属于哪个阶段**：它是 platform patch 还是 worker patch？依据是它登记在 `patch/__init__.py` 的哪一段，以及它的实际 import 是否出现在 `patch/platform/__init__.py` 或 `patch/worker/__init__.py` 中。
4. **追踪触发链**：写出「谁在哪个进程、调用了什么、最终 import 到这个补丁模块」的链路。例如对 `patch_fused_moe.py`，应是：
   - 平台阶段：`register_connector`（engine-core 子进程）→ `_ensure_global_patch()` → `adapt_patch(True)` → `import patch.platform` → `patch.platform/__init__` 第 45 行 `import patch_fused_moe`。
   - worker 阶段：`NPUWorker.__init__`（worker 子进程）→ `adapt_patch()` → `import patch.worker` → `patch.worker/__init__` 第 63 行 `import patch_fused_moe`（worker 侧复用平台补丁，避免重复包装）。
5. **写一句「何时可移除」的结论**：根据 Future Plan，说明在什么上游条件下这个补丁可以删掉。

通过这个任务，你会把「为什么打补丁 → 两阶段分流 → 登记规范 → 触发链」完整走一遍，为后续 u3-l2（Platform Patch 实战）和 u3-l3（Worker Patch 实战）打好基础。

## 6. 本讲小结

- vllm-ascend 用 **Monkey-patch** 在运行时替换上游 vLLM 的函数/类，实现「上游零侵入」的 NPU 适配；每个补丁遵循「捕获原对象 → 定义替换实现 → 重绑定到原模块」三步，且 `import` 补丁模块即生效。
- 补丁通过 **`adapt_patch(is_global_patch)`** 这个总闸分两阶段：`True` 触发 **platform 补丁**（引擎核心子进程、影响调度/配置等全局逻辑），`False` 触发 **worker 补丁**（每个 worker 子进程的 `__init__`、影响前向/算子）。
- 之所以要两阶段，是因为 vLLM 多进程架构下 `spawn` 出的 worker 是全新解释器、不继承父进程补丁，且不同进程关心的逻辑不同；`_ensure_global_patch` 提供「同进程一次」的幂等保证。
- `patch/__init__.py` 是补丁**登记簿**，每个补丁必须登记 **What / Why / How / Related PR / Future Plan** 五要素，既给读者当地图，也给维护者当退场清单；`AGENTS.md` 要求新补丁经过严格评审。
- 补丁的**时机**至关重要：必须在「上游真正取用该符号之前」完成替换，例如 `adapt_patch()` 在 `NPUWorker.__init__` 中早于 `from vllm_ascend import ops` 与模型加载。
- 子包 `__init__.py` 用 `is_310p()` / `HAS_TRITON` / 环境变量 / `try-except` 等条件分支，按硬件与运行环境**选择性**地触发补丁。

## 7. 下一步学习建议

- **u3-l2 Platform Patch 实战解析**：深入 `patch/platform/` 下影响调度、分布式、MoE、KV cache 的具体平台级补丁，理解它们为何必须在引擎核心进程生效。
- **u3-l3 Worker Patch 实战解析**：深入 `patch/worker/` 下与模型前向、投机解码、Triton 算子、Graph 相关的 worker 级补丁。
- 顺带可阅读 `AGENTS.md` 的 **Patching Requirement** 与 **NPU Considerations** 小节，了解贡献新补丁的评审清单与 `item()` 同步等 NPU 性能注意事项（与 u11-l5 二次开发实战呼应）。
