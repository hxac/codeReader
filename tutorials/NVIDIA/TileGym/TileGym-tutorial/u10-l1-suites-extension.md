# Suites 扩展机制（liger/flashinfer/unsloth）

## 1. 本讲目标

本讲回答一个问题：**当 TileGym 自带的「核心算子」不够用时，怎样在不污染核心接口的前提下，成体系地塞进一整套「外部内核库」的算子？**

答案是 **Suites（套件）机制**。读完本讲，你应当能够：

- 理解「命名空间算子名」（如 `liger.cross_entropy`、`flashinfer.gemm.gemm_alpha_beta`）在全局注册表里的含义。
- 掌握每个 suite 的 `ops.py` 统一接口与 `cutile/` 后端实现目录之间的关系。
- 区分 `suites/` 与核心 `ops/` 在「声明位置、是否自动加载、是否有 fallback」上的关键差异。
- 读懂 liger suite 当前的算子目录，知道它如何通过一个新增的 `liger/ops.py` 暴露统一接口。

本讲默认你已经学过 **u2-l2（后端注册表与分发机制 dispatcher.py）**：你知道 `_REGISTRY` 是 `{算子名: {后端: 实现}}` 的全局嵌套字典、`register_impl` 把实现挂进字典、`dispatch` 装饰器返回的 `wrapper` 按当前后端查表。本讲**不再重复**这套机制本身，而是讲它如何被「套上一层命名空间」复用到外部内核库上。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**① 什么是「外部内核库」？** TileGym 自身有一套「核心算子」（softmax、matmul、fmha 等，写在 `src/tilegym/ops/`）。但业界还有若干知名的开源 GPU 内核库，它们各自定义了一大批算子（损失函数、归一化、激活、量化 GEMM……），例如：

- **Liger-Kernel**（LinkedIn 出品）：偏训练侧，提供大量「融合损失 + 归一化 + 激活」内核，省显存、省 kernel launch。
- **FlashInfer**：偏推理侧，提供分页 / 不规则（ragged）注意力、量化 GEMM 等。
- **Unsloth**：偏训练加速，提供融合的前向 / 反向内核。

TileGym 不想把这些库「硬编码」进核心，而是希望：**用 cuTile 重新实现它们的算子，再以「可插拔套件」的方式挂进 TileGym**。这就是 suites。

**② 为什么需要「命名空间前缀」？** 核心算子的名字是裸字符串（`"softmax"`、`"matmul"`），它们是全局注册表的键。如果 liger 也想叫自己的算子 `softmax`、`rms_norm`、`rope`，就会和核心的**同名算子撞键**，导致一个键被两个库的实现瓜分、语义混乱。解决办法：给每个 suite 的算子名加一个 **`.` 分隔的命名空间前缀**——`liger.softmax`、`liger.rms_norm`、`flashinfer.attention.decode_attention_kv_paged`。前缀让「同名的不同实现」在注册表里各自独立，互不覆盖。

**③ 命名空间只是字符串约定，不是新机制。** 这是最关键的一点：dispatcher 完全不知道 `liger.cross_entropy` 里的那个 `.` 有什么特殊含义。对它来说，`"liger.cross_entropy"` 就是一个**普通的字典键**，与 `"softmax"` 没有任何区别。分发查找逻辑一行都没改。suite 机制 = 「复用同一套 `@dispatch`/`register_impl` 机制 + 约定带前缀的名字」。理解了这一点，后面所有源码都会非常直白。

> 关键术语回顾：`_REGISTRY`（全局注册表）、`register_impl`（注册后端实现）、`dispatch`（声明 stub + 查表 wrapper）、stub（只抛 `NotImplementedError` 的占位函数）、`fallback_backend`（缺失时降级到哪个后端）。详见 u2-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/suites/liger/ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py) | liger suite 的**统一算子接口**：每个算子用 `@dispatch("liger.xxx")` 声明一个 stub。本讲的「统一接口」主样本。 |
| [src/tilegym/suites/liger/__init__.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/__init__.py) | liger suite 的**加载入口**：在 `is_backend_available("cutile")` 门控下导入 cutile 实现完成注册，并从 `ops.py` re-export 统一接口。 |
| [src/tilegym/suites/flashinfer/ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/flashinfer/ops.py) | flashinfer suite 的统一接口，展示**多段命名空间**（`flashinfer.gemm.gemm_alpha_beta`）。 |
| [src/tilegym/suites/liger/cutile/cross_entropy.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py) | 一个具体后端实现样本：`@register_impl("liger.cross_entropy", backend="cutile")` 把真实内核挂到带前缀的名字上。 |
| [src/tilegym/suites/liger/cutile/__init__.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/__init__.py) | liger 的 **cutile 后端实现目录聚合点**：一次性 import 所有内核模块，触发它们的 `register_impl` 副作用。 |
| [src/tilegym/suites/__init__.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/__init__.py) | suites 顶层包，提供 `list_available()` 列出当前可加载的套件。 |
| [src/tilegym/backend/dispatcher.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/backend/dispatcher.py) | 分发器本体（u2-l2 已详解）。本讲引用它，只为证明「命名空间对它透明」。 |

