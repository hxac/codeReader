# 量化感知训练 QAT 实现要点

## 1. 本讲目标

本讲是第四单元「Vitis AI 量化」的第二篇，承接 u4-l1。u4-l1 把量化的「是什么、为什么」与 PTQ（训练后量化）的校准流程讲透了，并在结尾留下了一个明确的接力点：**当 PTQ 的精度掉得太多、需要让权重主动去适应量化噪声时，就要转 QAT（量化感知训练，Quantization Aware Training）**。本讲就来拆解 QAT 在代码层面到底改了什么。

读完本讲你应当能够：

- 说清 **`QatProcessor` 如何把一个浮点模型变成「可训练的量化模型」**，以及为什么要同时维护一份 `ori_model` 浮点副本。
- 区分两种模型形态——**`trainable_model`（训练用，伪量化节点阈值可训练）** 与 **`deployable model`（评估/导出用，伪量化折叠成真定点算子）**——并解释 `convert_to_deployable` 在每个 epoch 做了什么。
- 掌握 QAT 里最关键的一个工程技巧：**为名字含 `threshold` 的参数单独建一个参数组、给约 **100×** 的学习率**，并能从「权重 vs 量化阈值」两类参数的本质差异论证它为什么有效。
- 理解 `quant_info`（量化阈值/尺度表）的**导出与加载链路**：训练时如何随 checkpoint 保存、加载时 QAT 与 PTQ 两条分支如何分别处理。
- 看懂 `software/quantization/modifications.md` 里对 `task.py` / `trainer.py` 两文件 QAT 改造的每一条描述，并能据此还原出 `_setup_train` 的伪代码。

本讲只聚焦 QAT 的「训练期改造与阈值优化、quant_info 导出」。两件相关但独立的事留给同单元后续讲义：`modules.py` 里 `C2f`/`SPPF`/`Detect` head 与 `cat`/`MaxPool2d` 的算子适配是 u4-l3；导出后的 `xmodel` 如何编译成 DPU 指令是 u4-l4。

> 重要前提（u4-l1 已说明，这里重申）：由于上游 Ultralytics 仓库的许可证限制，`software/quantization/` **不提供量化源码**，只有两份文档——命令说明 `README.md` 与改动概述 `modifications.md`。真正的 QAT 逻辑在 AMD 的 `pytorch_nndct` 库（Vitis AI 3.5）里。因此本讲的「源码精读」一律以这两份文档的原文为锚点，给出永久链接与行号；凡是涉及 `pytorch_nndct` 库内部行为（如 `QatProcessor` 的构造细节、伪量化的反传机制）的描述，都属于量化通用原理，会明确标注为通用知识、不编造行号。

## 2. 前置知识

在进入 QAT 实现之前，先补齐四个直觉。第一个来自 u4-l1 的复述，后三个是本讲新引入。

**第一，伪量化（fake quantization）是 QAT 的心脏。** u4-l1 讲过 int8 量化的数学：把浮点值 \(x\) 经缩放因子 \(s\) 映射成整数 \(q\)。在 QAT 里，我们不真的把权重存成 int8（那样就没法求导了），而是在前向传播中插入一对 **quant / dequant 节点**——先量化、再立刻反量化：

\[
\hat{x} \;=\; s \cdot \mathrm{clip}\!\left(\mathrm{round}\!\left(\frac{x}{s}\right),\,-128,\,127\right)
\]

也就是说，网络在前向时「看到的」是 \(\hat{x}\)，它和真实浮点值 \(x\) 之间有一个 \(\le s/2\) 的舍入误差。**这个误差被故意暴露在损失函数里**，于是反向传播就能驱动权重去「避开」量化敏感的方向。这套 quant/dequant 包络，正是 u4-l1 引用的 `task.py` 改动里「在模型 predict 前后调用 stub」要落地的东西。

**第二，nndct 用「阈值（threshold）」来参数化每一层的量化范围。** 在 AMD 的术语里，每一层要被量化的张量（权重或激活）都有一个 **threshold**（阈值）\(T\)，它就是「映射到 int8 最大等级的那个浮点值」。threshold 与 u4-l1 里的 scale \(s\) 是一一对应的：

\[
s \;=\; \frac{T}{2^{b-1}-1} \;=\; \frac{T}{127}\qquad(b=8)
\]

所以「训练 threshold」就是「训练量化尺度」——threshold 决定了这一层 int8 格子的疏密。理解了「threshold = 量化尺度参数」，后面 4.2「为什么给 threshold 单独设大学习率」才会有根基。

**第三，QAT 不是从零开始，而是「骑在 PTQ 校准的肩膀上」。** 这是一条容易被忽略、但极其关键的工程事实。`QatProcessor` 产出可训练模型时，需要一个 `calib_dir`（校准目录）参数——它指向**一次先前 PTQ 校准的输出目录**，里面装着校准得到的初始 threshold。换句话说，QAT 的 threshold 初值来自 PTQ 校准，然后 QAT 再微调它们。所以推荐工作流是 **先 PTQ calib 拿初值 → 再 QAT train 精修**，这也再次印证了 u1-l3 训推一致性暗线：PTQ 与 QAT 命令的 `imgsz=800`、两个 `--nndct_convert_*` 激活标志必须完全一致，否则喂给 QAT 的初始 threshold 就和当前图拓扑对不上。

**第四，EMA（指数移动平均）模型是最终部署的那个。** Ultralytics 在训练中维护一份权重的指数移动平均：

\[
\theta_{\text{ema}} \;\leftarrow\; \alpha\,\theta_{\text{ema}} + (1-\alpha)\,\theta
\]

