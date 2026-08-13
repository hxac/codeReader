# 经典图压缩与算子融合优化流程

## 1. 本讲目标

本讲是专家层的入口讲义之一，聚焦 AMCT 的 **Classic 经典压缩流程** 的工程实现。学完本讲，你应当能够：

- 说清 `ModelOptimizer` 这个「pass 编排器」是如何把一组优化 pass 按顺序作用到模型上的，以及它为何对量化本身一无所知；
- 读懂 `BaseModuleFusionPass.run()` 的「先全量匹配、再统一改写」两段式框架，理解它为什么要这样设计；
- 把 `quantize()` → `InsertQuantizeModulePass`（插入伪量化）与 `convert()` → `ReplaceNpuQuantModulePass`（替换为 NPU 部署算子）这两步串成一条完整的「训练态 → 部署态」算子替换链路，并指出 `quant_to_deploy` 注册表在其中扮演的桥梁角色；
- 了解 `classic/graph_based/` 这套更重的、基于 ONNX 计算图的压缩工具箱（张量分解、知识蒸馏、通道剪枝）各自做什么、和本讲主线的 module 级 pass 有何不同。

> 本讲承接 [u7-l3 双注册表体系](./u7-l3-dual-registry.md)：那里讲清了 classic `AlgorithmRegistry`「二维键 + quant_to_deploy 反向映射」的静态结构，本讲回答的是**这两张表在运行时如何被两个 pass 消费、完成算子的两次替换**。

## 2. 前置知识

阅读本讲前，建议你已经具备以下认知（若没有，先看对应讲义）：

- **两条主线的区别**（见 u1-l3 / u7-l3）：AMCT 仓库里 `classic/` 下并存两条线——一条是面向大 LLM 的 PTQ 四阶段 CLI（eval/extract/ptq/deploy），另一条是面向中小模型（含 `Conv2d`、需要整图替换）的 **Classic 经典流程**。本讲讲的是后者。
- **伪量化 vs 部署算子**（见 u7-l3）：Classic 流程用「伪量化模块」做训练/校准（产出浮点 fake-quant，可反向），用「NPU 部署算子」做推理（烘焙真低比特权重、调用 `torch_npu`）。二者在 `AlgorithmRegistry` 里成对注册。
- **量化基本概念**（见 u2-l1）：scale/offset、权重（静态）/激活（动态）、per-channel/per-token 等术语。
- **pass（优化遍）的直觉**：借自编译器——把「对模型的一次改写」封装成一个对象，按固定顺序逐个跑，前一个 pass 的输出是后一个 pass 的输入。本讲的 `ModelOptimizer` 就是这种 pass 管线的一个极简实现。

一个关键术语先点透：本讲会出现**两个同名但不同的 `ModelOptimizer`**：

| 文件 | 作用对象 | 典型 pass | 被谁调用 |
| --- | --- | --- | --- |
| `amct_pytorch/classic/optimizer/model_optimizer.py` | 活的 `nn.Module` | `InsertQuantizeModulePass` / `ReplaceNpuQuantModulePass` | `classic/quantize.py` 的 `quantize()` / `convert()` |
| `amct_pytorch/classic/graph_based/amct_pytorch/optimizer/model_optimizer.py` | ONNX 计算图 + 模型 | `ConvBnFusionPass`、`InsertQuantPass` 等几十种图 pass | `graph_based/amct_pytorch/quantize_tool.py` 等 |

本讲 4.1、4.2 节讲的是**第一个**（module 级，主线）；4.3 节介绍 graph_based 时会带出**第二个**（图级）。务必不要混淆。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| `amct_pytorch/classic/optimizer/model_optimizer.py` | pass 编排器：维护有序 pass 列表，顺序执行 | 4.1 主角 |
| `amct_pytorch/classic/optimizer/base_module_fusion_pass.py` | 所有 module 级 pass 的基类，定义「匹配→改写」两段式 `run()` | 4.1 / 4.2 框架 |
| `amct_pytorch/classic/optimizer/insert_quantize_op_pass.py` | `InsertQuantizeModulePass`：把原算子换成伪量化模块 | 4.2 第一阶段 |
| `amct_pytorch/classic/optimizer/replace_npu_quant_pass.py` | `ReplaceNpuQuantModulePass`：把伪量化模块换成 NPU 部署算子 | 4.2 第二阶段 |
| `amct_pytorch/classic/quantize.py` | 对外入口 `quantize()` / `convert()`，负责装 pass、跑编排器 | 4.2 串联 |
| `amct_pytorch/algorithms/__init__.py` + `register_algo.py` | classic `AlgorithmRegistry`（`algo` 表 + `quant_to_deploy` 表） | 4.2 数据来源（承接 u7-l3） |
| `amct_pytorch/common/utils/model_util.py` | `ModuleHelper`：模块字典与按名替换 | 4.1 / 4.2 工具 |
| `amct_pytorch/classic/graph_based/amct_pytorch/auto_channel_prune_search.py` | 自动通道剪枝搜索（Taylor 敏感度 + 贪心搜索） | 4.3 主角 |
| `amct_pytorch/classic/graph_based/amct_pytorch/tensor_decompose/tensor_decompose.py` | 张量分解接口（Conv2d 拆成两个小卷积） | 4.3 同类能力 |
| `amct_pytorch/classic/graph_based/amct_pytorch/distillation_interface.py` / `prune_interface.py` | 知识蒸馏 / 通道剪枝接口 | 4.3 同类能力 |

