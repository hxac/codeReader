# 训练调度核心：C() 函数与步数感知参数

## 1. 本讲目标

DreamCraft3D 的四条 yaml 配置里散布着大量形如 `[2000, 5., 1., 2001]`、`[0, 0.7, 0.2, 200]` 的列表。它们不是普通数组，而是一套微型调度语言：由 [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) 中不到 30 行的 `C()` 函数在运行期解释成「随训练步数变化的数值」。粗阶段正则为何在第 2000 步突然增压、扩散时间步区间为何从高噪声收缩到中噪声、数据管线为何可以不加载法向图——全部由这套机制驱动。

学完本讲，你应该能够：

1. 准确说出 `C()` 对标量、三元组、四元组、六元素以上列表这四种输入各自的解析规则。
2. 手算任意四元组在任意步数的取值，并理解 `end_step` 的 int/float 类型决定时间轴这一隐藏开关。
3. 说清 `C_max()` 与 `cmaxgt0` resolver 如何在**配置期**（还没有 global_step 时）用「可达上界」联动数据加载开关。
4. 沿 `BaseSystem.C → dreamcraft3d.py 损失加权 → guidance.update_step 时间步区间` 这条消费链走一遍源码。
5. 仿照 `cmaxgt0` 注册一个自己的 OmegaConf resolver，并理解为什么它不能用 `custom_import` 注入。

本讲承接 u6-l4 的结论「全部损失经 C() 四元组调度、set_loss/lambda_ 查表加权汇总」——那一讲看的是调度机制的**使用处**，本讲钻进机制**本身**。

## 2. 前置知识

**课程式调度（curriculum）**。3D 生成训练不是一锅炖：前期需要大梯度先把形状拉出来，后期需要小权重保稳定；扩散先验前期该在高噪声区工作（管大结构），后期该收缩到中低噪声区（管细节）。让超参数随训练步数变化，就叫课程式调度。`C()` 就是这个项目里实现课程式调度的通用引擎。

**截断线性插值**。给定起点 \((s_0, v_0)\) 和终点 \((s_1, v_1)\)，在两点之间画直线，两端之外「钳制」（clamp）在端点值上：

\[ v(s) = v_0 + (v_1 - v_0)\cdot \operatorname{clip}\!\left(\frac{s-s_0}{s_1-s_0},\ 0,\ 1\right) \]

`C()` 的全部数学就是这一条公式，外加「多个区间接力」的扩展。

**配置期与运行期**。这是本讲最重要的一个区分：

- **配置期**：`load_config` 里 `OmegaConf.resolve(cfg)` 展开插值的时刻。此时还没有任何训练步数，只能做与步数无关的静态计算（比如「这个权重整个训练过程最大会是多少」）。
- **运行期**：每个训练批次里，系统拿着当前 `global_step` 现场求值。`C()` 是运行期的主角。

**OmegaConf resolver**（u2-l2 已讲，一句回顾）：yaml 里 `${rmspace:...}`、`${cmaxgt0:...}` 这种 `名字:参数` 插值，由 `OmegaConf.register_new_resolver` 注册的 Python 函数求值。resolver 在配置期执行——记住这一点，第 4.3 节会反复用到。

**步数感知参数的两种实现**。本仓库里「随步数变化」的参数有两类写法：`C()` 的**连续插值**（损失权重、时间步区间），以及 `bisect`/整数除法的**离散换挡**（分辨率里程碑、哈希编码层级解锁，见 u3-l2、u4-l1）。本讲聚焦前者，末尾会对比两者。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) | `C()` 函数本体：把列表规格解释为步数相关的数值 |
| [threestudio/utils/config.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py) | resolver 注册区 + `C_max()` 可达上界 + `load_config`（配置期入口） |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | `BaseSystem.C()` 方法与 `true_global_step` 时间源 |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | 消费现场：损失门控、加权、`train_params` 日志 |
| [threestudio/models/guidance/deep_floyd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py) | `update_step` 中用 `C()` 收缩扩散时间步区间 |
| configs/dreamcraft3d-coarse-nerf.yaml 等 | 四元组调度的「数据」侧：配置里写什么 |

## 4. 核心概念与源码讲解

### 4.1 C() 的解析规则：标量、三元组与四元组

#### 4.1.1 概念说明

`C()` 的签名是 `C(value, epoch, global_step)`，职责一句话：**把配置里一个「可能是标量、也可能是列表」的参数，解释成当前时刻的数值**。它的输入语言支持四种形态：

| 配置写法 | 含义 |
| --- | --- |
| `0.1`（标量） | 常数，任何步数都返回 0.1 |
| `[v₀, v₁, e₁]`（三元组） | 缺省 `start_step=0`，等价于 `[0, v₀, v₁, e₁]` |
| `[s₀, v₀, v₁, e₁]`（四元组） | 从第 `s₀` 步的 `v₀` 线性过渡到第 `e₁` 步的 `v₁`，两端钳制 |
| `[s₀, v₀, v₁, e₁, v₂, e₂, ...]`（≥6 元素） | 多段接力，见 4.2 |

