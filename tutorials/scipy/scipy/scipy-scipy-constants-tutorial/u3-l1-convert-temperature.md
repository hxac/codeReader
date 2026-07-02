# 非线性温度转换 convert_temperature

## 1. 本讲目标

学完本讲，你应该能够：

- 解释**为什么温度换算不能像 `mile → m` 那样只乘一个因子**，而必须用一个专门的函数。
- 读懂 `_constants.py` 中 `convert_temperature` 的「**先把任意温标统一到 Kelvin，再从 Kelvin 转到目标温标**」的中转（hub）设计。
- 复述 Celsius / Kelvin / Fahrenheit / Rankine 四个温标之间的换算公式，并能解释其中的零点偏移与刻度比。
- 看懂 `@xp_capabilities()` 装饰器如何让一个函数同时接受 NumPy 数组、Python 列表以及其它数组库的输入。
- 理解当传入不支持温标时，函数如何抛出 `NotImplementedError`，以及要新增一个温标需要改哪两处。

本讲是 u2（CODATA 数据库）之后的专家层讲义，但只依赖一个事实：`zero_Celsius`、`degree_Fahrenheit` 是 `_constants.py` 里已定义的温度相关常量。本讲不再重复 CODATA 内容，而是聚焦「温度这种非线性物理量在 constants 子包里是怎么被处理的」。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 线性换算 vs 非线性换算

在前面的讲义（u1-l4）里我们看到，绝大多数单位换算都是**线性**的：把某单位换算到 SI 基本单位，只需要乘一个常数因子。例如 `inch = 0.0254`，于是 `10 * inch` 就是 0.254 米。用公式描述就是：

\[ y = a \cdot x \]

这里 \(a\) 是换算因子，\(x\) 是原始读数，\(y\) 是 SI 值。关键在于：**当原始读数为 0 时，SI 值也是 0**。原点对齐，所以一个乘数就够了。

温度换算不满足这一点。摄氏度 0 °C 对应的开尔文并不是 0，而是 273.15 K；华氏度 0 °F 对应的也不是 0 °C。两个温标的**零点不一致**，而且**每一度的「大小」（刻度比）也可能不同**。这种「先平移零点、再缩放刻度」的变换在数学上叫**仿射变换（affine transformation）**：

\[ y = a \cdot x + b \]

多出来的常数项 \(b\) 就是零点偏移。只要 \(b \neq 0\)，就没办法用一个乘数搞定，必须用函数。

### 2.2 温度的两个锚点

要把任意温标相互换算，最稳的办法是选定一个「绝对零点」作为中转站。Kelvin（开尔文）的 0 是绝对零度（分子热运动的理论下限），是天然的「真零点」，所以 SciPy 选 Kelvin 做中转：

\[ \text{任意温标} \xrightarrow{\text{第一步}} \text{Kelvin} \xrightarrow{\text{第二步}} \text{目标温标} \]

这样 \(N\) 个温标之间的两两换算，不必写 \(N^2\) 个公式，只需要写 \(2N\) 个（\(N\) 个「→Kelvin」加 \(N\) 个「Kelvin→」）。对 4 个温标，就是 \(4+4=8\) 条公式，而不是 \(4\times4=16\) 条。这就是「中转设计」的全部价值。

> 术语提示：本讲里「温标（scale）」指一套温度刻度系统，如摄氏温标、华氏温标等；「刻度比」指同一温度差在两个温标上的读数之比（如 1 °C 的温差 = 1.8 °F 的温差）。

## 3. 本讲源码地图

