# u6-l3 Python converter 与适配层测试

## 1. 本讲目标

前两讲我们看清了 torch_ops_extension 的骨架（u6-l1）与单个算子的 csrc 适配四件套（u6-l2）。csrc 层解决的是「**eager 模式下** `torch.ops.custom.xxx` 能在 NPU 上跑起来、能进 autograd」。但盘古 2.0 是大模型训练，真实训练脚本通常会用 `torch.compile` 把整个模型编译成图来减少 Python 开销。**一旦进图，csrc 里注册的 `PrivateUse1` 实现就不再是第一公民**——图编译器看到的是一个 FX 图节点，需要有人告诉它「这个节点在目标图里对应什么算子」。

这个「有人」就是本讲的主角之一：**converter（torchair fx2ge 转换器）**。本讲的另一位主角是 **test 目录的单算子验证脚本**。围绕它们，本讲回答四个问题：

1. 仓库里 `converter/` 目录下的 Python 文件到底做了什么？`@register_fx_node_ge_converter` 和 `torchair.ge.custom_op` 是怎么协作的？
2. 为什么全仓库 15 个左右有 csrc 的算子里，**只有 MHC post 家族三个算子**配齐了 `csrc + converter + test` 三件套？这套目录范式规范是什么？
3. `test/test_npu_xxx.py` 脚本的模板长什么样？它验证了什么、**没验证什么**？
4. converter、csrc、test 三者各自的职责边界在哪里？给一个新算子补适配层时，什么该写在 C++ 里、什么该写在 Python 里？

先给出一个**重要纠偏**：本讲大纲原本把 converter 理解为「参数转换 / 默认值填充的 Python 包装」。读真实源码后发现，仓库里的 converter 是 **torchair 的 fx2ge 转换器**——它不做运行期参数校验，也不填默认值（这些已经在 csrc 的 torch schema 里完成了），它的唯一职责是**把 torch FX 图节点翻译成昇腾 GE（Graph Engine）图里的原生自定义算子**。本讲将按真实源码讲解，并在综合实践中同时覆盖「fx2ge converter」与「eager 包装函数」两种 Python 侧适配形态。

## 2. 前置知识

### 2.1 eager 模式与图模式：为什么需要 converter

PyTorch 有两种执行方式：

- **eager 模式（逐算子执行）**：Python 里每调用一个算子，立刻经 Dispatcher 分发到对应实现、同步返回结果。u6-l2 讲的 `TORCH_LIBRARY_IMPL(custom, PrivateUse1, m)` 注册的就是这条路径的实现。
- **图模式（torch.compile）**：先用 torch Dynamo 把 Python 函数追踪（trace）成一张 **FX 图**——图的中每个节点是一个算子调用（如 `torch.ops.custom.xxx.default`），然后再把整张图交给某个编译后端整体优化和执行。

对昇腾来说，这个「编译后端」通常是 **torchair**（华为开源的 PyTorch 图模式适配库）提供的 `npu_backend`：它把 FX 图翻译成 **GE 图**（昇腾计算图），再整图下发 NPU 执行。仓库文档里有现成的运行期样例：