项目配置里的注释也点明了顺序约定——[configs/dreamcraft3d-coarse-nerf.yaml:L110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L110) 写着 `# (start_iter, start_val, end_val, end_iter)`，即**步、值、值、步**，两个值夹在两个步数中间。

还有一个隐藏开关：`e₁` 写成 int（如 `2001`）则按 `global_step` 调度；写成 float（如 `2001.` 或 `2.001e3`）则改按 **epoch** 调度。类型选择时间轴，这是极易踩的坑。

#### 4.1.2 核心流程

```
C(value, epoch, global_step):
    若 value 是 int/float        → 原样返回（常数参数）
    否则 config_to_primitive     → ListConfig 转纯 Python list
    若长度为 3                    → 前面补 0，变成 [0, v0, v1, e1]
    （≥6 的多段分支见 4.2）
    断言长度为 4，解包 start_step, start_value, end_value, end_step
    若 end_step 是 int            → 时间轴取 global_step
    若 end_step 是 float          → 时间轴取 epoch
    返回 start_value + (end_value - start_value) * clip(进度, 0, 1)
```

其中进度 \( p = \dfrac{s - s_0}{s_1 - s_0} \)，钳制到 \([0,1]\)——于是 `s < s₀` 时恒为 `v₀`，`s > e₁` 时恒为 `v₁`。

#### 4.1.3 源码精读

函数本体位于 [threestudio/utils/misc.py:L65-L97](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L65-L97)，先看前半段（标量 / 三元组归一化）：

```python
def C(value: Any, epoch: int, global_step: int) -> float:
    if isinstance(value, int) or isinstance(value, float):
        pass
    else:
        value = config_to_primitive(value)
        if not isinstance(value, list):
            raise TypeError("Scalar specification only supports list, got", type(value))
        if len(value) == 3:
            value = [0] + value
```

这段做了两件事：标量直接放行（`pass` 后落到函数末尾的 `return value`）；列表先经 `config_to_primitive`（内部是 `OmegaConf.to_container`）把 ListConfig 转成纯 list——因为运行期从 `self.cfg.loss` 里取出的四元组还是 OmegaConf 容器。长度为 3 时补 `[0]` 前缀。

再看插值与双时间轴分支，[threestudio/utils/misc.py:L87-L96](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L87-L96)：

```python
        if isinstance(end_step, int):
            current_step = global_step
            value = start_value + (end_value - start_value) * max(
                min(1.0, (current_step - start_step) / (end_step - start_step)), 0.0
            )
        elif isinstance(end_step, float):
            current_step = epoch
            value = start_value + (end_value - start_value) * max(
                min(1.0, (current_step - start_step) / (end_step - start_step)), 0.0
            )
```

`max(min(1.0, p), 0.0)` 就是 clip；两个分支唯一的差别是时间轴变量。注意 yaml 中 `2001` 解析为 int、`2001.` 解析为 float——配置里手滑多写一个点，调度轴就从 step 换成了 epoch，且不会有任何报错。

拿粗阶段两个真实参数手算验证（`global_step` 轴）：

- `lambda_orient: [2000, 1., 10., 2001]`（[configs/dreamcraft3d-coarse-nerf.yaml:L136](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L136)）：`e₁ - s₀ = 1`，过渡窗口只有一步宽。第 1999 步取 1.0（钳制在起点），第 2000 步进度 0 仍为 1.0，第 2001 步进度 1 变成 10.0。**一行实现阶跃函数**——这就是本仓库「step 跳变」的标准惯用法（`lambda_sparsity`、`lambda_opaque`、`lambda_3d_normal_smooth` 同款，见 [configs/dreamcraft3d-coarse-nerf.yaml:L135-L138](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L135-L138)）。
- `min_step_percent: [0, 0.7, 0.2, 200]`（[configs/dreamcraft3d-coarse-nerf.yaml:L98](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L98)）：第 0 步 0.7，第 100 步 \(0.7 + (0.2-0.7)\times 0.5 = 0.45\)，第 200 步及以后 0.2。真正的线性缓降。

#### 4.1.4 代码实践

**实践目标**：用脚本验证上表手算值，建立对解析规则的肌肉记忆。

**操作步骤**：

1. 若已按 u1-l2 装好 `tinycudann` 等编译扩展，直接 `from threestudio.utils.misc import C`（misc.py 顶层 `import tinycudann`，缺它无法导入）；否则把 `C()` 的函数体原样复制进脚本，并标注「示例代码（复制自 misc.py）」。
2. 运行下面脚本（示例代码）：