## 4. 核心概念与源码讲解

### 4.1 ModelOptimizer：pass 编排器

#### 4.1.1 概念说明

`ModelOptimizer` 解决的问题是：**一次量化要做的「改写模型」工作往往不止一件**（先插伪量化、再换成部署算子、还可能先做 BN 融合……），如何把它们组织成一条可复用、可排序、互不耦合的流水线？

它的设计哲学是经典的 **Strategy / 插件模式**：编排器自己**完全不懂量化**，它只懂「拿到一个 pass，调它的 `run(model)`」。所有量化知识都被推进了一个个独立的 pass 对象里。这样一来：

- 加一个新 pass 不需要改编排器；
- 调整 pass 顺序只需要调整 `add_pass` 的调用顺序；
- 同一个编排器既能跑量化 pass，也能跑将来新增的剪枝/融合 pass。

#### 4.1.2 核心流程

```text
ModelOptimizer
  ├── __init__()      : self.__passes = []           # 空的有序列表
  ├── add_pass(p)     : self.__passes.append(p)      # 追加（顺序即执行顺序）
  ├── clear_pass()    : self.__passes.clear()        # 清空
  └── do_optimizer(model):
        for p in self.__passes:                       # 严格按插入顺序
            p.run(model)                              # 每个 pass 自己决定怎么改模型
```

三个要点：

1. **顺序敏感**：`do_optimizer` 就是 for 循环按 `__passes` 的插入顺序逐个 `run`。pass 的顺序由调用方（`quantize.py`）的 `add_pass` 顺序决定。
2. **私有列表**：`self.__passes` 双下划线触发 Python 名称改写（`_ModelOptimizer__passes`），外部无法直接篡改 pass 列表，只能通过 `add_pass` / `clear_pass`。
3. **无返回值**：`do_optimizer(model)` 原地改写传入的 `model`，不返回新对象——下游直接用原引用。

#### 4.1.3 源码精读

编排器本体非常薄，只有 40 行：

