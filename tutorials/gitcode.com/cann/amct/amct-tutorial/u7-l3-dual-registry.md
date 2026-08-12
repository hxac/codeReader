# 双注册表体系：PTQ 注册表 vs Classic 经典注册表

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 classic 经典流程的 `AlgorithmRegistry` 是如何用**二维复合键 `(算法名, 源算子类型)`** 同时登记「伪量化模块」和「NPU 部署模块」的；
- 解释 `quantize_op`（训练态伪量化）与 `deploy_op`（部署态 NPU 算子）为何是**成对**关系，以及一个伪量化模块为何可能对应多个部署模块；
- 把 classic 流（`AlgorithmRegistry` + classic 模块 + 图遍历两遍 pass）与 LLM PTQ 流（`ALGO_REGISTRY` + `quantization` 模块 + 块级重建）在**主键设计、部署归属、入口命令**三个维度上做出清晰对比，知道面对一个任务该走哪一条路。

## 2. 前置知识

本讲是 u7 单元的第 3 篇，默认你已掌握以下认知（来自前置讲义，这里只做最小回顾）：

- **两套并行的量化体系**（u1-l3）：AMCT 同时维护 classic 经典图压缩主线与 LLM PTQ 主线，二者不是新旧替代，而是并存。
- **通用 `Registry` 基类**（u3-l3）：`common/utils/registry_factory.py` 提供了一个带校验的全局字典，存 `RegistryItem(name, target, metadata)`，四大注册表（模型/求解器/数据类型/算法）都基于它。
- **LLM PTQ 的 `ALGO_REGISTRY`**（u6-l2）：算法用 `@ALGO_REGISTRY.register(targets=(...))` 装饰器靠 import 副作用登记，`targets` 取值 `weight`/`activation`/`structure` 决定挂载点，部署走各模块自己的 `export_deploy`。
- **伪量化 vs 真量化**（u7-l1/u7-l2）：训练/校准态产出**浮点伪量化张量**（fake quant），部署态产出**真低比特 payload**。

本讲的关键直觉是：**classic 体系把「算法」和「算子替换」绑死在注册表里，是一张「谁替换谁」的查找表；LLM PTQ 体系只把「算法」登记为可插拔插件，部署是独立的 workflow 阶段，不在注册表里。** 这是一个根本性的架构分野。

> 名词速查：`quant_op`（伪量化模块，fake-quant，继承 `BaseQuantizeModule`）；`deploy_op`（NPU 部署模块，跑真低比特算子）；`src_op`（被量化的源算子类型，如 `Linear`/`Conv2d`）；pass（图遍历改写 pass）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `amct_pytorch/algorithms/register_algo.py` | classic `Algorithm` 类：用三张 dict 维护 (算法, 源算子)→伪量化模块、伪量化→部署模块的映射 |
| `amct_pytorch/algorithms/__init__.py` | 实例化 `AlgorithmRegistry` 并批量 `.register(...)` 登记所有内置 classic 算法 |
| `amct_pytorch/algorithms/registry_factory.py` | LLM PTQ 的 `ALGO_REGISTRY`（通用 `Registry` 基类的特化子类 `QuantAlgorithmRegistry`） |
| `amct_pytorch/common/utils/registry_factory.py` | 通用 `Registry` 基类与 `RegistryItem`（LLM PTQ 四大注册表的地基） |
| `amct_pytorch/classic/quantize_op/linear_awq_module.py` | classic 伪量化模块示例 `LinearAWQuant`（awq，训练态 fake-quant） |
| `amct_pytorch/classic/deploy_op/npu_quantization_linear.py` | classic 部署模块 `NpuQuantizationLinear`（W+A，跑 `npu_quant_matmul`） |
| `amct_pytorch/classic/deploy_op/weight_npu_quant_module.py` | classic 部署模块 `NpuWeightQuantizedLinear`（W-only，跑 `npu_weight_quant_batchmatmul`） |
| `amct_pytorch/quantize_op/base_quant_module.py` | classic 伪量化模块的基类 `BaseQuantizeModule`（注意：位于顶层 `quantize_op/` 而非 `classic/quantize_op/`） |
| `amct_pytorch/classic/quantize.py` | classic 流的两个入口函数 `quantize()` / `convert()`，编排两遍 pass |
| `amct_pytorch/classic/optimizer/insert_quantize_op_pass.py` | 第一遍 pass：按 (算法, 源算子) 插入伪量化模块 |
| `amct_pytorch/classic/optimizer/replace_npu_quant_pass.py` | 第二遍 pass：按伪量化模块类型替换为 NPU 部署模块 |
| `amct_pytorch/quantization/modules/quant_base.py` | LLM PTQ 的反向路由 `build_algorithms_by_target`（对照用） |