本讲几乎只围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [_constants.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py) | constants 子包的私有实现，包含数学常数、SI/二进制前缀、单位换算因子，以及本讲的主角 `convert_temperature`（第 228–302 行）。 |
| [_constants.py#L203-L205](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L203-L205) | 温度相关常量 `zero_Celsius`、`degree_Fahrenheit` 的定义，`convert_temperature` 会复用其中的 `zero_Celsius`。 |
| [tests/test_constants.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py) | `TestConvertTemperature` 用大量断言锁定了四温标两两换算的正确值，是本讲代码实践的依据。 |
| [scipy/_lib/_array_api.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py) | `@xp_capabilities` 装饰器的定义（第 839 行起），`convert_temperature` 用它声明自己对各数组后端的支持范围。该文件在 constants 子包之外，本讲只引用、不深入。 |

---

## 4. 核心概念与源码讲解

### 4.1 温度为何是 constants 里「唯一的非线性换算」

#### 4.1.1 概念说明

打开 `_constants.py`，你会看到上百行形如 `inch = 0.0254`、`mile = 1760 * yard` 的换算因子定义——它们都是线性换算。而在所有这些常量之后，源码专门留了一行注释，把后面的函数单独隔开：

[_constants.py#L225](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L225) —— 注释 `# functions for conversions that are not linear`，明确告诉你：从这里开始的函数，处理的都是「乘一个因子搞不定」的换算。

在当前版本的 constants 子包里，`convert_temperature` 是这行注释之下**唯一**针对「物理量换算」的非线性函数（紧随其后的 `lambda2nu` / `nu2lambda` 是光学波长与频率的换算，本质是除法 \(c/\lambda\)，并非单位换算）。也就是说：**温度是 constants 内置的唯一需要「平移零点」的物理量**。

为什么只有温度特殊？因为长度、质量、时间、能量……这些量的 SI 基准零点都和日常零点重合（0 米就是 0 米，0 焦耳就是 0 焦耳），唯独人造温标（摄氏、华氏）的零点是人为选定的（水的冰点、盐水冰点等），与热力学绝对零点错开了。

#### 4.1.2 核心流程

温度换算的数学本质是仿射变换：

\[ T_{\text{new}} = a \cdot T_{\text{old}} + b \]

其中 \(a\) 是刻度比，\(b\) 是零点偏移。对四个常见温标，以水的冰点（0 °C = 32 °F = 273.15 K = 491.67 °R）为锚点，可以列出它们的「刻度」关系：

- 1 °C 的温差 = 1 K 的温差（刻度比 1）。
- 1 °C 的温差 = 1.8 °F 的温差（华氏度更「密」，刻度比 9/5）。
- 1 °R 的温差 = 1 °F 的温差（Rankine 和 Fahrenheit 刻度相同，只是零点在绝对零度）。
- 1 °R 的温差 = 5/9 K 的温差。

这些刻度比和零点偏移组合起来，就得到后面四组公式。

#### 4.1.3 源码精读

定义温度常量的位置：

[_constants.py#L203-L205](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L203-L205) —— 定义 `zero_Celsius = 273.15`（摄氏零点对应的开尔文值，即水的冰点）与 `degree_Fahrenheit = 1/1.8`（华氏度相对摄氏度的刻度比的倒数，注释 `# only for differences` 提醒它只能用于「温度差」，不能用于带零点的温度本身）。

随后是那条分界注释：

[_constants.py#L225](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L225) —— 用注释把「线性换算因子」和「非线性换算函数」隔开，`convert_temperature` 紧随其后。

#### 4.1.4 代码实践

**目标**：亲手验证「温度不能只靠乘因子换算」。

**操作步骤**：

1. 在 Python 里尝试用一个乘数把摄氏度「换算」成开尔文，观察错误。
2. 再用正确的「加 273.15」方式，对照结果。

```python
# 示例代码（非项目原有代码，供理解用）
import scipy.constants as sc

c_val = 0  # 0 摄氏度
# 错误做法：只乘因子
print("错误做法 0*C * k:", c_val * 1.0)        # 得到 0，但真实是 273.15 K
# 正确做法：用专门函数
print("正确做法 convert_temperature:", sc.convert_temperature(c_val, 'Celsius', 'Kelvin'))
```

**需要观察的现象**：错误做法得到 0，与真实值 273.15 K 完全不符——这正是零点偏移造成的。

**预期结果**：`convert_temperature(0, 'Celsius', 'Kelvin')` 返回 `273.15`。

> 待本地验证：若你的环境未安装 SciPy，可先 `pip install scipy` 再运行。

#### 4.1.5 小练习与答案

**练习 1**：水的沸点是 100 °C，等于多少 °F？请先手算，再用 `convert_temperature` 验证。

**答案**：先转 K：100 + 273.15 = 373.15 K；再转 F：(373.15 − 273.15) × 9/5 + 32 = 212 °F。验证：`sc.convert_temperature(100, 'Celsius', 'fahrenheit')` 应返回 `212.0`。

**练习 2**：为什么 `degree_Fahrenheit` 的注释写 `# only for differences`？

**答案**：因为 `degree_Fahrenheit = 1/1.8` 只表示「华氏度刻度与摄氏度刻度的大小比」，不含零点信息。它能换算「温度差」（如某物体升高 1 °C 等于升高 1.8 °F），但换算「温度本身」时还需要补上 32 °F 的零点偏移，所以不能单独用于温度值。

---

### 4.2 函数签名与 @xp_capabilities 装饰器

#### 4.2.1 概念说明

`convert_temperature` 的对外接口非常简洁：传一个值（或一组值）、说明它现在是哪个温标、想换成哪个温标，函数返回换算结果。

值得专门讲的是它头顶的 `@xp_capabilities()` 装饰器。SciPy 正在推进 **Array API 标准**：让同一套函数既能吃 NumPy 数组，也能吃 PyTorch、JAX、CuPy 等其它符合标准的数组库。`@xp_capabilities()` 就是用来给一个函数**登记「我支持哪些数组后端」**的元数据标签。它本身**不改变函数的运行逻辑**，主要做两件事（详见装饰器源码注释）：

1. 把函数的「能力清单」登记进一张表，供测试框架 `@make_xp_test_case` 读取，自动生成跳过/预期失败标记。
2. 往函数 docstring 里追加一张「已测试后端」表格。

因此 `convert_temperature` 加上 `@xp_capabilities()`，等于声明：这个函数已经准备好接受任意 Array API 数组库的输入。

#### 4.2.2 核心流程

函数体的开头两行，是「Array API 适配」的固定套路：

1. `xp = array_namespace(val)`：探测输入 `val` 来自哪个数组库（NumPy、PyTorch……），把这个库的命名空间赋给 `xp`。
2. `_val = _asarray(val, xp=xp, subok=True)`：把输入统一变成该库的数组（`subok=True` 表示对 NumPy 输入保留子类，行为类似 `np.asanyarray`）。

之后所有的加减乘除都作用在 `_val` 这个数组上，运算符（`+`、`-`、`*`、`/`）由对应数组库实现，所以同一段代码能在不同后端上跑。返回值 `res` 的类型会随输入变化：传 NumPy 数组返回 NumPy 数组，传 Python 列表则（经 `_asarray`）按 NumPy 处理。

#### 4.2.3 源码精读

函数签名与装饰器：

[_constants.py#L228-L233](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L228-L233) —— `@xp_capabilities()` 装饰 `convert_temperature(val, old_scale, new_scale)`，返回类型标注为 `Any`（因为具体类型取决于输入的数组库）。

Array API 适配的入口：

[_constants.py#L273-L274](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L273-L274) —— `xp = array_namespace(val)` 探测数组库，`_val = _asarray(val, xp=xp, subok=True)` 规范化输入，这是后续所有运算的统一入口。

装饰器本身的定义在 constants 子包之外：

[_array_api.py#L839-L880](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L839-L880) —— `xp_capabilities` 装饰器，docstring 说明它的两个效果（支持 `@make_xp_test_case` 测试标记 + 自动追加能力说明表）。本讲只用到它「不传参数」的默认形态，即默认支持全部后端。

#### 4.2.4 代码实践

**目标**：观察 `convert_temperature` 对不同输入类型返回不同类型，并查看 docstring 里被装饰器注入的「能力表」。

**操作步骤**：

1. 分别用 NumPy 数组、Python 列表、标量调用函数，打印返回类型。
2. 打印 docstring，找一张被 `@xp_capabilities` 自动追加的表格。

```python
# 示例代码
import numpy as np
import scipy.constants as sc

print("numpy 输入 ->", type(sc.convert_temperature(np.array([0., 100.]), 'C', 'K')))
print("list 输入  ->", type(sc.convert_temperature([0., 100.], 'C', 'K')))
print("标量输入   ->", type(sc.convert_temperature(0., 'C', 'K')))
# 查看被装饰器追加的能力说明
print(sc.convert_temperature.__doc__[-400:])
```

**需要观察的现象**：NumPy 输入返回 `numpy.ndarray`；列表输入（经 `_asarray`）也返回 NumPy 数组；标量输入返回 NumPy 标量类型。docstring 末尾应有一段关于数组后端支持的说明（由 `@xp_capabilities` 注入）。

**预期结果**：返回类型随输入的数组库变化，证明函数是「数组库无关」的。

> 待本地验证：若环境中没有 NumPy，`import numpy` 会失败，需先安装。

#### 4.2.5 小练习与答案

**练习 1**：`@xp_capabilities()` 没有传任何参数，这代表什么？

**答案**：代表使用默认能力表——函数声明支持所有标准 Array API 后端（不主动跳过或预期失败任何后端）。需要限制时，可以传 `skip_backends=`、`xfail_backends=`、`np_only=True` 等参数。

**练习 2**：如果把 `array_namespace(val)` 和 `_asarray(...)` 这两行删掉，直接用 `val` 参与运算，函数对哪些输入仍然能工作？

**答案**：对 NumPy 数组和 Python 列表/标量仍能工作（因为 Python 运算符和 NumPy 都支持 `+ - * /`）。但会失去对「非 NumPy 数组库」的适配——那些库的对象可能不支持用 Python 运算符直接运算，或行为不一致。这两行是「把输入归一化到正确的数组命名空间」的关键。

---

### 4.3 old_scale → Kelvin：四进一出的「汇聚」中转

#### 4.3.1 概念说明

函数体的第一段，负责把**任意**温标的读数统一换成 Kelvin。这正是 2.2 节说的「中转设计」的第一步：四个温标各有一条「→Kelvin」的公式，汇聚到一个共同的中间量 `tempo`（临时变量，存放 Kelvin 值）。

为什么选 Kelvin 做中转？因为它的零点是绝对零度，是「真零点」，所有温标都能干净地映射过去而不产生歧义。

#### 4.3.2 核心流程

四个分支的公式（设输入为 \(x\)，输出 Kelvin 为 \(T_K\)）：

| old_scale | 公式 | 含义 |
| --- | --- | --- |
| Celsius (c) | \(T_K = x + 273.15\) | 仅平移零点，刻度相同 |
| Kelvin (k) | \(T_K = x\) | 已经是 Kelvin，不变 |
| Fahrenheit (f) | \(T_K = (x - 32) \times \dfrac{5}{9} + 273.15\) | 先减 32 拿到华氏温差，再换刻度，再加零点 |
| Rankine (r) | \(T_K = x \times \dfrac{5}{9}\) | Rankine 刻度同华氏，但零点已是绝对零度，只需换刻度 |

Fahrenheit 分支稍复杂，拆开理解：`(x - 32)` 把华氏读数变成「相对冰点的华氏温差」，`* 5/9` 把华氏温差换算成开尔文温差（因为 9 °F 的差 = 5 K 的差），`+ 273.15` 再把冰点本身补到绝对零点。

如果 `old_scale` 不在这四种里，走 `else` 分支抛 `NotImplementedError`，提示只支持这四种温标。

#### 4.3.3 源码精读

「old_scale → Kelvin」整段：

[_constants.py#L275-L287](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L275-L287) —— 四个 `if/elif` 分支分别处理摄氏、开尔文、华氏、兰氏，结果都赋给同一个中间变量 `tempo`；最后的 `else` 抛 `NotImplementedError`，并用 f-string 把出错的 `old_scale` 值拼进错误信息，方便排错。

注意几处实现细节：

- 用 `old_scale.lower() in ['celsius', 'c']` 同时兼容大小写和全称/缩写（`'Celsius'`、`'celsius'`、`'C'`、`'c'` 都认）。
- 摄氏与开尔文分支复用了模块常量 `zero_Celsius`（273.15），而不是写魔法数字，体现「单一数据源」。
- 华氏与兰氏分支用字面量 `5 / 9`（见 4.4.3 的进一步讨论）。

#### 4.3.4 代码实践

**目标**：手算每个 `old_scale → Kelvin` 公式，与 `convert_temperature(x, old, 'Kelvin')` 的输出逐一对照。

**操作步骤**：

1. 取四个温标的「水的冰点」读数：C=0、K=273.15、F=32、R=491.67。
2. 手算它们各自转 Kelvin 的结果（应都是 273.15）。
3. 用代码验证。

```python
# 示例代码
import scipy.constants as sc

for old, x in [('celsius', 0), ('kelvin', 273.15), ('fahrenheit', 32), ('rankine', 491.67)]:
    print(f"{x} {old} -> K =", sc.convert_temperature(x, old, 'Kelvin'))
```

**需要观察的现象**：四个不同温标的「冰点」读数，换算后都应等于 273.15 K。

**预期结果**：四行输出全是 `273.15`，证明汇聚到同一个中转点。

> 待本地验证：兰氏度 491.67 是因为 273.15 × 9/5 = 491.67。

#### 4.3.5 小练习与答案

**练习 1**：`convert_temperature(-40, 'fahrenheit', 'Kelvin')` 等于多少？手算验证。

**答案**：\((-40 - 32) \times 5/9 + 273.15 = (-72) \times 5/9 + 273.15 = -40 + 273.15 = 233.15\) K。（巧合：−40 是 C 和 F 唯一相等的点。）

**练习 2**：为什么华氏分支要先减 32，而不是直接乘 5/9？

**答案**：因为华氏度的零点（0 °F）不在绝对零度，而在盐水冰点附近。必须先用 `(x - 32)` 去掉「相对冰点」的零点偏移，得到纯粹的华氏温差，才能用刻度比 5/9 换算成开尔文温差；最后再 `+ 273.15` 补回绝对零点。直接乘 5/9 会把零点偏移也按比例缩放，结果就错了。

---

### 4.4 Kelvin → new_scale：对称的反向分支与错误处理

#### 4.4.1 概念说明

汇聚到 `tempo`（Kelvin 值）之后，第二段做反向操作：从 Kelvin 转到目标温标。它的结构与第一段**完全对称**——四个 `if/elif` 分支对应四个目标温标，外加一个 `else` 抛 `NotImplementedError`。这种「正向四分支 + 反向四分支」的对称结构，正是中转设计的直接体现。

本节还会澄清一个细节：函数**复用了 `zero_Celsius`**，但**并没有直接引用 `degree_Fahrenheit`**——华氏分支里用的是字面量 `5 / 9` 和 `9 / 5`。

#### 4.4.2 核心流程

四个反向分支的公式（设 Kelvin 中转值为 \(T_K\)，目标读数为 \(y\)）：

| new_scale | 公式 | 含义 |
| --- | --- | --- |
| Celsius (c) | \(y = T_K - 273.15\) | 反向平移零点 |
| Kelvin (k) | \(y = T_K\) | 已经是 Kelvin |
| Fahrenheit (f) | \(y = (T_K - 273.15) \times \dfrac{9}{5} + 32\) | 先减零点得开尔文温差，换刻度，再加华氏零点 |
| Rankine (r) | \(y = T_K \times \dfrac{9}{5}\) | 只换刻度（零点已对齐绝对零度） |

注意华氏与兰氏的正反向是互逆的：正向 `* 5/9`、反向 `* 9/5`；华氏正向 `- 32`、反向 `+ 32`。这种互逆性保证了 `convert_temperature(convert_temperature(x, A, B), B, A) == x`（往返一致）。

错误处理上，第二段也有自己的 `else`：当 `new_scale` 不被支持时，抛出携带 `new_scale=` 字样的 `NotImplementedError`，与第一段的 `old_scale=` 报错信息区分开，便于定位是哪个参数写错了。

#### 4.4.3 源码精读

「Kelvin → new_scale」整段与返回：

[_constants.py#L288-L302](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L288-L302) —— 四个反向分支把 `tempo` 换算成目标温标的 `res`，结构镜像第一段；末尾 `return res` 返回结果。两段报错信息分别带 `old_scale=` 与 `new_scale=` 前缀。

关于常量复用的细节（重要）：

- 函数在摄氏分支里**复用了模块常量 `zero_Celsius`**（[_constants.py#L204](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L204) 定义，[_constants.py#L277](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L277) 等处使用）。
- 但它**没有**直接引用 `degree_Fahrenheit`（[_constants.py#L205](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L205)）。华氏/兰氏分支用的是字面量 `5 / 9` 与 `9 / 5`。

这里有一个值得想清楚的关联：

\[ \frac{5}{9} = \frac{1}{1.8} = \texttt{degree\_Fahrenheit}, \qquad \frac{9}{5} = 1.8 = \frac{1}{\texttt{degree\_Fahrenheit}} \]

也就是说，函数里那些「看起来随意」的 `5/9` 和 `9/5`，**数值上恰好等于 `degree_Fahrenheit` 和它的倒数**。作者没有复用 `degree_Fahrenheit`，可能是因为「温度差换算」与「温度值换算」在概念上不同——`degree_Fahrenheit` 的注释明确写了 `# only for differences`，而 `convert_temperature` 处理的是带零点的温度值，直接写成 `5/9` 更不容易让人误解为「只用于温度差」。这是一个细微但真实的实现选择，阅读源码时不要想当然地以为它「复用了 degree_Fahrenheit」。

错误处理的测试依据：

[tests/test_constants.py#L58-L62](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py#L58-L62) —— `test_convert_temperature_errors` 用 `pytest.raises(NotImplementedError, match="old_scale=")` 和 `match="new_scale="` 分别验证两个方向的不支持温标都会抛错，且错误信息能区分是哪个参数。

#### 4.4.4 代码实践（核心任务：Rankine ↔ Fahrenheit 对照表）

**目标**：构造一张 Rankine ↔ Fahrenheit 的对照表，验证公式 `F = (R × 5/9 − 273.15) × 9/5 + 32`，并扩展思考「新增温标要改哪里」。

**操作步骤**：

1. 取一组 Rankine 读数（如 0、491.67、555.27、671.67）。
2. 手算每个对应的华氏度（提示：先 R→K：`R * 5/9`，再 K→F：`(K − 273.15) * 9/5 + 32`）。
3. 用 `convert_temperature` 验证，并核对测试里已锁定的锚点（`491.67 °R == 32 °F`）。

```python
# 示例代码
import scipy.constants as sc

rankine = [0, 491.67, 555.27, 671.67]
print("Rankine -> Fahrenheit 对照表：")
for r in rankine:
    f = sc.convert_temperature(r, 'rankine', 'fahrenheit')
    # 手算复核：先转 K，再转 F
    manual = (r * 5/9 - 273.15) * 9/5 + 32
    print(f"  {r:>8} R = {f:8.3f} F   (手算 {manual:8.3f})")

# 验证测试锚点：491.67 R 应等于 32 F
assert abs(sc.convert_temperature(491.67, 'r', 'F') - 32.0) < 1e-9
# 验证往返一致
assert abs(sc.convert_temperature(sc.convert_temperature(100, 'R', 'F'), 'F', 'R') - 100) < 1e-9
```

**需要观察的现象**：491.67 °R 对应 32 °F（水的冰点）；671.67 °R 对应 212 °F（水的沸点）；手算与函数输出一致；往返换算回到原值。

**预期结果**：对照表数值与手算吻合，两个 `assert` 都通过。

> 待本地验证：测试文件 [tests/test_constants.py#L31-L42](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py#L31-L42) 已用 `491.67 R == 32 F` 等断言锁定这些锚点，可作为权威参考。

**扩展练习**：假设要新增一个温标（比如「Delisle」），需要修改函数的哪两处？为什么 `old_scale` 和 `new_scale` 都要做判断？

**参考答案**：

- 需要改**两处**：第一段（old_scale → Kelvin）加一条「Delisle → Kelvin」的 `elif`；第二段（Kelvin → new_scale）加一条「Kelvin → Delisle」的 `elif`。
- 原因：函数用中转设计，输入温标和输出温标是**两个独立的参数**。用户可能把 Delisle 当作输入（`old_scale='delisle'`），也可能当作输出（`new_scale='delisle'`），甚至两者都是 Delisle。所以「→Kelvin」和「Kelvin→」两个方向都必须认识新温标，缺一个都会落到对应的 `else` 分支抛 `NotImplementedError`。这正是两段报错信息分别带 `old_scale=` / `new_scale=` 前缀的意义——它告诉你到底是哪个方向漏了支持。

#### 4.4.5 小练习与答案

**练习 1**：`convert_temperature(273.15, 'Kelvin', 'R')` 等于多少？为什么？

**答案**：等于 491.67 °R。因为 Rankine 与 Kelvin 的零点都是绝对零度，只需换刻度：\(273.15 \times 9/5 = 491.67\)。

**练习 2**：函数里华氏分支用 `9 / 5`，而没有写成 `1 / degree_Fahrenheit`。这两种写法在数值上等价吗？为什么作者选前者？

**答案**：数值上完全等价，因为 `degree_Fahrenheit = 1/1.8 = 5/9`，其倒数就是 `9/5 = 1.8`。作者选用字面量 `5/9` 和 `9/5`，很可能是为了语义清晰——`degree_Fahrenheit` 的注释强调它「只用于温度差」，而 `convert_temperature` 处理的是带零点的温度值，用字面量能避免读者把它误当成「温度差专用因子」。

**练习 3**：如果调用 `convert_temperature(20, 'Celsius', 'Celsius')`，结果是什么？中间经过 Kelvin 吗？

**答案**：结果是 20（原值返回）。但中间确实经过了 Kelvin：第一段 C→K 得到 293.15，第二段 K→C 又减回 20。这说明中转设计对所有调用都一视同仁，即使输入输出温标相同也会「绕一圈」，对线性变换（如 C↔K）是无损往返的。

---

## 5. 综合实践

把本讲的知识串起来：实现一个**「四温标温度卡」小程序**，输入任意温标的一个温度，一次性打印它在 C / K / F / R 四个温标下的读数，并加上自检。

**要求**：

1. 用 `convert_temperature` 完成「一次输入 → 四个输出」，不允许自己写换算公式。
2. 选一个锚点（如水的三相点 0.01 °C）验证四温标读数两两自洽。
3. 故意传一个不支持的温标，捕获 `NotImplementedError`，打印友好提示。

```python
# 示例代码（综合实践）
import scipy.constants as sc

SCALES = ['Celsius', 'Kelvin', 'Fahrenheit', 'Rankine']

def temp_card(val, old_scale):
    """把一个温度同时换算成四个温标的读数。"""
    print(f"输入：{val} {old_scale}")
    for s in SCALES:
        try:
            out = sc.convert_temperature(val, old_scale, s)
            print(f"  -> {out:10.4f} {s}")
        except NotImplementedError as e:
            print(f"  -> [不支持] {s}: {e}")

# 1) 水的三相点 0.01 C
temp_card(0.01, 'Celsius')

# 2) 自检：从 C 出发换到 K，再换回 C，应回到 0.01
k_val = sc.convert_temperature(0.01, 'C', 'Kelvin')
back  = sc.convert_temperature(k_val, 'Kelvin', 'Celsius')
print(f"\n往返自检：0.01 C -> {k_val} K -> {back} C")
assert abs(back - 0.01) < 1e-12, "往返不一致！"
print("往返自检通过。")

# 3) 错误处理演示
print("\n错误处理演示：")
temp_card(100, 'cheddar')   # 不支持的温标
```

**自检要点**：

- 三相点 0.01 °C 应换算为 273.16 K、32.018 °F、491.688 °R（待本地验证精确值）。
- 往返自检的 `assert` 应通过，证明中转设计无损。
- 不支持的温标应被 `try/except` 捕获，打印 `[不支持]` 而不是崩溃。

---

## 6. 本讲小结

- 温度是 constants 子包里**唯一**需要非线性（仿射）换算的物理量：温标零点不一致，必须 `y = a·x + b`，所以无法像 `inch` 那样只乘因子，需要专门的 `convert_temperature`。
- `convert_temperature` 采用**中转设计**：先把 `old_scale` 统一换成 Kelvin（四进一出），再从 Kelvin 换成 `new_scale`（一进四出），把 \(N^2\) 个两两公式压缩成 \(2N\) 个。
- 四个温标的换算公式都围绕水的冰点（0 °C = 273.15 K = 32 °F = 491.67 °R）展开：摄氏只平移零点，华氏既移零点又换刻度，兰氏只换刻度。
- 函数**复用**了模块常量 `zero_Celsius`，但华氏/兰氏分支用字面量 `5/9` 和 `9/5`——它们在数值上恰等于 `degree_Fahrenheit` 及其倒数，作者选择字面量以避免与「温度差专用」的语义混淆。
- `@xp_capabilities()` 装饰器不改变运行逻辑，只登记「支持哪些数组后端」的元数据，并配合 `array_namespace` / `_asarray` 让函数适配 NumPy 之外的数组库。
- 两个方向各有独立的 `else` 抛 `NotImplementedError`，错误信息分别带 `old_scale=` / `new_scale=` 前缀，便于定位；新增温标必须**同时**改「→Kelvin」和「Kelvin→」两处分支。

## 7. 下一步学习建议

- 继续阅读 [_constants.py#L305-L369](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L305-L369) 中的 `lambda2nu` / `nu2lambda`，它们是另一种「非线性换算」（波长 ↔ 频率，本质是 \(c/\lambda\) 的除法），同样用了 `@xp_capabilities`，可对比理解装饰器的通用用法——这正是下一讲 **u3-l2「Array API 集成与 xp_capabilities」** 的主题。
- 阅读 [tests/test_constants.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_constants.py) 中的 `@make_xp_test_case(sc.convert_temperature)`，理解测试框架如何读取 `@xp_capabilities` 元数据、自动把同一个温度测试跑到多个数组后端上（u3-l4「测试体系」会展开）。
- 若想体会「线性换算为什么不需要函数」，回头对比 [_constants.py#L196-L201](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L196-L201) 的速度换算因子（`kmh`、`mph`、`knot`）——它们都是纯乘数，印证本讲「温度特殊」的结论。