EMA 权重比原始权重更平滑、泛化更稳，验证与选优都看它。在本项目的 QAT 改造里你会看到 `ori_model_ema`、`qat_ema_state_dict`、`qat_ema_quant_info`——**凡是带 `ema` 的，都是最终要被加载、转换、部署的那一份**。记住这条，4.3 的 quant_info 导出链路就一目了然。

> 术语速查：
> - **stub（quant/dequant stub）**：插入网络前后的一对「量化 / 反量化」占位算子，前向时把数值在浮点与定点间来回转，是伪量化的载体。
> - **trainable_model**：`QatProcessor` 产出的、带可训练 threshold 的伪量化模型，训练时用它（前向 + 反向）。
> - **deployable model**：把伪量化「折叠」成真定点 quant/dequant 算子、threshold 已固定的模型，评估与导出 `xmodel` 时用它。
> - **threshold**：每层量化范围的浮点上界，等于 \(127\cdot s\)；QAT 要训练的就是它。
> - **EMA**：权重的指数移动平均，更稳的那一份，本项目里是被部署的那一份。

## 3. 本讲源码地图

与 u4-l1 一样，本讲只涉及 `software/quantization/` 下的两份文档（无源码），但侧重点不同——u4-l1 主要读 `README.md` 的命令，本讲主要读 `modifications.md` 的代码改动概述：

| 文件 | 在本讲的作用 |
| --- | --- |
| `software/quantization/modifications.md` | **本讲主角**。它用 4 节概述了上游 Ultralytics 四个文件（`task.py`/`trainer.py`/`modules.py`/`validator.py`）为接入 Vitis AI 量化所做的改动。本讲聚焦其中 `trainer.py`（4.1、4.2、4.3 主力）与 `task.py`（4.3 的加载分支）两节。 |
| `software/quantization/README.md` | 提供一条 **QAT 训练命令**作为对照锚点，用来反推参数（`lr0=0.005`、`epochs=100` 等），并支撑 4.2 的「~100× 学习率」数值论证。 |

`modifications.md` 把改动按文件编号为 1～4。本讲的三个最小模块恰好对应其中编号 2（`trainer.py`）的三处关键改法（`_setup_train`、`build_optimizer`、`save_model`），并在 4.3 借助编号 1（`task.py` 的 `attempt_load`）讲加载分支。编号 3（`modules.py`）的算子适配留给 u4-l3，编号 4（`validator.py`）的 quantizer/export 已在 u4-l1 讲过。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲 **`QatProcessor` 与 deployable model**（4.1，回答「QAT 训练用一个什么样的模型」），再讲 **阈值参数组的 ~100× 学习率技巧**（4.2，回答「QAT 怎么优化 threshold」），最后讲 **quant_info 的导出与加载**（4.3，回答「训练完的量化参数怎么存、怎么读」）。

### 4.1 QatProcessor 与 deployable model

#### 4.1.1 概念说明

这个模块回答 QAT 的头号问题：**训练时用的那个「量化模型」到底是什么形态，它和最终部署的模型又是什么关系？**

先建立核心直觉：**QAT 在一次训练里同时存在「两种模型形态」**，它们来回切换：

1. **`trainable_model`（可训练模型）**：这是 `QatProcessor` 产出的主形态。它在网络里插入了伪量化节点，并且**把这些节点的 threshold 设成了可训练参数**（带梯度、会被优化器更新）。训练的前向 + 反向都在它上面进行——前向时数值经受量化误差，反向时梯度同时回流到权重和 threshold。
2. **deployable model（可部署模型）**：把 `trainable_model` 里的伪量化「折叠」掉——即把每个伪量化节点固化成一对真正的定点 quant/dequant 算子、threshold 锁定为当前值。它**不再用于求导**，而是用于**评估真实 int8 精度**与**导出 `xmodel`**。

为什么要分两种？因为伪量化是为「求导」服务的（threshold 要能收梯度），而 DPU 要的是「真定点」。`trainable_model` 负责训练，deployable model 负责落地，两者靠 `convert_to_deployable` 这一步衔接。

再看 `QatProcessor` 这个名字本身。它是 AMD `pytorch_nndct` 提供的「QAT 处理器」对象，承担三件事：

- **吸收一个浮点模型 + 一个 `calib_dir`**：`calib_dir` 装着先前 PTQ 校准算出的初始 threshold（见前置知识第三点），`QatProcessor` 据此为每层初始化量化节点。
- **暴露 `trainable_model`**：把浮点模型改造成带可训练 threshold 的伪量化模型，交给 trainer 当作 `self.model` 继续训练。
- **提供 `convert_to_deployable`**：在需要评估/导出时，把当前训练态模型折叠成 deployable model。

位宽方面，u4-l1 已确认本项目用 **8 比特**（`modifications.md` 明写 `bitwidth of 8`），所以 `QatProcessor` 自始至终是 int8 量化。

最后解释为什么 trainer 还要保留一份 `ori_model` 与 `ori_model_ema`。`QatProcessor` 在改造模型时会「包装 / 变换」原模型；为了能随时回到原始浮点结构（用于对比、用于在 `convert_to_deployable` 里参照原架构、或用于回滚），trainer 在调用 `QatProcessor` **之前**先把 `self.model` 深拷贝两份，作为浮点基准（原始版 + EMA 版）。训练过程中这两份副本的 `state_dict` 会被持续同步更新，保证它们和量化训练态不脱节。

#### 4.1.2 核心流程

把 4.1.1 的概念落到 trainer 的生命周期上，QAT 的一次训练循环长这样：

