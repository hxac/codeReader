# QuantAlgorithmBase 与 is_observe 通路

## 1. 本讲目标

本讲打开 AMCT 量化算法的「公共契约」黑盒。读完本讲你应该能够：

- 说清 `QuantAlgorithmBase` 定义的五个接口（`calib_forward` / `forward` / `trainable_params` / `export_ptq_params` / `load_ptq_params`）各自承担什么职责。
- 解释 `is_observe` 这个布尔开关如何让同一个算法模块在校准态（统计）与量化态（伪量化）之间切换，从而复用同一段前向代码。
- 看懂 PTQ 主流程里 `set_model_to_observe` 的一对调用（True → 生成 GT → False）为什么是「重建训练」能成立的前提。

本讲是 u6（量化算法机制）的开篇。它只讲「算法基类与通路开关」这一件事，具体某个算法的数学细节（AWQ 网格搜索、FlatQuant 结构变换等）留给 u6-l3、u6-l4。

## 2. 前置知识

本讲承接两篇讲义，下面几个结论会直接用到，不再重复论证：

- **u4-l2（PTQ 主流程）**：PTQ 把每个待量化子模块切成 `PtqUnit`，逐单元跑「准备 batch → 求解 → finalize → 存盘」。求解目标是**重建**——让量化子模块的输出逼近原始浮点子模块的输出（GT，ground truth）。原始权重始终冻结，只训练算法的可学习参数。
- **u4-l3（BlockwiseSolver）**：求解器靠 `_collect_trainable_param_groups` + `trainable_params()` 点名，只把算法暴露出来的参数加入优化器；重建损失是 MSE。
- **u5-l3（quant_apply）**：量化算子通过「原地替换子模块」挂到 decoder layer 上，`ActivationQuantizer` 挂在 Linear 之前做激活量化，`QuantLinear` 内部含 `WeightQuantizer` 做权重量化。挂载后，算法对象就**永久寄生**在模型子模块里了。

一个随之而来的关键问题：算法模块既已挂在模型上，那 `materialize_gt` 跑「原始浮点模块」生成重建目标时，激活不就会被算法和量化器篡改吗？GT 还是「干净的浮点输出」吗？

这正是 `is_observe` 要解决的问题，也是本讲的主线。

补充两个 PyTorch 基础术语：

- **`nn.Module`**：PyTorch 所有网络层的基类。一个对象只要继承 `nn.Module`，它内部的 `torch.nn.Parameter` 就能被 `parameters()` / `named_parameters()` 枚举，从而被优化器管理。
- **buffer**：`register_buffer` 注册的张量，属于模块状态（会随 `state_dict` 存盘、随 `.to(device)` 搬迁），但**不是**可学习参数（不进优化器、没有 `requires_grad`）。本讲里 LAC 的 `maxval`/`minval` 就是 buffer。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/algorithms/quant/base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/base.py) | 算法抽象基类 `QuantAlgorithmBase`，定义五个接口与 `is_observe` 初值 |
| [amct_pytorch/algorithms/quant/auto_clip.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py) | `LAC`（激活可学习截断）与 `LWC`（权重可学习截断）两个具体算法，LAC 是本讲主角示例 |
| [amct_pytorch/quantization/modules/quant_base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py) | `ActivationQuantizer` / `WeightQuantizer`——`is_observe` 的真正消费方，决定走 `calib_forward` 还是 `forward` |
| [amct_pytorch/common/models/llm/common/quant_apply.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py) | `set_model_to_observe`——批量翻转子树里所有 `is_observe` 标志 |
| [amct_pytorch/workflows/llm_ptq.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py) | `_prepare_unit_batch`——在生成 GT 前后包夹 `set_model_to_observe(True/False)` |
| [amct_pytorch/common/datasets/ptq_provider.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py) | `materialize_gt`——在 observe 态下跑原始浮点模块产出重建目标 |

---

## 4. 核心概念与源码讲解

### 4.1 QuantAlgorithmBase 抽象接口

#### 4.1.1 概念说明

AMCT 的 `--algos`（如 `lwc lac flatquant`）背后是一族算法类（LWC、LAC、FlatQuant、OmniQuant、AutoRound）。这些算法干的事天差地别——有的截断、有的做矩阵分解、有的搜索缩放因子——但它们都必须遵守同一份**接口契约**，这套契约就是 `QuantAlgorithmBase`。

为什么需要这份契约？因为主流程（workflow、solver、quant 模块）不认识具体算法，它只认基类提供的几个方法。只要算法实现这几个方法，主流程就能：