- lightning_indexer 的文档展示了 torchair 路径：`import torchair as tng`、`npu_backend = tng.get_npu_backend(compiler_config=config)`，最后 `torch.compile(model, backend=npu_backend, dynamic=False, fullgraph=True)`，见 [ascendc/src/ops-transformer/attention/lightning_indexer_enhance/docs/npu_lightning_indexer_enhance.md:L138-L154](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/docs/npu_lightning_indexer_enhance.md#L138-L154) 与 [L191](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/docs/npu_lightning_indexer_enhance.md#L191)。
- post 算子的文档展示了另一条**不走 GE 翻译**的图模式：`torch.compile(model, fullgraph=True, backend="aot_eager", dynamic=False)` 再用 `torch_npu.npu.NPUGraph()` 捕获，见 [ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/docs/npu_ai_infra_manifold_constrained_hyper_connection_post.md:L146-L150](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/docs/npu_ai_infra_manifold_constrained_hyper_connection_post.md#L146-L150)。

两条路径的关键差别：`aot_eager` + NPUGraph 只是提前捕获、不做图翻译，FX 节点仍按 eager 的 Dispatcher 路径执行，**不需要 converter**；而 torchair 的 `npu_backend` 要把 FX 图翻译成 GE 图，FX 节点必须能映射成一个 GE 算子——**这就是 converter 存在的意义**。

### 2.2 torch.fx 与 fx2ge：一张图两种表示

FX 图的节点是「torch 算子对象」，例如 `torch.ops.custom.npu_ai_infra_manifold_constrained_hyper_connection_post.default`（注意尾巴上的 `.default`，这是 OpOverload 的具体重载名）。GE 图的节点则是「算子类型字符串 + 输入列表 + 属性列表」，例如类型 `ManifoldConstrainedHyperConnectionPost`、输入名为 `x/h_res/h_out/h_post`。

torchair 内部负责这个翻译的部件就叫 **fx2ge**（从 converter 文件的导入路径 `torchair._ge_concrete_graph.fx2ge_converter` 可以直接看出来）。它维护一张「torch 算子 → 转换函数」的注册表；本讲精读的 converter 文件，作用就是往这张表里添一行。

### 2.3 三件套：csrc + converter + test

本讲反复使用「三件套」指一个算子在 torch_ops_extension 里的完整适配层：

| 目录 | 语言 | 作用 | 生效场景 |
|---|---|---|---|
| `csrc/` | C++ | 注册 torch schema、桥接 aclnn、拼 autograd | eager 模式（一切的基础） |
| `converter/` | Python | 注册 fx2ge 转换器，FX 节点 → GE 算子 | torchair 图模式 |
| `test/` | Python | 单算子冒烟验证（造数 → 调用 → 断言） | 开发自测 |

一个先记住的事实：**全仓库只有 MHC post 家族的三个算子（post、post_grad、mhc_post_grad）配齐了三件套**，其余算子只有 csrc（`manifold_constrained_hyper_connection_pre` 连 `__init__.py` 都是空文件，只有 csrc 一个文件）。所以本讲以 post 家族为范式标本，综合实践再带你把 pre 算子的三件套补齐。

### 2.4 复习：本讲用到的 u6-l2 结论

- torch schema 集中注册在 `ops_def_registration.cpp` 的 `TORCH_LIBRARY_FRAGMENT(custom, m)` 里；
- `EXEC_NPU_CMD_V1` 宏按名动态解析 aclnn 两段式符号；
- dispatch key 三件套：`PrivateUse1`（NPU 实现）/ `AutogradPrivateUse1`（autograd 包装）/ `Meta`（shape 推导）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py` | **本讲核心标本**：post 算子的 fx2ge converter（36 行） |
| `.../ai_infra_manifold_constrained_hyper_connection_post/converter/__init__.py` | converter 包入口，`import *` 透传 |
| `.../ai_infra_manifold_constrained_hyper_connection_post/__init__.py` | 算子目录入口，导入 converter 子包 |
| `.../mhc/__init__.py` | MHC 家族五个算子包的聚合入口 |
| `.../ai_infra_manifold_constrained_hyper_connection_post/test/test_npu_ai_infra_manifold_constrained_hyper_connection_post.py` | post 算子单算子测试（48 行） |
| `.../ai_infra_manifold_constrained_hyper_connection_post/csrc/npu_ai_infra_manifold_constrained_hyper_connection_post.cpp` | post 算子 csrc（对照其注册形态） |
| `.../mhc/ai_infra_mhc_post_grad/converter/npu_ai_infra_mhc_post_grad.py` | 多输出 converter 对照样本 |
| `.../mhc/ai_infra_mhc_post_grad/test/test_npu_mhc_post_grad.py` | 多输出测试对照样本 |
| `ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp` | torch schema 定义（converter 签名的唯一依据） |
| `ascendc/torch_ops_extension/setup.py` | 打包规则（converter/test 如何进 wheel） |
| `ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_def.cpp` | GE 侧算子原型（inputs/outputs 名字的对照依据） |
| `ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp` | pre 算子原型（综合实践依据） |
| `ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h` | pre 算子 aclnn 接口（综合实践依据） |

（表中 `...` 为 `ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer` 的缩写。）

## 4. 核心概念与源码讲解

### 4.1 三件套目录范式与导入注册链

#### 4.1.1 概念说明

Python 的装饰器注册模式有一个前提：**注册发生在模块被 import 的那一刻**。`@register_fx_node_ge_converter` 装饰器把转换函数写进 torchair 的全局注册表——但只有这个 `.py` 文件被解释器执行过，装饰器才有机会运行。所以三件套不只是「文件放对地方」，还包含一条**导入链**：从最外层的包入口逐级 `import *`，最终保证「使用方 import 一次，全部 converter 完成注册」。

理解这条链，才能回答两个工程问题：新增一个算子的 converter 要挂接哪些 `__init__.py`？为什么我 import 了包却提示转换器没注册？

#### 4.1.2 核心流程

post 算子 converter 的注册链（自下而上）：

```text
npu_ai_infra_manifold_constrained_hyper_connection_post.py   ← 装饰器在此执行，注册进 torchair
        ▲ from .npu_xxx import *
converter/__init__.py
        ▲ from .converter import *
ai_infra_manifold_constrained_hyper_connection_post/__init__.py
        ▲ from .ai_infra_..._post import *（以及 post_grad、mhc_post_grad、sinkhorn、sinkhorn_grad）
mhc/__init__.py
        ▲ （仓库内无自动导入点！需使用方显式 import omni_training_custom_ops.ops_transformer...）
ops_transformer/
```

注意链的顶端是「断」的：顶层包 `omni_training_custom_ops/__init__.py` 只 `from . import custom_ops_lib`（导入 C++ 扩展 .so），**不导入 ops_transformer**。converter 的导入由使用方（训练框架脚本）显式完成。仓库内所有文档示例都只 `import omni_training_custom_ops`，恰好都不依赖 converter 生效（见 4.2.3 的分析），所以这个「断点」目前没有造成问题——实际训练框架如何导入，仓库内无源码可考，待确认。

#### 4.1.3 源码精读

**（1）链的最底端：converter 文件本身**（下一节逐行精读）：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py:L17-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py#L17-L36) —— `@register_fx_node_ge_converter` 装饰 `convert_npu_ai_infra_manifold_constrained_hyper_connection_post` 函数，完成注册。

**（2）converter 包入口**：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/__init__.py:L9](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/__init__.py#L9) —— 一行 `from .npu_ai_infra_manifold_constrained_hyper_connection_post import *`，把子模块的符号（含被装饰函数触发的注册副作用）透传出去。

**（3）算子目录入口**：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/__init__.py:L10](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/__init__.py#L10) —— `from .converter import *`，把 converter 子包挂到算子包上。注意这一行是「导入 converter 子包」而不是「导入 csrc」——csrc 是编译进 `.so` 的，Python 侧无需也无法直接导入。

**（4）MHC 家族聚合入口**：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/__init__.py:L10-L14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/__init__.py#L10-L14) —— 五行 `import *` 聚合 post、post_grad、mhc_post_grad、sinkhorn、sinkhorn_grad 五个算子包。其中 sinkhorn / sinkhorn_grad 包内只有空 `__init__.py`（它们没有 converter），导入是空操作但保持了目录规范的一致性。

**（5）打包规则：converter 怎么进 wheel**：

[ascendc/torch_ops_extension/setup.py:L22-L23](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/setup.py#L22-L23) —— 两条 glob 只收集 `csrc_base/*.cpp` 和 `*/*/*/csrc/*.cpp` 的 **C++ 源码**；Python 文件不在这两条规则里。

[ascendc/torch_ops_extension/setup.py:L40-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/setup.py#L40-L43) —— Python 包靠 `find_packages()`（凡含 `__init__.py` 的目录都算包）自动收集，`package_data` 再补充声明。一个小瑕疵：`'omni_training_custom_ops.converter'` 这个顶层 converter 包在磁盘上并不存在（converter 都嵌在 ops_transformer/mhc 之下），该条目是无效但无害的历史残留；嵌套的 converter 包实际靠 `find_packages()` 进 wheel。

#### 4.1.4 代码实践

**实践目标**：亲手验证「导入即注册」这条链，并确认链的断点位置。

**操作步骤**：

1. 在装好本扩展包与 torchair 的环境中（无 NPU 也可以做导入实验，只要 import 不触发设备初始化）执行：

   ```python
   import torch, torch_npu
   import omni_training_custom_ops                       # 只导入 .so
   from torchair._ge_concrete_graph.fx2ge_converter import get_ge_converter  # 工具名以本机 torchair 版本为准
   # 尝试查表（具体 API 名以 torchair 版本为准，也可以改用注册表长度对比法）
   ```

2. 改用「注册表长度对比法」（不依赖 torchair 内部 API 细节）：

   ```python
   import torch, torch_npu, torchair
   import omni_training_custom_ops
   n1 = len(torchair._ge_concrete_graph.fx2ge_converter.util.exports)  # 属性名以本机版本为准
   import omni_training_custom_ops.ops_transformer.mhc                  # 显式走完导入链
   n2 = len(torchair._ge_concrete_graph.fx2ge_converter.util.exports)
   print(n1, n2)
   ```

   若不确定内部属性名，退一步只验证副作用：分别在「只 import 顶包」与「import 到 mhc 层」两种状态下运行 4.2.4 的图模式脚本，观察行为差异。

3. 用 `pip show -f omni_training_custom_ops | grep converter` 检查 wheel 里实际携带了哪些 converter 文件。

**需要观察的现象**：只导入顶包时注册表不增长（或图模式脚本失败/回退）；显式导入 `...ops_transformer.mhc` 后注册表增长（幅度应为 3，对应 post 家族三个 converter）。

**预期结果**：注册链在 `ops_transformer` 顶层断开、需显式导入的结论成立。**待本地验证**：步骤 2 中 torchair 内部注册表的具体属性名随版本变化，若名称不符请以对比法或图模式行为差异为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ai_infra_manifold_constrained_hyper_connection_post/__init__.py` 里只有 `from .converter import *`，却没有一行关于 csrc 的导入？

**答案**：csrc 的 C++ 源码在构建期被 `setup.py` 的 glob 规则收进 `custom_ops_lib` 扩展模块编译成 `.so`；`TORCH_LIBRARY_IMPL` 的注册在 `.so` 被 Python 加载（`from . import custom_ops_lib`）时由 C++ 静态初始化完成。Python 侧的 `__init__.py` 只需要负责「纯 Python 的注册副作用」，即 converter 的装饰器执行。

**练习 2**：如果新增第 4 个带 converter 的 MHC 算子（比如综合实践里的 pre），需要改动哪几个 `__init__.py`？

**答案**：三个。新建 `ops_transformer/mhc/manifold_constrained_hyper_connection_pre/converter/__init__.py`（透传 converter 模块）、把 `manifold_constrained_hyper_connection_pre/__init__.py` 从空文件改为 `from .converter import *`、在 `mhc/__init__.py` 增加一行 `from .manifold_constrained_hyper_connection_pre import *`。

**练习 3**：`pip install` 之后删掉 site-packages 里某个 converter 的 `.py` 文件，eager 模式调用该算子会失败吗？

**答案**：不会。eager 模式走 Dispatcher → `PrivateUse1` → `.so` 里的实现，完全不经过 fx2ge 注册表。受影响的只有 torchair 图模式（FX 节点找不到转换函数）。

### 4.2 torchair fx2ge converter 逐行精读

#### 4.2.1 概念说明

converter 要解决的问题：**torchair 把 FX 图翻成 GE 图时，遇到 `torch.ops.custom.xxx.default` 节点，去哪里找它的 GE 表示？**

答案是一张注册表。`@register_fx_node_ge_converter(算子重载对象)` 把一个 Python 函数登记为该节点的翻译规则；翻译时 torchair 调用这个函数，函数返回一个 GE 算子描述（`torchair.ge.custom_op(...)`），包括：

- **GE 算子类型字符串**——必须能被运行时识别（通常是 `_def.cpp` 里 `OP_ADD` 注册的类名）；
- **inputs 字典**——FX 节点的每个张量实参，按 GE 侧 `Input("名字")` 绑定；
- **attrs 字典**——标量属性（int/float/str 等），按 GE 侧 `Attr("名字")` 绑定；
- **outputs 列表**——GE 侧 `Output("名字")` 的有序列表。

所以 converter 是一张**纯声明式的对照表**：torch 侧的 schema 签名 ↔ GE 侧的 def 原型。它不搬数据、不做校验、不产生任何运行期逻辑——所有「翻译之外」的职责都在 csrc（eager）与 tiling/kernel（设备侧）。

#### 4.2.2 核心流程

一次 torchair 图模式调用的完整链路：

```text
model(x, h_res, h_out, h_post)                      # Python 前向
  └─ torch.compile(backend=npu_backend) 捕获
       └─ FX 图节点: torch.ops.custom.npu_ai_infra_..._post.default(x, h_res, h_out, h_post)
            └─ fx2ge 查注册表 → convert_npu_ai_infra_..._post(x, h_res, h_out, h_post, meta_outputs)
                 └─ 返回 torchair.ge.custom_op("ManifoldConstrainedHyperConnectionPost",
                        inputs={x, h_res, h_out, h_post}, attrs={}, outputs=["output"])
                      └─ GE 图节点（类型/输入名/输出名与 _def.cpp 原型对齐）
                           └─ 整图下发 → GE 调度 tiling → 启动 kernel
```

对比 eager 模式（u6-l2）：FX 节点这一步原本会走 Dispatcher 命中 `PrivateUse1` 实现；图模式把它替换成「查表 → 生成 GE 节点」。**两条路最终都落到同一个已安装的算子包（run 包）上**，只是调度入口不同。

#### 4.2.3 源码精读

**（1）post converter 全文结构**。先看导入区：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py:L9-L14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py#L9-L14) —— 导入 `Any`、`torch`、`torchair`，以及三个关键件：`register_fx_node_ge_converter`（注册装饰器，来自 `torchair._ge_concrete_graph.fx2ge_converter`，模块路径直接暴露了它属于 FX→GE 翻译部件）、`Tensor`（torchair 的 GE 张量类型，来自 `torchair.ge._ge_graph`，注意**不是** torch.Tensor）、`attr`（GE 属性包装工具）。注意 `attr` 在这三个 converter 里导入了但没用到——它是为带标量属性的算子准备的钩子，综合实践中给 pre 写 converter 时会用到。

然后是注册与函数体：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py:L17-L25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py#L17-L25) —— 装饰器参数是 `torch.ops.custom.npu_ai_infra_manifold_constrained_hyper_connection_post.default`（**精确到 `.default` 重载**，注册粒度是 OpOverload 而非算子名）。函数签名必须与 torch schema 逐参对应：四个张量参数按 schema 位置顺序排列，末尾固定追加仅关键字参数 `meta_outputs: Any = None`（torchair 用它携带输出元信息）。

对照 schema 原文（[ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp:L60-L61](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L60-L61)）：

```cpp
m.def("npu_ai_infra_manifold_constrained_hyper_connection_post(Tensor x, Tensor h_res, Tensor h_out, Tensor "
      "h_post) -> Tensor");
```

四个位置参数、无属性、单输出——converter 签名与之严格同构。

最后是翻译结果：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py:L26-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py#L26-L35) —— 返回 `torchair.ge.custom_op(...)`：GE 类型字符串 `"ManifoldConstrainedHyperConnectionPost"`，inputs 字典四个键 `x/h_res/h_out/h_post`，attrs 空字典（无标量属性），outputs 列表 `["output"]`。

**（2）inputs/outputs 名字必须与 GE 侧 def 对齐**。对照 post 的 `_def.cpp`：

[ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_def.cpp:L24-L52](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_def.cpp#L24-L52) —— `Input("x")`、`Input("h_res")`、`Input("h_out")`、`Input("h_post")`、`Output("output")`，与 converter 的 inputs 键、outputs 列表**逐字一致**。这不是巧合而是硬契约：GE 运行时按名字绑定张量，写错一个字母图就接不上。

**（3）多输出算子的 converter**。看 mhc_post_grad：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_mhc_post_grad/converter/npu_ai_infra_mhc_post_grad.py:L17-L38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_mhc_post_grad/converter/npu_ai_infra_mhc_post_grad.py#L17-L38) —— schema 是 `(Tensor grad_output, Tensor x, Tensor h_res, Tensor h_out, Tensor h_post) -> (Tensor, Tensor, Tensor, Tensor)`（见 [ops_def_registration.cpp:L64-L65](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L64-L65)）。converter 签名照抄五个张量参数（L18-L26），outputs 列表给出四个名字 `["grad_x", "grad_h_res", "grad_h_out", "grad_h_post"]`（L37）。这四个名字同样能在 GE 侧 def 中逐一找到：[ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_def.cpp:L54-L69](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_def.cpp#L54-L69) 定义了 `grad_output` 输入与 `grad_x/grad_h_res/grad_h_out/grad_h_post` 四个输出。post_grad 的 converter（[npu_ai_infra_manifold_constrained_hyper_connection_post_grad.py:L17-L38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post_grad/converter/npu_ai_infra_manifold_constrained_hyper_connection_post_grad.py#L17-L38)）结构与它完全同构。

**（4）一个值得警惕的坑：GE 类型字符串与 def 类名不一致**。三份对照：

| converter 里的类型字符串 | `_def.cpp` 的 OP_ADD 类名 | 是否一致 |
|---|---|---|
| `"AiInfraMhcPostGrad"` | `AiInfraMhcPostGrad`（[L87](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_mhc_post_grad/op_host/ai_infra_mhc_post_grad_def.cpp#L87)） | 一致 |
| `"AiInfraManifoldConstrainedHyperConnectionPostGrad"` | `AiInfraManifoldConstrainedHyperConnectionPostGrad` | 一致 |
| `"ManifoldConstrainedHyperConnectionPost"` | `AiInfraManifoldConstrainedHyperConnectionPost`（[L66](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/op_host/ai_infra_manifold_constrained_hyper_connection_post_def.cpp#L66)） | **少了 `AiInfra` 前缀** |

GE 侧引用算子类型时用的是全类名（例如 pre 的 op_api 以 `ADD_TO_LAUNCHER_LIST_AICORE(AiInfraManifoldConstrainedHyperConnectionPre, ...)` 挂载，见 [ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/ai_infra_manifold_constrained_hyper_connection_pre.cpp:L48](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/ai_infra_manifold_constrained_hyper_connection_pre.cpp#L48)）。post converter 的字符串少了前缀，与自家 def 类名不匹配——要么是笔误，要么依赖安装包里的某种别名机制，仓库内无从判断，**待确认**。给自己写 converter 的守则：**类型字符串照抄 `_def.cpp` 的 OP_ADD 类名，一个字符都不要改**。

#### 4.2.4 代码实践

**实践目标**：建立「converter 签名 ← torch schema ← GE def」三点对齐的检查能力。

**操作步骤**：

1. 打开 [ops_def_registration.cpp:L60-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L60-L65)，抄下 post 家族三条 schema。
2. 打开三份 converter 文件，逐参数比对：位置参数个数、顺序、名字；仅关键字参数是否只有 `meta_outputs`。
3. 打开对应的三份 `_def.cpp`（post / post_grad / mhc_post_grad），逐名字比对 inputs 字典键与 `Input("...")`、outputs 列表与 `Output("...")`。
4. 填写下面这张对齐检查表（示例答案已填 post 一行）：

| 算子 | schema 位置参数 | converter inputs 键 | def Input 名 | 一致？ |
|---|---|---|---|---|
| post | x, h_res, h_out, h_post | x, h_res, h_out, h_post | x, h_res, h_out, h_post | 是 |
| post_grad | （待填） | （待填） | （待填） | （待填） |
| mhc_post_grad | （待填） | （待填） | （待填） | （待填） |

**需要观察的现象**：三份对照中，参数与输入输出名字全部逐字一致；唯一的「不一致」只出现在 4.2.3（4）指出的 post 类型字符串前缀上。

**预期结果**：你会得到一个可复用的三列核对流程。**待本地验证**：若你有 NPU 环境，可进一步用 torchair 图模式跑通 post（参考 lightning_indexer 文档的 `torch.compile(backend=npu_backend, fullgraph=True)` 写法），观察使用当前类型字符串是否能成功构图——这也是验证 4.2.3（4）坑点的唯一手段。

#### 4.2.5 小练习与答案

**练习 1**：converter 函数的 `x: Tensor` 类型注解里，`Tensor` 是 `torch.Tensor` 吗？

**答案**：不是。它来自 `from torchair.ge._ge_graph import Tensor`（L13），是 torchair 对「GE 图中张量」的 Python 包装。fx2ge 翻译期间函数收到的实参是 GE 图里的符号张量（携带 shape/dtype 等元信息），不是真实数据——这正是 converter 只能做「结构翻译」、不能做数值计算的原因。

**练习 2**：如果 schema 里有标量属性（如 pre 的 `out_flag/norm_eps/hc_eps`），converter 里应放在哪一侧？怎么写？

**答案**：标量不是张量，不能进 inputs 字典，应进 attrs 字典，并用 `attr.Int(x)` / `attr.Float(x)` 等包装（即文件顶部导入的 `attr` 工具的用途），键名与 `_def.cpp` 的 `Attr("...")` 名字对齐（pre 是 `outFlag/normEps/hcEps`）。仓库内三个 converter 都是无属性的纯张量算子，attrs 为空字典，所以没有现成样例——综合实践会写出完整示例。

**练习 3**：为什么装饰器要精确到 `.default` 这个重载，而不是只给算子名？

**答案**：torch 的一个算子名可以有多个重载（schema 各不相同），转换函数的签名只能匹配某一个具体 schema。按 OpOverload 注册让 fx2ge 在遇到该重载的节点时精确命中；如果同一算子将来加了 `.mutable` 等新重载，需要另写一份 converter。

### 4.3 test 单算子验证脚本：模板与局限

#### 4.3.1 概念说明

`test/` 目录存放**单算子冒烟测试**：在真实 NPU 上用一组小规模输入调用 `torch.ops.custom.xxx`，断言输出的 shape 与 dtype。它回答的问题是「**适配层接通了吗**」——run 包已安装、wheel 已安装、schema 正确、aclnn 能找到符号、输出能分配回来。

它**不回答**「算得对不对」。数值精度验证是 ST 测试的职责（MARE/MERE/RMSE 指标，第 8 单元 u8-l3 详讲）。本讲会明确指出仓库现有 test 的这个局限，并在实践中带你补上 CPU golden 对比。

#### 4.3.2 核心流程

test 脚本是五段式模板：

```text
① import 区：torch / torch_npu / numpy
② 参数区：B、S、n、D 等规模常量
③ 造数区：torch.randn(..., dtype=...).npu() 逐个构造输入
④ 调用区：output = torch.ops.custom.xxx(...)（多输出算子用元组解包）
⑤ 断言区：assert output.shape == (...) / assert output.dtype == ...
⑥ main 区：if __name__ == "__main__": import omni_training_custom_ops 后执行
```

运行方式：直接 `python test_npu_xxx.py`（脚本自带 `__main__` 入口）；函数名以 `test_` 开头，也可被 pytest 收集。test 目录没有 pytest.ini / conftest.py，不是工程化测试套件，而是随算子交付的自验脚本。

#### 4.3.3 源码精读

**（1）post 的 test 全文**。导入与参数区：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/test/test_npu_ai_infra_manifold_constrained_hyper_connection_post.py:L11-L24](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/test/test_npu_ai_infra_manifold_constrained_hyper_connection_post.py#L11-L24) —— `import torch / torch_npu / numpy`（numpy 导入后未使用，模板残留）；测试参数 B=1、S=4096、n=4、D=2560，正好是 `_def.cpp` 支持的真实规模量级。

造数区（注意 dtype 与 def 的约束一一对应）：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/test/test_npu_ai_infra_manifold_constrained_hyper_connection_post.py:L27-L30](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/test/test_npu_ai_infra_manifold_constrained_hyper_connection_post.py#L27-L30) —— `x` 与 `h_out` 是 bfloat16（def 里 `DataType({ge::DT_BF16, ge::DT_FLOAT16})`），`h_res` 与 `h_post` 是 float32（def 里 `DataType({ge::DT_FLOAT})`），shape 分别为 `[B,S,n,D]`、`[B,S,n,n]`、`[B,S,D]`、`[B,S,n]`，与文档规格一致。

调用与断言区：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/test/test_npu_ai_infra_manifold_constrained_hyper_connection_post.py:L33-L41](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/test/test_npu_ai_infra_manifold_constrained_hyper_connection_post.py#L33-L41) —— 直接调用 `torch.ops.custom.npu_ai_infra_manifold_constrained_hyper_connection_post(x, h_res, h_out, h_post)`，断言输出 shape 为 `(B,S,n,D)`、dtype 为 bfloat16。这两个断言实际验证的是 csrc 里 `construct_mhc_post_returns` 的分配逻辑（[csrc/npu_ai_infra_manifold_constrained_hyper_connection_post.cpp:L27-L57](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/csrc/npu_ai_infra_manifold_constrained_hyper_connection_post.cpp#L27-L57)）加上 aclnn 执行没报错。

main 区：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/test/test_npu_ai_infra_manifold_constrained_hyper_connection_post.py:L46-L48](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/test/test_npu_ai_infra_manifold_constrained_hyper_connection_post.py#L46-L48) —— `__main__` 里先 `import omni_training_custom_ops`（确保 `.so` 加载、`torch.ops.custom` 命名空间可用）再执行测试。

**（2）多输出算子的 test 对照**。mhc_post_grad 的测试在调用区用元组解包四个梯度：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_mhc_post_grad/test/test_npu_mhc_post_grad.py:L34-L36](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_mhc_post_grad/test/test_npu_mhc_post_grad.py#L34-L36) —— `grad_x, grad_h_res, grad_h_out, grad_h_post = torch.ops.custom.npu_ai_infra_mhc_post_grad(grad_output, x, h_res, h_out, h_post)`。

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_mhc_post_grad/test/test_npu_mhc_post_grad.py:L39-L48](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_mhc_post_grad/test/test_npu_mhc_post_grad.py#L39-L48) —— 断言区对四个输出分别检查 shape 与 dtype。值得注意的细节：`grad_x`/`grad_h_out` 是 bfloat16（与对应前向输入同 dtype），`grad_h_res`/`grad_h_post` 是 float32——梯度 dtype 跟随各自前向输入，这是写多输出断言时的通用规律。

**（3）局限：没有任何数值断言**。两份测试都只看 shape/dtype，`import numpy as np` 落满灰尘。与 `ascendc/src/tests/st/` 下的 ST 精度测试（CPU fp64 golden + MARE/MERE/RMSE 分级容差，u8-l3）相比，这里连简单的 allclose 都没有。也就是说：**算子就算把输出全填成垃圾值，这两个测试照样绿色通过**。4.3.4 的实践就来补这个缺口。

#### 4.3.4 代码实践

**实践目标**：给 post 的测试补一个 CPU golden 数值断言，让它从「冒烟」升级为「最小精度验证」。

**依据**：post 的计算公式（来自算子文档）为

\[ x_{l+1} = (H_l^{res})^{\mathsf{T}} x_l + h_l^{out} \otimes H_l^{post} \]

见 [ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/docs/npu_ai_infra_manifold_constrained_hyper_connection_post.md:L16-L18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/docs/npu_ai_infra_manifold_constrained_hyper_connection_post.md#L16-L18)。展开成分量（对每个 batch、每个 token）：

\[ \text{out}_{i,d} = \sum_{j=1}^{n} (H^{res}_{j,i}) \, x_{j,d} \;+\; h^{post}_{i} \cdot h^{out}_{d} \]

**操作步骤**（以下为示例代码，在原测试文件断言区之后追加）：

```python
# ---- 示例代码：CPU golden 数值断言（追加到原 test 函数末尾）----
# 保留 CPU 侧参考副本（.npu() 之前先在 CPU 造数，或用 .cpu() 拷回）
x_cpu = x.detach().float().cpu()            # [B,S,n,D]
hres_cpu = h_res.detach().float().cpu()     # [B,S,n,n]
hout_cpu = h_out.detach().float().cpu()     # [B,S,D]
hpost_cpu = h_post.detach().float().cpu()   # [B,S,n]

golden = torch.einsum('bsji,bsjd->bsid', hres_cpu, x_cpu) \
       + hout_cpu.unsqueeze(2) * hpost_cpu.unsqueeze(-1)   # [B,S,n,D]

out_cpu = output.detach().float().cpu()
assert torch.allclose(out_cpu, golden, atol=1e-2, rtol=1e-2), \
    f"max abs err: {(out_cpu - golden).abs().max().item()}"
print("Numeric check passed!")
```

注意原测试的造数是 `torch.randn(...).npu()` 直接落到设备，CPU 副本需按上面 `.cpu()` 取回（数据量小，代价可忽略）。

**需要观察的现象**：allclose 通过则打印 Numeric check passed；不通过时打印最大绝对误差，便于判断是转置方向错了（误差巨大）还是 bf16 精度问题（误差在 1e-2 量级边缘）。

**预期结果**：公式中的转置（\(H^{res}_{j,i}\) 下标顺序）与逐元素外积项若理解正确，误差应落在 bf16 正常精度范围内。**待本地验证**：atol/rtol 阈值 1e-2 是按 bf16 经验给的初值，需实测调整；einsum 的转置方向以 docs 公式为准，若不匹配可尝试 `'bsij,bsjd->bsid'` 对照误差量级来甄别。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__main__` 里才 `import omni_training_custom_ops`，而不是放在文件顶部？

**答案**：两种写法功能等价，放 `__main__` 是为了强调「测试本体不依赖扩展包的 Python 层，只依赖 torch.ops.custom 命名空间」；同时若该文件被 pytest 收集，conftest 或环境里通常已保证扩展导入。本质上这个 import 是 `.so` 加载与 `torch.ops.custom` 挂载的触发器（见 u6-l1 的 `__init__.py` 机制）。

**练习 2**：把测试参数里的 `h_res` 改成 bfloat16 会发生什么？

**答案**：违反 `_def.cpp` 中 `h_res` 的 `DataType({ge::DT_FLOAT})` 约束，tiling 侧的输入校验（OP_CHECK_IF 类型检查）会报错返回 `GRAPH_FAILED`，aclnn 第一段接口返回非 0 状态码，`EXEC_NPU_CMD_V1` 抛出异常。这正好可以当作「约束真实生效」的反向验证用例。

**练习 3**：test 目录的脚本和 `ascendc/src/tests/st/` 的 ST 测试是什么关系？

**答案**：分层互补。`torch_ops_extension` 的 test 验证**适配层**（schema→aclnn→输出分配这条链），输入规模小、无精度指标；ST 验证**算子数值正确性**（CPU fp64 golden、MARE/MERE/RMSE、L0/L1/L2 精度分级），直接调用 aclnn 或 torch 接口，参数化多组规模。适配层测试绿不代表算得对，ST 才是精度防线（详见 u8-l3）。

### 4.4 converter 与 csrc 的职责分界

#### 4.4.1 概念说明

三件套各自守一段职责，互不越界：

| 职责 | 归属 | 载体 |
|---|---|---|
| torch schema（参数名/类型/默认值） | csrc | `ops_def_registration.cpp` 的 `m.def(...)` |
| eager 执行（输出分配、aclnn 桥接） | csrc | `EXEC_NPU_CMD_V1` |
| autograd 拼接 | csrc | `torch::autograd::Function` 子类 + `AutogradPrivateUse1` 注册 |
| shape 推导 | csrc | `Meta` 注册的伪实现 |
| 运行期参数校验 | csrc | `TORCH_CHECK`（进函数第一件事） |
| 图模式结构翻译 | converter | `@register_fx_node_ge_converter` + `torchair.ge.custom_op` |
| 开发自验 | test | `torch.ops.custom.xxx` 调用 + 断言 |

核心判断标准：**凡是「每次调用都要执行的逻辑」（校验、分配、下发、求导）在 csrc；converter 只提供「一张静态对照表」，它在构图期运行一次，不接触任何真实数据**。至于 Python 侧的输入校验/默认值填充，本仓库的做法是把默认值直接写进 torch schema（如 pre 的 `int out_flag=0, float norm_eps=1e-6`），由 PyTorch 机制在调用期生效——不需要额外的 Python 包装层；若要更友好的 API（namedtuple 输出、中文报错），才在使用方再包一层薄函数（综合实践演示）。

#### 4.4.2 核心流程

同一条算子调用在两种模式下的路径分岔：

```text
torch.ops.custom.npu_ai_infra_..._post(x, h_res, h_out, h_post)
│
├─ eager：Dispatcher → AutogradPrivateUse1（若注册）→ PrivateUse1
│    └─ csrc: construct_returns 分配输出 → EXEC_NPU_CMD_V1 → aclnn 两段式 → NPU
│
└─ torchair 图模式：Dynamo 捕获 → FX 节点(.default)
     └─ fx2ge 查表 → converter 返回 GE 节点描述 → GE 整图编译下发
          └─ GE 侧按 _def.cpp 原型走 tiling → kernel（与 eager 终点相同）
```

两条路共用同一个已安装算子包；**终点相同、入口不同**。这也解释了为什么 converter 可以这么薄——所有重活都在两条路径的公共后段（aclnn/tiling/kernel）里。

#### 4.4.3 源码精读

**（1）post 的 csrc 是「无 autograd」的精简形态**：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/csrc/npu_ai_infra_manifold_constrained_hyper_connection_post.cpp:L85-L98](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/csrc/npu_ai_infra_manifold_constrained_hyper_connection_post.cpp#L85-L98) —— 只有两路注册：`PrivateUse1`（NPU 前向）与 `Meta`（shape 推导），**没有** `AutogradPrivateUse1`。对比 u6-l2 的 aggregate_hidden（三路齐全）少了 autograd 层。这意味着 eager 模式下对 post 的输出调 `.backward()`，梯度不会流过这个算子——反向必须由训练框架显式调用 `post_grad` / `mhc_post_grad` 两个独立算子手工拼接。MHC 家族把前反向拆成多个独立 torch 算子（前向 1 个 + 反向 2 个数学等价实现），由模型代码自行编排，这与 aggregate_hidden「一个算子内藏 autograd」是两种不同的工程取舍。

**（2）pre 是「全功能」的反例**：同一个 MHC 家族里，pre 的 csrc 配齐了 autograd Function：

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/manifold_constrained_hyper_connection_pre/csrc/npu_manifold_constrained_hyper_connection_pre.cpp:L226-L267](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/manifold_constrained_hyper_connection_pre/csrc/npu_manifold_constrained_hyper_connection_pre.cpp#L226-L267) —— `ManifoldConstrainedHyperConnectionPreFunction` 的 forward：加 `AutoDispatchBelowADInplaceOrView` 守卫、经 Dispatcher 调真实实现、`save_for_backward` 保存反向所需中间量。

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/manifold_constrained_hyper_connection_pre/csrc/npu_manifold_constrained_hyper_connection_pre.cpp:L269-L322](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/manifold_constrained_hyper_connection_pre/csrc/npu_manifold_constrained_hyper_connection_pre.cpp#L269-L322) —— backward：取回保存的张量、为未定义的 grad_outputs 补零张量、调 `npu_manifold_constrained_hyper_connection_pre_grad` 算子，返回 8 个梯度（3 个标量参数返回未定义张量，u6-l2 讲过的规则）。

[ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/manifold_constrained_hyper_connection_pre/csrc/npu_manifold_constrained_hyper_connection_pre.cpp:L340-L354](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/manifold_constrained_hyper_connection_pre/csrc/npu_manifold_constrained_hyper_connection_pre.cpp#L340-L354) —— 三路注册齐全（PrivateUse1 两个算子 + AutogradPrivateUse1 + Meta）。

这两个样本放在一起得到的结论：**三件套的每一件都是可裁剪的**——post 证明可以不要 autograd，pre 证明可以不要 converter 和 test（目前没配）。裁剪依据是使用方式：训练框架自己管理反向（MHC post）就不需要 autograd 层；不上 torchair 图模式就不需要 converter。

**（3）schema 里的默认值就是「参数转换层」**。pre 的 schema：

[ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp:L66-L68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L66-L68) —— `npu_manifold_constrained_hyper_connection_pre(Tensor x, Tensor phi, Tensor alpha, Tensor bias, *, Tensor? gamma=None, int out_flag=0, float norm_eps=1e-6, float hc_eps=1e-6) -> (Tensor×6)`。可选参数 `gamma=None` 与三个标量默认值都内嵌在 schema 里，调用方少传即可——PyTorch 在 schema 解析层完成「默认值填充」，无需任何 Python 中间层。这直接推翻了「必须有一层 Python converter 做默认值填充」的预设。

#### 4.4.4 代码实践

**实践目标**：用「删除实验」验证职责分界（源码阅读型实践，不改仓库文件，只在本地副本做思想实验或在 site-packages 副本上做）。

**操作步骤**：

1. **思想实验 A**：假设删掉 post 的 converter 目录，列出受影响与不受影响的调用方式各两个。
2. **思想实验 B**：假设把 pre 的 `AutogradPrivateUse1` 注册（L346-L349）注释掉重新编译，预测 `loss.backward()` 时 x/phi/alpha 的梯度会怎样。
3. 对照 4.4.1 的表格，把每个「假设」的预测写到表格右侧一列。

**需要观察的现象 / 预期结果**：

- A：受影响的只有 torchair 图模式构图（FX 节点无翻译规则）；eager 调用、`aot_eager`+NPUGraph 捕获、`Meta` 推 shape 均不受影响。
- B：前向照常返回六个输出，但 `backward` 时梯度在 pre 算子处截断，`x.grad`/`phi.grad`/`alpha.grad` 为 None 或不更新（除非框架手工调 pre_grad）。

以上推理均可从 4.4.2 的路径图直接导出，**待本地验证**（若你有环境，A 可通过临时改 site-packages 副本实测）。

#### 4.4.5 小练习与答案

**练习 1**：post 没有 autograd 注册，但 post_grad / mhc_post_grad 却有完整的 converter 和 test。为什么反向算子反而配得更齐？

**答案**：post 家族的设计是「前向简单、反向交给框架显式编排」——框架在图里同时需要前向节点和反向节点，两个都要能被 fx2ge 翻译，所以反向算子的 converter 必不可少；test 同理，框架联调前要先单独验证反向算子的适配链路。而 autograd Function 是 eager 模式的专属机制，这个家族不打算用，自然不写。

**练习 2**：如果要给 post 补 autograd（让 eager 也能 `backward`），按 pre 的样板需要加哪些代码？

**答案**：在 post 的 csrc 里加一个 `PostFunction : public torch::autograd::Function<PostFunction>`，forward 调 `PrivateUse1` 实现并 `save_for_backward(x, h_res, h_out, h_post)`；backward 用 `grad_output` 加四个输入调 `torch.ops.custom.npu_ai_infra_manifold_constrained_hyper_connection_post_grad`（其 schema 恰好是 `(grad_output, x, h_res, h_out, h_post) -> 四梯度`，见 [ops_def_registration.cpp:L62-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L62-L63)），返回四个梯度；再注册 `TORCH_LIBRARY_IMPL(custom, AutogradPrivateUse1, m)`。注意 backward 需要重算（post 不保存中间量），这正是 mhc_post_grad 存在两个实现的原因之一。

**练习 3**：converter 的 attrs 字典和 tiling 侧 `GetAttrs`（u2-l5）是什么关系？

**答案**：同一个标量参数的两段旅程。attrs 字典把它从 FX 节点写进 GE 算子的属性；GE 下发后属性进入算子的 Attr 通路，被 Host 侧 tiling 的 `GetAttrs` 读出用于切分决策。converter 只负责第一段——把参数放进图；后段的消费逻辑它一概不感知。

## 5. 综合实践：为 MHC pre 算子补全三件套

这是本讲的收官任务：pre 算子目前只有 csrc（还是全功能形态），**没有 converter 也没有 test**。请按仓库范式把两块补齐，并额外写一个 eager 包装函数，把「converter（图模式对照表）+ wrapper（使用层封装）+ test（自验脚本）」三种 Python 适配形态一次练全。

### 5.1 先收集依据（只读，不改源码）

1. **torch schema**（converter 签名的唯一依据）：[ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp:L66-L68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L66-L68) —— 4 个位置张量参数 + 仅关键字参数 `gamma/out_flag/norm_eps/hc_eps`，返回 6 元组。
2. **GE 侧原型**（inputs/attrs/outputs 名字的唯一依据）：[ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp:L22-L46](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp#L22-L46) —— `Input("x"/"phi"/"alpha"/"bias"/"gamma")`；[L48-L74](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp#L48-L74) —— `Output("hin"/"h_post"/"h_res"/"inv_rms"/"mm_res"/"h_pre")`；[L89-L91](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp#L89-L91) —— `Attr("outFlag"/"normEps"/"hcEps")`；[L95](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_host/ai_infra_manifold_constrained_hyper_connection_pre_def.cpp#L95) —— OP_ADD 类名 `AiInfraManifoldConstrainedHyperConnectionPre`（converter 类型字符串照抄它）。
3. **aclnn 规格**（shape/dtype 约束）：[ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h:L25-L41](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/op_api/aclnn_ai_infra_manifold_constrained_hyper_connection_pre.h#L25-L41) —— x 为 [B,S,N,D] 或 [T,N,D]（BF16/FP16），phi 为 \([n^2+2n, nD]\)（FP32），alpha 为 [3]，bias 为 \([n^2+2n]\)，gamma 可选 \([n,D]\)；六个输出及 dtype。
4. **计算公式**（test 的 golden 依据）：[ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/docs/npu_manifold_constrained_hyper_connection_pre.md:L19-L35](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/ai_infra_manifold_constrained_hyper_connection_pre/docs/npu_manifold_constrained_hyper_connection_pre.md#L19-L35) —— RmsNorm 与三路投影公式，其中 \(\operatorname{Rms}(\mathbf{x})=\sqrt{\frac{1}{n}\sum_i x_i^2+norm\_eps}\)。

### 5.2 步骤一：写 converter（示例代码）

新建 `ops_transformer/mhc/manifold_constrained_hyper_connection_pre/converter/npu_manifold_constrained_hyper_connection_pre.py`：

```python
# ---- 示例代码：pre 算子的 fx2ge converter（仿照 post 家族三份 converter 的范式）----
from typing import Any, Optional
import torch
import torchair
from torchair._ge_concrete_graph.fx2ge_converter import register_fx_node_ge_converter
from torchair.ge._ge_graph import Tensor
from torchair.ge import attr


@register_fx_node_ge_converter(torch.ops.custom.npu_manifold_constrained_hyper_connection_pre.default)
def convert_npu_manifold_constrained_hyper_connection_pre(
    x: Tensor,
    phi: Tensor,
    alpha: Tensor,
    bias: Tensor,
    *,
    gamma: Optional[Tensor] = None,   # schema 中的 Tensor? gamma=None
    out_flag: int = 0,                # schema 中的 int out_flag=0
    norm_eps: float = 1e-6,
    hc_eps: float = 1e-6,
    meta_outputs: Any = None
):
    return torchair.ge.custom_op(
        "AiInfraManifoldConstrainedHyperConnectionPre",   # 照抄 OP_ADD 类名，不带引号内改动
        inputs={
            "x": x,
            "phi": phi,
            "alpha": alpha,
            "bias": bias,
            "gamma": gamma,
        },
        attrs={                       # 标量走 attrs，键名与 _def.cpp 的 Attr("...") 对齐
            "outFlag": attr.Int(out_flag),
            "normEps": attr.Float(norm_eps),
            "hcEps": attr.Float(hc_eps),
        },
        outputs=["hin", "h_post", "h_res", "inv_rms", "mm_res", "h_pre"],
    )
```

与仓库现有 converter 的三点差异（都源于 pre 比 post 多了标量属性与可选输入）：

1. **仅关键字参数区**放了 schema 的四个默认参数，再接 `meta_outputs`；
2. **attrs 非空**：`attr.Int/attr.Float` 的具体包装函数名以本机 torchair 版本为准（仓库现有 converter 均为 `attrs={}`，无对照样例，此写法依据文件顶部 `from torchair.ge import attr` 导入约定，**待本地验证**）；
3. **可选张量入图**：`gamma` 为 `Optional[Tensor]`，None 时如何处理（跳过该键 / 传 None 占位）取决于 torchair 对 optional 输入的约定，建议对照本机 torchair 内置 converter 中带 `Tensor?` 参数的写法，**待确认**。

再按 4.1.3 的导入链挂接三个 `__init__.py`（新建 `converter/__init__.py`、填充算子目录的空 `__init__.py`、在 `mhc/__init__.py` 加一行）。

### 5.3 步骤二：写 eager 包装函数（示例代码）

schema 已含默认值，wrapper 的增量价值是**输入校验前置到 Python 层（报错更友好）+ 输出语义化**。新建 `.../converter` 之外任意使用方模块均可，这里按「蓝图」给出完整文件 `npu_mhc_pre_wrapper.py`：

```python
# ---- 示例代码：pre 算子的 eager 使用层封装（蓝图）----
from typing import NamedTuple, Optional
import torch
import torch_npu


class MHCPreOutput(NamedTuple):
    h_in: torch.Tensor        # Atten/MLP 层输入 [B,S,D] / [T,D]，BF16/FP16
    h_post: torch.Tensor      # [B,S,n] / [T,n]，FP32
    h_res: torch.Tensor       # 交 Sinkhorn 的矩阵 [B,S,n,n]，FP32
    inv_rms: torch.Tensor     # out_flag=1 时有效 [B,S]，FP32
    mm_res: torch.Tensor      # out_flag=1 时有效 [B,S,n^2+2n]，FP32
    h_pre: torch.Tensor       # out_flag=1 时有效 [B,S,n]，FP32


def npu_mhc_pre(
    x: torch.Tensor,
    phi: torch.Tensor,
    alpha: torch.Tensor,
    bias: torch.Tensor,
    gamma: Optional[torch.Tensor] = None,
    out_flag: int = 0,
    norm_eps: float = 1e-6,
    hc_eps: float = 1e-6,
) -> MHCPreOutput:
    """MHC 前处理算子的友好封装：校验 → 调 torch.ops.custom → 语义化输出。"""
    # 1) 输入校验（csrc 里的 TORCH_CHECK 是最后防线，这里让报错更早更友好）
    assert x.dim() in (3, 4), f"x 须为 [B,S,n,D] 或 [T,n,D]，实际 {x.shape}"
    n = x.shape[-2]
    assert phi.dtype == torch.float32 and phi.shape[0] == n * n + 2 * n, \
        f"phi 须为 fp32 [{n}^2+2{n}, {n}*D]，实际 {tuple(phi.shape)} ({phi.dtype})"
    assert alpha.dtype == torch.float32 and alpha.numel() == 3, "alpha 须为 fp32 [3]"
    assert bias.dtype == torch.float32 and bias.numel() == n * n + 2 * n, "bias 须为 fp32 [n^2+2n]"
    assert x.dtype in (torch.bfloat16, torch.float16), "x 须为 bf16/fp16"
    # 2) 调用 torch.ops.custom 接口（默认值已在 schema 层生效，这里显式透传）
    outputs = torch.ops.custom.npu_manifold_constrained_hyper_connection_pre(
        x, phi, alpha, bias, gamma=gamma, out_flag=out_flag,
        norm_eps=norm_eps, hc_eps=hc_eps,
    )
    # 3) 输出整理：六元组 → 具名结构
    return MHCPreOutput(*outputs)


if __name__ == "__main__":
    import omni_training_custom_ops
    B, S, n, D = 1, 128, 4, 192
    x = torch.randn(B, S, n, D, dtype=torch.bfloat16).npu()
    phi = torch.randn(n * n + 2 * n, n * D, dtype=torch.float32).npu()
    alpha = torch.randn(3, dtype=torch.float32).npu()
    bias = torch.randn(n * n + 2 * n, dtype=torch.float32).npu()
    out = npu_mhc_pre(x, phi, alpha, bias, out_flag=1)
    print(out.h_in.shape, out.inv_rms.shape)
```

**职责分界说明**（本实践任务的最后一问）：converter 与 csrc 的分界是「**图模式静态对照 vs 一切运行期逻辑**」——csrc 负责 schema、输出分配、aclnn 桥接、autograd、Meta 推 shape 与 TORCH_CHECK 校验；converter 只在构图期把 FX 节点映射成 GE 算子，不执行任何校验或计算。上面的 wrapper 既不是 converter 也不是 csrc 的替代品，而是**使用层**的便利封装：校验只是为了报错友好（真正的约束仍由 csrc/tiling 兜底），输出整理只是把六元组变成具名对象。三层各司其职：**csrc 定义能力边界，converter 让能力进图，wrapper 让能力好用**。

### 5.4 步骤三：写 test（示例代码）

新建 `.../test/test_npu_manifold_constrained_hyper_connection_pre.py`，仿照 4.3.3 的五段式模板，并补 CPU golden：

```python
# ---- 示例代码：pre 算子单算子测试（CPU 参考值 + NPU 调用 + assert）----
import torch
import torch_npu


def test_npu_manifold_constrained_hyper_connection_pre():
    """Test npu_manifold_constrained_hyper_connection_pre operator"""
    # 参数区（小规模，n^2+2n=24，phi 为 [24, n*D]）
    B, S, n, D = 1, 128, 4, 192
    norm_eps, hc_eps = 1e-6, 1e-6

    # 造数区（同时保留 CPU 副本供 golden 使用）
    x_cpu = torch.randn(B, S, n, D, dtype=torch.float32)
    phi_cpu = torch.randn(n * n + 2 * n, n * D, dtype=torch.float32)
    alpha_cpu = torch.randn(3, dtype=torch.float32)
    bias_cpu = torch.randn(n * n + 2 * n, dtype=torch.float32)

    x = x_cpu.to(torch.bfloat16).npu()
    phi, alpha, bias = phi_cpu.npu(), alpha_cpu.npu(), bias_cpu.npu()

    # 调用区（out_flag=1 才会物化 inv_rms/mm_res/h_pre，见 csrc Meta 实现 L218-L222）
    outputs = torch.ops.custom.npu_manifold_constrained_hyper_connection_pre(
        x, phi, alpha, bias, out_flag=1, norm_eps=norm_eps, hc_eps=hc_eps)
    h_in, h_post, h_res, inv_rms, mm_res, h_pre = outputs

    # 断言区 1：shape / dtype
    assert h_in.shape == (B, S, D) and h_in.dtype == torch.bfloat16
    assert h_post.shape == (B, S, n) and h_post.dtype == torch.float32
    assert h_res.shape == (B, S, n, n) and h_res.dtype == torch.float32
    assert inv_rms.shape == (B, S) and inv_rms.dtype == torch.float32
    assert mm_res.shape == (B, S, n * n + 2 * n) and mm_res.dtype == torch.float32
    assert h_pre.shape == (B, S, n) and h_pre.dtype == torch.float32

    # 断言区 2：CPU golden 抽查 inv_rms（公式依据 docs：Rms 对每个 token 的 n*D 个元素求均方根）
    # golden 公式的归约轴由输出 shape [B,S] 与 docs 公式推断，待本地验证
    inv_rms_golden = torch.rsqrt(x_cpu.pow(2).mean(dim=(-2, -1)) + norm_eps)   # [B,S]
    err = (inv_rms.cpu().float() - inv_rms_golden).abs().max().item()
    assert err < 1e-2, f"inv_rms max abs err: {err}"
    print("Test passed!")


if __name__ == "__main__":
    import omni_training_custom_ops
    test_npu_manifold_constrained_hyper_connection_pre()
```

两个关键设计决定：

- **必须 `out_flag=1`**：csrc 的 Meta 实现在 `out_flag==0` 时把 `inv_rms/mm_res/h_pre` 置为空张量 `{0}`（[npu_manifold_constrained_hyper_connection_pre.cpp:L218-L222](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/manifold_constrained_hyper_connection_pre/csrc/npu_manifold_constrained_hyper_connection_pre.cpp#L218-L222)），要断言这三个输出就必须开全量输出；
- **golden 只抽查 inv_rms**：完整的六输出 golden（RmsNorm + 三路投影 + sigmoid）实现成本高，抽查最简单的标量场 inv_rms 已能证明「输入被正确消费、公式方向正确」；mm_res（\(x' \cdot \varphi\)）可作为进阶练习。

### 5.5 预期结果

1. 三个新文件（converter、wrapper、test）+ 三处 `__init__.py` 挂接，构成 pre 的完整三件套；
2. 有 NPU 环境时：`python test_npu_manifold_constrained_hyper_connection_pre.py` 打印 Test passed！；torchair 图模式下 pre 节点可成功翻译为 GE 算子；
3. 无 NPU 环境时：至少完成「三点对齐」静态检查（converter 签名 ↔ schema ↔ def 原型），并用 `python -m py_compile` 校验三个文件语法。

**待本地验证清单**：`attr.Int/attr.Float` 的函数名、`Optional[Tensor]` 在 fx2ge 中的处理方式、inv_rms golden 的归约轴、inv_rms 的容差阈值 1e-2、post converter 类型字符串前缀问题（4.2.3）。

## 6. 本讲小结

- **converter 的真实身份**：仓库里的 converter 不是参数转换 wrapper，而是 torchair 的 fx2ge 转换器——`@register_fx_node_ge_converter(算子.default)` 把「FX 节点 → `torchair.ge.custom_op`（GE 类型 + inputs + attrs + outputs）」的静态对照表注册进 torchair，只服务于 torchair 图模式；eager 与 `aot_eager`+NPUGraph 路径都不经过它。
- **三条硬契约**：converter 函数签名逐参对齐 torch schema（含 `.default` 重载与 `meta_outputs` 尾参）；inputs/outputs 名字与 `_def.cpp` 的 `Input()/Output()` 逐字一致；类型字符串照抄 OP_ADD 类名——post converter 少写 `AiInfra` 前缀是与自家 def 不一致的坑点（待确认）。
- **三件套是范式也是可选件**：全仓库只有 MHC post 家族三个算子配齐 `csrc + converter + test`；post 的 csrc 故意不配 autograd（反向由框架显式调 post_grad/mhc_post_grad 拼接），pre 的 csrc 则配齐 autograd 却没有 converter/test——每一件按使用方式裁剪。
- **注册链在顶端是断的**：`converter 文件 → converter/__init__.py → 算子/__init__.py → mhc/__init__.py` 逐级 `import *`，但顶层包不导入 ops_transformer，converter 注册需使用方显式 import；打包靠 `find_packages()` 自动收齐。
- **test 是冒烟不是精度**：五段式模板（参数/造数/调用/断言/main）只断言 shape 与 dtype，无数值 golden；本讲实践给 post 补了基于文档公式 \((H^{res})^{\mathsf{T}}x + h^{out}\otimes h^{post}\) 的 CPU 对比，把防线向前推了一步，真正的精度防线在 ST（u8-l3）。
- **三层职责一句话**：csrc 定义能力边界（schema/执行/autograd/Meta/校验），converter 让能力进图（静态翻译），wrapper 让能力好用（友好校验与语义化输出）。

## 7. 下一步学习建议

本讲完结后，torch_ops_extension 单元（u6）只剩收尾。建议：

1. **动手完成第 5 节综合实践**，并把三个示例文件与 post 家族三件套做一次目录级 diff，体会「范式复制」的开发方式；
2. **回头对照 u5-l3**：pre 算子在 aclnn 层的两段式接口（GetWorkspaceSize 的输出回拷、l0op 的默认值填充）与本讲 torch 侧的 schema 默认值，体会「默认值填充」在 aclnn 层和 torch 层各做了一遍——层次不同、动机相同；
3. **预习第 8 单元（u8-l1/u8-l3）**：本讲 test 只做 shape/dtype 断言的局限，正是 UT/ST 测试体系要解决的问题——faker 框架让 tiling 逻辑无硬件可测，ST 的 MARE/MERE/RMSE 与 L0/L1/L2 精度分级补上数值防线；
4. 若对图模式翻译意犹未尽，可在装有 torchair 的环境中阅读其安装目录下 `_ge_concrete_graph/fx2ge_converter` 的源码，找几个带 `Tensor?` 可选输入与标量属性的内置 converter，验证 5.2 中两处「待确认」的写法。