```
# —— 训练开始前：_setup_train ——
ori_model     = deepcopy(self.model)        # 保留浮点基准（原始）
ori_model_ema = deepcopy(self.model)        # 保留浮点基准（EMA）
patch(__deepcopy__)                          # 修掉 nndct 自定义张量子类的深拷贝 bug
qat_processor = QatProcessor(self.model, bitwidth=8)
self.model    = qat_processor.trainable_model(calib_dir=...)   # 训练用伪量化模型

for epoch in range(epochs):
    train_one_epoch(self.model)              # 前向(伪量化)+反向, 更新权重和 threshold

    # —— 每个 epoch 验证前：折叠成 deployable ——
    deployable, deployable_ema = qat_processor.convert_to_deployable(self.model, model_ema)
    ori_model.load_state_dict(...)           # 同步浮点基准
    ori_model_ema.load_state_dict(...)
    validate(deployable_ema)                 # 在真定点模型上评估 int8 精度
    save_model(...)                          # 存 checkpoint(含 quant_info, 见 4.3)
```

关键节点有三：

1. **`_setup_train` 里一次性造好训练态**：深拷贝浮点基准 → 打 `__deepcopy__` 补丁 → 建 `QatProcessor` → 用 `trainable_model` 替换 `self.model`。此后整个训练循环里的 `self.model` 都是带可训练 threshold 的伪量化模型。
2. **每个 epoch 验证前做一次 `convert_to_deployable`**：把当前训练态折叠成 deployable model（原始 + EMA 各一份），这样验证看到的才是**真实 int8 精度**而不是「伪量化精度」——两者在阈值边界处有差异，必须折叠后才准。
3. **`convert_to_deployable` 还顺带产出 quant_info**：这一步算出的量化信息会被 `save_model` 收走存盘（见 4.3）。

注意「原始 vs EMA」这条双线贯穿始终：训练、折叠、保存都成对处理 `model` 与 `model_ema`，而最终部署用的是 EMA 那一份。

#### 4.1.3 源码精读

上面流程里的第 1 步（`_setup_train` 的四行核心），`modifications.md` 在描述 `trainer.py` 时逐条写明：

> [software/quantization/modifications.md:14-19](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L14-L19) —— 这段概述 `trainer.py` 的 `_setup_train` 改动，恰好对应 4.1.2 流程的前四行：(a) 把 `self.model` 深拷贝成 `ori_model` 和 `ori_model_ema` 两个类属性；(b) 覆写 PyTorch 的 `__deepcopy__` 来修补一个与自定义张量子类相关的报错；(c) 创建 `QatProcessor`，**bitwidth of 8**；(d) 令 `self.model = self.qat_processor.trainable_model(calib_dir=calib_dir.as_posix())`——注意 `calib_dir` 入参，这正是 4.1.1 说的「QAT 骑在 PTQ 校准肩膀上、用其 threshold 作初值」的落点。

第 2 步（每个 epoch 验证前的折叠），紧接着的下一节：

> [software/quantization/modifications.md:21-23](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L21-L23) —— 说明「每个 epoch 验证前」：用 `self.qat_processor.convert_to_deployable` 从 `model` 与 model EMA 各造一份 deployable model（写到 `qat/deployableema|deployable`），再用 `state_dict` 把 `ori_model` 与 `ori_model_ema` 更新同步。这正是 4.1.2 流程的循环内两步：折叠出真定点模型用于评估，同时让浮点基准不脱节。

把 4.1.3 的两段引用与 4.1.2 的伪代码逐行对齐，你会发现 `modifications.md` 的寥寥数语已经完整刻画了 QAT 训练态的构造与每轮折叠——这是本模块最值得反复对照的一组锚点。

#### 4.1.4 代码实践（本讲主实践）

**实践目标**：根据 `modifications.md` 对 `trainer.py` 的描述，写出 `_setup_train` 中「创建 `QatProcessor`、提取 `trainable_model`」的伪代码，并解释为什么要给 `__deepcopy__` 打补丁。这正是本讲规格指定的代码实践。

**操作步骤**：

1. 先回读 4.1.3 的两处永久链接原文（[modifications.md:14-19](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L14-L19) 与 [modifications.md:21-23](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L21-L23)），把每一句bullet翻译成代码。
2. 把下面的伪代码（**示例代码，非仓库代码**，仅用于还原改动意图）补全：

```python
# 示例代码：还原 trainer._setup_train 的 QAT 构造流程（伪代码，依 modifications.md 改写）
import copy
import torch

def _setup_train(self):
    # (a) 保留浮点基准：原始版 + EMA 版（QatProcessor 会改造模型，需留底）
    self.ori_model     = copy.deepcopy(self.model)
    self.ori_model_ema = copy.deepcopy(self.model)

    # (b) 打补丁：修掉 nndct 自定义张量子类在 deepcopy 时的报错（见下方解释）
    _patch_deepcopy_for_nndct_tensor()

    # (c) 创建 QAT 处理器，固定位宽 8（int8）
    self.qat_processor = QatProcessor(self.model, bitwidth=8)

    # (d) 用「可训练量化模型」替换 self.model；calib_dir 指向先前 PTQ 校准输出
    calib_dir = self.save_dir / "calib"          # 由先前 quant_mode=calib 产生
    self.model = self.qat_processor.trainable_model(calib_dir=calib_dir.as_posix())

    # 之后：构建优化器（含 threshold 参数组，见 4.2）、调度器、EMA 等……

def after_epoch_before_validate(self):
    # 每个 epoch 验证前：把训练态折叠成真定点 deployable model（原始 + EMA）
    deployable, deployable_ema = self.qat_processor.convert_to_deployable(
        self.model, self.ori_model_ema)
    # 同步浮点基准的 state_dict，保持不脱节
    self.ori_model.load_state_dict(self.model.state_dict())
    # 验证走 deployable_ema（EMA 那一份，即最终部署形态）
    self.validator(deployable_ema)
```