1. 在校准态调 `calib_forward` 让算法收集统计量；
2. 在训练/量化态调 `forward` 让算法施加变换；
3. 用 `trainable_params()` 向求解器暴露要训练的参数；
4. 用 `export_ptq_params()` / `load_ptq_params()` 把训练好的参数存成 `.pt`、或读回复用（断点续跑）。

换句话说，`QuantAlgorithmBase` 是算法与主流程之间的**插座标准**。

#### 4.1.2 核心流程

一个算法对象的生命周期里，基类五个接口被调用的时机大致是：

```text
构建期：  __init__ 注册 Parameter / buffer，is_observe 初值 = False
校准期：  主流程把 is_observe 置 True  → 主流程反复调 calib_forward(x) 收集统计
          （calib_forward 必须返回 x 本身，不能改激活）
训练期：  主流程把 is_observe 置 False → solver 调 forward(x) 施加截断/变换
          solver 用 trainable_params() 收集可学习参数进优化器
存档期：  solver.finalize() 调 export_ptq_params() → workflow 存 .pt
复用期：  下次启动调 load_ptq_params(params) 把 .pt 读回（断点续跑）
```

注意 `calib_forward` 与 `forward` 是**同一个对象上的两条通路**，由 `is_observe` 选择走哪条——这是 4.2 的主题。

#### 4.1.3 源码精读

基类本体很短，信息密度却很高：