## 4. 核心概念与源码讲解

### 4.1 classic AlgorithmRegistry 注册表

#### 4.1.1 概念说明

classic 体系的注册表叫 `AlgorithmRegistry`，但它**不是**前置讲义里那个通用 `Registry` 基类的实例，而是一个独立手写的 `Algorithm` 类。它的任务是回答两个问题：

1. **插入阶段**：「算法 `awq` 作用在 `Linear` 上，应该把原 `nn.Linear` 替换成哪个伪量化模块？」
2. **替换阶段**：「这个伪量化模块，应该被替换成哪个 NPU 部署模块？」

为此它维护三张表（两张有用、一张声明但未在注册中写入）：

- `algo`：`{算法名: {源算子类型字符串: 伪量化模块类}}` —— 二维复合键，服务插入阶段；
- `quant_to_deploy`：`{伪量化模块类: [部署模块类, ...]}` —— 以伪量化模块**类对象本身**为键，服务替换阶段；
- `quant_op`：一个列表，声明了但在 `register()` 里没有写入（历史遗留，可忽略）。

#### 4.1.2 核心流程

注册一条算法就是一次性把「算法名、源算子类型、伪量化模块、部署模块」四元组写进两张表：

```text
register(name, src_op, quant_op, deploy_op):
    algo[name][src_op] = quant_op                 # 第一张表：二维键 → 伪量化模块
    quant_to_deploy[quant_op] += deploy_op 去重    # 第二张表：伪量化模块 → [部署模块]
```

注意 `quant_to_deploy` 的键是 `quant_op`（类对象），值是**列表**——因为同一个伪量化模块可能对应多个部署模块（详见 4.2）。

#### 4.1.3 源码精读