```python
from threestudio.utils.misc import C  # 或使用复制版

JUMP = [2000, 5., 1., 2001]     # 阶跃惯用法
RAMP = [0, 0.7, 0.2, 200]       # 线性缓降
EPOCH_AXIS = [0, 0.7, 0.2, 200.]  # 注意结尾的小数点：切换到 epoch 轴

for s in [0, 100, 199, 200, 1999, 2000, 2001, 3000]:
    print(f"step={s:5d}  JUMP={C(JUMP, 0, s):6.2f}  RAMP={C(RAMP, 0, s):6.3f}")

# epoch 轴演示：global_step 随便给多大都不影响结果
print("epoch=100:", C(EPOCH_AXIS, epoch=100, global_step=99999))
```

3. 把打印结果与 4.1.3 的手算值逐行对拍。

**需要观察的现象**：`JUMP` 在 step 2000→2001 之间从 5.00 直接跳到 1.00，中间没有任何过渡值；`RAMP` 在 step 100 恰为 0.450；`EPOCH_AXIS` 一行即使 `global_step=99999`，返回值仍由 `epoch=100` 决定（应为 0.45）。

**预期结果**：打印序列为 `JUMP: 5.00, 5.00, 5.00, 5.00, 5.00, 5.00, 1.00, 1.00`；`RAMP: 0.700, 0.450, 0.202, 0.200, 0.200, ...`（step 199 时进度 199/200 = 0.995，值 = 0.7 − 0.5×0.995 = 0.2025，按 3 位小数打印约 0.202）。绘图环节待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：配置里写 `lambda_xxx: [5., 1., 3000]`，第 1500 步的值是多少？
**答案**：三元组先归一化为 `[0, 5., 1., 3000]`，进度 1500/3000 = 0.5，值 = 5 + (1−5)×0.5 = **3.0**。

**练习 2**：想让某正则权重从第 500 步的 0 平滑升到第 2500 步的 8，配置该怎么写？
**答案**：`lambda_xxx: [500, 0.0, 8.0, 2500]`。第 500 步之前被钳制在 0，之后线性升到 8，2500 步后恒为 8。

**练习 3**：`[0, 0.7, 0.2, 200]` 和 `[0, 0.7, 0.2, 200.]` 行为有何不同？
**答案**：前者 `end_step` 是 int，按 `global_step` 调度，200 步内完成过渡；后者是 float，按 `epoch` 调度——若每个 epoch 只跑一个批次，过渡会拉长到 200 个 epoch（对应 200 个 step 才巧合相等，多批次 epoch 时完全不同）。二者可能长期「看起来都对」，是最隐蔽的配置事故之一。

### 4.2 多段调度：≥6 元素的分段线性插值

#### 4.2.1 概念说明

一个四元组只能表达「一次起落」。要表达「先升后降」或「阶梯式多段变化」，就把列表延长到 6 个元素以上：**第一个四元组照旧，之后每追加两个元素 `(值, 步)`，就接续一段**。记号化地写：