[amct_pytorch/algorithms/quant/base.py:23-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/base.py#L23-L39) —— 类声明、`is_observe` 初值、`calib_forward` 默认实现、`forward` 抽象方法。

```python
class QuantAlgorithmBase(nn.Module, ABC):
    def __init__(self):
        super().__init__()
        self.is_observe = False          # 默认「量化态」，不是「观察态」

    def calib_forward(self, x, *args, **kwargs):
        return x                         # 默认：原样返回，什么都不统计

    @abstractmethod
    def forward(self, x, *args, **kwargs):
        raise NotImplementedError        # 子类必须实现「真正的量化行为」
```

四个关键点：

1. **`nn.Module, ABC` 双继承**：`nn.Module` 让算法的 `Parameter` 能被 PyTorch 跟踪（进而被求解器优化、被 `state_dict` 存盘）；`ABC`（抽象基类）配合 `@abstractmethod` 强制子类必须实现 `forward`，否则实例化时报 `TypeError`。
2. **`is_observe = False`**：默认是「量化态」。这意味着除非主流程显式翻成 `True`，算法一上来就按 `forward` 走真实量化——校准态是一种需要主动开启的「特殊模式」。
3. **`calib_forward` 默认 `return x`**：不重写时，校准态就是个透明的恒等函数。需要统计量的算法（如 LAC）才重写它。
4. **`forward` 是抽象方法**：没有默认实现，这是「你必须告诉我这个算法在量化态干什么」的强制点。

参数存取与可学习参数接口：

[amct_pytorch/algorithms/quant/base.py:35-47](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/base.py#L35-L47) —— `trainable_params`、`export_ptq_params`、`load_ptq_params`。

```python
    def trainable_params(self):
        return list(self.parameters())          # 默认：所有 Parameter 都可训练

    def export_ptq_params(self):
        return {name: param.detach().cpu()      # 落盘：detach + 搬到 CPU
                for name, param in self.named_parameters()}

    def load_ptq_params(self, params):
        named_params = dict(self.named_parameters())
        for name, value in params.items():
            if name not in named_params:        # 多出来的键静默忽略
                continue
            param = named_params[name]
            param.data.copy_(value.to(device=param.device, dtype=param.dtype))
```

三个细节：

- `trainable_params()` 默认返回**全部** `parameters()`。基类实现是「宽口径」，具体算法/包装类常会重写以精确点名（u4-l3 讲过求解器靠它决定解冻谁）。
- `export_ptq_params()` 只导出 `named_parameters()`，**不含 buffer**。注意：默认实现不含 buffer 是个有意的设计——但 LAC 需要存 `maxval`/`minval` 这两个 buffer，所以 LAC 重写了 `export_ptq_params`（见 4.3.3）。
- `load_ptq_params` 对未知键 `continue` 跳过，这让存档格式向后兼容（新增字段不会让旧 `.pt` 崩溃）。

#### 4.1.4 代码实践

**实践目标**：用最小代码验证基类的三条契约——`forward` 必须实现、`calib_forward` 默认原样返回同一个对象、参数能导出又读回。

**操作步骤**（源码阅读 + 本地可选运行）：

1. 阅读单测 [tests/unit_test/algorithms/test_quant_algorithm_base.py:106-113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_quant_algorithm_base.py#L106-L113)。它定义了一个不实现 `forward` 的 `_IncompleteAlgorithm`，断言实例化抛 `TypeError(match="abstract")`。这验证了 `@abstractmethod` 的强制力。
2. 阅读同文件 [tests/unit_test/algorithms/test_quant_algorithm_base.py:76-85](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_quant_algorithm_base.py#L76-L85)。关键断言是 `assert output is x`——注意是 `is`（同一对象），不是 `equal`（值相等）。它证明默认 `calib_forward` 返回的就是传入的张量本身，连拷贝都没做。
3. 阅读同文件 [tests/unit_test/algorithms/test_quant_algorithm_base.py:88-103](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_quant_algorithm_base.py#L88-L103)。它把 `source.weight` 改成 2.5，导出后喂给一个 `float64` 的 `target`，并故意混入一个未知键 `"unknown"`，最终断言 `target.weight.item() == 2.5`。这验证了「导出 detach+cpu、读回按目标 dtype 转换、未知键忽略」三件事。

**需要观察的现象 / 预期结果**：上述三个测试是 `tests/unit_test/algorithms/` 下真实存在的用例，可本地用 `pytest tests/unit_test/algorithms/test_quant_algorithm_base.py -m cpu` 运行（若无 NPU 标记需求）。若运行通过，即证明基类契约与上述描述一致；无法本地运行时，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `QuantAlgorithmBase` 要同时继承 `nn.Module` 和 `ABC`，而不是只用其中一个？

**参考答案**：`nn.Module` 提供参数管理（`parameters()`/`named_parameters()`/`state_dict`）和 `.to(device)` 等基础设施，让算法能被优化器和存档机制复用；但它不强制子类实现 `forward`。`ABC` + `@abstractmethod` 补上这道强制，保证任何具体算法都必须告诉框架「量化态下怎么处理张量」。两者职责互补。

**练习 2**：默认 `export_ptq_params` 只导出 `named_parameters()` 不导出 buffer。如果一个算法在校准阶段把统计量存进了 buffer，且这些统计量在部署时还需要，会出现什么问题？该怎么办？

**参考答案**：默认实现会让这些 buffer 在 `.pt` 里丢失，`load_ptq_params` 读不回来，断点续跑或 deploy 时统计量被清零。解决办法是像 LAC 那样重写 `export_ptq_params` / `load_ptq_params`，把 buffer 显式纳入存取（见 4.3.3）。

---

### 4.2 is_observe 通路控制：一个算法模块的「双面人格」

#### 4.2.1 概念说明

`is_observe` 是一个布尔标志，存在于三处：每个算法对象上（基类 `__init__` 设 `False`）、`ActivationQuantizer` 上、`WeightQuantizer` 上。它的作用是让**同一个挂载好的量化模块**在两种人格之间切换：

- **校准态（observe，`is_observe=True`）**：算法和量化器都变成「透明的统计员」。激活原样穿过（不被截断、不被量化），但算法可以在穿过时偷偷记录统计量（如 LAC 记录 min/max）。
- **量化态（quantize，`is_observe=False`）**：算法和量化器各司其职，算法施加截断/变换、量化器施加伪量化，输出就是真正「被量化过」的结果。

为什么不能搞成两个独立的模块？因为算法的可学习参数、统计 buffer、权重都只有一份，必须共享。`is_observe` 让「统计」和「量化」共用同一份状态和同一段前向代码，只通过一个标志分叉——这是复用而非复制。

「校准 / 训练 / 推理」三态的对应关系：

| 阶段 | `is_observe` | 算法做什么 | 量化器（dtype）做什么 |
| --- | --- | --- | --- |
| 校准（calibration） | `True` | `calib_forward`：记统计量，激活原样返回 | `fake_quant`：原样返回，不量化 |
| 训练（training，PTQ 重建） | `False` | `forward`：施加截断/变换 | `fake_quant`：做伪量化 |
| 推理（inference / eval 测 PPL） | `False` | `forward`：施加截断/变换 | `fake_quant`：做伪量化 |

注意训练态与推理态在模块层面**走同一条通路**（都是 `is_observe=False`），区别只在外层：训练态外面套着 solver 在反向传播、推理态外面是 `torch.no_grad()` 测精度。

#### 4.2.2 核心流程

以 `ActivationQuantizer` 为例，一次 `forward(x)` 的分叉逻辑（伪代码）：

```text
ActivationQuantizer.forward(x):
    for algo in self.algorithms:            # 串行跑所有 activation 算法
        if self.is_observe:
            x = algo.calib_forward(x)       # 通路 A：统计，返回 x 本身
        else:
            x = algo(x)                     # 通路 B：algo.forward(x)，施加变换
    return self.fake_quant(x)

ActivationQuantizer.fake_quant(x):
    if self.is_observe:
        return x                            # 通路 A：不量化，激活原样出
    return self.quant_obj(x)                # 通路 B：用 dtype 量化器伪量化
```

要点：在 observe 态，两层（算法层 + 量化层）都「放行」，激活完全不被改动；但夹在两层之间的 `algo.calib_forward` 仍可产生副作用（更新 buffer）。这就是「透明管道 + 偷偷记录」的实现。

#### 4.2.3 源码精读

`ActivationQuantizer` 是 `is_observe` 最典型的消费方：

[amct_pytorch/quantization/modules/quant_base.py:83-106](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L83-L106) —— `__init__` 里 `self.is_observe = False`，`fake_quant` 与 `forward` 的双通路分叉。

```python
class ActivationQuantizer(torch.nn.Module):
    def __init__(self, args, bits):
        ...
        self.quant_obj = DTYPE_REGISTRY.get(args.quant_dtype)(bits=self.bits, is_act=True)
        self.is_observe = False                     # 自己也持有一份标志

    def fake_quant(self, x):
        if self.is_observe:
            return x                                # 校准态：不量化
        return self.quant_obj(x)                    # 量化态：伪量化

    def forward(self, x):
        for algo in self.algorithms.values():
            x = algo.calib_forward(x) if self.is_observe else algo(x)   # ← 关键分叉
        return self.fake_quant(x)
```

第 105 行那一行三元表达式就是通路开关的本体：`is_observe=True` 走 `algo.calib_forward(x)`，`False` 走 `algo(x)`（即 `algo.__call__` → `algo.forward`）。

权重量化器 `WeightQuantizer` 也是同样的设计，只是因为某些权重算法带自定义 `quantize()` 钩子而稍复杂：

[amct_pytorch/quantization/modules/quant_base.py:132-147](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L132-L147) —— `algo_forward` 中 `is_observe` 同样决定走 `calib_forward` 还是真实算法。

```python
    def algo_forward(self, x):
        quantize_algo = None
        for algo in self.algorithms.values():
            if self.is_observe:
                x = algo.calib_forward(x)           # 校准态：同样放行 + 统计
                continue
            quantize_fn = getattr(algo, "quantize", None)
            if callable(quantize_fn):
                ...                                 # 带 quantize() 钩子的特殊算法
                quantize_algo = algo
                continue
            x = algo(x)                             # 普通权重算法：施加变换
        return x, quantize_algo
```

注意 `is_observe` 的「一标志多处持有」：算法自身一份、`ActivationQuantizer` 一份、`WeightQuantizer` 一份。它们必须**同时翻转**，否则会出现「算法在校准态但量化器在量化态」的错乱。这正是 4.3 的 `set_model_to_observe` 要解决的——它一次性把整棵子树上的标志全翻过来。

#### 4.2.4 代码实践

**实践目标**：直接验证 `ActivationQuantizer.forward` 在两种 `is_observe` 下的输出差异——校准态激活原样穿过、量化态被伪量化。

**操作步骤**（源码阅读型，可选本地运行）：

1. 阅读 [tests/unit_test/quantization/modules/test_quant_base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/quantization/modules/test_quant_base.py) 中关于 `ActivationQuantizer` 的用例，重点看有没有分别构造 `is_observe=True` 和 `False`、喂同一个 `x`、断言输出是否 `equal`（甚至 `is`）的测试。
2. 自己手写一段最小脚本（**示例代码**，非项目原有）：

   ```python
   # 示例代码：演示 is_observe 两态差异（仅供理解，非仓库文件）
   import torch
   from types import SimpleNamespace
   from amct_pytorch.quantization.modules.quant_base import ActivationQuantizer

   args = SimpleNamespace(algos=["lac"], is_per_tensor=True,
                          quant_dtype="int", w_bits=8, bit_policy=None)
   # 注：实际 bit_policy 需由 ensure_bit_policy 补齐，此处仅示意通路
   aq = ActivationQuantizer(args, bits=8)
   x = torch.randn(2, 4)

   aq.is_observe = True
   print("observe:", (aq(x) is x))   # 预期 True（无 lac 统计副作用时原样返回）

   aq.is_observe = False
   print("quantize out == x:", torch.equal(aq(x), x))  # 预期 False（被伪量化）
   ```

**需要观察的现象 / 预期结果**：`is_observe=True` 时 `aq(x)` 应等于（甚至就是）原 `x`；`is_observe=False` 时输出与 `x` 不同（被 `quant_obj` 伪量化）。真实 `bit_policy` 初始化细节较繁，**待本地验证**——核心是确认第 105 行三元分叉确实让两态输出不同。

#### 4.2.5 小练习与答案

**练习 1**：假如只把 `ActivationQuantizer.is_observe` 翻成 `True`，却忘了翻它内部 LAC 算法的 `is_observe`，会怎样？

**参考答案**：`ActivationQuantizer.forward` 第 105 行看的是**自己**的 `self.is_observe`。若它为 `True`，会调 `algo.calib_forward(x)`——注意它调的是 `calib_forward` 而非 `algo.forward`，所以即便 algo 自己的 `is_observe` 标志没翻，通路 A 依然生效。也就是说，分叉由「量化器的标志」决定，算法自身的标志在此处并不参与决策。但 `set_model_to_observe` 仍然会把两者的标志一起翻齐，以保持状态一致、避免别处依赖算法自身标志的逻辑出错。

**练习 2**：为什么训练态和推理态在模块层面走同一条通路（都 `is_observe=False`）？它们到底区别在哪？

**参考答案**：对量化模块而言，「量化」这件事是确定的——算法参数已固定，前向输出一样。区别在外层上下文：训练态外层是 solver，开启 autograd、用重建 MSE 反向更新 `trainable_params()`；推理态外层是 `torch.no_grad()`，只测精度不更新。模块复用同一条通路正是为了「训练时量化 == 推理时量化」，避免训练与推理不一致。

---

### 4.3 set_model_to_observe 与 calib_forward：在 workflow 里如何切换

#### 4.3.1 概念说明

前面两节分别讲了「算法该实现什么」和「标志怎么分叉通路」。本节把它们装回 PTQ 主流程，回答开头那个问题：**生成 GT 时，怎么保证挂在模块上的算法不污染激活？**

答案是一个固定的「三明治」结构：

```text
set_model_to_observe(unit.module, True)      # ① 把整棵子树翻成校准态
try:
    gts = materialize_gt(inps, unit.module)  # ② 跑「浮点」前向，激活全透明 → 干净 GT
finally:
    set_model_to_observe(unit.module, False) # ③ 务必翻回量化态，供后续 solver 训练
```

`set_model_to_observe(model, flag)` 遍历 `model.modules()`，凡是有 `is_observe` 属性的子模块一律赋值。这一刀切下去，命中三类对象：所有算法对象、所有 `ActivationQuantizer`、所有 `WeightQuantizer`——正好是 4.2 提到的「一标志多处持有」的全部宿主。所以一次调用就能保证整棵子树状态齐整。

`materialize_gt` 此时跑的是 `unit.module`（已挂载量化包装的原始浮点子模块）。因为 observe 态下所有量化通路都透明，激活不被截断也不被伪量化，输出就是真正的浮点参考值——这就是重建目标 GT。生成完 GT，`finally` 把标志翻回 `False`，之后 solver 训练时模块就恢复成真实量化态，重建损失才能度量「量化输出 vs 浮点 GT」的差距。

`try/finally` 不是装饰：一旦 GT 生成抛异常，也必须把标志翻回，否则后续单元会误在校准态训练。

#### 4.3.2 核心流程

把 4.3.1 的三明治放回 u4-l2 的 PTQ 单元循环里，一个 `PtqUnit` 的完整处理是：

```text
_prepare_unit_batch(unit):
    inps, kwargs = load_unit_inputs(unit)          # 读回 extract 阶段录的激活
    unit.module.float().to(device)
    set_model_to_observe(unit.module, True)        # ① 校准态
    try:
        gts = materialize_gt(inps, unit.module, kwargs)   # ② 产 GT（激活透明）
    finally:
        set_model_to_observe(unit.module, False)   # ③ 回量化态
    return build_unit_batch(unit, inps, kwargs, gts)      # 包成 DataLoader

# 之后 solver.solve(...) 在 is_observe=False 下做重建训练
```

而 `materialize_gt` 内部就是朴素的前向循环（无量化介入，因为上层已 observe）：

```text
materialize_gt(inps, ori_module, kwargs):
    ori_module.float().eval()
    for x in DataLoader(inps, batch_size=cali_bsz):
        gt = ori_module(x, **kwargs)       # observe 态：算法/量化器全透明
        gts.append(gt.detach())
    return torch.cat(gts)
```

#### 4.3.3 源码精读

`set_model_to_observe` 的实现极其简洁：

[amct_pytorch/common/models/llm/common/quant_apply.py:47-50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L47-L50)

```python
def set_model_to_observe(model, flag):
    for mod in model.modules():
        if hasattr(mod, "is_observe"):
            mod.is_observe = flag
```

`model.modules()` 是 PyTorch 的递归遍历（包含自身及所有子孙模块）。`hasattr` 过滤保证只改「有这个标志」的模块——普通 `nn.Linear`、layernorm 等不受影响。一句话就完成了全子树批量翻转。

PTQ workflow 里的三明治调用（u4-l2 讲过 `_prepare_unit_batch`，这里聚焦 observe 包夹）：

[amct_pytorch/workflows/llm_ptq.py:165-170](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L165-L170)

```python
        set_model_to_observe(unit.module, True)
        try:
            gts = self.data_provider.materialize_gt(inps, unit.module, kwargs=kwargs)
        finally:
            set_model_to_observe(unit.module, False)
        return self.data_provider.build_unit_batch(unit, inps, kwargs, gts)
```

对应的 `materialize_gt`：

[amct_pytorch/common/datasets/ptq_provider.py:73-91](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L73-L91)

```python
    def materialize_gt(self, inps, ori_module, kwargs=None):
        ori_module.float().eval().to(self.device)
        ...
        with torch.no_grad():
            for (x,) in ori_loader:
                ...
                gt = ori_module(x, **forward_kwargs)   # observe 态下，这是干净浮点输出
                ...
                gts.append(gt.detach())
        ...
        return torch.cat(gts, dim=0)
```

注意 `ori_module` 就是挂了量化包装的 `unit.module`。若没有第 165 行的 observe 翻转，这里 `ori_module(x)` 会走量化通路，GT 就成了「量化输出」，重建目标失效。observe 态让这层包装「隐身」，GT 才是真正的浮点参考。

现在看本讲的主角算法 LAC，它如何同时实现两条通路。

LAC 的校准态——记录激活的 min/max，激活本身原样返回：

[amct_pytorch/algorithms/quant/auto_clip.py:132-137](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L132-L137)

```python
    def calib_forward(self, x, *args, **kwargs):
        if x.max() > self.maxval.to(x.device):
            self.maxval.data = x.max()        # 副作用：更新统计 buffer
        if x.min() < self.minval.to(x.device):
            self.minval.data = x.min()
        return x                              # 激活原样返回（不截断）
```

LAC 的量化态——用学到的 `clip_factor` 与统计到的 min/max 做可学习截断：

[amct_pytorch/algorithms/quant/auto_clip.py:112-130](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L112-L130)

```python
    def apply_clip(self, x):
        if self.is_per_tensor:
            cur_max = self.maxval.clone()          # 用 calib_forward 统计到的边界
            cur_min = self.minval.clone()
        else:
            ...
            cur_max, cur_min = x.amax(1, keepdim=True), x.amin(1, keepdim=True)  # per-token 现算
        cur_max *= self.sigmoid(self.clip_factor_max.to(x.device))   # 学到的缩放
        cur_min *= self.sigmoid(self.clip_factor_min.to(x.device))
        x = torch.clamp(x, min=cur_min, max=cur_max)
        return x.reshape(init_shape)

    def forward(self, x):
        return self.apply_clip(x)
```

`apply_clip` 的数学含义。设当前边界为 \(m_{\max}\)、可学习因子为 \(\alpha_{\max}\)，则实际截断上界为：

\[
b_{\max} = m_{\max}\cdot\sigma(\alpha_{\max}),\qquad \sigma(\alpha)=\frac{1}{1+e^{-\alpha}}
\]

截断操作：

\[
x_{\text{clip}} = \mathrm{clip}\!\left(x,\ b_{\min},\ b_{\max}\right)
\]

`clip_factor` 初始化为 4.0，而 \(\sigma(4.0)\approx 0.982\)，故训练初期几乎不截断（保留全量程）；训练中 solver 可把 \(\alpha\) 推向 0（\(\sigma=0.5\)，截到一半）甚至更小，从而压掉 outlier、减小量化误差。这正是「可学习截断」的直觉。

因为 LAC 把统计量存进了 buffer，它必须重写存取接口（4.1 练习 2 的答案就在这里）：

[amct_pytorch/algorithms/quant/auto_clip.py:86-110](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L86-L110)

```python
    def export_ptq_params(self):
        return {
            "clip_factor_min": self.clip_factor_min.detach().cpu(),
            "clip_factor_max": self.clip_factor_max.detach().cpu(),
            "maxval": self.maxval.detach().cpu(),   # buffer 也要存
            "minval": self.minval.detach().cpu(),
        }

    def load_ptq_params(self, params):
        self.clip_factor_min.data.copy_(...)   # 连同 buffer 一起读回
        ...
        self.maxval.copy_(...)
        self.minval.copy_(...)
```

对比 LWC（权重可学习截断，[auto_clip.py:29-56](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py#L29-L56)）：它**没有**重写 `calib_forward`（用基类默认的原样返回），也**没有** `maxval/minval` buffer——因为 `apply_clip` 直接从权重张量 `x` 现算 `x.min(1)`/`x.max(1)`，不需要预先校准统计。这是「activation 算法才需要 observe 统计、weight 算法可直接从张量算边界」的典型对照。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：以 LAC 算法为例，对照 `calib_forward` 与 `forward`，说清校准阶段做了什么（更新 maxval/minval）、量化阶段做了什么（apply_clip），并解释 `is_observe` 如何在 `ActivationQuantizer` 中决定走哪条路径。

**操作步骤**：

1. **读校准态**：看 [tests/unit_test/algorithms/test_auto_clip.py:103-118](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_auto_clip.py#L103-L118) `test_lac_calib_forward_updates_min_max_buffers_and_returns_input`。它依次喂 `x1=[[-3,5]]`、`x2=[[-1,7]]`，断言三件事：
   - `out1 is x1`、`out2 is x2`（返回原对象，激活未被修改）；
   - `torch.equal(x1, snapshot1)`（输入张量内容也未变）；
   - `lac.maxval.item() == 7.0`、`lac.minval.item() == -3.0`（buffer 被更新成所见过的全局极值）。

   这正好刻画「校准态 = 透明管道 + 偷偷记极值」。

2. **读量化态**：看 [tests/unit_test/algorithms/test_auto_clip.py:121-131](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_auto_clip.py#L121-L131) `test_lac_clip_per_tensor_uses_observed_buffers`。它手动把 `maxval/minval` 设为 ±2.0、`clip_factor` 清零（\(\sigma(0)=0.5\)，故边界收窄到 ±1.0），再喂 `[-3, 0.5, 1.5, 3.0]`，断言输出 `[-1, 0.5, 1, 1]`——即用校准阶段统计的 buffer 做了真实截断。这就是「量化态 = apply_clip」。

3. **读通路选择**：对照 [quant_base.py:103-106](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L103-L106) 的 `ActivationQuantizer.forward`，确认：当 `is_observe=True` 时调 `algo.calib_forward(x)`（步骤 1 的行为），`is_observe=False` 时调 `algo(x)` 即 `forward`（步骤 2 的行为）。

**需要观察的现象 / 预期结果**：
- 校准阶段（observe）：LAC 不改激活，但 `maxval`/`minval` 持续累积到所见过数据的全局 max/min。
- 量化阶段（quantize）：LAC 用累积的 `maxval`/`minval` × \(\sigma\)(`clip_factor`) 做截断；solver 训练 `clip_factor` 使重建损失下降。
- 通路决策点：`ActivationQuantizer.forward` 第 105 行三元表达式，由量化器的 `is_observe` 一锤定音。
- 以上两个测试可本地运行 `pytest tests/unit_test/algorithms/test_auto_clip.py -m cpu` 验证；无法运行时**待本地验证**。

**一句话结论（答案模板）**：LAC 在 `calib_forward` 里只更新 `maxval`/`minval` 两个 buffer、激活原样返回；在 `forward`/`apply_clip` 里用这两个 buffer 配合可学习的 `clip_factor` 做 `torch.clamp` 截断。`ActivationQuantizer.forward` 凭 `self.is_observe` 在两者间二选一——`True` 走 `calib_forward`（校准/统计），`False` 走 `algo(x)`（量化/训练）。PTQ workflow 在生成 GT 前用 `set_model_to_observe(True)` 把整棵子树翻成校准态，生成完用 `finally` 翻回，从而保证 GT 是干净的浮点参考。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `set_model_to_observe(unit.module, True)` 这一行删掉（即 GT 生成时仍处于量化态），后续重建训练会发生什么？

**参考答案**：`materialize_gt` 跑 `unit.module(x)` 会走量化通路——激活被 LAC 截断、被 dtype 量化器伪量化，于是 GT 变成「量化后的输出」而非浮点参考。接着 solver 在量化态下让量化输出逼近这个「已经是量化结果」的 GT，重建损失几乎恒为 0、学不到任何有用的 `clip_factor`，PTQ 退化。observe 三明治是重建有意义的必要前提。

**练习 2**：LAC 的 `calib_forward` 写的是 `if x.max() > self.maxval` 才更新。为什么用「严格大于」而不是直接 `self.maxval = max(self.maxval, x.max())`？两者有实质差别吗？

**参考答案**：语义上都是在「保留所见过数据的全局最大值」。严格 `>` 写法在相等时不触发赋值，省一次无意义的写操作；功能上与 `max` 等价。注意它统计的是**整个校准集的全局极值**（per-tensor 粒度），而不是每 batch 的极值——因为 `maxval` 是跨 batch 累积的 buffer。per-token 模式（`is_per_tensor=False`）则不依赖这两个 buffer，每 batch 现算。

**练习 3**：为什么 LAC 同时重写了 `export_ptq_params`/`load_ptq_params`，而 LWC 没有重写（沿用基类）？

**参考答案**：LAC 的部署需要 `maxval`/`minval` 两个 buffer（per-tensor 截断边界），而基类默认只存 `named_parameters()` 不存 buffer，所以必须重写把 buffer 一并存取。LWC 的 `apply_clip` 直接从权重张量现算 `x.min/x.max`，没有需要持久化的统计 buffer，沿用基类的「存所有 Parameter」即可，故无需重写。

---

## 5. 综合实践

**任务**：把本讲三个最小模块（基类契约、is_observe 双通路、observe 三明治）串起来，画出 LAC 算法从「注册 → 挂载 → 校准 → 训练 → 存档 → deploy 读回」的完整状态流，并标注每一步 `is_observe` 的值与调用的基类方法。

**要求产出一张表或一幅流程图**，至少覆盖以下节点（每行写明：阶段、`is_observe` 取值、调用 LAC 的哪个方法、LAC 对激活/权重做了什么、是否更新 buffer/Parameter）：

1. 构建期：`build_algorithms_by_target` 把 LAC 实例化进 `ActivationQuantizer.algorithms`。
2. 校准期（GT 生成）：workflow 调 `set_model_to_observe(True)` → `materialize_gt` 触发 `ActivationQuantizer.forward` → 走 `LAC.calib_forward`。
3. 训练期：`set_model_to_observe(False)` → solver 调 `ActivationQuantizer.forward` → 走 `LAC.forward/apply_clip`，solver 反向更新 `clip_factor_min/max`。
4. 存档期：solver.finalize → `LAC.export_ptq_params`（含 buffer）→ 写 `.pt`。
5. 复用/deploy 期：`LAC.load_ptq_params` 读回（断点续跑或 deploy 烘焙）。

**进阶思考**：在第 4 步，如果忘了重写 `export_ptq_params`（沿用基类），第 5 步 `load_ptq_params` 后 LAC 还能正确工作吗？为什么？（提示：回顾 4.1.5 练习 2 与 4.3.5 练习 3。）

**预期结果**：得到一张能解释「同一个 LAC 对象为何能既当统计员又当量化器」的状态表。本实践不要求运行命令，重点是理清通路切换与接口调用的对应关系。

## 6. 本讲小结

- `QuantAlgorithmBase`（`nn.Module, ABC`）是所有量化算法的插座标准：`calib_forward`（默认原样返回）、`forward`（抽象，必实现）、`trainable_params`、`export_ptq_params`、`load_ptq_params` 五个接口让主流程能不认识具体算法地驱动它。
- `is_observe` 是一个布尔开关，让同一个挂载模块在**校准态**（透明 + 统计）与**量化态**（截断 + 伪量化）之间切换；`ActivationQuantizer.forward` 第 105 行的三元表达式 `calib_forward if is_observe else algo(x)` 是通路分叉的本体。
- 「一标志多处持有」（算法、`ActivationQuantizer`、`WeightQuantizer` 各一份）必须齐整翻转，`set_model_to_observe` 用一次 `modules()` 遍历完成这件事。
- PTQ workflow 在 `_prepare_unit_batch` 里用 `set_model_to_observe(True) → materialize_gt → finally set_model_to_observe(False)` 的三明治结构，保证生成 GT 时模块透明、GT 是干净浮点参考；`try/finally` 保证异常时也翻回。
- LAC 是典型示范：`calib_forward` 更新 `maxval/minval` buffer、激活原样返回；`forward/apply_clip` 用 buffer × \(\sigma\)(`clip_factor`) 做可学习截断；因含 buffer 而重写存取接口。对照 LWC（weight 算法，不重写、不需校准）可看清 activation 算法为何依赖 observe。
- 三态对应：校准 `is_observe=True`；训练与推理都 `is_observe=False`，区别只在外层（solver 反向 vs `no_grad` 测精度）。

## 7. 下一步学习建议

本讲只讲了「算法基类与通路开关」这一公共机制。接下来：

- **u6-l2 算法注册与 target 路由机制**：搞清 LAC 为何声明 `targets=("activation",)`、LWC 为何是 `("weight",)`，以及 `build_algorithms_by_target` 如何把算法挂到 `ActivationQuantizer` 还是 `WeightQuantizer`（即本讲里 `self.algorithms` 是怎么填进去的）。
- **u6-l3 AWQ 实现**、**u6-l4 可学习算法族**：看具体算法的 `forward` 数学细节（AWQ 网格搜索、FlatQuant 的 Kronecker 结构变换），它们都建立在本讲的基类契约与 `is_observe` 通路之上。
- 建议同步阅读 [amct_pytorch/algorithms/quant/omniquant.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/omniquant.py) 与 [amct_pytorch/algorithms/quant/flatquant.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py)，对照它们各自如何重写 `calib_forward`（OmniQuant 也含校准统计），巩固本讲的通路模型。