## 4. 核心概念与源码讲解

### 4.1 Suite 命名空间算子名

#### 4.1.1 概念说明

「命名空间算子名」就是把算子的全局键写成带点号前缀的字符串，形如：

- `liger.cross_entropy`
- `liger.grpo_loss`
- `flashinfer.gemm.masked_bmm`
- `unsloth.swiglu_fg`

前缀（第一段，如 `liger`/`flashinfer`/`unsloth`）标识算子属于哪个套件；点号 `.` 仅作分隔，**dispatcher 不解析它**。第二段（及第三段）是该套件内部的算子名。

它解决的真正问题是**命名冲突**：核心 ops 已经占用了 `softmax`、`rms_norm`、`rope` 这些短名字；而 Liger-Kernel、Unsloth 这些外部库又恰恰都有自己的 `softmax`/`rms_norm`/`rope` 实现，且**语义并不完全相同**（例如 liger 的 `rms_norm` 多了 `casting_mode`、`in_place` 等训练向参数）。加前缀后，`"softmax"`（核心）与 `"liger.softmax"`（套件）、`"unsloth.swiglu_fg"` 各占一个独立键，互不覆盖，调用方也不会把「核心的 softmax」和「liger 的 softmax」搞混。

#### 4.1.2 核心流程

一个带前缀算子名从「声明」到「被调用」的完整生命周期：

1. **声明 stub**：在 `suites/<name>/ops.py` 里写一个 `@dispatch("liger.cross_entropy")` 装饰、函数体只 `raise NotImplementedError` 的占位函数。
2. **挂入注册表**：`@dispatch` 装饰器执行时，把该 stub 作为 `"default"` 实现写入 `_REGISTRY["liger.cross_entropy"]`（与核心算子用的是**同一个全局字典**）。
3. **注册后端实现**：在 `suites/<name>/cutile/cross_entropy.py` 写真实内核，用 `@register_impl("liger.cross_entropy", backend="cutile")` 挂到同一个键的 `"cutile"` 子键。
4. **调用**：`from tilegym.suites import liger; liger.cross_entropy(...)` → wrapper 拿当前后端 `cutile` → 查 `_REGISTRY["liger.cross_entropy"]["cutile"]` → 命中真实内核。

伪代码（注册表视角）：

```text
_REGISTRY = {
  "softmax":                 {"default": core_stub, "cutile": core_impl, "tilecpp": ...},   # 核心
  "liger.cross_entropy":     {"default": liger_stub, "cutile": liger_cutile_impl},          # liger 套件
  "liger.grpo_loss":         {"default": liger_stub, "cutile": liger_cutile_impl},
  "flashinfer.gemm.gemm_alpha_beta": {"default": fi_stub, "cutile": fi_cutile_impl},
  ...
}
```

前缀只是键字符串的一部分；字典里核心算子与套件算子**平起平坐**，没有任何特殊分支。

#### 4.1.3 源码精读

liger suite 的统一接口文件开头导入分发器，并声明第一个带前缀算子 `liger.jsd`：

[liger/ops.py:16-22](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L16-L22) —— 导入 `dispatch` / `get_current_backend`，并用 `@dispatch("liger.jsd")` 声明一个只抛 `NotImplementedError` 的 stub。注意：这里只传了算子名字符串，**没有** `fallback_backend` 参数。

实战目标算子 `cross_entropy` 也是同样套路：

[liger/ops.py:95-98](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L95-L98) —— `@dispatch("liger.cross_entropy")` 声明 stub，函数名 `cross_entropy` 与算子名里的最后一段一致（这只是为了可读性，并非必须）。

flashinfer 进一步展示**两段前缀** `flashinfer.gemm.xxx` / `flashinfer.attention.xxx`：