3. **回答「为什么要把 `__deepcopy__` 打补丁」**。把你的解释写下来，再与下方参考答案对照。

**需要观察的现象 / 预期结果（解释型实践，答案见下）**：

`__deepcopy__` 补丁的成因可以这样论证（属 PyTorch / nndct 通用原理，**具体报错形态待本地 Vitis AI 环境验证**）：

- `QatProcessor.trainable_model(...)` 产出的模型，其内部张量被 `pytorch_nndct` 包装成了一种**自定义 Tensor 子类**——目的是拦截每一次运算、在前向时插入伪量化、在反向时正确分发梯度。这是「量化感知」能工作的底层机制。
- PyTorch 原生的 `copy.deepcopy` 在处理「带自定义 dispatch / autograd 钩子的张量子类」时并不总是一帆风顺：深拷贝会递归复制张量的全部元数据与 dispatch key，自定义子类若没有正确实现 `__deepcopy__`，就可能抛出「无法拷贝某子类」「dispatch key 缺失」之类的错误。
- 而 trainer 偏偏要在 `QatProcessor` 改造模型**之前**就 `deepcopy(self.model)`（造 `ori_model`），又要在之后继续拷贝；为了让这两次拷贝都安全，干脆**覆写 `__deepcopy__`**，把自定义张量子类的拷贝逻辑收敛到一个受控的实现里——这就是 `modifications.md` 那句「Override pytorch `__deepcopy__` operator to patch error with custom tensor subclass」的含义。

**预期结论**：补丁的本质是「让 PyTorch 的通用深拷贝机制与 nndct 的自定义张量子类兼容」。它不是业务逻辑，而是为了打通「浮点基准深拷贝」与「QAT 改造」共存的生命周期。如果省掉这一步，在 `ori_model = deepcopy(self.model)` 处就可能直接报错中断。

> 说明：由于本仓库不含量化源码，本实践产出的是**一份伪代码还原 + 原理论证**，`QatProcessor` 的真实构造签名、`__deepcopy__` 补丁的具体实现均标注为「待本地（Vitis AI 3.5 环境）验证」。

#### 4.1.5 小练习与答案

**练习 1**：QAT 训练时，`self.model`（即 `trainable_model`）里的伪量化 threshold 是「可训练参数」。如果把它改成 `requires_grad=False`（冻结 threshold、只训权重），QAT 就退化成了什么？

> **参考答案**：退化成「带伪量化前向、但量化参数固定」的微调——本质上近似于一次**用 PTQ 校准 threshold 作固定值的 finetune**。它失去了 QAT 最核心的能力（让量化尺度自身去适应数据），精度收益会大打折扣。可见 QAT 的价值恰恰在于「threshold 可训练」这一条。

**练习 2**：为什么「每个 epoch 验证前」都要调用一次 `convert_to_deployable`，而不是只在训练结束时折叠一次？

> **参考答案**：因为验证要看**真实 int8 精度**，而 `trainable_model` 的伪量化精度与折叠后的定点精度在 threshold 边界处有差异。若不折叠就验证，选出的「最佳权重」可能是伪量化下的假象，部署后精度对不上。每轮折叠一次，才能保证「验证选优」与「最终部署」在同一口径下进行——这也呼应 u1-l3 的训推一致性。

### 4.2 阈值参数组优化

#### 4.2.1 概念说明

这个模块讲 QAT 里最巧妙、也最容易被忽略的一个工程技巧：**给量化 threshold 单独开一个「高学习率」参数组**。

先把「QAT 里到底在优化谁」想清楚。一次 QAT 训练，优化器要更新的参数其实分**两类**：

| 参数类别 | 是什么 | 数量级 | 初始状态 |
| --- | --- | --- | --- |
| **权重（weights）** | 卷积核、BN 参数、检测头参数等「原模型参数」 | 千万～上亿 | 已是浮点预训练的好值（来自 u3 的 `.pt`） |
| **threshold（量化阈值）** | 每层量化节点的范围上界 \(T\)，决定 scale \(s=T/127\) | 每层一个，整体很少 | 来自先前 PTQ 校准（见 4.1 的 `calib_dir`） |

这两类参数的「处境」截然不同，决定了它们该用**不同的学习率**：

- **权重的处境**：它已经坐在浮点训练得到的「好最优」附近，QAT 只是想让它**微微挪动**去适应量化噪声。学习率该**小**，否则会把已经训好的权重带飞。
- **threshold 的处境**：它是 QAT 才引入的「新变量」，要在训练中主动搜索「能让当前权重 + 当前数据下量化误差最小的范围」。它需要**大步探索**，学习率该**大**。另外，threshold 每层只有一个、梯度信号聚合得少，天然需要更大学习率来补偿。

所以正确做法是**差异化学习率（differential learning rate）**：权重用一个常规 LR，threshold 用一个显著更大的 LR。`modifications.md` 给出的倍数是 **~100×**——也就是 threshold 的学习率约为权重的 100 倍。

这个倍数不是随便定的，它和 QAT 命令里的 `lr0` 直接挂钩。README 的 QAT 命令里 `lr0=0.005`（见 4.2.3），那么 threshold 组的学习率约为 \(0.005 \times 100 = 0.5\)。权重小步微调（5e-3 量级）、threshold 大步搜索（5e-1 量级），两者协同，才能在 100 个 epoch 内把量化精度拉回来。