`Algorithm` 类定义在 [amct_pytorch/algorithms/register_algo.py:22-44](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/register_algo.py#L22-L44)：

```python
class Algorithm:
    def __init__(self):
        self.algo = dict()
        self.quant_to_deploy = dict()
        self.quant_op = []

    def register(self, name, src_op, quant_op, deploy_op):
        if self.algo.get(name) is None:
            self.algo[name] = {}
        self.algo[name][src_op] = quant_op
        ...
        self.quant_to_deploy[quant_op] = list(set(...))   # set 去重
```

实例化和批量登记发生在 [amct_pytorch/algorithms/__init__.py:59-100](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L59-L100)。`AlgorithmRegistry = Algorithm()` 创建单例，随后是一串 `.register(...)` 调用：

```python
AlgorithmRegistry.register('gptq', 'Linear', GPTQuant, NpuWeightQuantizedLinear)
AlgorithmRegistry.register('awq',  'Linear', LinearAWQuant, NpuWeightQuantizedLinear)
AlgorithmRegistry.register('smoothquant', 'Linear', SmoothQuant, NpuQuantizationLinear)
AlgorithmRegistry.register('minmax', 'Linear', MinMaxQuant,
                            [NpuWeightQuantizedLinear, NpuQuantizationLinear])
```

几个值得注意的设计点：

- **同一算法名可作用在多种源算子上**：第二维 `src_op` 让 `ofmr` 同时登记 `'Linear'`（[L80-82](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L80-L82)）和 `'Conv2d'`（[L83](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L83)），分别配不同的部署模块。这正是「二维复合键」带来的表达力。
- **部署模块可以是列表**：`minmax` 给的是 `[NpuWeightQuantizedLinear, NpuQuantizationLinear]`，因为它既能做 W-only 也能做 W+A，运行时再按属性二选一（见 4.2）。
- **存在一个边界写法**：[L91](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L91) `register('cast', None, 'FP8Linear', NpuHIF8Linear)` 把 `src_op=None`、`quant_op` 填成字符串 `'FP8Linear'`（而非类），与其它条目风格不同，是一条特殊路径（其精确消费场景待确认，不展开）。

`BUILT_IN_ALGORITHM` 列表（[L60-69](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L60-L69)）只是内置算法名的清单，供配置校验等场景枚举，与注册表写入无关。

#### 4.1.4 代码实践

实践目标：亲手跑通 `AlgorithmRegistry` 的两张表，验证「二维键」与「quant→deploy 去重」行为。

操作步骤（**示例代码**，可在 `python -c` 或 REPL 执行，无需 NPU）：

```python
# 示例代码：复刻 Algorithm 的最小骨架，观察两张表的结构
class FakeAlgo:
    def __init__(self):
        self.algo = dict()
        self.quant_to_deploy = dict()
    def register(self, name, src_op, quant_op, deploy_op):
        self.algo.setdefault(name, {})[src_op] = quant_op
        bucket = self.quant_to_deploy.setdefault(quant_op, [])
        bucket += deploy_op if isinstance(deploy_op, list) else [deploy_op]
        self.quant_to_deploy[quant_op] = list(set(bucket))

class Q: pass      # 假装是 LinearAWQuant
class D1: pass     # 假装是 NpuWeightQuantizedLinear
class D2: pass     # 假装是 NpuQuantizationLinear

r = FakeAlgo()
r.register('awq', 'Linear', Q, D1)
r.register('minmax', 'Linear', Q, [D1, D2])
print(r.algo)                # {'awq': {'Linear': Q}, 'minmax': {'Linear': Q}}
print(r.quant_to_deploy)     # {Q: [D1, D2]}  ← 同一 quant_op 的部署模块被合并去重
```

需要观察的现象：

1. `algo` 的键是算法名，值是「源算子→伪量化模块」的二级字典；
2. `quant_to_deploy` 的键是伪量化模块**类**，值是去重后的部署模块**列表**；同一伪量化模块被多次注册时，部署模块列表会合并去重（这正是源码末尾 `list(set(...))` 的作用）。

预期结果：两张表的结构与上面注释一致。如果你直接 `import amct_pytorch.algorithms as A`，也可以打印 `A.AlgorithmRegistry.algo['awq']` 与 `A.AlgorithmRegistry.quant_to_deploy` 的真实键值来对照（待本地验证当前环境能否 import）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Algorithm` 类不直接复用通用 `Registry` 基类（`common/utils/registry_factory.Registry`）？

参考答案：通用 `Registry` 是「单键 → 单对象」的一维字典，而 classic 的核心需求是「(算法名, 源算子) 二维键 → 伪量化模块」外加「伪量化模块 → 多个部署模块」的反向映射，数据形状不匹配，所以单独写了一个 `Algorithm` 类维护两张表。

**练习 2**：`quant_to_deploy` 的键是 `quant_op`（类对象）而不是算法名字符串，这样做有什么好处？

参考答案：替换 pass（4.2）在遍历模型时拿到的是**已实例化的模块对象**，最自然的判定就是「这个对象的类型是不是某个伪量化模块类」，用类对象做键可以直接 `type(module) in quant_to_deploy`，无需反查它属于哪个算法名。

### 4.2 quantize_op ↔ deploy_op 成对映射

#### 4.2.1 概念说明

classic 体系的灵魂是**两阶段算子替换**：先在训练态插入「伪量化模块」做校准（算出 scale/offset），再在部署态替换成「NPU 部署模块」跑真低比特算子。两类模块成对出现，由注册表在登记时就绑定好关系：

- **伪量化模块（`quantize_op/`）**：继承 `BaseQuantizeModule`，`forward` 产出**浮点伪量化张量**，在校准数据上算并缓存 scale/offset。典型如 `LinearAWQuant`（awq）、`GPTQuant`（gptq）、`SmoothQuant`（smoothquant）、`MinMaxQuant`（minmax）。
- **NPU 部署模块（`deploy_op/`）**：`__init__` 吃一个伪量化模块，把它的参数「烘焙」成真低比特权重 + NPU 算子；`forward` 真正跑低比特推理。典型如 `NpuWeightQuantizedLinear`（W-only）、`NpuQuantizationLinear`（W+A）。

#### 4.2.2 核心流程

classic 流的入口是 `amct_pytorch/classic/quantize.py` 暴露的两个函数 `quantize()` 与 `convert()`，它们各跑一遍图遍历 pass：

```text
原始模型                    quantize()                     convert()
nn.Linear  ─── InsertQuantizeModulePass ──►  LinearAWQuant  ─── ReplaceNpuQuantModulePass ──►  NpuWeightQuantizedLinear
            按 (算法名, 源算子类型) 从            (伪量化模块)      按伪量化模块类型从               (NPU 部署模块)
            algo 表查 quant_op 替换                                quant_to_deploy 表查 deploy_op 替换
```

当 `quant_to_deploy[quant_op]` 是**列表**时（如 minmax 的 `[NpuWeightQuantizedLinear, NpuQuantizationLinear]`），替换 pass 会用 `_should_use_deploy_op` 按模块属性二选一：

- 有 `scale_d`（激活 scale）→ W+A → `NpuQuantizationLinear`；
- 无 `scale_d` → W-only → `NpuWeightQuantizedLinear`；
- `ori_module_type == 'Conv2d'` → `NpuQuantizationConv2d`；
- `dynamic is True` → `NpuQuantizationLinear`。

#### 4.2.3 源码精读

**第一遍 pass（插入伪量化）** 在 [amct_pytorch/classic/optimizer/insert_quantize_op_pass.py:43-66](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/insert_quantize_op_pass.py#L43-L66)，匹配逻辑就是查 `algo` 这张二维表：

```python
alg_name = alg_names[0]                                  # 每个模块只配一个算法
if AlgorithmRegistry.algo.get(alg_name):
    for ori_op in AlgorithmRegistry.algo.get(alg_name).keys():
        if type(module).__name__ == ori_op:              # 源算子类型名 == 第二维键
            self.quantize_ops[name] = AlgorithmRegistry.algo.get(alg_name).get(ori_op)
            return True
```

`do_pass` 随后用查到的伪量化模块类 `new_module = self.quantize_ops[name](object_module, object_name, layer_config)` 替换原模块（[L77-82](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/insert_quantize_op_pass.py#L77-L82)）。

**第二遍 pass（替换为 NPU 算子）** 在 [amct_pytorch/classic/optimizer/replace_npu_quant_pass.py:41-58](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/replace_npu_quant_pass.py#L41-L58)，匹配靠的是 `quant_to_deploy` 表的键（注意同时兼容类对象与类名两种键）：

```python
module_type = type(module)
if (module_type in AlgorithmRegistry.quant_to_deploy.keys()
        or module_type.__name__ in AlgorithmRegistry.quant_to_deploy.keys()):
    return True
```

列表二选一在 [_get_deploy_module / _should_use_deploy_op](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/replace_npu_quant_pass.py#L105-L139)。

**伪量化模块示例** `LinearAWQuant`（awq）继承 `BaseQuantizeModule`（基类契约见 [amct_pytorch/quantize_op/base_quant_module.py:21-37](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantize_op/base_quant_module.py#L21-L37)，约定了 `scale_w`/`scale_d`/`offset_w`/`offset_d`/`wts_type`/`act_type` 等属性槽位）。它的 [forward](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize_op/linear_awq_module.py#L63-L97) 首次调用时做 search_scale + apply_scale 校准、算出 `scale_w/offset_w`，之后切到带缓存的 `fake_quant_forward` 产出浮点伪量化输出——整个过程 `@torch.no_grad()`、无可学习参数。

**NPU 部署模块示例** `NpuWeightQuantizedLinear`（W-only）的 [__init__](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/deploy_op/weight_npu_quant_module.py#L40-L108) 吃进伪量化模块，调 `get_quantize_weight` 把权重烘焙成真低比特张量、`scale_w`/`offset_w` 整形成 NPU 算子要求的 `(K,N)` 形状并 register 为 buffer；其 [forward](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/deploy_op/weight_npu_quant_module.py#L197-L234) 调 `torch_npu.npu_weight_quant_batchmatmul` 跑真低比特推理。对照 `NpuQuantizationLinear`（W+A）的 [forward](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/deploy_op/npu_quantization_linear.py#L64-L125)，它额外调 `torch_npu.npu_dynamic_quant` / `npu_quantize` 对激活做运行时量化，再喂 `npu_quant_matmul`。

#### 4.2.4 代码实践

实践目标：把 4.1 的注册表与本节的「成对替换」串起来，画出一次 classic 量化的模块生命周期。

操作步骤（源码阅读型实践）：

1. 打开 `amct_pytorch/classic/quantize.py`，找到 [quantize()](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize.py#L33-L47) 与 [convert()](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/quantize.py#L51-L59)，确认它们分别只 add 了一个 pass。
2. 假设用户配置里某层算法是 `awq`，对照 [algorithms/__init__.py:72](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L72) 写出该层的三态：`nn.Linear → LinearAWQuant → NpuWeightQuantizedLinear`。
3. 再假设用户配置算法是 `minmax` 且该层带激活量化（有 `scale_d`），对照 [algorithms/__init__.py:74-76](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L74-L76) 与 [_should_use_deploy_op](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/replace_npu_quant_pass.py#L121-L139) 推断三态。

需要观察的现象：

- `awq` 层：部署模块唯一（`NpuWeightQuantizedLinear`），替换 pass 无需二选一；
- `minmax` 层（带 `scale_d`）：替换 pass 在列表 `[NpuWeightQuantizedLinear, NpuQuantizationLinear]` 里选 `NpuQuantizationLinear`。

预期结果：`awq` 走 W-only 部署；`minmax` 带激活量化走 W+A 部署。若该 `minmax` 层不带 `scale_d`，则 `_should_use_deploy_op` 会落到 W-only 分支选 `NpuWeightQuantizedLinear`（待本地验证：可在 REPL 构造带/不带 `scale_d` 的假模块对象，调用 `_should_use_deploy_op` 观察返回）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `minmax` 的部署模块是一个**列表**，而 `awq` 是单个？

参考答案：`awq` 是纯权重量化算法（W-only），部署模块只能是 `NpuWeightQuantizedLinear`；`minmax` 既可配成 W-only 也可配成 W+A，两种配方的部署算子不同（`npu_weight_quant_batchmatmul` vs `npu_quant_matmul`），所以登记为列表，由运行时按 `scale_d` 是否存在二选一。

**练习 2**：`mxquant` 的注册是 `register('mxquant', 'Linear', NpuMXQuantizationLinear, NpuMXQuantizationLinear)`——伪量化模块和部署模块是**同一个类**，这说明什么？

参考答案：说明 MX 量化的伪量化阶段与部署阶段共用同一套实现（该类既能在校准时算 Microscaling 参数，又能直接跑 NPU 推理），不区分训练态/部署态两套类——这是与 awq/gptq 等「伪量化与部署分离」风格不同的特例。

### 4.3 两套体系边界：classic 与 LLM PTQ 的根本区别

#### 4.3.1 概念说明

classic `AlgorithmRegistry` 与 LLM PTQ `ALGO_REGISTRY` 虽然都叫「算法注册表」，但它们解决的是**不同的问题**，绝不能混用。核心分野有三点：

1. **主键设计**：classic 是「(算法名, 源算子类型) 二维键 → 伪量化模块类」；LLM PTQ 是「算法名一维键 → 算法类」，作用对象放在 metadata `targets` 里。
2. **部署归属**：classic 在注册表里**就绑定好**部署模块（`quant_to_deploy`），部署 = 再跑一遍 pass 替换；LLM PTQ 注册表**完全不碰**部署，部署是独立的 `deploy` workflow 阶段，由各模块的 `export_deploy` 产出 payload。
3. **入口与执行模型**：classic 走包根 eager 导出的 `amct_pytorch.quantize/convert` + 全模型图遍历两遍 pass；LLM PTQ 走四条 CLI 命令 + 块级前向 + 逐单元重建训练 + safetensors 导出。

#### 4.3.2 核心流程

LLM PTQ 侧的反向路由 `build_algorithms_by_target` 与 classic 的「二维表查找」形成鲜明对照——它是按 `targets` metadata **过滤**算法名，而非按键查找模块类：

```text
LLM PTQ:  args.algos（如 ['lwc','lac','flatquant']）
            │
            ▼  get_algo_names_by_target(args, target)
          过滤出 metadata['targets'] 含该 target 的算法名
            │
            ▼  _build_algorithm（用 inspect.signature 自适应传参）
          实例化算法，挂到对应挂载点（weight/activation/structure）
          （部署不在这一步，由 deploy workflow 读 .pt 再烘焙）
```

#### 4.3.3 源码精读

**LLM PTQ 的 `ALGO_REGISTRY`** 定义在 [amct_pytorch/algorithms/registry_factory.py:22-31](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/registry_factory.py#L22-L31)，是通用 `Registry` 基类的特化子类，`_register` 里强制校验必须继承 `QuantAlgorithmBase`：

```python
class QuantAlgorithmRegistry(Registry):
    def _register(self, key, obj, force, metadata):
        if not isinstance(obj, type) or not issubclass(obj, QuantAlgorithmBase):
            raise TypeError(...)
        super()._register(key, obj, force, metadata)

ALGO_REGISTRY = QuantAlgorithmRegistry("algo")
```

通用基类 [Registry](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/registry_factory.py#L29-L100) 是「单键 `name` → `RegistryItem(target, metadata)`」的一维字典，靠装饰器 `@ALGO_REGISTRY.register(targets=(...))` 在 import 时副作用登记（登记清单见 [amct_pytorch/algorithms/quant/__init__.py:33-44](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/__init__.py#L33-L44)：LWC/LAC/AutoRound/OmniQuant/FlatQuant）。

反向路由 [get_algo_names_by_target / build_algorithms_by_target](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L28-L67) 的本质是「按 target 过滤」：

```python
for algo_name in args.algos:
    targets = tuple(ALGO_REGISTRY.get_item(algo_name).metadata.get("targets", ()))
    if target in targets:                     # target ∈ {weight, activation, structure}
        selected.append(algo_name)
```

注意它的部署**不在注册表**里——`build_algorithms_by_target` 只实例化算法并挂载，部署阶段（u4-l4）另由 `export_deploy` 把算法参数烘焙成 payload，写进 safetensors。

把两套体系并排对比：

| 维度 | classic `AlgorithmRegistry` | LLM PTQ `ALGO_REGISTRY` |
| --- | --- | --- |
| 基类 | 手写 `Algorithm` 类（两张普通 dict） | 通用 `Registry` 基类（`RegistryItem`） |
| 注册方式 | 命令式 `.register(name, src_op, quant_op, deploy_op)` | 装饰器 `@ALGO_REGISTRY.register(targets=...)` |
| 主键 | **二维复合键** `(算法名, 源算子类型字符串)` | **一维单键** `算法名` |
| 作用对象 | 显式第二维 `src_op`（`Linear`/`Conv2d`/...） | metadata `targets`（`weight`/`activation`/`structure`） |
| 返回物 | 伪量化**模块类**（还要再查部署模块） | **算法类**（无部署模块概念） |
| 部署归属 | **注册表内绑定** `quant_to_deploy`，二遍 pass 替换 | **不在注册表**，由 `deploy` workflow + `export_deploy` 独立完成 |
| 入口 | `amct_pytorch.quantize()` / `convert()`（包根 eager 导出） | 四条 CLI（eval/extract/ptq/deploy） |
| 执行模型 | 全模型图遍历两遍 pass | 块级前向 + 逐 PtqUnit 重建训练 |
| 校验 | 无类型约束（甚至允许字符串 `'FP8Linear'`） | 强制 `issubclass(obj, QuantAlgorithmBase)` |

一个容易踩的坑：`algorithms/__init__.py` 这**一个文件**同时 import 了 classic 的 `quantize_op`/`deploy_op` 模块和 LLM PTQ 的 `ALGO_REGISTRY`（[L25-57](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L25-L57)），两套体系在 import 层是混居的——但它们在**运行时互不相干**：classic 流只读 `AlgorithmRegistry`，LLM PTQ 流只读 `ALGO_REGISTRY`。

#### 4.3.4 代码实践

实践目标：用一个最小判别题，确认你分得清两套注册表的 key 设计。

操作步骤（源码阅读型实践）：

1. 在 `amct_pytorch/algorithms/__init__.py` 找到 `AlgorithmRegistry.register('minmax', 'Linear', MinMaxQuant, [NpuWeightQuantizedLinear, NpuQuantizationLinear])`（[L74-76](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L74-L76)）。
2. 在 `amct_pytorch/algorithms/quant/auto_clip.py`（或同目录其它算法文件）找到 `@ALGO_REGISTRY.register(targets=(...))` 的装饰器写法。
3. 对照回答：要查「minmax 作用在 Linear 上用哪个伪量化模块」，classic 用什么 key？要查「lac 这个算法能否作用在 activation 上」，LLM PTQ 用什么 key？

预期结果：

- classic：两级查 `AlgorithmRegistry.algo['minmax']['Linear']` → `MinMaxQuant`；
- LLM PTQ：`ALGO_REGISTRY.get_item('lac').metadata['targets']` 是否含 `'activation'`（由 [get_algo_names_by_target](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L28-L39) 完成）。

需要观察的现象：classic 的「作用对象」是**显式数据维度**（必须和算法名一起给出才能定位）；LLM PTQ 的「作用对象」是**属性标签**（算法名单独可定位，target 只在反向过滤时用）。这正是「二维键 + 模块替换」与「一维键 + target 路由」的根本区别。

#### 4.3.5 小练习与答案

**练习 1**：如果要在 classic 体系新增一个「W8A8 的全量化算法」`fooquant`，注册调用长什么样？如果要新增一个 LLM PTQ 可学习算法 `fooquant` 呢？

参考答案：
- classic：`AlgorithmRegistry.register('fooquant', 'Linear', FooQuant, NpuQuantizationLinear)`——必须同时给出伪量化模块 `FooQuant` 和部署模块 `NpuQuantizationLinear`，部署在注册时就绑定。
- LLM PTQ：在算法类定义上方加 `@ALGO_REGISTRY.register(targets=('activation',))`（或 `weight`/`structure`），并在 `register_algorithms()` 里补一行 import；**不需要**也无法在这里指定部署模块。

**练习 2**：为什么 LLM PTQ 的注册表**不**像 classic 那样维护 quant→deploy 映射？

参考答案：LLM PTQ 的部署产物是 compressed-tensors 格式的 safetensors 权重 + `quantization_config`（u4-l4），由 `deploy` workflow 读 `.pt` 参数、调各模块的 `export_deploy` 把参数烘焙进权重——这是一条「逐层烘焙权重」的数据流水线，不存在「用 NPU 算子模块替换原模块」的图改写步骤，自然不需要 classic 那种「模块→模块」的替换映射表。

## 5. 综合实践

把本讲三节串起来，完成一张「classic 算法注册清单 + 生命周期」总表。

任务：

1. 对照 [algorithms/__init__.py:71-100](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/__init__.py#L71-L100) 的全部 `register` 调用，填写下表（已给两行示范）：

   | 算法名 | src_op | 伪量化模块 (quant_op) | 部署模块 (deploy_op) | W-only / W+A |
   | --- | --- | --- | --- | --- |
   | gptq | Linear | GPTQuant | NpuWeightQuantizedLinear | W-only |
   | smoothquant | Linear | SmoothQuant | NpuQuantizationLinear | W+A |
   | awq | … | … | … | … |
   | minmax | … | … | … | 两者皆可 |
   | mxquant | … | … | … | … |

2. 在表末尾用一句话写出每个算法的「三态生命周期」（如 `nn.Linear → GPTQuant → NpuWeightQuantizedLinear`）。
3. 最后写一段话，对比 classic `AlgorithmRegistry` 与 LLM PTQ `ALGO_REGISTRY` 在**主键设计**上的根本区别，并说明：如果你面对的是一个非 LLM 的小模型（如 ResNet）该走哪条路、一个 70B 的 LLM 又该走哪条路。

参考结论：

- 小模型（带 Conv2d、走整图替换、要导出 NPU 部署算子）走 classic 流：`amct_pytorch.quantize(model, config)` → 校准 → `amct_pytorch.convert(model)`，靠 `AlgorithmRegistry` 的两遍 pass 完成插入与替换；
- 70B LLM（显存放不下整模型、需要块级重建训练、导出 safetensors）走 LLM PTQ 流：四条 CLI + `ALGO_REGISTRY` 的 target 路由 + 块级重建 + `deploy` 烘焙，注册表只管算法插件、不碰部署。

## 6. 本讲小结

- classic `AlgorithmRegistry` 是手写的 `Algorithm` 类，不是通用 `Registry` 基类；它用**二维复合键 `(算法名, 源算子类型)`** 查伪量化模块，用 `quant_to_deploy` 表维护「伪量化模块 → 部署模块」的反向映射。
- classic 体系的核心是**两阶段算子替换**：`quantize()` 跑 `InsertQuantizeModulePass` 把 `nn.Linear` 替换为伪量化模块（如 `LinearAWQuant`），`convert()` 跑 `ReplaceNpuQuantModulePass` 把伪量化模块替换为 NPU 部署模块（如 `NpuWeightQuantizedLinear`）。
- 伪量化模块（`quantize_op/`，继承 `BaseQuantizeModule`）产出浮点 fake-quant；部署模块（`deploy_op/`）把参数烘焙成真低比特权重并调 `torch_npu` 算子。二者在注册时成对绑定，一个伪量化模块可对应多个部署模块（如 minmax），运行时按属性二选一。
- LLM PTQ 的 `ALGO_REGISTRY` 是通用 `Registry` 的特化子类，**一维键 + targets metadata**，靠装饰器副作用登记，强制继承 `QuantAlgorithmBase`；它**不绑定部署模块**，部署是独立的 `deploy` workflow。
- 两套体系在 import 层混居（同属 `algorithms/` 包）但运行时互不相干：classic 流读 `AlgorithmRegistry`，LLM PTQ 流读 `ALGO_REGISTRY`，选型依据是「全图替换 + NPU 算子替换（小模型）」还是「块级重建 + safetensors 烘焙（大 LLM）」。

## 7. 下一步学习建议

- 本讲聚焦两套注册表的边界，但 classic 流的图遍历 pass 编排细节在 u9-l1「经典图压缩与算子融合优化流程」深入展开（`ModelOptimizer` 如何串 pass、`base_module_fusion_pass` 的 `run/match_pattern/do_pass` 模板方法），建议接着读。
- 若想确认本讲提到的部署模块在真机上的行为，可结合 u8 单元（amct_ops）阅读 `npu_weight_quant_batchmatmul` / `npu_quant_matmul` 底层算子，但这些算子需 NPU 环境才能运行。
- 若你的兴趣在 LLM PTQ，回到 u6-l2「算法注册与 target 路由机制」复习 `build_algorithms_by_target`，并预习 u4-l4「部署导出 deploy」看 LLM PTQ 如何不经注册表、纯靠 `export_deploy` 完成部署烘焙。