[flashinfer/ops.py:25-28](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/flashinfer/ops.py#L25-L28) —— `@dispatch("flashinfer.gemm.gemm_alpha_beta")`，把 GEMM 族算子归到 `flashinfer.gemm.` 子前缀下。两段前缀只是字符串，dispatcher 同样不解析。

**最重要的证据**——dispatch 装饰器的实现里，对「带不带点号」一视同仁：

[dispatcher.py:95-97](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/backend/dispatcher.py#L95-L97) —— 查表就一句 `if name in _REGISTRY and current_backend in _REGISTRY[name]:`。`name` 是 `"liger.cross_entropy"` 还是 `"softmax"`，对这行代码没有任何区别。整个分发器没有一行针对 `.` 的特殊处理。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，无需 GPU，目标是亲手验证「命名空间只是普通字符串键」。

1. **实践目标**：确认 `liger.cross_entropy` 在注册表里与核心 `softmax` 是同级键，且 dispatcher 没有针对前缀的特判。
2. **操作步骤**：
   - 打开 [dispatcher.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/backend/dispatcher.py)，通读 `dispatch`（60 行起）与 `register_impl`（34 行起）两个函数，统计其中出现 `.` 字符串切分或 `startswith` 之类「前缀解析」的代码行数。
   - 在能 import `tilegym` 的环境里（无需真 GPU，但要能 `import cuda.tile`；若无，跳过本步标注「待本地验证」）执行：
     ```python
     import tilegym
     from tilegym.suites import liger          # 必须显式导入，否则 stub 与实现都没注册
     from tilegym.backend import get_registry_info
     info = get_registry_info()
     print("liger.cross_entropy ->", info.get("liger.cross_entropy"))
     print("softmax            ->", info.get("softmax"))
     ```
3. **需要观察的现象**：`liger.cross_entropy` 与 `softmax` 是**两个独立键**，各自挂着自己的 `default`/`cutile` 实现；它们在 `info` 字典里平级出现。
4. **预期结果**：dispatcher 中「前缀解析」相关代码行数 = 0。`info["liger.cross_entropy"]` 形如 `{"default": "tilegym.suites.liger.ops.cross_entropy", "cutile": "tilegym.suites.liger.cutile.cross_entropy.cross_entropy"}`（具体模块路径以本地为准）。
5. 若本地无 GPU 或 `cuda.tile` 不可导入：**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：核心 ops 里已有一个算子叫 `"rms_norm"`，liger suite 又声明了 `"liger.rms_norm"`。如果某人手滑把 liger 的 `@dispatch` 写成了 `@dispatch("rms_norm")`（漏掉前缀），会发生什么？

**答案**：两个 stub 会被 `@dispatch` 写进**同一个键** `_REGISTRY["rms_norm"]` 的 `"default"` 子键，后声明的覆盖先声明的；后端实现也会挤到一起。结果是「核心 rms_norm 与 liger rms_norm 的实现互相覆盖 / 混用」，语义错乱。这正是前缀要避免的灾难，也反过来说明前缀是**必需的字符串约定**。

**练习 2**：算子名 `flashinfer.gemm.gemm_alpha_beta` 里有两层 `.`，`flashinfer.attention.decode_attention_kv_paged` 也有两层。这两层前缀是被 dispatcher 解析的吗？

**答案**：不是。整个名字对 dispatcher 就是一个不透明字符串键，`.` 既不分层也不被解析。两段前缀只是给人读、给目录归类的约定（`flashinfer` 套件下有 `gemm`/`attention`/`rope`/`quant` 等子目录概念），机器侧它和一个点都没有的键完全等价。

---

### 4.2 Suite 统一接口 ops.py 与后端实现目录

#### 4.2.1 概念说明

每个 suite 都遵循一个固定的「**两层结构**」：

- **统一接口层** `suites/<name>/ops.py`：列出该套件**所有**算子的 stub（带前缀名、统一签名、docstring、只抛 `NotImplementedError`）。它的角色和核心的 `ops/ops.py` 完全对称——**只定义接口，不算结果**。
- **后端实现层** `suites/<name>/<backend>/`：每个后端一个目录（目前主要是 `cutile/`），目录下**一个算子一个 `.py` 文件**，每个文件用 `@register_impl` 把真实内核挂到 `ops.py` 里声明的那个带前缀名字上。

这套「接口在 `ops.py`、实现在 `cutile/` 目录、靠 `register_impl` 对齐」的组织，正是 u2-l2 讲过的「接口—分发—实现」三层架构的**直接复用**，只是搬到了 `suites/` 子树下。

#### 4.2.2 核心流程

以「给 liger 新增一个 cuTile 实现」为例，注册的发生顺序：

1. 用户代码 `from tilegym.suites import liger` 触发 [liger/__init__.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/__init__.py)。
2. `__init__.py` 第 17-18 行在 `is_backend_available("cutile")` 门控下 `from . import cutile as _cutile_impl`。
3. `cutile/__init__.py` 依次 `from . import cross_entropy`、`from . import dyt`、… 把所有实现模块导入一遍。
4. 每个模块顶层的 `@register_impl("liger.xxx", backend="cutile")` 装饰器在导入时执行，把实现写进 `_REGISTRY["liger.xxx"]["cutile"]`。
5. 与此同时，`__init__.py` 第 21-43 行从 `.ops` re-export 所有 stub，于是 `liger.cross_entropy` 这个名字在用户侧可用，且其 wrapper 已经能在注册表里查到 cutile 实现。

关键点：**注册是「导入副作用」**，整个套件的实现挂载完全由「是否 import 了 `cutile` 子包」决定，受 `is_backend_available` 门控——和核心 ops 的注册门控（u2-l2、u9-l2）是同一套思路。

#### 4.2.3 源码精读

liger 的加载入口，门控 + 注册 + re-export 三件事一次完成：

[liger/__init__.py:13-18](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/__init__.py#L13-L18) —— `is_backend_available("cutile")` 为真才导入 cutile 实现；若机器上 cuTile 不可用，则**不注册任何后端实现**，套件只剩会抛 `NotImplementedError` 的 stub。

[liger/__init__.py:21-43](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/__init__.py#L21-L43) —— 从 `.ops` 把所有 stub re-export 出来，构成 `liger.cross_entropy`、`liger.grpo_loss` 等用户可见的调用入口。

cutile 后端实现目录的聚合点，逐个 import 触发注册副作用：

[liger/cutile/__init__.py:7-29](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/__init__.py#L7-L29) —— 一连串 `from . import cross_entropy  # noqa: F401`。`# noqa: F401` 表示「这些 import 不是为了拿到名字，而是为了触发它们模块顶层的 `@register_impl`」。这正是 u9-l2 讲过的「注册即导入副作用」。

一个具体后端实现，挂到带前缀的键上：

[liger/cutile/cross_entropy.py:509-510](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py#L509-L510) —— `@register_impl("liger.cross_entropy", backend="cutile")` 把这个真实内核挂到 `_REGISTRY["liger.cross_entropy"]["cutile"]`。注意它的**第二个参数 `backend="cutile"` 与核心算子的注册写法完全相同**——这是 suite 复用核心机制的铁证。

flashinfer 套件的门控更「防御性」一些，多套了一层 try/except：

[flashinfer/__init__.py:34-41](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/flashinfer/__init__.py#L34-L41) —— 即使 `is_backend_available("cutile")` 为真，导入 cutile 实现时若失败（如某个内核模块编译报错），也会降级为 `cutile = None` 并发出告警，而不是让整个套件崩掉。这体现了推理侧套件「尽量不连累主流程」的工程取向。

#### 4.2.4 代码实践

源码阅读 + 结构对照型实践。

1. **实践目标**：验证「一个套件的『接口在 ops.py、实现在 cutile/ 目录、靠 register_impl 对齐』三件套是否齐备且键名一致」。
2. **操作步骤**：
   - 在 `src/tilegym/suites/liger/cutile/` 目录里，用搜索工具统计 `@register_impl("liger.` 的出现次数与对应的算子名集合 A（提示：本仓库 liger cutile 目录下共有 23 处这样的注册）。
   - 在 `src/tilegym/suites/liger/ops.py` 里统计 `@dispatch("liger.` 声明的算子名集合 B（共 23 个）。
   - 求两个集合的差集 `A − B` 与 `B − A`。
3. **需要观察的现象**：理想情况下 `A == B`（每个有实现的算子都有 stub，每个 stub 都有 cutile 实现）。
4. **预期结果**：若出现 `B − A` 非空（某 stub 暂无 cutile 实现），说明该算子当前调用会落到 `default` 实现而抛 `NotImplementedError`；若出现 `A − B` 非空（有实现却没声明 stub），则该实现无法被 dispatch 找到（孤儿实现）。读者据差集自行判断哪些是「尚未实现」、哪些是「声明缺失」。
5. 本步纯静态分析，无 GPU 也能完成；结论以本地仓库为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `liger/cutile/__init__.py` 里每个 import 都带 `# noqa: F401`？如果删掉这些 import，会发生什么？

**答案**：这些 import 的目的不是「在 `__init__.py` 里使用这些名字」，而是「触发被导入模块顶层的 `@register_impl` 装饰器执行」，从而把实现写进 `_REGISTRY`。`# noqa: F401` 告诉 linter「我知道这些名字没在本文件里被使用，别报警」。删掉它们 → 相应模块不被导入 → `@register_impl` 不执行 → `_REGISTRY["liger.xxx"]["cutile"]` 不存在 → 调用该算子时 wrapper 查不到 cutile 实现，只能落到 `default` 抛 `NotImplementedError`。

**练习 2**：flashinfer 的 `__init__.py` 把 cutile 导入包在 `try/except (ImportError, RuntimeError)` 里，liger 的却没有（只用 `is_backend_available` 门控）。这两种写法各自的好处是什么？

**答案**：flashinfer 面向推理侧，套件大、内核多，单个内核模块加载失败不应让整个套件不可用，故「尽力加载、失败降级为 `cutile=None` 并告警」，更鲁棒。liger 相对收敛，直接靠 `is_backend_available` 二元门控即可；若想更鲁棒也可照搬 try/except，但当前实现选择了简单。两者都符合 suite「可插拔、不连累核心」的原则。

---

### 4.3 liger 算子目录与统一接口

#### 4.3.1 概念说明

liger 是当前**最活跃、覆盖最广**的套件，定位偏**训练侧**。它把 Liger-Kernel 的训练损失、归一化、激活等算子用 cuTile 重写，并通过**新增的 `liger/ops.py`** 暴露统一接口。理解 liger，等于理解了「一个套件长什么样、能做什么」。

liger 当前的算子目录可大致分为四族：

| 族 | 代表算子 | 说明 |
| --- | --- | --- |
| **训练损失** | `grpo_loss`、`cross_entropy`、`fused_linear_cross_entropy`、`jsd`、`fused_linear_jsd`、`kl_div`、`tvd` | 损失与蒸馏，含策略梯度（GRPO/DAPO/GSPO…）、融合线性交叉熵（不物化 logits）、JSD/KL/TVD 散度。 |
| **归一化** | `rms_norm`、`fused_add_rms_norm`、`layer_norm`、`group_norm`、`poly_norm`、`dyt` | 含残差融合、PolyCom 多项式归一化、Dynamic Tanh 等变体。 |
| **激活** | `swiglu`、`geglu`、`softmax`、`sparsemax` | 门控激活与稀疏化 softmax。 |
| **位置编码 / 注意力 / MLP** | `rope`、`llama4_rope`、`qwen2vl_mrope`、`multi_token_attention`、`fused_neighborhood_attention`、`tiled_mlp` | 各种 RoPE 变体、邻域注意力、分片 MLP。 |

其中 `grpo_loss`、`fused_linear_cross_entropy`、`fused_add_rms_norm`、`poly_norm`、`dyt`、`rms_norm`、`softmax`、`swiglu`、`tvd` 等是本轮 liger suite 扩容重点纳入的训练向算子（具体哪几个算子属于哪个提交，以本地 `git log src/tilegym/suites/liger/` 为准）。

#### 4.3.2 核心流程

调用一个 liger 算子的最短路径（以 `cross_entropy` 为例）：

```text
from tilegym.suites import liger       # 1) 触发 __init__.py：门控导入 cutile 实现 + re-export stub
liger.cross_entropy(logits, target)    # 2) 调 stub 的 wrapper
   └─ wrapper 取当前后端 cutile
   └─ 查 _REGISTRY["liger.cross_entropy"]["cutile"]
   └─ 命中 cutile/cross_entropy.py 里的 cross_entropy()
   └─ 内部走 CrossEntropyCuTileFunction.apply(...)（autograd Function，前向+反向）
```

注意第三步的「当前后端」——liger 算子和核心算子**共用同一个进程级后端**（`_CURRENT_BACKENDS`，默认 `cutile`）。所以一般不需要为 liger 单独切后端；若要临时覆盖，可用调用级 `backend=` 参数（由 wrapper 的 `kwargs.pop("backend", None)` 拦截，见 u2-l2）。

#### 4.3.3 源码精读

liger 统一接口的开头与三个代表性 stub：

[liger/ops.py:20-22](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L20-L22) —— `@dispatch("liger.jsd")` 声明 Jensen-Shannon 散度损失 stub。

[liger/ops.py:326-328](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L326-L328) —— `@dispatch("liger.dyt")` 声明 Dynamic Tanh 激活 stub。

[liger/ops.py:710-712](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L710-L712) —— `@dispatch("liger.tvd")` 声明总变差距离损失 stub。

策略梯度损失 `grpo_loss` 是 liger 里**签名最复杂**的算子之一，是本轮扩容的代表性新增项：

[liger/ops.py:441-444](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L441-L444) —— `@dispatch("liger.grpo_loss")`，stub 支持多种 RL 变体（`grpo`/`dapo`/`bnpo`/`dr_grpo`/`cispo`/`sapo`/`luspo`/`vespo`）。

与之对应的 cutile 实现注册：

[liger/cutile/grpo_loss.py:1227](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/grpo_loss.py#L1227) —— `@register_impl("liger.grpo_loss", backend="cutile")`，把策略梯度损失的真实内核挂到带前缀的键上。

最后，[liger/__init__.py:45-69](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/__init__.py#L45-L69) 的 `__all__` 就是 liger 对外暴露的「算子目录清单」，读者可对照它快速了解 liger 当前到底提供哪些算子。

#### 4.3.4 代码实践

源码阅读型实践（无需 GPU）。

1. **实践目标**：用 liger 的 `cross_entropy` 把「stub → 实现」的链路走一遍，并验证 liger 套件的「损失 + 归一化 + 激活」三族是否齐备。
2. **操作步骤**：
   - 打开 [liger/ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py)，把所有 `@dispatch("liger.xxx")` 的算子名抄下来，按「损失 / 归一化 / 激活 / 其它」分类。
   - 对照 [liger/cutile/__init__.py:7-29](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/__init__.py#L7-L29)，确认每个 stub 是否都有对应的 cutile 实现模块。
   - 在 [liger/cutile/cross_entropy.py:509-563](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py#L509-L563) 阅读实现：它把请求转发给 `CrossEntropyCuTileFunction.apply(...)`（一个 `torch.autograd.Function`），并根据 `return_*` 标志决定返回标量还是 4-tuple。
3. **需要观察的现象**：liger 的「损失」族（`grpo_loss`/`cross_entropy`/`fused_linear_cross_entropy`/`jsd`/`kl_div`/`tvd` 等）大多走 autograd Function，即同时实现了前向与反向——这是训练向套件的典型特征。
4. **预期结果**：得到一张「liger 算子目录表」，至少能列出 20+ 算子名，且每个都能在 `cutile/` 下找到同名实现文件。
5. 若想真正运行 `liger.cross_entropy`：需要 cu130/torch/cuda.tile 环境，**待本地验证**。无 GPU 时仅完成阅读与列表。

#### 4.3.5 小练习与答案

**练习 1**：`liger.cross_entropy` 的 stub 在 [liger/ops.py:95-137](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L95-L137)，函数体只抛 `NotImplementedError`。但在 [liger/cutile/cross_entropy.py:509-563](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py#L509-L563) 又有一个完整的 `cross_entropy`。请解释：用户调 `liger.cross_entropy(...)` 时，到底执行的是哪一个？

**答案**：执行的是 **cutile 实现那个**。`liger.cross_entropy` 这个名字指向的是 stub 被 `@dispatch` 包装后的 `wrapper`。`wrapper` 自身不算结果，它先取当前后端（`cutile`），再去 `_REGISTRY["liger.cross_entropy"]["cutile"]` 取出 cutile 实现并调用。stub 函数体只会在「查不到任何后端实现、且允许走 default」时作为兜底被执行（此时抛 `NotImplementedError`）。所以 stub 是「接口契约」，实现是「真正干活的人」，二者同名但通过注册表解耦。

**练习 2**：liger 的损失算子（如 `cross_entropy`、`grpo_loss`）大多返回带 `return_*` 标志的可选输出，且通过 `torch.autograd.Function` 实现。这和「核心 ops 里的 softmax（仅前向）」相比，反映了 liger 的什么定位？

**答案**：liger 定位于**训练侧**——损失算子必须能反向传播梯度，且常把「前向 + 梯度计算」融合进单个内核（Liger 式 fused forward+backward）以省显存与 launch。因此 liger 的损失/归一化族普遍带 autograd Function 与反向内核；而核心 ops 里不少展示型算子（softmax）当前只实现前向。这是 suite 与核心在「算子族取舍」上的功能差异，而非机制差异。

---

### 4.4 Suite 与核心 ops 的异同

#### 4.4.1 概念说明

讲到这里，结论已经很清楚：**机制上，suite 与核心 ops 完全同构**——都用 `@dispatch` 声明 stub、都用 `register_impl` 挂实现、都用同一个全局 `_REGISTRY`、都用同一个 wrapper 按当前后端查表。命名空间前缀只是字符串约定，dispatcher 不识别。

但在**工程组织**上，二者有四个关键差异，决定了「为什么要把它们分开存放」：

1. **位置**：核心 stub 在 `src/tilegym/ops/ops.py`；suite stub 在 `src/tilegym/suites/<name>/ops.py`。
2. **加载方式**：核心 ops 在 `import tilegym` 时**自动**加载（`ops/__init__.py` 的 `from .ops import *`）；suite 必须**显式** `from tilegym.suites import liger` 才加载——核心包的 `__init__.py` 完全不导入 `suites`。
3. **fallback 策略**：核心 ops 里不少算子设了 `fallback_backend="triton"`，能优雅降级；suite 的 stub **几乎都不设** `fallback_backend`（用默认 `"pytorch"`），而又没有注册 pytorch 实现，所以「缺失即报错」，是严格后端门控。
4. **语义对齐**：核心 ops 是「TileGym 自研算子」；suite 是「对某外部库（Liger/FlashInfer/Unsloth）的算子用 cuTile 重新实现」，算子名与签名尽量对齐上游库。

#### 4.4.2 核心流程

把上述差异列成对照表，便于记忆：

| 维度 | 核心 ops（`ops/ops.py`） | Suite（`suites/<name>/ops.py`） |
| --- | --- | --- |
| 算子名 | 裸名 `"softmax"` `"matmul"` | 带前缀 `"liger.softmax"` `"flashinfer.gemm.xxx"` |
| 注册机制 | `@dispatch` + `register_impl` | **完全相同** |
| 全局注册表 | 共用 `_REGISTRY` | **共用同一个 `_REGISTRY`** |
| 加载时机 | `import tilegym` 自动加载 | 显式 `from tilegym.suites import <name>` |
| fallback | 常设 `fallback_backend="triton"` | 多用默认（无优雅降级，缺失即报错） |
| 后端实现目录 | `ops/cutile/` `ops/tilecpp/` … | `suites/<name>/cutile/`（形态同构） |
| 来源 | TileGym 自研 | 对齐 Liger-Kernel / FlashInfer / Unsloth |

一句话：**机制同构、组织分离**。分离是为了「核心精简、套件可插拔、命名不冲突」。

#### 4.4.3 源码精读

证明「核心包不自动加载 suites」：

通过搜索可确认 `src/tilegym/__init__.py` 里**完全没有** `suites` 字样（即不 import suites）。这意味着 `import tilegym` 之后，`_REGISTRY` 里**还没有**任何 `liger.*` / `flashinfer.*` 键——只有显式 `from tilegym.suites import liger` 才会触发 stub 与实现的注册。这是 suite 与核心 ops 在加载时机上最硬的区别。

对比「核心 ops 用 fallback、suite 不用」：

[ops/ops.py:27-31](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/ops.py#L27-L31) —— 核心 `get_apply_rope_func` 显式带 `fallback_backend="triton"`，主后端缺失时可降级到 triton（详见 u2-l1、u2-l3）。

而 liger 的 stub：

[liger/ops.py:95-97](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L95-L97) —— `@dispatch("liger.cross_entropy")` **只传了名字**，没有 `fallback_backend`，故用 [dispatcher.py:60](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/backend/dispatcher.py#L60) 的默认值 `"pytorch"`。由于 liger 没有注册 pytorch 实现，cuTile 不可用时调用会直接落到 `default` stub 抛 `NotImplementedError`，而不会悄悄降级。flashinfer、unsloth 同理（本仓库三个 suite 的 ops.py 中 `fallback_backend` 出现次数均为 0）。

suite 顶层包提供「列出可用套件」的辅助函数：

[suites/__init__.py:21-34](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/__init__.py#L21-L34) —— `list_available()` 尝试导入三个套件，返回能成功导入的名字列表。它是「套件层」独有的自省入口（核心 ops 没有对应的「list_available_ops」）。

#### 4.4.4 代码实践（本讲综合实践任务）

这是本讲的主实践任务，**选 `liger.cross_entropy`** 回答三个问题。

1. **实践目标**：
   (a) 说明它的算子名为何带 `"liger."` 前缀；
   (b) 说明它与核心 `ops.py` 中算子的 `register_impl` 注册方式有何相同与不同；
   (c) 列出本轮 `liger/ops.py` 新增的若干算子名。
2. **操作步骤**：
   - 阅读 [liger/ops.py:95-137](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py#L95-L137)（`cross_entropy` stub）与 [liger/cutile/cross_entropy.py:509-510](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py#L509-L510)（cutile 实现）。
   - 对照 [ops/ops.py:44-48](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/ops.py#L44-L48)（核心 `apply_rope_base` stub）。
   - 用 `git log --oneline -- src/tilegym/suites/liger/ops.py` 查看本套件接口文件的演进提交，确认本轮扩容的算子。
3. **参考答案（即预期结论）**：
   - **(a) 为何带前缀**：核心 ops 已占用 `cross_entropy`/`softmax`/`rms_norm` 等短名，且 liger 版语义（多了 `lse_square_scale`、`softcap`、`return_z_loss` 等训练向参数）与核心不同。前缀 `liger.` 让两者在全局 `_REGISTRY` 里各占独立键，避免覆盖与混淆。
   - **(b) 相同点**：liger cutile 实现的注册写法 `@register_impl("liger.cross_entropy", backend="cutile")` 与核心算子注册**完全相同**（同样的装饰器、同样的 `backend="cutile"` 子键、同样写进同一个 `_REGISTRY`、同样作为「导入副作用」在 `__init__.py` 里被门控触发）。**不同点**：① 名字带前缀；② stub 不设 `fallback_backend`（无优雅降级）；③ 实现位于 `suites/liger/cutile/` 而非 `ops/cutile/`；④ 需显式 `from tilegym.suites import liger` 才加载。
   - **(c) 本轮 liger/ops.py 新增算子名（参考）**：`liger.dyt`、`liger.fused_add_rms_norm`、`liger.fused_linear_cross_entropy`、`liger.grpo_loss`、`liger.poly_norm`、`liger.rms_norm`、`liger.softmax`、`liger.swiglu`、`liger.tvd` 等（具体集合以本地 `git log`/`git diff` 为准）。
4. **需要观察的现象**：liger 套件的「训练损失 + 归一化 + 激活」族在本轮明显扩容，且全部走「`ops.py` 声明 stub + `cutile/` 实现」的同一套骨架。
5. 本实践为静态阅读，无 GPU 也能完成全部三个问题。

#### 4.4.5 小练习与答案

**练习 1**：`import tilegym` 之后，立刻 `tilegym.ops.softmax(...)` 能用，但 `tilegym.suites.liger.cross_entropy(...)` 不一定能用（要先 `from tilegym.suites import liger`）。请用本讲的「加载时机」差异解释。

**答案**：核心 ops 在 `import tilegym` 时被 `ops/__init__.py` 自动加载，故 `softmax` 的 stub 与 cutile 实现已注册进 `_REGISTRY`，可直接调用。而 `suites` 子包**不在**核心包 `__init__.py` 的导入链里，`import tilegym` 不会触发任何 suite 的 stub/实现注册；必须显式 `from tilegym.suites import liger`，才会运行 `liger/__init__.py`，进而门控导入 cutile 实现、注册带前缀算子。这是「核心自动、套件按需」的设计。

**练习 2**：假如你想给 liger 的某个算子加上「cutile 不可用时降级到 triton」的能力，最少要改哪一处？

**答案**：把 [liger/ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/ops.py) 里该算子 stub 的 `@dispatch("liger.xxx")` 改成 `@dispatch("liger.xxx", fallback_backend="triton")`，并在 `suites/liger/triton/` 下补一个用 `@register_impl("liger.xxx", backend="triton")` 注册的 triton 实现（并让 `liger/__init__.py` 在 `is_backend_available("triton")` 时导入它）。机制层面无需改动 dispatcher——这正说明 suite 与核心 ops 共用同一套 fallback 机制。

---

## 5. 综合实践

把本讲四个最小模块串成一个任务：**为一个「假想的新套件 `foo`」搭出最小骨架**（仅设计，不必真写内核）。

要求：

1. 在 `src/tilegym/suites/foo/ops.py` 里，为一个新算子 `foo.bar` 写出 stub（`@dispatch("foo.bar")`、统一签名、docstring、`raise NotImplementedError`）。
2. 在 `src/tilegym/suites/foo/cutile/bar.py` 里，写出注册骨架：`@register_impl("foo.bar", backend="cutile")`，函数体先 `raise NotImplementedError("待实现")`（标注「示例代码」）。
3. 在 `src/tilegym/suites/foo/cutile/__init__.py` 里，写 `from . import bar  # noqa: F401`。
4. 在 `src/tilegym/suites/foo/__init__.py` 里，复刻 liger 的模式：`is_backend_available("cutile")` 门控导入 cutile 实现，并 `from .ops import bar`。
5. 画出调用链：`from tilegym.suites import foo` → `foo.bar(...)` → wrapper 查 `_REGISTRY["foo.bar"]["cutile"]` → `cutile/bar.py` 的 `bar()`。

完成后，回答：你的 `foo.bar` 与核心 `ops.softmax` 共享了哪几样东西？（答案：`@dispatch` 装饰器、`register_impl` 装饰器、全局 `_REGISTRY`、wrapper 的查表逻辑、进程级当前后端。）又有哪些不同？（答案：带前缀名、位于 `suites/` 子树、需显式导入、默认无 fallback。）

> 注意：本实践是「在教程目录里画/写设计稿」，**不要真的在仓库 `src/` 下创建文件**（那会改动源码，违反本讲约束）。把骨架写在 `TileGym-tutorial/` 下自己的笔记文件里即可。

## 6. 本讲小结

- **Suites = 复用核心分发机制 + 命名空间前缀**：`liger.cross_entropy`、`flashinfer.gemm.gemm_alpha_beta` 这类带 `.` 的算子名，对 dispatcher 而言只是普通字典键，没有任何特殊解析。
- **每个 suite 是固定的两层结构**：`ops.py`（带前缀 stub 的统一接口）+ `<backend>/`（一算子一文件的实现目录），靠 `register_impl` 对齐——与核心 `ops/ops.py` + `ops/cutile/` 同构。
- **注册是导入副作用、受 `is_backend_available` 门控**：`from tilegym.suites import liger` 触发 `__init__.py` → 门控导入 cutile 子包 → 各模块顶层 `@register_impl` 把实现写进 `_REGISTRY`。
- **liger 是训练向套件**：当前覆盖训练损失（grpo_loss/cross_entropy/fused_linear_cross_entropy/jsd/kl_div/tvd）、归一化（rms_norm/fused_add_rms_norm/layer_norm/group_norm/poly_norm/dyt）、激活（swiglu/geglu/softmax/sparsemax）等族，损失算子普遍带 autograd Function。
- **机制同构、组织分离**：suite 与核心 ops 共用 `@dispatch`/`register_impl`/`_REGISTRY`/wrapper；但 suite 位于 `suites/` 子树、需显式导入、stub 多不设 `fallback_backend`（严格后端门控）。
- **核心包不自动加载 suites**：`import tilegym` 之后 `_REGISTRY` 里没有任何 `liger.*` 键，必须显式导入对应套件。

## 7. 下一步学习建议

- **u10-l4（Liger 训练内核族）**：本讲只讲了 suite 机制，u10-l4 会深入 liger 的具体训练内核（grpo_loss 的前向 grid 与反向重计算、fused_linear_cross_entropy 的分块、归一化变体等），建议紧接着读。
- **u10-l2（实验内核追踪与内核清单生成）**：讲 `experimental_kernel` 装饰器与 `kernel_inventory` 如何**遍历 suite 后端**（含 cutile、triton、cutile-rs）的 solution 生成内核清单，是 suite 与工具链结合的进阶话题。
- **直接读源码**：挑一个 liger 损失算子（建议 [liger/cutile/cross_entropy.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/suites/liger/cutile/cross_entropy.py)），把 `@register_impl` → `CrossEntropyCuTileFunction.apply` → `_liger_cross_entropy_kernel` 这条链走通，验证本讲的「stub→实现」模型。
- **回看 u2-l2**：若对 `dispatch`/`register_impl`/`_REGISTRY` 的细节有遗忘，回到 u2-l2 对照阅读，会发现本讲所有「suite 特性」都建立在那五个决策点之上。