#### 4.2.2 核心流程

差异化学习率在 trainer 里的落地，是改写 `build_optimizer`：

1. **遍历模型参数，按名字分流**：把名字里含 `"threshold"` 的参数挑出来——nndct 给量化阈值参数命名时带 `threshold` 字样，正好用来识别。
2. **建两个参数组**：
   - 普通组：其余所有参数，学习率 = 主 LR（如 `lr0=0.005`）。
   - threshold 组：仅含 threshold 参数，学习率 = 主 LR × ~100。
3. **把两组一起塞进同一个优化器**（如 SGD）。PyTorch 优化器原生支持「参数组（param groups）」，每组可独立设学习率，所以一个 `optimizer` 就能同时按两种步长更新。

伪代码：

```python
# 示例代码：build_optimizer 里的差异化学习率（依 modifications.md 改写）
threshold_params = [p for n, p in model.named_parameters() if "threshold" in n]
other_params     = [p for n, p in model.named_parameters() if "threshold" not in n]

base_lr = 0.005                                  # 来自 QAT 命令 lr0=0.005
optimizer = SGD([
    {"params": other_params,     "lr": base_lr},          # 权重: 常规 LR
    {"params": threshold_params, "lr": base_lr * 100},    # threshold: ~100x LR
], momentum=0.9)                                  # 来自 QAT 命令 momentum=0.9
```

注意 `momentum=0.9`、`optimizer=SGD` 这两个值都和 README 的 QAT 命令一致——`build_optimizer` 实际上就是把命令行传进来的超参装配进带两组 LR 的优化器。

#### 4.2.3 源码精读

差异化学习率的改动概述，`modifications.md` 在 `trainer.py` 的 `build_optimizer` 一节写得非常明确：

> [software/quantization/modifications.md:29-31](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L29-L31) —— 这段说明 `build_optimizer` 的改动：先选出名字含 `"threshold"` 的模型参数；再为这些 QAT threshold 建一个参数组，学习率设为「约为模型主体学习率的 100 倍（~100x larger）」并加入优化器。这正是 4.2.2 伪代码的两步来源。

这「~100×」的数值，要和 QAT 训练命令的 `lr0` 对读才有意义：

> [software/quantization/README.md:47-49](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L47-L49) —— QAT 训练命令。逐项与 `build_optimizer` 对应：`optimizer=SGD` 决定优化器类型；`momentum=0.9` 进 SGD；`lr0=0.005` 是**权重组**的初始学习率；于是 **threshold 组的初始学习率约为 \(0.005 \times 100 = 0.5\)**；`epochs=100`、`warmup_epochs=0` 决定训练长度与无预热。这些训练超参的存在本身（u4-l1 已论证）就宣告「QAT 是一次完整训练」，而本讲进一步揭示：这次训练里 threshold 跑得比权重快约两个数量级。

把这两段引用合起来看，「~100×」就从一个抽象倍数变成了一个可算的数字：**权重 5e-3 微调、threshold 5e-1 搜索**。这就是本项目 QAT 能在 100 epoch 内把量化精度拉回 ~2%/3% 差距的关键旋钮之一。

#### 4.2.4 代码实践

**实践目标**：用一段**示例代码**亲手感受「差异化学习率参数组」——同一个优化器里两组参数按不同 LR 更新，验证 threshold 那组确实走得更快。

**操作步骤**：运行下面示例代码（非仓库代码，仅依赖 torch）。

```python
# 示例代码：演示差异化学习率参数组
import torch

# 造两个参数：一个模拟"权重"(需要小 LR 微调)，一个模拟"threshold"(需要大 LR 搜索)
weight    = torch.nn.Parameter(torch.tensor([1.0]), requires_grad=True)
threshold = torch.nn.Parameter(torch.tensor([10.0]), requires_grad=True)

base_lr = 0.005
opt = torch.optim.SGD([
    {"params": [weight],    "lr": base_lr},         # 权重: 0.005
    {"params": [threshold], "lr": base_lr * 100},   # threshold: 0.5 (~100x)
], momentum=0.0)

# 给两个参数各塞一个相同大小的梯度，看一步后位移差距
weight.grad    = torch.tensor([1.0])
threshold.grad = torch.tensor([1.0])
opt.step()

print("weight    一步更新量:", 1.0 - weight.item())     # 期望 ≈ 0.005
print("threshold 一步更新量:", 10.0 - threshold.item())  # 期望 ≈ 0.5
print("两组位移比 :", (10.0 - threshold.item()) / (1.0 - weight.item()))
```

**需要观察的现象（待本地验证具体数值，规律确定）**：

1. `weight` 一步只走了约 `0.005`（从 1.0 → 0.995）；`threshold` 一步走了约 `0.5`（从 10.0 → 9.5）。
2. 两组位移比约为 `100`——印证「~100×」直接体现在更新步长上。
3. 打印 `opt.param_groups`，能看到两组各自带独立的 `lr` 字段。

**预期结论**：差异化学习率不是「两个优化器」，而是「一个优化器、两个参数组」。把它套到 QAT 上，权重小步守住预训练最优、threshold 大步搜索最佳量化范围——这正是 4.2.1 论证的「两类参数、两种处境」的落地。

#### 4.2.5 小练习与答案

**练习 1**：如果把 threshold 组的学习率也设成和权重一样的 `0.005`（即取消 100×），训练会表现成什么样？