[amct_pytorch/classic/optimizer/model_optimizer.py:21-33](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/model_optimizer.py#L21-L33) —— `ModelOptimizer` 类与 `__init__`，核心就是一个 `self.__passes = []` 列表。

[amct_pytorch/classic/optimizer/model_optimizer.py:51-59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/model_optimizer.py#L51-L59) —— `do_optimizer`：for 循环里 `LOGGER.logi('Do {}'.format(type(model_pass)))` 打印当前 pass 类型，然后 `model_pass.run(model)`。这就是全部执行逻辑——编排器对 pass 内部一无所知。

那么 pass 的 `run(model)` 长什么样？答案在基类 `BaseModuleFusionPass`，它定义了所有 module 级 pass 的统一执行框架（见 4.2.3）。`InsertQuantizeModulePass` 和 `ReplaceNpuQuantModulePass` 都继承自它，只重写「匹配条件」和「改写动作」，执行框架复用基类。

> 小贴士：`amct_pytorch/classic/graph_based/amct_pytorch/optimizer/model_optimizer.py` 里有一个**同名** `ModelOptimizer`，它的 `do_optimizer(self, model, graph)` 多了一个 `graph` 参数，因为它服务于基于 ONNX 计算图的 pass（pass 在图节点上操作）。本节讲的是 module 级那个，签名只有 `(self, model)`。

#### 4.1.4 代码实践

**实践目标**：验证「编排器对量化一无所知、只是个 pass 跑步机」。

**操作步骤**（示例代码，可脱离 NPU 在纯 CPU + PyTorch 环境运行）：

```python
# 示例代码：用一个假 pass 验证 ModelOptimizer 的通用性
import torch.nn as nn
from amct_pytorch.classic.optimizer import ModelOptimizer, BaseModuleFusionPass

class CountLinearPass(BaseModuleFusionPass):
    """数一数模型里有多少个 Linear，与量化完全无关。"""
    def __init__(self):
        super().__init__()
        self.n = 0
    def match_pattern(self, module, name):
        return isinstance(module, nn.Linear)
    def do_pass(self, model, object_module, object_name):
        self.n += 1

model = nn.Sequential(nn.Linear(10, 20), nn.ReLU(), nn.Linear(20, 5))
opt = ModelOptimizer()
p = CountLinearPass()
opt.add_pass(p)
opt.do_optimizer(model)
print("Linear 数量 =", p.n)
```

**需要观察的现象**：`ModelOptimizer` 跑了一个跟量化毫无关系的 `CountLinearPass`，照样工作。这说明编排器是通用的 pass 执行器。

**预期结果**：打印 `Linear 数量 = 2`。

> 注意：`BaseModuleFusionPass` 基类里的 `match_pattern` 模板签名写得有点怪（`@staticmethod def match_pattern(self, module, name)`），但**具体 pass 都会重写它**，运行时走的是子类版本，正常工作。你只需记住「子类实现 `match_pattern` / `do_pass`，基类提供 `run()`」即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `add_pass(A)` 和 `add_pass(B)` 调换顺序，模型最终结果会受影响吗？
**答案**：会。`do_optimizer` 严格按插入顺序执行，pass 之间存在数据依赖（B 通常消费 A 的产物），顺序错了要么报错、要么语义错误。这正是 `quantize.py` 里两个 pass 顺序不可颠倒的原因。

**练习 2**：为什么 `do_optimizer` 不返回模型对象，而是原地改写？
**答案**：因为 pass 通过 `ModuleHelper.replace_module_by_name` 用 `setattr` 直接修改原模型的子模块引用（见 4.2.3）。返回新对象反而会让调用方丢失改写结果，原地改写最简单可靠。

---

### 4.2 量化算子插入/替换 pass：训练 → 部署的两阶段算子替换

#### 4.2.1 概念说明

这是本讲的核心。Classic 流程把「量化」拆成两次对模型的算子替换，对应两个对外 API：

- **`quantize(model, config)`**：跑 `InsertQuantizeModulePass`。把原始算子（如 `nn.Linear`）原地换成**伪量化模块**（如 `LinearAWQuant`）。伪量化模块在前向里做 fake-quant（量化再反量化回浮点），既能模拟低比特误差、又能让梯度正常回传，供校准/训练求 scale/offset。
- **`convert(model)`**：跑 `ReplaceNpuQuantModulePass`。把上一步插入的伪量化模块再换成 **NPU 部署算子**（如 `NpuWeightQuantizedLinear`）。部署算子把 scale/offset 烘焙进权重，产出真低比特权重，前向调用 `torch_npu` 算子，供昇腾 NPU 推理。

两次替换的「桥梁」是 u7-l3 讲过的 `AlgorithmRegistry.quant_to_deploy` 反向映射表：第一阶段写入的模块**类型**，恰好是第二阶段查表的 **key**。

```
nn.Linear  ──quantize()──▶  LinearAWQuant  ──convert()──▶  NpuWeightQuantizedLinear
(原始)        插入伪量化      (训练/校准态)     替换部署算子    (推理态, 真低比特)
```

为什么非要拆两步、而不是一步到位？因为伪量化模块承担「训练/校准」职责，需要在两次调用之间插入**用户自己的训练循环**（喂校准数据、跑前向反向、让 AWQ/GPTQ 等算法算出 scale）。这个「中间训练」是用户代码，不在 AMCT 内。所以 AMCT 只能提供「插」和「换」两个边界动作，中间留白给用户。

#### 4.2.2 核心流程

两个 pass 共享同一个执行框架（`BaseModuleFusionPass.run`），分两段：

```text
run(model):
  # 第一段：全量匹配（先快照，后改写，避免边遍历边修改）
  matched = {}
  for name, module in ModuleHelper(model).named_module_dict.items():
      if self.match_pattern(module, name):     # 子类定义「什么样的模块要处理」
          matched[name] = module
  # 第二段：逐个改写
  for name, module in matched.items():
      self.do_pass(model, module, name)        # 子类定义「怎么改」
```

两个 pass 的差异只在「匹配什么」和「换成什么」：

| pass | match_pattern 判定 | do_pass 动作 | 数据来源 |
| --- | --- | --- | --- |
| `InsertQuantizeModulePass` | 层名 ∈ 配置层 且 `type(module).__name__ == 源算子类型`（如 `'Linear'`） | 实例化伪量化模块包住原模块，按名替换 | `AlgorithmRegistry.algo[算法名][源算子]` |
| `ReplaceNpuQuantModulePass` | `type(module)` 或其 `__name__` ∈ `quant_to_deploy` 的键 | 选一个部署算子包住伪量化模块，按名替换 | `AlgorithmRegistry.quant_to_deploy[伪量化模块类型]` |

#### 4.2.3 源码精读

**(a) 两段式执行框架**——所有 module 级 pass 的根基：

[amct_pytorch/classic/optimizer/base_module_fusion_pass.py:54-69](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/base_module_fusion_pass.py#L54-L69) —— `run()` 先在 `named_module_dict` 上匹配收集（Step1，L60-65），再统一对命中模块执行 `do_pass`（Step2，L67-69）。**先收集后改写**是关键：如果在遍历中直接替换，会改变后续模块的类型/字典内容，导致匹配逻辑错乱。

`named_module_dict` 是一次性的模块快照：

[amct_pytorch/common/utils/model_util.py:25-29](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/model_util.py#L25-L29) —— `ModuleHelper.__init__` 遍历 `model.named_modules()` 建一张 `{名字: 模块}` 字典。

按名替换的核心：

[amct_pytorch/common/utils/model_util.py:31-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/model_util.py#L31-L39) —— `replace_module_by_name` 把名字按 `.` 切分，逐级 `getattr` 下钻到父模块，最后 `setattr(父, 末段, 新模块)`。这就是「原地换子模块」的实现，所有 pass 的改写都落到这一句 `setattr`。

**(b) 第一阶段 `InsertQuantizeModulePass`**——把原算子换成伪量化模块：

[amct_pytorch/classic/optimizer/insert_quantize_op_pass.py:43-66](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/insert_quantize_op_pass.py#L43-L66) —— `match_pattern`：先看 `name` 是否在配置要量化的层列表里（L52），再从配置取出该层算法名，查 `AlgorithmRegistry.algo[算法名]` 拿到「源算子类型 → 伪量化模块类」字典（L57-61），若 `type(module).__name__` 命中某个源算子（如 `'Linear'`），就把对应的伪量化类缓存进 `self.quantize_ops[name]` 并返回 True。

[amct_pytorch/classic/optimizer/insert_quantize_op_pass.py:68-86](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/insert_quantize_op_pass.py#L68-L86) —— `do_pass`：用缓存好的伪量化类 `self.quantize_ops[name](原模块, 名字, 层配置)` 实例化一个新模块（构造时把原始 `nn.Linear` 传进去包起来），再 `ModuleHelper.replace_module_by_name` 原地换上。

**(c) 第二阶段 `ReplaceNpuQuantModulePass`**——把伪量化模块换成 NPU 部署算子：

[amct_pytorch/classic/optimizer/replace_npu_quant_pass.py:41-58](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/replace_npu_quant_pass.py#L41-L58) —— `match_pattern`：判定 `type(module)`（或其 `__name__`）是否是 `AlgorithmRegistry.quant_to_deploy` 的键。注意——上一阶段插入的伪量化模块**类型**（如 `LinearAWQuant`）正是这里的 key，这就是两阶段衔接的纽带。

[amct_pytorch/classic/optimizer/replace_npu_quant_pass.py:60-103](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/replace_npu_quant_pass.py#L60-L103) —— `do_pass`：查 `quant_to_deploy` 拿到候选部署算子列表；先做幂等保护（L79-81，若已经是部署算子就跳过，避免重复 convert 报错）；对 `FlatQuantAttention/FlatQuantMLP` 走特殊的重参数化分支（L83-95，需访问上层 layernorm）；其余走 `_get_deploy_module`。

[amct_pytorch/classic/optimizer/replace_npu_quant_pass.py:121-139](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/replace_npu_quant_pass.py#L121-L139) —— `_should_use_deploy_op`：当一个伪量化模块对应**多个**部署算子时（如 `minmax` 的 `Linear` 同时注册了 `NpuWeightQuantizedLinear` 和 `NpuQuantizationLinear`），按模块属性二选一——`Conv2d` 来源选 `NpuQuantizationConv2d`；`dynamic=True` 或 `scale_d` 已设置（W+A 全量化）选 `NpuQuantizationLinear`；否则（W-only）选 `NpuWeightQuantizedLinear`。这正好对应 u2-l3 讲的 W vs W+A 选型。

**(d) 串联入口 `quantize()` / `convert()`**：

[amct_pytorch/classic/quantize.py:32-47](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize.py#L32-L47) —— `quantize(model, config)`：`parse_config` 把用户配置 + `AlgorithmRegistry` 解析成 `layer_config`，新建编排器、`add_pass(InsertQuantizeModulePass(layer_config))`、`do_optimizer(model)`。**只加一个 pass**。

[amct_pytorch/classic/quantize.py:50-59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize.py#L50-L59) —— `convert(model)`：同样只加一个 pass `ReplaceNpuQuantModulePass()`，跑编排器。注意它**不接收 config**——因为要换成什么部署算子，完全由 `quant_to_deploy` 表和伪量化模块自身的属性（`scale_d`、`dynamic` 等）决定，这些在第一阶段+中间训练时已经定型。

**(e) 两张表的填充**（承接 u7-l3，这里只看运行时怎么被消费）：

[amct_pytorch/algorithms/register_algo.py:28-44](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/register_algo.py#L28-L44) —— `Algorithm.register` 同时维护两张表：`self.algo[算法名][源算子] = 伪量化模块类`（供第一阶段查）和 `self.quant_to_deploy[伪量化模块类] = [部署算子...]`（供第二阶段查）。

[amct_pytorch/algorithms/__init__.py:71-76](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L71-L76) —— 几条典型注册：`gptq/awq`（W-only）→ 伪量化 `GPTQuant/LinearAWQuant` → 部署 `NpuWeightQuantizedLinear`；`smoothquant`（W+A）→ `SmoothQuant` → `NpuQuantizationLinear`；`minmax` → 同时绑定两个部署算子（运行时按 `_should_use_deploy_op` 选）。

#### 4.2.4 代码实践

**实践目标**（即本讲指定任务）：阅读 `model_optimizer.py` 与 `quantize.py`，列出 pass 执行顺序，并说明两个 pass 如何配合完成「训练 → 部署」的算子替换。

**操作步骤**：

1. 打开 `amct_pytorch/classic/quantize.py`，读 `quantize()`（L33-47）与 `convert()`（L51-59）。注意两者**各自新建一个 `ModelOptimizer`、各只加一个 pass**——它们不是同一次 `do_optimizer` 里的两个 pass，而是用户在**不同时间点**分别调用的两次编排。
2. 列出顺序：
   - 调用 `quantize(model, config)` → 编排器执行 `[InsertQuantizeModulePass]` → 原算子变伪量化模块；
   - **（用户代码：喂校准数据、训练/校准，伪量化模块算出 scale/offset）**；
   - 调用 `convert(model)` → 编排器执行 `[ReplaceNpuQuantModulePass]` → 伪量化模块变 NPU 部署算子。
3. 说明配合机制（一句话）：第一阶段按 `AlgorithmRegistry.algo` 把 `nn.Linear` 换成伪量化类 `X`；第二阶段按 `AlgorithmRegistry.quant_to_deploy[X]` 把 `X` 换成部署算子。两张表通过「伪量化模块类型」这一共同 key 串起两次替换。
4. 用下面示例代码（可在 CPU + PyTorch 跑，无需 NPU）亲手观察两次类型变化：

```python
# 示例代码：用 mock 复现两次替换的类型流转（不依赖真 AlgorithmRegistry / torch_npu）
import torch.nn as nn
from amct_pytorch.classic.optimizer import ModelOptimizer, BaseModuleFusionPass
from amct_pytorch.common.utils.model_util import ModuleHelper

class FakeQuantLinear(nn.Module):   # 伪量化模块（替身）
    def __init__(self, ori): super().__init__(); self.ori = ori
class NpuDeployLinear(nn.Module):   # 部署算子（替身）
    def __init__(self, ori): super().__init__(); self.ori = ori

class InsertPass(BaseModuleFusionPass):
    def match_pattern(self, m, name): return isinstance(m, nn.Linear)
    def do_pass(self, model, m, name):
        ModuleHelper.replace_module_by_name(model, name, FakeQuantLinear(m))

class ReplacePass(BaseModuleFusionPass):
    def match_pattern(self, m, name): return isinstance(m, FakeQuantLinear)
    def do_pass(self, model, m, name):
        ModuleHelper.replace_module_by_name(model, name, NpuDeployLinear(m))

model = nn.Sequential(nn.Linear(8, 8))
def show(tag): print(tag, type(model[0]).__name__)

show("原始        :")                       # nn.Linear
ModelOptimizer().add_pass  # 仅示意，真正调用见下
opt = ModelOptimizer(); opt.add_pass(InsertPass()); opt.do_optimizer(model)
show("quantize后 :")                          # FakeQuantLinear
opt = ModelOptimizer(); opt.add_pass(ReplacePass()); opt.do_optimizer(model)
show("convert后  :")                          # NpuDeployLinear
```

**需要观察的现象**：顶层子模块的类型按 `nn.Linear → FakeQuantLinear → NpuDeployLinear` 三态流转；且第二次 `ReplacePass` 的 `match_pattern` 只认 `FakeQuantLinear`，印证了「第二阶段靠第一阶段产出的类型来匹配」。

**预期结果**：依次打印 `nn.Linear`、`FakeQuantLinear`、`NpuDeployLinear`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `convert()` 不需要传 `config`，而 `quantize()` 需要？
**答案**：`quantize()` 要根据配置决定「哪些层、用什么算法、换成哪个伪量化模块」，所以需要 config 查 `algo` 表；`convert()` 换成哪个部署算子完全由伪量化模块自身的类型和属性（`scale_d`/`dynamic` 等）决定，这些在第一阶段+中间训练已定型，查 `quant_to_deploy` 表即可，无需 config。

**练习 2**：若对同一个模型连调两次 `convert()`，会发生什么？
**答案**：第二次会在 `do_pass` 里命中幂等保护（`isinstance(object_module, deploy_ops[0])` 为真，直接 return），不会重复替换、不会报错。这是为了让 convert 可重入。

**练习 3**：`minmax` 算法在 `Linear` 上注册了两个部署算子 `[NpuWeightQuantizedLinear, NpuQuantizationLinear]`，运行时如何选？
**答案**：由 `_should_use_deploy_op` 按属性选——若伪量化模块 `scale_d is None`（只压了权重，W-only）选 `NpuWeightQuantizedLinear`；若 `scale_d` 已设置或 `dynamic=True`（连激活一起压，W+A）选 `NpuQuantizationLinear`。

---

### 4.3 graph_based 图压缩能力：张量分解、蒸馏、通道剪枝

#### 4.3.1 概念说明

4.1、4.2 讲的 module 级 pass 作用在**活的 `nn.Module`** 上，靠 `setattr` 换子模块，够轻、够直接，但能力有限——它看不到「整个计算图的结构」（比如某个 Conv 的输出被谁消费、能不能和后面的 BN 融合、能不能沿通道拆开）。

`classic/graph_based/` 是另一套**更重的工具箱**：它先把模型导出成 **ONNX 计算图**，在图节点层面做结构性压缩。因为依赖 `onnx` / `protobuf`，这套代码在 u1-l3 里被描述为「重依赖、懒加载」。它包含三类典型能力：

| 能力 | 入口文件 | 做什么 |
| --- | --- | --- |
| **张量分解** | `tensor_decompose/tensor_decompose.py` | 把一个大 `Conv2d` 的权重用 SVD 思路分解成两个小卷积（first + last），降 FLOPs |
| **知识蒸馏** | `distillation_interface.py` | 用大模型（教师）的输出监督小模型（学生）训练，做 QAT 式量化感知训练 |
| **通道剪枝** | `prune_interface.py` / `auto_channel_prune_search.py` | 按通道重要性（敏感度）剪掉不重要的通道，降体积/算力 |

这三类能力的 pass 跑在**第二个 `ModelOptimizer`**（`graph_based/amct_pytorch/optimizer/model_optimizer.py`，签名 `do_optimizer(self, model, graph)`）上，pass 在 ONNX 图节点上操作（如 `ConvBnFusionPass`、`InsertQuantPass`），和本讲主线的 module 级 pass 是两套体系。

本节以指定源码 `auto_channel_prune_search.py` 为样本，讲清「自动通道剪枝搜索」这条链路，另两类能力点到为止。

#### 4.3.2 核心流程

**自动通道剪枝搜索**（`auto_channel_prune_search`）的目标是：在给定算力/体积预算下，自动决定**每层剪掉哪些通道**，并输出一份剪枝配置。它的流程是典型的「评估重要性 → 贪心选择」：

```text
1. 导出 ONNX 图：Parser.export_onnx → parse_net_to_graph
2. 逐节点算「代价」：get_graph_bitops 估算每个可剪层（Conv/MatMul/Gemm）的 bitops
3. 评估「通道重要性」：TaylorLossSensitivity
     - 前向 + 反向，收集每层权重的梯度 g 与权重 w
     - 逐通道计算 Taylor 显著性 saliency ≈ ‖g ⊙ w‖₁（一阶泰勒近似剪掉该通道的损失增量）
4. 贪心搜索：GreedySearch 按显著性从低到高尝试剪通道，在满足预算的前提下剪掉最不重要的
5. 输出剪枝配置文件 output_cfg
```

其中「Taylor 敏感度」的直觉：剪掉一个通道相当于把它的权重置零，损失的变化量用一阶泰勒展开近似为

\[
\Delta \mathcal{L} \approx g^{\top}\Delta w = -\sum_{c} g_{c}\, w_{c}
\]

取绝对值并在非通道维上求范数，得到每个通道的「剪掉它的代价」。代价越低的通道越该先剪。代码里就是 `taylor = weights * grads` 再 `taylor.norm(p=1, dim=非通道轴)`。

「bitops 代价」则是 FLOPs 乘以位宽平方：

\[
\text{bitops} = \text{flops} \times (\text{element\_size} \times 8)^{2}
\]

这是一个把「计算量」和「精度位宽」合在一起的粗略算力代理指标——位宽翻倍、bitops 翻四倍，用来在搜索时量化「剪掉这一层省下多少算力」。

#### 4.3.3 源码精读

**(a) 对外入口与建图**：

[amct_pytorch/classic/graph_based/amct_pytorch/auto_channel_prune_search.py:271-321](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/graph_based/amct_pytorch/auto_channel_prune_search.py#L271-L321) —— `auto_channel_prune_search(model, config, input_data, output_cfg, sensitivity='TaylorLossSensitivity', search_alg='GreedySearch')`：校参后 `Parser.export_onnx` 导图、`parse_net_to_graph` 解析，把字符串名映射成实际类（`GreedySearch` / `TaylorLossSensitivity`），建 `AutoChannelPruneSearch` 并 `amc.run(input_data)`。

**(b) 逐节点代价估算**：

[amct_pytorch/classic/graph_based/amct_pytorch/auto_channel_prune_search.py:72-92](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/../amct_pytorch/auto_channel_prune_search.py#L72-L92) —— `get_graph_bitops`：遍历图节点，只看 `CAPACITY.PRUNABLE_ONNX_TYPES` 里的可剪类型（Conv / MatMul / Gemm），分别用 `_cal_conv2d_flops` / `_cal_matmul_flops` 算 flops，再 `bitops = flops * ((element_size*8)**2)`，记录每层的 `cin/cout/bitops`。

[amct_pytorch/classic/graph_based/amct_pytorch/auto_channel_prune_search.py:94-116](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/../amct_pytorch/auto_channel_prune_search.py#L94-L116) —— `_cal_conv2d_flops`：从 weight 形状与输出尺寸算卷积 flops（含 group、bias），末尾乘 `element_size*8` 的平方得 bitops。

**(c) Taylor 敏感度**：

[amct_pytorch/classic/graph_based/amct_pytorch/auto_channel_prune_search.py:197-219](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/../amct_pytorch/auto_channel_prune_search.py#L197-L219) —— `compute_taylor_by_channel`：`taylor = weights * grads`（即 `g ⊙ w`），按 `cout/cin` 切分到通道维，再用 `taylor.norm(p=1, dim=非通道轴)` 把每条通道的显著性塌缩成一个标量。这就是上面公式 \(\Delta\mathcal{L}\) 的逐通道量化实现。

[amct_pytorch/classic/graph_based/amct_pytorch/auto_channel_prune_search.py:221-260](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/../amct_pytorch/auto_channel_prune_search.py#L221-L260) —— `get_backward_grad`：喂数据跑 `test_iteration` 次前向 + `loss.backward()`，逐层累加权重梯度，最后拷贝权重，返回 `(grads, weights)` 供上一步使用。

**(d) 同类能力速览（非重点，建立全景）**：

- **张量分解** [amct_pytorch/classic/graph_based/amct_pytorch/tensor_decompose/tensor_decompose.py:32-80](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/graph_based/amct_pytorch/tensor_decompose/tensor_decompose.py#L32-L80)：`auto_decomposition(model, decompose_info_path=None)` 遍历所有 `nn.Conv2d`，对每个权重调底层 C++ `tensor_decomposition` 做 SVD 类分解，按返回的 `mode`（`FCSK/SCFK/FCFK/SCSK/UNCHANGE`，分别对应「首/末通道 × 首/末核」）把一层卷积拆成 `nn.Sequential(Conv2d_first, Conv2d_last)` 重新挂回模型；可选把分解信息存 json 供 `decompose_network` 复用。C++ 侧的分解模式枚举见 [amct_pytorch/classic/graph_based/amct_tensor_decompose/inc/tensor_decomposition.h:29-35](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/graph_based/amct_tensor_decompose/inc/tensor_decomposition.h#L29-L35)（`DM_FIRST_CHANNEL_FIRST_KERNEL` 等即 `FCSK` 的全称）。
- **知识蒸馏** [amct_pytorch/classic/graph_based/amct_pytorch/distillation_interface.py:43-70](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/graph_based/amct_pytorch/distillation_interface.py#L43-L70)：`create_distill_config` 导出 ONNX、建蒸馏配置，配合 `distill/` 下的 helper/sample 做教师→学生蒸馏训练。
- **通道剪枝（retrain 版）** `prune_interface.py` 的 `create_prune_retrain_model`：创建可重训的剪枝模型，跑 graph 级 `ModelOptimizer`（含 `InsertRetrainPrunePass` 等）。

#### 4.3.4 代码实践

**实践目标**：跟踪 `auto_channel_prune_search` 的「建图 → 算代价 → 算敏感度 → 搜索」四步，理解它为何要先导成 ONNX 图。

**操作步骤**（源码阅读型实践，无需运行）：

1. 读 [auto_channel_prune_search.py:300-304](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/graph_based/amct_pytorch/auto_channel_prune_search.py#L300-L304)：为什么用 `Parser.export_onnx` + `parse_net_to_graph` 把模型变成图？——答：通道剪枝要判断「某个通道的输出会被哪些下游算子消费」（producer/consumer 关系），这种结构信息在 `nn.Module` 上拿不到，必须求助于显式计算图。
2. 读 `get_graph_bitops`（L72-92）：找出它只统计哪几类节点（`PRUNABLE_ONNX_TYPES` 里的 Conv/MatMul/Gemm），并解释 `bitops` 里的平方项 `(element_size*8)**2` 为何是位宽的平方（粗略的算力-精度联合代理）。
3. 读 `compute_taylor_by_channel`（L197-219）：手算一个 2 通道的例子——若 `w=[1, 5]`、`g=[-2, 1]`，则 `taylor=w*g=[-2, 5]`，两个通道的显著性 `|−2|` 与 `|5|`，显然通道 0 更该被剪（剪它损失更小）。
4. 对比主线：本节的能力跑在 graph 级 `ModelOptimizer`（带 `graph` 参数）上，而 4.2 的量化替换跑在 module 级 `ModelOptimizer` 上——把这条区别写进你的笔记。

**需要观察的现象**：graph_based 的所有 pass 都需要 `(model, graph)` 两个对象；module 级 pass 只要 `model`。

**预期结果**：能用自己的话说出「为什么通道剪枝必须导成图」——因为需要 producer/consumer 结构信息来保证剪掉的通道在所有上下游算子上一致地同步。

#### 4.3.5 小练习与答案

**练习 1**：`bitops = flops * (bitwidth)^2`，为什么用平方而不是一次方？
**答案**：这是一个启发式代理指标：卷积/矩阵乘涉及「权重 × 激活」，两侧都受位宽影响，故用位宽的平方近似「精度相关的算力代价」。它只用于搜索时比较相对大小，不追求绝对精确。

**练习 2**：张量分解为什么只对 `Conv2d` 生效，且 `mode == UNCHANGE` 时跳过？
**答案**：分解依赖权重张量的低秩结构（SVD/EVBMF 估计秩），只对 4D 卷积权重有定义；当某层权重不存在可分解的低秩结构（分解不划算）时，C++ 侧返回 `UNCHANGE`，Python 侧 `_decompose_one_layer` 直接返回空列表、保持原层不动。

**练习 3**：graph_based 的 `ModelOptimizer.do_optimizer(self, model, graph)` 比 module 级多一个 `graph` 参数，这说明它的 pass 操作对象是什么？
**答案**：图节点（ONNX node）。它的 pass（如 `ConvBnFusionPass`、`InsertQuantPass`）在计算图的节点/边上做增删改，而不是用 `setattr` 换 `nn.Module` 子模块。这也是 graph_based 能做「结构感知」压缩（融合、剪枝、分解）的根本原因。

## 5. 综合实践

**任务**：把本讲三个模块串起来——用一段示例代码完整复现 Classic 量化「两次替换」的类型流转，并手画 graph_based 通道剪枝的「建图→评估→搜索」流程对照图。

**步骤**：

1. **跑通两次替换**（CPU 可跑）：基于 4.2.4 的示例代码，扩展成「带配置的迷你 Classic 流程」——定义两个算法 `A`、`B`，各自有伪量化模块与部署算子，再写一个迷你 `parse_config` 把层名映射到算法名，验证：
   - `quantize` 后，指定层变成对应算法的伪量化模块；
   - `convert` 后，再变成对应部署算子；
   - 用两个不同算法量化两层，观察它们各自走向不同的部署算子（模拟 W-only vs W+A 的分流）。
2. **画对照图**：在纸上画两条泳道——
   - 上泳道（module 级）：`nn.Linear →[quantize]→ 伪量化 →[用户训练]→ 伪量化(带scale) →[convert]→ NPU部署算子`，标注每步用到的表（`algo` / `quant_to_deploy`）；
   - 下泳道（graph 级）：`nn.Module →[export_onnx]→ ONNX图 →[get_bitops]→ 代价 →[taylor]→ 敏感度 →[greedy]→ 剪枝配置`，标注它用的是第二个 `ModelOptimizer`、操作图节点。
3. **写一句话总结**两条线的根本差异：module 级 pass 用 `setattr` 换子模块、做量化替换；graph 级 pass 在 ONNX 节点上操作、做结构压缩。

**预期结果**：你能不看讲义，向别人讲清「`quantize` 和 `convert` 各加了一个什么 pass、它们靠哪张表衔接」，以及「为什么通道剪枝要导成图」。

## 6. 本讲小结

- `ModelOptimizer` 是个与量化无关的通用 pass 编排器：维护有序 pass 列表，`do_optimizer` 按 `add_pass` 顺序逐个 `run(model)`，所有量化知识都被封装进 pass。
- `BaseModuleFusionPass.run()` 提供「先全量匹配 `match_pattern`、再统一改写 `do_pass`」的两段式框架，`replace_module_by_name` 用 `setattr` 原地换子模块。
- Classic 量化的算子替换分两阶段：`quantize()` 跑 `InsertQuantizeModulePass` 把原算子换成伪量化模块（查 `algo` 表），`convert()` 跑 `ReplaceNpuQuantModulePass` 把伪量化模块换成 NPU 部署算子（查 `quant_to_deploy` 表），两表以「伪量化模块类型」为共同 key 衔接。
- `convert` 不需要 config——换成哪个部署算子由伪量化模块的属性（`scale_d`/`dynamic`/`ori_module_type`）经 `_should_use_deploy_op` 决定；同一伪量化模块可对应多个部署算子（如 W-only vs W+A）。
- 当伪量化模块对应多个部署算子时，按属性二选一：`Conv2d` 来源→`NpuQuantizationConv2d`、W+A（`scale_d` 已设/`dynamic`）→`NpuQuantizationLinear`、W-only→`NpuWeightQuantizedLinear`。
- `classic/graph_based/` 是另一套基于 ONNX 计算图的压缩工具箱（张量分解/蒸馏/通道剪枝），跑在第二个、带 `graph` 参数的 `ModelOptimizer` 上；`auto_channel_prune_search` 用 Taylor 一阶敏感度 `‖g⊙w‖₁` 评估通道重要性、用 bitops（flops×位宽²）估代价、贪心搜索输出剪枝配置。

## 7. 下一步学习建议

- **若想深入 Classic 经典流程的图级 pass**：阅读 `amct_pytorch/classic/graph_based/amct_pytorch/quantize_tool.py`，它是 graph 级 `ModelOptimizer` 最大的调用方，能看到几十种 pass（`ConvBnFusionPass`、`InsertQuantPass`、`ReplaceQuantPass` 等）如何编排成「校准 → 量化 → 部署」的完整图变换流水线。
- **若想回到 LLM PTQ 主线**：本讲的两次算子替换是 module 级「小模型」思路；大 LLM 因显存放不下整图、改用块级重建，对应的部署烘焙在 [u4-l4 部署导出 deploy](./u4-l4-deploy-export.md) 与 [u7-l2 量化数据类型与 export_deploy 落盘](./u7-l2-dtypes-export.md) 里讲过（`export_block_deploy` / `quant_payload`），可对照体会两种部署路径的差异。
- **若对剪枝/分解感兴趣**：继续读 `prune_interface.py`（retrain 式通道剪枝）与 `tensor_decompose.py` 配合 C++ 源码 `amct_tensor_decompose/src/tensor_decomposition.cpp`（SVD/EVBMF 秩估计的实现细节）。
