# 激活函数替换与 DPU 算子适配

## 1. 本讲目标

本讲是第四单元「Vitis AI 量化」的第三篇，承接 u4-l1（量化基础与 PTQ 校准）和 u4-l2（QAT 实现要点）。前两讲解决了「为什么量化」「怎么校准/怎么 QAT」，本讲聚焦一个更底层的工程问题：**当 YOLOv8 的浮点模型被 nndct 解析成量化计算图、再编译给 KV260 的 DPU 跑时，为什么我们必须把某些 PyTorch 算子提前替换掉，否则模型根本无法高效（甚至无法正确）地在 DPU 上运行？**

读完本讲，你应该能够：

1. 说清 DPU 这种 int8 定点加速器**为什么偏爱分段线性激活**，并能推导 `sigmoid→hsigmoid`、`silu→hswish` 的数学关系与定点实现成本差异。
2. 解释 `--nndct_convert_sigmoid_to_hsigmoid` 与 `--nndct_convert_silu_to_hswish` 两个命令行标志的作用，以及为什么它们必须在 calib / 导出 / QAT 三类命令里**逐字保持一致**。
3. 理解 `modules.py` 中 `torch.cat → nndct cat`、为每个调用点新建 `MaxPool2d` 实例、以及对 `C2f / SPPF / Detect head` 的改造，是如何帮助 nndct **正确追踪量化计算图**的。

---

## 2. 前置知识

本讲默认你已经掌握 u4-l1 与 u4-l2 的内容，这里只做最小回顾：

- **DPU 是 int8 定点加速器**：KV260 上的 DPU（`DPUCZDX8G`）原生支持的运算是卷积、逐元素加/乘（eltwise add/mul）、ReLU/clamp（截断）这一类「加减乘 + 比较」的整数运算；它**不原生支持** `exp`、`log`、除法这类超越函数。
- **量化计算图（quantization graph）**：`pytorch_nndct`（nndct）会遍历你的 PyTorch `nn.Module`，把每个算子登记成图里的一个节点，并在节点边界插入「伪量化」（fake-quant，即 `quant→dequant` stub），用来在前向模拟 int8 误差、在校准时统计每层激活的动态范围（u4-l1 讲过的 `scale`/`threshold`）。
- **激活替换是「图改写」**：nndct 提供了一组命令行标志，在量化前**把模型里的某种激活函数替换成它的硬件友好近似**，使得改写后的算子能被 nndct 识别并最终编译成 DPU 指令。

一句话：本讲讨论的不是「量化精度」，而是「量化可行性」——把一个为 GPU/CPU 设计的浮点网络，改造成一个 DPU 能高效吃下去的定点网络。

> ⚠️ 关于源码可见性：受 YOLOv8（Ultralytics）许可证限制，本仓库**不包含** `modules.py` 的实际 Python 源码，只提供一份「改动清单」`software/quantization/modifications.md`。真正的 `modules.py` / `task.py` / `trainer.py` / `validator.py` 位于另一个需自行 clone 的 `ultralytics-vitis-ai` 仓库（见 [README.md:L9-L14](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L9-L14)）。因此本讲引用的「源码」主要是这两份 Markdown，涉及具体 Python 行为时会标注「待本地验证」。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
|---|---|---|
| [software/quantization/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md) | 量化全流程操作手册，含 PTQ/QAT/导出/编译命令 | 四条命令里反复出现的两个 `--nndct_convert_*` 标志 |
| [software/quantization/modifications.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md) | 相对上游 Ultralytics 的改动清单（许可证原因不附源码） | 第 3 项 `modules.py` 的四条改动（`C2f/SPPF/Detect head`、`cat`、`MaxPool2d`） |

本讲不进入 `task.py`/`trainer.py`/`validator.py` 的 QAT 逻辑（那是 u4-l2 的内容），只关心**激活函数与算子**这一层。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

- **4.1 DPU 硬件友好激活**——为什么要把 `sigmoid`/`silu` 换成 `hsigmoid`/`hswish`。
- **4.2 nndct 算子替换**——为什么要把 `torch.cat` 换成 nndct 的 `cat`。
- **4.3 模块量化改造**——为什么 `C2f/SPPF/Detect head` 要改、为什么 `MaxPool2d` 不能复用。