> **参考答案**：threshold 会更新得太慢，100 个 epoch 内搜不到最优量化范围，量化精度回收不充分——QAT 的精度优势被削弱，最终可能只比纯 PTQ 好一点点。这反过来说明「~100×」是经过调参的、对 threshold 这类「少量、需大步探索」参数的合理加速。

**练习 2**：为什么用「名字含 `threshold`」来识别量化阈值参数，而不是用一个单独的可训练列表？

> **参考答案**：因为 nndct 把量化阈值作为带 `threshold` 命名的 `nn.Parameter` 挂在模型里，最省事、最稳健的识别方式就是按命名筛选。这样新增/删减量化层时无需维护额外列表，`build_optimizer` 自动适配。代价是命名约定耦合——若上游改了命名规则，这里的筛选条件也要跟着改。

### 4.3 quant_info 导出

#### 4.3.1 概念说明

前两个模块解决了「QAT 训练态怎么造、threshold 怎么优化」。本模块收尾最后一个问题：**训练得到的量化参数（threshold/quant_info）怎么存盘、加载时又怎么取出来？**

先明确 **quant_info 是什么**。它就是整张网络每一层量化阈值的「登记表」——记录了每个要被量化的张量（权重或激活）对应的 threshold（等价于 scale \(s=T/127\)）。有了这张表，浮点模型才能被「折叠」成真定点 `xmodel`、最终编译成 DPU 指令。u4-l1 里 PTQ 的 `export_quant_config()` 写出的就是同性质的东西；QAT 里它来自 `convert_to_deployable` 的产物。

再明确 **quant_info 在 QAT 里为何需要专门管理**。PTQ 的 quant_info 是校准「一次性」产出、写到一个文件即可；而 QAT 的 threshold **每个 epoch 都在变**，所以必须：

- **随 checkpoint 保存**：每个保存点都要把当时的 quant_info 一起存下来，否则加载 checkpoint 时只有权重、没有量化范围，模型无法部署。
- **加载时分 QAT / PTQ 两条分支**：因为 QAT 与 PTQ 存盘的内容不同（QAT 存的是 EMA 模型 + 可转换状态，PTQ 存的是 quant_info 表），加载逻辑要分别处理。

这里要再次强调前置知识第四点的 **EMA 主线**：本项目最终部署的是 EMA 那一份。所以你会看到保存的键名里 **`ema` 频繁出现**——`qat_ema_state_dict`、`qat_ema_quant_info`——它们才是加载、转换、部署时真正取用的对象；而 `qat_model_*` 是对应的原始（非 EMA）版本，留作对照。

#### 4.3.2 核心流程

quant_info 在 QAT 全生命周期的流转：

```
训练期(每个 epoch):
    convert_to_deployable(model, model_ema)   →  产出 quant_info(原始 + EMA)
            ↓
    save_model: 把下列键写入 checkpoint
        - qat_model_state_dict       (原始权重)
        - qat_model_quant_info       (原始 quant_info)
        - qat_model_quant_info_test  (测试用 quant_info)
        - qat_ema_state_dict         (EMA 权重 ← 部署用)
        - qat_ema_quant_info         (EMA quant_info ← 部署用, 见 task.py PTQ 分支)

加载期(attempt_load, 按 QAT/PTQ 分流):
    若 QAT:  从 checkpoint 读 qat_ema_state_dict 灌进模型
             → 再 convert_to_deployable 得到可评估的定点模型
    若 PTQ:  从 checkpoint 读 qat_ema_quant_info
             → 写出到 quant_info.json 文件(供后续 quant_mode=test 导出复用)
```

两个要点：

1. **save_model 的 quant_info 来源是上一次 `convert_to_deployable`**：训练循环里每轮先折叠、再保存，所以存进 checkpoint 的 quant_info 是「该 epoch 折叠后」的最新值，与权重严格配套。
2. **加载分支的差异本质是「QAT 要复活一个可训练量化模型再折叠，PTQ 只需把量化表落盘」**：QAT 分支加载 EMA 权重后还要再走一次 `convert_to_deployable` 才能评估；PTQ 分支根本不复活训练态，直接把 quant_info 写成 `quant_info.json` 交给 u4-l1 讲过的 `quant_mode=test` 导出流程。

#### 4.3.3 源码精读

先看 **保存侧**（`save_model`）：

> [software/quantization/modifications.md:25-27](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L25-L27) —— 这段说明 `save_model` 的改动：往 checkpoint 里新增 `qat_model_quant_info`、`qat_model_quant_info_test`、`qat_model_state_dict`、`qat_ema_state_dict` 四类键；并指明 quant_info「来自上一次 `convert_to_deployable` 调用」。注意这里 `ema` 与非 `ema` 成对出现——EMA 那一份（`qat_ema_state_dict`，以及加载分支里的 `qat_ema_quant_info`）是部署主线。

再看 **加载侧**（`task.py` 的 `attempt_load`），它按 QAT / PTQ 分两条分支，恰好对应 4.3.2 流程图的最末：

> [software/quantization/modifications.md:8-10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L8-L10) —— 这段说明 `attempt_load` 的改动：**若是 QAT**，从 checkpoint 把 `qat_ema_state_dict` 加载进模型，并把该量化模型转换成 deployable model 用于评估；**若是 PTQ**，把 checkpoint 里的 `qat_ema_quant_info` 写成一个 `quant_info.json` 文件。这一条 bullet 同时回答了「QAT 加载要复活+折叠」「PTQ 加载只落盘 quant_info」两个分支。