\[ [\,s_0,\ v_0,\ \underbrace{v_1,\ e_1}_{\text{第 1 段终点},\ \text{起点重复},\ \underbrace{v_2,\ e_2}_{\text{第 2 段}},\ \cdots\,] \]

即第 k 段从拐点 \((e_{k-1},\, v_{k-1})\) 走到 \((e_k,\, v_k)\)（其中 \(e_0 := s_0\)）。拐点处前后两段共享同一个 `(步, 值)`，所以整条曲线**天然连续**。默认配置里没用到这个形态，但 `DeepFloydGuidance.Config.grad_clip` 的注释给出了现成示例——[threestudio/models/guidance/deep_floyd_guidance.py:L31-L33](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L31-L33) 的 `field(default_factory=lambda: [0, 2.0, 8.0, 1000])` 是四元组写法，若要「0→2 在 0..1000 步，再 2→8 在 1000..2000 步」，写 `[0, 2.0, 8.0, 1000, 8.0, 2000]` 即可（此处为讲解用的示例取值）。

#### 4.2.2 核心流程

多段分支的任务是：**从列表里选出当前步数落在哪一段，取出该段的起点与终点，退化成四元组**。

```
select_i = 3                              # 先假设落在第 1 段
for i in {3, 5, 7, ...}（除最后一个拐点外的所有 e 的下标）:
    if global_step >= value[i]:           # 已越过拐点 e
        select_i = i + 2                  # 跳到下一段的终点下标
select_i 最终 = 当前活跃段的 end_step 下标
段内插值与 4.1 的四元组完全相同
```

下标对应关系（以 6 元素为例，`[s₀, v₀, v₁, e₁, v₂, e₂]`）：

| select_i | 活跃段 | 起点 (step, value) | 终点 (step, value) |
| --- | --- | --- | --- |
| 3 | 第 1 段 | (value[0], value[1]) = (s₀, v₀) | (value[3], value[2]) = (e₁, v₁) |
| 5 | 第 2 段 | (value[3], value[2]) = (e₁, v₁) | (value[5], value[4]) = (e₂, v₂) |

#### 4.2.3 源码精读

[threestudio/utils/misc.py:L74-L86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L74-L86)：

```python
        if len(value) >= 6:
            select_i = 3
            for i in range(3, len(value) - 2, 2):
                if global_step >= value[i]:
                    select_i = i + 2
            if select_i != 3:
                start_value, start_step = value[select_i - 3], value[select_i - 2]
            else:
                start_step, start_value = value[:2]
            end_value, end_step = value[select_i - 1], value[select_i]
            value = [start_step, start_value, end_value, end_step]
        assert len(value) == 4
        start_step, start_value, end_value, end_step = value
```

三个精读要点：

1. **循环上界 `len(value) - 2`**：`range(3, len-2, 2)` 枚举的是下标 3、5、7…但**不含最后一个拐点**。以 6 元素为例 `range(3, 4, 2)` 只有 `{3}`——最后一段无需判断，超出末段终点直接被 4.1 的 clip 钳制住。
2. **两个分支的解包顺序不同**：`select_i != 3` 时按 `(value, step)` 取 `select_i-3, select_i-2`；`select_i == 3` 时按 `(step, value)` 取 `value[:2]`。读代码时极易看串，对照 4.2.2 的表格即可理顺。
3. **静默吞掉畸形输入**：长度为 5 或 7 的列表不会触发断言失败，多余元素被悄悄丢弃（例如 5 元素只按前 4 个解释）。只有长度归约不到 4（如长度 2）才会被 `assert` 拦下。写多段配置时务必成对追加。

#### 4.2.4 代码实践

**实践目标**：画出一条两段式调度曲线，直观确认「拐点连续 + 末段钳制」。

**操作步骤**（示例代码）：

```python
import matplotlib.pyplot as plt
from threestudio.utils.misc import C  # 或使用复制版

TWO_SEG = [0, 0., 10., 1000, 1., 3000]  # (0,0)→(1000,10)→(3000,1)
steps = list(range(0, 3001, 10))
vals = [C(TWO_SEG, 0, s) for s in steps]

plt.plot(steps, vals)
for s in [500, 1000, 2000, 3000]:
    plt.scatter([s], [C(TWO_SEG, 0, s)], color="red")
    plt.annotate(f"({s}, {C(TWO_SEG, 0, s):g})", (s, C(TWO_SEG, 0, s)))
plt.xlabel("global_step"); plt.ylabel("value"); plt.savefig("c_two_seg.png")
```

**需要观察的现象**：曲线在 (1000, 10) 处形成峰，前后两段在该点无缝衔接；3000 步之后（把采样范围扩到 4000 验证）曲线保持在 1.0 不再变化。

**预期结果**：拐点值 (500, 5)、(1000, 10)、(2000, 5.5)、(3000, 1)——第 2 段进度 (2000−1000)/(3000−1000)=0.5，值 10+(1−10)×0.5=5.5。图形输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`[0, 0., 10., 1000, 1., 3000]` 在 step=1000 时落在第几段？值是多少？
**答案**：循环里 `global_step(1000) >= value[3](1000)` 成立，select_i=5，落进第 2 段；第 2 段进度 0，值 = 起点值 10.0。注意**边界步归后段**（`>=` 判断）。

**练习 2**：用多段列表表达「前 1000 步恒为 0，1000→2000 步线性升到 5，之后恒为 5」。
**答案**：`[0, 0.0, 0.0, 1000, 5.0, 2000]`。第 1 段 (0,0)→(1000,0) 保持 0，第 2 段 (1000,0)→(2000,5) 线性上升，2000 步后钳制在 5。（用 `[1000, 0.0, 5.0, 2000]` 四元组也能达到同样效果——前 1000 步被钳制在 0。）

**练习 3**：为什么多段列表的拐点值不会出现「前后段定义不一致」？
**答案**：因为段 k 的终点值（`value[select_i-3]`）与段 k+1 的起点值取自**同一个列表元素**——拐点 `(e_k, v_k)` 在列表中只出现一次，两段共享，几何上即分段线性函数在拐点处连续。

### 4.3 C_max 与 cmaxgt0：配置期的可达上界

#### 4.3.1 概念说明

有个问题配置期就能回答、且必须配置期回答：**「这个调度参数在整个训练过程中最大会到多少？」** 典型场景是数据加载——若 `lambda_normal`（法向损失权重）从头到尾都不超过 0，就没必要加载 GT 法向图，能省内存和预处理时间。但配置期没有 `global_step`，不能调用 `C()`。

解法是一条数学性质：分段线性函数的最大值必在某个拐点取到，所以「可达上界 = 所有拐点值的最大值」。`C_max()` 实现的正是这个静态上界，而 resolver `cmaxgt0` 把它接进 yaml。

#### 4.3.2 核心流程

```
C_max(value):
    标量 → 原样返回
    ≥6 元素 → 收集下标 2, 4, 6, ... 处的所有 v，取 max
    归约成四元组后 → max(start_value, end_value)
```

配置侧消费链（coarse-nerf 为例）：

```
yaml: requires_normal: ${cmaxgt0:${system.loss.lambda_normal}}
  → 配置期 OmegaConf.resolve 求值
  → lambda_normal = 0.0（标量）→ C_max = 0.0 → False → 不加载 GT 法向图
若 lambda_normal 改为 [1000, 0., 5., 2000] → C_max = 5.0 → True → 加载
```

#### 4.3.3 源码精读

`C_max` 位于 [threestudio/utils/config.py:L31-L48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L31-L48)：

```python
def C_max(value: Any) -> float:
    if isinstance(value, int) or isinstance(value, float):
        pass
    else:
        value = config_to_primitive(value)
        if not isinstance(value, list):
            raise TypeError("Scalar specification only supports list, got", type(value))
        if len(value) >= 6:
            max_value = value[2]
            for i in range(4, len(value), 2):
                max_value = max(max_value, value[i])
            value = [value[0], value[1], max_value, value[3]]
        if len(value) == 3:
            value = [0] + value
        assert len(value) == 4
        start_step, start_value, end_value, end_step = value
        value = max(start_value, end_value)
    return value
```

多段分支把下标 2、4、6…（各段终点值 \(v_1, v_2, \dots\)）先取 max 塞回一个伪四元组，最后再与 `start_value`（\(v_0\)）取 max——合计覆盖全部拐点值 \(v_0 \dots v_k\)。与 `C()` 不同，它完全不看 `epoch/global_step`，是纯粹的静态函数。

resolver 注册区在 [threestudio/utils/config.py:L10-L28](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L10-L28)，其中与本讲相关的两条：

```python
OmegaConf.register_new_resolver("cmaxgt0", lambda s: C_max(s) > 0)
OmegaConf.register_new_resolver(
    "cmaxgt0orcmaxgt0", lambda a, b: C_max(a) > 0 or C_max(b) > 0
)
```

`C_max(s) > 0` 的含义是「**在训练的某一步**这个权重会大于 0」，即「这个损失项终将被启用」。配置里的两处真实消费：

- [configs/dreamcraft3d-coarse-nerf.yaml:L17](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L17)：`requires_normal: ${cmaxgt0:${system.loss.lambda_normal}}`——按法向损失是否启用，决定数据管线是否加载 GT 法向图（`lambda_normal: 0.0`，故 coarse 阶段为 False）。
- [configs/dreamcraft3d-coarse-nerf.yaml:L86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L86)：`return_comp_normal: ${cmaxgt0:${system.loss.lambda_normal_smooth}}`——按法向平滑损失是否启用，决定渲染器是否顺带输出屏幕空间法向图。

注意配套的运行期联动：即使 `requires_normal` 为 False（不加载 GT 法向），`lambda_3d_normal_smooth: [2000, 5., 1., 2001]` 的 C_max 为 5 > 0，所以渲染器仍会在第 2000 步后开始输出法向并计算平滑正则——**配置期开关只裁剪数据加载，不裁剪损失计算**。

#### 4.3.4 代码实践

**实践目标**：注册一个自己的 `cmax` resolver（直接返回可达上界），在 yaml 字符串里使用并验证。

**操作步骤**（示例代码）：

```python
# register_cmax.py
from omegaconf import OmegaConf
import threestudio.utils.config  # 触发内置 resolver 注册（含 C_max）
from threestudio.utils.config import C_max, load_config

# 仿照 cmaxgt0，注册返回上界本体的 cmax；replace=True 便于重复运行
OmegaConf.register_new_resolver("cmax", lambda s: C_max(s), replace=True)

yaml_str = """
system:
  loss:
    lambda_orient: [2000, 1., 10., 2001]
    lambda_opaque: [2000, 0.1, 10., 2001]
  orient_max: ${cmax:${system.loss.lambda_orient}}
  opaque_max: ${cmax:${system.loss.lambda_opaque}}
"""
cfg = OmegaConf.create(yaml_str)
OmegaConf.resolve(cfg)
print(cfg.system.orient_max, cfg.system.opaque_max)
```

2. 运行 `python register_cmax.py`。
3. 进阶：用 `load_config(yaml_str, from_string=True)`（[threestudio/utils/config.py:L107-L117](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L107-L117) 支持 `from_string`）替换手动 `create+resolve`，体验完整配置管线。

**需要观察的现象**：打印出 `10.0 10.0`；若把 `lambda_orient` 改成多段 `[0, 1., 3., 100, 10., 2001]`，`orient_max` 应变成 10.0（各拐点 1、3、10 的最大值）。

**预期结果**：`10.0 10.0`。实际输出待本地验证。

**一个重要的坑**：不要试图用 `custom_import` 注入 resolver。[launch.py:L100-L105](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L100-L105) 中 `load_config`（内部已执行 `OmegaConf.resolve`）**先于** `custom_import` 执行——外部模块注册的 resolver 赶不上本份配置的求值。要使用自定义 resolver，必须像本实践这样写自己的入口脚本，在调用 `load_config` 之前完成注册。（对比之下，u3-l1 的组件注册走 `find`，发生在 `custom_import` 之后，所以组件可以用 `custom_import` 注入，resolver 不行。）

#### 4.3.5 小练习与答案

**练习 1**：`C_max([0, 0.7, 0.2, 200])` 等于多少？它和 `C(...) ` 在 step=0 的取值有何关系？
**答案**：max(0.7, 0.2) = **0.7**，恰好等于该调度在 step=0 的值——因为这段调度单调递减，最大值就在起点。`C_max` 不依赖步数，恒为 0.7。

**练习 2**：为什么 `cmaxgt0` 能安全地用作数据加载开关，而不会出现「配置期判断为不用加载、运行期却又需要」的矛盾？
**答案**：`C_max` 是所有拐点值的最大值，而分段线性函数的最大值必在拐点取得，所以 `C_max > 0` 等价于「存在某个步数 s 使 C(s) > 0」。配置期与运行期基于同一条调度曲线判断，逻辑上严格一致。

**练习 3**：`cmaxgt0orcmaxgt0` 这个名字略长的 resolver 可能用在什么场景？
**答案**：两个损失共享同一份输入数据的场合——例如某张图只有在「损失 A 或损失 B 任一启用」时才需要加载，就写 `${cmaxgt0orcmaxgt0:${system.loss.lambda_a},${system.loss.lambda_b}}`。本仓库配置未使用它，属于预留能力。

### 4.4 消费现场：时间源 true_global_step 与三类调度用法

#### 4.4.1 概念说明

`C()` 是纯函数，真正的工程问题在于**喂给它哪个步数**。训练中有三个「当前步数」候选人：Lightning 的 `self.global_step`、恢复评估（export/test）时从检查点读回的步数、以及 epoch。`BaseSystem` 用 `true_global_step` / `true_current_epoch` 两个 property 统一了这件事（u3-l3 已建立概念，此处看代码落点）。在此之上，全项目对 `C()` 的消费收敛为三类：**门控**（要不要算这个损失）、**加权**（损失乘多大权重）、**参数刷新**（引导组件的内部超参）。

#### 4.4.2 核心流程

```
每批训练开始（on_train_batch_start, base.py L174-178）
  → do_update_step(epoch, true_global_step) 递归刷新组件树（u3-l2）
      → guidance.update_step 里 C() 刷新 min/max_step、grad_clip
training_substep 里（dreamcraft3d.py）
  → 门控：  if self.C(self.cfg.loss.lambda_xxx) > 0: 才计算该损失
  → 加权：  loss += value * self.C(self.cfg.loss.lambda_xxx)
  → 日志：  self.log("train_params/lambda_xxx", self.C(value))
```

#### 4.4.3 源码精读

先看时间源。[threestudio/systems/base.py:L69-L81](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L69-L81)：

```python
    @property
    def true_global_step(self):
        if self._resumed_eval:
            return self._resumed_eval_status["global_step"]
        else:
            return self.global_step
```

`--export`/`--test` 等非 fit 模式下，Lightning 的 `global_step` 从 0 重新计数，[launch.py:L192](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L192) 调 `set_resume_status(ckpt["epoch"], ckpt["global_step"])` 把检查点里的真实步数存进 `_resumed_eval_status`——否则导出时所有步数感知参数都会按「第 0 步」取值。包装方法在 [threestudio/systems/base.py:L92-L93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L92-L93)：

```python
    def C(self, value: Any) -> float:
        return C(value, self.true_current_epoch, self.true_global_step)
```

再看 `training_substep` 里的门控与加权。门控示例（法向损失，[threestudio/systems/dreamcraft3d.py:L176](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L176)）：`if self.C(self.cfg.loss.lambda_normal) > 0:` 才进入法向损失计算；加权循环在 [threestudio/systems/dreamcraft3d.py:L321-L332](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L332)：

```python
        loss = 0.0
        for name, value in loss_terms.items():
            self.log(f"train/{name}", value)
            if name.startswith(loss_prefix):
                loss_weighted = value * self.C(
                    self.cfg.loss[name.replace(loss_prefix, "lambda_")]
                )
                self.log(f"train/{name}_w", loss_weighted)
                loss += loss_weighted
        for name, value in self.cfg.loss.items():
            self.log(f"train_params/{name}", self.C(value))
```

损失项 `loss_sd` 去掉前缀拼成 `lambda_sd` 再查表——同一处 `self.C` 调用既决定权重；门控处那一次决定「算不算」。`train_params/` 日志把每个调度参数的当前值写进 TensorBoard，等于免费的调度监控面板（u6-l4 的 `set_loss` 机制即建立在此之上）。

第三类消费在引导组件的 `update_step`。DeepFloyd 引导（[threestudio/models/guidance/deep_floyd_guidance.py:L490-L500](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L490-L500)）：

```python
    def update_step(self, epoch: int, global_step: int, on_load_weights: bool = False):
        # clip grad for stable training as demonstrated in
        # Debiasing Scores and Prompts of 2D Diffusion for Robust Text-to-3D Generation
        # http://arxiv.org/abs/2303.15413
        if self.cfg.grad_clip is not None:
            self.grad_clip_val = C(self.cfg.grad_clip, epoch, global_step)

        self.set_min_max_steps(
            min_step_percent=C(self.cfg.min_step_percent, epoch, global_step),
            max_step_percent=C(self.cfg.max_step_percent, epoch, global_step),
        )
```

`set_min_max_steps`（[threestudio/models/guidance/deep_floyd_guidance.py:L140-L142](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L140-L142)）把百分比换算成离散时间步：`self.min_step = int(self.num_train_timesteps * min_step_percent)`。粗阶段配置 `[0, 0.7, 0.2, 200]` × `[0, 0.85, 0.5, 200]`（1000 个训练时间步）即时间步采样区间约从 \([700, 850]\)（高噪声，管大结构）收缩到 \([200, 500]\)（中噪声，管细节）——与 u7-l2 讲过的 SDS 时间步调度正是同一件事。BSD 引导完全同构（[threestudio/models/guidance/stable_diffusion_bsd_guidance.py:L1124-L1134](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1124-L1134)），texture 配置的上界调度 `[0, 0.5, 0.2, 5000]`（[configs/dreamcraft3d-texture.yaml:L85](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L85)）对应 500→200 的缓降。geometry 阶段的 `lambda_normal_consistency: [1000,10.0,1,2000]`（[configs/dreamcraft3d-geometry.yaml:L111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L111)）则是 1000→2000 步间 10 线性降到 1 的缓降。

最后是对比项：**离散换挡**不走 `C()`。[threestudio/models/networks.py:L159-L163](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L159-L163) 的哈希编码层级解锁用整数除法：

```python
        current_level = min(
            self.start_level
            + max(global_step - self.start_step, 0) // self.update_steps,
            self.n_level,
        )
```

同样消费 `global_step`，但产出整数档位而非连续值——与 u4-l1 的分辨率 `bisect` 换挡同族。两类机制的共同前提是：`global_step` 必须是「真」的，这正是 `true_global_step` 存在的意义。

#### 4.4.4 代码实践

**实践目标**：不经真实训练，模拟出 DeepFloyd 引导的时间步区间随训练收缩的完整轨迹（源码阅读型实践 + 轻量模拟）。

**操作步骤**（示例代码）：

```python
import matplotlib.pyplot as plt
from threestudio.utils.misc import C

MIN_P, MAX_P = [0, 0.7, 0.2, 200], [0, 0.85, 0.5, 200]
NUM_TRAIN_TIMESTEPS = 1000  # DeepFloyd IF 的 scheduler.config.num_train_timesteps

steps = list(range(0, 501, 5))
lo = [int(NUM_TRAIN_TIMESTEPS * C(MIN_P, 0, s)) for s in steps]
hi = [int(NUM_TRAIN_TIMESTEPS * C(MAX_P, 0, s)) for s in steps]

plt.fill_between(steps, lo, hi, alpha=0.4)
plt.plot(steps, lo, label="min_step"); plt.plot(steps, hi, label="max_step")
plt.xlabel("global_step"); plt.ylabel("diffusion timestep t")
plt.legend(); plt.savefig("timestep_band.png")
```

2. 对照 [threestudio/models/guidance/deep_floyd_guidance.py:L202-L210](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/deep_floyd_guidance.py#L202-L210) 的 `t = torch.randint(self.min_step, self.max_step + 1, ...)`，确认渲染图实际加噪的 t 就从这个带子里均匀采样。
3. 把 `MAX_P` 换成 texture 阶段的 `[0, 0.5, 0.2, 5000]` 再画一张，观察收缩节奏的差异。

**需要观察的现象**：第 0 步带子约为 \([700, 850]\)，随步数增大整体下沉并收窄，第 200 步后固定在 \([200, 500]\)；texture 版带子要拖到第 5000 步才收敛到上界 200。

**预期结果**：区间端点值与 4.1 的手算一致（step=100 时约 \([450, 675]\)）。图形输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `training_substep` 里门控判断和加权取值要分别调两次 `self.C(...)`，而不是算一次存下来？
**答案**：二者语义不同：门控判断的是「此刻权重是否大于 0」（决定算不算这个损失），加权取的是「此刻权重的具体数值」。分开调用让代码无需维护中间状态；`C()` 是无副作用的纯函数，重复调用代价只是一点算术。另外 `train_params` 日志还要对所有 `lambda_*`（包括未启用的）再求值一次，统一走 `self.C` 最简洁。

**练习 2**：若删掉 `true_global_step`、让所有 `C()` 直接用 `self.global_step`，`--export` 模式会发生什么？
**答案**：非 fit 模式下 Lightning 的 `global_step` 从 0 开始计数，所有调度参数会按「第 0 步」取值——例如 texture 阶段的 `max_step_percent` 取到初始 0.5 而非训练结束时的 0.2，导出流程若涉及任何步数感知逻辑（如 BSD 的重画强度）就会与训练末状态不一致。`set_resume_status` 就是为堵住这个洞。

**练习 3**：哈希编码层级解锁（4.4.3 末尾的 `//` 整除版）与 `C()` 相比，各自的适用场景是什么？
**答案**：`C()` 适合连续量（权重、百分比、阈值），输出可微调地平滑变化；整除/`bisect` 换挡适合离散档位（编码层级、分辨率），本来就是整数集合上的跳变，用插值反而要再做取整。两者共享「消费 `global_step` 的 `update_step` 钩子」这一传输通道（u3-l2）。

## 5. 综合实践

**任务：为 coarse-nerf 阶段绘制一张「调度全景图」，并用自定义 resolver 交叉验证。**

1. **收集调度规格**：从 [configs/dreamcraft3d-coarse-nerf.yaml:L98-L99](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L98-L99)、[L110-L111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L110-L111)、[L135-L138](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L135-L138) 抄出全部四元组参数：`min/max_step_percent`（两条 guidance 各一对）与 `lambda_3d_normal_smooth / lambda_orient / lambda_sparsity / lambda_opaque`。
2. **画图**：对 0..3000 步采样，每个参数一条曲线，画进 2×2 子图（损失权重）+ 1 张时间步区间带图（把 4.4.4 的脚本扩展成双 guidance 对比）。在第 200 步与第 2000/2001 步画竖直参考线。
3. **读图回答**：前 200 步发生了什么（时间步区间收缩）？第 2000 步发生了什么（三项正则 100 倍级跳变 + normal_smooth 5→1 降档）？结合 u6-l4 的结论「coarse 阶跃增压」解释：为什么正则要等几何粗成形（约 2000 步）之后再加大力度？（提示：早期密度场还是一团雾，orient/sparsity 惩罚的对象尚不存在。）
4. **交叉验证**：注册 4.3.4 的 `cmax` resolver，对四个损失权重逐一求 `C_max`，与图上各曲线的最大值对拍，应逐一相等。
5. **交付物**：一张全景图 + 一段 200 字以内的调度叙事（「第 X 步之前……之后……」）。

预期：图上第 2001 步处 `lambda_orient/sparsity/opaque` 同步从 0.1/1 跳到 10，`lambda_3d_normal_smooth` 从 5 降到 1；`cmax` 输出 10、10、10、5。运行结果待本地验证。

## 6. 本讲小结

- `C()`（[misc.py:L65-L97](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L65-L97)）是全项目通用的步数感知调度引擎：标量直通、三元组补 0、四元组做截断线性插值、≥6 元素做多段接力（拐点共享、曲线连续）。
- `end_step` 的类型是隐藏开关：int 按 `global_step` 调度，float 按 `epoch` 调度；`[s, v, 1., s+1]` 的 1 步宽窗口是本仓库实现阶跃的标准惯用法。
- `C_max()`（[config.py:L31-L48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L31-L48)）取所有拐点值的最大值即「训练全程可达上界」，经 `cmaxgt0` resolver 在配置期联动 `requires_normal`、`return_comp_normal` 等数据/渲染开关。
- 运行期消费分三类：损失门控（`self.C(...) > 0`）、损失加权（`value * self.C(...)`，[dreamcraft3d.py:L321-L332](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L332)）、引导内部超参刷新（guidance 的 `update_step` 收缩扩散时间步区间）。
- 时间源必须是 `true_global_step`（[base.py:L69-L81](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L69-L81)），否则 `--export` 等恢复评估模式下调度全部错位到第 0 步。
- 自定义 resolver 必须在 `load_config` 之前注册——`launch.py` 的 `custom_import` 晚于 `OmegaConf.resolve` 执行，只能注入组件、不能注入 resolver；连续调度用 `C()`，离散换挡（编码层级、分辨率）用整除/`bisect`，两条路线共享 `update_step` 钩子。

## 7. 下一步学习建议

下一讲（u8-l2）进入 **DreamBooth 个性化与多视图数据生成**：看 `threestudio/scripts/train_dreambooth_lora.py` 如何训练专属 LoRA、`lora_weights_path` 如何替换粗阶段文生图先验——其中 DeepFloyd LoRA 的训练同样大量依赖 `C()` 驱动的学习率与时间步调度，本讲内容会直接复用。

继续深读源码的建议路径：

1. [threestudio/models/guidance/stable_diffusion_bsd_guidance.py:L1124-L1134](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py#L1124-L1134) 的 `update_step`——对照 texture 配置的 `[0, 0.5, 0.2, 5000]`，把 4.4.4 的模拟扩展到 BSD 引导。
2. [threestudio/utils/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py) 的 `do_update_step` 递归遍历——理解 `C()` 结果如何随 u3-l2 的 Updateable 钩子送达每个组件。
3. 动手实验：把 coarse 配置的 `lambda_orient` 从阶跃 `[2000, 1., 10., 2001]` 改成缓降 `[2000, 1., 10., 3000]`，低步数短跑对比渲染结果，体会「阶跃 vs 平滑」对训练稳定性的影响（需本地验证）。