### 4.1 DPU 硬件友好激活

#### 4.1.1 概念说明

YOLOv8 的卷积块默认激活函数是 **SiLU**（也叫 Swish），检测头里还会用到 **sigmoid**。它们在浮点 GPU 上几乎免费，但在 int8 定点 DPU 上却是「贵客」：

\[ \text{silu}(x) = x \cdot \sigma(x), \qquad \sigma(x) = \frac{1}{1+e^{-x}} \]

两者都依赖 \( e^{-x} \)，而 **DPU 没有指数运算单元**。如果硬要让 DPU 算 `exp`，只有两条难路：

1. 用多项式/LUT（查找表）去近似 \( e^{-x} \)，精度低、占用资源、且 nndct 未必有对应指令；
2. 把含 `exp` 的那一小段子图**退回到 PS（ARM CPU）上用软件算**，再搬回 PL（DPU）——这种 PS↔PL 数据搬运的代价极高，往往抵消掉整个 DPU 加速收益。

社区（MobileNetV3 起）给出的标准解法是：用**分段线性（piecewise-linear）的「硬」近似**替换光滑激活，使其只用「加、乘、截断（ReLU/clamp）」这些 DPU 原生指令即可实现。这就是 `hsigmoid` 与 `hswish`：

\[ \text{hsigmoid}(x) = \frac{\text{ReLU}_6(x+3)}{6} = \text{clip}\!\left(\frac{x}{6}+\tfrac{1}{2},\; 0,\; 1\right) \]

\[ \text{hswish}(x) = x \cdot \text{hsigmoid}(x) = \frac{x\,\text{ReLU}_6(x+3)}{6} \]

其中 \(\text{ReLU}_6(y)=\min(\max(y,0),6)\) 就是「截断到 \([0,6]\)」。

`hsigmoid` 在三段上分别是常数/线性/常数：

| 区间 | hsigmoid(x) |
|---|---|
| \(x \le -3\) | \(0\) |
| \(-3 < x < 3\) | \((x+3)/6\)（线性，斜率 \(1/6\)）|
| \(x \ge 3\) | \(1\) |

这就意味着：**一次加常数 + 一次乘常数（\(1/6\)）+ 两次比较截断**就能算出来，全是 DPU 的拿手好戏。`hswish` 多了一次逐元素乘（`x ⊙ hsigmoid(x)`），但 DPU 的 eltwise-mul 同样是原生指令——整个过程**没有任何 `exp`**。

#### 4.1.2 核心流程

把「替换」放进量化全流程来看，它发生在**校准之前、模型加载之后**：

```text
yolo detect val/train ... nndct_quant=True --nndct_convert_sigmoid_to_hsigmoid --nndct_convert_silu_to_hswish
        │
        ▼
nndct 在构建量化图时，扫描 nn.Module 树，把：
   nn.Sigmoid  ──► 注册为 hsigmoid 算子节点（原生 DPU 指令）
   SiLU/Swish  ──► 注册为 hswish   算子节点（原生 DPU 指令）
        │
        ▼
图里不再有任何 exp 节点 → 校准统计的是 hsigmoid/hswish 的输出范围
        │
        ▼
导出 xmodel / 编译 → DPU 指令序列里全是 add/mul/clamp，无 CPU 回退
```

这里有一条**一致性铁律**（u4-l1 已强调，本讲给出它的算子层面解释）：`--nndct_convert_sigmoid_to_hsigmoid` 与 `--nndct_convert_silu_to_hswish` 这两个标志，必须在 **calib、export、QAT 三类命令里逐字保持一致**。原因是——校准时统计的 `threshold`（即 u4-l1 的 scale 来源）是针对 `hsigmoid/hswish` 的输出分布算出来的；如果校准时替换、导出时不替换（或反之），模型结构就与量化参数对不上，要么精度暴跌，要么干脆编译失败。

定点实现的额外好处可以从量化误差角度定量理解。设 int8 对称量化的单点误差上界为 \(s/2\)（\(s\) 为 scale），对光滑激活 \(g\) 有：