把保存侧（[L25-27](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L25-L27)）与加载侧（[L8-10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L8-L10)）对读，quant_info 的完整闭环就清楚了：**训练时由 `convert_to_deployable` 产出 → `save_model` 存进 checkpoint（EMA 主线）→ 加载时 QAT 分支复活 EMA 再折叠评估 / PTQ 分支落盘成 `quant_info.json` 交给导出**。其中 PTQ 分支产出的 `quant_info.json`，正是 u4-l1 讲过的「`quant_mode=test` 导出时复用的 scale 表」的来源——两个讲义在这里精确接轨。

#### 4.3.4 代码实践

**实践目标**：用一段示例代码，把 quant_info 在「保存 → 加载（QAT 分支 / PTQ 分支）」之间的数据流抽象成可读的结构，巩固对两条加载分支差异的理解。

**操作步骤**：阅读并补全下面的示例代码（**示例代码，非仓库代码**，仅作数据流示意）。

```python
# 示例代码：quant_info 保存/加载的数据流示意（依 modifications.md 改写）
import json, torch

# —— 训练期 save_model：把 convert_to_deployable 的产物随 checkpoint 存盘 ——
def save_model(path, model, model_ema, quant_info, quant_info_ema):
    torch.save({
        "qat_model_state_dict":      model.state_dict(),          # 原始权重
        "qat_model_quant_info":      quant_info,                  # 原始 quant_info
        "qat_model_quant_info_test": quant_info,                  # 测试用(同源)
        "qat_ema_state_dict":        model_ema.state_dict(),      # EMA 权重 ← 部署主线
        # qat_ema_quant_info 在 PTQ 加载分支被取出
        "qat_ema_quant_info":        quant_info_ema,              # EMA quant_info ← 部署主线
    }, path)

# —— 加载期 attempt_load：按 QAT / PTQ 分流 ——
def attempt_load(path, mode):
    ckpt = torch.load(path, map_location="cpu")
    if mode == "qat":
        # QAT 分支：复活 EMA 权重 → 再 convert_to_deployable 评估
        model.load_state_dict(ckpt["qat_ema_state_dict"])
        deployable_ema = qat_processor.convert_to_deployable(model)
        return deployable_ema
    elif mode == "ptq":
        # PTQ 分支：不复活训练态，直接把 quant_info 落盘成 json
        with open("quant_info.json", "w") as f:
            json.dump(ckpt["qat_ema_quant_info"], f)   # 交给后续 quant_mode=test 导出
        return "quant_info.json"
```

**需要观察的现象（解释型实践，预期结果见下）**：

1. `save_model` 存的键名里，`ema` 与非 `ema` 成对出现——印证「EMA 主线 + 原始对照」。
2. `attempt_load` 的两条分支做的事**性质不同**：QAT 分支返回**一个模型对象**（deployable_ema，可直接评估）；PTQ 分支返回**一个文件路径**（`quant_info.json`，供导出复用）。
3. 把本示例的 PTQ 分支与 u4-l1 的 [modifications.md:8-10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L8-L10) 对读，应能说清：PTQ 分支写出的 `quant_info.json`，就是 u4-l1 PTQ 流程「`quant_mode=test` 固化导出」所依赖的那张 scale 表。

**预期结果**：你能用自己的话复述 quant_info 的闭环，并指出「QAT 分支产模型、PTQ 分支产文件」这条关键分水岭。具体的 quant_info 数据结构（字段名、嵌套）依赖 `pytorch_nndct` 实现，标注为「待本地（Vitis AI 环境）验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果 `save_model` 只存了 `qat_ema_state_dict` 而漏存了 quant_info，加载后会发生什么？

> **参考答案**：加载时只有 EMA 权重、没有量化范围（threshold 表）。模型无法被 `convert_to_deployable` 正确折叠——因为折叠需要知道每层的 threshold 才能把伪量化固化成定点算子。结果是要么报错、要么得到一个量化参数缺失的废模型。这说明 quant_info 必须与权重**成套保存**。

**练习 2**：为什么加载分支里，QAT 用的是 `qat_ema_state_dict`（EMA 版），而不是 `qat_model_state_dict`（原始版）？

> **参考答案**：因为 EMA 权重更平滑、泛化更稳，是项目选定的**部署主线**（前置知识第四点）。验证与最终部署都应基于 EMA 那一份，才能和训练期「每轮验证走 deployable_ema」的口径一致（4.1.5 练习 2 的同一逻辑）。原始版只作对照与回滚用。

## 5. 综合实践

**实践目标**：把本讲三个模块串起来，完成一次「QAT 训练循环全景还原」——把 `QatProcessor` 构造、threshold 参数组的 ~100× 学习率、每轮 `convert_to_deployable` 与 `save_model` 的 quant_info 导出，整合成一段连贯的伪代码，并标注每一处改动在 `modifications.md` 里的出处。

**背景设定**：假设你已经按 u4-l1 跑过一次 PTQ 校准（得到 `calib/` 目录与初始 threshold），但 detection F1 掉得太多，决定转 QAT 把精度拉回来。请用伪代码还原 trainer 的 QAT 全流程。

**操作步骤**：

1. **写出 `_setup_train` 的 QAT 构造段**（4.1.4 已给模板）：包含两次 `deepcopy`、`__deepcopy__` 补丁、`QatProcessor(bitwidth=8)`、`trainable_model(calib_dir=...)` 四步，并标注出处 [modifications.md:14-19](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L14-L19)。
2. **写出 `build_optimizer` 的差异化学习率段**（4.2.4 已给模板）：按 `"threshold"` 名字分流、threshold 组用 `lr0*100`、两组塞进同一个 SGD（`momentum=0.9`），标注出处 [modifications.md:29-31](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L29-L31)，并与 [README.md:47-49](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L47-L49) 的 `lr0=0.005` 对读算出 threshold LR ≈ 0.5。
3. **写出「每轮 epoch」的三段**：(a) `train_one_epoch`（前向伪量化 + 反向更新权重与 threshold）；(b) `convert_to_deployable` 折叠出原始/EMA 两份 deployable model、同步 `ori_model(_ema)` 的 `state_dict`（出处 [modifications.md:21-23](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L21-L23)）；(c) `validate(deployable_ema)`。
4. **写出 `save_model` 段**：把 `qat_model_state_dict`/`qat_model_quant_info`/`qat_model_quant_info_test`/`qat_ema_state_dict`（及加载分支要用的 `qat_ema_quant_info`）写入 checkpoint，quant_info 取自上一步 `convert_to_deployable`（出处 [modifications.md:25-27](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L25-L27)）。
5. **画一张「参数流转图」**：标出 threshold 从「PTQ calib 初值 → `trainable_model` → 每 epoch 被 ~100× LR 更新 → `convert_to_deployable` 固化 → `save_model` 落盘 → 加载时 QAT 分支复活/PTQ 分支写 `quant_info.json`」的完整旅程。

**预期结果（参考答案要点）**：

- 全景伪代码应能体现「**两类参数、两种学习率**」「**两种模型形态、每轮折叠**」「**EMA 主线**」三条主线同时运行。
- threshold 的数值旅程清晰可见：初值来自 PTQ calib → 训练里以 ~100× 权重的步长搜索 → 折叠固化 → 随 checkpoint 成套存盘。
- 三条主线与本讲三个最小模块一一对应，可作为后续阅读真实 `pytorch_nndct` 代码时的「地图」。

> 说明：本实践产出的是一份**伪代码全景 + 出处标注图**，所有涉及 `QatProcessor`/`convert_to_deployable`/`__deepcopy__` 补丁的具体签名与实现，均依赖 Vitis AI 3.5 的 `pytorch_nndct`（本仓库不含），命令与代码的真实可运行性标注为「待本地（Vitis AI 环境）验证」。

## 6. 本讲小结

- **QAT 训练用 `trainable_model`、部署用 deployable model**：`QatProcessor` 把浮点模型改造成带「可训练 threshold 的伪量化模型」（`trainable_model`，负责前向+反向），需要评估/导出时再用 `convert_to_deployable` 折叠成真定点 deployable model——两种形态靠每 epoch 一次的折叠衔接。
- **QAT 骑在 PTQ 校准肩膀上**：`trainable_model(calib_dir=...)` 的 `calib_dir` 指向先前 PTQ 校准产物，threshold 初值即来源于此；所以 PTQ 与 QAT 命令的 `imgsz`、激活标志必须逐字一致（u1-l3 暗线）。
- **保留浮点基准 + `__deepcopy__` 补丁**：trainer 在改造前深拷贝出 `ori_model`/`ori_model_ema`，并覆写 `__deepcopy__` 以兼容 nndct 的自定义张量子类，保证「浮点基准深拷贝」与「QAT 改造」能共存。
- **threshold 参数组用 ~100× 学习率**：权重已近预训练最优、需小 LR 微调；threshold 是 QAT 新引入的量化范围参数、需大步搜索，故单独建组给约 100× LR。对照 README 的 `lr0=0.005`，threshold LR ≈ 0.5。
- **quant_info 是「成套存、分支读」的量化阈值表**：训练期由 `convert_to_deployable` 产出，随 checkpoint 成套保存（EMA 主线）；加载时 QAT 分支复活 `qat_ema_state_dict` 再折叠评估、PTQ 分支把 `qat_ema_quant_info` 落盘成 `quant_info.json` 交给 u4-l1 的 `quant_mode=test` 导出。
- **EMA 贯穿部署主线**：`ori_model_ema`、`qat_ema_state_dict`、`qat_ema_quant_info`、`deployable_ema`——凡带 `ema` 的，都是最终验证与部署的那一份，验证选优与部署同口径。

## 7. 下一步学习建议

本讲把 QAT 的「训练态构造、threshold 优化、quant_info 导出」三件事讲透了，`modifications.md` 里还剩两块接力点：

- **如果你想搞清 QAT 模型为什么还要改网络结构** → 进入 **u4-l3（激活函数替换与 DPU 算子适配）**：它会讲 `modifications.md` 编号 3 的 `modules.py` 改动——`C2f`/`SPPF`/`Detect` head 的改造、`torch.cat`→`pytorch_nndct.nn.modules.functional.cat`、每次 `cat`/`MaxPool2d` 都新建实例的原因。这些改造和本讲的「让量化器能干净追踪计算图」直接相关。
- **如果你想知道 QAT 产出的量化模型怎么变成 DPU 指令** → 进入 **u4-l4（模型导出与 DPU 编译）**：讲 `dump_xmodel`/`dump_onnx` 导出、`vai_c_xir` 用 `arch.json` 把 `xmodel` 编译成 KV260 DPU 专用指令、用 `xir subgraph` 定位输出层名填进 `prototxt`。本讲 4.3 PTQ 分支写出的 `quant_info.json` 正是在这一步被消费。

建议阅读顺序：u4-l3 → u4-l4。三者合起来把「量化」这一阶段从 QAT 训练推进到板载可执行。之后第五单元（u5）将转入硬件平台构建，把量化产出的模型真正部署到 KV260 上；本讲提到的「EMA deployable model → 导出 xmodel → 编译」链路，正是 u4-l4 与 u5 的交接点。