\[ |g(\hat{x}) - g(x)| \;\lesssim\; |g'(x)| \cdot \frac{s}{2} \]

- `sigmoid` 的最大导数为 \(\sigma'(0)=0.25\)；
- `hsigmoid` 的最大斜率仅 \(1/6 \approx 0.167\)。

即 `hsigmoid` 的「 Lipschitz 常数」更小，量化噪声经由它放大的程度也更小——这是硬件友好激活「顺带」带来的量化稳定性提升。

#### 4.1.3 源码精读

这两个标志在本仓库里**没有任何一处被省略**，四类量化命令全部带上它们，这本身就是「不可漏、必须一致」的强证据。

PTQ 校准命令（命令出现在 [software/quantization/README.md:L39-L41](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L39-L41)）：

```bash
yolo detect val data="xview3-vitis.yaml" model=<path-to-your-model.pt> \
  nndct_quant=True quant_mode=calib imgsz=800 \
  --nndct_convert_sigmoid_to_hsigmoid --nndct_convert_silu_to_hswish
```

QAT 训练命令同样带上（[software/quantization/README.md:L47-L49](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L47-L49)）：

```bash
yolo detect train cfg="qat.yaml" data="xview3-vitis.yaml" model=<...> imgsz=800 \
  epochs=100 optimizer=SGD momentum=0.9 lr0=0.005 warmup_epochs=0 \
  nndct_quant=True --nndct_convert_sigmoid_to_hsigmoid --nndct_convert_silu_to_hswish
```

导出 xmodel/ONNX（[software/quantization/README.md:L58-L60](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L58-L60)）和「只评估不导出」（[software/quantization/README.md:L64-L66](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L64-L66)）也一字不差地重复这两个标志。

**阅读这四段命令的方法**：注意 `nndct_quant=True` 与两个 `--nndct_convert_*` 是**两个层次**的开关——前者打开「整个量化流程」，后者是「图改写规则」。后者以前者为前提，没有 `nndct_quant=True` 时这两个 convert 标志不会有任何效果。

#### 4.1.4 代码实践

**实践目标**：亲手把 `sigmoid` 与 `hsigmoid`、`silu` 与 `hswish` 的曲线画在一起，直观看出后者为什么「更适合 int8 定点」。

**操作步骤**（示例代码，需要本机有 `numpy` 与 `matplotlib`）：

```python
# 示例代码：本仓库不含此脚本，请自行创建 compare_activations.py 运行
import numpy as np
import matplotlib.pyplot as plt

x = np.linspace(-6, 6, 401)

def sigmoid(x):       return 1 / (1 + np.exp(-x))
def hsigmoid(x):      return np.clip(x / 6 + 0.5, 0.0, 1.0)   # = ReLU6(x+3)/6
def silu(x):          return x * sigmoid(x)
def hswish(x):        return x * hsigmoid(x)

fig, ax = plt.subplots(1, 2, figsize=(11, 4))
ax[0].plot(x, sigmoid(x),  label="sigmoid")
ax[0].plot(x, hsigmoid(x), label="hsigmoid", linestyle="--")
ax[0].set_title("sigmoid vs hsigmoid"); ax[0].legend(); ax[0].grid(True)

ax[1].plot(x, silu(x),  label="silu (swish)")
ax[1].plot(x, hswish(x), label="hswish", linestyle="--")
ax[1].set_title("silu vs hswish"); ax[1].legend(); ax[1].grid(True)
plt.savefig("activation_compare.png", dpi=110)
```

**需要观察的现象**：

1. `hsigmoid` 在 \([-3,3]\) 之外是**水平直线**（恒 0 或恒 1），`sigmoid` 则是处处弯曲的光滑 S 形——水平段意味着定点实现里只需一次比较就「短路」返回常量。
2. `hswish` 与 `silu` 在 \(x \ge 3\) 后几乎重合（都趋近于 \(y=x\)），差异集中在 \([-3,3]\) 的过渡区，说明替换引入的近似误差是有界的、且主要落在小数值区。
3. 两条硬曲线都只由「直线段」组成（`hswish` 中段是二次，但仍是 `x ⊙ 线性` 的乘积），没有任何 \(e^{-x}\) 的形状。

**预期结果**：你会看到硬激活曲线是「折线」形态，光滑激活是「弯曲线」形态。这正是「分段线性 vs 超越函数」的视觉差异，也对应着 DPU 上「add/mul/clamp vs 多项式/LUT」的实现成本差异。

> 渲染出的 `activation_compare.png` 具体长相「待本地验证」（取决于你的 matplotlib 版本与字体），但上面三点观察是确定的。

**对照思考**（回答实践任务的核心提问）：`hsigmoid` 更适合 int8 定点，原因是——

- 它的每一段（含两端常数段）都只需 **加、乘、比较截断**，恰好是 DPU 的原生指令集；
- 它的输出严格落在 \([0,1]\)、斜率上界 \(1/6\)，**动态范围小且 Lipschitz 常数小**，校准时容易确定 `threshold`、量化误差放大也更小；
- 它**不含 `exp`**，因此不会触发 DPU 子图向 PS 的回退（避免昂贵的 PS↔PL 搬运）。

#### 4.1.5 小练习与答案

**练习 1**：计算 `sigmoid(0)`、`hsigmoid(0)`、`sigmoid(3)`、`hsigmoid(3)`，验证两个函数在「锚点」上是否吻合。

答：\(\sigma(0)=0.5\)；\(\text{hsigmoid}(0)=0/6+0.5=0.5\)，吻合。\(\sigma(3)=1/(1+e^{-3})\approx0.953\)；\(\text{hsigmoid}(3)=\text{clip}(3/6+0.5,0,1)=1.0\)。可见在 \(x=3\) 处开始进入「饱和水平段」，这正是硬近似偏离光滑原型的位置。

**练习 2**：如果把 `--nndct_convert_silu_to_hswish` 加在了 QAT 训练命令里、却忘了加在导出命令（`dump_xmodel=True`）里，预测会发生什么？

答：训练时模型按 `hswish` 学习权重与 `threshold`，导出时却按 `silu` 重建计算图，二者算子不一致。轻则导出的 `threshold`（针对 `hswish` 分布）套到 `silu` 上导致精度大幅下降，重则 nndct 报算子不匹配或导出失败。这正是「两个标志必须三类命令逐字一致」的实操含义。

**练习 3**：为什么 `hswish` 的中段是二次函数（\(x(x+3)/6\)），却仍被算作「硬件友好」？

答：因为它等于 `x ⊙ hsigmoid(x)`，即「一次逐元素乘」组合「一个分段线性函数」。逐元素乘是 DPU 原生 eltwise-mul 指令，分段线性部分又是 add/mul/clamp，全程不含 `exp`。硬件友好≠「纯线性」，而是「能用原生定点指令组合出来」。

---

### 4.2 nndct 算子替换

#### 4.2.1 概念说明

第二个模块处理的是**图追踪**问题，而非函数本身的复杂度。

`torch.cat`（张量拼接）本身计算极其简单，但它是 **functional 调用**——你直接写 `torch.cat([a, b], dim=1)`，它不是 `nn.Module` 的一个实例。nndct 在构建量化图时，主要靠遍历「模块树」来登记节点；对于这种「散落在 `forward` 里的 functional 调用」，它需要额外的识别规则才能正确地把它登记成一个可量化节点、并在其输入/输出边界插对 quant/dequant stub。

如果 nndct 的版本对某个 functional 调用「不认识」，可能出现的后果包括：

- 该拼接处**不插入量化节点**，前后两段的量化域对不上，精度异常；
- 或干脆**断图**，把一部分算子划到一个 DPU 子图、另一部分划到另一个，中间靠 PS 桥接，性能塌陷；
- 或在量化阶段直接报「unsupported op」。

`pytorch_nndct` 因此自带了一份**经过验证、可被图追踪器识别**的算子实现，其中就包括 `pytorch_nndct.nn.modules.functional.cat`。把它换上去，等于是告诉 nndct：「这是一个你认识、且明确知道怎么量化的拼接算子」。

#### 4.2.2 核心流程

```text
原生 YOLOv8 的 forward 里：
    out = torch.cat([branch1, branch2], dim=1)        # functional，nndct 可能不追踪

改造后：
    from pytorch_nndct.nn.modules.functional import cat
    self.my_cat = cat(dim=1)                          # 构造一个 nndct 认识的模块实例
    ...
    out = self.my_cat([branch1, branch2])             # 作为模块调用 → 图里是一个被追踪的节点
```

注意这里有一个**模式转变**：从「函数式调用」变成了「模块实例调用」。这也引出 modifications.md 里紧接着的一条要求——「为每个拼接操作新建一个 cat 对象，而不是复用」（见 4.3.2 详述，同一份 `modules.py` 改动清单里）。

#### 4.2.3 源码精读

改动清单在 [software/quantization/modifications.md:L36-L38](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L36-L38)：

> - Modify `torch.cat` to `pytorch_nndct.nn.modules.functional.cat`
> - Initializing a cat object for each operation instead of reusing

注意两点：

1. 这条改动写在 **`modules.py`**（即 Ultralytics 的层/模块定义文件）项下，说明替换发生在**网络结构定义层**，而不是训练脚本里——即改的是「YOLOv8 的构建块」本身。
2. 紧跟其后的「为每个操作新建 cat 对象」说明：替换不仅是「换个函数名」，还要把它**实例化为模块**，并且每个调用点一个实例。这两条是配套的，缺一不可。

YOLOv8 里大量用到拼接：`C2f` 模块内拼接两条分支、neck 的 FPN/PAN 在每个尺度上拼接上采样与跨层特征。这些拼接点是量化图里的关键节点，必须确保 nndct 都能正确登记。

> 具体的 Python 代码不在本仓库（许可证原因），「`cat` 类的构造签名与调用方式」待在 `ultralytics-vitis-ai` 仓库中本地确认。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读，理解「functional 调用 vs 模块实例调用」对图追踪的影响。

**操作步骤**：

1. 在 `ultralytics-vitis-ai` 仓库中，用搜索定位 `C2f` 模块的 `forward`，找到所有 `torch.cat(...)`（或已替换后的 `self.*cat(...)`）调用点，数一下一个 `C2f` 里大约有几处拼接。
2. 对照 [modifications.md:L36-L38](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L36-L38)，确认每一处拼接是否都对应一个**独立的** `cat` 实例（而非共享同一个 `self.cat`）。

**需要观察的现象**：你会看到 `C2f` 内部（尤其 `forward` 里把 `x` 与各 `Bottleneck` 输出拼起来的地方）每一处拼接，都由一个单独命名的 `cat` 模块实例承担。

**预期结果**：拼接点数量 = `cat` 实例数量，一一对应。这验证了「不复用、每点一个实例」的改造。

> 若无法访问 `ultralytics-vitis-ai` 仓库，本步骤标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 nndct 对「`nn.Conv2d` 这种模块」天然能追踪，却对「`torch.cat` 这种 functional」需要特别处理？

答：`nn.Conv2d` 是 `nn.Module` 子类的实例，会被注册进模块树，nndct 遍历树时自然遇见它并登记节点。`torch.cat` 是普通函数，不出现在模块树里，nndct 必须靠专门的「functional 识别规则」才能在 `forward` 的调用流里捕获它；规则覆盖不到就会漏追踪。把 `cat` 包装成模块实例，就是把它「拉回」模块树里。

**练习 2**：如果某个拼接点没被 nndct 追踪到，最直接的危害是什么？

答：该点两侧的张量可能处于「一个量化域 vs 另一个量化域」的不一致状态（一边是 int8 伪量化、一边是浮点），拼接后数值尺度错配，导致后续层输入分布异常、精度下降；极端情况下会断图，触发 PS 回退。

---

### 4.3 模块量化改造

#### 4.3.1 概念说明

第三个模块综合处理 `C2f`、`SPPF`、`Detect head` 以及 `MaxPool2d` 的实例化方式。它们共享同一个主题：**让 YOLOv8 的核心构建块在「被 nndct 追踪 + 被 DPU 编译」这两个阶段都保持稳定**。

先说 `MaxPool2d`。`nn.MaxPool2d` 本身**没有可学习参数**，看起来「复用一个实例到处调用」很自然——比如 SPPF 里把同一个 5×5 最大池化连续套用多次。但问题不在参数，而在**图追踪的节点身份**：当同一个模块实例在网络里被调用多次时，图追踪器要在计算图里**为每一次调用生成一个独立节点**，并各自维护独立的量化参数。复用实例会让追踪器在「这是同一个节点被复用，还是多个节点」之间产生歧义，轻则节点合并错误、量化参数错配，重则追踪失败。

`C2f`、`SPPF`、`Detect head` 的改造动机则更综合，至少包含两类：

- **算子可追踪性**：如 4.2 所述，把内部的 `torch.cat` 等换成 nndct 认识的版本，让整个模块对 nndct「透明」。
- **激活/输出域可控**：检测头（Detect head）涉及 DFL（Distribution Focal Loss）解码，里面有 `softmax`（又是 \(e^x/\sum e^x\)），对量化敏感；改造方向是让头的结构在 int8 下数值稳定、输出层范围明确，便于校准和后续（u4-l4）用 `xir subgraph` 定位输出层名。

> 受许可证限制，`C2f/SPPF/Detect head` 改造的**具体代码行**不在本仓库，[modifications.md:L34](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L34) 只用一行 "Modifications to `C2f`, `SPPF`, `Detect` head" 概括。下面讲到具体「改了哪行」时均标注「待本地验证」。

#### 4.3.2 核心流程

把 `modules.py` 的四条改动（一份清单，见 [modifications.md:L33-L39](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L33-L39)）按「改什么 / 为什么」整理：

| 改动（modifications.md）| 改什么 | 为什么 |
|---|---|---|
| L34 | `C2f` / `SPPF` / `Detect` head | 让这几个核心块对 nndct 透明、激活与输出域在 int8 下稳定（具体待本地验证）|
| L36 | `torch.cat` → `pytorch_nndct.nn.modules.functional.cat` | 拼接成为可追踪、可量化节点（见 4.2）|
| L37 | 每个 `cat` 操作新建一个对象，不复用 | 每个拼接点在图里是独立节点，各自维护量化参数 |
| L39 | 每个 `MaxPool2d` 操作新建对象，不复用 | 每次池化在图里是独立节点，避免追踪器节点身份歧义 |

一个关键直觉：**「不复用、每点一个实例」本质上是把「函数式/共享」风格，改成「显式、可枚举」风格**。后者对量化图追踪器最友好，因为每个节点都是模块树里一个独一无二、可枚举的叶子。

#### 4.3.3 源码精读

`modules.py` 整段改动清单见 [software/quantization/modifications.md:L33-L39](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L33-L39)：

> 3. **`modules.py`:**
>     - Modifications to `C2f`, `SPPF`, `Detect` head
>     - Modify `torch.cat` to `pytorch_nndct.nn.modules.functional.cat`
>     - Initializing a cat object for each operation instead of reusing
>     - Initialize `nn.MaxPool2d` objects for each operation instead of reusing

阅读这段清单时，建议这样分组理解：

- **算子替换组**：L36（`cat` 换 nndct 版）——解决「nndct 不认识」。
- **实例化组**：L37（`cat` 每点新建）、L39（`MaxPool2d` 每点新建）——解决「图里节点身份歧义」。
- **模块改造组**：L34（`C2f/SPPF/Detect head`）——解决「整块结构在 int8 下的稳定性」，是范围最大、许可证未公开细节的一项。

把 L37 和 L39 放在一起看尤其重要：它们是同一种模式（「不复用、每点一个实例」）应用在两种无参数模块（`cat`、`MaxPool2d`）上。这告诉我们，**无参数≠可随意复用**——只要它出现在计算图的关键路径上，复用就会给量化图追踪添麻烦。

> SPPF 是这条改动最典型的落点：原生 SPPF 用同一个 `MaxPool2d` 实例连续池化三次，再拼接。改造后每一次池化都是独立的 `MaxPool2d` 实例、每一次拼接都是独立的 `cat` 实例，使整段在量化图里展开成清晰可枚举的节点序列（具体实现待本地验证）。

#### 4.3.4 代码实践

**实践目标**：用一段最小 PyTorch 代码，直观对比「复用 MaxPool2d 实例」与「每次新建实例」在 `forward` 调用结构上的差异，体会图追踪器看到的是「同一个对象三次」还是「三个对象各一次」。

**操作步骤**（示例代码，本仓库不含此脚本）：

```python
# 示例代码：对比 SPPF 风格的两种写法
import torch
import torch.nn as nn

class SPPF_reuse(nn.Module):
    """原生风格：复用同一个 MaxPool2d 实例"""
    def __init__(self):
        super().__init__()
        self.pool = nn.MaxPool2d(kernel_size=5, stride=1, padding=2)  # 只有一个实例
    def forward(self, x):
        y1 = self.pool(x)
        y2 = self.pool(y1)
        y3 = self.pool(y2)
        return y3

class SPPF_new(nn.Module):
    """量化友好风格：每次池化一个独立实例"""
    def __init__(self):
        super().__init__()
        self.pool1 = nn.MaxPool2d(kernel_size=5, stride=1, padding=2)
        self.pool2 = nn.MaxPool2d(kernel_size=5, stride=1, padding=2)
        self.pool3 = nn.MaxPool2d(kernel_size=5, stride=1, padding=2)
    def forward(self, x):
        y1 = self.pool1(x)
        y2 = self.pool2(y1)
        y3 = self.pool3(y2)
        return y3

m_reuse, m_new = SPPF_reuse(), SPPF_new()
print("reuse 子模块数:", len(dict(m_reuse.named_children())))   # 预期 1
print("new   子模块数:", len(dict(m_new.named_children())))     # 预期 3
```

**需要观察的现象**：

- `SPPF_reuse` 的 `named_children()` 只有 **1** 个子模块（`pool`），但 `forward` 里它被调用了 **3** 次——图追踪器需要「凭调用上下文」区分这三次。
- `SPPF_new` 有 **3** 个子模块（`pool1/pool2/pool3`），每次调用对应一个独一无二的模块实例——图追踪器可以直接枚举。

**预期结果**：打印出 `reuse 子模块数: 1` 与 `new 子模块数: 3`。这就是 [modifications.md:L39](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L39)「Initialize `nn.MaxPool2d` objects for each operation instead of reusing」在结构上的可视化。

#### 4.3.5 小练习与答案

**练习 1**：`nn.MaxPool2d` 没有可学习参数，为什么还要为每次调用新建实例？

答：因为量化图追踪关心的是「计算图节点」而非「参数」。复用同一实例时，追踪器要在图里把「同一对象的三次调用」拆成三个独立节点并各自分配量化参数，这容易产生节点身份与量化域的歧义；每次新建实例则让每个节点在模块树里独一无二，追踪无歧义。

**练习 2**：`C2f/SPPF/Detect head` 的改造与 4.1 的激活替换、4.2 的 `cat` 替换，三者各自解决量化链路的哪一段问题？

答：4.1（激活替换）解决「**算子能否被 DPU 定点单元执行**」（避免 `exp`）；4.2（`cat` 替换）解决「**算子能否被 nndct 图追踪器识别**」（functional→模块）；4.3（`C2f/SPPF/Detect head` 与实例化）解决「**整块结构与节点身份在量化图里是否稳定、可枚举**」。三者合起来，把一个为浮点 GPU 设计的网络，改造成「可追踪 → 可校准 → 可定点编译」的 DPU 友好网络。

**练习 3**：如果只做 4.1 的激活替换，却不做 4.3 的 `MaxPool2d` 实例化改造，量化还能跑通吗？

答：很可能跑不通或精度异常。激活替换只保证「单个激活算子」定点友好，但 SPPF 等模块里若复用 `MaxPool2d`，图追踪/量化参数分配仍可能在「同一对象多次调用」上出错，导致整条链路在量化图层面断裂或错配。四条改动是配套的，共同保证整网可量化。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「**给 DPU 友好化清单打分**」的源码审阅实践。

**任务**：假设你刚拿到 `ultralytics-vitis-ai` 仓库的 `modules.py`，请对照本讲学到的「DPU 友好化」三条原则，给这个文件做一次量化友好性体检，输出一份检查表。

**操作步骤**：

1. **激活体检**（对应 4.1）：在 `modules.py` 及其依赖里搜索 `SiLU`、`Sigmoid`、`silu`、`sigmoid`，确认默认卷积块的激活是否仍为 SiLU（这是正常的——替换由命令行标志在量化时完成，而非改源码）。然后思考：如果有人误删了 `--nndct_convert_silu_to_hswish`，哪个模块最先暴露问题？（答：所有 `Conv` 块，因为 YOLOv8 的默认激活就是 SiLU。）
2. **拼接体检**（对应 4.2）：搜索 `torch.cat`，确认 `C2f`、neck、`SPPF` 内的拼接是否已改用 `pytorch_nndct.nn.modules.functional.cat`，且是否每个拼接点对应一个独立实例。
3. **实例化体检**（对应 4.3）：在 `SPPF` 的 `__init__` 里数 `MaxPool2d` 实例个数，确认等于其 `forward` 里的池化调用次数（原生 SPPF 是 3 次池化 → 应有 3 个独立实例）。
4. **命令一致性体检**（对应 4.1.2 的铁律）：打开 [software/quantization/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md)，核对 PTQ（[L39-L41](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L39-L41)）、QAT（[L47-L49](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L47-L49)）、导出（[L58-L60](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L58-L60)）、评估（[L64-L66](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L64-L66)）四条命令的两个 `--nndct_convert_*` 标志是否**完全一致**（预期：完全一致）。

**预期结果**：你应得到一张形如下表的体检单（其中「源码侧」项依赖 `ultralytics-vitis-ai` 仓库，标注「待本地验证」；「命令侧」项可在本仓库直接核对）：

| 体检项 | 原则 | 期望状态 | 在本仓库可核对？ |
|---|---|---|---|
| 四条命令的两个 convert 标志 | 一致性铁律 | 完全一致 | ✅ 可 |
| `cat` 改为 nndct 版 + 每点一实例 | 图追踪 | 是 | ⚠️ 待本地验证 |
| `MaxPool2d` 每点一实例 | 节点身份 | 是 | ⚠️ 待本地验证 |
| `C2f/SPPF/Detect head` 已改造 | 整块稳定 | 是 | ⚠️ 待本地验证 |

> 这份综合实践把「读 Markdown 改动清单」与「读真实 Python 源码」分成两条可分别执行的线：前者在本仓库内即可完成，后者需要在 `ultralytics-vitis-ai` 仓库中进行。

---

## 6. 本讲小结

- **DPU 没有 `exp` 单元**：`sigmoid`/`silu` 依赖指数，必须换成分段线性的 `hsigmoid`/`hswish`，后者只用 add/mul/clamp——DPU 的原生指令。
- **两个标志贯穿全程**：`--nndct_convert_sigmoid_to_hsigmoid` 与 `--nndct_convert_silu_to_hswish` 必须在 calib、QAT、导出、评估四类命令里**逐字一致**，否则量化参数与模型结构错配。
- **硬激活还顺带更稳定**：`hsigmoid` 斜率上界 \(1/6 < 0.25\)，Lipschitz 常数更小，量化误差经由它放大得更少。
- **`torch.cat` 要换成 nndct 的 `cat`**：把 functional 调用变成 nndct 可追踪的模块节点，避免拼接处断图或量化域错配。
- **无参数模块也别复用**：`cat`、`MaxPool2d` 都要「每点一个实例」，让计算图里每个节点身份独一无二、可枚举。
- **三条原则分工**：4.1 管「算子能否定点执行」，4.2 管「算子能否被追踪」，4.3 管「整块结构与节点身份是否稳定」——合起来把浮点网络改造为 DPU 友好。

---

## 7. 下一步学习建议

- 本讲结束后，量化侧的「算子适配」已讲清。下一篇 **u4-l4 模型导出与 DPU 编译** 会把改好的量化模型导出为 `xmodel`/ONNX，并用 `vai_c_xir` 配合 `arch.json` 编译成 KV260 上 DPU 可执行的 `.xmodel`——届时你会看到本讲改造的算子最终如何落成 DPU 指令。
- 想立刻验证本讲直觉的读者，可先跑通 4.1.4 的曲线对比脚本，再带着「硬激活是折线、软激活是弯曲线」的印象去读 u4-l4 的编译产物。
- 对「DPU 到底支持哪些激活、unsupported op 如何导致子图回退到 PS」感兴趣的读者，建议补充阅读 Xilinx Vitis AI 文档中关于 DPU 指令集与子图划分（subgraph partitioning）的章节，这与本讲的「避免 PS↔PL 搬运」直接相关。
